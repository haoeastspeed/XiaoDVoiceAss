# -*- coding: utf-8 -*-
"""auto 模式：VAD 自动断句 -> ASR -> LLM -> TTS，验证免按键自然对话。"""
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
PRE_SILENCE = 0.5
POST_SILENCE = 0.9

os.environ["PATH"] = BIN + os.pathsep + os.environ.get("PATH", "")

import opuslib_next
import websockets


def load_packets():
    tmp = tempfile.mkdtemp(prefix="auto_")
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
        WS_URL, additional_headers={"device-id": "auto-test"}, max_size=None
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
                print(f"[VAD自动断句] ASR: {stt}", flush=True)
            elif t == "tts":
                if m.get("state") == "sentence_start":
                    replies.append(m.get("text", ""))
                elif m.get("state") == "stop":
                    got_stop = True

    task = asyncio.create_task(recv())
    await ws.send(json.dumps({
        "type": "hello", "device_id": "auto-test", "token": "",
        "features": {"mcp": False, "emoji": False},
        "audio_params": {"format": "opus", "sample_rate": 24000,
                         "channels": 1, "frame_duration": 60},
    }))
    for _ in range(50):
        if sid:
            break
        await asyncio.sleep(0.1)

    # auto 模式（默认），不发 stop，由 VAD 自动判结束
    await ws.send(json.dumps({"type": "listen", "state": "start", "mode": "auto"}))
    for pkt in packets:
        await ws.send(pkt)
        await asyncio.sleep(FRAME_MS / 1000.0)
    print(f"[*] 已发 {len(packets)} 帧（含前后静音），等待 VAD 自动断句", flush=True)

    deadline = asyncio.get_event_loop().time() + 60
    while not got_stop and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.2)
    await asyncio.sleep(0.5)
    task.cancel()
    await ws.close()

    reply = "".join(replies).strip()
    asr_core = "".join(ch for ch in stt if ch not in "，。！？、,.!? ")
    print(f"\nASR={stt}")
    print(f"回复={reply}")
    print(f"下行 {frames} 帧")
    ok = len(asr_core) >= 4 and bool(reply) and frames > 0 and got_stop
    print("auto 模式：{}".format("通过 ✅" if ok else "存在问题 ❌"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
