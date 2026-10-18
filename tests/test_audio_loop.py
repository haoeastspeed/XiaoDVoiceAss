# -*- coding: utf-8 -*-
"""
M1 验证（二）：音频链路（VAD -> ASR(SenseVoice) -> LLM -> TTS）
用 SenseVoice 自带中文 zh.wav -> 转 16k PCM -> 编 60ms opus -> listen start/二进制/stop
用 xiaozhi-server 的 .venv 运行：
  .venv\\Scripts\\python.exe tests\\test_audio_loop.py
"""
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
DEVICE_ID = "test-device-002"
SAMPLE_RATE = 16000
FRAME_MS = 60
FRAME_SIZE = int(SAMPLE_RATE * FRAME_MS / 1000)  # 960
PRE_SILENCE = 0.2
POST_SILENCE = 0.3
OVERALL_TIMEOUT = 90.0

# 让 opus.dll 可被 opuslib_next 找到（必须在 import opuslib_next 之前）
os.environ["PATH"] = BIN + os.pathsep + os.environ.get("PATH", "")

import opuslib_next
import websockets


def make_pcm() -> bytes:
    tmpdir = tempfile.mkdtemp(prefix="audiotest_")
    pcm = os.path.join(tmpdir, "say.pcm")
    ff = os.path.join(BIN, "ffmpeg.exe")
    subprocess.run(
        [ff, "-y", "-i", SRC_WAV, "-ar", str(SAMPLE_RATE), "-ac", "1",
         "-f", "s16le", "-acodec", "pcm_s16le", pcm],
        check=True, capture_output=True,
    )
    with open(pcm, "rb") as f:
        speech = f.read()
    pre = b"\x00" * int(SAMPLE_RATE * 2 * PRE_SILENCE)
    post = b"\x00" * int(SAMPLE_RATE * 2 * POST_SILENCE)
    audio = pre + speech + post
    print(f"[*] 已载入测试音频 {os.path.basename(SRC_WAV)}，"
          f"总时长 {len(audio)/2/SAMPLE_RATE:.1f}s")
    return audio


def encode_opus(pcm: bytes):
    enc = opuslib_next.Encoder(SAMPLE_RATE, 1, opuslib_next.APPLICATION_VOIP)
    frame_bytes = FRAME_SIZE * 2  # int16
    packets = []
    for i in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
        chunk = pcm[i:i + frame_bytes]
        packets.append(enc.encode(chunk, FRAME_SIZE))
    return packets


async def main() -> int:
    pcm = make_pcm()
    packets = encode_opus(pcm)
    print(f"[*] 编码 {len(packets)} 个 opus 帧")

    try:
        ws = await websockets.connect(
            WS_URL, extra_headers={"device-id": DEVICE_ID}, max_size=None
        )
    except Exception as e:
        print(f"连接失败: {e}")
        return 1

    stt_text = ""
    reply_parts = []
    opus_frames = 0
    got_tts_stop = False
    session_id = None

    async def recv_loop():
        nonlocal stt_text, opus_frames, got_tts_stop, session_id
        async for raw in ws:
            if isinstance(raw, bytes):
                opus_frames += 1
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            t = msg.get("type")
            if t == "hello":
                session_id = msg.get("session_id")
                print(f"[1] 握手成功 session_id={session_id}")
            elif t == "stt":
                stt_text = msg.get("text", "")
                print(f"[2] ASR 识别: {stt_text}")
            elif t == "tts":
                state = msg.get("state")
                if state == "sentence_start":
                    reply_parts.append(msg.get("text", ""))
                    print(f"[3] 回复: {msg.get('text','')}")
                elif state == "stop":
                    got_tts_stop = True
                    print("[4] TTS 结束")

    try:
        recv_task = asyncio.create_task(recv_loop())
        await ws.send(json.dumps({
            "type": "hello", "device_id": DEVICE_ID, "device_name": "M1音频测试",
            "device_mac": "00:11:22:33:44:66", "token": "",
            "features": {"mcp": False, "emoji": False},
            "audio_params": {"format": "opus", "sample_rate": 24000,
                             "channels": 1, "frame_duration": 60},
        }))
        for _ in range(50):
            if session_id:
                break
            await asyncio.sleep(0.1)
        if not session_id:
            print("握手超时")
            return 1

        # manual 模式：缓存全部音频，listen stop 时一次性识别（只触发一次，避免重复）
        await ws.send(json.dumps({"type": "listen", "state": "start", "mode": "manual"}))
        for pkt in packets:
            await ws.send(pkt)
            await asyncio.sleep(FRAME_MS / 1000.0)
        await ws.send(json.dumps({"type": "listen", "state": "stop"}))
        print(f"[*] 已实时发送 {len(packets)} 帧 + listen stop")

        deadline = asyncio.get_event_loop().time() + OVERALL_TIMEOUT
        while not got_tts_stop and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.2)
        await asyncio.sleep(1.0)
        recv_task.cancel()
    finally:
        await ws.close()

    reply = "".join(reply_parts).strip()
    # 去除标点/空白后，判断识别是否为有效中文
    asr_core = "".join(ch for ch in stt_text if ch not in "，。！？、,.!? ")
    print("\n================ 结果汇总 ================")
    print(f"源音频   : {os.path.basename(SRC_WAV)}")
    print(f"ASR 识别 : {stt_text or '(无)'}")
    print(f"小D 回复 : {reply or '(无)'}")
    print(f"回复 opus: {opus_frames} 帧")
    print("==========================================")

    asr_ok = len(asr_core) >= 4
    ok = asr_ok and bool(reply) and opus_frames > 0 and got_tts_stop
    print("结论：音频链路（ASR + LLM + TTS）{}".format("通过 ✅" if ok else "存在问题 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
