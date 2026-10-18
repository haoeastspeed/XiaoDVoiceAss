# -*- coding: utf-8 -*-
"""回归：开口前长时间静音不能被判成噪声丢弃。

用户场景：打开对讲窗口 → 沉默 10s → 说一句话。旧实现按"语音帧占比"判据，
占比 = 语音时长 /（从 listen start 起算的总时长）= 2/12 ≈ 0.17 < 0.35，
整句被丢弃 → "显示正在聆听，说话没反应"。本用例把前置静音拉到 10s 复现。

同时验证：重复的 listen start 兜底重发被服务端幂等忽略（不再把 VAD 的
静音计时清零，句子能正常结束）。
"""
import asyncio
import json
import os
import subprocess
import tempfile

ROOT = "D:\\projects\\xiaod-voice"
BIN = os.path.join(ROOT, "reference", "bin")
SRC_WAV = ("D:/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09/"
           "test_wavs/zh.wav")
WS_URL = "ws://127.0.0.1:8000/xiaozhi/v1/"
SAMPLE_RATE = 16000
FRAME_MS = 60
FRAME_SIZE = int(SAMPLE_RATE * FRAME_MS / 1000)
PRE_SILENCE = 10.0     # 开口前静默 10 秒（复现占比被稀释）
POST_SILENCE = 1.2

os.environ["PATH"] = BIN + os.pathsep + os.environ.get("PATH", "")

import opuslib_next  # noqa: E402
import websockets  # noqa: E402


def load_packets():
    tmp = tempfile.mkdtemp(prefix="wait_")
    pcm_path = os.path.join(tmp, "p.pcm")
    subprocess.run(
        [os.path.join(BIN, "ffmpeg.exe"), "-y", "-i", SRC_WAV,
         "-ar", str(SAMPLE_RATE), "-ac", "1", "-f", "s16le", pcm_path],
        check=True, capture_output=True,
    )
    with open(pcm_path, "rb") as f:
        speech = f.read()
    pre = b"\x00" * int(SAMPLE_RATE * 2 * PRE_SILENCE)
    post = b"\x00" * int(SAMPLE_RATE * 2 * POST_SILENCE)
    pcm = pre + speech + post
    enc = opuslib_next.Encoder(SAMPLE_RATE, 1, opuslib_next.APPLICATION_VOIP)
    packets = []
    for i in range(0, len(pcm) - FRAME_SIZE * 2 + 1, FRAME_SIZE * 2):
        packets.append(enc.encode(pcm[i:i + FRAME_SIZE * 2], FRAME_SIZE))
    return packets


async def main():
    packets = load_packets()
    ws = await websockets.connect(
        WS_URL, extra_headers={"device-id": "wait-test"},
        max_size=None, ping_interval=None)
    stt = ""
    got_tts_start = False

    async def recv():
        nonlocal stt, got_tts_start
        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)):
                continue
            m = json.loads(raw)
            t = m.get("type")
            if t == "stt":
                stt = m.get("text", "")
                print(f"[stt] {stt}")
            elif t == "tts" and m.get("state") == "start":
                got_tts_start = True
                print("[tts] start")

    task = asyncio.create_task(recv())
    await ws.send(json.dumps({
        "type": "hello", "device_id": "wait-test", "device_name": "wait",
        "device_mac": "00:00:00:00:00:00", "token": "",
        "features": {"mcp": False},
        "audio_params": {"format": "opus", "sample_rate": 24000,
                         "channels": 1, "frame_duration": FRAME_MS}}))
    await asyncio.sleep(0.5)
    await ws.send(json.dumps({"type": "listen", "state": "start",
                              "mode": "auto"}))
    # 模拟客户端的兜底重发（tts_stop 后会重发）：每 2s 一条
    async def spam():
        for _ in range(6):
            await asyncio.sleep(2.0)
            try:
                await ws.send(json.dumps({"type": "listen", "state": "start",
                                          "mode": "auto"}))
            except Exception:
                return
    spam_task = asyncio.create_task(spam())

    for p in packets:
        await ws.send(p)
        await asyncio.sleep(FRAME_MS / 1000.0)
    await asyncio.sleep(1.0)
    spam_task.cancel()

    for _ in range(120):            # 最多等 60s 出识别结果
        if stt:
            break
        await asyncio.sleep(0.5)
    for _ in range(60):
        if got_tts_start:
            break
        await asyncio.sleep(0.5)

    task.cancel()
    try:
        await ws.close()
    except Exception:
        pass

    ok = bool(stt.strip())
    print("\n=== 结果 ===")
    print(f"ASR 文本: {stt!r}")
    print(f"TTS 已播报: {got_tts_start}")
    print("PASS" if ok else "FAIL（沉默后被判噪声丢弃）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
