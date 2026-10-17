# -*- coding: utf-8 -*-
"""Ollama 本地大模型 Provider（走原生 /api/chat，可控 thinking、真流式）。"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, List

import httpx

from gateway.providers.base import BaseLLM

# 可安全重试的网络/协议类异常
_RETRY_ERRORS = (
    httpx.TransportError,
    httpx.RemoteProtocolError,
    ConnectionError,
    TimeoutError,
)
_MAX_ATTEMPTS = 3
_RETRY_DELAY = (0.4, 0.8)


class OllamaLLM(BaseLLM):
    #: Ollama >= 0.3 的 /api/chat 支持 tools（function calling）。
    supports_tools = True

    def __init__(self, base_url: str, model: str, enable_thinking: bool = False,
                 keep_alive: str = "30m"):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.enable_thinking = enable_thinking
        # Ollama 默认 keep_alive 只有 5 分钟：闲置后模型被逐出，下一句就要
        # 冷加载数 GB（首句延迟数十秒）。显式按久一点。
        self.keep_alive = keep_alive
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=40.0, write=60.0, pool=10.0),
            # Ollama 就在本机，绝不能走系统/环境里的 HTTP 代理 —— 有些环境
            # （公司网关、某些开发工具）会注入 http_proxy，代理去连 localhost
            # 会直接回 502 "upstream connect failed"，表现为"模型没反应"。
            trust_env=False,
        )

    async def quick(self, prompt: str, max_tokens: int = 32,
                    timeout: float = 30.0) -> str:
        """短问短答（非流式）：给"要不要联网搜索"这类内部小判断用。

        注意：绝不能让它的异常冒出去影响整轮对话，调用方负责兜底。"""
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "think": False,
            "keep_alive": self.keep_alive,
            "options": {"num_predict": int(max_tokens), "temperature": 0.1},
        }
        r = await self._client.post(f"{self.base_url}/api/chat", json=payload,
                                    timeout=timeout)
        r.raise_for_status()
        data = r.json()
        return ((data.get("message") or {}).get("content") or "").strip()

    async def _stream_once(self, payload: dict) -> AsyncIterator[str]:
        """单次流式请求，逐 token 产出正文。"""
        async with self._client.stream(
            "POST", f"{self.base_url}/api/chat", json=payload
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                m = json.loads(line)
                content = m.get("message", {}).get("content", "")
                if content:
                    yield content

    async def chat(self, messages: List[dict]) -> AsyncIterator[str]:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "think": self.enable_thinking,
            "keep_alive": self.keep_alive,
        }
        for attempt in range(_MAX_ATTEMPTS):
            produced = False
            try:
                async for piece in self._stream_once(payload):
                    produced = True
                    yield piece
            except asyncio.CancelledError:
                raise
            except _RETRY_ERRORS:
                if attempt < _MAX_ATTEMPTS - 1:
                    await asyncio.sleep(_RETRY_DELAY[min(attempt, len(_RETRY_DELAY) - 1)])
                    continue
                raise
            if produced:
                return
            # 整条流无任何正文：偶发空响应，重试
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_RETRY_DELAY[min(attempt, len(_RETRY_DELAY) - 1)])
        # 全部尝试都为空：由上层走兜底

    # ---------- 工具调用（function calling）----------

    async def _stream_tools_once(self, payload: dict):
        """带 tools 的单次流式请求。

        产出 ("content", 片段) 与（流末尾）("tool_calls", [ {id,name,arguments} ])。
        Ollama 老版本 / 不支持 tools 的模型会对 tools 参数回 400，这里显式抛出，
        由上层熔断回落到普通对话。"""
        calls: dict = {}
        order: list = []
        async with self._client.stream(
            "POST", f"{self.base_url}/api/chat", json=payload
        ) as resp:
            if resp.status_code == 400:
                body = await resp.aread()
                raise RuntimeError(
                    "ollama 不接受 tools 参数：" +
                    body.decode("utf-8", "replace")[:200])
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                m = json.loads(line)
                msg = m.get("message") or {}
                content = msg.get("content") or ""
                if content:
                    yield ("content", content)
                for tc in (msg.get("tool_calls") or []):
                    fn = tc.get("function") or {}
                    name = fn.get("name") or tc.get("name") or ""
                    if not name:
                        continue
                    args = fn.get("arguments", tc.get("arguments"))
                    cid = tc.get("id") or f"call_{len(order)}"
                    if cid not in calls:
                        order.append(cid)
                    calls[cid] = {"id": cid, "name": name, "arguments": args}
        if order:
            yield ("tool_calls", [calls[c] for c in order])

    async def chat_with_tools(self, messages, tools):
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "think": self.enable_thinking,
            "keep_alive": self.keep_alive,
            "tools": tools or [],
        }
        for attempt in range(_MAX_ATTEMPTS):
            produced = False
            tool_calls = []
            try:
                async for ev in self._stream_tools_once(payload):
                    if ev[0] == "content":
                        produced = True
                    elif ev[0] == "tool_calls":
                        tool_calls = ev[1]
                    yield ev
            except asyncio.CancelledError:
                raise
            except _RETRY_ERRORS:
                if attempt < _MAX_ATTEMPTS - 1:
                    await asyncio.sleep(
                        _RETRY_DELAY[min(attempt, len(_RETRY_DELAY) - 1)])
                    continue
                raise
            if produced or tool_calls:
                return
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(
                    _RETRY_DELAY[min(attempt, len(_RETRY_DELAY) - 1)])

    async def keepalive(self):
        """最小请求重置 Ollama 的 keep_alive 计时器，把模型按在内存里。"""
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "think": False,
            "keep_alive": self.keep_alive,
            "options": {"num_predict": 1},
        }
        try:
            await self._client.post(
                f"{self.base_url}/api/chat", json=payload, timeout=15.0)
        except Exception:  # noqa: BLE001
            pass

    async def aclose(self):
        await self._client.aclose()
