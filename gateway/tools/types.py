# -*- coding: utf-8 -*-
"""MCP / 工具生态的公共类型。

字段命名与官方 core/providers/tools 保持一致（inputSchema / isError / content），
方便与设备侧及第三方 MCP server 直接互操作。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

# 工具执行器：参数 dict → ToolResult（全部异步）
ToolHandler = Callable[[Dict[str, Any]], Awaitable["ToolResult"]]

# 对齐官方 core/utils/util.py::sanitize_tool_name：
# 保留中文、英文字母、数字、下划线和连字符，其余替换为下划线。
# （OpenAI function name 只允许 [a-zA-Z0-9_-]，官方额外放行中文并按原样回传。）
_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]")


def sanitize_tool_name(name: str) -> str:
    """把工具名清洗成 OpenAI function name 兼容的形式（对齐官方）。"""
    return _SANITIZE_RE.sub("_", name or "")


def empty_schema() -> Dict[str, Any]:
    return {"type": "object", "properties": {}, "required": []}


@dataclass
class ToolResult:
    """一次工具执行的结果。对齐官方 MCP tools/call 的 result 结构。"""

    text: str = ""
    is_error: bool = False

    @classmethod
    def ok(cls, text: Any) -> "ToolResult":
        return cls(text="" if text is None else str(text), is_error=False)

    @classmethod
    def error(cls, text: Any) -> "ToolResult":
        return cls(text="" if text is None else str(text), is_error=True)

    def to_content(self) -> List[dict]:
        """转成 MCP result.content 列表。"""
        return [{"type": "text", "text": self.text}]

    def to_mcp_result(self) -> dict:
        return {"content": self.to_content(), "isError": bool(self.is_error)}


@dataclass
class ToolDef:
    """一个可被大模型调用的工具。

    name        —— 送给大模型的名字（已 sanitize，且带来源前缀）
    source      —— builtin / device / endpoint:<name>
    handler     —— 网关侧的统一执行入口（设备/endpoint 工具也包成 handler，
                   这样 ToolRegistry 只认一种调用方式）
    original    —— 对端真实名字（设备工具 sanitize 前的原名）
    """

    name: str
    description: str = ""
    input_schema: Dict[str, Any] = field(default_factory=empty_schema)
    source: str = "builtin"
    handler: Optional[ToolHandler] = None
    original: str = ""

    def openai_function(self) -> dict:
        """转成 OpenAI tools 参数里的 function 定义（对齐官方 get_available_tools）。"""
        schema = self.input_schema or {}
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description or "",
                "parameters": {
                    "type": schema.get("type", "object") or "object",
                    "properties": schema.get("properties", {}) or {},
                    "required": [s for s in (schema.get("required") or [])
                                 if isinstance(s, str)],
                },
            },
        }
