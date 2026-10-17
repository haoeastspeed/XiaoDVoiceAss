# -*- coding: utf-8 -*-
"""MCP 与工具生态。

对齐官方 xiaozhi-esp32 / xiaozhi-server：
  * types      —— 公共类型（ToolDef / ToolResult / sanitize_tool_name）
  * registry   —— 统一工具注册表（内置 + 设备侧 + endpoint 三源合一）
  * device     —— 设备侧 MCP 客户端（PC 客户端 / ESP32 都是对端 MCP server）
  * endpoint    —— MCP endpoint 管理（本地 stdio / 远程 SSE / streamable-http）
  * builtin    —— 网关内置 self.gateway.* 工具
  * loop       —— 工具调用循环（多轮 tool call + TTS 反馈）
"""
from gateway.tools.types import ToolDef, ToolResult, sanitize_tool_name
from gateway.tools.registry import ToolRegistry

__all__ = ["ToolDef", "ToolResult", "sanitize_tool_name", "ToolRegistry"]
