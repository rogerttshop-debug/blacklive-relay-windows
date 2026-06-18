#!/usr/bin/env python3
"""
BlackLive Relay Tray — v1.2
============================
Ícone na bandeja do sistema que gerencia o Local Relay.
Inicia o local_relay.py em background e oferece menu de controle.
"""

import sys
import os
import asyncio
import subprocess
import threading
import time
import json
import urllib.request
import urllib.error

VPS_URL      = "https://fabricalive.johne.tech"
PORT         = 8902

relay_thread = None
relay_loop   = None
tray_icon    = None


# ── Controle do relay ──────────────────────────────────────────────────────────
def relay_running():
    return relay_thread is not None and relay_thread.is_alive()


def _run_relay():
    global relay_loop
    import local_relay
    relay_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(relay_loop)
    try:
        relay_loop.run_until_complete(local_relay.main())
    except Exception:
        pass
    finally:
        relay_loop.close()
        relay_loop = None


def start_relay():
    global relay_thread
    if relay_running():
        return
    relay_thread = threading.Thread(target=_run_relay, daemon=True, name="relay")
    relay_thread.start()


def stop_relay():
    global relay_loop, relay_thread
    if relay_loop and not relay_loop.is_closed():
        relay_loop.call_soon_threadsafe(relay_loop.stop)
    relay_thread = None


def ping_relay():
    """Verifica se o relay está respondendo na porta 8902."""
    try:
        import websockets
        import asyncio
        async def _ping():
            async with websockets.connect(f"ws://localhost:{PORT}/ping", open_timeout=2) as ws:
                resp = json.loads(await ws.recv())
                return resp.get("version", "?")
        return asyncio.run(_ping())
    except Exception:
        return None


# ── Menu da bandeja ────────────────────────────────────────────────────────────
def build_menu(Icon, Menu, MenuItem):
    def on_abrir_painel(_):
        import webbrowser
        webbrowser.open(VPS_URL)

    def on_status(_):
        v = ping_relay()
        if v:
            show_notification(f"✅ Relay ativo — v{v}")
        else:
            show_notification("❌ Relay não está respondendo")

    def on_restart(_):
        stop_relay()
        time.sleep(1)
        start_relay()
        show_notification("🔄 Relay reiniciado")

    def on_quit(_):
        stop_relay()
        tray_icon.stop()

    return Menu(
        MenuItem("🟢 Abrir Painel", on_abrir_painel, default=True),
        MenuItem("📡 Verificar Status", on_status),
        MenuItem("🔄 Reiniciar Relay", on_restart),
        Menu.SEPARATOR,
        MenuItem("❌ Encerrar", on_quit),
    )


def show_notification(msg):
    try:
        if tray_icon:
            tray_icon.notify(msg, "BlackLive Relay")
    except Exception:
        pass


# ── Ícone gerado programaticamente (sem arquivo externo) ──────────────────────
def create_icon_image():
    try:
        from PIL import Image, ImageDraw
        img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([4, 4, 60, 60], fill=(220, 38, 38, 255))   # vermelho
        draw.polygon([(22, 16), (22, 48), (50, 32)], fill=(255, 255, 255, 255))  # play
        return img
    except Exception:
        return None


# ── Watchdog: reinicia o relay se morrer ──────────────────────────────────────
def watchdog():
    while True:
        time.sleep(10)
        if not relay_running():
            start_relay()


# ── Entry point ────────────────────────────────────────────────────────────────
def main():
    global tray_icon

    import pystray
    from PIL import Image

    # Inicia o relay imediatamente (em thread, sem subprocess)
    start_relay()

    # Watchdog em background
    threading.Thread(target=watchdog, daemon=True).start()

    # Cria o ícone da bandeja
    icon_img = create_icon_image()
    if icon_img is None:
        icon_img = Image.new("RGB", (64, 64), color=(220, 38, 38))

    menu = build_menu(pystray.Icon, pystray.Menu, pystray.MenuItem)
    tray_icon = pystray.Icon(
        "BlackLive Relay",
        icon_img,
        "BlackLive Relay",
        menu
    )

    tray_icon.run()


if __name__ == "__main__":
    main()
