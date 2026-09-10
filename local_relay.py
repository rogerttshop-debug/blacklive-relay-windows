#!/usr/bin/env python3
"""
BlackLive Local Relay — v1.2
============================
Roda em background no computador do usuário.
Recebe o stream do browser (WebM via WebSocket) e envia via RTMP
usando o IP LOCAL da máquina — não o IP do servidor.

Novidades v1.2:
  - FFmpeg bundled (não precisa instalar separadamente)
  - Auto-update automático via VPS a cada abertura
  - Rota /render para renderizar MP4 localmente e enviar ao VPS
"""

import asyncio
import subprocess
import sys
import os
import json
import signal
import time
import urllib.parse
import urllib.request
import logging
import threading

PORT    = 8902
VERSION = "1.9.0"
VPS_URL = "https://blacklive.com.br"

ALLOWED_ORIGINS = {
    "https://blacklive.com.br",
    "http://blacklive.com.br",
    "http://localhost:8900",
    "http://127.0.0.1:8900",
}

# ── FIX Chrome 152+ ("Private Network Access") ────────────────────────────────
# O Chrome novo manda um preflight OPTIONS antes de deixar o site (https) falar
# com o app local (ws://127.0.0.1). A lib websockets so aceita GET e derrubava o
# preflight -> "Nao consegui falar com o app Black Live!" mesmo com a permissao
# "Acesso a rede local" liberada. Patch: aceita OPTIONS e responde autorizando
# SOMENTE as nossas origens (ALLOWED_ORIGINS). Validado em teste local 06/09.
def _instala_fix_pna():
    try:
        import websockets.http11 as _h11
        def _parse_allow_options(cls, read_line):
            # copia do Request.parse da websockets 15.0.1, aceitando OPTIONS
            try:
                request_line = yield from _h11.parse_line(read_line)
            except EOFError as exc:
                raise EOFError("connection closed while reading HTTP request line") from exc
            try:
                method, raw_path, protocol = request_line.split(b" ", 2)
            except ValueError:
                raise ValueError("invalid HTTP request line") from None
            if protocol != b"HTTP/1.1":
                raise ValueError("unsupported protocol; expected HTTP/1.1")
            if method not in (b"GET", b"OPTIONS"):
                raise ValueError(f"unsupported HTTP method: {method!r}")
            path = raw_path.decode("ascii", "surrogateescape")
            headers = yield from _h11.parse_headers(read_line)
            if "Transfer-Encoding" in headers:
                raise NotImplementedError("transfer codings aren't supported")
            if "Content-Length" in headers and not (method == b"OPTIONS" and headers["Content-Length"] == "0"):
                raise ValueError("unsupported request body")
            req = cls(path, headers)
            if method == b"OPTIONS":
                headers["X-BL-Preflight"] = "1"
            return req
        _h11.Request.parse = classmethod(_parse_allow_options)
        log.info("Fix PNA (Chrome 152+) instalado: preflight OPTIONS aceito")
    except Exception as e:
        log.warning(f"Fix PNA nao instalado ({e}) — relay segue como antes")

def pna_process_request(connection, request):
    """Responde o preflight do Chrome autorizando rede local (so p/ nossas origens)."""
    try:
        h = request.headers
        if h.get("X-BL-Preflight") == "1" or h.get("Access-Control-Request-Private-Network") == "true":
            origin = h.get("Origin", "")
            resp = connection.respond(200, "")
            if origin in ALLOWED_ORIGINS:
                resp.headers["Access-Control-Allow-Origin"] = origin
                resp.headers["Access-Control-Allow-Private-Network"] = "true"
                resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
                resp.headers["Access-Control-Allow-Headers"] = "*"
                resp.headers["Access-Control-Max-Age"] = "86400"
            return resp
    except Exception as e:
        log.warning(f"pna_process_request erro: {e}")
    return None

# ── FFmpeg bundled via imageio_ffmpeg ─────────────────────────────────────────
def get_ffmpeg():
    """Retorna o path do FFmpeg bundled. Fallback para o do sistema."""
    try:
        import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
        if path and os.path.isfile(path):
            return path
    except Exception:
        pass
    return "ffmpeg"

FFMPEG = get_ffmpeg()

# ── Logging ───────────────────────────────────────────────────────────────────
log_path = os.path.join(os.path.expanduser("~"), ".blacklive_relay.log")
logging.basicConfig(
    filename=log_path,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("relay")

# ── Auto-update ────────────────────────────────────────────────────────────────
def check_update():
    """Auto-update desativado — versão de teste com c:v copy."""
    log.info(f"Relay v{VERSION} — auto-update desativado (binario nao se auto-atualiza)")
    return
    try:
        url = f"{VPS_URL}/local_relay.py"
        req = urllib.request.Request(url, headers={"User-Agent": f"BlackLive-Relay/{VERSION}"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            new_code = resp.read().decode("utf-8")

        remote_version = ""
        for line in new_code.splitlines():
            if line.strip().startswith("VERSION") and "=" in line and '"' in line:
                remote_version = line.split('"')[1]
                break

        if remote_version and remote_version != VERSION:
            self_path = os.path.abspath(__file__)
            with open(self_path, "w", encoding="utf-8") as f:
                f.write(new_code)
            log.info(f"Auto-update: {VERSION} → {remote_version}. Reiniciando...")
            os.execv(sys.executable, [sys.executable] + sys.argv)
        else:
            log.info(f"Auto-update: v{VERSION} já é a mais recente.")
    except Exception as e:
        log.warning(f"Auto-update falhou: {e}")

# ── Handler principal WebSocket ────────────────────────────────────────────────
async def handle(websocket):
    try:
        origin = websocket.request.headers.get("Origin", "")
    except Exception:
        origin = ""

    if origin not in ALLOWED_ORIGINS:
        log.warning(f"Conexao recusada — origem nao autorizada: {origin!r}")
        await websocket.close(1008, "Unauthorized")
        return

    try:
        path = websocket.request.path
    except AttributeError:
        try:
            path = websocket.path
        except AttributeError:
            path = "/"

    parsed = urllib.parse.urlparse(path)
    qs     = urllib.parse.parse_qs(parsed.query)

    # /ping — health check
    if parsed.path == "/ping":
        await websocket.send(json.dumps({
            "status": "ok",
            "version": VERSION,
            "ffmpeg": FFMPEG,
            "ip": "local",
            "modo_leve": {"armado": bool(MODO_LEVE["video"]),
                          "no_ar": MODO_LEVE["ativo"],
                          "encoder": MODO_LEVE["encoder"]}
        }))
        return

    # /render — renderiza MP4 localmente e faz upload pro VPS
    if parsed.path == "/render":
        await _handle_render(websocket)
        return

    # /compose — Jeito 2: composição AO VIVO no relay (câmera+layers) e push direto
    if parsed.path == "/compose":
        await _handle_compose(websocket)
        return

    # /leve — controle do MODO LEVE pelo PAINEL (29/08): o botao fica na pagina,
    # o relay abre o seletor de arquivo na maquina. Sem caçar icone de bandeja.
    if parsed.path == "/leve":
        try:
            msg = await asyncio.wait_for(websocket.recv(), timeout=10)
            _dados = json.loads(msg)
            cmd = _dados.get("cmd", "")
        except Exception:
            _dados = {}
            cmd = "status"
        if cmd == "escolher":
            loop = asyncio.get_event_loop()
            path = await loop.run_in_executor(None, leve_escolher_video)
            await websocket.send(json.dumps({"ok": bool(path),
                "video": os.path.basename(path) if path else None}))
        elif cmd == "ligar":
            # 1 CLIQUE (v1.8.3): o painel manda a chave direto — igual aos outros modos,
            # sem o "transmitir e clicar PARAR". O takeover antigo continua como fallback.
            rtmp = str(_dados.get("rtmp", "")).strip()
            if not (MODO_LEVE["video"] and os.path.isfile(MODO_LEVE["video"])):
                await websocket.send(json.dumps({"ok": False, "erro": "sem_video"}))
            elif not rtmp.startswith("rtmp"):
                await websocket.send(json.dumps({"ok": False, "erro": "chave_invalida"}))
            else:
                # escolha de audio: "video" (audio do arquivo), "mudo" (silencio) ou
                # "aovivo" (blocos/picotador/mic/narracao do navegador via este WS)
                _amode = str(_dados.get("audio", "")).strip()
                if _amode not in ("mudo", "aovivo", "ambos"):
                    _amode = "video"
                MODO_LEVE["audio"] = _amode
                if _amode in ("aovivo", "ambos"):
                    import queue as _queue
                    myq = _queue.Queue(maxsize=400)   # ~cap: dropa o mais velho, nunca infla
                    MODO_LEVE["audio_q"] = myq
                    MODO_LEVE["audio_hdr"] = None     # sessao nova = cabecalho webm novo
                    MODO_LEVE["audio_ws_vivo"] = True
                ok_l = leve_iniciar(rtmp)
                if ok_l:
                    log.info("[LEVE] ligado pelo painel (1 clique, audio=%s)" % _amode)
                await websocket.send(json.dumps({"ok": bool(ok_l),
                    "video": os.path.basename(MODO_LEVE["video"])}))
                if ok_l and _amode in ("aovivo", "ambos"):
                    # MANTEM o WS aberto recebendo o audio ao vivo do navegador.
                    # O 1o chunk e o CABECALHO webm — guarda separado (o writer escreve
                    # ele primeiro em CADA ffmpeg, inclusive na religa).
                    try:
                        async for amsg in websocket:
                            if isinstance(amsg, (bytes, bytearray)):
                                b = bytes(amsg)
                                if MODO_LEVE.get("audio_hdr") is None:
                                    MODO_LEVE["audio_hdr"] = b
                                    continue
                                try:
                                    myq.put_nowait(b)
                                except Exception:
                                    try:
                                        myq.get_nowait(); myq.put_nowait(b)  # dropa o mais velho
                                    except Exception:
                                        pass
                    except Exception:
                        pass
                    finally:
                        MODO_LEVE["audio_ws_vivo"] = False
                        # navegador saiu -> cai pro audio do proprio video pra live NAO morrer
                        if MODO_LEVE["ativo"] and not MODO_LEVE["stop"] and MODO_LEVE.get("audio") in ("aovivo", "ambos"):
                            log.info("[LEVE] navegador saiu do audio ao vivo -> fallback p/ audio do video")
                            MODO_LEVE["audio"] = "video"
                            try: myq.put_nowait(None)
                            except Exception: pass
                            try: leve_iniciar(MODO_LEVE["rtmp"] or rtmp)
                            except Exception: pass
                    return
        elif cmd == "desligar":
            leve_desligar()
            await websocket.send(json.dumps({"ok": True, "video": None}))
        else:
            await websocket.send(json.dumps({"ok": True,
                "armado": bool(MODO_LEVE["video"]),
                "video": os.path.basename(MODO_LEVE["video"]) if MODO_LEVE["video"] else None,
                "no_ar": MODO_LEVE["ativo"], "encoder": MODO_LEVE["encoder"]}))
        return

    # /rtmp — relay de stream ao vivo (câmera → TikTok)
    if "rtmp" not in qs:
        await websocket.send(json.dumps({"error": "rtmp param missing"}))
        return

    await _handle_rtmp(websocket, qs)


# ── Relay ao vivo: WebM → FFmpeg → RTMP ───────────────────────────────────────
def _q_auto_off_path():
    return os.path.join(os.path.expanduser("~"), ".bl_relay_qualidade.auto_off")

# ── v1.9.0 ADAPTATIVO: degraus de qualidade (filosofia OBS/Live Studio) ───────
# 0=ALTA (1080p/4500k)  1=MEDIA (720p/2500k)  2=ESTAVEL (copy, repassa).
# Regra de ouro: sob pressao, DESCE um degrau — a live NUNCA fecha por qualidade.
# O degrau descido fica gravado por 24h (no dia seguinte re-tenta o de cima).
TIERS = {0: "ALTA 1080p/4500k", 1: "MEDIA 720p/2500k", 2: "ESTAVEL (copy)"}

def _tier_path():
    return os.path.join(os.path.expanduser("~"), ".bl_relay_tier")

def _tier_ler():
    try:
        d = json.load(open(_tier_path()))
        if time.time() - float(d.get("ts", 0)) < 86400 and int(d.get("tier", 0)) in (1, 2):
            return int(d["tier"])
    except Exception:
        pass
    return None

def _tier_gravar(t):
    try:
        json.dump({"tier": int(t), "ts": time.time()}, open(_tier_path(), "w"))
    except Exception:
        pass

def _tele(ev, extra=""):
    """v1.9.0 TESTE: manda eventos-chave pro servidor (a URL fica no access log —
    mesmo truque do pictest/hbtick). A gente ve remotamente se o app novo esta
    instalado e adaptando, sem pedir log pro aluno. Nao-bloqueante; falha e muda."""
    def _go():
        try:
            try:
                import socket; pc = socket.gethostname()[:32]
            except Exception:
                pc = "?"
            q = urllib.parse.urlencode({"v": VERSION, "ev": ev, "x": str(extra)[:120], "pc": pc})
            # http (nao https): o app empacotado pode nao ter cert SSL; o nginx loga a
            # requisicao no access log mesmo redirecionando. urllib do topo (sem re-import).
            urllib.request.urlopen("http://blacklive.com.br/api/ext/relaylog?" + q, timeout=5)
        except Exception as e:
            try: log.warning(f"[tele] falhou {ev}: {type(e).__name__}: {e}")
            except Exception: pass
    threading.Thread(target=_go, daemon=True).start()

def _ffmpeg_erro_tail():
    """Ultimas linhas UTEIS do log do ffmpeg (sem frame=) — vai junto na telemetria
    quando o motor morre, pra vermos O ERRO remotamente sem pedir log ao aluno."""
    try:
        p = os.path.join(os.path.expanduser("~"), ".blacklive_ffmpeg.log")
        data = open(p, "rb").read()[-4000:].decode("utf-8", "replace").replace("\r", "\n")
        ln = [l.strip() for l in data.split("\n") if l.strip() and not l.startswith("frame=")]
        return " | ".join(ln[-2:])[:200]
    except Exception:
        return ""

def _qualidade_benchmark(enc):
    """v1.6.3: mede se a maquina AGUENTA reencodar 1080p em tempo real (3s de teste
    sintetico com o MESMO filtro/bitrate da transmissao). Precisa de folga (>=1.6x
    tempo real) pra nao engasgar ao vivo junto com navegador+canvas."""
    try:
        t0 = time.time()
        r = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=30",
             "-t", "3",
             "-vf", "scale=1080:-2:flags=bicubic",
             "-c:v", enc, "-b:v", "4500k",
             "-f", "null", "-"],
            capture_output=True, timeout=60, creationflags=_sub_flags())
        dt = time.time() - t0
        ok = (r.returncode == 0) and (dt > 0) and (3.0 / dt >= 1.6)
        log.info(f"[QUALIDADE] benchmark {enc}: 3s codificados em {dt:.1f}s ({3.0/max(dt,0.001):.1f}x) -> {'QUALIDADE 1080p' if ok else 'modo leve (copy)'}")
        return ok
    except Exception as e:
        log.warning(f"[QUALIDADE] benchmark falhou ({e}) -> modo leve (copy)")
        return False

def _tier_atual(q_forcado=None):
    """v1.9.0: decide o DEGRAU da transmissao. Ordem de prioridade:
    .off manual > pedido do painel (q=alta/estavel) > degrau adaptativo (24h) >
    .auto_off legado > cache do benchmark > benchmark."""
    off = os.path.join(os.path.expanduser("~"), ".bl_relay_qualidade.off")
    if os.path.exists(off):
        return 2, None, "desligado manualmente (.off)"
    enc = leve_detectar_encoder()
    if not enc or enc == "libx264":
        return 2, None, "sem encoder de hardware"
    if q_forcado == "estavel":
        return 2, enc, "painel pediu ESTAVEL"
    if q_forcado == "alta":
        return 0, enc, "painel pediu ALTA"
    t = _tier_ler()
    if t is not None:
        return t, enc, "degrau adaptativo (ultimas 24h)"
    if os.path.exists(_q_auto_off_path()):
        return 2, enc, "fallback automatico legado (.auto_off)"
    cache = os.path.join(os.path.expanduser("~"), ".bl_relay_qualidade.cache")
    try:
        c = json.load(open(cache))
        if c.get("enc") == enc and c.get("modo") in ("hq", "copy"):
            return (0 if c["modo"] == "hq" else 2), enc, "cache do benchmark"
    except Exception:
        pass
    modo = "hq" if _qualidade_benchmark(enc) else "copy"
    try:
        json.dump({"enc": enc, "modo": modo}, open(cache, "w"))
    except Exception:
        pass
    return (0 if modo == "hq" else 2), enc, "benchmark"

def _build_tx_cmd(rtmp_url, q_forcado=None):
    """Monta o comando ffmpeg da transmissao. Retorna (cmd, tier).
    v1.9.0 ADAPTATIVO: 3 degraus (0=1080p/4500k, 1=720p/2500k, 2=copy). O degrau
    vem de _tier_atual(); sob pressao ao vivo o _handle_rtmp DESCE um degrau e
    religa — a live nao fecha por qualidade. Desliga tudo com ~/.bl_relay_qualidade.off."""
    base_meta = [
        "-user_agent", "TikTokLiveStudio/0.46.1",
        "-metadata", "title=TikTok Live Studio",
        "-metadata", "encoder=TikTok Live Studio 0.46.1",
    ]
    tier, enc, motivo = _tier_atual(q_forcado)
    if tier in (0, 1):
        # Degrau 0 = 1080p/4500k; degrau 1 = 720p/2500k (alivia encoder E upload).
        # keyframe 2s (30fps -> g=60), bitrate estavel.
        KBPS = 4500 if tier == 0 else 2500
        ESCALA = "1080:-2" if tier == 0 else "720:-2"
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "warning",
               "-fflags", "+genpts+discardcorrupt",
               "-f", "webm", "-i", "pipe:0",
               "-vf", "scale=%s:flags=bicubic" % ESCALA,
               "-r", "30",
               "-c:v", enc,
               "-b:v", "%dk" % KBPS, "-maxrate", "%dk" % KBPS, "-bufsize", "%dk" % (KBPS * 2),
               "-g", "60", "-keyint_min", "60",
               "-pix_fmt", "yuv420p"]
        if enc == "h264_videotoolbox":
            cmd += ["-realtime", "1", "-profile:v", "high"]
        elif enc == "h264_nvenc":
            cmd += ["-rc", "cbr", "-preset", "p4", "-tune", "ll", "-profile:v", "high"]
        elif enc == "h264_qsv":
            cmd += ["-profile:v", "high"]
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100"]
        cmd += base_meta + ["-f", "flv", rtmp_url]
        log.info(f"[QUALIDADE] degrau {tier} ({TIERS[tier]}) via {enc} ({motivo})")
        return cmd, tier
    # Degrau 2: repasse original (nao mexe na CPU)
    log.info(f"[QUALIDADE] degrau 2 ({TIERS[2]}) — {motivo}")
    return [FFMPEG, "-hide_banner", "-loglevel", "warning",
            "-fflags", "+genpts+discardcorrupt",
            "-f", "webm", "-i", "pipe:0",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100"] + base_meta + ["-f", "flv", rtmp_url], 2


async def _handle_rtmp(websocket, qs):
    rtmp_url  = qs["rtmp"][0]
    proxy_url = qs.get("proxy", [None])[0]

    # [LEVE] navegador vai transmitir: se o modo leve estiver no ar, sai de cena
    # (dois pushers na mesma chave brigam)
    if MODO_LEVE["ativo"]:
        log.info("[LEVE] navegador reassumiu — parando o push leve")
        leve_parar_push()

    log.info(f"Relay ao vivo → {rtmp_url.split('?')[0]}...")

    env = os.environ.copy()
    if proxy_url:
        parts = proxy_url.split(":")
        if len(parts) == 4:
            proxy_url = f"{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        env["http_proxy"]  = f"http://{proxy_url}"
        env["https_proxy"] = f"http://{proxy_url}"

    ffmpeg_log = os.path.join(os.path.expanduser("~"), ".blacklive_ffmpeg.log")
    q_forcado = qs.get("q", [None])[0]              # painel pode mandar q=alta / q=estavel
    ffmpeg_cmd, _tx_tier = _build_tx_cmd(rtmp_url, q_forcado)

    def _degrau_abaixo(motivo_txt):
        """v1.9.0: desce UM degrau de qualidade (0->1->2) e persiste por 24h.
        Retorna True se desceu (o chamador mata o ffmpeg e religa)."""
        if q_forcado in ("alta", "estavel"):
            return False                             # painel fixou: nao mexe
        if _tx_tier >= 2:
            return False                             # ja esta no copy: nada a descer
        _tier_gravar(_tx_tier + 1)
        log.warning(f"[ADAPT] {motivo_txt} — descendo p/ degrau {_tx_tier+1} ({TIERS[_tx_tier+1]}); religa ja vem no degrau novo")
        _tele("adapt_desce", f"de={_tx_tier} para={_tx_tier+1} {motivo_txt}")
        _notify("⚙️ Ajustei a qualidade pra manter a live estável (religa sozinha)")
        return True

    # v1.6.1: escrita via FILA + thread (nunca trava o relay). Se o ffmpeg ENTALA
    # (vivo mas surdo), a fila enche em ~20s -> mata e fecha a conexao NA HORA.
    # Antes, o relay travava junto e o navegador acumulava ~20MB/min de memoria
    # ate estourar (comprovado 28/08 — caso juliana "Out of Memory de madrugada").
    import queue as _q
    fila = _q.Queue(maxsize=20)   # ~20s de video; ~6MB no pior caso

    def _writer(p, f):
        while True:
            item = f.get()
            if item is None:
                break
            try:
                p.stdin.write(item)
                p.stdin.flush()
            except Exception:
                break   # pipe fechado/quebrado: o loop principal percebe pelo poll()

    try:
        proc = subprocess.Popen(
            ffmpeg_cmd,
            stdin=subprocess.PIPE,
            stdout=open(ffmpeg_log, "w"),
            stderr=open(ffmpeg_log, "a"),
            env=env,
            creationflags=_sub_flags()   # Windows: sem janela preta
        )
        threading.Thread(target=_writer, args=(proc, fila), daemon=True).start()
        log.info("FFmpeg iniciado (local → TikTok)")
        _tele("tx_start", f"tier={_tx_tier} key={rtmp_url.split('stream-')[-1].split('?')[0][:22]}")
        await websocket.send(json.dumps({"status": "streaming", "ip": "local"}))

        _press0 = None    # v1.9.0: inicio da pressao sustentada na fila (sensor do adaptativo)
        async for msg in websocket:
            if isinstance(msg, bytes):
                if proc.poll() is not None:
                    log.warning("FFmpeg morreu — fechando a conexao p/ o painel religar")
                    _tele("ffmpeg_morreu", _ffmpeg_erro_tail())   # o ERRO real vai junto
                    break
                # v1.9.0 ADAPT: fila >=10 (~10s de atraso) sustentada por >=6s = este degrau
                # nao esta escoando -> desce ANTES de entalar de vez (OBS/Live Studio:
                # baixa a qualidade, nunca derruba a live).
                _qsz = fila.qsize()
                if _qsz >= 10 and _tx_tier < 2:
                    if _press0 is None:
                        _press0 = time.time()
                    elif time.time() - _press0 >= 6:
                        if _degrau_abaixo(f"pressao sustentada (fila={_qsz} por 6s+)"):
                            try: proc.kill()
                            except Exception: pass
                            break
                else:
                    _press0 = None
                try:
                    fila.put_nowait(msg)
                except _q.Full:
                    log.warning("FFmpeg ENTALADO (fila cheia ~20s) — matando e fechando p/ religar")
                    _tele("entalo", f"tier={_tx_tier} {_ffmpeg_erro_tail()}")
                    _degrau_abaixo("entalo (fila cheia)")   # se desceu, o religa ja vem mais leve
                    try: proc.kill()
                    except Exception: pass
                    break
    except Exception as e:
        log.error(f"Erro no relay: {e}")
        _notify("❌ Erro na transmissao — veja o arquivo de log")
    finally:
        try: proc.kill()          # kill primeiro: desbloqueia o writer se estiver preso
        except: pass
        try: fila.put_nowait(None)
        except Exception: pass
        try: proc.stdin.close()
        except: pass
        log.info("Relay encerrado")
        # [LEVE] navegador soltou a live (PARAR): se ha video armado, o relay assume
        try:
            if MODO_LEVE["video"] and leve_iniciar(rtmp_url):
                log.info("[LEVE] assumindo a live na mesma chave")
        except Exception as _e:
            log.warning(f"[LEVE] falha ao assumir: {_e}")


# ── Compose ao vivo (Jeito 2): relay compõe as camadas em tempo real e empurra ─
async def _handle_compose(websocket):
    import compose as _compose_mod
    import urllib.request as _u
    sess = _compose_mod.ComposeSession(FFMPEG, log, notify=_notify)
    audio_path = os.path.join(os.path.expanduser("~"), ".blacklive_compose_audio.mp3")
    cfg = {"layers": [], "rtmp": "", "proxy": "", "audio_url": ""}
    enc = leve_detectar_encoder()
    _notify("🎬 Black Live conectado")
    try:
        async for msg in websocket:
            if isinstance(msg, (bytes, bytearray)):
                # se estamos recebendo uma MIDIA (tem _pending) -> chunk de midia;
                # senao -> é o AUDIO AO VIVO do navegador (pipe:0 do ffmpeg)
                if sess._pending:
                    sess.media_chunk(bytes(msg))
                else:
                    sess.audio_write(bytes(msg))
                continue
            try:
                d = json.loads(msg)
            except Exception:
                continue
            cmd = d.get("cmd", "")
            if cmd == "config":
                for k in ("layers", "rtmp", "proxy", "audio_url"):
                    if k in d:
                        cfg[k] = d[k]
                sess.audio_live = bool(d.get("audio_live"))
                sess.hwdec = bool(d.get("hwdec"))   # decode por hardware (opt-in por sala, teste)
                try:
                    sess.ch = int(d.get("canvas_h") or sess.ch)
                    sess.cw = int(d.get("canvas_w") or sess.cw)
                except Exception:
                    pass
                sess.set_proxy(cfg.get("proxy", ""))
                await websocket.send(json.dumps({"status": "config_ok"}))
            elif cmd == "media_begin":
                sess.media_begin(d.get("media_id"), d.get("ext", "bin"), d.get("size", 0))
                await websocket.send(json.dumps({"status": "media_ready", "media_id": d.get("media_id")}))
            elif cmd == "media_url":
                sess.add_media_url(d.get("media_id"), d.get("url", ""))
            elif cmd == "go":
                try:
                    if cfg.get("audio_url"):
                        _u.urlretrieve(cfg["audio_url"], audio_path)
                    else:
                        subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i",
                                        "anullsrc=r=48000:cl=stereo", "-t", "2", audio_path],
                                       capture_output=True, creationflags=_sub_flags())
                except Exception as _ea:
                    subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i",
                                    "anullsrc=r=48000:cl=stereo", "-t", "2", audio_path],
                                   capture_output=True, creationflags=_sub_flags())
                    log.warning("[COMPOSE] audio falhou (%s) — silencio" % _ea)
                pid = sess.start(cfg["layers"], audio_path, cfg["rtmp"], enc)
                _notify("🔴 Black Live AO VIVO no ar")
                await websocket.send(json.dumps({"status": "streaming", "pid": pid}))
            elif cmd == "update":
                cfg["layers"] = d.get("layers", cfg["layers"])
                sess.start(cfg["layers"], audio_path, cfg["rtmp"], enc)   # rebuild (o "pisca" ao editar ao vivo)
                await websocket.send(json.dumps({"status": "updated"}))
            elif cmd == "stop":
                sess.stop()
                await websocket.send(json.dumps({"status": "stopped"}))
                break
    except Exception as e:
        log.error("[COMPOSE] erro: %s" % e)
    finally:
        sess.cleanup()
        log.info("[COMPOSE] sessao encerrada")


# ── Render local: camadas → MP4 → upload VPS → VPS transmite ─────────────────
async def _handle_render(websocket):
    """
    Fluxo:
      1. Recebe config JSON via WebSocket (layers, audio_url, username, sala, rtmp_url, proxy)
      2. Baixa o áudio concatenado do VPS
      3. Roda FFmpeg local sem -re (muito mais rápido que real-time)
      4. Faz upload do MP4 pro VPS
      5. VPS transmite com -c copy sem gastar CPU
    """
    try:
        msg    = await asyncio.wait_for(websocket.recv(), timeout=10)
        config = json.loads(msg)
    except Exception as e:
        await websocket.send(json.dumps({"error": f"Config inválida: {e}"}))
        return

    layers    = config.get("layers", [])
    audio_url = config.get("audio_url", "")
    username  = config.get("username", "")
    sala      = config.get("sala", "")
    rtmp_url  = config.get("rtmp_url", "")
    proxy_url = config.get("proxy", "")

    if not layers or not audio_url:
        await websocket.send(json.dumps({"error": "layers e audio_url são obrigatórios"}))
        return

    # 1. Baixa áudio concatenado do VPS
    await websocket.send(json.dumps({"status": "baixando_audio", "msg": "Baixando áudio..."}))
    audio_path  = os.path.join(os.path.expanduser("~"), ".blacklive_audio_render.mp3")
    output_path = os.path.join(os.path.expanduser("~"), ".blacklive_render.mp4")

    try:
        urllib.request.urlretrieve(audio_url, audio_path)
    except Exception as e:
        await websocket.send(json.dumps({"error": f"Erro ao baixar áudio: {e}"}))
        return

    # 2. Monta filter_complex com as camadas (igual rtmp_streamer.py)
    await websocket.send(json.dumps({"status": "renderizando", "msg": "Renderizando vídeo..."}))

    canvas_w, canvas_h = 720, 1280
    input_args   = []
    filter_parts = []
    overlay_idx  = 0
    layer_map    = []  # mapeia layer → input index no FFmpeg

    for layer in layers:
        ltype = layer.get("type", "")
        path  = layer.get("path", "")

        if ltype == "clock":
            layer_map.append(None)
            continue

        if ltype == "ticker":
            scale   = layer.get("scale", 100)
            bar_w   = int(720 * scale / 100)
            bar_w   = bar_w if bar_w % 2 == 0 else bar_w + 1
            bar_h   = max(30, int(90 * scale / 100))
            effect  = layer.get("effect", "news_red")
            bg_hex  = layer.get("bgColor", "")
            if bg_hex:
                bg_color = f"0x{bg_hex.lstrip('#')}FF"
            elif effect == "promo_gold":
                bg_color = "0xf59e0bE6"
            elif effect == "modern_dark":
                bg_color = "0x000000BF"
            else:
                bg_color = "0xdc2626E6"
            input_args.extend(["-f", "lavfi", "-i", f"color=c={bg_color}:s={bar_w}x{bar_h}:r=30"])
            layer_map.append(overlay_idx)
            overlay_idx += 1
            continue

        if ltype == "banner_rotation":
            images = layer.get("images", [])
            if images and os.path.isfile(images[0]):
                input_args.extend(["-loop", "1", "-i", images[0]])
                layer_map.append(overlay_idx)
                overlay_idx += 1
            else:
                layer_map.append(None)
            continue

        if path and os.path.isfile(path):
            ext = path.lower().rsplit(".", 1)[-1]
            if ext in ("mp4", "mov", "webm"):
                input_args.extend(["-stream_loop", "-1", "-i", path])
            else:
                input_args.extend(["-loop", "1", "-i", path])
            layer_map.append(overlay_idx)
            overlay_idx += 1
        else:
            layer_map.append(None)

    # Fundo preto
    bg_idx = overlay_idx
    input_args.extend(["-f", "lavfi", "-i", f"color=c=black:s={canvas_w}x{canvas_h}:r=30"])

    # Scale de cada camada
    for i, layer in enumerate(layers):
        inp = layer_map[i]
        if inp is None:
            continue
        ltype  = layer.get("type", "")
        scale  = layer.get("scale", 100)
        sw     = int(canvas_w * scale / 100)
        sh     = int(canvas_h * scale / 100)
        sw     = sw if sw % 2 == 0 else sw + 1
        sh     = sh if sh % 2 == 0 else sh + 1

        if ltype == "ticker":
            raw_text = layer.get("text", "PROMOCAO").replace("'", "'\\''").replace(":", "\\\\:")
            repeated = f"   ---   {raw_text}   ---   {raw_text}   ---   {raw_text}"
            bar_h    = max(30, int(90 * scale / 100))
            fontsize = max(16, int(bar_h * 0.55))
            filter_parts.append(
                f"[{inp}:v]format=rgba,"
                f"drawtext=fontfile='/System/Library/Fonts/Supplemental/Arial Bold.ttf':"
                f"text='{repeated}':fontcolor=white:fontsize={fontsize}:"
                f"x='W-mod(t*120\\,W+tw)':y=(h-th)/2[scaled{i}]"
            )
        else:
            fps = ",fps=30" if ltype in ("banner_rotation", "roulette") else ""
            filter_parts.append(
                f"[{inp}:v]fps=30,format=rgba{fps},"
                f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease:flags=bicubic,"
                f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2:color=black@0.0,"
                f"scale={sw}:{sh}:flags=bicubic[scaled{i}]"
            )

    # Overlay das camadas em sequência
    prev = f"[{bg_idx}:v]"
    valid_layers = [(i, l) for i, l in enumerate(layers) if layer_map[i] is not None]

    for pos, (i, layer) in enumerate(valid_layers):
        is_last = (pos == len(valid_layers) - 1)
        nxt     = "[outv]" if is_last else f"[bg{pos}]"
        off_x   = layer.get("x", 0)
        off_y   = layer.get("y", 0)
        scale   = layer.get("scale", 100)
        sw      = int(canvas_w * scale / 100)
        sh      = int(canvas_h * scale / 100)
        x       = int(canvas_w / 2 - sw / 2 + off_x)
        y       = int(canvas_h / 2 - sh / 2 + off_y)

        if layer.get("type") == "ticker":
            filter_parts.append(f"{prev}[scaled{i}]overlay=x={x}:y={y}{nxt}")
        else:
            filter_parts.append(f"{prev}[scaled{i}]overlay=x={x}:y={y}{nxt}")
        prev = nxt

    if not valid_layers:
        filter_parts.append(f"[{bg_idx}:v]copy[outv]")

    filter_str = ";".join(filter_parts)

    # 3. Roda FFmpeg sem -re (muito mais rápido que real-time)
    ffmpeg_cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "warning",
        *input_args,
        "-i", audio_path,
        "-filter_complex", filter_str,
        "-map", "[outv]",
        "-map", f"{bg_idx + 1}:a",
        "-c:v", "libx264", "-preset", "fast",
        "-b:v", "2500k", "-pix_fmt", "yuv420p", "-g", "60",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-shortest",
        "-y", output_path
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="ignore")[-400:]
            await websocket.send(json.dumps({"error": f"FFmpeg falhou: {err}"}))
            return
    except Exception as e:
        await websocket.send(json.dumps({"error": f"Erro FFmpeg: {e}"}))
        return

    log.info("Renderização concluída. Fazendo upload...")

    # 4. Upload do MP4 pro VPS
    await websocket.send(json.dumps({"status": "uploading", "msg": "Enviando para o servidor..."}))

    try:
        boundary = "----BlackLiveBoundary"

        def field(name, value):
            return (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()

        with open(output_path, "rb") as f:
            video_data = f.read()

        body  = f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="video"; filename="render.mp4"\r\n'.encode()
        body += b"Content-Type: video/mp4\r\n\r\n"
        body += video_data + b"\r\n"
        body += field("username", username)
        body += field("sala", sala)
        body += field("rtmp_url", rtmp_url)
        body += field("proxy", proxy_url)
        body += f"--{boundary}--\r\n".encode()

        req = urllib.request.Request(
            f"{VPS_URL}/api/render/upload",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode())

        await websocket.send(json.dumps({"status": "done", "result": result}))
        log.info("Upload concluído com sucesso.")

    except Exception as e:
        await websocket.send(json.dumps({"error": f"Erro no upload: {e}"}))
    finally:
        try: os.remove(output_path)
        except: pass
        try: os.remove(audio_path)
        except: pass


# ═══ MODO LEVE (v1.6) ═════════════════════════════════════════════════════════
# O relay toca o MP4 LOCAL com encoder de HARDWARE e empurra direto pro TikTok.
# O navegador sai do caminho: sem canvas, sem MediaRecorder — pode ate fechar.
# Fluxo: usuario escolhe o video (menu da bandeja) -> transmite normal 1x pelo
# painel (o relay ve a chave RTMP) -> clica PARAR -> o relay ASSUME na mesma
# chave, com religa automatico e anti-suspensao. Provado 27-28/08 no Mac:
# 4-12% de 1 nucleo vs ~93% do navegador.

import atexit
def _kill_compose_leftovers():
    """Mata o ffmpeg do COMPOSE (Jeito 2) por assinatura — inclusive ORFAOS (pai morreu).
    Roda no fechar (atexit/sinal) E no abrir do relay (limpa orfao de crash anterior).
    Sem isso, fechar o relay deixava o ffmpeg empurrando pro TikTok sozinho."""
    try:
        import psutil
        for pr in psutil.process_iter(attrs=["pid", "cmdline"]):
            try:
                cl = " ".join(pr.info.get("cmdline") or [])
                if "blcompose_" in cl or ".blacklive_compose_audio" in cl:
                    pr.kill()
            except Exception:
                pass
        return
    except Exception:
        pass
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["wmic", "process", "where",
                            "commandline like '%blcompose_%'", "delete"],
                           capture_output=True, timeout=8)
        else:
            subprocess.run(["pkill", "-f", "blcompose_"], capture_output=True, timeout=8)
    except Exception:
        pass

def _mata_filhos_no_exit():
    """App fechando: leva os motores junto (sem orfaos empurrando video velho)."""
    try:
        p = MODO_LEVE.get("proc")
        if p:
            p.kill()
    except Exception:
        pass
    try:
        c = MODO_LEVE.get("caff")
        if c:
            c.terminate()
    except Exception:
        pass
    _kill_compose_leftovers()
atexit.register(_mata_filhos_no_exit)
try:
    signal.signal(signal.SIGTERM, lambda *_a: (_mata_filhos_no_exit(), os._exit(0)))
except Exception:
    pass

MODO_LEVE = {"video": None, "proc": None, "rtmp": None, "stop": False,
             "encoder": None, "mortes_rapidas": 0, "caff": None, "gen": 0,
             "ativo": False, "audio": "video", "audio_q": None, "audio_ws_vivo": False,
             "audio_hdr": None}
NOTIFY = [None]   # relay_tray injeta show_notification aqui

def _notify(msg):
    try:
        if NOTIFY[0]:
            NOTIFY[0](msg)
    except Exception:
        pass
    log.info(f"[LEVE] {msg}")

def _sub_flags():
    """No Windows, esconde a janela preta dos subprocessos."""
    return 0x08000000 if sys.platform.startswith("win") else 0

def leve_detectar_encoder():
    """Testa os encoders de hardware da maquina (1s sintetico). Cacheia o vencedor."""
    if MODO_LEVE["encoder"]:
        return MODO_LEVE["encoder"]
    if sys.platform == "darwin":
        candidatos = ["h264_videotoolbox"]
    elif sys.platform.startswith("win"):
        candidatos = ["h264_nvenc", "h264_qsv", "h264_amf"]
    else:
        candidatos = []
    escolhido = "libx264"   # fallback por software (sempre existe)
    for enc in candidatos:
        try:
            r = subprocess.run(
                [FFMPEG, "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
                 "-t", "1", "-c:v", enc, "-f", "null", "-"],
                capture_output=True, timeout=25, creationflags=_sub_flags())
            if r.returncode == 0:
                escolhido = enc
                break
        except Exception:
            pass
    MODO_LEVE["encoder"] = escolhido
    log.info(f"[LEVE] encoder detectado: {escolhido}")
    return escolhido

def _leve_tem_audio(video):
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-i", video],
                           capture_output=True, timeout=20, creationflags=_sub_flags())
        return b"Audio:" in r.stderr
    except Exception:
        return True   # na duvida assume que tem (ffmpeg reclama se nao tiver mapa)

def _leve_cmd(rtmp_url):
    enc   = leve_detectar_encoder()
    video = MODO_LEVE["video"]
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "warning"]
    if enc == "h264_videotoolbox":
        cmd += ["-hwaccel", "videotoolbox"]
    cmd += ["-stream_loop", "-1", "-re", "-fflags", "+genpts", "-i", video]
    _amodo = MODO_LEVE.get("audio", "video")
    _tem_a = _leve_tem_audio(video)
    _ao_vivo = _amodo in ("aovivo", "ambos")   # blocos/picotador/mic do navegador via pipe
    if _ao_vivo:
        cmd += ["-thread_queue_size", "1024", "-fflags", "+genpts+discardcorrupt",
                "-f", "webm", "-i", "pipe:0"]

    # filtro de video (scale p/ libx264 + relogio de teste opcional)
    _vf_extra = []
    if enc == "libx264":
        _vf_extra.append("scale=720:-2")   # sem hardware: reduz p/ aliviar a CPU
    _rel = os.path.join(os.path.expanduser("~"), ".bl_leve_relogio.on")
    if os.path.exists(_rel):
        _fonte = "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if sys.platform == "darwin" else "C\\:/Windows/Fonts/arialbd.ttf"
        _vf_extra.append("drawtext=fontfile='" + _fonte + "':expansion=strftime:text='%H\\:%M\\:%S':fontsize=54:fontcolor=white:box=1:boxcolor=black@0.45:boxborderw=14:x=(w-tw)/2:y=90")

    if _ao_vivo and _amodo == "ambos" and _tem_a:
        # COM audio do video: mistura audio do arquivo + audio ao vivo do navegador.
        # filter_complex (nao pode conviver com -vf, entao o filtro de video entra junto)
        if _vf_extra:
            fc = "[0:v]" + ",".join(_vf_extra) + "[vout];[0:a][1:a]amix=inputs=2:duration=first:normalize=0[aout]"
            cmd += ["-filter_complex", fc, "-map", "[vout]", "-map", "[aout]"]
        else:
            cmd += ["-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first:normalize=0[aout]",
                    "-map", "0:v:0", "-map", "[aout]"]
    elif _ao_vivo:
        # SEM audio do video: so o audio ao vivo (blocos/picotador); video mudo
        if _vf_extra:
            cmd += ["-vf", ",".join(_vf_extra)]
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    elif _tem_a and _amodo != "mudo":
        if _vf_extra:
            cmd += ["-vf", ",".join(_vf_extra)]
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]
    else:
        # arquivo sem audio OU mudo -> silencio valido (live nao cai)
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        if _vf_extra:
            cmd += ["-vf", ",".join(_vf_extra)]
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    cmd += ["-c:v", enc]
    if enc == "libx264":
        cmd += ["-preset", "veryfast"]
    elif enc == "h264_nvenc":
        cmd += ["-preset", "p4"]
    cmd += ["-b:v", "2500k", "-maxrate", "2500k", "-bufsize", "5000k", "-g", "60",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "-user_agent", "TikTokLiveStudio/0.46.1",
            "-metadata", "title=TikTok Live Studio",
            "-metadata", "encoder=TikTok Live Studio 0.46.1",
            "-f", "flv", rtmp_url]
    return cmd

def _leve_anti_sleep(on):
    """Impede o PC de dormir durante a transmissao leve."""
    try:
        if sys.platform == "darwin":
            if on and not MODO_LEVE["caff"]:
                MODO_LEVE["caff"] = subprocess.Popen(["caffeinate", "-s"])
            elif not on and MODO_LEVE["caff"]:
                MODO_LEVE["caff"].terminate()
                MODO_LEVE["caff"] = None
        elif sys.platform.startswith("win"):
            import ctypes
            ES_CONTINUOUS, ES_SYSTEM = 0x80000000, 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(
                (ES_CONTINUOUS | ES_SYSTEM) if on else ES_CONTINUOUS)
    except Exception:
        pass

def _leve_push_loop(gen):
    rtmp_url = MODO_LEVE["rtmp"]
    lelog = os.path.join(os.path.expanduser("~"), ".blacklive_leve.log")
    time.sleep(3)   # da tempo do PARAR assentar no TikTok
    _leve_anti_sleep(True)
    MODO_LEVE["ativo"] = True
    try:
        while not MODO_LEVE["stop"] and MODO_LEVE["gen"] == gen:
            t0 = time.time()
            try:
                _aovivo = MODO_LEVE.get("audio") == "aovivo"
                proc = subprocess.Popen(_leve_cmd(rtmp_url),
                                        stdin=(subprocess.PIPE if _aovivo else subprocess.DEVNULL),
                                        stdout=open(lelog, "a"),
                                        stderr=subprocess.STDOUT,
                                        creationflags=_sub_flags())
                if _aovivo and MODO_LEVE.get("audio_q") is not None:
                    _q = MODO_LEVE["audio_q"]
                    try:                                   # esvazia backlog -> audio FRESCO no (re)start
                        while True: _q.get_nowait()
                    except Exception:
                        pass
                    threading.Thread(target=_leve_audio_writer, args=(proc, gen, _q),
                                     daemon=True, name="leve-audio").start()
            except Exception as e:
                log.error(f"[LEVE] falha ao iniciar ffmpeg: {e}")
                _notify("❌ Erro ao iniciar (veja o arquivo de log)")
                break
            MODO_LEVE["proc"] = proc
            _notify("🚀 Black Live NO AR")
            proc.wait()
            MODO_LEVE["proc"] = None
            if MODO_LEVE["stop"] or MODO_LEVE["gen"] != gen:
                break
            rodou = time.time() - t0
            if rodou < 20:
                MODO_LEVE["mortes_rapidas"] += 1
                if MODO_LEVE["mortes_rapidas"] >= 5:
                    log.error("[LEVE] 5 mortes rapidas — desistindo (live encerrada no TikTok?)")
                    _notify("❌ TikTok recusou a chave — transmita de novo pelo painel e clique PARAR")
                    break
            else:
                MODO_LEVE["mortes_rapidas"] = 0
            log.warning(f"[LEVE] ffmpeg caiu apos {int(rodou)}s — religando em 5s")
            _notify("🔄 Religando a live...")
            time.sleep(5)
    finally:
        MODO_LEVE["proc"] = None
        MODO_LEVE["ativo"] = False
        _leve_anti_sleep(False)
        log.info("[LEVE] loop encerrado")

def _leve_audio_writer(proc, gen, q):
    """Alimenta o ffmpeg do leve com o audio ao vivo (webm do navegador) via stdin.
    Segue ESTE ffmpeg; morre junto com ele (na religa nasce um writer novo).
    CRITICO: escreve o CABECALHO webm primeiro (o 1o chunk do MediaRecorder) — sem ele
    o ffmpeg falha o parse EBML do pipe:0 e morre em 0s (bug v1.8.5, parecia 'TikTok recusou')."""
    t0 = time.time()
    while time.time() - t0 < 12 and MODO_LEVE["gen"] == gen and not MODO_LEVE["stop"] and proc.poll() is None:
        hdr = MODO_LEVE.get("audio_hdr")
        if hdr:
            try:
                if proc.stdin and proc.poll() is None:
                    proc.stdin.write(hdr)
                    proc.stdin.flush()
            except Exception:
                pass
            break
        time.sleep(0.1)
    while MODO_LEVE["gen"] == gen and not MODO_LEVE["stop"] and proc.poll() is None:
        try:
            data = q.get(timeout=0.5)
        except Exception:
            continue
        if data is None:
            break
        try:
            if proc.stdin and proc.poll() is None:
                proc.stdin.write(data)
                proc.stdin.flush()
        except Exception:
            break
    try:
        if proc.stdin: proc.stdin.close()
    except Exception:
        pass

def leve_iniciar(rtmp_url):
    """Chamado quando o navegador SOLTA a live (PARAR) e ha video escolhido."""
    if not (MODO_LEVE["video"] and os.path.isfile(MODO_LEVE["video"])):
        return False
    leve_parar_push()
    MODO_LEVE["gen"] += 1
    MODO_LEVE["rtmp"] = rtmp_url
    MODO_LEVE["stop"] = False
    MODO_LEVE["mortes_rapidas"] = 0
    threading.Thread(target=_leve_push_loop, args=(MODO_LEVE["gen"],),
                     daemon=True, name="leve").start()
    return True

def leve_parar_push():
    """Para o push leve (mantem o video escolhido)."""
    MODO_LEVE["stop"] = True
    MODO_LEVE["gen"] += 1
    p = MODO_LEVE["proc"]
    if p:
        try: p.terminate()
        except Exception: pass

def leve_desligar():
    """Desliga o modo leve por completo (para o push e esquece o video)."""
    MODO_LEVE["audio"] = "video"          # evita o fallback do audio ao vivo re-ligar
    MODO_LEVE["audio_ws_vivo"] = False
    MODO_LEVE["audio_hdr"] = None
    try:
        if MODO_LEVE.get("audio_q"): MODO_LEVE["audio_q"].put_nowait(None)
    except Exception:
        pass
    leve_parar_push()
    MODO_LEVE["video"] = None

def leve_escolher_video():
    """Dialogo de escolha do video (osascript no Mac, tkinter no Windows)."""
    path = None
    try:
        if sys.platform == "darwin":
            r = subprocess.run(
                ["osascript", "-e",
                 'POSIX path of (choose file with prompt "Escolha o vídeo da live:" of type {"public.movie"})'],
                capture_output=True, text=True, timeout=180)
            path = (r.stdout or "").strip() or None
        else:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk(); root.withdraw()
            try: root.attributes("-topmost", True)
            except Exception: pass
            path = filedialog.askopenfilename(
                title="Escolha o vídeo da live",
                filetypes=[("Vídeos", "*.mp4 *.mov *.mkv *.avi"), ("Todos", "*.*")]) or None
            root.destroy()
    except Exception as e:
        log.warning(f"[LEVE] escolher video falhou: {e}")
    if path and os.path.isfile(path):
        MODO_LEVE["video"] = path
        threading.Thread(target=leve_detectar_encoder, daemon=True).start()
        log.info(f"[LEVE] video armado: {path}")
        return path
    return None


# ── Servidor WebSocket ─────────────────────────────────────────────────────────
async def handle_safe(websocket):
    """Blindagem: qualquer erro numa conexao fica ISOLADO e nao derruba o servidor."""
    try:
        await handle(websocket)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning(f"Conexao encerrada com erro (isolada): {type(e).__name__}: {e}")
        try:
            await websocket.close()
        except Exception:
            pass


async def main():
    try:
        import websockets
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "websockets"], check=True)
        import websockets

    _instala_fix_pna()   # Chrome 152+: aceita o preflight de "Acesso a rede local"
    log.info(f"BlackLive Local Relay v{VERSION} iniciado | FFmpeg: {FFMPEG}")
    _tele("start")   # v1.9.0 TESTE: prova remota de que o app novo foi instalado e abriu
    _kill_compose_leftovers()   # limpa ffmpeg de compose orfao de uma sessao/crash anterior

    # Auto-update em background — não bloqueia o start
    threading.Thread(target=check_update, daemon=True).start()

    while True:
        try:
            async with websockets.serve(
                handle_safe,
                "127.0.0.1",
                PORT,
                process_request=pna_process_request,   # preflight Chrome 152+ (rede local)
                max_size=100 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=60,
            ):
                MODO_LEVE["_port_retry"] = 0   # subiu: zera o contador de porta presa
                print(f"✅ BlackLive Relay v{VERSION} em ws://127.0.0.1:{PORT}")
                await asyncio.Future()
        except asyncio.CancelledError:
            raise
        except OSError as e:
            # Porta ocupada. Duas causas (caso juliana 28/08, "abre e ja fecha"):
            #  (a) OUTRO Black Live saudavel ja aberto -> avisa e sai (nao pode ter 2);
            #  (b) socket morto/preso (ex: PC voltou do sono, instancia mal-fechada) ->
            #      espera e tenta de novo ate 6x em vez de fechar na cara do usuario.
            vivo = False
            try:
                import websockets as _ws
                _hdr = {"Origin": "http://127.0.0.1:8900"}   # origem autorizada p/ passar no filtro
                try:
                    _conn = _ws.connect(f"ws://127.0.0.1:{PORT}/ping", open_timeout=3, additional_headers=_hdr)
                except TypeError:   # versoes antigas do websockets usam outro nome
                    _conn = _ws.connect(f"ws://127.0.0.1:{PORT}/ping", open_timeout=3, extra_headers=_hdr)
                async with _conn as _w:
                    json.loads(await _w.recv())
                    vivo = True
            except Exception:
                vivo = False
            if vivo:
                log.error(f"Porta {PORT} ja tem um Black Live saudavel — este sai (evite abrir 2x).")
                _notify("⚠️ O Black Live já está aberto — procure o ícone na bandeja")
                os._exit(1)
            _tent = MODO_LEVE.get("_port_retry", 0) + 1
            MODO_LEVE["_port_retry"] = _tent
            if _tent == 2 and sys.platform.startswith("win"):
                # v1.6.3 TAKEOVER: ha um Black Live CONGELADO segurando a porta (vivo no
                # gerenciador, surdo no ping). Encerra os OUTROS processos do nosso exe e
                # assume. PyInstaller onefile = 2 processos por instancia (pai bootloader +
                # filho); preserva o proprio PID e o PID do pai.
                log.warning("instancia congelada segurando a porta — encerrando ela e assumindo")
                _notify("🔄 Encontrei um Black Live travado — encerrando ele e assumindo")
                try:
                    _meus = {os.getpid(), os.getppid()}
                    for _img in ("Black Live.exe", "BlackLive-Relay.exe"):
                        r = subprocess.run(["tasklist", "/FO", "CSV", "/FI", f"IMAGENAME eq {_img}"],
                                           capture_output=True, text=True, timeout=15, creationflags=_sub_flags())
                        for line in r.stdout.splitlines()[1:]:
                            parts = [p.strip('"') for p in line.split('","')]
                            if len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) not in _meus:
                                subprocess.run(["taskkill", "/F", "/PID", parts[1]],
                                               capture_output=True, timeout=10, creationflags=_sub_flags())
                                log.info(f"takeover: encerrei o processo congelado {_img} pid={parts[1]}")
                except Exception as _tk:
                    log.warning(f"takeover falhou: {_tk}")
            if _tent >= 6:
                log.error(f"Porta {PORT} presa apos {_tent} tentativas ({e}). Encerrando.")
                _notify("❌ Não consegui abrir a porta do Black Live — reinicie o computador")
                os._exit(1)
            log.warning(f"Porta {PORT} ocupada sem relay vivo ({e}) — tentativa {_tent}/6, aguardando 5s...")
            await asyncio.sleep(5)
        except Exception as e:
            # Qualquer outra falha do servidor: loga e reergue em 2s (nao morre de vez).
            log.error(f"Servidor WebSocket caiu ({type(e).__name__}: {e}). Reerguendo em 2s...")
            try:
                await asyncio.sleep(2)
            except Exception:
                pass


# ── Auto-install Mac (LaunchAgent) ────────────────────────────────────────────
def install_mac():
    script_path = os.path.abspath(__file__)
    python_path = sys.executable
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.blacklive.relay</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_path}</string>
        <string>{script_path}</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{log_path}</string>
    <key>StandardErrorPath</key><string>{log_path}</string>
</dict>
</plist>"""
    plist_path = os.path.expanduser("~/Library/LaunchAgents/com.blacklive.relay.plist")
    with open(plist_path, "w") as f:
        f.write(plist)
    os.system(f"launchctl load {plist_path}")
    print("✅ Auto-start instalado no Mac!")


# ── Auto-install Windows (Registry) ───────────────────────────────────────────
def install_win():
    import winreg
    script_path = os.path.abspath(__file__)
    python_path = sys.executable.replace("python.exe", "pythonw.exe")
    cmd = f'"{python_path}" "{script_path}"'
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                         r"Software\Microsoft\Windows\CurrentVersion\Run",
                         0, winreg.KEY_SET_VALUE)
    winreg.SetValueEx(key, "BlackLiveRelay", 0, winreg.REG_SZ, cmd)
    winreg.CloseKey(key)
    print("✅ Auto-start instalado no Windows!")


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if "--install-mac" in sys.argv:
        install_mac(); sys.exit(0)
    if "--install-win" in sys.argv:
        install_win(); sys.exit(0)

    # teste no terminal: `python3 local_relay.py /caminho/video.mp4` ja arma o modo leve
    for _a in sys.argv[1:]:
        if os.path.isfile(_a):
            MODO_LEVE["video"] = _a
            print(f"🎬 Vídeo armado: {_a}\n   Transmita pelo painel e clique PARAR — o Black Live assume sozinho.")
            threading.Thread(target=leve_detectar_encoder, daemon=True).start()
            break

    def on_signal(*_):
        log.info("Relay encerrado por sinal")
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nRelay encerrado.")
    except SystemExit:
        raise
    except Exception as e:
        log.error(f"Relay caiu de forma fatal ({type(e).__name__}: {e}). Encerrando o processo (sem deixar zumbi).")
        os._exit(1)
