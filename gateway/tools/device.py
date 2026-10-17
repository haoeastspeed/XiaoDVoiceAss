# -*- coding: utf-8 -*-
"""设备侧 MCP 客户端（对齐官方 core/providers/tools/device_mcp）。

网关在这里扮演 **MCP 客户端（client）**，把对端（PC 客户端 / ESP32 固件）
当作 **MCP server**：先 initialize，再 tools/list（支持 nextCursor 分页），
之后 tools/call 执行对端暴露的工具。

协议要点（见 docs/mcp-protocol_zh.md）：
  * 每条 MCP 消息都包在基础协议的 {"type":"mcp","payload":<JSON-RPC 2.0>} 里
  * initialize  固定 id=1，tools/list 固定 id=2（分页继续也用 2）
  * tools/call  用自增 id（从 FIRST_CALL_ID 起，避开 1/2），响应靠 id 配对 Future
  * 工具名先 sanitize 后交给大模型，调用时用 name_mapping 还原成对端原名
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from gateway.tools.types import ToolDef, ToolResult, empty_schema, sanitize_tool_name

# 固定请求 ID（对齐官方常量）
_INIT_ID = 1
_LIST_ID = 2
# 工具调用从 100 起：避开 initialize / tools-list 占用的 1/2，避免 id 混淆
_FIRST_CALL_ID = 100
_DEFAULT_TIMEOUT = 30.0


class DeviceMCPClient:
    """管理一次会话中"对端设备"的 MCP 工具。"""

    def __init__(
        self,
        send: Callable[[dict], Awaitable[bool]],
        *,
        on_tools_changed: Optional[Callable[[], None]] = None,
        logger: Optional[Callable[[str], None]] = None,
    ):
        # send 接收完整的信封 {"type":"mcp","payload":{...}} 并发出去
        self._send = send
        self._on_changed = on_tools_changed
        self._log = logger or (lambda m: print(f"[mcp] {m}", flush=True))

        self.tools: Dict[str, dict] = {}          # sanitized -> {name,description,inputSchema}
        self.name_mapping: Dict[str, str] = {}    # sanitized -> 对端原名
        self.server_info: Dict[str, Any] = {}
        self.ready = False
        self._call_results: Dict[int, asyncio.Future] = {}
        self._next_id = _FIRST_CALL_ID
        self._warned_args = False

    # ---------- 生命周期 ----------

    async def start(self, vision: Optional[dict] = None):
        """发送 initialize（对齐官方 send_mcp_initialize_message）。

        vision: {"url": "http://.../mcp/vision/explain", "token": "..."} ——
                支持摄像头的设备据此知道把照片 POST 到哪里。"""
        caps: Dict[str, Any] = {
            "roots": {"listChanged": True},
            "sampling": {},
        }
        if vision:
            caps["vision"] = vision
        await self._send_payload({
            "jsonrpc": "2.0",
            "id": _INIT_ID,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": caps,
                "clientInfo": {"name": "XiaoDGateway", "version": "1.0.0"},
            },
        })

    async def close(self):
        """取消所有在途调用。"""
        for fut in list(self._call_results.values()):
            if not fut.done():
                fut.cancel()
        self._call_results.clear()

    # ---------- 发送 ----------

    async def _send_payload(self, payload: dict):
        env = {"type": "mcp", "payload": payload}
        try:
            await self._send(env)
        except Exception as e:  # noqa: BLE001
            self._log(f"发送 MCP 消息失败：{type(e).__name__}: {e}")

    async def _next_call_id(self) -> int:
        cid = self._next_id
        self._next_id += 1
        return cid

    # ---------- 接收分派（对齐官方 handle_mcp_message）----------

    async def handle_message(self, payload: dict):
        if not isinstance(payload, dict):
            return
        if "result" in payload:
            await self._handle_result(payload)
        elif "error" in payload:
            await self._handle_error(payload)
        # method（对端发来的 request）/ notification：本网关暂不处理，静默忽略

    async def _handle_result(self, payload: dict):
        result = payload.get("result")
        try:
            msg_id = int(payload.get("id", 0))
        except (TypeError, ValueError):
            return

        # 工具调用响应优先（id 命中在途 Future）
        if msg_id in self._call_results:
            fut = self._call_results.pop(msg_id)
            if not fut.done():
                fut.set_result(result)
            return

        if msg_id == _INIT_ID:
            if isinstance(result, dict):
                self.server_info = result.get("serverInfo") or {}
                self._log(f"设备 MCP 初始化完成："
                          f"{self.server_info.get('name', '?')} "
                          f"{self.server_info.get('version', '')}")
            await asyncio.sleep(0.2)   # 官方是 sleep(1)，这里压缩到 200ms
            await self._send_payload({
                "jsonrpc": "2.0", "id": _LIST_ID, "method": "tools/list",
            })
            return

        if msg_id == _LIST_ID:
            await self._handle_tools_list(result)
            return

    async def _handle_tools_list(self, result: Any):
        if not isinstance(result, dict) or "tools" not in result:
            self._log("tools/list 响应格式错误，跳过")
            return
        tools_data = result.get("tools")
        if not isinstance(tools_data, list):
            return

        for tool in tools_data:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name", "") or ""
            if not name:
                continue
            schema = empty_schema()
            ins = tool.get("inputSchema")
            if isinstance(ins, dict):
                schema["type"] = ins.get("type", "object") or "object"
                schema["properties"] = ins.get("properties", {}) or {}
                schema["required"] = [s for s in (ins.get("required") or [])
                                      if isinstance(s, str)]
            sanitized = sanitize_tool_name(name)
            self.tools[sanitized] = {
                "name": sanitized,
                "description": tool.get("description", "") or "",
                "inputSchema": schema,
            }
            self.name_mapping[sanitized] = name

        # 描述文本里出现的原名也换成 sanitize 名（对齐官方，避免大模型照描述里的
        # 原名去调用而匹配不上）
        for data in self.tools.values():
            desc = data.get("description") or ""
            for sanitized, original in self.name_mapping.items():
                if original and original != sanitized:
                    desc = desc.replace(original, sanitized)
            data["description"] = desc

        next_cursor = result.get("nextCursor", "")
        if next_cursor:
            await self._send_payload({
                "jsonrpc": "2.0", "id": _LIST_ID, "method": "tools/list",
                "params": {"cursor": next_cursor},
            })
            return

        self.ready = True
        self._log(f"设备 MCP 就绪：{len(self.tools)} 个工具 "
                  f"{sorted(self.tools.keys())[:6]}")
        if self._on_changed:
            try:
                self._on_changed()
            except Exception as e:  # noqa: BLE001
                self._log(f"工具变更回调异常：{type(e).__name__}: {e}")

    async def _handle_error(self, payload: dict):
        err = payload.get("error") or {}
        msg = err.get("message", "未知错误") if isinstance(err, dict) else str(err)
        try:
            msg_id = int(payload.get("id", 0))
        except (TypeError, ValueError):
            return
        if msg_id in self._call_results:
            fut = self._call_results.pop(msg_id)
            if not fut.done():
                fut.set_exception(RuntimeError(f"MCP 错误：{msg}"))
        else:
            self._log(f"设备返回 MCP 错误：{msg}")

    # ---------- 调用 ----------

    def has_tool(self, sanitized_name: str) -> bool:
        return sanitized_name in self.tools

    def available_tools(self) -> List[dict]:
        """转成 OpenAI function 列表（对齐官方 get_available_tools）。"""
        out = []
        for name, data in self.tools.items():
            schema = data.get("inputSchema") or empty_schema()
            out.append({"type": "function", "function": {
                "name": name,
                "description": data.get("description", ""),
                "parameters": {
                    "type": schema.get("type", "object"),
                    "properties": schema.get("properties", {}) or {},
                    "required": schema.get("required", []) or [],
                },
            }})
        return out

    def tool_defs(self) -> List[ToolDef]:
        """转成统一 ToolDef（handler 闭包调用对端），供注册表使用。"""
        defs: List[ToolDef] = []
        for name, data in self.tools.items():
            def _make(n: str, d: dict):
                async def _handler(args: Dict[str, Any]) -> ToolResult:
                    return await self.call(n, args)
                return _handler
            defs.append(ToolDef(
                name=name,
                description=data.get("description", ""),
                input_schema=data.get("inputSchema") or empty_schema(),
                source="device",
                handler=_make(name, data),
                original=self.name_mapping.get(name, name),
            ))
        return defs

    @staticmethod
    def _normalize_args(args: Any) -> Dict[str, Any]:
        """把模型给的参数规整成 dict（对齐官方 call_mcp_tool 的清洗逻辑）。"""
        if args is None:
            return {}
        if isinstance(args, dict):
            return args
        if isinstance(args, str):
            s = args.strip()
            if not s:
                return {}
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
            # 模型偶尔会吐出多个 JSON 片段，合并它们（官方同款策略）
            merged: Dict[str, Any] = {}
            for frag in re.findall(r"\{[^{}]*\}", s):
                try:
                    o = json.loads(frag)
                    if isinstance(o, dict):
                        merged.update(o)
                except json.JSONDecodeError:
                    continue
            return merged
        return {}

    async def call(self, sanitized_name: str, args: Any = None,
                   timeout: float = _DEFAULT_TIMEOUT) -> ToolResult:
        """调用对端工具，返回 ToolResult。"""
        if not self.ready:
            return ToolResult.error("设备 MCP 尚未就绪")
        if not self.has_tool(sanitized_name):
            return ToolResult.error(f"工具不存在：{sanitized_name}")

        arguments = self._normalize_args(args)
        cid = await self._next_call_id()
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._call_results[cid] = fut
        actual = self.name_mapping.get(sanitized_name, sanitized_name)
        await self._send_payload({
            "jsonrpc": "2.0", "id": cid, "method": "tools/call",
            "params": {"name": actual, "arguments": arguments},
        })
        try:
            raw = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._call_results.pop(cid, None)
            return ToolResult.error(f"工具 {sanitized_name} 调用超时")
        except Exception as e:  # noqa: BLE001
            self._call_results.pop(cid, None)
            return ToolResult.error(f"工具 {sanitized_name} 调用失败：{e}")

        return self._to_result(raw)

    @staticmethod
    def _to_result(raw: Any) -> ToolResult:
        if isinstance(raw, dict):
            if raw.get("isError") is True:
                return ToolResult.error(
                    raw.get("error") or "工具返回错误但未说明原因")
            content = raw.get("content")
            if isinstance(content, list) and content:
                first = content[0]
                if isinstance(first, dict) and "text" in first:
                    return ToolResult.ok(first.get("text", ""))
            return ToolResult.ok(str(raw))
        return ToolResult.ok(str(raw))
