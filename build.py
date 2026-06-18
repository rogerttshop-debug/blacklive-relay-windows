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

NOME        = 'BlackLive-Relay' if sys.platform == 'win32' else 'Black Live'
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

def obfuscate():
    """Obfusca os arquivos com PyArmor antes de empacotar."""
    obf_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'obfuscated')
    if os.path.exists(obf_dir):
        shutil.rmtree(obf_dir)

    candidates = [
        shutil.which('pyarmor'),
        os.path.expanduser('~/Library/Python/3.9/bin/pyarmor'),
        os.path.expanduser('~/Library/Python/3.10/bin/pyarmor'),
        os.path.expanduser('~/.local/bin/pyarmor'),
    ]
    pyarmor = next((p for p in candidates if p and os.path.isfile(p)), None)
    if not pyarmor:
        print('   PyArmor nao encontrado — buildando sem obfuscacao')
        return None

    result = subprocess.run(
        [pyarmor, 'gen', '--output', obf_dir, 'relay_tray.py', 'local_relay.py'],
        cwd=os.path.dirname(os.path.abspath(__file__))
    )
    if result.returncode != 0:
        print('❌ PyArmor falhou — buildando sem obfuscação')
        return None
    print('   Obfuscação concluída')
    return obf_dir


def build():
    print(f'\n🔨 Construindo {NOME} v{VERSAO}...')
    print(f'   Plataforma: {sys.platform}\n')

    base_dir = os.path.dirname(os.path.abspath(__file__))

    # Obfusca o código
    obf_dir = obfuscate()

    datas = get_ffmpeg_data()
    if os.path.exists(ICON_PNG):
        datas.append((ICON_PNG, '.'))

    # Inclui o runtime do PyArmor no bundle
    if obf_dir:
        runtime_dirs = [d for d in os.listdir(obf_dir) if d.startswith('pyarmor_runtime')]
        for rd in runtime_dirs:
            datas.append((os.path.join(obf_dir, rd), rd))

    # Usa os arquivos obfuscados como source
    work_dir = obf_dir if obf_dir else base_dir
    entry    = os.path.join(work_dir, ENTRY_POINT)

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
        '--hidden-import=local_relay',
    ]

    for src, dst in datas:
        args.append(f'--add-data={src}{os.pathsep}{dst}')

    if sys.platform == 'win32' and os.path.exists(os.path.join(base_dir, ICON_WIN)):
        args.append(f'--icon={os.path.join(base_dir, ICON_WIN)}')
    elif sys.platform == 'darwin' and os.path.exists(os.path.join(base_dir, ICON_MAC)):
        args.append(f'--icon={os.path.join(base_dir, ICON_MAC)}')
    elif os.path.exists(os.path.join(base_dir, ICON_PNG)):
        args.append(f'--icon={os.path.join(base_dir, ICON_PNG)}')

    args.append(entry)

    print('   Executando PyInstaller...')
    result = subprocess.run(args, cwd=base_dir)

    if result.returncode == 0:
        dist_path = os.path.join('dist', NOME)
        if sys.platform == 'darwin':
            app_path = os.path.join('dist', f'{NOME}.app')
            # Remove quarantine para evitar "app danificado" no Mac
            subprocess.run(['xattr', '-cr', app_path], capture_output=True)
            subprocess.run(['codesign', '--force', '--deep', '--sign', '-', app_path], capture_output=True)
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
