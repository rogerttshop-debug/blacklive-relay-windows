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
import urllib.parse
import urllib.request
import logging
import threading

PORT    = 8902
VERSION = "1.2.0"
VPS_URL = "https://fabricalive.johne.tech"

ALLOWED_ORIGINS = {
    "https://fabricalive.johne.tech",
    "http://localhost:8900",
    "http://127.0.0.1:8900",
}

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
    """Baixa nova versão do local_relay.py do VPS e reinicia se houver update."""
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
            "ip": "local"
        }))
        return

    # /render — renderiza MP4 localmente e faz upload pro VPS
    if parsed.path == "/render":
        await _handle_render(websocket)
        return

    # /rtmp — relay de stream ao vivo (câmera → TikTok)
    if "rtmp" not in qs:
        await websocket.send(json.dumps({"error": "rtmp param missing"}))
        return

    await _handle_rtmp(websocket, qs)


# ── Relay ao vivo: WebM → FFmpeg → RTMP ───────────────────────────────────────
async def _handle_rtmp(websocket, qs):
    rtmp_url  = qs["rtmp"][0]
    proxy_url = qs.get("proxy", [None])[0]

    log.info(f"Relay ao vivo → {rtmp_url.split('?')[0]}...")

    env = os.environ.copy()
    if proxy_url:
        parts = proxy_url.split(":")
        if len(parts) == 4:
            proxy_url = f"{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        env["http_proxy"]  = f"http://{proxy_url}"
        env["https_proxy"] = f"http://{proxy_url}"

    ffmpeg_log = os.path.join(os.path.expanduser("~"), ".blacklive_ffmpeg.log")
    ffmpeg_cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "warning",
        "-fflags", "+genpts+discardcorrupt",
        "-f", "webm", "-i", "pipe:0",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-b:v", "2500k", "-pix_fmt", "yuv420p", "-g", "60",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-user_agent", "TikTokLiveStudio/0.46.1",
        "-metadata", "title=TikTok Live Studio",
        "-metadata", "encoder=TikTok Live Studio 0.46.1",
        "-f", "flv", rtmp_url
    ]

    try:
        proc = subprocess.Popen(
            ffmpeg_cmd,
            stdin=subprocess.PIPE,
            stdout=open(ffmpeg_log, "w"),
            stderr=open(ffmpeg_log, "a"),
            env=env
        )
        log.info("FFmpeg iniciado (local → TikTok)")
        await websocket.send(json.dumps({"status": "streaming", "ip": "local"}))

        async for msg in websocket:
            if isinstance(msg, bytes):
                try:
                    proc.stdin.write(msg)
                    proc.stdin.flush()
                except BrokenPipeError:
                    log.warning("FFmpeg encerrou o pipe")
                    break
    except Exception as e:
        log.error(f"Erro no relay: {e}")
    finally:
        try: proc.stdin.close()
        except: pass
        try: proc.terminate()
        except: pass
        log.info("Relay encerrado")


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


# ── Servidor WebSocket ─────────────────────────────────────────────────────────
async def main():
    try:
        import websockets
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "websockets"], check=True)
        import websockets

    log.info(f"BlackLive Local Relay v{VERSION} iniciado | FFmpeg: {FFMPEG}")

    # Auto-update em background — não bloqueia o start
    threading.Thread(target=check_update, daemon=True).start()

    async with websockets.serve(
        handle,
        "localhost",
        PORT,
        max_size=100 * 1024 * 1024,
        ping_interval=20,
        ping_timeout=60,
    ):
        print(f"✅ BlackLive Relay v{VERSION} em ws://localhost:{PORT}")
        await asyncio.Future()


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

    def on_signal(*_):
        log.info("Relay encerrado por sinal")
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nRelay encerrado.")
