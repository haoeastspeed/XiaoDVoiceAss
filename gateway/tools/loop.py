# -*- coding: utf-8 -*-
"""工具调用循环：多轮 tool call + 工具执行期间的 TTS 反馈。

接口刻意与 session 里原来的 `_iter_sentences()` 一致（产出句子字符串），
这样替换时 session 的改动最小。

流程（对齐官方 core/connection.py 的 response_with_functions 递归）：
  1. 带 tools 请求大模型，流式产出正文句子；
  2. 若本轮产生了 tool_calls：先播一句"好的，我查一下"（可关），并发执行工具；
  3. 把 assistant(tool_calls) 与 role=tool 的结果回填进 messages，进入下一轮；
  4. 轮数超过上限（tool_loop_max_turns）就用已得内容收尾，避免死循环。

降级策略（关键，保证不回归）：
  * 大模型不支持 tools（supports_tools=False）→ 直接走普通 chat；
  * 工具注册表为空 → 直接走普通 chat；
  * 带 tools 的请求抛异常（旧模型 400 等）→ 本轮回落普通 chat，并置
    `supports_tools=False` 熔断，后续不再尝试。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from gateway.tools.registry import ToolRegistry

_DEFAULT_ACK = "好的，我查一下。"
_FAIL_LIMIT = 3


class ToolCallLoop:
    def __init__(self, llm, registry: ToolRegistry, *,
                 max_turns: int = 4,
                 ack_text: str = _DEFAULT_ACK,
                 enable_ack: bool = True,
                 enable_tools: bool = True,
                 tool_timeout: float = 30.0,
                 logger=None):
        self.llm = llm
        self.registry = registry
        self.max_turns = max(1, int(max_turns or 4))
        self.ack_text = ack_text
        self.enable_ack = enable_ack
        # 总开关：False 时不把 tools 交给大模型，完全退回普通对话（=改造前行为）
        self.enable_tools = enable_tools
        self.tool_timeout = tool_timeout
        self._log = logger or (lambda m: print(f"[tool-loop] {m}", flush=True))
        self._consecutive_fail = 0
        # 熔断只在**本会话**生效：早期实现直接改全局 LLM 的 supports_tools，
        # 一个会话失败 3 次会把进程内所有会话的工具能力永久关掉（要重启
        # 网关才恢复），而且"工具执行失败"也会被算进通道失败。
        self._tools_disabled = False

    # ---------- 对外主入口 ----------

    async def run(self, messages: List[dict]) -> AsyncIterator[str]:
        from gateway.session import split_sentence   # 延迟导入，避免循环依赖

        turns = 0
        while True:
            buf = ""
            tool_calls: List[dict] = []
            got_content = False
            try:
                async for kind, val in self._stream(messages):
                    if kind == "content":
                        buf += val
                        while True:
                            split = split_sentence(buf)
                            if split is None:
                                break
                            sent, buf = split
                            if sent.strip():
                                got_content = True
                                yield sent
                    elif kind == "tool_calls":
                        tool_calls = val or []
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                # 带 tools 请求失败 → 熔断并回落普通 chat
                self._consecutive_fail += 1
                self._log(f"带工具请求失败（{type(e).__name__}: {e}），"
                          f"回落普通对话")
                if self._consecutive_fail >= _FAIL_LIMIT:
                    # 只禁**本会话**，不动全局 LLM：别的会话/设备不受影响，
                    # 也不需要重启网关才能恢复。
                    self._tools_disabled = True
                    self._log("工具通道连续失败，本会话回退普通对话")
                async for sent in self._fallback(messages, buf, got_content):
                    yield sent
                return
            if buf.strip():
                if not got_content:
                    pass
                got_content = True
                yield buf

            if not tool_calls:
                return

            turns += 1
            if turns >= self.max_turns:
                self._log(f"工具轮数达上限 {self.max_turns}，收尾")
                yield "我先按现在掌握的这些来回答。"
                return

            # 先起工具执行任务，再报一句 ack：消费方念 ack 的同时工具在跑
            tasks = [asyncio.create_task(self._exec_one(tc)) for tc in tool_calls]
            if self.enable_ack and not got_content:
                yield self.ack_text
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 回填 assistant(tool_calls) + 每条 tool 结果
            messages.append(_assistant_tool_call_msg(tool_calls))
            for tc, res in zip(tool_calls, results):
                if isinstance(res, Exception):
                    text = f"工具执行异常：{res}"
                else:
                    text = res.text
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "name": tc.get("name", ""),
                    "content": text or "(无输出)",
                })
            # 循环下一轮，让模型基于工具结果继续说

    # ---------- 内部 ----------

    def _use_tools(self) -> bool:
        if not self.enable_tools or self._tools_disabled:
            return False
        if not self.registry or len(self.registry) == 0:
            return False
        if not getattr(self.llm, "supports_tools", False):
            return False
        if not hasattr(self.llm, "chat_with_tools"):
            return False
        return True

    async def _stream(self, messages) -> AsyncIterator[Tuple[str, Any]]:
        if self._use_tools():
            tools = self.registry.openai_functions()
            async for ev in self.llm.chat_with_tools(messages, tools):
                yield ev
        else:
            async for piece in self.llm.chat(messages):
                yield ("content", piece)

    async def _fallback(self, messages, pre_buf: str,
                        already: bool) -> AsyncIterator[str]:
        """工具通道失败时，用普通 chat 出这一轮的话。"""
        from gateway.session import split_sentence
        buf = pre_buf or ""
        if buf.strip() and not already:
            yield buf
            buf = ""
        try:
            async for piece in self.llm.chat(messages):
                buf += piece
                while True:
                    split = split_sentence(buf)
                    if split is None:
                        break
                    sent, buf = split
                    if sent.strip():
                        yield sent
        except Exception as e:  # noqa: BLE001
            self._log(f"普通对话兜底也失败：{type(e).__name__}: {e}")
        if buf.strip():
            yield buf

    async def _exec_one(self, tc: dict):
        from gateway.tools.types import ToolResult
        name = tc.get("name", "")
        tool = self.registry.get(name)
        if tool is None or tool.handler is None:
            # 未知工具是"模型给了个不存在的名字"，不是通道故障：不计入熔断，
            # 否则几次工具名笔误就把整会话的工具能力关掉。
            return ToolResult.error(f"未知工具：{name}")
        try:
            res = await asyncio.wait_for(tool.handler(tc.get("arguments")),
                                         timeout=self.tool_timeout)
        except asyncio.TimeoutError:
            # 工具自身超时 ≠ 通道故障，不计入熔断
            return ToolResult.error(f"工具 {name} 执行超时")
        except Exception as e:  # noqa: BLE001
            # 工具执行异常 ≠ 通道故障，不计入熔断
            return ToolResult.error(f"工具 {name} 执行失败：{e}")
        return res


def _assistant_tool_call_msg(tool_calls: List[dict]) -> dict:
    """构造回填用的 assistant 消息（带 tool_calls）。"""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": tc.get("id", ""),
                "type": "function",
                "function": {
                    "name": tc.get("name", ""),
                    "arguments": (tc.get("arguments") if isinstance(
                        tc.get("arguments"), str)
                        else json.dumps(tc.get("arguments") or {},
                                        ensure_ascii=False)),
                },
            }
            for tc in tool_calls
        ],
    }


def _disable_tools(llm):
    """连续失败熔断：关掉该 LLM 实例的 tools 能力（本进程内）。"""
    try:
        llm.supports_tools = False
    except Exception:  # noqa: BLE001
        pass
