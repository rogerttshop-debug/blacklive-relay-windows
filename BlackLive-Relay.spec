# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['relay_tray.py'],
    pathex=[],
    binaries=[],
    datas=[('/Users/johne/Library/Python/3.9/lib/python/site-packages/imageio_ffmpeg/binaries', 'imageio_ffmpeg/binaries'), ('icon.png', '.')],
    hiddenimports=['imageio_ffmpeg', 'websockets', 'pystray', 'PIL', 'PIL.Image', 'PIL.ImageDraw', 'tkinter'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='BlackLive-Relay',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.png'],
)
app = BUNDLE(
    exe,
    name='BlackLive-Relay.app',
    icon='icon.png',
    bundle_identifier=None,
)
