# -*- coding: utf-8 -*-
"""会话状态机 + 全双工实时编排（自动断句、流式 LLM/TTS、随时打断）。"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import AsyncIterator, Optional

import websockets

from gateway import protocol
from gateway.audio.opus import UplinkDecoder
from gateway.config import GatewayConfig
from gateway.providers.base import BaseASR, BaseLLM, BaseTTS, BaseVAD

# 句末标点（强切）与逗号（长句缓冲上限时软切）
_SENT_END = re.compile(r"[。！？!?；;\n]")
_COMMA = re.compile(r"[，,]")
_MAX_CHUNK = 35
_BARGE_KEEP_BYTES = 16000  # 打断时保留约 0.5s 开头


def split_sentence(buf: str) -> Optional[tuple[str, str]]:
    m = _SENT_END.search(buf)
    if m:
        return buf[:m.end()], buf[m.end():]
    # 累积过长仍无句末标点时，在窗口内找最后一个逗号切（避免碎成 2~3 字）
    if len(buf) >= _MAX_CHUNK:
        commas = list(_COMMA.finditer(buf[:_MAX_CHUNK + 5]))
        if commas:
            last = commas[-1]
            return buf[:last.end()], buf[last.end():]
    return None


class VoiceSession:
    def __init__(self, ws, config: GatewayConfig,
                 llm: BaseLLM, asr: BaseASR, tts: BaseTTS, vad: BaseVAD):
        self.ws = ws
        self.config = config
        self.llm = llm
        self.asr = asr
        self.tts = tts
        self.vad = vad

        self.session_id = uuid.uuid4().hex
        self.state = "idle"  # idle/listening/thinking/speaking
        self.messages = [{"role": "system", "content": config.system_prompt}]

        self._collecting = False
        self.listen_mode = "auto"
        self._utterance = bytearray()
        self._barge = bytearray()
        self.vad_state = vad.new_state()

        self._decoder = UplinkDecoder(
            config.uplink_sample_rate, config.uplink_frame_size
        )
        self._response_task: Optional[asyncio.Task] = None
        self._closed = False

    # ---------- 主循环 ----------

    async def run(self):
        try:
            async for raw in self.ws:
                if isinstance(raw, (bytes, bytearray)):
                    await self._on_binary(bytes(raw))
                else:
                    await self._on_text(raw)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            await self._cleanup()

    async def _cleanup(self):
        self._closed = True
        task = self._response_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except Exception:
                pass

    # ---------- 发送 ----------

    async def _send(self, data) -> bool:
        if self._closed:
            return False
        try:
            await self.ws.send(data)
            return True
        except Exception:
            return False

    # ---------- 控制消息 ----------

    async def _on_text(self, raw: str):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        t = msg.get("type")
        if t == "hello":
            await self._on_hello(msg)
        elif t == "listen":
            await self._on_listen(msg)
        elif t == "abort":
            await self._cancel_response()
            self._collecting = False
            self._utterance = bytearray()
            self.state = "listening"
        elif t == "ping":
            await self._send(json.dumps({"type": "pong"}))
        # iot / mcp：暂不处理，保持连接

    async def _on_hello(self, msg: dict):
        audio_params = {
            "format": "opus",
            "sample_rate": self.config.downlink_sample_rate,
            "channels": self.config.channels,
            "frame_duration": self.config.frame_duration_ms,
        }
        # 若客户端声明了音频参数，沿用其格式（采样率仍以服务端下行能力为准）
        if msg.get("audio_params"):
            client_ap = msg["audio_params"]
            audio_params["format"] = client_ap.get("format", "opus")
        await self._send(protocol.hello(self.session_id, audio_params))

    async def _on_listen(self, msg: dict):
        state = msg.get("state")
        if state == "start":
            await self._cancel_response()
            self.listen_mode = msg.get("mode", "auto")
            self._collecting = True
            self._utterance = bytearray()
            self._barge = bytearray()
            self.vad.reset_sentence(self.vad_state)
            self.state = "listening"
        elif state == "stop":
            was_collecting = self._collecting
            self._collecting = False
            if was_collecting and self.listen_mode == "manual":
                await self._finish_utterance()
        elif state == "detect":
            text = (msg.get("text") or "").strip()
            if text:
                self._collecting = False
                await self._start_response(text)

    # ---------- 上行音频 ----------

    async def _on_binary(self, data: bytes):
        if not self._collecting and not self._response_active():
            return
        pcm = self._decoder.decode(data)
        if not pcm:
            return

        if self._response_active():
            # 响应中：只用于检测用户是否开口（barge-in）
            speaking, _ = self.vad.process(self.vad_state, pcm)
            self._barge.extend(pcm)
            if len(self._barge) > _BARGE_KEEP_BYTES:
                del self._barge[:len(self._barge) - _BARGE_KEEP_BYTES]
            if speaking and self.listen_mode == "auto":
                await self._cancel_response()
                self._collecting = True
                self._utterance = bytearray(self._barge)
                self._barge = bytearray()
                self.state = "listening"
            return

        if not self._collecting:
            return
        self._utterance.extend(pcm)
        if self.listen_mode == "manual":
            return
        _, ended = self.vad.process(self.vad_state, pcm)
        if ended:
            self._collecting = False
            await self._finish_utterance()

    # ---------- 识别 + 响应 ----------

    def _response_active(self) -> bool:
        return self._response_task is not None and not self._response_task.done()

    async def _finish_utterance(self):
        pcm = bytes(self._utterance)
        self._utterance = bytearray()
        if len(pcm) < 3200:  # 不足 0.1s
            return
        try:
            text = await asyncio.to_thread(self.asr.transcribe, pcm)
        except Exception:
            text = ""
        if not text:
            return
        self.vad.reset_sentence(self.vad_state)
        await self._start_response(text)

    async def _start_response(self, text: str):
        await self._send(protocol.stt(text))
        self.messages.append({"role": "user", "content": text})
        self._barge = bytearray()
        self.vad.reset_sentence(self.vad_state)
        self.state = "thinking"
        self._response_task = asyncio.create_task(self._run_response())

    async def _iter_sentences(self) -> AsyncIterator[str]:
        buf = ""
        async for chunk in self.llm.chat(self.messages):
            buf += chunk
            while True:
                split = split_sentence(buf)
                if split is None:
                    break
                sent, buf = split
                if sent.strip():
                    yield sent
        if buf.strip():
            yield buf

    async def _run_response(self):
        sid = self.session_id
        full = ""
        tts_started = False
        try:
            async for sent in self._iter_sentences():
                if not tts_started:
                    await self._send(protocol.tts_start(sid))
                    tts_started = True
                    self.state = "speaking"
                sentid = protocol.new_sentence_id()
                await self._send(protocol.sentence_start(sent, sid, sentid))
                try:
                    packets = await self.tts.synthesize(sent)
                except Exception:
                    packets = []
                for p in packets:
                    if not await self._send(p):
                        break
                await self._send(protocol.sentence_end(sid, sentid))
                full += sent
            if not tts_started:
                await self._send(protocol.tts_start(sid))
                hint = "哎呀，我刚刚没反应过来，你再说一遍呗。"
                sid2 = protocol.new_sentence_id()
                await self._send(protocol.sentence_start(hint, sid, sid2))
                for p in await self.tts.synthesize(hint):
                    await self._send(p)
                await self._send(protocol.sentence_end(sid, sid2))
                full = hint
            await self._send(protocol.tts_stop(sid))
            if full:
                self.messages.append({"role": "assistant", "content": full})
            self.state = "idle"
        except asyncio.CancelledError:
            await self._send(protocol.tts_stop(sid))
            raise
        except Exception as e:
            import traceback
            print(f"[session-dbg] _run_response 异常: {e}", flush=True)
            traceback.print_exc()
            await self._send(protocol.tts_stop(sid))
            self.state = "idle"

    async def _cancel_response(self):
        task = self._response_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._response_task = None
