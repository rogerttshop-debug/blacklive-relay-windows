#!/usr/bin/env python3
"""
BlackLiveCam — TESTE da camera virtual (v0.1)
=============================================
Cria uma camera virtual no Windows (driver Unity Capture) e toca um
padrao animado "BLACK LIVE" pra validar que Live Studio / Chrome /
Live Center enxergam e exibem a camera.

Requisito: registrar o driver 1x com "Instalar Camera Virtual.bat" (admin).
"""
import time
import math
import sys

import numpy as np
import pyvirtualcam
from PIL import Image, ImageDraw

W, H, FPS = 720, 1280, 30


def frame_base():
    """Fundo gradiente escuro estatico (pre-calculado)."""
    y = np.linspace(0, 1, H)[:, None]
    x = np.linspace(0, 1, W)[None, :]
    base = np.zeros((H, W, 3), np.uint8)
    base[..., 0] = (18 + 60 * y).astype(np.uint8)          # R
    base[..., 1] = (10 + 24 * y * x).astype(np.uint8)      # G
    base[..., 2] = (28 + 90 * y).astype(np.uint8)          # B
    return base


def main():
    print("BlackLiveCam TESTE — criando camera virtual...")
    try:
        cam = pyvirtualcam.Camera(width=W, height=H, fps=FPS,
                                  backend="unitycapture",
                                  fmt=pyvirtualcam.PixelFormat.RGB)
    except Exception as e:
        print("")
        print("ERRO ao criar a camera: %s" % e)
        print("")
        print("Provavelmente o driver nao esta registrado ainda.")
        print("Rode ANTES o 'Instalar Camera Virtual.bat' (clicar com direito > executar como administrador),")
        print("depois abra este programa de novo.")
        input("Enter para sair...")
        sys.exit(1)

    print("Camera criada: %s" % cam.device)
    print("Abra o Live Studio / Chrome e escolha essa camera no seletor.")
    print("(Ctrl+C aqui encerra)")

    base = frame_base()
    t0 = time.time()
    n = 0
    while True:
        t = time.time() - t0
        img = Image.fromarray(base.copy())
        d = ImageDraw.Draw(img)

        # bola quicando (prova de movimento/fps)
        bx = int((W - 120) / 2 * (1 + math.sin(t * 1.4)) + 60)
        by = int((H - 320) / 2 * (1 + math.sin(t * 0.9 + 1.3)) + 160)
        d.ellipse((bx - 42, by - 42, bx + 42, by + 42), fill=(220, 38, 38))

        # textos (fonte default do PIL — teste, nao precisa ser bonito)
        d.text((W // 2 - 60, 140), "BLACK LIVE", fill=(255, 255, 255))
        d.text((W // 2 - 78, 170), "CAMERA VIRTUAL — TESTE", fill=(255, 200, 60))
        d.text((W // 2 - 40, H - 160), time.strftime("%H:%M:%S"), fill=(160, 255, 160))
        d.text((W // 2 - 40, H - 130), "frame %d" % n, fill=(140, 140, 255))

        cam.send(np.asarray(img))
        cam.sleep_until_next_frame()
        n += 1


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("encerrado.")
