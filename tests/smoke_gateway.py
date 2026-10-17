# -*- coding: utf-8 -*-
"""网关端到端冒烟：真连网关 → 说一句话 → 必须拿到 stt + tts 音频。

刻意只跑在 --config 指定的临时目录上（默认 8137 端口），
绝不连用户真实运行目录，避免污染 .tmp/gw_overrides.json。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("PATH", os.path.join(ROOT, "reference", "bin")
                      + os.pathsep + os.environ.get("PATH", ""))
sys.path.insert(0, os.path.join(ROOT, "app"))

import numpy as np  # noqa: E402
import opuslib_next  # noqa: E402
import websockets  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402

URL = "ws://127.0.0.1:8137/xiaozhi/v1/"
SR = 16000
FRAME = 960          # 60ms @16k


def tts_wav(text: str) -> np.ndarray:
    from gateway.providers.tts_sapi import _synth_wav
    pcm, src = _synth_wav("Microsoft Huihui", text, {})
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    n = int(len(x) * SR / src)
    return np.interp(np.arange(n) * src / SR, np.arange(len(x)), x)


async def main():
    enc = opuslib_next.Encoder(SR, 1, opuslib_next.APPLICATION_AUDIO)
    dec = opuslib_next.Decoder(24000, 1)
    hello = {
        "type": "hello", "version": 1, "transport": "websocket",
        "audio_params": {"format": "opus", "sample_rate": SR,
                         "channels": 1, "frame_duration": 60},
    }
    async with connect(URL, additional_headers={"device-id": "smoke-001"}) as ws:
        await ws.send(json.dumps(hello))
        await asyncio.sleep(0.4)
        await ws.send(json.dumps({"type": "listen", "state": "start",
                                  "mode": "auto"}))

        speech = tts_wav("今天天气不错，我们出去玩吧")
        speech = np.clip(speech * 3.0, -32768, 32767).astype("<i2")

        got_stt = None
        tts_pkts = 0
        tts_stop = False
        notices = []

        async def reader():
            nonlocal got_stt, tts_pkts, tts_stop
            async for raw in ws:
                if isinstance(raw, bytes):
                    tts_pkts += 1
                    try:
                        dec.decode(raw, 960 * 3)
                    except Exception:
                        pass
                    continue
                m = json.loads(raw)
                t = m.get("type")
                if t == "stt":
                    got_stt = m.get("text", "")
                    print(f"[stt] {got_stt}", flush=True)
                elif t == "notice":
                    notices.append(m.get("text", ""))
                    print(f"[notice] {m.get('text')}", flush=True)
                elif t == "tts":
                    if m.get("state") == "stop":
                        tts_stop = True
                elif t == "error":
                    print(f"[error] {m}", flush=True)

        task = asyncio.create_task(reader())
        # 先来 0.4s 静音（前置静音裁剪路径），再放语音
        await ws.send(enc.encode(np.zeros(FRAME * 4, dtype="<i2").tobytes(),
                                 FRAME))
        for i in range(0, len(speech) - FRAME, FRAME):
            await ws.send(enc.encode(speech[i:i + FRAME].tobytes(), FRAME))
            await asyncio.sleep(0.055)
        await ws.send(json.dumps({"type": "listen", "state": "stop"}))

        deadline = time.monotonic() + 75
        while time.monotonic() < deadline and not tts_stop:
            await asyncio.sleep(0.3)
        task.cancel()

        print(f"\n--- 结果 ---")
        print(f"识别文本: {got_stt!r}")
        print(f"下行音频包: {tts_pkts}  tts_stop: {tts_stop}")
        print(f"提示: {notices}")
        ok = bool(got_stt) and tts_pkts > 0
        print("SMOKE_" + ("PASS" if ok else "FAIL"))
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
