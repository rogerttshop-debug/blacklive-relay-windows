#!/usr/bin/env python3
"""
BlackLive Local Relay — relay_tray.py
======================================
Ponto de entrada principal do executável.
- Roda o relay WebSocket em background
- Exibe ícone na bandeja do sistema (Windows/Mac/Linux)
- Clique no ícone → abre o browser no studio
- Auto-start com o sistema (via --install)
"""

import sys
import os
import threading
import webbrowser
import logging

# Importa a lógica do relay
from local_relay import run as run_relay, PORT, VERSION, URL_STUDIO, log_path

# ── Configuração de logging (também no console se não bundled) ─────────────────
logging.basicConfig(
    filename=log_path,
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)

# ── Ícone da bandeja ───────────────────────────────────────────────────────────
def get_icon_path():
    """Retorna caminho do ícone PNG (bundled ou local)."""
    if getattr(sys, 'frozen', False):
        base = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, 'icon.png')

def create_icon_image():
    """Cria ícone padrão se icon.png não existir."""
    from PIL import Image, ImageDraw
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Círculo verde como ícone padrão
    draw.ellipse([4, 4, 60, 60], fill='#22c55e', outline='#166534', width=3)
    draw.text((20, 22), 'BL', fill='white')
    return img

def load_icon():
    from PIL import Image
    icon_path = get_icon_path()
    if os.path.exists(icon_path):
        return Image.open(icon_path)
    return create_icon_image()

# ── Menu da bandeja ────────────────────────────────────────────────────────────
def abrir_studio(icon, item):
    webbrowser.open(URL_STUDIO)

def ver_status(icon, item):
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    messagebox.showinfo(
        'BlackLive Relay',
        f'✅ Rodando na porta {PORT}\n'
        f'🔗 IP: Residencial (local)\n'
        f'📋 Log: {log_path}'
    )
    root.destroy()

def encerrar(icon, item):
    icon.stop()
    os.kill(os.getpid(), 9)

def build_menu():
    import pystray
    return pystray.Menu(
        pystray.MenuItem(f'BlackLive Relay v{VERSION}', None, enabled=False),
        pystray.MenuItem('─────────────────', None, enabled=False),
        pystray.MenuItem('🌐 Abrir Studio', abrir_studio, default=True),
        pystray.MenuItem('📊 Ver Status', ver_status),
        pystray.MenuItem('─────────────────', None, enabled=False),
        pystray.MenuItem('❌ Encerrar Relay', encerrar),
    )

# ── Auto-start ─────────────────────────────────────────────────────────────────
def install_autostart():
    exe = sys.executable if not getattr(sys, 'frozen', False) else os.path.abspath(sys.argv[0])

    if sys.platform == 'darwin':  # Mac
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.blacklive.relay</string>
    <key>ProgramArguments</key>
    <array><string>{exe}</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{log_path}</string>
    <key>StandardErrorPath</key><string>{log_path}</string>
</dict>
</plist>"""
        plist_path = os.path.expanduser('~/Library/LaunchAgents/com.blacklive.relay.plist')
        with open(plist_path, 'w') as f:
            f.write(plist)
        os.system(f'launchctl load {plist_path}')
        print(f'✅ Auto-start instalado no Mac.')

    elif sys.platform == 'win32':  # Windows
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r'Software\Microsoft\Windows\CurrentVersion\Run',
                             0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, 'BlackLiveRelay', 0, winreg.REG_SZ, f'"{exe}"')
        winreg.CloseKey(key)
        print('✅ Auto-start instalado no Windows.')

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    # Instalar auto-start
    if '--install' in sys.argv:
        install_autostart()
        return

    # Inicia o relay em background thread
    relay_thread = threading.Thread(target=run_relay, daemon=True)
    relay_thread.start()

    print(f'✅ BlackLive Local Relay v{VERSION} iniciado na porta {PORT}')
    print(f'   Studio: {URL_STUDIO}')
    print(f'   Log: {log_path}')

    # Tenta iniciar com ícone na bandeja
    try:
        import pystray
        from PIL import Image

        icon_img = load_icon()
        icon = pystray.Icon(
            'BlackLive Relay',
            icon_img,
            f'BlackLive Relay v{VERSION} — 🟢 Ativo',
            menu=build_menu()
        )
        print('   Ícone na bandeja ativo. Clique para abrir o studio.')
        icon.run()  # bloqueia aqui — relay continua na thread

    except ImportError:
        # Sem pystray — modo simples (janela de terminal)
        print('   (pystray não instalado — modo terminal)')
        print('   Pressione Ctrl+C para encerrar.')
        relay_thread.join()

if __name__ == '__main__':
    main()
