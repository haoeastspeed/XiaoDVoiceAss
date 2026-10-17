# -*- coding: utf-8 -*-
"""视觉大模型（OpenAI 兼容多模态）——把一张图 + 一个问题交给模型回答。

国内多数多模态模型都提供 OpenAI 兼容的 `chat/completions`（qwen-vl / glm-4v /
gpt-4o 等），请求体用 content 数组 + image_url(data URI) 即可。
"""
from __future__ import annotations

import base64
from typing import Optional

import httpx


class VisionLLM:
    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout_s: float = 45.0):
        b = (base_url or "").strip().rstrip("/")
        if b.endswith("/chat/completions"):
            b = b[: -len("/chat/completions")]
        self.base_url = b
        self.api_key = (api_key or "").strip()
        self.model = model
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=timeout_s,
                                  write=30.0, pool=10.0),
            trust_env=False)

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.model)

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def describe(self, image_bytes: bytes, question: str,
                       image_format: str = "jpeg") -> str:
        """看图回答。失败抛异常，由调用方转成工具错误结果。"""
        if not self.ready:
            raise RuntimeError("视觉模型未配置")
        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_uri = f"data:image/{image_format};base64,{b64}"
        payload = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text",
                     "text": question or "请描述这张图片的内容。"},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }],
            "max_tokens": 512,
        }
        r = await self._client.post(f"{self.base_url}/chat/completions",
                                    json=payload, headers=self._headers())
        r.raise_for_status()
        data = r.json()
        try:
            return (data["choices"][0]["message"]["content"] or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    async def aclose(self):
        await self._client.aclose()


def from_config(config) -> Optional[VisionLLM]:
    """按网关配置构造视觉模型（沿用当前 LLM 后端与在线地址）。"""
    model = str(getattr(config, "vision_model", "") or "").strip()
    base = str(getattr(config, "llm_api_base", "") or "").strip()
    key = str(getattr(config, "llm_api_key", "") or "")
    if not model:
        # 未单配就以当前对话模型兜底（多模态模型时可用）
        model = str(getattr(config, "llm_model", "") or "")
    if not base:
        return None
    return VisionLLM(base, key, model)
