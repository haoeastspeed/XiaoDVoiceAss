# -*- coding: utf-8 -*-
"""端到端集成冒烟：真实 VoiceSession + MCP 握手 + 工具注册 + 一轮对话。

用 QueueTransport 当"假对端"，不连真机、不加载模型、不发网络请求
（预热音色表被替换成空实现）。

跑法：python tests/smoke_mcp_toolcall.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import gateway.session as session_mod                              # noqa: E402
from gateway.config import GatewayConfig                          # noqa: E402
from gateway.transport.base import QueueTransport                  # noqa: E402

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


# ---- 假件 ----

class FakeLLM:
    supports_tools = True
    model = "fake"

    async def chat(self, messages):
        for piece in ["你好", "呀。"]:
            yield piece

    async def chat_with_tools(self, messages, tools):
        yield ("content", "好的")
        yield ("content", "。")

    async def quick(self, prompt, max_tokens=32, timeout=10.0):
        return ""


class FakeASR:
    def transcribe(self, pcm, spans=None):
        return "你好"


class FakeTTS:
    def __init__(self):
        self.voice = "zh-CN-XiaoxiaoNeural"

    def set_voice(self, v):
        self.voice = v

    async def synthesize(self, text, style=None):
        return [b"\x00" * 8]

    async def stream(self, text, style=None):
        yield b"\x00" * 8


class FakeVAD:
    def new_state(self):
        return {}

    def reset_sentence(self, st):
        pass

    def process(self, st, pcm):
        return (False, False)


def _sent_json(transport, type_name):
    out = []
    for item in transport.sent:
        try:
            m = json.loads(item)
        except Exception:  # noqa: BLE001
            continue
        if m.get("type") == type_name:
            out.append(m)
    return out


async def run():
    # 预热音色表会走网络 → 替换掉
    session_mod.official_voices = _noop
    cfg = GatewayConfig()
    cfg.echo_cancel = "off"
    cfg.mcp_enabled = True
    cfg.tools_first = False

    transport = QueueTransport()
    session = session_mod.VoiceSession(transport, cfg, FakeLLM(), FakeASR(),
                                       FakeTTS(), FakeVAD())
    task = asyncio.create_task(session.run())
    try:
        # ---- 1. hello：声明 features.mcp + version=2 ----
        transport.feed(json.dumps({
            "type": "hello", "version": 2,
            "features": {"mcp": True, "emoji": False},
            "audio_params": {"format": "opus", "sample_rate": 24000},
        }))
        await _wait(lambda: len(_sent_json(transport, "hello")) >= 1)
        hello = _sent_json(transport, "hello")[0]
        ok(hello.get("version") == 2, "hello 回显协商后的版本 2", hello)
        ok(hello.get("session_id"), "hello 带 session_id")
        ok(session._codec is not None and session._codec.version == 2,
           "会话按 v2 建立帧编解码器")

        # ---- 2. 网关主动发 MCP initialize ----
        await _wait(lambda: _mcp_with_method(transport, "initialize"))
        init = _mcp_with_method(transport, "initialize")
        ok(init is not None, "收到 MCP initialize 请求")
        ok(init["payload"].get("id") == 1, "initialize 固定 id=1")
        ok("clientInfo" in init["payload"].get("params", {}),
           "initialize 带 clientInfo")

        # ---- 3. 回 serverInfo → 期望 tools/list ----
        transport.feed(json.dumps({"type": "mcp", "payload": {
            "jsonrpc": "2.0", "id": 1,
            "result": {"serverInfo": {"name": "FakePeer", "version": "1.0"},
                       "protocolVersion": "2024-11-05",
                       "capabilities": {"tools": {}}}}}))
        await _wait(lambda: _mcp_with_method(transport, "tools/list"))
        ok(_mcp_with_method(transport, "tools/list") is not None,
           "收到 tools/list 请求")

        # ---- 4. 回工具列表 → 注册进统一工具表 ----
        transport.feed(json.dumps({"type": "mcp", "payload": {
            "jsonrpc": "2.0", "id": 2,
            "result": {"tools": [
                {"name": "self.audio_speaker.set_volume",
                 "description": "设置音量",
                 "inputSchema": {"type": "object",
                                 "properties": {"volume": {"type": "number"}},
                                 "required": ["volume"]}},
                {"name": "self.get_device_status", "description": "状态",
                 "inputSchema": {"type": "object", "properties": {}}},
            ]}}}))
        await _wait(lambda: session.tools.has(
            "self_audio_speaker_set_volume"))
        ok(session.tools.has("self_audio_speaker_set_volume"),
           "设备工具已 sanitize 注册")
        ok(session.tools.has("self_get_device_status"), "第二个设备工具也在")
        ok(session.tools.has("self_gateway_get_status"), "网关内置工具共存")
        srcs = session.tools.sources()
        ok(srcs.get("device") == 2 and srcs.get("builtin", 0) >= 9,
           "来源统计正确（device=2, builtin≥9）", srcs)

        # ---- 5. 通过注册表调用设备工具（走 tools/call） ----
        tool = session.tools.get("self_audio_speaker_set_volume")
        call_task = asyncio.create_task(tool.handler({"volume": 50}))
        await _wait(lambda: _mcp_with_method(transport, "tools/call"))
        call_env = _mcp_with_method(transport, "tools/call")
        ok(call_env["payload"]["params"]["name"] == "self.audio_speaker.set_volume",
           "tools/call 用对端原名", call_env["payload"]["params"])
        ok(call_env["payload"]["params"]["arguments"] == {"volume": 50},
           "参数原样传递")
        cid = call_env["payload"]["id"]
        ok(cid >= 100, "工具调用 id 避开 1/2（≥100）", cid)
        transport.feed(json.dumps({"type": "mcp", "payload": {
            "jsonrpc": "2.0", "id": cid,
            "result": {"content": [{"type": "text", "text": "true"}],
                       "isError": False}}}))
        res = await asyncio.wait_for(call_task, 5.0)
        ok(not res.is_error and res.text == "true", "工具结果正确回填", res)

        # ---- 6. 一轮对话（tools_first=False → 普通对话路径）----
        n_before = len(_sent_json(transport, "tts"))
        transport.feed(json.dumps({"type": "listen", "state": "detect",
                                   "text": "你好"}))
        await _wait(lambda: len(_sent_json(transport, "tts")) > n_before)
        kinds = [m["state"] for m in _sent_json(transport, "tts")]
        ok("start" in kinds and "stop" in kinds, "走完 tts start→stop", kinds)
        ok(len(_sent_json(transport, "stt")) >= 1, "回发了 stt 字幕")
    finally:
        task.cancel()
        try:
            await task
        except Exception:  # noqa: BLE001
            pass


async def _noop():
    return None


def _mcp_with_method(transport, method):
    for item in transport.sent:
        try:
            m = json.loads(item)
        except Exception:  # noqa: BLE001
            continue
        if m.get("type") == "mcp" and \
                (m.get("payload") or {}).get("method") == method:
            return m
    return None


async def _wait(pred, timeout=5.0):
    import time
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


if __name__ == "__main__":
    asyncio.run(run())
    print(f"\nSMOKE_MCP_PASS {PASS}/{PASS + FAIL}")
    sys.exit(0 if FAIL == 0 else 1)
