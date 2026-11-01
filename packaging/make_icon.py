"""生成 exe 图标 packaging/app.ico（渐变底 + 雪花），避免打包程序默认的空白图标。"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 512
OUT = Path(__file__).with_name("app.ico")


def _gradient(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    c1 = (122, 162, 255)   # 蓝
    c2 = (201, 167, 255)   # 紫
    for y in range(size):
        t = y / max(1, size - 1)
        color = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))
        draw.line([(0, y), (size, y)], fill=color + (255,))
    # 圆角遮罩
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1],
                                           radius=int(size * 0.22), fill=255)
    img.putalpha(mask)
    return img


def _snowflake(draw: ImageDraw.ImageDraw, size: int) -> None:
    cx = cy = size / 2
    arm = size * 0.31
    width = max(3, int(size * 0.028))
    white = (255, 255, 255, 235)
    for k in range(6):
        angle = math.radians(k * 60)
        x = cx + arm * math.cos(angle)
        y = cy + arm * math.sin(angle)
        draw.line([(cx, cy), (x, y)], fill=white, width=width)
        # 每根臂上两对分枝
        for frac, blen in ((0.55, 0.22), (0.78, 0.16)):
            bx = cx + arm * frac * math.cos(angle)
            by = cy + arm * frac * math.sin(angle)
            for sign in (-1, 1):
                ba = angle + sign * math.radians(38)
                draw.line(
                    [(bx, by), (bx + arm * blen * math.cos(ba), by + arm * blen * math.sin(ba))],
                    fill=white,
                    width=max(2, width - 1),
                )
    draw.ellipse([cx - width, cy - width, cx + width, cy + width], fill=white)


def main() -> int:
    base = _gradient(SIZE)
    draw = ImageDraw.Draw(base)
    _snowflake(draw, SIZE)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    base.save(OUT, format="ICO",
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"icon written: {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
