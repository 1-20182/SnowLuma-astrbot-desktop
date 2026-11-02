"""生成应用图标：Windows 用 packaging/app.ico，Linux/Electron 用 desktop/assets/icon.png。

为什么要手写 PNG 编码（而不是又装个库）：
  * 打包链路要求「无网也能构建」，加依赖就意味着离线环境装不上；
  * Python 标准库自带 zlib，写一个 8bit RGBA 的真彩 PNG 只要几十行；
  * PNG 这条路径**完全不依赖 Pillow**，所以 Linux 构建机上不装 Pillow 也能出图标。
    ICO 只能靠 Pillow（Windows 打包机上一键启动脚本里已经装了），没有 Pillow 时只跳过 ICO。

用法：
    python packaging/make_icon.py                 # 同时生成 app.ico 与 desktop/assets/icon.png
    python packaging/make_icon.py --png out.png   # 只生成 PNG
    python packaging/make_icon.py --size 512      # 指定 PNG 像素尺寸（默认 512）
"""

from __future__ import annotations

import argparse
import math
import struct
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ICO_OUT = HERE / "app.ico"
PNG_OUT = ROOT / "desktop" / "assets" / "icon.png"

# 与界面主色保持一致：上蓝下紫
COLOR_TOP = (122, 162, 255)
COLOR_BOTTOM = (201, 167, 255)
SNOWFLAKE = (255, 255, 255, 235)


# --------------------------------------------------------------------------- #
# 几何：圆角矩形 + 六角雪花（用「点到线段的距离」做矢量渲染，不依赖任何绘图库）
# --------------------------------------------------------------------------- #
def _rounded_rect_coverage(x: float, y: float, size: float, radius: float) -> float:
    """圆角矩形的覆盖率（边缘给 1px 羽化，避免锯齿）。"""

    cx = cy = (size - 1) / 2.0
    half = size / 2.0
    dx = abs(x - cx) - (half - radius)
    dy = abs(y - cy) - (half - radius)
    outside_x, outside_y = max(dx, 0.0), max(dy, 0.0)
    dist = math.hypot(outside_x, outside_y) - radius if (dx > 0 or dy > 0) else max(dx, dy) - radius
    # 内部（dist<0）为 1，外部渐隐到 0
    return min(1.0, max(0.0, 0.5 - dist))


def _snowflake_segments(size: float) -> list[tuple[float, float, float, float, float]]:
    """返回 [(x1, y1, x2, y2, half_width), ...]：6 根主臂 + 每根 2 对分枝。"""

    cx = cy = (size - 1) / 2.0
    arm = size * 0.31
    width = max(3, int(size * 0.028))
    segments: list[tuple[float, float, float, float, float]] = []
    for k in range(6):
        angle = math.radians(k * 60)
        tip = (cx + arm * math.cos(angle), cy + arm * math.sin(angle))
        segments.append((cx, cy, tip[0], tip[1], width / 2.0))
        for frac, blen in ((0.55, 0.22), (0.78, 0.16)):
            bx = cx + arm * frac * math.cos(angle)
            by = cy + arm * frac * math.sin(angle)
            for sign in (-1, 1):
                ba = angle + sign * math.radians(38)
                segments.append((bx, by,
                                 bx + arm * blen * math.cos(ba),
                                 by + arm * blen * math.sin(ba),
                                 max(2, width - 1) / 2.0))
    return segments


def _segment_distance(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    vx, vy = x2 - x1, y2 - y1
    wx, wy = px - x1, py - y1
    denom = vx * vx + vy * vy
    t = 0.0 if denom == 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / denom))
    return math.hypot(wx - t * vx, wy - t * vy)


def render_rgba(size: int) -> bytes:
    """渲染 size×size 的 RGBA 像素（行优先，每像素 4 字节）。"""

    total = size * size
    flake = bytearray(total)          # 雪花覆盖率（0-255）
    for x1, y1, x2, y2, half_width in _snowflake_segments(size):
        x_lo = max(0, int(min(x1, x2) - half_width - 1))
        x_hi = min(size - 1, int(max(x1, x2) + half_width + 1))
        y_lo = max(0, int(min(y1, y2) - half_width - 1))
        y_hi = min(size - 1, int(max(y1, y2) + half_width + 1))
        for y in range(y_lo, y_hi + 1):
            row = y * size
            for x in range(x_lo, x_hi + 1):
                dist = _segment_distance(x, y, x1, y1, x2, y2) - half_width
                coverage = int(min(1.0, max(0.0, 0.5 - dist)) * 255)
                if coverage > flake[row + x]:
                    flake[row + x] = coverage

    radius = size * 0.22
    out = bytearray(total * 4)
    for y in range(size):
        t = y / max(1, size - 1)
        base = tuple(int(COLOR_TOP[i] + (COLOR_BOTTOM[i] - COLOR_TOP[i]) * t) for i in range(3))
        row = y * size
        for x in range(size):
            bg_alpha = _rounded_rect_coverage(x, y, size, radius)
            idx = (row + x) * 4
            if bg_alpha <= 0.0:
                continue                     # 全透明（圆角外的角落）
            r, g, b = base
            a = int(bg_alpha * 255)
            # 雪花叠上去（source-over，白色带一点透明）
            f = (flake[row + x] / 255.0) * (SNOWFLAKE[3] / 255.0) * bg_alpha
            if f > 0:
                fr, fg, fb = SNOWFLAKE[:3]
                r = int(r + (fr - r) * f)
                g = int(g + (fg - g) * f)
                b = int(b + (fb - b) * f)
            out[idx:idx + 4] = bytes((r, g, b, a))
    return bytes(out)


def png_bytes(size: int) -> bytes:
    """把 RGBA 像素编码成 PNG（8bit/通道、无交错、filter=0）。"""

    raw = bytearray()
    pixels = render_rgba(size)
    stride = size * 4
    for y in range(size):
        raw.append(0)                       # 每行的 filter type：0 = None
        raw += pixels[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8bit, RGBA
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


# --------------------------------------------------------------------------- #
# ICO：Windows 打包资源，交给 Pillow（它能把多尺寸 ICO 一次写好）
# --------------------------------------------------------------------------- #
def write_ico(out: Path = ICO_OUT, size: int = 512) -> bool:
    try:
        from PIL import Image, ImageDraw
    except Exception:  # noqa: BLE001 - 没装 Pillow 就只跳过 ICO，PNG 仍然能出
        print("跳过 ICO：没有安装 Pillow（Linux 构建机不需要它）")
        return False

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for y in range(size):
        t = y / max(1, size - 1)
        color = tuple(int(COLOR_TOP[i] + (COLOR_BOTTOM[i] - COLOR_TOP[i]) * t) for i in range(3))
        draw.line([(0, y), (size, y)], fill=color + (255,))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1],
                                           radius=int(size * 0.22), fill=255)
    img.putalpha(mask)
    for x1, y1, x2, y2, half_width in _snowflake_segments(size):
        draw.line([(x1, y1), (x2, y2)], fill=SNOWFLAKE, width=max(2, int(half_width * 2)))

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, format="ICO",
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"icon written: {out} ({out.stat().st_size} bytes)")
    return True


def write_png(out: Path = PNG_OUT, size: int = 512) -> Path:
    data = png_bytes(size)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"icon written: {out} ({size}x{size}, {len(data)} bytes)")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成应用图标（ICO / PNG）")
    parser.add_argument("--ico", type=Path, default=None, help=f"ICO 输出路径（默认 {ICO_OUT}）")
    parser.add_argument("--png", type=Path, default=None, help=f"PNG 输出路径（默认 {PNG_OUT}）")
    parser.add_argument("--size", type=int, default=512, help="PNG 边长，默认 512")
    parser.add_argument("--png-only", action="store_true", help="只生成 PNG（Linux 构建用）")
    args = parser.parse_args(argv)

    if args.png is not None or args.png_only or args.ico is None:
        write_png(args.png or PNG_OUT, args.size)
    if not args.png_only:
        write_ico(args.ico or ICO_OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
