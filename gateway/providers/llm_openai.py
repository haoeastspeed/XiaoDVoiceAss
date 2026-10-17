# -*- coding: utf-8 -*-
"""OpenAI 兼容 Chat Completions 大模型 Provider（流式 SSE）。

国内主流在线大模型基本都提供 OpenAI 兼容端点，所以只写这一个实现就能接住
一整片：DeepSeek、阿里百炼（兼容模式）、智谱 GLM、Kimi、硅基流动、OpenAI、
以及任何自建的 vLLM / Ollama(兼容模式) / One-API 聚合。

端点约定统一为 **base_url + /chat/completions**：
  * base_url 填到含版本号的那一层即可，例如
    `https://api.deepseek.com/v1` → 实际请求 `.../v1/chat/completions`
  * 若用户填的 base_url 已经自带 `/chat/completions`，也认（不重复拼接）。

流式用 SSE（`data: {...}` 行），非 200 或非流式的响应当作普通 JSON 处理，
这样"端点不支持 stream"的服务商也能用。
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Dict, List

import httpx

from gateway.providers.base import BaseLLM

# 可安全重试的网络/协议类异常
_RETRY_ERRORS = (
    httpx.TransportError,
    httpx.RemoteProtocolError,
    ConnectionError,
    TimeoutError,
)
_MAX_ATTEMPTS = 2


def _normalize_base(base_url: str) -> str:
    """把用户填的 base_url 规整成可以直接拼 '/chat/completions' 的形式。"""
    b = (base_url or "").strip().rstrip("/")
    if not b:
        return ""
    if b.endswith("/chat/completions"):
        return b[: -len("/chat/completions")]
    return b


class OpenAICompatLLM(BaseLLM):
    #: OpenAI 兼容端点的 function calling（tools）——主流服务商均支持。
    supports_tools = True

    def __init__(self, base_url: str, api_key: str, model: str,
                 temperature: float = 0.7, timeout_s: float = 60.0):
        self.base_url = _normalize_base(base_url)
        self.api_key = (api_key or "").strip()
        self.model = model
        self.temperature = temperature
        # 在线服务首 Token 通常比本地慢，读超时给足；连接阶段单独卡 10s。
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=timeout_s,
                                  write=30.0, pool=10.0),
            trust_env=False,
        )

    # ---------- 内部 ----------

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    @property
    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _payload(self, messages: List[dict], stream: bool,
                 max_tokens: int = 0, temperature: float | None = None,
                 extra: dict | None = None) -> dict:
        p = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "temperature": self.temperature if temperature is None
            else temperature,
        }
        if max_tokens:
            p["max_tokens"] = int(max_tokens)
        if extra:
            p.update(extra)
        return p

    async def quick(self, prompt: str, max_tokens: int = 64,
                    timeout: float = 30.0) -> str:
        """短问短答（非流式）：给意图判断 / 历史压缩这类内部小任务用。"""
        payload = self._payload([{"role": "user", "content": prompt}],
                                stream=False, max_tokens=max_tokens,
                                temperature=0.1)
        r = await self._client.post(self._endpoint, json=payload,
                                    headers=self._headers(), timeout=timeout)
        r.raise_for_status()
        data = r.json()
        try:
            return ((data["choices"][0]["message"]["content"]) or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    # ---------- 流式 ----------

    async def _stream_once(self, payload: dict) -> AsyncIterator[str]:
        headers = self._headers()
        headers["Accept"] = "text/event-stream"
        async with self._client.stream("POST", self._endpoint, json=payload,
                                       headers=headers) as resp:
            resp.raise_for_status()
            ctype = (resp.headers.get("content-type") or "").lower()
            if "event-stream" not in ctype:
                # 服务端没真流式（或网关把 SSE 缓冲成整块）→ 当 JSON 收
                body = await resp.aread()
                try:
                    data = json.loads(body.decode("utf-8", "replace"))
                    txt = (data["choices"][0]["message"]["content"]) or ""
                except Exception:  # noqa: BLE001
                    txt = ""
                if txt:
                    yield txt
                return
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    m = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                try:
                    delta = m["choices"][0]["delta"]
                except Exception:  # noqa: BLE001
                    continue
                # 智谱 / 部分聚合端会把思考过程放在 reasoning_content，
                # 正文放在 content；只取正文，别把思考过程念出来。
                piece = delta.get("content") or ""
                if piece:
                    yield piece

    async def chat(self, messages: List[dict]) -> AsyncIterator[str]:
        payload = self._payload(messages, stream=True)
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
                    await asyncio.sleep(0.5)
                    continue
                raise
            if produced:
                return
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(0.5)
        # 全部尝试都为空：由上层走兜底

    async def keepalive(self):
        """在线后端没有"模型被逐出"这回事，什么都不用做。"""
        return None

    # ---------- 工具调用（function calling）----------

    def _accumulate_delta_tool_calls(self, acc: dict, order: list, tcs: list):
        """按 index 累积 delta 里的 tool_calls 分片（对齐官方 _merge_tool_calls）。"""
        for tc in tcs or []:
            if not isinstance(tc, dict):
                continue
            idx = tc.get("index", 0)
            try:
                idx = int(idx)
            except (TypeError, ValueError):
                idx = 0
            slot = acc.get(idx)
            if slot is None:
                slot = {"id": "", "name": "", "arguments": ""}
                acc[idx] = slot
                order.append(idx)
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                nm = fn["name"]
                if not slot["name"]:
                    slot["name"] = nm
                elif nm not in slot["name"]:
                    slot["name"] += nm
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]

    def _finalize_tool_calls(self, acc: dict, order: list) -> list:
        out = []
        for i in order:
            slot = acc.get(i) or {}
            name = (slot.get("name") or "").strip()
            if not name:
                continue
            out.append({
                "id": slot.get("id") or f"call_{i}",
                "name": name,
                "arguments": slot.get("arguments") or "{}",
            })
        return out

    async def _stream_tools_once(self, payload: dict):
        headers = self._headers()
        headers["Accept"] = "text/event-stream"
        acc: dict = {}
        order: list = []
        async with self._client.stream("POST", self._endpoint, json=payload,
                                       headers=headers) as resp:
            resp.raise_for_status()
            ctype = (resp.headers.get("content-type") or "").lower()
            if "event-stream" not in ctype:
                body = await resp.aread()
                try:
                    data = json.loads(body.decode("utf-8", "replace"))
                    msg = data["choices"][0]["message"]
                except Exception:  # noqa: BLE001
                    msg = {}
                txt = (msg.get("content") or "")
                if txt:
                    yield ("content", txt)
                self._accumulate_delta_tool_calls(
                    acc, order, msg.get("tool_calls"))
                final = self._finalize_tool_calls(acc, order)
                if final:
                    yield ("tool_calls", final)
                return
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    m = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                try:
                    delta = m["choices"][0]["delta"]
                except Exception:  # noqa: BLE001
                    continue
                piece = delta.get("content") or ""
                if piece:
                    yield ("content", piece)
                if delta.get("tool_calls"):
                    self._accumulate_delta_tool_calls(
                        acc, order, delta["tool_calls"])
        final = self._finalize_tool_calls(acc, order)
        if final:
            yield ("tool_calls", final)

    async def chat_with_tools(self, messages, tools):
        payload = self._payload(messages, stream=True)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
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
                    await asyncio.sleep(0.5)
                    continue
                raise
            if produced or tool_calls:
                return
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(0.5)

    async def aclose(self):
        await self._client.aclose()

    # ---------- 模型列表 ----------

    async def list_models(self) -> List[str]:
        """拉服务端可用模型（失败返回空列表，由上层用预设兜底）。"""
        if not self.base_url:
            return []
        try:
            r = await self._client.get(f"{self.base_url}/models",
                                       headers=self._headers(), timeout=6.0)
            if r.status_code != 200:
                return []
            data = r.json()
            return sorted({(m.get("id") or m.get("name") or "")
                           for m in data.get("data", [])
                           if (m.get("id") or m.get("name"))})
        except Exception:  # noqa: BLE001
            return []


# ---- 常见在线后端预设 ----
# 只列提供 OpenAI 兼容端点、且国内可直连的。base_url 一律填到版本号那一层。
PRESETS: List[dict] = [
    {"id": "deepseek", "name": "DeepSeek（深度求索）",
     "base_url": "https://api.deepseek.com/v1",
     "model": "deepseek-chat", "key_hint": "sk- 开头"},
    {"id": "dashscope", "name": "阿里百炼（通义千问）",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "model": "qwen-plus", "key_hint": "sk- 开头"},
    {"id": "zhipu", "name": "智谱 ChatGLM（GLM-4）",
     "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "model": "glm-4-flash", "key_hint": "控制台 API Key"},
    {"id": "moonshot", "name": "Kimi（月之暗面）",
     "base_url": "https://api.moonshot.cn/v1",
     "model": "moonshot-v1-8k", "key_hint": "sk- 开头"},
    {"id": "siliconflow", "name": "硅基流动（SiliconFlow）",
     "base_url": "https://api.siliconflow.cn/v1",
     "model": "Qwen/Qwen2.5-7B-Instruct", "key_hint": "sk- 开头"},
    {"id": "openai", "name": "OpenAI",
     "base_url": "https://api.openai.com/v1",
     "model": "gpt-4o-mini", "key_hint": "sk- 开头"},
]


def preset_by_id(pid: str) -> dict | None:
    for p in PRESETS:
        if p["id"] == pid:
            return p
    return None
