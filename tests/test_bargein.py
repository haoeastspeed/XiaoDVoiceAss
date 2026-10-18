# -*- coding: utf-8 -*-
"""barge-in 打断：第一句长回复播放中，用户立即开口，应停止并转入第二句。"""
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
SR = 16000
FRAME_MS = 60
FRAME_SIZE = int(SR * FRAME_MS / 1000)

os.environ["PATH"] = BIN + os.pathsep + os.environ.get("PATH", "")

import opuslib_next
import websockets


def wav_to_pcm(wav, pre=0.0, post=0.0, trim_head=0.0):
    tmp = tempfile.mkdtemp(prefix="bg_")
    pcm_path = os.path.join(tmp, "p.pcm")
    cmd = [os.path.join(BIN, "ffmpeg.exe"), "-y"]
    if trim_head > 0:
        cmd += ["-ss", str(trim_head)]
    cmd += ["-i", wav, "-ar", str(SR), "-ac", "1", "-f", "s16le", pcm_path]
    subprocess.run(cmd, check=True, capture_output=True)
    with open(pcm_path, "rb") as f:
        speech = f.read()
    pre_b = b"\x00" * int(SR * 2 * pre)
    post_b = b"\x00" * int(SR * 2 * post)
    return pre_b + speech + post_b


def encode(pcm):
    enc = opuslib_next.Encoder(SR, 1, opuslib_next.APPLICATION_VOIP)
    out = []
    for i in range(0, len(pcm) - FRAME_SIZE * 2 + 1, FRAME_SIZE * 2):
        out.append(enc.encode(pcm[i:i + FRAME_SIZE * 2], FRAME_SIZE))
    return out


async def send_packets(ws, packets):
    for pkt in packets:
        await ws.send(pkt)
        await asyncio.sleep(FRAME_MS / 1000.0)


async def main():
    ws = await websockets.connect(
        WS_URL, extra_headers={"device-id": "barge-test"}, max_size=None
    )
    stt_texts = []
    tts_start_count = 0
    tts_stop_count = 0
    sid = None
    audio_frames = 0
    audio_before_barge = 0

    async def recv():
        nonlocal tts_start_count, tts_stop_count, sid, audio_frames
        async for raw in ws:
            if isinstance(raw, bytes):
                audio_frames += 1
                continue
            m = json.loads(raw)
            t = m.get("type")
            if t == "hello":
                sid = m.get("session_id")
            elif t == "stt":
                stt_texts.append(m.get("text", ""))
                print(f"[STT] {m.get('text')}", flush=True)
            elif t == "tts":
                st = m.get("state")
                if st == "start":
                    tts_start_count += 1
                    print(f"[TTS start #{tts_start_count}]", flush=True)
                elif st == "stop":
                    tts_stop_count += 1
                    print(f"[TTS stop #{tts_stop_count}]", flush=True)

    task = asyncio.create_task(recv())
    await ws.send(json.dumps({
        "type": "hello", "device_id": "barge-test", "token": "",
        "features": {"mcp": False, "emoji": False},
        "audio_params": {"format": "opus", "sample_rate": 24000,
                         "channels": 1, "frame_duration": 60},
    }))
    for _ in range(50):
        if sid:
            break
        await asyncio.sleep(0.1)

    # 第一句：detect 文本，引出长回复
    await ws.send(json.dumps({
        "type": "listen", "state": "detect",
        "text": "请详细介绍一下你自己，以及你都能帮我做哪些事情，尽量说详细一些",
    }))
    # 等第一句开始播放且已经播了一些
    for _ in range(300):
        if tts_start_count >= 1 and audio_frames >= 20:
            break
        await asyncio.sleep(0.1)
    audio_before_barge = audio_frames
    print(f"[*] 第一句正在播放（已 {audio_before_barge} 帧），现在立即开口打断", flush=True)

    # 第二句：裁掉 wav 开头静音（trim 0.45s），仅留 0.1s pre，让用户立刻开口
    second = encode(wav_to_pcm(SRC_WAV, pre=0.1, post=0.9, trim_head=0.45))
    await send_packets(ws, second)
    print("[*] 第二句已发（打断），等待第二句完整响应", flush=True)

    for _ in range(400):
        if tts_start_count >= 2:
            break
        await asyncio.sleep(0.1)
    deadline = asyncio.get_event_loop().time() + 30
    while asyncio.get_event_loop().time() < deadline:
        if tts_start_count >= 2 and tts_stop_count >= 2:
            break
        await asyncio.sleep(0.2)
    await asyncio.sleep(1.0)
    task.cancel()
    await ws.close()

    print("\n=== 结果 ===")
    print(f"STT 次数={len(stt_texts)}: {stt_texts}")
    print(f"TTS start={tts_start_count}, stop={tts_stop_count}")
    print(f"打断前第一句帧={audio_before_barge}")
    ok = (
        len(stt_texts) >= 1
        and tts_start_count == 2
        and tts_stop_count >= 2
        and audio_before_barge >= 20  # 第一句确实在播放
    )
    print("barge-in 打断：{}".format("通过 ✅" if ok else "存在问题 ❌"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
