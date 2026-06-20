#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PolyFeed のマーケット用ロゴ(500x500 PNG)。確率の分割=予測市場を表すドーナツ。"""
import os
from PIL import Image, ImageDraw

W = 500
NAVY = (11, 13, 18, 255)
GREEN = (74, 222, 128, 255)
DIM = (74, 222, 128, 70)

img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([0, 0, W - 1, W - 1], radius=112, fill=NAVY)

m = 120
box = [m, m, W - m, W - m]
yes_end = -90 + 0.64 * 360          # YES ≈ 64% を緑、残りを淡色 → "オッズ分割"
d.pieslice(box, -90, yes_end, fill=GREEN)
d.pieslice(box, yes_end, 270, fill=DIM)

hm = 188                            # 中央をくり抜いてドーナツ(リング)に
d.ellipse([hm, hm, W - hm, W - hm], fill=NAVY)
# 中央に小さな緑のドット(マーカー)
d.ellipse([W // 2 - 16, W // 2 - 16, W // 2 + 16, W // 2 + 16], fill=GREEN)

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "polyfeed_logo.png")
img.save(out)
print("saved:", out, img.size)
