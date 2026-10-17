# -*- coding: utf-8 -*-
"""试听合成端点：把 TTS 合成放进网关进程（GUI 进程只负责播放）。

为什么需要它：GUI 进程里跑 sherpa-onnx（Kokoro）或 SAPI 的原生栈，在打包
环境下会触发进程级崩溃（表现为"离线音色试听无声音、软件闪退"）。而网关
进程里同一套合成早就常驻且稳定（离线对话一直用它），所以试听的合成改由
网关完成，GUI 通过 HTTP 拿 WAV 直接放。

请求：POST /tts/preview/  body: {"voice": "...", "text": "可选"}
响应：200 audio/wav（24k 16bit mono，头 X-Engine 标实际引擎）
      4xx/5xx JSON {"success": false, "message": "..."}
"""
from __future__ import annotations

import asyncio
import io
import json
import wave
from typing import Tuple

from gateway.http.httpd import GatewayHTTP, json_response

_SAMPLE_RATE = 24000


def _default_text(voice: str) -> str:
    """试听语句跟音色语言走（英语音色听中文样本判断不了音色）。"""
    from gateway.providers.tts_router import LOCAL_PREFIX
    vid = voice[len(LOCAL_PREFIX):] if voice.startswith(LOCAL_PREFIX) \
        else voice
    low = vid.lower()
    if low.startswith("en-") or (low[:3] in ("af_", "am_", "bf_", "bm_")):
        return ("Hello, I am XiaoD, your local voice assistant. "
                "How is your day going so far?")
    return "你好，我是小D，很高兴认识你。今天过得怎么样？"


def _pcm_to_wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class TTSPreviewHandler:
    def __init__(self, config, logger=None):
        self.config = config
        self._log = logger or (lambda m: print(f"[tts-preview] {m}",
                                               flush=True))

    def register(self, app: GatewayHTTP):
        app.route("POST", r"/tts/preview/?$", self.handle_preview)

    def handle_preview(self, body: bytes, headers: dict,
                       match) -> Tuple[int, dict, bytes]:
        try:
            data = json.loads(body or b"{}")
        except Exception:  # noqa: BLE001
            return json_response({"success": False,
                                  "message": "请求体不是合法 JSON"}, 400)
        voice = str(data.get("voice") or "").strip()
        if not voice:
            return json_response({"success": False,
                                  "message": "缺少 voice"}, 400)
        text = str(data.get("text") or "").strip() or _default_text(voice)
        # 引擎跟随音色：离线音色直接本地合成；在线音色 auto（含断网兜底）
        engine = "local" if voice.startswith("local:") else "auto"
        try:
            from gateway.providers.tts_router import TTSRouter
            router = TTSRouter(voice, _SAMPLE_RATE, 1440, engine=engine)
            pcm = asyncio.run(router.preview_pcm(
                text, {"rate": 1.0, "pitch": 1.0, "volume": 1.0}))
        except Exception as e:  # noqa: BLE001
            self._log(f"合成失败：{type(e).__name__}: {e}")
            return json_response({"success": False,
                                  "message": f"合成失败：{str(e)[:80]}"}, 502)
        if not pcm:
            return json_response({"success": False,
                                  "message": "合成没有返回音频"}, 502)
        self._log(f"voice={voice} engine={router.last_engine} "
                  f"{len(pcm)}B")
        return (200, {"Content-Type": "audio/wav",
                      "X-Engine": router.last_engine or engine},
                _pcm_to_wav(pcm, _SAMPLE_RATE))
