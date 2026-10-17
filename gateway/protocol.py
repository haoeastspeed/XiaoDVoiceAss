# -*- coding: utf-8 -*-
"""小智协议消息构造（无业务逻辑）。"""
from __future__ import annotations

import json
import uuid


def _dump(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


def hello(session_id: str, audio_params: dict, version: int = 1) -> str:
    """服务端握手响应。version 为协商后的二进制协议版本（1/2/3）。"""
    return _dump({
        "type": "hello",
        "session_id": session_id,
        "version": int(version),
        "transport": "websocket",
        "audio_params": audio_params,
    })


def stt(text: str, speaker: str | None = None) -> str:
    msg: dict = {"type": "stt", "text": text}
    if speaker:
        # 扩展字段：说话人（声纹识别结果）。官方固件不认识该字段，安全忽略。
        msg["speaker"] = speaker
    return _dump(msg)


def search_state(state: str, query: str = "", count: int = 0) -> str:
    """联网搜索进度（客户端据此显示"正在联网搜索…"或检索到的条数）。"""
    return _dump({"type": "search", "state": state,
                  "query": query, "count": count})


def notice(text: str) -> str:
    """给用户看的一条提示（TTS 降级/恢复等）：客户端弹一个系统气泡。

    与"内部提示"区分：内部提示（音频端点回退）走 stderr，不发这里。"""
    return _dump({"type": "notice", "text": text})


def tts(state: str, text: str | None = None, session_id: str | None = None) -> str:
    msg: dict = {"type": "tts", "state": state}
    if text is not None:
        msg["text"] = text
    if session_id is not None:
        msg["session_id"] = session_id
    return _dump(msg)


def tts_start(session_id: str) -> str:
    return tts("start", session_id=session_id)


def sentence_start(text: str, session_id: str, sentence_id: str) -> str:
    return _dump({
        "type": "tts",
        "state": "sentence_start",
        "text": text,
        "session_id": session_id,
        "sentence_id": sentence_id,
    })


def sentence_end(session_id: str, sentence_id: str) -> str:
    return _dump({
        "type": "tts",
        "state": "sentence_end",
        "session_id": session_id,
        "sentence_id": sentence_id,
    })


def tts_stop(session_id: str) -> str:
    return tts("stop", session_id=session_id)


def goodbye(session_id: str | None = None) -> str:
    msg: dict = {"type": "goodbye"}
    if session_id:
        msg["session_id"] = session_id
    return _dump(msg)


def new_sentence_id() -> str:
    return uuid.uuid4().hex
