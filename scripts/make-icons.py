#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 FPK更新 的**全部**图标 —— 一份几何定义，产出所有槽位。

为什么要一份生成器而不是各处手写：
    这个应用有 7 处图标 —— fnOS 的 ICON.PNG / ICON_256.PNG、桌面入口的
    app/ui/images/icon_64.png / icon_256.png、网页 favicon.svg、网页页头标志、
    以及「被管理应用没有图标」时的灰色回退图。
    以前 PNG 由本脚本生成、SVG 各处手写，结果**圆角比例都不一样**
    （PNG 的圆角是边长的 16%，手写的 favicon 是 59%）—— 同一个应用在不同位置
    看起来像两个图标。现在全部从下面这套常量算出来，不可能再漂。

产出（全部覆盖写入）：
    ICON.PNG                       fnOS 应用图标
    ICON_256.PNG                   fnOS 应用图标（小尺寸槽位）
    app/ui/images/icon_64.png      桌面入口图标
    app/ui/images/icon_256.png     桌面入口图标
    app/www/favicon.svg            网页图标
    app/www/icon.svg               网页页头标志（index.html 引它，不再内联手写 SVG）
    app/www/default-icon.svg       「没有图标的应用」的灰色回退图（同一套圆角/线宽）

用法：python3 scripts/make-icons.py <仓库根目录>
      python3 scripts/make-icons.py <仓库根目录> --check    # 只校验与现文件是否一致
"""

import math
import os
import struct
import sys
import zlib

# ── 目标尺寸（照既有先例：四个槽位均为 512²）──
OUT_SIZE = 512
SS = 4                        # 超采样倍数（2048² 上渲染再降采样）

# ── 主图标配色（与网页 CSS 同一套）──
C1 = (0x4f, 0x7c, 0xff)
C2 = (0x7b, 0x5c, 0xff)
WHITE = (0xff, 0xff, 0xff)

# 圆角方块。世界坐标：整个画布是 [-1, 1]（1.0 = 半个画布）。
# 圆角占边长比例 = BOX_RADIUS / (2*BOX_HALF) = 0.40/1.84 ≈ 21.7%，
# 与主流 App 图标一致（iOS 约 22%）。上一版是 16%，明显偏方。
BOX_HALF = 0.92
BOX_RADIUS = 0.40

# 「下载 / 更新」标记：**实心箭头**（三角箭头 + 矩形杆）+ 底部托盘。
#
# 为什么不用「几根胶囊拼箭头」：那种拼法在杆与两撇的接缝处必然留一个凹口（视觉上是个毛刺），
# 托盘也会显得像药丸。改成「矩形杆 ∪ 三角头」后，两者共用同一条水平边，接缝天然无缝；
# 再整体外扩 CORNER 做圆角，就得到一枚边缘干净、任意尺寸都锐利的实心箭头。
#
# 世界坐标：画布 [-1, 1]。所有形状先按「尖锐几何」定义，再由 CORNER 统一做圆角外扩 ——
# 光栅化是 d - CORNER，矢量是「矩形先外扩 / 多边形用 stroke-linejoin:round」，
# 两条路算的是同一个形状，所以 PNG 与 SVG 不会像以前那样各写各的。
CORNER = 0.02

# 杆：矩形 x∈[-0.085, 0.085]，y∈[-0.575, -0.095]
STEM_RECT = (-0.085, -0.595, 0.085, -0.115)
# 头：三角 (±0.295, -0.095) 与顶点 (0, 0.27)。顶边与杆的底边**完全重合** → 无缝
HEAD_TRI = ((-0.295, -0.115), (0.295, -0.115), (0.0, 0.25))
# 托盘：圆角横条（中心x, 中心y, 半长, 半粗, 圆角）
TRAY_BOX = (0.0, 0.545, 0.35, 0.065, 0.032)

# 回退图（没有图标的应用）配色：中性灰，几何参数与主图标同族
GREY1 = (0xee, 0xf1, 0xf6)
GREY2 = (0xd8, 0xdd, 0xe7)
GREY_INK = "#8b93a3"


# 回退图（没有图标的应用）配色：中性灰，几何参数与主图标同族
GREY1 = (0xee, 0xf1, 0xf6)
GREY2 = (0xd8, 0xdd, 0xe7)
GREY_INK = "#8b93a3"


# ───────────────────────────── SDF（光栅化） ─────────────────────────────

def sd_round_box(px, py, hx, hy, r):
    qx = abs(px) - hx + r
    qy = abs(py) - hy + r
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r


def sd_seg(px, py, ax, ay, bx, by):
    """点到线段的距离。"""
    ex, ey = bx - ax, by - ay
    vx, vy = px - ax, py - ay
    ee = ex * ex + ey * ey
    t = 0.0 if ee == 0.0 else max(0.0, min(1.0, (vx * ex + vy * ey) / ee))
    return math.hypot(vx - ex * t, vy - ey * t)


def sd_triangle(px, py, tri):
    """三角形 SDF（iq 的标准做法，含内外部判定）。"""
    (ax, ay), (bx, by), (cx, cy) = tri
    e0 = (bx - ax, by - ay)
    e1 = (cx - bx, cy - by)
    e2 = (ax - cx, ay - cy)
    v0 = (px - ax, py - ay)
    v1 = (px - bx, py - by)
    v2 = (px - cx, py - cy)

    def dp(a, b):
        return a[0] * b[0] + a[1] * b[1]

    def cl01(v, e):
        ee = dp(e, e)
        return 0.0 if ee == 0.0 else max(0.0, min(1.0, dp(v, e) / ee))

    p0 = (v0[0] - e0[0] * cl01(v0, e0), v0[1] - e0[1] * cl01(v0, e0))
    p1 = (v1[0] - e1[0] * cl01(v1, e1), v1[1] - e1[1] * cl01(v1, e1))
    p2 = (v2[0] - e2[0] * cl01(v2, e2), v2[1] - e2[1] * cl01(v2, e2))
    d = min(min(dp(p0, p0), dp(p1, p1)), dp(p2, p2))
    s = 1.0 if (e0[0] * e2[1] - e0[1] * e2[0]) > 0 else -1.0
    dd = min(min((v0[0] * e0[1] - v0[1] * e0[0]) * s,
                 (v1[0] * e1[1] - v1[1] * e1[0]) * s),
             (v2[0] * e2[1] - v2[1] * e2[0]) * s)
    return -math.sqrt(d) * (1.0 if dd > 0 else -1.0)


def mark_sdf(x, y):
    """白色主标记 = 实心箭头（矩形杆 ∪ 三角头）∪ 托盘，整体外扩 CORNER 做圆角。"""
    x0, y0, x1, y1 = STEM_RECT
    stem = sd_round_box(x - (x0 + x1) / 2.0, y - (y0 + y1) / 2.0,
                        (x1 - x0) / 2.0, (y1 - y0) / 2.0, 0.0)
    head = sd_triangle(x, y, HEAD_TRI)
    cx, cy, hl, ht, r = TRAY_BOX
    tray = sd_round_box(x - cx, y - cy, hl, ht, r)
    return min(min(stem, head), tray) - CORNER


def coverage(d, aa):
    """SDF → 覆盖率（解析抗锯齿）。d<0 在内部。"""
    return min(1.0, max(0.0, 0.5 - d / aa))


def lerp(a, b, t):
    return a + (b - a) * t


def render(size):
    """渲染 size×size 的 RGBA 字节（主图标）。"""
    n = size * SS
    aa = 2.0 / n
    out = bytearray(size * size * 4)
    for oy in range(size):
        for ox in range(size):
            r_acc = g_acc = b_acc = a_acc = 0.0
            for sy in range(SS):
                for sx in range(SS):
                    # 世界坐标 [-1, 1]，y 向下
                    x = ((ox * SS + sx + 0.5) / n) * 2.0 - 1.0
                    y = ((oy * SS + sy + 0.5) / n) * 2.0 - 1.0
                    bg_a = coverage(sd_round_box(x, y, BOX_HALF, BOX_HALF, BOX_RADIUS), aa)
                    if bg_a <= 0.0:
                        continue
                    t = min(1.0, max(0.0, (x + y) * 0.5 * 0.5 + 0.5))
                    bgc = (lerp(C1[0], C2[0], t), lerp(C1[1], C2[1], t), lerp(C1[2], C2[2], t))
                    ar_a = coverage(mark_sdf(x, y), aa) * bg_a
                    if ar_a > 0.0:
                        k = ar_a / bg_a if bg_a else 0.0
                        cr = lerp(bgc[0], WHITE[0], k)
                        cg = lerp(bgc[1], WHITE[1], k)
                        cb = lerp(bgc[2], WHITE[2], k)
                    else:
                        cr, cg, cb = bgc
                    r_acc += cr * bg_a
                    g_acc += cg * bg_a
                    b_acc += cb * bg_a
                    a_acc += bg_a
            cnt = SS * SS
            a = a_acc / cnt
            if a_acc > 0:
                r, g, b = r_acc / a_acc, g_acc / a_acc, b_acc / a_acc
            else:
                r = g = b = 0.0
            i = (oy * size + ox) * 4
            out[i] = int(round(min(255.0, max(0.0, r))))
            out[i + 1] = int(round(min(255.0, max(0.0, g))))
            out[i + 2] = int(round(min(255.0, max(0.0, b))))
            out[i + 3] = int(round(min(255.0, max(0.0, a * 255.0))))
    return bytes(out)


def write_png(path, size, rgba):
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)                        # filter: None
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data +
                struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "wb") as f:
        f.write(png)
    return len(png)


# ───────────────────────────── SVG（矢量槽位） ─────────────────────────────
#
# 关键：下面的坐标全部由上面同一套常量算出（世界坐标 → viewBox 坐标），
# 所以矢量版与光栅版的圆角比例、线宽、箭头位置必然一致。

def _f(v):
    """数字格式化：够精确又稳定（不把 0.30000000004 写进文件）。"""
    s = ("%.3f" % float(v)).rstrip("0").rstrip(".")
    return s if s else "0"


def _w2v(v, view):
    """世界坐标 → viewBox 坐标。"""
    return (v + 1.0) * (view / 2.0)


def mark_svg(view=48):
    """页头 / favicon 用的主图标 SVG —— 与 PNG 同一套几何（含同样的圆角外扩）。"""
    half = view / 2.0
    inset = (1.0 - BOX_HALF) * half
    side = view - 2 * inset
    rx = BOX_RADIUS * half
    d_corner = CORNER * half
    x0, y0, x1, y1 = STEM_RECT
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
        'width="%d" height="%d" role="img" aria-label="FPK更新">' % (view, view, view, view),
        '  <!-- 由 scripts/make-icons.py 生成，与 ICON.PNG 共用同一套几何常量，'
        '请勿手改 -->',
        '  <defs>',
        '    <linearGradient id="fpg" x1="0" y1="0" x2="1" y2="1">',
        '      <stop offset="0" stop-color="#%02x%02x%02x"/>' % C1,
        '      <stop offset="1" stop-color="#%02x%02x%02x"/>' % C2,
        '    </linearGradient>',
        '  </defs>',
        '  <rect x="%s" y="%s" width="%s" height="%s" rx="%s" fill="url(#fpg)"/>'
        % (_f(inset), _f(inset), _f(side), _f(side), _f(rx)),
        '  <g fill="#FFFFFF">',
        # 杆：矩形先按 CORNER 外扩（= 光栅里 d-CORNER 的等价形状）
        '    <rect x="%s" y="%s" width="%s" height="%s" rx="%s"/>'
        % (_f(_w2v(x0, view) - d_corner), _f(_w2v(y0, view) - d_corner),
           _f((x1 - x0) * half + 2 * d_corner), _f((y1 - y0) * half + 2 * d_corner),
           _f(d_corner)),
        # 头：三角填充 + round join 描边（= 同样外扩 CORNER）
        '    <path d="M%s %sL%s %sL%s %sZ" stroke="#FFFFFF" stroke-width="%s" '
        'stroke-linejoin="round"/>'
        % tuple([v for t in HEAD_TRI
                 for v in (_f(_w2v(t[0], view)), _f(_w2v(t[1], view)))]
                + [_f(d_corner * 2)]),
        # 托盘：圆角横条，同样外扩
        '    <rect x="%s" y="%s" width="%s" height="%s" rx="%s"/>'
        % (_f(_w2v(TRAY_BOX[0], view) - (TRAY_BOX[2] + CORNER) * half),
           _f(_w2v(TRAY_BOX[1], view) - (TRAY_BOX[3] + CORNER) * half),
           _f((TRAY_BOX[2] + CORNER) * 2 * half),
           _f((TRAY_BOX[3] + CORNER) * 2 * half),
           _f((TRAY_BOX[4] + CORNER) * half)),
        '  </g>',
        '</svg>',
        '',
    ]
    return "\n".join(parts)


def fallback_svg(view=96):
    """「没有图标的应用」的回退图：同一套圆角比例与线宽语言，中性灰。

    画一个通用「包裹」轮廓 —— 不抢戏，一眼看出「这个没有图标」。
    """
    rx = BOX_RADIUS / BOX_HALF * (view / 2.0)          # 与主图标同圆角比例
    sw = TRAY_BOX[3] * 2 * (view / 2.0)                    # 与托盘同线宽
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
        'width="%d" height="%d" role="img" aria-label="没有图标">' % (view, view, view, view),
        '  <!-- 由 scripts/make-icons.py 生成，请勿手改 -->',
        '  <defs>',
        '    <linearGradient id="fbg" x1="0" y1="0" x2="1" y2="1">',
        '      <stop offset="0" stop-color="#%02x%02x%02x"/>' % GREY1,
        '      <stop offset="1" stop-color="#%02x%02x%02x"/>' % GREY2,
        '    </linearGradient>',
        '  </defs>',
        '  <rect x="0" y="0" width="%d" height="%d" rx="%s" fill="url(#fbg)"/>'
        % (view, view, _f(rx)),
        '  <g fill="none" stroke="%s" stroke-width="%s" stroke-linecap="round" '
        'stroke-linejoin="round">' % (GREY_INK, _f(sw)),
        '    <path d="M30 29l18-9 18 9v32l-18 9-18-9z"/>',
        '    <path d="M30 29l18 9 18-9"/>',
        '    <path d="M48 38v32"/>',
        '  </g>',
        '</svg>',
        '',
    ]
    return "\n".join(parts)


# ───────────────────────────── 产出 ─────────────────────────────

PNG_SLOTS = (
    "ICON.PNG",
    "ICON_256.PNG",
    "app/ui/images/icon_64.png",
    "app/ui/images/icon_256.png",
)
SVG_SLOTS = (
    ("app/www/favicon.svg", mark_svg(48)),
    ("app/www/icon.svg", mark_svg(48)),
    ("app/www/default-icon.svg", fallback_svg(96)),
)
ALL_SLOTS = PNG_SLOTS + tuple(r for r, _ in SVG_SLOTS)


def generate(root, quiet=False):
    if not quiet:
        print("渲染 %d×%d（%d 倍超采样）…" % (OUT_SIZE, OUT_SIZE, SS))
    rgba = render(OUT_SIZE)
    for rel in PNG_SLOTS:                    # 同一份像素 → 四个槽位逐字节一致
        n = write_png(os.path.join(root, rel), OUT_SIZE, rgba)
        if not quiet:
            print("  ✓ %-32s %8d 字节" % (rel, n))
    for rel, text in SVG_SLOTS:
        p = os.path.join(root, rel)
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        data = text.encode("utf-8")
        with open(p, "wb") as f:
            f.write(data)
        if not quiet:
            print("  ✓ %-32s %8d 字节" % (rel, len(data)))
    return list(PNG_SLOTS) + [r for r, _ in SVG_SLOTS]


def check(root):
    """校验仓库里的图标与「现生成」的结果一致 —— 抓住任何手改/漂移。"""
    import tempfile
    bad = []
    with tempfile.TemporaryDirectory() as tmp:
        generate(tmp, quiet=True)
        for rel in ALL_SLOTS:
            a, b = os.path.join(root, rel), os.path.join(tmp, rel)
            da = open(a, "rb").read() if os.path.isfile(a) else b""
            db = open(b, "rb").read()
            if da != db:
                bad.append(rel)
    if bad:
        for rel in bad:
            print("::error::图标与生成器不一致（被手改了？）：%s" % rel)
        print("✘ %d 个图标槽位与生成器不一致 —— 请重跑 scripts/make-icons.py 并提交" % len(bad))
        return 1
    print("✅ 全部图标（4 个 PNG 槽位 + favicon + 页头标志 + 回退图）都出自同一套几何，完全一致")
    return 0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = args[0] if args else "."
    if "--check" in sys.argv:
        return check(root)
    generate(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
