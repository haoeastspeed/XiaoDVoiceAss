# -*- coding: utf-8 -*-
"""测原生 /api/chat 流式，逐 chunk 打印时间，判断是否真流式。"""
import asyncio
import json
import time

import httpx


async def main():
    url = "http://localhost:11434/api/chat"
    body = {
        "model": "qwen3.5:9b-q4km",
        "messages": [
            {"role": "system", "content": "你是小D，简洁回答。"},
            {"role": "user", "content": "用一句话介绍你自己"},
        ],
        "stream": True,
        "think": False,
    }
    t0 = time.monotonic()
    n = 0
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream("POST", url, json=body) as resp:
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                n += 1
                m = json.loads(line)
                c = m.get("message", {}).get("content", "")
                done = m.get("done")
                print(f"[{time.monotonic()-t0:6.2f}s] #{n:<3} {c!r} done={done}", flush=True)
    print(f"总耗时 {time.monotonic()-t0:.2f}s，共 {n} 行")


if __name__ == "__main__":
    asyncio.run(main())
