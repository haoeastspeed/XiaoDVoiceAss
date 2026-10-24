# -*- coding: utf-8 -*-
"""MCP endpoint 管理：把第三方/本地 MCP server 的工具接进网关。

配置格式对齐官方 mcp_server_settings.json 的 `mcpServers`：
```
{
  "mcpServers": {
    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]},
    "weather":    {"url": "http://127.0.0.1:9000/mcp", "transport": "streamable-http"},
    "remote":     {"url": "https://example.com/sse", "headers": {"Authorization": "Bearer x"}}
  }
}
```

三种传输：
  * stdio            —— 起子进程，newline-delimited JSON-RPC（本地 MCP server）
  * streamable-http  —— HTTP POST JSON-RPC，响应可为 JSON 或 SSE 流
  * sse              —— HTTP POST JSON-RPC，从 SSE 响应里取结果（简化实现）

设计取舍：不引官方 mcp SDK（会拖 pydantic v1/v2 双栈与 anyio，打包体量大且版本
风险高）。这里只实现 initialize / tools/list / tools/call 三个方法，自研可控，
并预留 `_rpc` 边界，将来可平替为官方 SDK。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

from gateway.tools.types import ToolDef, ToolResult, empty_schema, sanitize_tool_name


def _no_window_kwargs() -> dict:
    """Windows 下启动子进程时隐藏控制台窗口的统一参数（双保险）。

    CREATE_NO_WINDOW 阻止创建控制台；STARTUPINFO + SW_HIDE 兜底某些
    Windows 版本对 CREATE_NO_WINDOW 处理不一致的情况。非 Windows 返回空。
    """
    if os.name != "nt":
        return {}
    import subprocess
    si = subprocess.STARTUPINFO()
    si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    si.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return {
        "creationflags": (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                         | getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
        "startupinfo": si,
    }


_INIT_TIMEOUT = 10.0
_CALL_TIMEOUT = 30.0
_FIRST_ID = 1


# ---------------------------------------------------------------------------
# 单个 endpoint
# ---------------------------------------------------------------------------


class _BaseEndpoint:
    """一个 MCP server 连接。子类实现 _rpc / _start / _close。"""

    def __init__(self, name: str, cfg: dict,
                 logger: Optional[Callable[[str], None]] = None):
        self.name = name
        self.cfg = cfg or {}
        self._log = logger or (lambda m: None)
        self._next_id = _FIRST_ID
        self._pending: Dict[int, asyncio.Future] = {}
        self.tools: Dict[str, dict] = {}       # 原始名 -> {name,description,inputSchema}
        self.ready = False

    # ---- 供子类覆盖 ----
    async def _start(self): ...
    async def _rpc(self, payload: dict) -> Optional[dict]: ...
    async def _close(self): ...

    async def connect(self) -> List[ToolDef]:
        await self._start()
        await self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {"roots": {"listChanged": True}, "sampling": {}},
            "clientInfo": {"name": "XiaoDGateway", "version": "1.0.0"},
        })
        await self._fetch_tools()
        self.ready = True
        self._log(f"endpoint「{self.name}」就绪：{len(self.tools)} 个工具")
        return self.tool_defs()

    async def _request(self, method: str, params: Optional[dict] = None,
                       timeout: float = _INIT_TIMEOUT) -> Any:
        rid = self._next_id
        self._next_id += 1
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        # tools/call 可能跑几十秒（搜索 / 远端服务 / 大文件），不能沿用
        # init 的 10s 超时——否则外层 30s 永远等不到，慢工具必然"调用超时"。
        resp = await asyncio.wait_for(self._rpc(payload), timeout=timeout)
        if not isinstance(resp, dict):
            return None
        if "error" in resp:
            err = resp["error"] or {}
            raise RuntimeError(err.get("message", "MCP 错误") if isinstance(err, dict)
                               else str(err))
        return resp.get("result")

    async def _fetch_tools(self, cursor: str = ""):
        params = {"cursor": cursor} if cursor else None
        try:
            result = await self._request("tools/list", params)
        except Exception as e:  # noqa: BLE001
            self._log(f"endpoint「{self.name}」tools/list 失败：{e}")
            return
        if not isinstance(result, dict):
            return
        for tool in result.get("tools") or []:
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
            self.tools[name] = {
                "description": tool.get("description", "") or "",
                "inputSchema": schema,
            }
        nxt = result.get("nextCursor", "")
        if nxt:
            await self._fetch_tools(nxt)

    def tool_defs(self) -> List[ToolDef]:
        """工具名加 `{endpoint}.` 前缀，避免与内置/设备工具撞名。"""
        defs: List[ToolDef] = []
        for raw_name, data in self.tools.items():
            full = f"{self.name}.{raw_name}"
            sanitized = sanitize_tool_name(full)

            def _make(rn: str):
                async def _handler(args: Dict[str, Any]) -> ToolResult:
                    return await self.call(rn, args)
                return _handler

            defs.append(ToolDef(
                name=sanitized,
                description=data.get("description", ""),
                input_schema=data.get("inputSchema") or empty_schema(),
                source=f"endpoint:{self.name}",
                handler=_make(raw_name),
                original=full,
            ))
        return defs

    async def call(self, raw_name: str, args: Any) -> ToolResult:
        if not self.ready:
            return ToolResult.error(f"endpoint「{self.name}」未就绪")
        arguments = args if isinstance(args, dict) else {}
        try:
            result = await asyncio.wait_for(
                self._request("tools/call",
                              {"name": raw_name, "arguments": arguments},
                              timeout=_CALL_TIMEOUT),
                timeout=_CALL_TIMEOUT)
        except asyncio.TimeoutError:
            return ToolResult.error(f"endpoint「{self.name}」{raw_name} 调用超时")
        except Exception as e:  # noqa: BLE001
            return ToolResult.error(f"endpoint「{self.name}」{raw_name} 调用失败：{e}")
        if isinstance(result, dict):
            if result.get("isError"):
                return ToolResult.error(
                    result.get("error") or "工具返回错误但未说明原因")
            content = result.get("content")
            if isinstance(content, list) and content:
                first = content[0]
                if isinstance(first, dict) and "text" in first:
                    return ToolResult.ok(first.get("text", ""))
            return ToolResult.ok(json.dumps(result, ensure_ascii=False))
        return ToolResult.ok(str(result))


class StdioEndpoint(_BaseEndpoint):
    """本地子进程 MCP server（newline-delimited JSON-RPC）。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None

    async def _start(self):
        command = self.cfg.get("command")
        if not command:
            raise ValueError("stdio endpoint 缺少 command")
        exe = shutil.which(command) or command
        args = [str(a) for a in (self.cfg.get("args") or [])]
        env = dict(os.environ)
        env.update({k: str(v) for k, v in (self.cfg.get("env") or {}).items()})
        self._proc = await asyncio.create_subprocess_exec(
            exe, *args, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=env, **_no_window_kwargs())
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        proc = self._proc
        if not proc or not proc.stdout:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except Exception:  # noqa: BLE001
                    continue
                self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass

    def _dispatch(self, msg: dict):
        rid = msg.get("id")
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            return
        fut = self._pending.pop(rid, None)
        if fut and not fut.done():
            fut.set_result(msg)

    async def _rpc(self, payload: dict) -> Optional[dict]:
        if not self._proc or not self._proc.stdin:
            return None
        rid = int(payload.get("id", 0))
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()
        try:
            return await asyncio.wait_for(fut, timeout=_CALL_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            return None

    async def _close(self):
        if self._reader_task:
            self._reader_task.cancel()
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:  # noqa: BLE001
                pass


class HttpEndpoint(_BaseEndpoint):
    """streamable-http：POST JSON-RPC，响应可为 JSON 或 SSE 流。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._client: Optional[httpx.AsyncClient] = None

    async def _start(self):
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=_CALL_TIMEOUT,
                                  write=10.0, pool=5.0),
            trust_env=False)

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        for k, v in (self.cfg.get("headers") or {}).items():
            h[str(k)] = str(v)
        return h

    async def _rpc(self, payload: dict) -> Optional[dict]:
        if not self._client:
            return None
        url = self.cfg.get("url", "")
        async with self._client.stream("POST", url, json=payload,
                                       headers=self._headers()) as resp:
            ctype = (resp.headers.get("content-type") or "").lower()
            if "event-stream" in ctype:
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        msg = json.loads(raw)
                    except Exception:  # noqa: BLE001
                        continue
                    if isinstance(msg, dict) and msg.get("id") == payload.get("id"):
                        return msg
                return None
            body = await resp.aread()
            try:
                return json.loads(body.decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                return None

    async def _close(self):
        if self._client:
            await self._client.aclose()


class SseEndpoint(HttpEndpoint):
    """sse：与 streamable-http 共用 POST + SSE 解析（简化实现）。

    严格 SSE 传输要先 GET 打开事件流、再从 endpoint 事件拿 POST 地址。这里
    多数实现都兼容直接 POST + SSE 响应，够用；不兼容的 server 用
    transport=streamable-http 指定即可。"""


# ---------------------------------------------------------------------------
# 管理器
# ---------------------------------------------------------------------------


class MCPEndpointManager:
    """按配置并发连接多个 MCP server，汇聚它们的工具。"""

    def __init__(self, servers: Dict[str, dict],
                 logger: Optional[Callable[[str], None]] = None):
        self._servers = servers or {}
        self._log = logger or (lambda m: print(f"[mcp-endpoint] {m}", flush=True))
        self._endpoints: Dict[str, _BaseEndpoint] = {}
        self.tools: List[ToolDef] = []

    def _make(self, name: str, cfg: dict) -> _BaseEndpoint:
        transport = str(cfg.get("transport", "") or "").lower()
        if cfg.get("command"):
            return StdioEndpoint(name, cfg, self._log)
        if transport == "streamable-http" or transport == "http":
            return HttpEndpoint(name, cfg, self._log)
        if transport == "sse":
            return SseEndpoint(name, cfg, self._log)
        # 只有 url：默认按 streamable-http 处理
        if cfg.get("url"):
            return HttpEndpoint(name, cfg, self._log)
        raise ValueError(f"无法识别的 MCP endpoint 配置：{name}")

    async def start(self) -> List[ToolDef]:
        """并发连接全部 endpoint；单个失败只告警、不影响其它与网关本身。"""
        if not self._servers:
            return []
        results = await asyncio.gather(
            *[self._connect_one(n, c) for n, c in self._servers.items()],
            return_exceptions=True)
        tools: List[ToolDef] = []
        for r in results:
            if isinstance(r, list):
                tools.extend(r)
        self.tools = tools
        self._log(f"endpoint 汇总：成功 {len(self._endpoints)} 个，"
                  f"共 {len(tools)} 个工具")
        return tools

    async def _connect_one(self, name: str, cfg: dict) -> List[ToolDef]:
        try:
            ep = self._make(name, cfg)
        except Exception as e:  # noqa: BLE001
            self._log(f"endpoint「{name}」配置错误：{e}")
            return []
        try:
            defs = await asyncio.wait_for(ep.connect(), timeout=_INIT_TIMEOUT * 2)
            self._endpoints[name] = ep
            return defs
        except Exception as e:  # noqa: BLE001
            self._log(f"endpoint「{name}」连接失败（已禁用）："
                      f"{type(e).__name__}: {e}")
            try:
                await ep._close()
            except Exception:  # noqa: BLE001
                pass
            return []

    async def close(self):
        for ep in self._endpoints.values():
            try:
                await ep._close()
            except Exception:  # noqa: BLE001
                pass
        self._endpoints.clear()
