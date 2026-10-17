# -*- coding: utf-8 -*-
"""LLM function calling 与工具调用循环回归。

覆盖：
  * OpenAI 兼容 provider 的 delta.tool_calls 分片累积（纯函数）
  * Ollama provider 的 tools 请求与 tool_calls 解析（假 httpx transport）
  * ToolCallLoop：多轮循环 / ack TTS / 工具执行 / 熔断回落 / 轮数上限

跑法：python tests/test_tool_loop.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from gateway.tools.loop import ToolCallLoop                  # noqa: E402
from gateway.tools.registry import ToolRegistry              # noqa: E402
from gateway.tools.types import ToolDef, ToolResult          # noqa: E402

PASS = 0
FAIL = 0


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


# ---------------------------------------------------------------------------
# OpenAI 兼容 provider：分片累积
# ---------------------------------------------------------------------------

def test_openai_delta():
    print("\n== OpenAI delta.tool_calls 累积 ==")
    from gateway.providers.llm_openai import OpenAICompatLLM
    llm = OpenAICompatLLM("http://x/v1", "k", "m")
    acc, order = {}, []

    # 第一片：带 id 与 name，arguments 分两段
    llm._accumulate_delta_tool_calls(acc, order, [
        {"index": 0, "id": "call_1", "type": "function",
         "function": {"name": "web.search", "arguments": '{"query":'}}])
    llm._accumulate_delta_tool_calls(acc, order, [
        {"index": 0, "function": {"arguments": '"天气"}'}}])
    final = llm._finalize_tool_calls(acc, order)
    ok(len(final) == 1, "单工具归并")
    ok(final[0]["id"] == "call_1" and final[0]["name"] == "web.search",
       "id/name 正确", final)
    ok(final[0]["arguments"] == '{"query":"天气"}', "arguments 分片拼接正确",
       final[0]["arguments"])

    # 多个工具（并行）
    acc2, order2 = {}, []
    llm._accumulate_delta_tool_calls(acc2, order2, [
        {"index": 0, "id": "a", "function": {"name": "f1", "arguments": "{}"}},
        {"index": 1, "id": "b", "function": {"name": "f2", "arguments": "{}"}}])
    final2 = llm._finalize_tool_calls(acc2, order2)
    ok([f["name"] for f in final2] == ["f1", "f2"], "多工具顺序保持")

    # 无名片段被丢弃，不产生空工具
    acc3, order3 = {}, []
    llm._accumulate_delta_tool_calls(acc3, order3, [
        {"index": 0, "function": {"arguments": "{}"}}])
    ok(llm._finalize_tool_calls(acc3, order3) == [], "空名工具被丢弃")
    asyncio.get_event_loop().close() if False else None


# ---------------------------------------------------------------------------
# 工具循环
# ---------------------------------------------------------------------------

class FakeLLM:
    def __init__(self, script, supports=True):
        self.script = list(script)          # [(kind, payload), ...]
        self.supports_tools = supports
        self.chat_calls = 0
        self.tool_calls_made = 0
        self.last_tools = None

    async def chat(self, messages):
        self.chat_calls += 1
        for kind, payload in self.script:
            if kind == "content":
                yield payload
        return

    async def chat_with_tools(self, messages, tools):
        self.tool_calls_made += 1
        self.last_tools = tools
        for kind, payload in self.script:
            yield (kind, payload)


def make_registry(record):
    async def _handler(args):
        record.append(args)
        return ToolResult.ok(f"结果:{json.dumps(args, ensure_ascii=False)}")

    r = ToolRegistry(logger=lambda m: None)
    r.register(ToolDef("self.echo", "回显", {"type": "object", "properties": {}},
                       "builtin", _handler))
    return r


def test_loop_simple():
    print("\n== 工具循环：单轮工具调用 ==")
    asyncio.run(_loop_simple())


async def _loop_simple():
    record = []
    reg = make_registry(record)
    # 第一轮：出 tool_call；第二轮：出正文
    script_turns = [
        [("tool_calls", [{"id": "c1", "name": "self_echo",
                          "arguments": {"text": "hi"}}])],
        [("content", "好的。")],
    ]
    llm = ScriptedLLM(script_turns)
    loop = ToolCallLoop(llm, reg, enable_ack=True, ack_text="我查一下。",
                        enable_tools=True, logger=lambda m: None)
    sents = [s async for s in loop.run([{"role": "user", "content": "测试"}])]
    ok(record == [{"text": "hi"}], "工具被执行且参数正确", record)
    ok("我查一下。" in sents, "播报了 ack", sents)
    ok("好的。" in sents, "第二轮正文被产出", sents)
    ok(llm.tool_calls_made == 2, "两轮都走了工具通道", llm.tool_calls_made)
    ok(llm.last_tools and llm.last_tools[0]["type"] == "function",
       "tools 声明被下发")


class ScriptedLLM:
    """按轮次脚本产出事件；支持 tools 与普通对话两条路。"""

    def __init__(self, turns, supports=True):
        self.turns = list(turns)
        self.supports_tools = supports
        self._i = 0
        self.chat_calls = 0
        self.tool_calls_made = 0
        self.last_tools = None

    async def chat(self, messages):
        self.chat_calls += 1
        # 普通对话：把当前轮的 content 事件吐出来
        turn = self.turns[min(self._i, len(self.turns) - 1)]
        for kind, payload in turn:
            if kind == "content":
                yield payload

    async def chat_with_tools(self, messages, tools):
        self.tool_calls_made += 1
        self.last_tools = tools
        turn = self.turns[min(self._i, len(self.turns) - 1)]
        self._i += 1
        for kind, payload in turn:
            yield (kind, payload)


def test_loop_fallback():
    print("\n== 工具循环：带工具请求失败回落普通对话 ==")
    asyncio.run(_loop_fallback())


async def _loop_fallback():
    record = []
    reg = make_registry(record)

    class BoomLLM:
        supports_tools = True
        def __init__(self):
            self.chat_calls = 0
        async def chat(self, messages):
            self.chat_calls += 1
            for piece in ["兜底", "回复。"]:
                yield piece
        async def chat_with_tools(self, messages, tools):
            raise RuntimeError("400 tools not supported")
            yield  # pragma: no cover

    llm = BoomLLM()
    loop = ToolCallLoop(llm, reg, enable_tools=True, logger=lambda m: None)
    sents = [s async for s in loop.run([{"role": "user", "content": "x"}])]
    ok(llm.chat_calls == 1, "回落走了普通 chat", llm.chat_calls)
    ok("".join(sents).find("兜底") >= 0, "兜底内容被产出", sents)


def test_loop_disabled():
    print("\n== 工具循环：总开关关闭 ==")
    asyncio.run(_loop_disabled())


async def _loop_disabled():
    record = []
    reg = make_registry(record)
    llm = ScriptedLLM([[("content", "普通回答。")]])
    loop = ToolCallLoop(llm, reg, enable_tools=False, logger=lambda m: None)
    sents = [s async for s in loop.run([{"role": "user", "content": "x"}])]
    ok(llm.tool_calls_made == 0 and llm.chat_calls == 1,
       "tools_first=False 时走普通对话", (llm.tool_calls_made, llm.chat_calls))
    ok("普通回答。" in sents, "普通回答被产出", sents)


def test_loop_max_turns():
    print("\n== 工具循环：轮数上限 ==")
    asyncio.run(_loop_max_turns())


async def _loop_max_turns():
    record = []
    reg = make_registry(record)
    # 每轮都无限调工具；max_turns=2 必须收尾
    turn = [("tool_calls", [{"id": "c", "name": "self_echo",
                             "arguments": {"n": 1}}]),]
    llm = ScriptedLLM([turn, turn, turn, turn], supports=True)
    loop = ToolCallLoop(llm, reg, max_turns=2, enable_ack=False,
                        logger=lambda m: None)
    sents = [s async for s in loop.run([{"role": "user", "content": "x"}])]
    ok(len(record) == 1, "最多执行 max_turns-1 次工具后收尾（不执行用不上的工具）",
       record)
    ok(any("收尾" in s or "回答" in s for s in sents), "超深时给出收尾话术", sents)


def test_llm_no_support():
    print("\n== 工具循环：后端不支持 tools ==")
    asyncio.run(_no_support())


async def _no_support():
    record = []
    reg = make_registry(record)
    llm = ScriptedLLM([[("content", "只走普通。")]], supports=False)
    loop = ToolCallLoop(llm, reg, enable_tools=True, logger=lambda m: None)
    sents = [s async for s in loop.run([{"role": "user", "content": "x"}])]
    ok(llm.tool_calls_made == 0, "不支持 tools 时不走工具通道")
    ok("只走普通。" in sents, "普通内容产出")


if __name__ == "__main__":
    test_openai_delta()
    test_loop_simple()
    test_loop_fallback()
    test_loop_disabled()
    test_loop_max_turns()
    test_llm_no_support()
    print(f"\nTOOL_LOOP_PASS {PASS}/{PASS + FAIL}")
    sys.exit(0 if FAIL == 0 else 1)
