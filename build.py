#!/usr/bin/env python3
"""
BlackLive Local Relay — build.py
==================================
Script para gerar o executável (.exe no Windows, .app no Mac).

Uso:
  python build.py           → gera para a plataforma atual
  python build.py --clean   → limpa dist/ e build/ antes
"""

import os
import sys
import subprocess
import shutil

NOME        = 'Black Live'
VERSAO      = '1.2.0'
ENTRY_POINT = 'relay_tray.py'
ICON_WIN    = 'icon.ico'
ICON_MAC    = 'icon.icns'
ICON_PNG    = 'icon.png'

def clean():
    for folder in ['build', 'dist', '__pycache__']:
        if os.path.exists(folder):
            shutil.rmtree(folder)
            print(f'  🗑️  {folder}/ removido')
    for f in os.listdir('.'):
        if f.endswith('.spec'):
            os.remove(f)

def get_ffmpeg_data():
    """Localiza o FFmpeg do imageio-ffmpeg para incluir no bundle."""
    try:
        import imageio_ffmpeg
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        ffmpeg_dir = os.path.dirname(ffmpeg_exe)
        return [(ffmpeg_dir, 'imageio_ffmpeg/binaries')]
    except ImportError:
        print('  ⚠️  imageio-ffmpeg não instalado. Instale: pip install imageio-ffmpeg')
        return []

def build():
    print(f'\n🔨 Construindo {NOME} v{VERSAO}...')
    print(f'   Plataforma: {sys.platform}\n')

    datas = get_ffmpeg_data()
    if os.path.exists(ICON_PNG):
        datas.append((ICON_PNG, '.'))

    args = [
        sys.executable, '-m', 'PyInstaller',
        '--onefile',
        '--noconsole',
        '--clean',
        f'--name={NOME}',
        '--hidden-import=imageio_ffmpeg',
        '--hidden-import=websockets',
        '--hidden-import=pystray',
        '--hidden-import=PIL',
        '--hidden-import=PIL.Image',
        '--hidden-import=PIL.ImageDraw',
        '--hidden-import=tkinter',
    ]

    for src, dst in datas:
        args.append(f'--add-data={src}{os.pathsep}{dst}')

    if sys.platform == 'win32' and os.path.exists(ICON_WIN):
        args.append(f'--icon={ICON_WIN}')
    elif sys.platform == 'darwin' and os.path.exists(ICON_MAC):
        args.append(f'--icon={ICON_MAC}')
    elif os.path.exists(ICON_PNG):
        args.append(f'--icon={ICON_PNG}')

    args.append(ENTRY_POINT)

    print('   Executando PyInstaller...')
    result = subprocess.run(args, cwd=os.path.dirname(os.path.abspath(__file__)))

    if result.returncode == 0:
        dist_path = os.path.join('dist', NOME)
        if sys.platform == 'darwin':
            app_path = os.path.join('dist', f'{NOME}.app')
            print(f'\n✅ Mac app gerado: {os.path.abspath(app_path)}')
        elif sys.platform == 'win32':
            dist_path += '.exe'
            print(f'\n✅ Windows exe gerado: {os.path.abspath(dist_path)}')
            print(f'   Tamanho: {os.path.getsize(dist_path) // 1024 // 1024} MB')
    else:
        print('\n❌ Falha ao gerar executável. Verifique os logs acima.')
        sys.exit(1)

def check_deps():
    try:
        import PyInstaller
        import websockets
        import imageio_ffmpeg
        import pystray
        import PIL
        print('✅ Todas as dependências instaladas')
    except ImportError as e:
        print(f'⚠️  Dependência faltando: {e}')
        print('   Instale: pip install pyinstaller websockets imageio-ffmpeg pystray Pillow')
        sys.exit(1)

if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    if '--clean' in sys.argv:
        clean()
    check_deps()
    build()
