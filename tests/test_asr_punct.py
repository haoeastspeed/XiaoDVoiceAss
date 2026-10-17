# -*- coding: utf-8 -*-
"""ASR 标点恢复的纯函数回归测试（不加载模型）。

覆盖三条路径：
1. `_punct_by_tokens` —— token 时间戳精确对齐（生产主路径）
2. `add_punctuation`   —— 无时间戳时的比例折算兜底
3. 边界守卫           —— 英文单词内部、已有标点、过短文本、过密标点
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway.providers.asr_sensevoice import (  # noqa: E402
    _energy_spans, _local_mark, _mid_comma, _punct_by_tokens,
    _snap_boundary, add_punctuation,
)

SR = 16000
FAILED = []


def check(name, got, want):
    if got != want:
        FAILED.append(f"{name}\n  got : {got!r}\n  want: {want!r}")
        print(f"FAIL {name}\n  got : {got!r}\n  want: {want!r}")
    else:
        print(f"ok   {name} -> {got!r}")


# ---------- 1. token 时间戳路径（主路径） ----------
# 模拟："开饭时间" 0.0~0.9s，"早上九点至下午五点" 1.6~2.4s（700ms 停顿）
def tok_path():
    text = "开饭时间早上九点至下午五点"
    tokens = list(text)
    stamps = []
    for ch in tokens:
        stamps.append(stamps[-1] + 0.1 if stamps else 0.0)
    # 前 4 字 0.0-0.4s；后 9 字 1.1-2.0s（停顿 0.7s）
    stamps = [0.0, 0.1, 0.2, 0.3, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9]
    spans = [(0, int(0.4 * SR)), (int(1.1 * SR), int(1.9 * SR))]
    check("tok_700ms_句号", _punct_by_tokens(text, tokens, stamps, spans, SR),
          "开饭时间。早上九点至下午五点")

    # 300ms 停顿 → 逗号
    stamps2 = [0.0, 0.1, 0.2, 0.3, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4, 1.5]
    spans2 = [(0, int(0.4 * SR)), (int(0.7 * SR), int(1.5 * SR))]
    check("tok_300ms_逗号", _punct_by_tokens(text, tokens, stamps2, spans2, SR),
          "开饭时间，早上九点至下午五点")

    # 单段无停顿 → 不乱插
    spans3 = [(0, int(1.2 * SR))]
    check("tok_无停顿", _punct_by_tokens(text, tokens, stamps, spans3, SR), text)


# ---------- 2. 比例折算兜底 ----------
def fallback_path():
    spans = [(0, 16000), (20000, 48000)]
    check("fb_英文逗号",
          add_punctuation("HELLO WORLD AND GOODBYE NOW FRIEND", spans,
                          48000, SR),
          "HELLO WORLD, AND GOODBYE NOW FRIEND")
    # 英文句号落在词边界（不切词）
    out = add_punctuation("HELLO WORLD AND GOODBYE NOW FRIEND",
                          [(0, 16000), (26000, 48000)], 48000, SR)
    check("fb_英文句号不切词", "G，O" not in out and "G.O" not in out, True)
    # 无停顿信息的长句 → 中点补逗号且贴词边界
    check("fb_中点兜底",
          add_punctuation("HELLO WORLD AND GOODBYE NOW FRIEND", [], 0, SR),
          "HELLO WORLD AND, GOODBYE NOW FRIEND")
    # 太短不加标点
    check("fb_太短", add_punctuation("好的", [], 0, SR), "好的")
    check("fb_太短_spans", add_punctuation("好的", spans, 48000, SR), "好的")
    # 只有一段 → 不插
    check("fb_单段",
          add_punctuation("开饭时间早上九点至下午五点再见", [(0, 48000)],
                          48000, SR),
          "开饭时间早上九点至下午五点再见")


# ---------- 3. 上下文标点样式 ----------
def mark_style():
    check("mark_英文逗号", _local_mark("，", "THE BOY AND", 7), (7, ","))
    check("mark_英文句号", _local_mark("。", "HELLO WORLD", 5), (5, "."))
    check("mark_英文吃空格", _local_mark("，", "WORLD AND", 6), (5, ","))
    check("mark_中文全角", _local_mark("，", "你好世界", 2), (2, "，"))
    check("snap_贴空格", _snap_boundary("AND GOODBYE", 3), 3)
    check("snap_贴左空格", _snap_boundary("AND GOODBYE", 4), 4)
    check("snap_中文不动", _snap_boundary("你好世界", 2), 2)
    check("mid_英文", _mid_comma("HELLO WORLD AND GOODBYE NOW FRIEND"),
          "HELLO WORLD AND, GOODBYE NOW FRIEND")
    check("mid_中文短句不动", _mid_comma("你好世界"), "你好世界")


# ---------- 4. 能量自查 ----------
def energy():
    import struct
    # 1s 静音 + 1s 方波 + 0.6s 静音 + 1s 方波
    pcm = b""
    pcm += b"\x00\x00" * SR
    pcm += struct.pack("<h", 8000) * SR
    pcm += b"\x00\x00" * int(SR * 0.6)
    pcm += struct.pack("<h", 8000) * SR
    spans = _energy_spans(pcm, SR)
    check("energy_两段", len(spans), 2)
    gap_ms = (spans[1][0] - spans[0][1]) * 1000.0 / SR
    check("energy_间隔", 550 <= gap_ms <= 650, True)
    # 碎段合并：20ms 窗 + 100ms 间隙的两段方波应合成一段
    pcm2 = struct.pack("<h", 8000) * (SR // 2)
    pcm2 += b"\x00\x00" * int(SR * 0.1)
    pcm2 += struct.pack("<h", 8000) * (SR // 2)
    check("energy_合并碎段", len(_energy_spans(pcm2, SR)), 1)


tok_path()
fallback_path()
mark_style()
energy()

print()
if FAILED:
    print(f"===== {len(FAILED)} 项失败 =====")
    sys.exit(1)
print("===== 全部通过 =====")
