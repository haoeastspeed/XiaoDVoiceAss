# -*- coding: utf-8 -*-
"""小D网关 WebSocket 服务器。"""
from __future__ import annotations

import asyncio
import socket
from urllib.parse import parse_qs

import websockets

from gateway.config import GatewayConfig
from gateway.providers.asr_sensevoice import SenseVoiceASR
from gateway.providers.llm_ollama import OllamaLLM
from gateway.providers.tts_edge import EdgeTTS
from gateway.providers.vad_silero import SileroVAD
from gateway.session import VoiceSession


def get_lan_ip() -> str:
    """获取本机局域网 IP（用于设备配置）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class XiaoDGateway:
    def __init__(self, config: GatewayConfig):
        self.config = config
        self._llm = None
        self._asr = None
        self._tts = None
        self._vad = None
        self._server = None
        self._lock = asyncio.Lock()

    async def _init_providers(self):
        cfg = self.config
        async with self._lock:
            if self._llm is not None:
                return
            self._llm = OllamaLLM(cfg.ollama_base_url, cfg.llm_model)
            self._asr = SenseVoiceASR(cfg.sensevoice_dir, cfg.uplink_sample_rate)
            self._vad = SileroVAD(
                cfg.silero_dir, cfg.vad_threshold, cfg.vad_threshold_low,
                cfg.min_silence_ms, cfg.uplink_sample_rate,
            )
            self._tts = EdgeTTS(
                cfg.tts_voice, cfg.downlink_sample_rate,
                cfg.downlink_frame_size,
            )

    async def handler(self, ws, *args):
        # 解析路径
        req = getattr(ws, "request", None)
        path = getattr(req, "path", "")
        if args and isinstance(args[0], str):
            path = args[0]
        qs = path.split("?", 1)[1] if "?" in path else ""

        # 路径校验
        if not path.startswith(self.config.ws_path):
            await ws.close(code=1008, reason="not found")
            return

        # device-id 校验（header 或 query）
        device_id = ""
        if req is not None:
            device_id = req.headers.get("device-id", "")
        if not device_id and qs:
            parsed = parse_qs(qs)
            device_id = (parsed.get("device-id") or [""])[0]
        if not device_id:
            await ws.close(code=1008, reason="missing device-id")
            return

        await self._init_providers()
        session = VoiceSession(
            ws, self.config, self._llm, self._asr, self._tts, self._vad
        )
        await session.run()

    async def _warmup(self):
        """后台初始化 providers 并让 Ollama 预加载模型，缩短首句 TTFT。"""
        try:
            await self._init_providers()
            async for _ in self._llm.chat(
                [{"role": "user", "content": "你好"}]
            ):
                pass
        except Exception:  # noqa: BLE001
            pass

    async def start(self):
        cfg = self.config
        self._server = await websockets.serve(
            self.handler, cfg.host, cfg.port, max_size=None
        )
        # 后台预热（不阻塞服务器启动）
        asyncio.create_task(self._warmup())
        ip = get_lan_ip()
        print(f"小D网关已启动：")
        print(f"  本机   ws://127.0.0.1:{cfg.port}{cfg.ws_path}")
        print(f"  设备   ws://{ip}:{cfg.port}{cfg.ws_path}")
        print(f"  LLM={cfg.llm_model}  TTS={cfg.tts_voice}")
        print("按 Ctrl+C 停止")

    async def serve_forever(self):
        await self.start()
        await self._server.wait_closed()

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
