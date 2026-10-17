# -*- coding: utf-8 -*-
"""小D网关 WebSocket 服务器。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from urllib.parse import parse_qs

import websockets

from gateway.config import GatewayConfig
from gateway.providers.asr_sensevoice import SenseVoiceASR
from gateway.providers.llm_ollama import OllamaLLM
from gateway.providers.llm_openai import OpenAICompatLLM
from gateway.providers.tts_router import TTSRouter
from gateway.providers.vad_silero import SileroVAD
from gateway.session import VoiceSession


def _make_llm(cfg: GatewayConfig):
    """按配置构造语言模型后端。

    ollama —— 本地 Ollama（默认，离线可用）
    openai —— OpenAI 兼容在线端点：DeepSeek / 阿里百炼 / 智谱 ChatGLM /
              Kimi / 硅基流动 / OpenAI / 自建聚合网关，都是同一套协议。

    在线后端缺少 key 或 base_url 时**不抛异常**，直接退回本地 Ollama——
    配置写了一半不该让整个网关起不来，宁可慢也不能哑。
    """
    backend = str(getattr(cfg, "llm_backend", "ollama") or "ollama").lower()
    if backend == "openai":
        base = str(getattr(cfg, "llm_api_base", "") or "").strip()
        if base:
            llm = OpenAICompatLLM(base, getattr(cfg, "llm_api_key", "") or "",
                                  cfg.llm_model)
            print(f"[gateway] 语言模型后端：OpenAI 兼容 {base} "
                  f"（模型 {cfg.llm_model}）", flush=True)
            return llm
        print("[gateway] 语言模型后端选了在线但未填接口地址，退回本地 Ollama",
              flush=True)
    llm = OllamaLLM(cfg.ollama_base_url, cfg.llm_model)
    print(f"[gateway] 语言模型后端：本地 Ollama {cfg.ollama_base_url} "
          f"（模型 {cfg.llm_model}）", flush=True)
    return llm


def _pid_alive(pid: int) -> bool:
    """宿主进程是否还活着（Windows 用 OpenProcess 探测，避免误判）。"""
    if pid <= 0:
        return True
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    except Exception:  # noqa: BLE001
        return True   # 探测不了就别误杀


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
        self._http = None
        self._mqtt = None
        self._lock = asyncio.Lock()
        self._sessions = 0            # 活跃会话数（空闲自动退出用）
        self._started_at = time.monotonic()
        self._last_activity = time.monotonic()
        # providers / 离线模型的加载状态（供 /healthz 自报，界面显示"模型已加载"）
        self._status = {"providers": False, "llm": False, "asr": False,
                        "vad": False, "tts": False, "warm": False,
                        "error": ""}
        # DeskWork 拉起网关时会设 XIAOD_IDLE_EXIT_S：无会话空闲超过该秒数
        # （且启动超过 60s 宽限期）就自动退出，避免 DeskWork 被强杀/异常退出
        # 后网关变成孤儿进程一直占着端口和内存。手动启动时不设此变量，永不退出。
        self._idle_exit_s = float(os.environ.get("XIAOD_IDLE_EXIT_S", "0") or 0)
        # 宿主应用 PID：应用（独立 exe）拉起网关时会带上，应用一旦消失
        # （正常退出或被强杀）网关立刻自杀，绝不会变成孤儿常驻。
        try:
            self._parent_pid = int(os.environ.get("XIAOD_PARENT_PID", "0") or 0)
        except ValueError:
            self._parent_pid = 0

    async def _init_providers(self):
        cfg = self.config
        async with self._lock:
            if self._llm is not None:
                return
            try:
                self._llm = _make_llm(cfg)
                # 离线模型（SenseVoice ASR + Silero VAD）在这里就把权重读进
                # 内存——设备/本机第一句话不用等冷加载
                self._asr = SenseVoiceASR(
                    cfg.sensevoice_dir, cfg.uplink_sample_rate,
                    language=getattr(cfg, "asr_language", "auto"),
                )
                self._vad = SileroVAD(
                    cfg.silero_dir, cfg.vad_threshold, cfg.vad_threshold_low,
                    cfg.min_silence_ms, cfg.uplink_sample_rate,
                )
                self._tts = TTSRouter(
                    cfg.tts_voice, cfg.downlink_sample_rate,
                    cfg.downlink_frame_size,
                    engine=getattr(cfg, "tts_engine", "auto"),
                )
                self._status.update({"providers": True, "llm": True,
                                     "asr": True, "vad": True, "tts": True,
                                     "error": ""})
                print("[gateway] 离线模型已加载："
                      "SenseVoice ASR + Silero VAD + TTS", flush=True)
            except Exception as e:  # noqa: BLE001
                # 加载失败不要"半初始化"：全部清空，下次连接再试一次，
                # 同时把原因留给 /healthz（界面据此显示"离线模型缺失"）
                self._llm = self._asr = self._vad = self._tts = None
                self._status["error"] = f"{type(e).__name__}: {e}"
                print(f"[gateway] 离线模型加载失败：{self._status['error']}",
                      flush=True)
                raise

    @staticmethod
    def _silence_handshake_noise():
        """把"握手失败"的堆栈压成一行限流告警。

        端口探活（应用/服务脚本/端口扫描）只是连一下或发个普通 HTTP，
        websockets 会为每种情况抛不同异常并打三层堆栈链
        （InvalidMessage / ConnectionClosedError / EOFError…）。这些都不是
        真故障，日志里留一行就够；真正的业务校验（路径、device-id）在
        handler 里，失败会带明确的 close code。"""
        try:
            _noisy = (websockets.exceptions.InvalidMessage,
                      websockets.exceptions.ConnectionClosedError,
                      websockets.exceptions.ConnectionClosedOK)
        except Exception:  # noqa: BLE001
            _noisy = ()
        state = {"last": 0.0}

        class _Filter(logging.Filter):
            def filter(self, record):
                exc = record.exc_info[1] if record.exc_info else None
                msg = record.getMessage().lower()
                hit = ("handshake" in msg and exc is not None
                       and isinstance(exc, Exception))
                if not hit and _noisy and isinstance(exc, _noisy):
                    hit = True
                if not hit:
                    return True
                now = time.time()
                if now - state["last"] >= 5.0:
                    state["last"] = now
                    print("[gateway] 忽略非法握手/端口探活连接"
                          "（日志已折叠）", flush=True)
                return False

        # 13.x 与 14.x 记在不同 logger 上，都挂上
        for name in ("websockets.server", "websockets.protocol",
                     "websockets.asyncio.server"):
            lg = logging.getLogger(name)
            if not any(isinstance(f, _Filter) for f in lg.filters):
                lg.addFilter(_Filter())

    def _start_http(self):
        """启动 OTA / 视觉 HTTP 端点（独立线程，失败不影响 WS 网关）。"""
        cfg = self.config
        want = (bool(getattr(cfg, "ota_enabled", True))
                or bool(getattr(cfg, "vision_enabled", False)))
        if not want or int(getattr(cfg, "http_port", 0) or 0) <= 0:
            return
        try:
            from gateway.http.httpd import GatewayHTTP
            app = GatewayHTTP(cfg)
            if getattr(cfg, "ota_enabled", True):
                from gateway.http.ota import OTAHandler
                OTAHandler(cfg, get_lan_ip).register(app)
            if getattr(cfg, "vision_enabled", False):
                from gateway.http.vision import VisionHandler
                VisionHandler(cfg).register(app)
            if app.start():
                self._http = app
        except Exception as e:  # noqa: BLE001
            print(f"[gateway] HTTP 端点启动失败（不影响语音）："
                  f"{type(e).__name__}: {e}", flush=True)

    def _health(self) -> dict:
        """网关自报状态（GET /healthz）：绑定地址 + 离线模型就绪情况。"""
        cfg = self.config
        st = self._status
        return {
            "ok": True, "app": "xiaod-gateway",
            "host": cfg.host, "port": cfg.port, "ws_path": cfg.ws_path,
            "sessions": self._sessions,
            "ready": bool(st["providers"]),
            "warm": bool(st["warm"]),
            "models": {"llm": st["llm"], "asr": st["asr"],
                       "vad": st["vad"], "tts": st["tts"]},
            "error": st["error"],
            "lan_ip": get_lan_ip(),
            "uptime_s": round(time.monotonic() - self._started_at, 1),
        }

    async def _process_request(self, connection, request):
        """普通 HTTP 请求：/healthz 回状态 JSON，其余（端口探活）回 400。

        仅新版（websockets.asyncio.server）会调用本方法；老版 legacy 实现
        没有等价物，那边靠日志过滤器兜底。"""
        try:
            path = getattr(request, "path", "") or ""
            if path.split("?")[0].rstrip("/") in ("/healthz", "/health"):
                import json as _json
                body = _json.dumps(self._health(), ensure_ascii=False)
                return connection.respond(200, body + "\n")
            if request.headers.get("Upgrade", "").lower() != "websocket":
                return connection.respond(400, "websocket only\n")
        except Exception:  # noqa: BLE001
            pass
        return None

    async def handler(self, ws, *args):
        # 解析路径
        req = getattr(ws, "request", None)
        path = getattr(req, "path", "")
        if args and isinstance(args[0], str):
            path = args[0]
        qs = path.split("?", 1)[1] if "?" in path else ""
        try:
            peer = str(getattr(ws, "remote_address", "") or "")
        except Exception:  # noqa: BLE001
            peer = ""

        # 路径校验
        if not path.startswith(self.config.ws_path):
            # 明确打出原因：设备侧只会显示"连接失败"，没有日志就只能靠猜
            print(f"[gateway] 拒绝连接（{peer}）：路径不对 {path!r}，"
                  f"应为 {self.config.ws_path}", flush=True)
            await ws.close(code=1008, reason="not found")
            return

        # device-id 校验（header 或 query）
        device_id = ""
        client_id = ""
        if req is not None:
            device_id = req.headers.get("device-id", "")
            client_id = req.headers.get("client-id", "")
        if not device_id and qs:
            parsed = parse_qs(qs)
            device_id = (parsed.get("device-id") or [""])[0]
            client_id = client_id or (parsed.get("client-id") or [""])[0]
        if not device_id:
            print(f"[gateway] 拒绝连接（{peer}）：缺少 device-id（设备固件应带 "
                  f"Device-Id 头）", flush=True)
            await ws.close(code=1008, reason="missing device-id")
            return

        # 设备鉴权（默认关）：开了之后要求 Bearer token
        if getattr(self.config, "auth_enabled", False):
            from gateway.auth import verify_ws_auth
            token = ""
            if req is not None:
                token = req.headers.get("authorization", "")
            if not verify_ws_auth(self.config, device_id, token):
                print(f"[gateway] 拒绝连接（{peer}）：鉴权失败 device={device_id}",
                      flush=True)
                await ws.close(code=1008, reason="unauthorized")
                return

        print(f"[gateway] 设备接入：id={device_id} from={peer}", flush=True)
        await self._init_providers()
        session = VoiceSession(
            ws, self.config, self._llm, self._asr, self._tts, self._vad,
            device_id=device_id, client_id=client_id,
        )
        self._sessions += 1
        self._last_activity = time.monotonic()
        try:
            await session.run()
        finally:
            self._sessions -= 1

    async def _warmup(self):
        """后台初始化 providers（=加载离线模型）并让 Ollama 预加载模型。

        这一步在网关启动时就跑，设备/本机第一句话因此不用等冷加载。
        失败不致命（例如 Ollama 还没起），原因写进 /healthz 供界面提示。"""
        try:
            await self._init_providers()
        except Exception as e:  # noqa: BLE001
            self._status["error"] = f"{type(e).__name__}: {e}"
            return
        try:
            async for _ in self._llm.chat(
                [{"role": "user", "content": "你好"}]
            ):
                pass
            self._status["warm"] = True
        except Exception as e:  # noqa: BLE001
            print(f"[gateway] 大模型预热失败（不影响启动）：{e!r}", flush=True)

    async def _keep_warm(self):
        """两件事：Ollama 模型保活 + 无会话空闲自动退出。

        保活：Ollama 默认 keep_alive 仅 5 分钟，闲置后模型被逐出，下一句就要
        冷加载数 GB（首句延迟数十秒，甚至可能拖垮 keepalive）。每 2 分钟发一次
        最小请求把模型按在内存里。

        空闲退出：见 __init__ 里 XIAOD_IDLE_EXIT_S 的说明。"""
        last_keep = 0.0
        while True:
            await asyncio.sleep(2)
            now = time.monotonic()
            if self._parent_pid and not _pid_alive(self._parent_pid):
                print("[gateway] 宿主应用已退出，网关随之停止", flush=True)
                os._exit(0)
            if (self._idle_exit_s > 0 and self._sessions <= 0
                    and now - self._started_at > 60
                    and now - self._last_activity >= self._idle_exit_s):
                print(f"[gateway] 无会话空闲 {self._idle_exit_s:.0f}s，自动退出",
                      flush=True)
                os._exit(0)
            if now - last_keep >= 120:
                last_keep = now
                try:
                    if self._llm is not None:
                        await self._llm.keepalive()
                except Exception:  # noqa: BLE001
                    pass

    def _start_mqtt_udp(self):
        """按配置启动 MQTT+UDP 传输（第二种传输，可选）。

        mqtt_gateway 未配置 / 未装 paho-mqtt → 直接返回，网关照常以 WS 工作。"""
        cfg = self.config
        endpoint = str(getattr(cfg, "mqtt_gateway", "") or "").strip()
        if not endpoint:
            return
        try:
            from gateway.transport.mqtt import (MqttControlChannel,
                                                UdpAudioEndpoint)
        except Exception as e:  # noqa: BLE001
            print(f"[gateway] MQTT 传输不可用：{type(e).__name__}: {e}", flush=True)
            return
        loop = asyncio.get_event_loop()
        chan = MqttControlChannel(cfg)
        if not chan.start(loop):
            return
        chan.on_message = lambda kind, mac, payload: asyncio.ensure_future(
            self._on_mqtt_message(chan, mac, payload))
        self._mqtt = chan
        print(f"[gateway] MQTT 传输已启用（{endpoint}）", flush=True)

    async def _on_mqtt_message(self, chan, mac: str, payload: bytes):
        """设备经 MQTT 发来的控制消息：hello 时建会话 + 下发 UDP 通道配置。"""
        try:
            msg = json.loads(payload.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return
        if msg.get("type") != "hello":
            # 其它控制消息由该设备已有会话处理（简化：只处理 hello，
            # 完整 IM 会话编排在 MQTT 通道下留待后续接入）
            return
        await self._init_providers()
        from gateway.transport.mqtt import UdpAudioEndpoint
        from gateway.transport.base import QueueTransport, subscribe_topic
        ep = UdpAudioEndpoint(self.config)
        await ep.start(asyncio.get_event_loop())
        transport = QueueTransport()
        session = VoiceSession(
            transport, self.config, self._llm, self._asr, self._tts, self._vad,
            device_id=mac)
        # 下行：文本经 MQTT、二进制经 UDP
        topic = subscribe_topic(mac)

        async def _send(data):
            if isinstance(data, (bytes, bytearray)):
                ep.send_audio(bytes(data))
            else:
                chan.publish(topic, data if isinstance(data, str) else str(data))
            return True

        transport.send = _send          # type: ignore[assignment]
        ep.on_audio = lambda opus: transport.feed(opus)
        # 服务端 hello 带 udp 配置
        hello = json.loads(json.dumps(msg))
        hello["type"] = "hello"
        chan.publish(topic, json.dumps({
            "type": "hello", "session_id": session.session_id,
            "version": int(msg.get("version", 3)),
            "transport": "udp",
            "udp": ep.hello_block(get_lan_ip()),
            "audio_params": {"format": "opus", "sample_rate":
                             self.config.downlink_sample_rate,
                             "channels": 1, "frame_duration":
                             self.config.frame_duration_ms},
        }))
        self._sessions += 1
        try:
            await session.run()
        finally:
            self._sessions -= 1
            ep.close()

    async def start(self):
        cfg = self.config
        self._silence_handshake_noise()
        # 用新版 asyncio 实现（websockets>=13 的 websockets.asyncio.server），
        # 而不是顶层 websockets.serve —— 后者在 13.x 还是 legacy 实现，
        # handler/process_request 签名都不同；显式引新版可让同一份代码在
        # 13.x 与 14.x 上行为一致（打包进独立 exe 时用的是系统 3.14 + 13.1）。
        try:
            from websockets.asyncio.server import serve as _ws_serve
        except Exception:  # noqa: BLE001
            _ws_serve = websockets.serve
        kwargs = dict(max_size=None, ping_interval=20, ping_timeout=60)
        try:
            # ping_timeout 放到 60s：模型冷加载/系统卡顿时事件循环可能超过
            # 20s 无响应，默认 20s 会把还活着的连接误杀（1011 keepalive timeout）。
            self._server = await _ws_serve(
                self.handler, cfg.host, cfg.port,
                process_request=self._process_request, **kwargs)
        except TypeError:
            # 兜底：老实现不认 process_request 的这种签名
            self._server = await _ws_serve(self.handler, cfg.host, cfg.port,
                                           **kwargs)
        # 后台预热（不阻塞服务器启动）
        asyncio.create_task(self._warmup())
        # 模型保活（不阻塞）
        asyncio.create_task(self._keep_warm())
        ip = get_lan_ip()
        lan_ok = cfg.host not in ("127.0.0.1", "localhost", "::1")
        # HTTP 端点（OTA / 视觉）—— 独立线程，失败不致命
        self._start_http()
        # MQTT+UDP（可选第二种传输）
        self._start_mqtt_udp()
        print(f"小D网关已启动：")
        print(f"  本机   ws://127.0.0.1:{cfg.port}{cfg.ws_path}")
        if lan_ok:
            print(f"  设备   ws://{ip}:{cfg.port}{cfg.ws_path}")
        else:
            print(f"  [警告] 仅监听本机（host={cfg.host}）：ESP32 等设备将无法"
                  f"连接。把配置里的 host 改成 0.0.0.0 后重启网关。")
        print(f"  LLM={cfg.llm_model}  TTS={cfg.tts_voice}")
        print("按 Ctrl+C 停止")

    async def serve_forever(self):
        await self.start()
        await self._server.wait_closed()

    async def stop(self):
        if self._mqtt is not None:
            self._mqtt.stop()
            self._mqtt = None
        if self._http is not None:
            self._http.stop()
            self._http = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
