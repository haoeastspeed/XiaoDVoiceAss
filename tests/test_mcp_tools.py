# -*- coding: utf-8 -*-
"""MCP 工具生态回归：注册表 / 设备侧 MCP / endpoint / 内置工具 / 工具循环。

全部用假件驱动（假对端、假 stdio server、假 session），不连真机、不加载模型，
秒级可跑。

跑法：python tests/test_mcp_tools.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from gateway.tools.device import DeviceMCPClient            # noqa: E402
from gateway.tools.endpoint import MCPEndpointManager       # noqa: E402
from gateway.tools.registry import ToolRegistry             # noqa: E402
from gateway.tools.types import (ToolDef, ToolResult,       # noqa: E402
                                 sanitize_tool_name)

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
# 注册表
# ---------------------------------------------------------------------------

def test_registry():
    print("\n== 工具注册表 ==")
    r = ToolRegistry(logger=lambda m: None)

    async def _h(args):
        return ToolResult.ok("x")

    t1 = ToolDef("self.a", "A", source="builtin", handler=_h)
    t2 = ToolDef("self.b", "B", source="device", handler=_h)
    ok(r.register(t1), "注册 self.a")
    ok(r.register(t2), "注册 self.b")
    ok(not r.register(ToolDef("self.a", "A2", handler=_h)),
       "重名注册被拒（保留先注册者）")
    ok(len(r) == 2, "工具数 = 2", f"len={len(r)}")
    # 名字注册时被 sanitize：self.a → self_a
    ok(r.has("self_a") and r.get("self_a").description == "A", "get/has 正确")
    ok(len(r.openai_functions()) == 2, "openai_functions 数量正确")
    ok(r.openai_functions()[0]["type"] == "function", "OpenAI function 结构")

    r.register(ToolDef("ep1.toolX", "X", source="endpoint:ep1", handler=_h))
    r.register(ToolDef("ep1.toolY", "Y", source="endpoint:ep1", handler=_h))
    ok(r.remove_by_prefix("ep1_") == 2, "按前缀注销 endpoint 工具")
    ok(len(r) == 2, "注销后数量回落到 2")
    ok(sorted(r.sources().keys()) == ["builtin", "device"], "来源统计正确")

    # 名字清洗：点号名一律 sanitize（OpenAI function name 合法性）
    r2 = ToolRegistry(logger=lambda m: None)
    r2.register(ToolDef("self.gateway.set_volume", "V", handler=_h))
    ok(r2.has("self_gateway_set_volume"),
       "注册时自动 sanitize 点号名")
    ok(r2.openai_functions()[0]["function"]["name"] == "self_gateway_set_volume",
       "openai_functions 名字合法")
    r2.register(ToolDef("self.other", "O", source="device", handler=_h))
    ok(r2.remove_by_source("device") == 1
       and r2.has("self_gateway_set_volume"), "按来源注销只清对应来源")


def test_sanitize():
    print("\n== 工具名清洗 ==")
    ok(sanitize_tool_name("self.audio_speaker.set_volume")
       == "self_audio_speaker_set_volume", "点号 → 下划线")
    ok(sanitize_tool_name("中文工具") == "中文工具", "中文保留")
    ok(sanitize_tool_name("a b/c") == "a_b_c", "空格与斜杠 → 下划线")
    # OpenAI function name 合法性：只含 [a-zA-Z0-9_-] 或中文
    import re
    s = sanitize_tool_name("weird!@#name中文-ok")
    ok(re.fullmatch(r"[a-zA-Z0-9_\-\u4e00-\u9fff]+", s) is not None,
       "清洗结果合法", s)


# ---------------------------------------------------------------------------
# 设备侧 MCP（假对端）
# ---------------------------------------------------------------------------

class FakePeer:
    """假设备：收到 initialize 回 serverInfo，tools/list 回工具，tools/call 回结果。"""

    def __init__(self, tools=None, cursor_pages=False):
        self.sent = []                 # 收到的信封
        self.tools = tools if tools is not None else [
            {"name": "self.echo", "description": "回显",
             "inputSchema": {"type": "object", "properties":
                             {"text": {"type": "string"}}, "required": ["text"]}},
        ]
        self.cursor_pages = cursor_pages
        self._page = 0

    async def __call__(self, env: dict) -> bool:
        self.sent.append(env)
        payload = env.get("payload") or {}
        rid = payload.get("id")
        method = payload.get("method")
        if method == "initialize":
            await self._reply({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "FakePeer", "version": "9.9"}}})
        elif method == "tools/list":
            cursor = (payload.get("params") or {}).get("cursor", "")
            if self.cursor_pages and not cursor:
                await self._reply({"jsonrpc": "2.0", "id": rid, "result": {
                    "tools": self.tools[:1], "nextCursor": "page2"}})
            elif self.cursor_pages:
                await self._reply({"jsonrpc": "2.0", "id": rid, "result": {
                    "tools": self.tools[1:], "nextCursor": ""}})
            else:
                await self._reply({"jsonrpc": "2.0", "id": rid, "result": {
                    "tools": self.tools}})
        elif method == "tools/call":
            name = (payload.get("params") or {}).get("name")
            args = (payload.get("params") or {}).get("arguments") or {}
            await self._reply({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text",
                             "text": f"called {name} with {json.dumps(args, ensure_ascii=False)}"}],
                "isError": False}})
        return True

    async def _reply(self, payload: dict):
        # 异步回包，模拟网络往返
        await asyncio.sleep(0)
        await self.client.handle_message(payload)


def test_device_client():
    print("\n== 设备侧 MCP ==")
    asyncio.run(_device_flow())
    asyncio.run(_device_paging())


async def _device_flow():
    peer = FakePeer()
    client = DeviceMCPClient(peer, logger=lambda m: None)
    peer.client = client
    await client.start()
    # 等 initialize 响应 → tools/list → 工具就绪（含官方 0.2s 延迟）
    for _ in range(50):
        if client.ready:
            break
        await asyncio.sleep(0.05)
    ok(client.ready, "设备 MCP 就绪")
    ok(client.server_info.get("name") == "FakePeer", "收到 serverInfo")
    ok(client.has_tool("self_echo"), "工具名已 sanitize（self.echo → self_echo）")
    ok(client.name_mapping.get("self_echo") == "self.echo", "name_mapping 还原原名")

    res = await client.call("self_echo", {"text": "你好"})
    ok(not res.is_error and "self.echo" in res.text, "工具调用成功（用原名发送）",
       res.text)
    # 参数以 JSON 字符串给（模拟 OpenAI tool_calls）也能跑
    res2 = await client.call("self_echo", '{"text": "hi"}')
    ok(not res2.is_error and "hi" in res2.text, "参数 JSON 字符串可解析", res2.text)

    # 未知工具
    res3 = await client.call("self_nope", {})
    ok(res3.is_error, "未知工具返回错误")

    defs = client.tool_defs()
    ok(len(defs) == 1 and defs[0].name == "self_echo"
       and defs[0].source == "device", "tool_defs 结构正确")


async def _device_paging():
    peer = FakePeer(tools=[
        {"name": "t.one", "description": "1", "inputSchema":
         {"type": "object", "properties": {}}},
        {"name": "t.two", "description": "2", "inputSchema":
         {"type": "object", "properties": {}}},
    ], cursor_pages=True)
    client = DeviceMCPClient(peer, logger=lambda m: None)
    peer.client = client
    await client.start()
    for _ in range(50):
        if client.ready:
            break
        await asyncio.sleep(0.05)
    ok(len(client.tools) == 2, "nextCursor 分页完整拉取 2 个工具",
       f"got={list(client.tools)}")


# ---------------------------------------------------------------------------
# MCP endpoint（本地 stdio 假 server）
# ---------------------------------------------------------------------------

_FAKE_SERVER = r'''
import sys, json
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try: msg = json.loads(line)
    except Exception: continue
    m = msg.get("method"); rid = msg.get("id")
    if m == "initialize":
        send({"jsonrpc":"2.0","id":rid,"result":{"serverInfo":{"name":"FakeStdio"}}})
    elif m == "tools/list":
        send({"jsonrpc":"2.0","id":rid,"result":{"tools":[
            {"name":"greet","description":"打招呼",
             "inputSchema":{"type":"object","properties":{"who":{"type":"string"}},
                            "required":["who"]}}]}})
    elif m == "tools/call":
        who = (msg.get("params") or {}).get("arguments",{}).get("who","?")
        send({"jsonrpc":"2.0","id":rid,"result":{
            "content":[{"type":"text","text":"hello "+str(who)}],"isError":False}})
    else:
        send({"jsonrpc":"2.0","id":rid,"error":{"code":-32601,"message":"no method"}})
'''


def test_endpoint_stdio():
    print("\n== MCP endpoint（stdio）==")
    asyncio.run(_endpoint_flow())


async def _endpoint_flow():
    fd, path = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_FAKE_SERVER)
    try:
        mgr = MCPEndpointManager({
            "fake": {"command": sys.executable, "args": [path]},
        }, logger=lambda m: None)
        defs = await mgr.start()
        names = [d.name for d in defs]
        ok(len(defs) == 1, "endpoint 拉到 1 个工具", f"names={names}")
        ok(defs and defs[0].name == "fake_greet", "工具名带 endpoint 前缀",
           f"names={names}")
        res = await defs[0].handler({"who": "世界"})
        ok(not res.is_error and "hello 世界" in res.text,
           "endpoint 工具可调用", res.text)
        await mgr.close()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    # 连接失败（命令不存在）→ 不抛异常、返回空
    mgr2 = MCPEndpointManager({
        "bad": {"command": "definitely-not-a-real-cmd-xyz"},
    }, logger=lambda m: None)
    defs2 = await mgr2.start()
    ok(defs2 == [], "endpoint 失败只禁用、不抛异常")


# ---------------------------------------------------------------------------
# 内置工具（假 session）
# ---------------------------------------------------------------------------

class FakeTTS:
    def __init__(self):
        self.voice = "zh-CN-XiaoxiaoNeural"
        self.engine = "auto"

    def set_voice(self, v):
        self.voice = v

    def set_engine(self, e):
        self.engine = e


class FakeLLM:
    model = "qwen3.5:9b-q4km"


class FakeSession:
    def __init__(self):
        from gateway.config import GatewayConfig
        self.config = GatewayConfig()
        self.tts = FakeTTS()
        self.llm = FakeLLM()
        self.messages = [{"role": "system", "content": "p"},
                         {"role": "user", "content": "hi"},
                         {"role": "assistant", "content": "yo"}]
        self.voice = "zh-CN-XiaoxiaoNeural"
        self._summary = "摘要"
        self._sleeping = False
        self.state = "idle"
        self.custom = []

    async def _send(self, data):
        return True

    async def _send_custom(self, action, **fields):
        self.custom.append((action, fields))


def test_builtin():
    print("\n== 内置工具 self.gateway.* ==")
    asyncio.run(_builtin_flow())


async def _builtin_flow():
    from gateway.tools.builtin import build_builtin_tools
    s = FakeSession()
    tools = {t.name: t for t in build_builtin_tools(s)}
    expect = {"self.gateway.get_status", "self.gateway.set_volume",
              "self.gateway.set_voice", "self.gateway.set_tts_engine",
              "self.gateway.set_search", "self.gateway.set_model",
              "self.gateway.set_memory", "self.gateway.clear_history",
              "self.gateway.sleep"}
    ok(expect.issubset(set(tools)), "内置工具齐全", f"missing={expect - set(tools)}")

    r = await tools["self.gateway.get_status"].handler({})
    ok(not r.is_error and "qwen3.5" in r.text, "get_status 返回模型信息", r.text)

    r = await tools["self.gateway.set_search"].handler({"enabled": True})
    ok(not r.is_error and s.config.web_search is True, "set_search 生效")

    r = await tools["self.gateway.set_voice"].handler(
        {"voice": "zh-CN-YunxiNeural"})
    ok(not r.is_error and s.tts.voice == "zh-CN-YunxiNeural", "set_voice 生效")

    r = await tools["self.gateway.set_voice"].handler({"voice": "nope"})
    ok(r.is_error, "未知音色返回错误")

    r = await tools["self.gateway.set_tts_engine"].handler({"engine": "local"})
    ok(not r.is_error and s.tts.engine == "local", "set_tts_engine 生效")

    r = await tools["self.gateway.set_memory"].handler({"mode": "summary"})
    ok(not r.is_error and s.config.memory_mode == "summary", "set_memory 生效")

    r = await tools["self.gateway.clear_history"].handler({})
    ok(not r.is_error and len(s.messages) == 1, "clear_history 保留人设")

    r = await tools["self.gateway.set_volume"].handler({"volume": 0.8})
    ok(not r.is_error and s.custom and s.custom[-1][0] == "set_volume",
       "set_volume 发 custom 消息给客户端")

    r = await tools["self.gateway.sleep"].handler({})
    ok(not r.is_error and s._sleeping is True, "sleep 生效")


if __name__ == "__main__":
    test_registry()
    test_sanitize()
    test_device_client()
    test_endpoint_stdio()
    test_builtin()
    print(f"\nMCP_TOOLS_PASS {PASS}/{PASS + FAIL}")
    sys.exit(0 if FAIL == 0 else 1)
