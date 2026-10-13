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
    def __init__(self, base_url: str, model: str, enable_thinking: bool = False):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.enable_thinking = enable_thinking
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=40.0, write=60.0, pool=10.0),
        )

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

    async def aclose(self):
        await self._client.aclose()
