# -*- coding: utf-8 -*-
"""稳定性验证：连续跑 N 次音频链路，统计真实 LLM 回复成功率。"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile

ROOT = "D:\\projects\\xiaod-voice"
BIN = os.path.join(ROOT, "reference", "bin")
SRC_WAV = ("D:/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09/"
           "test_wavs/zh.wav")
WS_URL = "ws://localhost:8000/xiaozhi/v1/"
SAMPLE_RATE = 16000
FRAME_MS = 60
FRAME_SIZE = int(SAMPLE_RATE * FRAME_MS / 1000)
ROUNDS = 4

os.environ["PATH"] = BIN + os.pathsep + os.environ.get("PATH", "")

import opuslib_next
import websockets


def load_packets():
    tmpdir = tempfile.mkdtemp(prefix="st_")
    pcm_path = os.path.join(tmpdir, "p.pcm")
    subprocess.run(
        [os.path.join(BIN, "ffmpeg.exe"), "-y", "-i", SRC_WAV,
         "-ar", str(SAMPLE_RATE), "-ac", "1", "-f", "s16le", pcm_path],
        check=True, capture_output=True,
    )
    with open(pcm_path, "rb") as f:
        pcm = f.read()
    enc = opuslib_next.Encoder(SAMPLE_RATE, 1, opuslib_next.APPLICATION_VOIP)
    packets = []
    for i in range(0, len(pcm) - FRAME_SIZE * 2 + 1, FRAME_SIZE * 2):
        packets.append(enc.encode(pcm[i:i + FRAME_SIZE * 2], FRAME_SIZE))
    return packets


async def one_round(round_idx, packets):
    device = f"stab-{round_idx}"
    ws = await websockets.connect(
        WS_URL, additional_headers={"device-id": device}, max_size=None
    )
    stt = ""
    replies = []
    frames = 0
    got_stop = False
    sid = None

    async def recv():
        nonlocal stt, frames, got_stop, sid
        async for raw in ws:
            if isinstance(raw, bytes):
                frames += 1
                continue
            m = json.loads(raw)
            t = m.get("type")
            if t == "hello":
                sid = m.get("session_id")
            elif t == "stt":
                stt = m.get("text", "")
            elif t == "tts":
                if m.get("state") == "sentence_start":
                    replies.append(m.get("text", ""))
                elif m.get("state") == "stop":
                    got_stop = True

    task = asyncio.create_task(recv())
    await ws.send(json.dumps({
        "type": "hello", "device_id": device, "token": "",
        "features": {"mcp": False, "emoji": False},
        "audio_params": {"format": "opus", "sample_rate": 24000,
                         "channels": 1, "frame_duration": 60},
    }))
    for _ in range(50):
        if sid:
            break
        await asyncio.sleep(0.1)
    await ws.send(json.dumps({"type": "listen", "state": "start", "mode": "manual"}))
    for pkt in packets:
        await ws.send(pkt)
        await asyncio.sleep(FRAME_MS / 1000.0)
    await ws.send(json.dumps({"type": "listen", "state": "stop"}))

    deadline = asyncio.get_event_loop().time() + 90
    while not got_stop and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.2)
    await asyncio.sleep(0.5)
    task.cancel()
    await ws.close()

    reply = "".join(replies).strip()
    fallback = "没反应过来" in reply
    return stt, reply, frames, got_stop, fallback


async def main():
    packets = load_packets()
    real_ok = 0
    for i in range(ROUNDS):
        stt, reply, frames, stop, fallback = await one_round(i, packets)
        good = bool(reply) and not fallback and stop and frames > 0
        if good:
            real_ok += 1
        tag = "OK" if good else ("兜底" if fallback else "失败")
        print(f"第{i+1}轮 [{tag}] ASR={stt} | 回复={reply} | {frames}帧", flush=True)
    print(f"\n真实回复成功 {real_ok}/{ROUNDS}")
    sys.exit(0 if real_ok == ROUNDS else 1)


if __name__ == "__main__":
    asyncio.run(main())
