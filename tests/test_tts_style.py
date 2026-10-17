# -*- coding: utf-8 -*-
"""TTS 双引擎 + 语速语气回归。

用例：
  1) auto 引擎：正常出话，且服务端日志里能看到 [style] 判定结果（语速/语气）；
  2) local 引擎：切到本地离线音色后仍能正常合成出下行音频帧；
  3) 无效在线音色：不应把整轮对话卡死（自动降级到本地）。
"""
import asyncio
import json
import os

WS_URL = "ws://127.0.0.1:8000/xiaozhi/v1/"
os.environ["PATH"] = (r"D:\projects\xiaod-voice\reference\bin" + os.pathsep
                      + os.environ.get("PATH", ""))

import websockets  # noqa: E402


async def ask(text, voice=None, engine=None, wait=90.0):
    ws = await websockets.connect(
        WS_URL, additional_headers={"device-id": "tts-test"},
        max_size=None, ping_interval=None)
    ev = {"frames": 0, "sent": [], "err": []}

    async def recv():
        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)):
                ev["frames"] += 1
                continue
            m = json.loads(raw)
            if m.get("type") == "tts" and m.get("state") == "sentence_start":
                ev["sent"].append(m.get("text", ""))

    task = asyncio.create_task(recv())
    await ws.send(json.dumps({
        "type": "hello", "device_id": "tts-test", "device_name": "t",
        "device_mac": "00:00:00:00:00:00", "token": "",
        "features": {"mcp": False},
        "audio_params": {"format": "opus", "sample_rate": 24000,
                         "channels": 1, "frame_duration": 60}}))
    await asyncio.sleep(0.4)
    await ws.send(json.dumps({"type": "listen", "state": "start",
                              "mode": "auto"}))
    if voice:
        await ws.send(json.dumps({"type": "set_voice", "voice": voice}))
    if engine:
        await ws.send(json.dumps({"type": "set_tts_engine", "engine": engine}))
    await asyncio.sleep(0.3)
    await ws.send(json.dumps({"type": "listen", "state": "detect",
                              "text": text}))
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < wait:
        if ev["frames"] > 20:
            break
        await asyncio.sleep(0.3)
    await asyncio.sleep(1.0)
    task.cancel()
    try:
        await ws.close()
    except Exception:
        pass
    return ev


async def main():
    print("=== 用例1：自动引擎（应看到 [style] 判定）===")
    a = await ask("我今天特别开心，我们出去玩吧！")
    print("  句子:", len(a["sent"]), " 下行帧:", a["frames"])
    print("  首句:", (a["sent"][0] if a["sent"] else "(无)")[:40])

    print("=== 用例2：本地离线引擎 ===")
    b = await ask("你好，用本地声音说句话。", engine="local")
    print("  句子:", len(b["sent"]), " 下行帧:", b["frames"])

    print("=== 用例3：无效在线音色（应自动降级不卡死）===")
    c = await ask("测试一下异常音色。", voice="zh-CN-NoSuchVoiceAtAll")
    print("  句子:", len(c["sent"]), " 下行帧:", c["frames"])

    ok1 = len(a["sent"]) >= 1 and a["frames"] > 20
    ok2 = len(b["sent"]) >= 1 and b["frames"] > 20
    ok3 = len(c["sent"]) >= 1 and c["frames"] > 20
    print("\n=== 结果 ===")
    print("自动引擎出话:", "PASS" if ok1 else "FAIL")
    print("本地离线引擎出话:", "PASS" if ok2 else "FAIL")
    print("异常音色自动降级:", "PASS" if ok3 else "FAIL")
    return 0 if (ok1 and ok2 and ok3) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
