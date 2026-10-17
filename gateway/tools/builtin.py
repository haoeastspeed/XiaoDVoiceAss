# -*- coding: utf-8 -*-
"""网关内置工具（`self.gateway.*`）。

命名与官方设备侧 `self.*` 工具风格一致，但作用在**网关自身**：这些工具直接
复用现有的控制路径（与客户端 set_* 控制消息走同一套代码），所以不会出现
"两条实现各说各话"。

大模型可以通过这些工具在对话中主动调整网关行为（换音色、开关联网搜索、
切模型、清上下文、进休眠…），把"控制"从人工点界面变成对话能力。
"""
from __future__ import annotations

from typing import List

from gateway.tools.types import ToolDef, ToolResult


def _schema(props: dict, required: List[str] | None = None) -> dict:
    return {"type": "object", "properties": props,
            "required": required or []}


def build_builtin_tools(session) -> List[ToolDef]:
    """按会话构造内置工具列表（handler 闭包持有 session）。"""
    cfg = session.config

    # ---------- get_status ----------
    async def _get_status(_args) -> ToolResult:
        try:
            llm_model = getattr(session.llm, "model", "?")
        except Exception:  # noqa: BLE001
            llm_model = "?"
        voice = getattr(session, "voice", "") or getattr(cfg, "tts_voice", "")
        engine = getattr(cfg, "tts_engine", "auto")
        lang = getattr(cfg, "asr_language", "auto")
        info = (
            f"当前状态：模型 {llm_model}；音色 {voice}（引擎 {engine}）；"
            f"识别语言 {lang}；联网搜索 {'开' if getattr(cfg,'web_search',False) else '关'}；"
            f"记忆模式 {getattr(cfg,'memory_mode','full')}；"
            f"对话轮数 {max(0, (len(session.messages) - 1) // 2)}。"
        )
        return ToolResult.ok(info)

    # ---------- set_volume ----------
    async def _set_volume(args) -> ToolResult:
        try:
            val = float(args.get("volume", args.get("value", 0)))
        except (TypeError, ValueError):
            return ToolResult.error("音量参数无效，应为 0~1.6 之间的数字")
        val = max(0.0, min(1.6, val))
        # 音量在客户端生效：发一条 self.* 同款的 custom 控制消息，由客户端换算。
        await session._send_custom("set_volume", value=val)
        return ToolResult.ok(f"已把音量调到 {val:.2f}")

    # ---------- set_voice ----------
    async def _set_voice(args) -> ToolResult:
        from gateway.providers.tts_edge import known_voice
        voice = str(args.get("voice", "") or "").strip()
        if not voice or not known_voice(voice):
            return ToolResult.error(
                f"未知音色：{voice or '(空)'}。可用音色见界面「声音」列表")
        session.voice = voice
        fn = getattr(session.tts, "set_voice", None)
        if callable(fn):
            fn(voice)
        else:
            session.tts.voice = voice
        return ToolResult.ok(f"音色已切换为 {voice}（下一句生效）")

    # ---------- set_tts_engine ----------
    async def _set_tts_engine(args) -> ToolResult:
        engine = str(args.get("engine", "") or "").strip().lower()
        if engine not in ("auto", "online", "local"):
            return ToolResult.error("引擎只能是 auto / online / local")
        fn = getattr(session.tts, "set_engine", None)
        if callable(fn):
            fn(engine)
        cfg.tts_engine = engine
        return ToolResult.ok(f"语音合成引擎已设为 {engine}")

    # ---------- set_search ----------
    async def _set_search(args) -> ToolResult:
        on = bool(args.get("enabled", True))
        cfg.web_search = on
        return ToolResult.ok(f"联网搜索已{'开启' if on else '关闭'}")

    # ---------- set_model ----------
    async def _set_model(args) -> ToolResult:
        model = str(args.get("model", "") or "").strip()
        if not model:
            return ToolResult.error("请提供要切换的模型名")
        try:
            session.llm.model = model
        except Exception as e:  # noqa: BLE001
            return ToolResult.error(f"切换模型失败：{e}")
        return ToolResult.ok(f"语言模型已切换为 {model}")

    # ---------- set_memory ----------
    async def _set_memory(args) -> ToolResult:
        mode = str(args.get("mode", "") or "").strip().lower()
        if mode not in ("full", "summary", "none"):
            return ToolResult.error("记忆模式只能是 full / summary / none")
        cfg.memory_mode = mode
        if mode != "summary":
            session._summary = ""
        return ToolResult.ok(f"记忆模式已设为 {mode}")

    # ---------- clear_history ----------
    async def _clear_history(_args) -> ToolResult:
        keep = [m for m in session.messages if m.get("role") == "system"][:1]
        session.messages = keep
        session._summary = ""
        return ToolResult.ok("已清空对话上下文（人设保留）")

    # ---------- sleep ----------
    async def _sleep(_args) -> ToolResult:
        session._sleeping = True
        session.state = "idle"
        return ToolResult.ok("好的，我先歇着，需要我时再叫我。")

    tools = [
        ToolDef("self.gateway.get_status",
                "查看当前语音助手的运行状态（模型、音色、语言、联网搜索、记忆模式等）",
                _schema({}), "builtin", _get_status),
        ToolDef("self.gateway.set_volume",
                "调整扬声器播放音量",
                _schema({"volume": {"type": "number",
                                    "description": "音量倍数 0~1.6，1.0 为原始音量"}},
                        ["volume"]), "builtin", _set_volume),
        ToolDef("self.gateway.set_voice",
                "切换语音助手的说话音色（下一句生效）",
                _schema({"voice": {"type": "string",
                                   "description": "音色 id，如 zh-CN-XiaoxiaoNeural"}},
                        ["voice"]), "builtin", _set_voice),
        ToolDef("self.gateway.set_tts_engine",
                "切换语音合成引擎：auto=在线优先断网降级 / online=只用在线 / local=只用本地",
                _schema({"engine": {"type": "string",
                                    "enum": ["auto", "online", "local"]}},
                        ["engine"]), "builtin", _set_tts_engine),
        ToolDef("self.gateway.set_search",
                "开启或关闭联网搜索能力",
                _schema({"enabled": {"type": "boolean"}}, ["enabled"]),
                "builtin", _set_search),
        ToolDef("self.gateway.set_model",
                "热切换对话使用的大语言模型",
                _schema({"model": {"type": "string"}}, ["model"]),
                "builtin", _set_model),
        ToolDef("self.gateway.set_memory",
                "设置记忆模式：full=超长记忆 / summary=本地总结 / none=无记忆",
                _schema({"mode": {"type": "string",
                                  "enum": ["full", "summary", "none"]}},
                        ["mode"]), "builtin", _set_memory),
        ToolDef("self.gateway.clear_history",
                "清空对话上下文（保留人设），用于重新开始一个话题",
                _schema({}), "builtin", _clear_history),
        ToolDef("self.gateway.sleep",
                "让语音助手进入休眠（停止预热模型，用户再说一句话即唤醒）",
                _schema({}), "builtin", _sleep),
    ]
    return tools
