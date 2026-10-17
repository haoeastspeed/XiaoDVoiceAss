# -*- coding: utf-8 -*-
"""
gen_xiaod_emoji.py — 生成「小D」卡通动态表情（GIF），替换小智固件屏幕表情。

角色设计：薄荷绿圆润小机器人
  - 大眼睛（含高光）、腮红、天线 + 随情绪变色的天线球
  - 全部表情共享同一张脸，参数化差异（眼型/嘴型/附加元素/动画）

输出：firmware/xiaozhi-esp32/main/assets/emoji/xiaod-gif/<name>.gif
  名字 = 固件 SetEmotion() 的字符串（21 官方名 + listening/speaking/cancel）

用法：python tools/gen_xiaod_emoji.py [--out DIR] [--preview]
GIF 规格：160x160、透明背景、disposal=2，gifdec 直接在 flash mmap 上解码。
"""

import argparse
import math
import os
import sys

from PIL import Image, ImageDraw, ImageFont

# ---------- 全局参数 ----------
OUT_SIZE = 160        # 最终尺寸
SCALE = 2             # 2x 绘制再缩小，边缘抗锯齿
S = OUT_SIZE * SCALE  # 画布 320

# 调色板
OUTLINE   = (47, 58, 70, 255)       # 深蓝灰描边
BODY      = (95, 212, 196, 255)     # 薄荷绿
BODY_DARK = (66, 184, 170, 255)
BODY_LIGHT= (158, 236, 222, 255)
WHITE     = (255, 255, 255, 255)
PUPIL     = (43, 56, 70, 255)
MOUTH_IN  = (126, 44, 62, 255)      # 口腔暗红
TONGUE    = (255, 122, 138, 255)
BLUSH     = (255, 148, 176, 255)
SWEAT     = (140, 205, 245, 255)
TEAR      = (120, 190, 240, 255)
HEART     = (255, 96, 130, 255)
SUNGLASS  = (40, 44, 52, 255)
YELLOW    = (255, 208, 92, 255)

# 天线球心情色
MOOD = {
    "neutral": BODY_LIGHT, "happy": YELLOW, "laughing": YELLOW, "funny": YELLOW,
    "sad": (120, 170, 235, 255), "crying": (120, 170, 235, 255),
    "angry": (255, 110, 100, 255), "surprised": YELLOW, "shocked": (255, 110, 100, 255),
    "embarrassed": BLUSH, "thinking": (180, 150, 240, 255), "winking": YELLOW,
    "cool": (110, 200, 250, 255), "relaxed": (170, 230, 180, 255),
    "delicious": (255, 170, 90, 255), "kissy": HEART, "confident": YELLOW,
    "sleepy": (150, 160, 200, 255), "silly": (255, 190, 90, 255),
    "confused": (200, 200, 210, 255), "loving": HEART,
    "listening": (110, 240, 210, 255), "speaking": BODY_LIGHT,
    "cancel": (170, 170, 175, 255),
}

# 几何（2x 坐标）
CX = 160
HEAD = (36, 76, 284, 296)     # 头部椭圆
EYE_Y = 172
EYE_L, EYE_R = 110, 210
MOUTH_Y = 234


def font_at(path_candidates, size):
    for p in path_candidates:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


FONT_BOLD = font_at([
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\msyhbd.ttc",
], 44)
FONT_SMALL = font_at([
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\msyhbd.ttc",
], 30)


def canvas():
    return Image.new("RGBA", (S, S), (0, 0, 0, 0))


def layer(img):
    """新建透明图层（用于半透明元素），返回 (layer, draw)。"""
    ly = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    return ly, ImageDraw.Draw(ly)


def composite(img, ly):
    return Image.alpha_composite(img, ly)


# ---------- 基础部件 ----------
def draw_antenna(d, img, mood, pulse=0.0):
    """天线 + 情绪球。pulse: 0~1 发光强度（发光层合成后重新绑定 draw）。"""
    d.line([(CX, HEAD[1] + 6), (CX, 50)], fill=OUTLINE, width=7)
    if pulse > 0:
        ly, ld = layer(img)
        r = 18 + int(8 * pulse)
        ld.ellipse([CX - r, 36 - r, CX + r, 36 + r],
                   fill=(mood[0], mood[1], mood[2], int(70 * pulse)))
        img = composite(img, ly)
        d = ImageDraw.Draw(img)  # 合成后必须重新绑定
    d.ellipse([CX - 14, 22, CX + 14, 50], fill=mood, outline=OUTLINE, width=4)
    d.ellipse([CX - 7, 27, CX - 1, 33], fill=WHITE)
    return img


def draw_head(img, oy=0, mood=BODY_LIGHT, pulse=0.0):
    """头 + 底部月牙阴影 + 顶部高光 + 天线。返回合成后的 img。"""
    d = ImageDraw.Draw(img)
    bbox = (HEAD[0], HEAD[1] + oy, HEAD[2], HEAD[3] + oy)
    d.line([(CX, HEAD[1] + 6 + oy), (CX, 50 + oy)], fill=OUTLINE, width=7)
    d.ellipse(bbox, fill=BODY)
    # 底部月牙阴影：head − head 下移 46px，只留下缘弧条
    head_mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(head_mask).ellipse(
        [bbox[0] + 3, bbox[1] + 3, bbox[2] - 3, bbox[3] - 3], fill=255)
    shifted = Image.new("L", (S, S), 0)
    ImageDraw.Draw(shifted).ellipse(
        [bbox[0], bbox[1] + 46, bbox[2], bbox[3] + 46], fill=255)
    from PIL import ImageChops
    crescent = ImageChops.subtract(head_mask, shifted)
    shade = Image.new("RGBA", (S, S), BODY_DARK)
    img = Image.composite(
        Image.alpha_composite(img, shade),
        img, crescent)
    d = ImageDraw.Draw(img)
    d.ellipse(bbox, outline=OUTLINE, width=6)
    # 顶部高光弧
    inset = 18
    d.arc((bbox[0] + inset, bbox[1] + inset + 10, bbox[2] - inset, bbox[3] - inset),
          start=195, end=300, fill=BODY_LIGHT, width=13)
    return draw_antenna(d, img, mood, pulse)


def eye_open(d, cx, cy, look=(0, 0), scale=1.0, lid=0.0):
    """睁眼。look: 瞳孔偏移；lid: 0~1 上眼睑压下程度。"""
    w, h = 58 * scale, 66 * scale
    bbox = [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]
    d.ellipse(bbox, fill=WHITE, outline=OUTLINE, width=5)
    pr = 19 * scale
    px, py = cx + look[0], cy + look[1] + lid * 12
    d.ellipse([px - pr, py - pr, px + pr, py + pr], fill=PUPIL)
    d.ellipse([px - 12, py - 13, px + 1, py], fill=WHITE)
    d.ellipse([px + 4, py + 5, px + 11, py + 12], fill=WHITE)
    if lid > 0.2:
        d.line([bbox[0] + 3, bbox[1] + 6 + lid * h * 0.45,
                bbox[2] - 3, bbox[1] + 6 + lid * h * 0.45],
               fill=OUTLINE, width=7)


def eye_arc_up(d, cx, cy, w=27):
    """开心闭眼 ∩。"""
    d.arc([cx - w, cy - 20, cx + w, cy + 26], start=185, end=355, fill=OUTLINE, width=9)


def eye_closed(d, cx, cy, w=27):
    """安眠闭眼 平缓 ∪。"""
    d.arc([cx - w, cy - 22, cx + w, cy + 20], start=15, end=165, fill=OUTLINE, width=9)


def eye_x(d, cx, cy, w=17):
    d.line([cx - w, cy - w, cx + w, cy + w], fill=OUTLINE, width=9)
    d.line([cx - w, cy + w, cx + w, cy - w], fill=OUTLINE, width=9)


def eye_heart(d, cx, cy, s=30):
    heart(d, cx, cy + 2, s, HEART, OUTLINE, 4)


def eye_wide(d, cx, cy, look=(0, 0)):
    eye_open(d, cx, cy, look, scale=1.18)


def brow(d, cx, cy, angle, w=9, ln=30):
    """眉毛。angle>0 内端向下（怒），<0 外端向下（愁）。"""
    dx = math.cos(math.radians(angle)) * ln
    dy = math.sin(math.radians(angle)) * ln
    d.line([cx - dx, cy - dy, cx + dx, cy + dy], fill=OUTLINE, width=w)


def mouth(d, style, cx=CX, cy=MOUTH_Y, **kw):
    if style == "smile":
        w = kw.get("w", 60)
        d.arc([cx - w / 2, cy - w / 3, cx + w / 2, cy + w / 3 + 8],
              start=25, end=155, fill=OUTLINE, width=9)
    elif style == "big_smile":
        bbox = [cx - 42, cy - 12, cx + 42, cy + 36]
        d.chord(bbox, start=0, end=180, fill=MOUTH_IN, outline=OUTLINE, width=5)
        d.ellipse([cx - 24, cy + 12, cx + 24, cy + 38], fill=TONGUE)
        d.ellipse([cx - 24, cy + 12, cx + 24, cy + 38], outline=OUTLINE, width=3)
    elif style == "talk":
        h = kw.get("h", 16)
        w = kw.get("w", 46)
        bbox = [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]
        d.ellipse(bbox, fill=MOUTH_IN, outline=OUTLINE, width=5)
        if h > 14:
            d.ellipse([cx - 16, cy + h / 2 - 12, cx + 16, cy + h / 2 + 6], fill=TONGUE)
    elif style == "o":
        r = kw.get("r", 15)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=MOUTH_IN,
                  outline=OUTLINE, width=5)
    elif style == "frown":
        w = kw.get("w", 54)
        d.arc([cx - w / 2, cy - 6, cx + w / 2, cy + w / 3 + 6],
              start=200, end=340, fill=OUTLINE, width=9)
    elif style == "line":
        w = kw.get("w", 42)
        d.line([cx - w / 2, cy, cx + w / 2, cy], fill=OUTLINE, width=8)
    elif style == "wavy":
        w = kw.get("w", 56)
        n = 4
        seg = w / n
        pts_up = [(cx - w / 2 + i * seg, cy + (8 if i % 2 else -4)) for i in range(n + 1)]
        for i in range(n):
            x0, y0 = pts_up[i]
            x1, y1 = pts_up[i + 1]
            d.arc([x0, min(y0, y1) - 6, x1, max(y0, y1) + 6],
                  start=270 if i % 2 == 0 else 90,
                  end=90 if i % 2 == 0 else 270, fill=OUTLINE, width=7)
    elif style == "smirk":
        d.arc([cx - 14, cy - 16, cx + 40, cy + 18], start=40, end=150,
              fill=OUTLINE, width=9)
    elif style == "pucker":
        r = kw.get("r", 11)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=HEART, outline=OUTLINE, width=4)
    elif style == "cat":
        w = kw.get("w", 26)
        d.arc([cx - w, cy - 8, cx, cy + 12], start=20, end=160, fill=OUTLINE, width=7)
        d.arc([cx, cy - 8, cx + w, cy + 12], start=20, end=160, fill=OUTLINE, width=7)


def heart(d, cx, cy, s, fill, outline=None, ow=0):
    """参数化心形。s≈半宽。"""
    pts = []
    for i in range(64):
        t = math.pi * 2 * i / 64
        x = 16 * math.sin(t) ** 3
        y = -(13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t)
              - math.cos(4 * t))
        pts.append((cx + x * s / 17, cy + y * s / 17))
    if outline:
        d.polygon([(px + 1.5, py) for px, py in pts], fill=outline)
        d.polygon([(px - 1.5, py) for px, py in pts], fill=outline)
        d.polygon([(px, py + 1.5) for px, py in pts], fill=outline)
        d.polygon([(px, py - 1.5) for px, py in pts], fill=outline)
    d.polygon(pts, fill=fill)


def blush_marks(img, cx=CX, cy=MOUTH_Y - 26, alpha=115, w=36, h=17):
    ly, ld = layer(img)
    for x in (cx - 76, cx + 76):
        ld.ellipse([x - w / 2, cy - h / 2, x + w / 2, cy + h / 2],
                   fill=(BLUSH[0], BLUSH[1], BLUSH[2], alpha))
    return composite(img, ly)


def sweat_drop(d, img, x, y, s=20):
    ly, ld = layer(img)
    ld.polygon([(x, y - s), (x + s * 0.62, y + s * 0.35), (x, y + s * 0.62),
                (x - s * 0.62, y + s * 0.35)], fill=SWEAT)
    ld.ellipse([x - s * 0.62, y - s * 0.1, x + s * 0.62, y + s * 0.62], fill=SWEAT)
    ld.ellipse([x - s * 0.3, y - s * 0.05, x - s * 0.05, y + s * 0.2], fill=WHITE)
    return composite(img, ly)


def tear_stream(d, img, x, y, t):
    """泪滴沿脸颊下滑，t: 0~1 循环相位。"""
    ly, ld = layer(img)
    drop_y = y + 26 + 34 * t
    ld.polygon([(x, drop_y - 16), (x + 9, drop_y + 4), (x, drop_y + 12),
                (x - 9, drop_y + 4)], fill=TEAR)
    ld.ellipse([x - 9, drop_y - 2, x + 9, drop_y + 10], fill=TEAR)
    return composite(img, ly)


def zzz(img, x, y, t):
    """Zzz 浮动，t: 0~1。"""
    ly, ld = layer(img)
    for i in range(3):
        ph = (t + i * 0.33) % 1.0
        zx = x + i * 26 + ph * 14
        zy = y - i * 24 - ph * 26
        a = int(200 * math.sin(ph * math.pi)) if ph < 0.9 else 0
        if a <= 5:
            continue
        s = 16 + i * 5
        col = (255, 255, 255, a)
        ld.line([(zx - s / 2, zy - s / 2), (zx + s / 2, zy - s / 2)], fill=col, width=6)
        ld.line([(zx + s / 2, zy - s / 2), (zx - s / 2, zy + s / 2)], fill=col, width=6)
        ld.line([(zx - s / 2, zy + s / 2), (zx + s / 2, zy + s / 2)], fill=col, width=6)
    return composite(img, ly)


def thought_dots(img, x, y, t):
    """思考泡泡：3 个圆点从头部冒出，闪烁序列。"""
    ly, ld = layer(img)
    pts = [(x, y), (x + 20, y - 24), (x + 44, y - 50)]
    radii = [7, 10, 15]
    for i, ((px, py), r) in enumerate(zip(pts, radii)):
        ph = (t * 2 + i * 0.4) % 1.0
        a = int(120 + 120 * math.sin(ph * math.pi))
        ld.ellipse([px - r, py - r, px + r, py + r],
                   fill=(255, 255, 255, a), outline=(OUTLINE[0], OUTLINE[1],
                                                     OUTLINE[2], min(a + 60, 255)),
                   width=4)
    return composite(img, ly)


def sparkle(d, x, y, s, alpha=255):
    col = (255, 240, 160, alpha)
    d.line([(x, y - s), (x, y + s)], fill=col, width=6)
    d.line([(x - s, y), (x + s, y)], fill=col, width=6)
    d.line([(x - s * 0.4, y - s * 0.4), (x + s * 0.4, y + s * 0.4)], fill=col, width=4)
    d.line([(x - s * 0.4, y + s * 0.4), (x + s * 0.4, y - s * 0.4)], fill=col, width=4)


def sparkle_overlay(img, pts, t):
    ly, ld = layer(img)
    for (x, y, s) in pts:
        ph = (t + x * 0.013) % 1.0
        a = int(230 * math.sin(ph * math.pi))
        if a > 8:
            sparkle(ld, x, y, s, a)
    return composite(img, ly)


def sound_waves(d, cx, cy, t):
    """聆听声波：头两侧圆弧扩散。"""
    for side in (-1, 1):
        for i in range(3):
            ph = (t * 1.6 + i * 0.33) % 1.0
            r = 26 + i * 24 + ph * 22
            a = int(220 * (1 - ph))
            bbox = [cx + side * 74 - r, cy - r, cx + side * 74 + r, cy + r]
            start = 128 if side < 0 else 32
            end = 232 if side < 0 else 148
            col = (110, 240, 210, a)
            d.arc(bbox, start=start, end=end, fill=col, width=8)


def anger_mark(d, img, x, y, t):
    """生气十字筋，闪烁。"""
    a = int(140 + 110 * abs(math.sin(t * math.pi * 2)))
    ly, ld = layer(img)
    col = (255, 90, 90, a)
    w, ln, off = 8, 14, 10
    for dx in (-1, 1):
        ld.line([(x - ln + off * dx, y - ln), (x + ln + off * dx, y + ln)],
                fill=col, width=w)
        ld.line([(x + ln + off * dx, y - ln), (x - ln + off * dx, y + ln)],
                fill=col, width=w)
    return composite(img, ly)


def qmark(img, x, y, t):
    ly, ld = layer(img)
    a = int(150 + 100 * abs(math.sin(t * math.pi)))
    ld.text((x, y), "?", font=FONT_BOLD, fill=(120, 200, 235, a),
            stroke_width=3, stroke_fill=(47, 58, 70, a))
    return composite(img, ly)


def float_hearts(img, t, base_x=CX, base_y=60, n=3, alpha_scale=1.0):
    ly, ld = layer(img)
    for i in range(n):
        ph = (t + i * 0.34) % 1.0
        hx = base_x + math.sin(ph * math.pi * 2 + i) * 30
        hy = base_y - ph * 36
        a = int(220 * math.sin(ph * math.pi) * alpha_scale)
        if a > 10:
            heart(ld, hx, hy, 16 + 6 * math.sin(ph * math.pi), (HEART[0], HEART[1], HEART[2], a))
    return composite(img, ly)


# ---------- 帧组装 ----------
def frame(oy=0, mood=BODY_LIGHT, pulse=0.0):
    """空画布 + 完整头部（含天线球），返回 (img, d, oy)。"""
    return finish_head(canvas(), oy, mood, pulse)


def finish_head(img, oy=0, mood=BODY_LIGHT, pulse=0.0):
    """在已有画布上画头部（供需要在头下垫底层的表情用）。"""
    img = draw_head(img, oy, mood, pulse)
    d = ImageDraw.Draw(img)
    return img, d, oy


def shrink(img):
    return img.resize((OUT_SIZE, OUT_SIZE), Image.LANCZOS)


def quantize_transparent(img):
    """RGBA → P（255 为透明索引，disposal=2 需要全帧重绘）。"""
    alpha = img.getchannel("A")
    p = img.convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=255)
    mask = alpha.point(lambda a: 255 if a < 200 else 0)
    p.paste(255, mask)
    return p


def save_gif(frames, durations, path):
    pframes = [quantize_transparent(f) for f in frames]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pframes[0].save(path, save_all=True, append_images=pframes[1:],
                    duration=durations, loop=0, transparency=255, disposal=2,
                    optimize=False)
    print(f"  {os.path.basename(path):24s} {len(frames)} 帧 "
          f"{os.path.getsize(path) // 1024}KB")


# ---------- 各表情 ----------
def em_neutral():
    fr, du = [], []
    for oy, eyes, dl in [(0, "open", 1100), (3, "open", 900),
                         (0, "closed", 130), (0, "open", 900)]:
        img, d, oy2 = frame(oy, MOOD["neutral"], 0.2)
        if eyes == "open":
            eye_open(d, EYE_L, EYE_Y + oy2)
            eye_open(d, EYE_R, EYE_Y + oy2)
        else:
            eye_closed(d, EYE_L, EYE_Y + oy2)
            eye_closed(d, EYE_R, EYE_Y + oy2)
        mouth(d, "smile", cy=MOUTH_Y + oy2)
        img = blush_marks(img, cy=MOUTH_Y - 26 + oy2)
        fr.append(shrink(img))
        du.append(dl)
    return fr, du


def em_happy():
    fr, du = [], []
    for oy in (0, 4):
        img, d, oy2 = frame(oy, MOOD["happy"], 0.6)
        eye_arc_up(d, EYE_L, EYE_Y + oy2)
        eye_arc_up(d, EYE_R, EYE_Y + oy2)
        mouth(d, "big_smile", cy=MOUTH_Y + oy2)
        img = blush_marks(img, cy=MOUTH_Y - 26 + oy2)
        fr.append(shrink(img))
        du.append(380)
    return fr, du


def em_laughing():
    fr, du = [], []
    for i, oy in enumerate((0, 5, 0, 5)):
        img, d, oy2 = frame(oy, MOOD["laughing"], 0.8 if oy else 0.3)
        eye_arc_up(d, EYE_L, EYE_Y + oy2)
        eye_arc_up(d, EYE_R, EYE_Y + oy2)
        mouth(d, "big_smile" if oy == 0 else "big_smile", cy=MOUTH_Y + oy2,
              w=46 + 6 * (oy > 0))
        img = blush_marks(img, cy=MOUTH_Y - 26 + oy2, alpha=150)
        ly, ld = layer(img)
        for k, xx in enumerate((66, 250)):
            if (i + k) % 2 == 0:
                ld.text((xx - 12, 96), "ha", font=FONT_SMALL,
                        fill=(255, 255, 255, 190), stroke_width=2,
                        stroke_fill=(47, 58, 70, 190))
        img = composite(img, ly)
        fr.append(shrink(img))
        du.append(210)
    return fr, du


def em_funny():
    fr, du = [], []
    for i in range(4):
        oy = 0 if i % 2 == 0 else 4
        img, d, oy2 = frame(oy, MOOD["funny"], 0.5)
        eye_arc_up(d, EYE_L, EYE_Y + oy2)
        eye_arc_up(d, EYE_R, EYE_Y + oy2)
        mouth(d, "big_smile", cy=MOUTH_Y + oy2)
        img = blush_marks(img, cy=MOUTH_Y - 26 + oy2, alpha=150)
        # 笑泪
        ly, ld = layer(img)
        for xx in (78, 242):
            ph = (i * 0.25 + (0.0 if xx < 160 else 0.5)) % 1.0
            ty = EYE_Y + 20 + ph * 30
            ld.ellipse([xx - 8, ty, xx + 8, ty + 18], fill=TEAR)
        img = composite(img, ly)
        fr.append(shrink(img))
        du.append(260)
    return fr, du


def em_sad():
    fr, du = [], []
    for i, tx in enumerate((-4, 0, 4, 0)):
        img, d, _ = frame(0, MOOD["sad"], 0.1)
        brow(d, EYE_L, EYE_Y - 52, -28)
        brow(d, EYE_R, EYE_Y - 52, 28)
        eye_open(d, EYE_L + tx, EYE_Y, look=(0, 8))
        eye_open(d, EYE_R + tx, EYE_Y, look=(0, 8))
        mouth(d, "frown", cy=MOUTH_Y + 2)
        fr.append(shrink(img))
        du.append(420)
    return fr, du


def em_crying():
    fr, du = [], []
    for i in range(4):
        t = i / 4.0
        oy = 2 if i % 2 else 0
        img, d, oy2 = frame(oy, MOOD["crying"], 0.1)
        brow(d, EYE_L, EYE_Y - 50, -25)
        brow(d, EYE_R, EYE_Y - 50, 25)
        eye_closed(d, EYE_L, EYE_Y + oy2)
        eye_closed(d, EYE_R, EYE_Y + oy2)
        mouth(d, "frown", cy=MOUTH_Y + oy2 + 4)
        img = tear_stream(d, img, EYE_L - 20, EYE_Y, t)
        img = tear_stream(d, img, EYE_R + 20, EYE_Y, (t + 0.5) % 1.0)
        fr.append(shrink(img))
        du.append(240)
    return fr, du


def em_angry():
    fr, du = [], []
    for i in range(3):
        img, d, _ = frame(0, MOOD["angry"], 0.9 if i != 1 else 0.3)
        brow(d, EYE_L, EYE_Y - 52, 30)
        brow(d, EYE_R, EYE_Y - 52, -30)
        eye_open(d, EYE_L, EYE_Y, lid=0.35)
        eye_open(d, EYE_R, EYE_Y, lid=0.35)
        mouth(d, "frown", cy=MOUTH_Y)
        img = anger_mark(d, img, 62 if i % 2 == 0 else 258, 96, i / 3.0)
        fr.append(shrink(img))
        du.append(330)
    return fr, du


def em_surprised():
    fr, du = [], []
    for i, (oy, orad) in enumerate(((6, 12), (0, 16), (2, 14), (0, 16))):
        img, d, oy2 = frame(oy, MOOD["surprised"], 0.8)
        eye_wide(d, EYE_L, EYE_Y + oy2)
        eye_wide(d, EYE_R, EYE_Y + oy2)
        mouth(d, "o", cy=MOUTH_Y + oy2, r=orad)
        fr.append(shrink(img))
        du.append(260)
    return fr, du


def em_shocked():
    fr, du = [], []
    for i, tx in enumerate((-4, 4, -3, 3)):
        img, d, _ = frame(2, MOOD["shocked"], 1.0)
        eye_wide(d, EYE_L + tx, EYE_Y, look=(0, 3))
        eye_wide(d, EYE_R + tx, EYE_Y, look=(0, 3))
        mouth(d, "o", cy=MOUTH_Y, r=20)
        fr.append(shrink(img))
        du.append(160)
    return fr, du


def em_embarrassed():
    fr, du = [], []
    for i in range(2):
        img, d, _ = frame(0, MOOD["embarrassed"], 0.4)
        eye_open(d, EYE_L, EYE_Y, look=(-7, 3))
        eye_open(d, EYE_R, EYE_Y, look=(-7, 3))
        mouth(d, "wavy", cy=MOUTH_Y + 2)
        img = blush_marks(img, cy=MOUTH_Y - 22, alpha=200, w=46, h=22)
        img = sweat_drop(d, img, 250, 100)
        fr.append(shrink(img))
        du.append(520)
    return fr, du


def em_thinking():
    fr, du = [], []
    for i in range(4):
        img, d, _ = frame(0, MOOD["thinking"], 0.5 + 0.3 * math.sin(i))
        eye_open(d, EYE_L, EYE_Y, look=(9, -8))
        eye_open(d, EYE_R, EYE_Y, look=(9, -8))
        mouth(d, "line", cy=MOUTH_Y + 4, w=26)
        img = thought_dots(img, 236, 92, i / 4.0)
        fr.append(shrink(img))
        du.append(420)
    return fr, du


def em_winking():
    fr, du = [], []
    for i in range(2):
        img, d, _ = frame(0, MOOD["winking"], 0.6)
        eye_open(d, EYE_L, EYE_Y)
        eye_arc_up(d, EYE_R, EYE_Y, w=20)
        mouth(d, "smirk", cy=MOUTH_Y)
        img = blush_marks(img, cy=MOUTH_Y - 26, alpha=130)
        img = sparkle_overlay(img, [(258, 88, 14), (70, 120, 9)], i / 2.0)
        fr.append(shrink(img))
        du.append(600)
    return fr, du


def em_cool():
    fr, du = [], []
    for i in range(3):
        img, d, _ = frame(0, MOOD["cool"], 0.5)
        # 墨镜
        d.rounded_rectangle([64, EYE_Y - 22, 256, EYE_Y + 22], radius=20,
                            fill=SUNGLASS, outline=OUTLINE, width=5)
        d.line([(86, EYE_Y - 12), (126, EYE_Y - 12)], fill=(150, 220, 250, 200), width=6)
        d.polygon([(64, EYE_Y - 6), (64, EYE_Y + 22), (160, EYE_Y + 22)],
                  fill=SUNGLASS)
        mouth(d, "smirk", cy=MOUTH_Y + 2)
        img = sparkle_overlay(img, [(258, 92, 13)], i / 3.0)
        fr.append(shrink(img))
        du.append(420)
    return fr, du


def em_relaxed():
    fr, du = [], []
    for oy, dl in ((0, 800), (3, 800), (0, 800), (2, 800)):
        img, d, oy2 = frame(oy, MOOD["relaxed"], 0.2)
        eye_closed(d, EYE_L, EYE_Y + oy2)
        eye_closed(d, EYE_R, EYE_Y + oy2)
        mouth(d, "smile", cy=MOUTH_Y + oy2, w=48)
        img = blush_marks(img, cy=MOUTH_Y - 26 + oy2, alpha=120)
        fr.append(shrink(img))
        du.append(dl)
    return fr, du


def em_delicious():
    fr, du = [], []
    for i in range(3):
        oy = 0 if i % 2 == 0 else 3
        img, d, oy2 = frame(oy, MOOD["delicious"], 0.7)
        eye_arc_up(d, EYE_L, EYE_Y + oy2)
        eye_arc_up(d, EYE_R, EYE_Y + oy2)
        # 咧嘴 + 舔舌
        d.chord([CX - 40, MOUTH_Y - 8 + oy2, CX + 40, MOUTH_Y + 30 + oy2],
                start=0, end=180, fill=MOUTH_IN, outline=OUTLINE, width=5)
        d.ellipse([CX - 26, MOUTH_Y + 8 + oy2, CX + 10, MOUTH_Y + 36 + oy2],
                  fill=TONGUE, outline=OUTLINE, width=3)
        img = blush_marks(img, cy=MOUTH_Y - 26 + oy2, alpha=140)
        img = sparkle_overlay(img, [(68, 104, 12), (252, 108, 12)], i / 3.0)
        fr.append(shrink(img))
        du.append(340)
    return fr, du


def em_kissy():
    fr, du = [], []
    for i in range(3):
        img, d, _ = frame(0, MOOD["kissy"], 0.7)
        eye_arc_up(d, EYE_L, EYE_Y, w=22)
        eye_arc_up(d, EYE_R, EYE_Y, w=22)
        mouth(d, "pucker", cy=MOUTH_Y + 2, r=13)
        img = blush_marks(img, cy=MOUTH_Y - 26, alpha=150)
        img = float_hearts(img, i / 3.0, base_x=CX + 66, base_y=104, n=2)
        fr.append(shrink(img))
        du.append(380)
    return fr, du


def em_confident():
    fr, du = [], []
    for i in range(2):
        img, d, _ = frame(0, MOOD["confident"], 0.6)
        eye_open(d, EYE_L, EYE_Y, look=(5, 0))
        eye_open(d, EYE_R, EYE_Y, look=(5, 0))
        # 挑眉
        brow(d, EYE_L, EYE_Y - 56, 6)
        d.line([EYE_R - 16, EYE_Y - 66, EYE_R + 16, EYE_Y - 74], fill=OUTLINE, width=9)
        mouth(d, "smirk", cy=MOUTH_Y)
        img = sparkle_overlay(img, [(62, 100, 13), (256, 130, 10)], i / 2.0)
        fr.append(shrink(img))
        du.append(520)
    return fr, du


def em_sleepy():
    fr, du = [], []
    for i in range(4):
        img, d, _ = frame(1 if i % 2 else 0, MOOD["sleepy"], 0.1)
        eye_closed(d, EYE_L, EYE_Y)
        eye_closed(d, EYE_R, EYE_Y)
        mouth(d, "o", cy=MOUTH_Y + 6, r=8)
        img = zzz(img, 238, 96, i / 4.0)
        fr.append(shrink(img))
        du.append(620)
    return fr, du


def em_silly():
    fr, du = [], []
    for i in range(4):
        oy = 0 if i % 2 == 0 else 4
        img, d, oy2 = frame(oy, MOOD["silly"], 0.5)
        eye_open(d, EYE_L, EYE_Y + oy2, look=(-4, 2), scale=1.1)
        eye_open(d, EYE_R, EYE_Y + oy2, look=(6, -3), scale=0.8)
        # 歪舌
        d.chord([CX - 34, MOUTH_Y - 6 + oy2, CX + 34, MOUTH_Y + 26 + oy2],
                start=0, end=180, fill=MOUTH_IN, outline=OUTLINE, width=5)
        d.ellipse([CX - 2, MOUTH_Y + 6 + oy2, CX + 34, MOUTH_Y + 38 + oy2],
                  fill=TONGUE, outline=OUTLINE, width=3)
        fr.append(shrink(img))
        du.append(300)
    return fr, du


def em_confused():
    fr, du = [], []
    for i in range(3):
        img, d, _ = frame(0, MOOD["confused"], 0.3)
        eye_open(d, EYE_L, EYE_Y, scale=1.05, look=(-3, 0))
        eye_open(d, EYE_R, EYE_Y, scale=0.75, look=(3, 4))
        mouth(d, "wavy", cy=MOUTH_Y + 2, w=44)
        img = qmark(img, 250, 78, i / 3.0)
        fr.append(shrink(img))
        du.append(440)
    return fr, du


def em_loving():
    fr, du = [], []
    for i in range(3):
        img, d, oy = frame(0 if i % 2 == 0 else -2, MOOD["loving"], 0.8)
        eye_heart(d, EYE_L, EYE_Y)
        eye_heart(d, EYE_R, EYE_Y)
        mouth(d, "big_smile", cy=MOUTH_Y, w=44)
        img = blush_marks(img, cy=MOUTH_Y - 26, alpha=160)
        img = float_hearts(img, i / 3.0, base_x=CX, base_y=72, n=3)
        fr.append(shrink(img))
        du.append(400)
    return fr, du


def em_listening():
    """聆听：眼睛微睁有神，头两侧声波扩散。"""
    fr, du = [], []
    for i in range(4):
        t = i / 4.0
        oy = 2 if i % 2 == 0 else 0
        img = canvas()
        # 声波在最底层（头部后面）
        ld = ImageDraw.Draw(img)
        sound_waves(ld, CX, EYE_Y, t)
        img, d, oy = finish_head(img, oy, MOOD["listening"],
                                 0.5 + 0.4 * math.sin(t * 6.28))
        eye_open(d, EYE_L, EYE_Y + oy, look=(0, -2), scale=1.05)
        eye_open(d, EYE_R, EYE_Y + oy, look=(0, -2), scale=1.05)
        mouth(d, "line", cy=MOUTH_Y + oy, w=30)
        fr.append(shrink(img))
        du.append(170)
    return fr, du


def em_speaking():
    """说话：嘴巴一张一合，眼睛正常。"""
    fr, du = [], []
    heights = (8, 22, 12, 26, 9, 20)
    for h in heights:
        img, d, _ = frame(0, MOOD["speaking"], 0.3 + 0.25 * (h / 26.0))
        eye_open(d, EYE_L, EYE_Y)
        eye_open(d, EYE_R, EYE_Y)
        mouth(d, "talk", cy=MOUTH_Y + 2, h=h, w=48)
        fr.append(shrink(img))
        du.append(115)
    return fr, du


def em_cancel():
    """错误/取消：X 眼 + 平嘴，轻微晃动。"""
    fr, du = [], []
    for i, tx in enumerate((-2, 2)):
        img, d, _ = frame(0, MOOD["cancel"], 0.0)
        eye_x(d, EYE_L + tx, EYE_Y, w=16)
        eye_x(d, EYE_R + tx, EYE_Y, w=16)
        mouth(d, "line", cy=MOUTH_Y + 4, w=44)
        fr.append(shrink(img))
        du.append(420)
    return fr, du


# ---------- 主流程 ----------
EMOTIONS = {
    "neutral": em_neutral, "happy": em_happy, "laughing": em_laughing,
    "funny": em_funny, "sad": em_sad, "crying": em_crying, "angry": em_angry,
    "surprised": em_surprised, "shocked": em_shocked,
    "embarrassed": em_embarrassed, "thinking": em_thinking,
    "winking": em_winking, "cool": em_cool, "relaxed": em_relaxed,
    "delicious": em_delicious, "kissy": em_kissy, "confident": em_confident,
    "sleepy": em_sleepy, "silly": em_silly, "confused": em_confused,
    "loving": em_loving, "listening": em_listening, "speaking": em_speaking,
    "cancel": em_cancel,
}


def build_preview(gif_dir, out_path):
    """把所有 GIF 以 base64 内嵌到单文件 HTML 预览页。"""
    import base64
    items = []
    for name in EMOTIONS:
        p = os.path.join(gif_dir, f"{name}.gif")
        if os.path.exists(p):
            with open(p, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            items.append(
                f'<div class="card"><img src="data:image/gif;base64,{b64}">'
                f'<p>{name}</p></div>')
    template = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>小D 动态表情预览</title>
<style>
body{background:#101820;color:#dfe9ee;font-family:system-ui,sans-serif;
     display:flex;flex-wrap:wrap;gap:14px;padding:24px;justify-content:center}
.card{background:#1b2733;border-radius:14px;padding:12px;text-align:center;
      box-shadow:0 2px 8px rgba(0,0,0,.4)}
.card img{width:140px;height:140px;image-rendering:auto;
      background:radial-gradient(circle at 50pc 60pc,#24333f,#16212b)}
.card p{margin:8px 0 0;font-size:13px;color:#9fc6d8}
h1{width:100pc;text-align:center;font-size:20px}
</style></head><body>
<h1>小D 卡通动态表情 · __N__ 个 · 160×160 透明背景 GIF</h1>
__ITEMS__</body></html>"""
    html = template.replace("50pc", "50%").replace("60pc", "60%").replace(
        "100pc", "100%")
    html = template.replace("__N__", str(len(items))).replace("__ITEMS__",
                                                               "\n".join(items))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"preview -> {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "firmware", "xiaozhi-esp32", "main", "assets", "emoji", "xiaod-gif"))
    ap.add_argument("--preview", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    total = 0
    for name, fn in EMOTIONS.items():
        frames, durations = fn()
        path = os.path.join(args.out, f"{name}.gif")
        save_gif(frames, durations, path)
        total += os.path.getsize(path)
    print(f"total {len(EMOTIONS)} gifs, {total // 1024}KB -> {args.out}")

    if args.preview:
        build_preview(args.out, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "outputs", "xiaod_emoji_preview.html"))


if __name__ == "__main__":
    main()
