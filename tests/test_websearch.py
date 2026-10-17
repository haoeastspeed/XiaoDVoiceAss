# -*- coding: utf-8 -*-
"""联网搜索链路回归：开启后，实时类问题应触发 search start/end 并给出回答。

直接用 `listen detect` 送入文本（跳过 ASR），验证：
  1) set_search 开关生效；
  2) 需要实时信息的问题会触发联网检索（收到 search start/end）；
  3) 检索结果被注入后模型仍能正常出话（tts sentence_start）。
"""
import asyncio
import json
import os

WS_URL = "ws://127.0.0.1:8000/xiaozhi/v1/"
BIN = r"D:\projects\xiaod-voice\reference\bin"
os.environ["PATH"] = BIN + os.pathsep + os.environ.get("PATH", "")

import websockets  # noqa: E402


async def ask(text: str, enabled: bool = True, wait: float = 90.0):
    ws = await websockets.connect(
        WS_URL, additional_headers={"device-id": "search-test"},
        max_size=None, ping_interval=None)
    ev = {"search": None, "count": None, "reply": [], "stt": ""}

    async def recv():
        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)):
                continue
            m = json.loads(raw)
            t = m.get("type")
            if t == "stt":
                ev["stt"] = m.get("text", "")
            elif t == "search":
                if m.get("state") == "start":
                    ev["search"] = m.get("query", "")
                    print(f"  [search start] {ev['search']}", flush=True)
                else:
                    ev["count"] = m.get("count")
                    print(f"  [search end] 命中 {ev['count']} 条", flush=True)
            elif t == "tts" and m.get("state") == "sentence_start":
                ev["reply"].append(m.get("text", ""))

    task = asyncio.create_task(recv())
    await ws.send(json.dumps({
        "type": "hello", "device_id": "search-test", "device_name": "t",
        "device_mac": "00:00:00:00:00:00", "token": "",
        "features": {"mcp": False},
        "audio_params": {"format": "opus", "sample_rate": 24000,
                         "channels": 1, "frame_duration": 60}}))
    await asyncio.sleep(0.4)
    await ws.send(json.dumps({"type": "listen", "state": "start",
                              "mode": "auto"}))
    await ws.send(json.dumps({"type": "set_search", "enabled": enabled}))
    await asyncio.sleep(0.2)
    await ws.send(json.dumps({"type": "listen", "state": "detect",
                              "text": text}))
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < wait:
        if ev["reply"]:
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
    print("=== 用例1：需要联网（今天北京天气）===")
    a = await ask("今天北京天气怎么样？", enabled=True)
    print("  回复:", "".join(a["reply"])[:80] or "(无)")
    print("=== 用例2：不需要联网（闲聊）===")
    b = await ask("你好呀，陪我聊聊天吧。", enabled=True)
    print("  搜索触发:", b["search"] or "无（正确）")
    print("  回复:", "".join(b["reply"])[:80] or "(无)")

    ok1 = bool(a["search"]) and bool(a["reply"])
    ok2 = (b["search"] is None) and bool(b["reply"])
    print("\n=== 结果 ===")
    print(f"实时问题触发搜索并回答: {'PASS' if ok1 else 'FAIL'}")
    print(f"闲聊不触发搜索且正常回答: {'PASS' if ok2 else 'FAIL'}")
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
