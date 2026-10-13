# -*- coding: utf-8 -*-
"""EdgeTTS：edge_tts 产 mp3 → ffmpeg 转 24k PCM → opus。

stream() 为真流式：edge mp3 块边到边喂 ffmpeg，PCM 边产出边编码 opus
并逐包 yield，比“收完整 mp3 再转”更早发出首包，降低首句延迟。
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import suppress
from typing import List

import edge_tts
import edge_tts.communicate

from gateway.audio.opus import DownlinkEncoder
from gateway.config import BIN_DIR
from gateway.providers.base import BaseTTS

# edge-tts 7.2.8 旧端点 403，切换到新端点（M1 已验证）
_NEW_WSS_URL = (
    "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
    f"?Ocp-Apim-Subscription-Key={edge_tts.constants.TRUSTED_CLIENT_TOKEN}"
)
edge_tts.constants.WSS_URL = _NEW_WSS_URL
edge_tts.communicate.WSS_URL = _NEW_WSS_URL

# Windows 下不弹黑框
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class EdgeTTS(BaseTTS):
    def __init__(self, voice: str, sample_rate: int = 24000,
                 frame_size: int = 1440):
        self.voice = voice
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self._frame_bytes = frame_size * 2

    async def _mp3(self, text: str) -> bytes:
        comm = edge_tts.Communicate(text, voice=self.voice)
        buf = bytearray()
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                buf.extend(chunk["data"])
        return bytes(buf)

    async def _to_pcm(self, mp3: bytes) -> bytes:
        ff = f"{BIN_DIR}/ffmpeg.exe"
        proc = await asyncio.create_subprocess_exec(
            ff, "-i", "pipe:0", "-ar", str(self.sample_rate), "-ac", "1",
            "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
        pcm, _ = await proc.communicate(mp3)
        return pcm

    async def synthesize(self, text: str) -> List[bytes]:
        text = text.strip()
        if not text:
            return []
        mp3 = await self._mp3(text)
        if not mp3:
            return []
        pcm = await self._to_pcm(mp3)
        # 每次合成独立 encoder（句子内帧连续；跨句无需共享，避免多会话状态串扰）
        encoder = DownlinkEncoder(self.sample_rate, self.frame_size)
        return encoder.encode_pcm(pcm)

    async def stream(self, text: str):
        """真流式：edge mp3 → ffmpeg → PCM → opus，逐包 yield。"""
        text = text.strip()
        if not text:
            return
        ff = f"{BIN_DIR}/ffmpeg.exe"
        proc = await asyncio.create_subprocess_exec(
            ff, "-i", "pipe:0", "-ar", str(self.sample_rate), "-ac", "1",
            "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
        encoder = DownlinkEncoder(self.sample_rate, self.frame_size)
        comm = edge_tts.Communicate(text, voice=self.voice)
        pcm_buf = bytearray()

        async def feed():
            try:
                async for chunk in comm.stream():
                    if chunk["type"] == "audio":
                        proc.stdin.write(chunk["data"])
                        await proc.stdin.drain()
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:  # noqa: BLE001
                    pass

        feeder = asyncio.create_task(feed())
        try:
            while True:
                pcm = await proc.stdout.read(2048)
                if not pcm:
                    break
                pcm_buf.extend(pcm)
                while len(pcm_buf) >= self._frame_bytes:
                    frame = bytes(pcm_buf[:self._frame_bytes])
                    del pcm_buf[:self._frame_bytes]
                    yield encoder.encode_frame(frame)
        finally:
            await feeder
            with suppress():
                await proc.wait()
