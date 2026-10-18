# -*- coding: utf-8 -*-
"""端到端实时语音（OpenAI Realtime API / 同构服务）。

与级联链路（ASR→LLM→TTS）不同，realtime 把上行音频直接喂给模型，模型边听边
出声，省掉三段式延迟。协议要点（OpenAI Realtime，GLM-Realtime 同构）：

  * 连 `wss://<host>/v1/realtime?model=<model>`，鉴权 Bearer + `OpenAI-Beta: realtime=v1`
  * `session.update` 选模态/音色/输入输出格式（pcm16）/服务端 VAD
  * `input_audio_buffer.append`（base64 PCM16）
  * 收 `response.audio.delta`（base64 PCM16）/ `response.audio_transcript.delta`
  * `response.cancel` 打断

本模块只负责"连接 + 收发 + 编解码"，会话状态机由 session 编排。
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import AsyncIterator, Optional

import websockets


def resample_s16(x, src: int, dst: int):
    """int16 单声道线性重采样（16k↔24k）。"""
    import numpy as np
    if src == dst or x.size == 0:
        return x
    n = int(round(x.size * dst / src))
    if n <= 0:
        return np.zeros(0, dtype=np.int16)
    idx = np.linspace(0.0, x.size - 1, n)
    y = np.interp(idx, np.arange(x.size, dtype=np.float64),
                  x.astype(np.float32))
    return np.clip(y, -32768, 32767).astype(np.int16)


def _to_pcm16(x, src_rate: int, dst_rate: int = 24000) -> bytes:
    """s16le bytes → 目标采样率 s16le bytes。"""
    import numpy as np
    a = np.frombuffer(x, dtype=np.int16)
    return resample_s16(a, src_rate, dst_rate).tobytes()


class RealtimeSession:
    """一次 realtime 语音会话（连接 + 事件流）。"""

    def __init__(self, base_url: str, api_key: str, model: str,
                 voice: str = "alloy", uplink_rate: int = 16000,
                 logger=None):
        # base_url 形如 "wss://api.openai.com" 或 "https://..."（自动换协议）
        b = (base_url or "").strip().rstrip("/")
        if b.startswith("https://"):
            b = "wss://" + b[len("https://"):]
        elif b.startswith("http://"):
            b = "ws://" + b[len("http://"):]
        self.base_url = b
        self.api_key = (api_key or "").strip()
        self.model = model
        self.voice = voice or "alloy"
        self.uplink_rate = int(uplink_rate)
        self._log = logger or (lambda m: print(f"[realtime] {m}", flush=True))
        self._ws = None
        self._recv_task: Optional[asyncio.Task] = None
        self._audio_q: asyncio.Queue = asyncio.Queue()
        self._text_q: asyncio.Queue = asyncio.Queue()
        self.ready = False

    @property
    def url(self) -> str:
        return f"{self.base_url}/v1/realtime?model={self.model}"

    async def connect(self):
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "OpenAI-Beta": "realtime=v1"}
        # websockets 13.x 的 legacy API 只认 extra_headers，14+ 才叫
        # additional_headers。传错的那个会被塞进 **kwargs 透传给
        # asyncio.create_connection → TypeError（realtime 一开启就崩）。
        try:
            self._ws = await websockets.connect(
                self.url, additional_headers=headers, max_size=None,
                ping_interval=20, ping_timeout=60)
        except TypeError:
            self._ws = await websockets.connect(
                self.url, extra_headers=headers, max_size=None,
                ping_interval=20, ping_timeout=60)
        await self._send({
            "type": "session.update",
            "session": {
                "modalities": ["audio", "text"],
                "voice": self.voice,
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "turn_detection": {"type": "server_vad"},
            },
        })
        self._recv_task = asyncio.create_task(self._recv_loop())
        self.ready = True
        self._log("realtime 会话已建立")

    async def _send(self, obj: dict):
        if self._ws is not None:
            await self._ws.send(json.dumps(obj))

    async def append_audio(self, pcm_s16le: bytes):
        """把上行 PCM（16k）重采样到 24k 后送入模型。"""
        if not self.ready or not pcm_s16le:
            return
        pcm24 = _to_pcm16(pcm_s16le, self.uplink_rate, 24000)
        await self._send({"type": "input_audio_buffer.append",
                          "audio": base64.b64encode(pcm24).decode("ascii")})

    async def cancel(self):
        await self._send({"type": "response.cancel"})

    async def close(self):
        self.ready = False
        if self._recv_task:
            self._recv_task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

    async def _recv_loop(self):
        try:
            async for raw in self._ws:
                try:
                    ev = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                t = ev.get("type")
                if t == "response.audio.delta" and ev.get("delta"):
                    pcm = base64.b64decode(ev["delta"])
                    await self._audio_q.put(pcm)
                elif t == "response.audio_transcript.delta" and ev.get("delta"):
                    await self._text_q.put(ev["delta"])
                elif t in ("response.done", "response.audio.done"):
                    await self._audio_q.put(None)   # 本轮结束哨兵
                elif t == "error":
                    self._log(f"realtime 错误：{ev.get('error')}")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self._log(f"realtime 接收循环结束：{type(e).__name__}: {e}")
        finally:
            self.ready = False

    async def audio_events(self) -> AsyncIterator[bytes]:
        """产出 24k PCM 块；None 表示本轮音频结束。"""
        while True:
            chunk = await self._audio_q.get()
            yield chunk
            if chunk is None:
                return

    async def text_events(self) -> AsyncIterator[str]:
        while True:
            yield await self._text_q.get()


def from_config(config, logger=None) -> Optional[RealtimeSession]:
    if not getattr(config, "realtime_enabled", False):
        return None
    base = str(getattr(config, "realtime_base_url", "") or "").strip()
    if not base:
        return None
    return RealtimeSession(
        base,
        str(getattr(config, "realtime_api_key", "") or ""),
        str(getattr(config, "realtime_model", "gpt-4o-realtime-preview")
            or "gpt-4o-realtime-preview"),
        uplink_rate=int(getattr(config, "uplink_sample_rate", 16000)),
        logger=logger)
