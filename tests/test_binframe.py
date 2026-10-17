# -*- coding: utf-8 -*-
"""二进制帧编解码回归（协议 v1 / v2 / v3）。

对 v2 / v3 的头布局做逐字节比对（对齐官方 docs/websocket_zh.md §3 的 C 结构），
并验证畸形/截断帧不会抛异常。

跑法：python tests/test_binframe.py
"""
from __future__ import annotations

import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from gateway.binframe.codec import FrameCodec, clamp_version   # noqa: E402

PASS = 0
FAIL = 0


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def test_version_clamp():
    print("\n== 版本协商 ==")
    ok(clamp_version(1) == 1, "1 → 1")
    ok(clamp_version(2) == 2, "2 → 2")
    ok(clamp_version(3) == 3, "3 → 3")
    ok(clamp_version(0) == 1, "非法 0 → 回退 1")
    ok(clamp_version(9) == 1, "非法 9 → 回退 1")
    ok(clamp_version("2") == 2, "字符串 '2' → 2")
    ok(clamp_version(None) == 1, "None → 1")
    ok(clamp_version("abc") == 1, "垃圾字符串 → 1")


def test_v1():
    print("\n== v1（裸 opus）==")
    c = FrameCodec(1)
    payload = b"\x01\x02\x03\x04"
    ok(c.decode(payload) == ("opus", payload, None), "decode 原样返回")
    ok(c.encode_audio(payload, 123) == payload, "encode_audio 不加头")
    ok(c.encode_json({"a": 1}) is None, "v1 的 JSON 走 text 帧（None）")


def test_v2_bytes():
    print("\n== v2 头布局逐字节 ==")
    c = FrameCodec(2)
    payload = b"AB"
    ts = 123456
    frame = c.encode_audio(payload, ts)
    # 期望：>HHIII = version(2), type(0), reserved(0), timestamp, payload_size
    head = struct.pack(">HHIII", 2, 0, 0, ts, len(payload))
    ok(frame == head + payload, "上行/下行头一致", f"got={frame!r}")
    ok(len(frame) == 16 + len(payload), "头长 16 字节")
    kind, got, got_ts = c.decode(frame)
    ok(kind == "opus" and got == payload and got_ts == ts,
       "decode round-trip（含 timestamp）", f"{kind},{got!r},{got_ts}")

    # JSON 类型帧
    jf = c.encode_json({"type": "hello"})
    ok(jf is not None and jf[2:4] == struct.pack(">H", 1),
       "JSON 帧 type=1")
    kind2, got2, ts2 = c.decode(jf)
    ok(kind2 == "json" and b"hello" in got2, "JSON 帧可解码")


def test_v3_bytes():
    print("\n== v3 头布局逐字节 ==")
    c = FrameCodec(3)
    payload = b"XYZ"
    frame = c.encode_audio(payload)
    head = struct.pack(">BBH", 0, 0, len(payload))
    ok(frame == head + payload, "4 字节头 + payload", f"got={frame!r}")
    ok(len(frame) == 4 + len(payload), "头长 4 字节")
    kind, got, ts = c.decode(frame)
    ok(kind == "opus" and got == payload and ts is None,
       "decode round-trip（无 timestamp）")


def test_malformed():
    print("\n== 畸形/截断容错 ==")
    for v in (1, 2, 3):
        c = FrameCodec(v)
        try:
            c.decode(b"")
            c.decode(b"\x00")
            c.decode(b"\x00\x01\x02")
            c.decode(os.urandom(7))
            ok(True, f"v{v} 畸形帧不抛异常")
        except Exception as e:  # noqa: BLE001
            ok(False, f"v{v} 畸形帧不抛异常", f"{type(e).__name__}: {e}")

    # v2 声明 payload_size 大于实际长度 → 只取实际可用的部分
    c = FrameCodec(2)
    fake = struct.pack(">HHIII", 2, 0, 0, 5, 999) + b"ab"
    kind, got, _ = c.decode(fake)
    ok(got == b"ab", "payload_size 越界时按实际长度截断", f"got={got!r}")

    # v2 头里 version 字段与 codec 版本不一致：仍按 codec 版本解析（对端应一致）
    c = FrameCodec(2)
    fake2 = struct.pack(">HHIII", 9, 0, 0, 0, 1) + b"z"
    kind, got, _ = c.decode(fake2)
    ok(got == b"z", "version 字段异常不影响解析")


if __name__ == "__main__":
    test_version_clamp()
    test_v1()
    test_v2_bytes()
    test_v3_bytes()
    test_malformed()
    print(f"\nBINFRAME_PASS {PASS}/{PASS + FAIL}")
    sys.exit(0 if FAIL == 0 else 1)
