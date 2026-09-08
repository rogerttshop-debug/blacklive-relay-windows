"""
compose.py — Jeito 2: composição AO VIVO no relay (ffmpeg) e push RTMP.
=======================================================================
O painel manda a config das camadas (posição, zoom, opacidade, tipo) + as
mídias (bytes via WS) e este módulo monta um filter_complex do ffmpeg que
compõe TUDO em tempo real e empurra pro TikTok — sem passar pelo canvas do
navegador (muito mais leve e estável).

Camadas suportadas (v0.1): video, image, banner_rotation, ticker, clock, camera.
(flash/antiframe/screen = fase 2 — ignoradas com segurança por enquanto.)

Isolado do relay antigo de propósito: se algo aqui falhar, o /rtmp de sempre
não é afetado.
"""
import os
import sys
import json
import math
import time
import shutil
import tempfile
import subprocess
import threading
import urllib.request

CANVAS_W, CANVAS_H = 720, 1560   # tela cheia 9:19,5 (padrao do painel; 'classico' = 1280)


def _sub_flags():
    return 0x08000000 if sys.platform.startswith("win") else 0


def _hwaccel_args():
    """Decode do video. Mac=videotoolbox (confiavel). Windows=SOFTWARE (a-prova-de-falhas):
    o '-hwaccel auto' dava tela preta / video nao chegando em varias placas e com video
    H.265 de celular; o decode por software decoda H.264 e H.265 sempre certo (custa um
    pouco de CPU, mas nunca fica preto)."""
    if sys.platform == "darwin":
        return ["-hwaccel", "videotoolbox"]
    if sys.platform.startswith("win"):
        return []   # software decode (robusto)
    return ["-hwaccel", "auto"]


def _cam_input(device=None):
    """Entrada de câmera por plataforma (captura LOCAL — não passa pelo navegador).
    thread_queue_size grande + framerate = feed ao vivo sem engasgo/trava."""
    wc = ["-use_wallclock_as_timestamps", "1"]   # carimba o feed AO VIVO com relogio real (anti-engasgo)
    if sys.platform == "darwin":
        return ["-thread_queue_size", "1024"] + wc + ["-f", "avfoundation", "-framerate", "30",
                "-video_size", "1280x720", "-i", (device or "0")]
    if sys.platform.startswith("win"):
        return ["-thread_queue_size", "1024"] + wc + ["-f", "dshow", "-rtbufsize", "100M",
                "-i", "video=%s" % (device or "Integrated Camera")]
    return ["-thread_queue_size", "1024"] + wc + ["-f", "v4l2", "-framerate", "30",
            "-i", (device or "/dev/video0")]


def _screen_input():
    """Captura de tela LOCAL."""
    if sys.platform == "darwin":
        return ["-f", "avfoundation", "-framerate", "30", "-i", "1:none"]  # 1 = tela (varia)
    if sys.platform.startswith("win"):
        return ["-f", "gdigrab", "-framerate", "30", "-i", "desktop"]
    return ["-f", "x11grab", "-framerate", "30", "-i", ":0.0"]


def _font_path():
    """Fonte cross-platform pro drawtext (ticker/relógio)."""
    cands = [
        os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "DejaVuSans-Bold.ttf"),
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",         # Mac
        "C:\\\\Windows\\\\Fonts\\\\arialbd.ttf",                       # Windows
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",       # Linux
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    return ""


def _color(c, default="0xffffff"):
    """Converte #RRGGBB -> 0xRRGGBB (formato de cor do ffmpeg). Aceita nomes tambem."""
    c = str(c or default).strip()
    if c.startswith("#"):
        c = "0x" + c[1:]
    return c or default


def _dtxt(s):
    """Sanitiza texto do usuario p/ drawtext: tira aspas/barra/percent e escapa ':'."""
    s = str(s or "")
    s = s.replace("\\", "").replace("'", "").replace("%", "")
    s = s.replace(":", "\\:")
    return s


class ComposeSession:
    """Uma sessão de composição ao vivo. Guarda mídias em tmp, monta o ffmpeg e empurra."""

    def __init__(self, ffmpeg, log, notify=None):
        self.ffmpeg = ffmpeg
        self.log = log
        self.notify = notify or (lambda m: None)
        self.tmp = tempfile.mkdtemp(prefix="blcompose_")
        self.cw, self.ch = CANVAS_W, CANVAS_H   # dimensoes do canvas (o painel manda a altura certa)
        self.media = {}          # media_id -> caminho local do arquivo
        self.proc = None
        self._pending = None     # (media_id, ext, size, bytes_acumulados)
        self.audio_live = False  # True = áudio AO VIVO do navegador (pipe:0); False = mp3/silêncio
        self.rtmp = ""
        self.env = os.environ.copy()

    # ---- recepção de mídia (bytes vindos do painel via WS) ----
    def media_begin(self, media_id, ext, size):
        p = os.path.join(self.tmp, "m_%s.%s" % (media_id, (ext or "bin").lstrip(".")))
        self._pending = {"id": media_id, "path": p, "size": int(size), "got": 0, "fh": open(p, "wb")}

    def media_chunk(self, data):
        pend = self._pending
        if not pend:
            return False
        pend["fh"].write(data)
        pend["got"] += len(data)
        if pend["got"] >= pend["size"]:
            pend["fh"].close()
            self.media[pend["id"]] = pend["path"]
            self.log.info("[COMPOSE] midia recebida %s (%d bytes)" % (pend["id"], pend["got"]))
            self._pending = None
            return True
        return False

    def add_media_url(self, media_id, url):
        """Baixa mídia de URL do servidor (ex.: vídeo da nuvem)."""
        try:
            ext = url.split("?")[0].rsplit(".", 1)[-1][:4] or "mp4"
            p = os.path.join(self.tmp, "u_%s.%s" % (media_id, ext))
            urllib.request.urlretrieve(url, p)
            self.media[media_id] = p
            self.log.info("[COMPOSE] midia baixada %s <- %s" % (media_id, url[:60]))
        except Exception as e:
            self.log.warning("[COMPOSE] falha baixando %s: %s" % (media_id, e))

    def _dims(self, path):
        """Largura x altura nativa da midia (via ffmpeg -i, pois nao temos ffprobe). Cacheia."""
        if not hasattr(self, "_dimcache"):
            self._dimcache = {}
        if path in self._dimcache:
            return self._dimcache[path]
        import re as _re
        wh = (720, 1280)
        try:
            r = subprocess.run([self.ffmpeg, "-hide_banner", "-i", path],
                               capture_output=True, text=True, timeout=20, creationflags=_sub_flags())
            m = _re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", r.stderr)
            if m:
                wh = (int(m.group(1)), int(m.group(2)))
        except Exception:
            pass
        self._dimcache[path] = wh
        return wh

    def set_proxy(self, proxy_str):
        if not proxy_str:
            return
        parts = proxy_str.split(":")
        if len(parts) == 4:
            pu = "%s:%s@%s:%s" % (parts[2], parts[3], parts[0], parts[1])
        else:
            pu = proxy_str
        self.env["http_proxy"] = "http://" + pu
        self.env["https_proxy"] = "http://" + pu

    # ---- camadas "desenhadas" (fiel-o-suficiente ao canvas do painel) ----
    def _dt_clock(self, layer, font, CW, CH):
        """Relógio MODERNO igual ao painel: cartão verde translúcido + borda +
        'AO VIVO' com bolinha vermelha piscando + hora grande branca.
        (Cantos retos — ffmpeg não arredonda fácil; resto idêntico.)"""
        sc = float(layer.get("scale", 100) or 100) / 100.0
        ox = int(layer.get("x", 0)); oy = int(layer.get("y", 0))
        cx = CW / 2.0 + ox; cy = CH / 2.0 + oy
        bw = max(80, int(150 * sc)); bh = max(44, int(74 * sc))
        x0 = int(cx - bw / 2.0); y0 = int(cy - bh / 2.0)
        acc = _color(layer.get("cor", "#3ddc97"), "0x3ddc97")
        tcx = int(cx)
        lf = max(10, int(13 * sc))       # fonte "AO VIVO"
        tf = max(18, int(30 * sc))       # fonte da hora
        ao_y = int(cy - bh * 0.24 - lf / 2.0)
        hora_y = int(cy + bh * 0.06)
        dot = max(6, int(7 * sc))
        dot_x = int(cx - 46 * sc); dot_y = int(cy - bh * 0.24 - dot / 2.0)
        hora = "%{localtime\\:%H\\\\\\:%M}"
        parts = []
        # ---- fundo VIDRO: degrade (topo mais claro -> base mais escura) em bandas ----
        NB = 6
        a_top, a_bot = 0.44, 0.14
        for k in range(NB):
            by = int(y0 + bh * k / NB)
            bhh = int(y0 + bh * (k + 1) / NB) - by
            a = a_top + (a_bot - a_top) * (k / float(NB - 1))
            parts.append("drawbox=x=%d:y=%d:w=%d:h=%d:color=%s@%.3f:t=fill" % (x0, by, bw, bhh, acc, a))
        # brilho branco sutil no topo (sheen do vidro)
        parts.append("drawbox=x=%d:y=%d:w=%d:h=%d:color=white@0.10:t=fill"
                     % (x0 + 2, y0 + 2, bw - 4, max(2, int(bh * 0.16))))
        # borda
        parts.append("drawbox=x=%d:y=%d:w=%d:h=%d:color=%s@0.90:t=2" % (x0, y0, bw, bh, acc))
        # bolinha vermelha piscando
        parts.append("drawbox=x=%d:y=%d:w=%d:h=%d:color=red@0.95:t=fill:enable='lt(mod(t\\,1)\\,0.55)'"
                     % (dot_x, dot_y, dot, dot))
        # AO VIVO + hora
        parts.append("drawtext=fontfile='%s':text='AO VIVO':fontcolor=0xbfe9d6:fontsize=%d:x=%d-tw/2+%d:y=%d"
                     % (font, lf, tcx, int(10 * sc), ao_y))
        parts.append("drawtext=fontfile='%s':text='%s':fontcolor=white:fontsize=%d:x=%d-tw/2:y=%d"
                     % (font, hora, tf, tcx, hora_y))
        return ",".join(parts)

    def _dt_flash(self, layer, font, CW, CH):
        """Oferta Relâmpago: título + contagem regressiva MM:SS + barra encolhendo.
        (Anel/ponteiro girando do painel: simplificado — o essencial é a contagem+barra.)"""
        sc = float(layer.get("scale", 100) or 100) / 100.0
        ox = int(layer.get("x", 0)); oy = int(layer.get("y", 0))
        cx = CW / 2.0 + ox; cy = CH / 2.0 + oy
        bw = 170.0 * sc; bh = 195.0 * sc
        D = max(60, int(layer.get("dur", 600) or 600)); Ds = str(D)
        txtcol = _color(layer.get("txtCor", "#ffffff"), "0xffffff")
        acc = _color(layer.get("cor", "#ff4d2e"), "0xff4d2e")
        title = _dtxt(layer.get("text", "OFERTA TERMINA EM"))
        fpct = float(layer.get("fontPct", 100) or 100) / 100.0
        tsize = max(12, int(15 * sc * fpct))
        title_y = int(cy - bh * 0.34)
        cd_size = max(24, int(36 * sc))
        cd_y = int(cy - cd_size / 2.0)
        bar_w = max(20, int(bw * 0.86)); bar_h = max(8, int(11 * sc))
        bar_x = int(cx - bar_w / 2.0); bar_y = int(cy + bh * 0.30)
        tcx = int(cx)  # centro horizontal do texto
        # contagem regressiva (conta de verdade pelo tempo do ffmpeg)
        cd = ("%{eif\\:floor((" + Ds + "-mod(t\\," + Ds + "))/60)\\:d\\:2}"
              "\\:%{eif\\:mod(floor(" + Ds + "-mod(t\\," + Ds + "))\\,60)\\:d\\:2}")
        fill = str(bar_w) + "*(" + Ds + "-mod(t\\," + Ds + "))/" + Ds
        return (
            "drawtext=fontfile='%s':text='%s':fontcolor=%s:fontsize=%d:x=%d-tw/2:y=%d,"
            "drawbox=x=%d:y=%d:w=%d:h=%d:color=%s:t=3,"
            "drawbox=x=%d:y=%d:w=%d:h=%d:color=black@0.45:t=fill,"
            "drawbox=x=%d:y=%d:w='%s':h=%d:color=%s:t=fill,"
            "drawtext=fontfile='%s':text='%s':fontcolor=%s:fontsize=%d:x=%d-tw/2:y=%d"
        ) % (
            font, title, txtcol, tsize, tcx, title_y,
            tcx - bar_w // 2, int(title_y + tsize * 1.1), bar_w, max(2, int(2.4 * sc)), acc,
            bar_x, bar_y, bar_w, bar_h,
            bar_x, bar_y, fill, bar_h, txtcol,
            font, cd, txtcol, cd_size, tcx, cd_y,
        )

    def _antiframe_specs(self, layer, CW, CH):
        """Anti-Frame (anti-ban) FIEL ao painel: linha varrendo (alterna H/V, lenta) +
        a IMAGEM que a pessoa colocou (ou a FRASE) quicando no tamanho/opacidade dela.
        Tudo via overlay/drawtext (que ANIMAM com t; drawbox congela).
        modo: 'linha'|'palavra'|'ambos'|'imagem'|'ambos_img'."""
        op = float(layer.get("opacity", 100) or 100) / 100.0
        col = _color(layer.get("cor", "#ffffff"), "0xffffff")
        modo = str(layer.get("modo", "ambos") or "ambos")
        seed = float(layer.get("seed", 0) or 0)
        vx = abs(float(layer.get("vx", 0) or 0)) or 24.0   # px/s do painel (lento). NAO multiplicar.
        vy = abs(float(layer.get("vy", 0) or 0)) or 17.0
        sc = float(layer.get("scale", 100) or 100) / 100.0
        quer_linha = ("linha" in modo) or ("ambos" in modo)          # ambos e ambos_img
        quer_imagem = ("imagem" in modo) or ("_img" in modo)          # imagem, ambos_img
        quer_palavra = ("palavra" in modo) or (modo == "ambos")       # palavra, ambos (puro)
        specs = []
        # ---- LINHA: alterna horizontal (ciclos pares) e vertical (impares); lenta (ciclo 5s) ----
        if quer_linha:
            CYC = int(float(layer.get("velLinha", 6) or 6))   # segundos por varredura (painel escolhe)
            CYC = max(2, min(12, CYC))                          # 2=rapida ... 12=bem lenta
            la = min(1.0, 0.35 * op)
            lh = 4
            specs.append({"kind": "color", "w": CW, "h": lh, "col": col, "alpha": la,
                          "xe": "0", "ye": "H*mod(t\\,%d)/%d" % (CYC, CYC),
                          "enable": "eq(mod(floor(t/%d)\\,2)\\,0)" % CYC})
            specs.append({"kind": "color", "w": lh, "h": CH, "col": col, "alpha": la,
                          "xe": "W*mod(t\\,%d)/%d" % (CYC, CYC), "ye": "0",
                          "enable": "eq(mod(floor(t/%d)\\,2)\\,1)" % CYC})
        # ---- IMAGEM real quicando (tamanho imgTam + opacidade da camada) ----
        img_path = self.media.get(layer.get("af_img_id")) if layer.get("af_img_id") else None
        if quer_imagem and img_path:
            tam = int(layer.get("imgTam", 80) or 80)
            iw = max(8, int(tam * sc)); iw -= iw % 2
            sw, sh = self._dims(img_path)
            ih = max(8, int(iw * (float(sh) / float(sw) if sw > 0 else 1.0))); ih -= ih % 2
            specs.append({"kind": "image", "path": img_path, "w": iw, "h": ih, "alpha": min(1.0, op * 0.85),
                          "xe": "abs(mod(%.2f*t+%.1f\\,%d)-%d)" % (vx, seed, 2 * (CW - iw), CW - iw),
                          "ye": "abs(mod(%.2f*t+%.1f\\,%d)-%d)" % (vy, seed * 1.3, 2 * (CH - ih), CH - ih)})
        # ---- FRASE (texto ascii) quicando ----
        word = str(layer.get("text", "") or "")
        if quer_palavra and word and word.isascii() and any(ch.isalnum() for ch in word):
            fs = max(16, int(28 * sc))
            specs.append({"kind": "text", "text": _dtxt(word), "fontsize": fs, "col": col, "alpha": min(1.0, 0.6 * op),
                          "xe": "abs(mod(%.2f*t+%.1f\\,2*(w-tw))-(w-tw))" % (vx, seed),
                          "ye": "abs(mod(%.2f*t+%.1f\\,2*(h-th))-(h-th))" % (vy, seed * 1.3)})
        # fallback: pediu imagem/palavra mas nao rolou (emoji sem fonte, sem imagem) -> quadradinho (anti-ban)
        if (quer_palavra or quer_imagem) and not any(s["kind"] in ("image", "text") for s in specs):
            sq = 34
            specs.append({"kind": "color", "w": sq, "h": sq, "col": col, "alpha": min(1.0, 0.45 * op),
                          "xe": "abs(mod(%.2f*t+%.1f\\,%d)-%d)" % (vx, seed, 2 * (CW - sq), CW - sq),
                          "ye": "abs(mod(%.2f*t+%.1f\\,%d)-%d)" % (vy, seed * 1.3, 2 * (CH - sq), CH - sq)})
        return specs

    # ---- construção do ffmpeg ----
    def _build_cmd(self, layers, audio_path, rtmp_url, encoder="libx264"):
        CANVAS_W, CANVAS_H = self.cw, self.ch   # usa as dimensoes da sessao (720x1560 padrao)
        inputs = []       # args de -i
        fps = []          # trechos do filter que preparam cada camada -> [pN]
        idx = 0           # índice do input no ffmpeg
        prepared = []     # (ordem_no_layers, label, x, y)
        vid_aud = []      # índices de inputs de VÍDEO com áudio p/ tocar na live (audioModo != mudo)

        def prep_scale(i, layer, in_label, dims):
            """Tamanho IGUAL ao painel (linha 461-473): baseado no ASPECTO NATIVO da midia.
            ratio=w/h; ajusta 720 de largura; no modo tela-cheia cobre a altura do canvas.
            O excesso (quando maior que a tela) e cortado pela propria tela no overlay."""
            src_w, src_h = dims
            if src_w <= 0 or src_h <= 0:
                src_w, src_h = 720, 1280
            ratio = float(src_w) / float(src_h)
            baseW = 720.0
            baseH = 720.0 / ratio
            _t = layer.get("type", "")
            CH = float(CANVAS_H)   # 1560 (tela cheia) / 1280 (classico)
            cobrir = (_t in ("video", "image", "screen")) and not layer.get("crop")
            if cobrir:
                if baseH < CH:
                    baseH = CH; baseW = CH * ratio
            elif baseH > CH:
                baseH = CH; baseW = CH * ratio
            sc = float(layer.get("scale", 100)) / 100.0
            bw = max(2, int(baseW * sc)); bw -= bw % 2
            bh = max(2, int(baseH * sc)); bh -= bh % 2
            op = float(layer.get("opacity", 100)) / 100.0
            rot = float(layer.get("rotation", 0) or 0)
            # scale exato: bw:bh ja preserva o aspecto (sem distorcao); sobra sai da tela no overlay
            geo = "scale=%d:%d:flags=fast_bilinear" % (bw, bh)
            needs_alpha = (_t in ("image", "banner_rotation")) or (op < 0.999) or (abs(rot) > 0.5)
            fmt = ",format=rgba" if needs_alpha else ""
            f = "[%s]fps=30%s,%s" % (in_label, fmt, geo)
            if op < 0.999:
                f += ",colorchannelmixer=aa=%.3f" % op
            if abs(rot) > 0.5:
                f += ",rotate=%.4f:c=black@0.0:ow=rotw(%.4f):oh=roth(%.4f)" % (
                    rot * math.pi / 180.0, rot * math.pi / 180.0, rot * math.pi / 180.0)
            f += "[p%d]" % i
            return f, bw, bh

        font = _font_path()
        for i, layer in enumerate(layers):
            t = layer.get("type", "")
            mid = layer.get("media_id")
            path = self.media.get(mid) if mid else None
            dims = (720, 1280)   # fallback

            if t in ("video",) and path:
                inputs += ["-stream_loop", "-1"] + _hwaccel_args() + ["-re", "-i", path]  # decode na placa
                dims = self._dims(path)
                # áudio do vídeo entra na live se NÃO estiver mudo (audioModo 'live'/'ambos')
                _am = str(layer.get("audioModo") or ("mudo" if layer.get("muted", True) else "ambos"))
                if _am != "mudo":
                    vid_aud.append(idx)
            elif t in ("image",) and path:
                inputs += ["-loop", "1", "-framerate", "30", "-i", path]
                dims = self._dims(path)
            elif t == "banner_rotation":
                imgs = [self.media.get(m) for m in (layer.get("media_ids") or []) if self.media.get(m)]
                if not imgs:
                    prepared.append(None); continue
                inputs += ["-loop", "1", "-framerate", "30", "-i", imgs[0]]  # v0.1: 1a imagem
                dims = self._dims(imgs[0])
            elif t == "camera":
                inputs += _cam_input(layer.get("device"))
                dims = (1280, 720)   # webcam tipica 16:9
            elif t == "screen":
                inputs += _screen_input()
                dims = (1920, 1080)  # tela tipica 16:9
            elif t in ("ticker", "clock", "flash"):
                # desenhados com drawtext/drawbox direto sobre o fundo (sem input próprio)
                prepared.append(("drawtext", i, layer)); continue
            elif t == "antiframe":
                # anti-ban: elementos que SE MOVEM (overlay/drawtext animam com t; drawbox congela)
                for sp in self._antiframe_specs(layer, CANVAS_W, CANVAS_H):
                    if sp["kind"] == "color":
                        inputs += ["-f", "lavfi", "-i", "color=c=%s:s=%dx%d:r=30" % (sp["col"], sp["w"], sp["h"])]
                        fps.append("[%d:v]format=rgba,colorchannelmixer=aa=%.3f[p%d]" % (idx, sp["alpha"], idx))
                        prepared.append(("overlay_expr", i, "[p%d]" % idx, sp["xe"], sp["ye"], sp.get("enable")))
                        idx += 1
                    elif sp["kind"] == "image":
                        inputs += ["-loop", "1", "-framerate", "30", "-i", sp["path"]]
                        fps.append("[%d:v]format=rgba,scale=%d:%d:flags=fast_bilinear,colorchannelmixer=aa=%.3f[p%d]"
                                   % (idx, sp["w"], sp["h"], sp["alpha"], idx))
                        prepared.append(("overlay_expr", i, "[p%d]" % idx, sp["xe"], sp["ye"], sp.get("enable")))
                        idx += 1
                    elif sp["kind"] == "text":
                        prepared.append(("drawtext_expr", i, sp))
                continue
            else:
                prepared.append(None); continue   # roulette/screen live = fase 2

            f, sw, sh = prep_scale(idx, layer, "%d:v" % idx, dims)
            fps.append(f)
            off_x = int(layer.get("x", 0)); off_y = int(layer.get("y", 0))
            x = int(CANVAS_W / 2 - sw / 2 + off_x)
            y = int(CANVAS_H / 2 - sh / 2 + off_y)
            prepared.append(("overlay", i, "[p%d]" % idx, x, y))
            idx += 1

        # fundo preto (base) + áudio
        inputs += ["-f", "lavfi", "-i", "color=c=black:s=%dx%d:r=30" % (CANVAS_W, CANVAS_H)]
        bg = idx
        if getattr(self, "audio_live", False):
            # áudio AO VIVO do navegador (mix do painel: blocos+mic+efeitos) via stdin
            inputs += ["-thread_queue_size", "512", "-use_wallclock_as_timestamps", "1", "-f", "webm", "-i", "pipe:0"]
        else:
            inputs += ["-stream_loop", "-1", "-re", "-i", audio_path]   # narração em loop, real-time
        aud = idx + 1

        chain = ["[%d:v]null[base0]" % bg]
        cur = "[base0]"; n = 0
        for item in prepared:
            if not item:
                continue
            kind = item[0]
            if kind == "overlay":
                _, i, lbl, x, y = item
                nxt = "[base%d]" % (n + 1)
                chain.append("%s%soverlay=x=%d:y=%d:eof_action=pass%s" % (cur, lbl, x, y, nxt))
                cur = nxt; n += 1
            elif kind == "overlay_expr":
                _, i, lbl, xe, ye, en = item
                nxt = "[base%d]" % (n + 1)
                en_s = (":enable='%s'" % en) if en else ""
                chain.append("%s%soverlay=x='%s':y='%s':eof_action=pass%s%s" % (cur, lbl, xe, ye, en_s, nxt))
                cur = nxt; n += 1
            elif kind == "drawtext_expr" and font:
                _, i, sp = item
                nxt = "[base%d]" % (n + 1)
                dt = ("drawtext=fontfile='%s':text='%s':fontcolor=%s@%.3f:fontsize=%d:x='%s':y='%s'"
                      % (font, sp["text"], sp["col"], sp["alpha"], sp["fontsize"], sp["xe"], sp["ye"]))
                chain.append("%s%s%s" % (cur, dt, nxt))
                cur = nxt; n += 1
            elif kind == "drawtext" and font:
                _, i, layer = item
                nxt = "[base%d]" % (n + 1)
                lt = layer.get("type")
                if lt == "clock":
                    dt = self._dt_clock(layer, font, CANVAS_W, CANVAS_H)
                elif lt == "flash":
                    dt = self._dt_flash(layer, font, CANVAS_W, CANVAS_H)
                else:  # ticker
                    raw = str(layer.get("text", "PROMOCAO")).replace("'", "").replace(":", "\\:")
                    rep = "   ---   %s   ---   %s" % (raw, raw)
                    yb = int(CANVAS_H / 2 + layer.get("y", 0))
                    dt = ("drawbox=x=0:y=%d:w=%d:h=64:color=0xdc2626@0.9:t=fill,"
                          "drawtext=fontfile='%s':text='%s':fontcolor=white:fontsize=34:"
                          "x='w-mod(t*140\\,w+tw)':y=%d" % (yb, CANVAS_W, font, rep, yb + 15))
                chain.append("%s%s%s" % (cur, dt, nxt))
                cur = nxt; n += 1

        # renomeia a última saída pra [outv]
        if chain:
            last = chain[-1]
            chain[-1] = last[:last.rfind("[base")] + "[outv]"
        else:
            chain = ["[%d:v]null[outv]" % bg]
        # ÁUDIO: navegador/mp3 (aud) + áudio dos vídeos NÃO-mudos (vid_aud) -> amix p/ [outa]
        asrcs = ["[%d:a]" % aud] + ["[%d:a]" % v for v in vid_aud]
        if len(asrcs) >= 2:
            achain = ("%samix=inputs=%d:duration=longest:normalize=0,aresample=async=1[outa]"
                      % ("".join(asrcs), len(asrcs)))
        else:
            achain = "[%d:a]aresample=async=1[outa]" % aud
        # CRITICO: os filtros de preparo (fps) DEFINEM os [pN] que o chain usa no overlay.
        filt = ";".join(fps + chain + [achain])

        vc = (["-c:v", encoder, "-b:v", "4500k", "-maxrate", "4500k", "-bufsize", "9000k",
               "-pix_fmt", "yuv420p", "-g", "60", "-keyint_min", "60"]
              if encoder != "libx264" else
              ["-c:v", "libx264", "-preset", "veryfast", "-b:v", "3500k",
               "-pix_fmt", "yuv420p", "-g", "60"])

        return [self.ffmpeg, "-hide_banner", "-loglevel", "warning",
                "-fflags", "+genpts+discardcorrupt", "-thread_queue_size", "1024",
                *inputs,
                "-filter_complex", filt,
                "-map", "[outv]", "-map", "[outa]",
                *vc,
                "-fps_mode", "cfr", "-r", "30",   # cadencia CONSTANTE 30fps (anti-engasgo da camera ao vivo)
                "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
                "-user_agent", "TikTokLiveStudio/0.46.1",
                "-metadata", "title=TikTok Live Studio",
                "-metadata", "encoder=TikTok Live Studio 0.46.1",
                "-f", "flv", rtmp_url]

    def _reporta_ffmpeg(self, logf, rtmp):
        """Envia o final do log do ffmpeg do compose pro servidor (diagnostico remoto, chave mascarada)."""
        try:
            import re as _re
            time.sleep(6)
            tail = ""
            try:
                with open(logf, "r", errors="replace") as f:
                    tail = f.read()[-2500:]
            except Exception:
                pass
            tail = _re.sub(r"rtmp[s]?://[^\s'\"]+", "rtmp://<masked>", tail)   # nao vaza a chave RTMP
            alive = bool(self.proc and self.proc.poll() is None)
            rc = (self.proc.poll() if self.proc else None)
            host = ""
            try:
                host = _re.sub(r"rtmp[s]?://", "", rtmp).split("/")[0]
            except Exception:
                pass
            body = json.dumps({"evt": "COMPOSE_FFMPEG", "cam": "COMPOSE",
                               "extra": "host=%s vivo=%s rc=%s :: %s" % (host, alive, rc, tail[-1500:])}).encode()
            req = urllib.request.Request("https://blacklive.com.br/api/debug/video", data=body,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=8)
        except Exception:
            pass

    def start(self, layers, audio_path, rtmp_url, encoder="libx264"):
        self.stop()
        self.rtmp = rtmp_url
        cmd = self._build_cmd(layers, audio_path, rtmp_url, encoder)
        logf = os.path.join(os.path.expanduser("~"), ".blacklive_compose.log")
        self.log.info("[COMPOSE] iniciando composicao ao vivo (%d camadas)" % len(layers))
        try:
            self.log.info("[COMPOSE] TIPOS recebidos: %s" % ([l.get("type") for l in layers]))
        except Exception:
            pass
        _stdin = subprocess.PIPE if getattr(self, "audio_live", False) else subprocess.DEVNULL
        self.proc = subprocess.Popen(cmd, stdin=_stdin,
                                     stdout=open(logf, "w"), stderr=open(logf, "a"),
                                     env=self.env, creationflags=_sub_flags())
        try:
            threading.Thread(target=self._reporta_ffmpeg, args=(logf, rtmp_url), daemon=True).start()
        except Exception:
            pass
        return self.proc.pid

    def audio_write(self, data):
        """Recebe um pedaço de áudio AO VIVO (webm/opus) do navegador e joga no ffmpeg (pipe:0)."""
        try:
            if self.proc and self.proc.stdin and self.proc.poll() is None:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
                return True
        except Exception:
            pass
        return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate(); time.sleep(0.3); self.proc.kill()
            except Exception:
                pass
        self.proc = None

    def cleanup(self):
        self.stop()
        try:
            shutil.rmtree(self.tmp, ignore_errors=True)
        except Exception:
            pass
