#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_icon.py —— 用 Pillow 生成应用图标：白底 + 黑色行书「卷」

依赖：pip install pillow
输出：assets/app.ico（含 16/24/32/48/64/128/256 多尺寸）
     assets/app_256.png（预览用）

字体优先用「华文行楷」(STXingkai，软笔行书)，依次回退到
楷体 / 华文楷体 / 隶书 / 宋体，最后回退到系统默认字体。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"

# 行书/软笔优先，越靠前越像「软笔行书」
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\STXINGKA.TTF",   # 华文行楷（行书，软笔感最强）
    r"C:\Windows\Fonts\STKAITI.TTF",    # 华文楷体
    r"C:\Windows\Fonts\simkai.ttf",     # 楷体
    r"C:\Windows\Fonts\STLITI.TTF",     # 华文隶书
    r"C:\Windows\Fonts\simfang.ttf",    # 仿宋
    r"C:\Windows\Fonts\simsun.ttc",     # 宋体
]

CHAR = "卷"
CANVAS = 1024          # 先在高分辨率画布上画，最后再降采样，边缘更干净
INK_RATIO = 0.80       # 字占画布的比例
INK_COLOR = (18, 18, 20)   # 近黑，纯黑在大图标上会显得笨重
BG_COLOR = (255, 255, 255)

ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def pick_font() -> Path:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return Path(path)
    raise SystemExit("没有找到可用的中文字体，请手动指定 FONT_CANDIDATES")


def render_glyph(font_path: Path) -> Image.Image:
    """把字画在一张透明大图上，返回裁到墨迹的 RGBA 图。"""
    layer = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    # 字号给足，画完再按实际墨迹缩放，避免不同字体的边距差异
    font = ImageFont.truetype(str(font_path), int(CANVAS * 0.9))
    draw.text((CANVAS / 2, CANVAS / 2), CHAR, font=font, fill=INK_COLOR, anchor="mm")

    box = layer.getbbox()
    if box is None:
        raise SystemExit(f"字体 {font_path.name} 渲染不出「{CHAR}」")
    return layer.crop(box)


def compose(glyph: Image.Image) -> Image.Image:
    """白底画布 + 居中墨迹。"""
    side = CANVAS
    target = int(side * INK_RATIO)

    w, h = glyph.size
    scale = target / max(w, h)
    new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
    glyph = glyph.resize(new_size, Image.LANCZOS)

    canvas = Image.new("RGB", (side, side), BG_COLOR)
    pos = ((side - new_size[0]) // 2, (side - new_size[1]) // 2)
    canvas.paste(glyph, pos, glyph)          # 用自身的 alpha 做蒙版，边缘不会出现黑边
    return canvas


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)

    font_path = pick_font()
    print(f"字体：{font_path.name}")

    canvas = compose(render_glyph(font_path))
    canvas.save(ASSETS / "app_256.png")       # 预览图（1024 原图另存一份方便看细节）
    canvas.save(ASSETS / "app_full.png")

    preview = canvas.resize((256, 256), Image.LANCZOS)
    preview.save(ASSETS / "app_256.png")

    ico = ASSETS / "app.ico"
    canvas.save(ico, format="ICO", sizes=ICO_SIZES)
    print(f"✓ 已生成 {ico}（{ico.stat().st_size / 1024:.1f} KB，含 {len(ICO_SIZES)} 种尺寸）")
    print(f"✓ 预览 {ASSETS / 'app_256.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
