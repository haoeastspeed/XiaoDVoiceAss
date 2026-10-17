# -*- coding: utf-8 -*-
"""P2 能力 provider 回归：声纹 / 视觉 / Realtime 格式 / 摄像头 / 唤醒词配置。

不连真机、不加载模型、不发网络请求（用假件与注入）。

跑法：python tests/test_p2_providers.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from gateway.config import GatewayConfig                          # noqa: E402
from gateway.providers import voiceprint as vp                    # noqa: E402
from gateway.providers.llm_realtime import (_to_pcm16,            # noqa: E402
                                            from_config as rt_from,
                                            resample_s16)

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


# ---------------------------------------------------------------------------
# 声纹
# ---------------------------------------------------------------------------

def test_wav():
    print("\n== PCM → WAV ==")
    wav = vp.pcm_to_wav(b"\x00\x01" * 160, 16000)
    ok(wav[:4] == b"RIFF" and wav[8:12] == b"WAVE", "RIFF/WAVE 头正确")
    ok(wav[22:24] == b"\x01\x00", "单声道")
    ok(int.from_bytes(wav[24:28], "little") == 16000, "采样率 16000")
    ok(int.from_bytes(wav[28:32], "little") == 32000, "字节率 32000")


def test_local_vp():
    print("\n== 本地声纹（注入假嵌入）==")

    class FakeLocal(vp.LocalVoiceprint):
        def __init__(self):
            self.threshold = 0.4
            self._enrolled = {}
            self.available = True
            self._vec = {"alice": [1.0, 0.0], "bob": [0.0, 1.0]}

        def _embed(self, pcm, sr):
            return self._vec.get(pcm.decode("ascii"), [1.0, 0.0])

    v = FakeLocal()
    v.enroll("alice", b"alice")
    v.enroll("bob", b"bob")
    ok(v.identify(b"alice") == "alice", "认出 alice")
    ok(v.identify(b"bob") == "bob", "认出 bob")
    v._vec["carol"] = [0.7, 0.7]        # 与两者余弦都 ~0.707
    ok(v.identify(b"carol") in ("alice", "bob"), "相似时给最近的一位")
    v._vec["dave"] = [-1.0, -1.0]       # 与两者余弦都为负
    ok(v.identify(b"dave") == "", "都不像时认不出（返回空）")


def test_vp_config():
    print("\n== 声纹配置开关 ==")
    cfg = GatewayConfig()
    ok(vp.from_config(cfg) is None, "默认关闭 → None")
    cfg.voiceprint_enabled = True
    cfg.voiceprint_mode = "remote"
    cfg.voiceprint_base_url = ""
    ok(vp.from_config(cfg) is None, "remote 缺地址 → None")
    cfg.voiceprint_base_url = "http://127.0.0.1:9000"
    got = vp.from_config(cfg)
    ok(got is not None and got.available, "remote 配好 → 可用")
    ok(vp.speaker_label("小明") == "当前和你说话的人叫「小明」。",
       "说话人提示文案")


# ---------------------------------------------------------------------------
# 视觉
# ---------------------------------------------------------------------------

def _multipart(fields, boundary="BND"):
    parts = []
    for name, (fn, ct, data) in fields.items():
        head = f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
        if fn:
            head += f'; filename="{fn}"'
        head += f"\r\nContent-Type: {ct}\r\n\r\n"
        parts.append(head.encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def test_vision():
    print("\n== 视觉：multipart 解析 ==")
    from gateway.http.vision import parse_multipart, VisionHandler
    body, ct = _multipart({
        "question": ("", "text/plain", "这是什么？".encode()),
        "image": ("a.jpg", "image/jpeg", b"\xff\xd8\xff\xe0JPEG"),
    })
    parts = parse_multipart(body, ct)
    ok(set(parts) == {"question", "image"}, "解析出两个字段", list(parts))
    ok(parts["question"][2].decode() == "这是什么？", "question 内容正确")
    ok(parts["image"][2] == b"\xff\xd8\xff\xe0JPEG", "image 二进制完整")
    ok(parts["image"][1] == "image/jpeg", "保留 part 的 content-type")

    # 拦截视觉模型
    class FakeVLM:
        ready = True

        async def describe(self, data, question, fmt="jpeg"):
            return f"看到 {len(data)} 字节，问题：{question}"

        async def aclose(self):
            pass

    import gateway.providers.llm_vision as lv
    orig = lv.from_config
    lv.from_config = lambda cfg: FakeVLM()
    try:
        cfg = GatewayConfig()
        h = VisionHandler(cfg, logger=lambda m: None)
        st, hd, payload = h.handle_explain(body, {"content-type": ct}, None)
        js = json.loads(payload)
        ok(st == 200 and js.get("success") is True and "看到 8 字节" in js["text"],
           "视觉端点返回模型结果", js)

        # 非 multipart
        st2, _h2, p2 = h.handle_explain(b"", {"content-type": "application/json"},
                                        None)
        ok(st2 == 400, "非 multipart → 400")

        # 缺图
        b3, c3 = _multipart({"question": ("", "text/plain", b"hi")})
        st3, _h3, p3 = h.handle_explain(b3, {"content-type": c3}, None)
        ok(st3 == 400, "缺 image → 400")
    finally:
        lv.from_config = orig


# ---------------------------------------------------------------------------
# Realtime
# ---------------------------------------------------------------------------

def test_realtime():
    print("\n== Realtime：格式与配置 ==")
    import numpy as np
    x = np.arange(1600, dtype=np.int16)
    y = resample_s16(x, 16000, 24000)
    ok(abs(y.size - 2400) <= 1, "16k→24k 样本数按比例", y.size)
    y2 = resample_s16(x, 16000, 16000)
    ok(y2.size == x.size, "同采样率原样返回")
    b = _to_pcm16(np.zeros(1600, dtype=np.int16).tobytes(), 16000)
    ok(len(b) == 2400 * 2, "PCM 字节数 = 24k 样本 × 2", len(b))

    cfg = GatewayConfig()
    ok(rt_from(cfg) is None, "默认关闭 → None")
    cfg.realtime_enabled = True
    cfg.realtime_base_url = "https://api.openai.com"
    rt = rt_from(cfg)
    ok(rt is not None and rt.url.startswith("wss://"), "https → wss 自动转换",
       rt.url if rt else None)
    ok("model=" in rt.url, "url 带 model 参数")


# ---------------------------------------------------------------------------
# 摄像头 / 唤醒词
# ---------------------------------------------------------------------------

def test_camera_wake():
    print("\n== 摄像头 / 唤醒词 ==")
    from gateway.providers.camera import Camera
    ok(isinstance(Camera.is_available(), bool), "Camera.is_available 返回布尔")
    c = Camera(index=0, max_bytes=5 * 1024 * 1024)
    ok(c.max_bytes == 5 * 1024 * 1024, "Camera 配置生效")

    from app.wakeword import WakeWord, CHUNK
    ok(CHUNK == 1280, "openWakeWord 分块 80ms/16k = 1280")
    w = WakeWord()
    ok(isinstance(w.available, bool), "WakeWord.available 为布尔（缺失时降级）")
    if not w.available:
        ok(w.feed(b"\x00" * 5120) is False, "引擎不可用时 feed 安全返回 False")
    ok(w.last_score == 0.0, "初始分数为 0")


if __name__ == "__main__":
    test_wav()
    test_local_vp()
    test_vp_config()
    test_vision()
    test_realtime()
    test_camera_wake()
    print(f"\nP2_PASS {PASS}/{PASS + FAIL}")
    sys.exit(0 if FAIL == 0 else 1)
