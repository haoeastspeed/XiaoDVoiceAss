# -*- coding: utf-8 -*-
"""统一工具注册表：内置 + 设备侧 + endpoint 三源合一。

会话把三处工具全部注册到这里，大模型只看到一份扁平列表；执行时按 name
找到对应 handler 即可，不需要知道工具来自哪里。

命名隔离约定（防止不同来源撞名）：
  * 内置（网关本地） ——  `self.gateway.*`
  * 设备侧（对端 MCP server，PC 客户端 / ESP32）—— 保留原名 `self.*`
  * endpoint（第三方 MCP server）—— `{endpoint名}.{原始工具名}`
"""
from __future__ import annotations

from typing import Dict, List, Optional

from gateway.tools.types import ToolDef, sanitize_tool_name


class ToolRegistry:
    def __init__(self, logger=None):
        self._tools: Dict[str, ToolDef] = {}
        self._cache: Optional[List[dict]] = None
        self._log = logger

    # ---------- 注册 / 注销 ----------

    def register(self, tool: ToolDef) -> bool:
        """注册一个工具。

        名字一律先 sanitize（OpenAI function name 只允许 [a-zA-Z0-9_-] 与中文），
        因此 `self.gateway.get_status` 这类点号名会变成
        `self_gateway_get_status`。同名冲突时保留先注册者并告警。"""
        if not tool or not tool.name:
            return False
        sanitized = sanitize_tool_name(tool.name)
        if not sanitized:
            return False
        if sanitized != tool.name:
            if not tool.original:
                tool.original = tool.name
            tool.name = sanitized
        if sanitized in self._tools:
            self._warn(f"工具重名，忽略后注册者：{sanitized}"
                       f"（已有 {self._tools[sanitized].source}）")
            return False
        self._tools[sanitized] = tool
        self._cache = None
        return True

    def unregister(self, name: str) -> bool:
        if name in self._tools:
            del self._tools[name]
            self._cache = None
            return True
        return False

    def remove_by_prefix(self, prefix: str) -> int:
        """按前缀批量注销（endpoint 断开/重连时清旧工具）。"""
        victims = [n for n in self._tools if n.startswith(prefix)]
        for n in victims:
            del self._tools[n]
        if victims:
            self._cache = None
        return len(victims)

    def remove_by_source(self, source: str) -> int:
        """按来源批量注销（设备重连时清掉旧设备工具）。"""
        victims = [n for n, t in self._tools.items() if t.source == source]
        for n in victims:
            del self._tools[n]
        if victims:
            self._cache = None
        return len(victims)

    # ---------- 查询 ----------

    def get(self, name: str) -> Optional[ToolDef]:
        t = self._tools.get(name)
        if t is not None:
            return t
        # 模型有时按描述里的"原始名"回答（如 fs.read_file），而注册表 key 是
        # sanitize 过的（fs_read_file）。直接查不到会报"未知工具"，所以这里
        # 先按 sanitize 形式再按 original 反查一次。
        if not name:
            return None
        t = self._tools.get(sanitize_tool_name(name))
        if t is not None:
            return t
        for cand in self._tools.values():
            if getattr(cand, "original", "") == name:
                return cand
        return None

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> List[str]:
        return list(self._tools.keys())

    def sources(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for t in self._tools.values():
            out[t.source] = out.get(t.source, 0) + 1
        return out

    def __len__(self) -> int:
        return len(self._tools)

    def openai_functions(self) -> List[dict]:
        """全部工具的 OpenAI function 列表（带缓存）。"""
        if self._cache is None:
            self._cache = [t.openai_function() for t in self._tools.values()]
        return self._cache

    # ---------- 内部 ----------

    def _warn(self, msg: str):
        if self._log:
            self._log(msg)
        else:
            print(f"[tools] {msg}", flush=True)
