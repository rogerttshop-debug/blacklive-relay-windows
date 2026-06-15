#!/usr/bin/env python3
"""
BlackLive Local Relay — local_relay.py
=======================================
Lógica central: WebSocket → FFmpeg → RTMP

Usa imageio-ffmpeg para ter o FFmpeg bundled no executável.
NÃO depende de FFmpeg instalado no sistema.
"""

import asyncio
import subprocess
import sys
import os
import json
import urllib.parse
import logging

PORT    = 8902
VERSION = "1.0.0"
URL_STUDIO = "https://blacklive.com.br"  # abre no browser ao clicar no ícone

# ── Detecta FFmpeg (bundled ou sistema) ────────────────────────────────────────
def get_ffmpeg():
    """Retorna o caminho do FFmpeg — bundled (PyInstaller) ou do sistema."""
    # PyInstaller coloca recursos em sys._MEIPASS
    if getattr(sys, 'frozen', False):
        base = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
        # imageio-ffmpeg bundled
        for candidate in [
            os.path.join(base, 'imageio_ffmpeg', 'binaries'),
            os.path.join(base, 'ffmpeg_bin'),
            base,
        ]:
            if os.path.isdir(candidate):
                for f in os.listdir(candidate):
                    if f.startswith('ffmpeg') and os.access(os.path.join(candidate, f), os.X_OK):
                        return os.path.join(candidate, f)
    # Tenta imageio-ffmpeg instalado como lib
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    # Fallback: ffmpeg do sistema
    return 'ffmpeg'

# ── Logging ────────────────────────────────────────────────────────────────────
log_path = os.path.join(os.path.expanduser('~'), '.blacklive_relay.log')
logging.basicConfig(
    filename=log_path,
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('relay')

# ── Handler WebSocket ──────────────────────────────────────────────────────────
async def handle(websocket):
    try:
        path = websocket.request.path
    except AttributeError:
        try:    path = websocket.path
        except: path = '/'

    parsed = urllib.parse.urlparse(path)
    qs     = urllib.parse.parse_qs(parsed.query)

    # /ping — health check da página
    if parsed.path == '/ping':
        await websocket.send(json.dumps({
            'status': 'ok',
            'version': VERSION,
            'ip': 'local'
        }))
        return

    # /rtmp — relay de stream
    if 'rtmp' not in qs:
        await websocket.send(json.dumps({'error': 'rtmp param missing'}))
        return

    rtmp_url = qs['rtmp'][0]
    proxy_url = qs.get('proxy', [None])[0]
    log.info(f'Relay iniciado → {rtmp_url.split("?")[0]}...')

    ffmpeg_exe = get_ffmpeg()
    ffmpeg_log = os.path.join(os.path.expanduser('~'), '.blacklive_ffmpeg.log')

    cmd = [
        ffmpeg_exe, '-hide_banner', '-loglevel', 'warning',
        '-fflags', '+genpts+discardcorrupt'
    ]
    
    if proxy_url:
        cmd.extend(['-http_proxy', proxy_url])
        
    cmd.extend([
        # Input: WebM do browser (VP8 + Opus)
        '-f', 'webm', '-i', 'pipe:0',
        # Video: H.264 para TikTok
        '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
        '-b:v', '2500k', '-pix_fmt', 'yuv420p', '-g', '60',
        # Audio: AAC 160k / 48000 Hz (igual rtmp_streamer.py)
        '-c:a', 'aac', '-b:a', '160k', '-ar', '48000',
        # Metadata TikTok Live Studio (spoof)
        '-user_agent', 'TikTokLiveStudio/0.46.1',
        '-metadata', 'title=TikTok Live Studio',
        '-metadata', 'encoder=TikTok Live Studio 0.46.1',
        '-f', 'flv',
        rtmp_url
    ])

    custom_env = os.environ.copy()
    if proxy_url:
        custom_env['http_proxy'] = proxy_url
        custom_env['https_proxy'] = proxy_url
        custom_env['HTTP_PROXY'] = proxy_url
        custom_env['HTTPS_PROXY'] = proxy_url
        log.info(f'Injetando proxy para FFmpeg: {proxy_url}')

    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=open(ffmpeg_log, 'w'),
            stderr=open(ffmpeg_log, 'a'),
            env=custom_env
        )
        log.info('FFmpeg iniciado (IP local → TikTok)')
        await websocket.send(json.dumps({'status': 'streaming', 'ip': 'local'}))

        async for msg in websocket:
            if isinstance(msg, bytes):
                try:
                    proc.stdin.write(msg)
                    proc.stdin.flush()
                except BrokenPipeError:
                    log.warning('FFmpeg encerrou pipe')
                    break

    except Exception as e:
        log.error(f'Erro no relay: {e}')
    finally:
        if proc:
            try: proc.stdin.close()
            except: pass
            try: proc.terminate()
            except: pass
        log.info('Relay encerrado')

# ── Servidor WebSocket ─────────────────────────────────────────────────────────
async def start_server():
    import websockets
    log.info(f'BlackLive Local Relay v{VERSION} na porta {PORT}')
    async with websockets.serve(
        handle,
        'localhost',
        PORT,
        max_size=100 * 1024 * 1024,
        ping_interval=20,
        ping_timeout=60,
    ):
        await asyncio.Future()

def run():
    asyncio.run(start_server())
if __name__ == '__main__': run()
