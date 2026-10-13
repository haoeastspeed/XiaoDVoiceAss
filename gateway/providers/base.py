# -*- coding: utf-8 -*-
"""Provider 统一接口基类。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, List, Tuple


class BaseLLM(ABC):
    @abstractmethod
    async def chat(self, messages: List[dict]) -> AsyncIterator[str]:
        """流式产出文本增量。"""
        raise NotImplementedError


class BaseASR(ABC):
    @abstractmethod
    def transcribe(self, pcm_s16le: bytes) -> str:
        """16k s16le PCM → 文本。"""
        raise NotImplementedError


class BaseTTS(ABC):
    @abstractmethod
    async def synthesize(self, text: str) -> List[bytes]:
        """文本 → 24k opus 包列表。"""
        raise NotImplementedError

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        """流式产出 opus 包（默认实现等价于一次性合成）。
        子类可覆盖，让首包更早发出，降低首句延迟。"""
        for p in await self.synthesize(text):
            yield p


class BaseVAD(ABC):
    @abstractmethod
    def new_state(self) -> dict:
        """创建会话独立的 VAD 状态。"""
        raise NotImplementedError

    @abstractmethod
    def reset_sentence(self, state: dict) -> None:
        """重置本句的断句状态（保留 VAD 内部连续状态）。"""
        raise NotImplementedError

    @abstractmethod
    def process(self, state: dict, pcm_s16le: bytes) -> Tuple[bool, bool]:
        """处理一帧 PCM，返回 (当前是否在说话, 本句是否结束)。"""
        raise NotImplementedError
