# -*- coding: utf-8 -*-
"""小智协议消息构造（无业务逻辑）。"""
from __future__ import annotations

import json
import uuid


def _dump(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


def hello(session_id: str, audio_params: dict) -> str:
    """服务端握手响应。"""
    return _dump({
        "type": "hello",
        "session_id": session_id,
        "version": 1,
        "transport": "websocket",
        "audio_params": audio_params,
    })


def stt(text: str) -> str:
    return _dump({"type": "stt", "text": text})


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
