# -*- coding: utf-8 -*-
"""
M1 验证（一）：文本链路（不走麦克风、无需 GUI）
流程：WS 连接(带 device-id) -> hello 握手 -> listen detect text
      -> 收集 STT / 回复文本(llm 或 tts sentence_start) / TTS 状态 / 二进制 opus 帧
用 xiaozhi-server 的 .venv 运行：
  .venv\\Scripts\\python.exe tests\\test_text_loop.py
"""
import asyncio
import json
import sys

import websockets

WS_URL = "ws://localhost:8000/xiaozhi/v1/"
DEVICE_ID = "test-device-001"
PROMPT = "你好，请用一句话介绍你自己。"
OVERALL_TIMEOUT = 60.0


async def main() -> int:
    print(f"[1] 连接 {WS_URL}")
    try:
        ws = await websockets.connect(
            WS_URL,
            extra_headers={"device-id": DEVICE_ID},
            max_size=None,
        )
    except Exception as e:
        print(f"    连接失败: {e}")
        return 1

    stt_text = ""
    llm_parts = []
    reply_parts = []   # 该版本回复文本随 tts sentence_start 携带
    tts_states = []
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
                print(f"[2] 握手成功，session_id={session_id}")
            elif t == "stt":
                stt_text = msg.get("text", "")
                print(f"[3] STT 回显: {stt_text}")
            elif t == "llm":
                llm_parts.append(msg.get("text", ""))
            elif t == "tts":
                state = msg.get("state")
                tts_states.append(state)
                if state == "sentence_start":
                    txt = msg.get("text", "")
                    reply_parts.append(txt)
                    print(f"[4] 回复句: {txt}")
                elif state == "stop":
                    got_tts_stop = True
                    print("[5] TTS 结束(stop)")

    try:
        recv_task = asyncio.create_task(recv_loop())

        hello = {
            "type": "hello",
            "device_id": DEVICE_ID,
            "device_name": "M1自动测试",
            "device_mac": "00:11:22:33:44:55",
            "token": "",
            "features": {"mcp": False, "emoji": False},
            "audio_params": {
                "format": "opus",
                "sample_rate": 24000,
                "channels": 1,
                "frame_duration": 60,
            },
        }
        await ws.send(json.dumps(hello))

        for _ in range(50):
            if session_id:
                break
            await asyncio.sleep(0.1)
        if not session_id:
            print("    握手超时（未收到 hello）")
            return 1

        await ws.send(json.dumps({"type": "listen", "state": "detect", "text": PROMPT}))
        print(f"[*] 已发送指令: {PROMPT}")

        deadline = asyncio.get_event_loop().time() + OVERALL_TIMEOUT
        while not got_tts_stop and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.2)

        await asyncio.sleep(1.0)
        recv_task.cancel()
    finally:
        await ws.close()

    llm_text = "".join(llm_parts).strip()
    reply_text = "".join(reply_parts).strip() or llm_text
    print("\n================ 结果汇总 ================")
    print(f"STT 回显 : {stt_text or '(无)'}")
    print(f"回复文本 : {reply_text or '(无)'}")
    print(f"TTS 状态 : {' -> '.join([s for s in tts_states if s])}")
    print(f"Opus 帧数: {opus_frames}")
    print("==========================================")

    ok = bool(reply_text) and opus_frames > 0 and got_tts_stop
    print("结论：文本链路（LLM + TTS）{}".format("通过 ✅" if ok else "存在问题 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
