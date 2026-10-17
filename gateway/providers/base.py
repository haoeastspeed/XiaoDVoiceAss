# -*- coding: utf-8 -*-
"""Provider 统一接口基类。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, List, Optional, Tuple


class BaseLLM(ABC):
    #: 该后端是否支持 function calling（tools 参数）。子类按实现置 True。
    #: ToolCallLoop 据此决定走工具通道还是普通对话通道。
    supports_tools: bool = False

    @abstractmethod
    async def chat(self, messages: List[dict]) -> AsyncIterator[str]:
        """流式产出文本增量。"""
        raise NotImplementedError

    async def chat_with_tools(self, messages: List[dict],
                              tools: List[dict]
                              ) -> AsyncIterator[Tuple[str, Any]]:
        """带工具声明的流式对话。

        产出事件元组：
          ("content", 文本增量)      —— 正文片段，边收边出
          ("tool_calls", [ {...} ])  —— 本轮要调用的工具（在流末尾一次性给出），
                                        每项 {id, name, arguments}

        默认实现忽略 tools 直接走普通对话（保证不支持的后端也能跑）。
        """
        async for piece in self.chat(messages):
            yield ("content", piece)


class BaseASR(ABC):
    @abstractmethod
    def transcribe(self, pcm_s16le: bytes,
                   spans: list | None = None) -> str:
        """16k s16le PCM → 文本。

        spans: 可选的语音段列表 [(起始样本, 结束样本), ...]（由上层 VAD 采集）。
                给了就按停顿时长补标点；不给则用能量包络自查。"""
        raise NotImplementedError


class BaseTTS(ABC):
    @abstractmethod
    async def synthesize(self, text: str, style: dict | None = None
                         ) -> List[bytes]:
        """文本 → 24k opus 包列表。

        style: 可选的情绪/韵律参数 {"rate":1.0,"pitch":1.0,"volume":1.0}，
               由上层按语境实时决定，让语气更接近真人。"""
        raise NotImplementedError

    async def stream(self, text: str, style: dict | None = None
                     ) -> AsyncIterator[bytes]:
        """流式产出 opus 包（默认实现等价于一次性合成）。
        子类可覆盖，让首包更早发出，降低首句延迟。"""
        for p in await self.synthesize(text, style):
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
