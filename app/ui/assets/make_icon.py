# -*- coding: utf-8 -*-
"""把生成的 Logo 原图加工成应用图标：去水印 / 统一圆角 / 导出多尺寸。

用法：python make_icon.py <原始png>
产物：logo.png(512)  logo256.png  icon.ico(16~256)
"""
from __future__ import annotations

import os
import sys

import numpy as np
from PIL import Image, ImageDraw

BG = (23, 28, 61)
WHITE = 253
SIZE = 1024
RADIUS = 224
TOL = 20


def clean(src_path: str, out_dir: str) -> None:
    src = Image.open(src_path).convert("RGB").resize((SIZE, SIZE))
    a = np.asarray(src).astype(np.int16)

    # 圆角方框遮罩
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, SIZE - 1, SIZE - 1], radius=RADIUS, fill=255)
    m = np.asarray(mask) > 0

    near_white = np.all(np.abs(a - WHITE) < 14, axis=2)
    near_bg = np.all(np.abs(a - np.array(BG, dtype=np.int16)) < TOL, axis=2)

    out = np.zeros((SIZE, SIZE, 3), dtype=np.uint8)
    out[...] = BG
    keep = (~near_white) & (~near_bg)
    keep[895:, 895:] = False          # 右下角水印区：直接铺背景
    out[keep] = a[keep].astype(np.uint8)

    rgba = np.dstack([out, np.where(m, 255, 0).astype(np.uint8)])
    img = Image.fromarray(rgba, "RGBA")

    # 边缘抗锯齿：把遮罩做成 4 倍超采样
    big = Image.new("L", (SIZE * 4, SIZE * 4), 0)
    ImageDraw.Draw(big).rounded_rectangle(
        [0, 0, SIZE * 4 - 1, SIZE * 4 - 1], radius=RADIUS * 4, fill=255)
    alpha = big.resize((SIZE, SIZE), Image.LANCZOS)
    img.putalpha(alpha)

    os.makedirs(out_dir, exist_ok=True)
    logo = img.resize((512, 512), Image.LANCZOS)
    logo.save(os.path.join(out_dir, "logo.png"))
    img.resize((256, 256), Image.LANCZOS).save(
        os.path.join(out_dir, "logo256.png"))
    img.save(os.path.join(out_dir, "icon.ico"),
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                    (128, 128), (256, 256)])
    print("已生成:", out_dir)


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else ""
    clean(src, os.path.dirname(os.path.abspath(__file__)))
