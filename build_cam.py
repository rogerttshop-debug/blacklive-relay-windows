#!/usr/bin/env python3
"""Build do BlackLiveCam (teste da camera virtual) — onefile com console."""
import sys
import subprocess

args = [
    sys.executable, "-m", "PyInstaller",
    "--onefile",
    "--console",            # teste: console visivel pra ver os erros
    "--clean",
    "--name=BlackLiveCam",
    "--icon=icon.ico",
    "--hidden-import=pyvirtualcam",
    "--hidden-import=numpy",
    "--hidden-import=PIL",
    "blacklive_cam.py",
]
sys.exit(subprocess.run(args).returncode)
