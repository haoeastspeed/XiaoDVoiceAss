# -*- coding: utf-8 -*-
"""会话状态机 + 全双工实时编排（自动断句、流式 LLM/TTS、随时打断）。"""
from __future__ import annotations

import asyncio
import contextlib
import difflib
import json
import re
import time
import uuid
from typing import AsyncIterator, Optional

import websockets

from gateway import protocol
from gateway.audio.opus import UplinkDecoder, DownlinkDecoder
from gateway.config import GatewayConfig
from gateway.providers.base import BaseASR, BaseLLM, BaseTTS, BaseVAD
from gateway.providers.tts_edge import official_voices
from gateway.providers.tts_edge import known_voice
from gateway.providers.websearch import as_context as web_search_as_context
from gateway.providers.websearch import looks_weather as web_looks_weather
from gateway.providers.websearch import search as web_search
from gateway.providers.websearch import weather as web_weather
from gateway.tools.builtin import build_builtin_tools
from gateway.tools.device import DeviceMCPClient
from gateway.tools.loop import ToolCallLoop
from gateway.tools.registry import ToolRegistry
from gateway.tools.types import ToolDef, ToolResult

# 句末标点（强切）与逗号（长句缓冲上限时软切）
_SENT_END = re.compile(r"[。！？!?；;\n]")
_COMMA = re.compile(r"[，,]")
_MAX_CHUNK = 35
_BARGE_KEEP_BYTES = 16000  # 打断时保留约 0.5s 开头
_BARGE_CONFIRM_FRAMES = 5  # 打断前需连续几帧 VAD 判语音（5×60ms=300ms）
_MIN_UTT_MS = 500          # 最短语音时长：低于此判为噪声/误触，不送 ASR
_MIN_SPEECH_MS = 400       # 本回合内 VAD 判语音的累计时长下限（绝对值，不是占比）
_LEAD_SILENCE_MS = 3000    # 开口前的静音超过这么久就丢弃（只保留一点前置音）
_PRE_ROLL_MS = 300         # 丢弃前置静音时保留的引导音，避免吃掉第一个字
_MAX_UTT_MS = 20000        # 兜底：单回合最长 20s，到点必送（别把用户的话憋死）
_TTS_FRAME_TIMEOUT_S = 20.0  # TTS 单帧看门狗（秒）：EdgeTTS 偶发挂起，不能钉死会话

# 旧实现用"语音帧 / 回合总时长"的占比做门限，而回合总时长是从 listen start
# 起算的（含开口前的全部静音）：用户沉默 10s 后说 2s → 占比 0.17 < 0.35，
# 整句被判噪声丢弃 —— 表现就是"显示正在聆听，说话永远没反应"。
# 现在改为"累计语音时长"绝对值判据，与等待多久无关。

# 自播放回声兜底：与最近说过的话比对的归一化字符集
_PUNCT = re.compile(r"[\s，。！？、,.!?；;：:\"'“”‘’()（）\[\]【】\-—_~…·]+")


def _norm_text(text: str) -> str:
    """归一化（去空白/标点），用于自回声相似度比对。"""
    return _PUNCT.sub("", text or "")


# 唤醒词文本（detect 的 text 只是唤醒词本身时，是"激活通知"而非用户话语）。
# 固件默认"你好小智"；可被 config.wake_words 覆盖。
_WAKE_WORDS_DEFAULT = ("你好小智", "你好小志", "你好小驰", "小智", "小志",
                       "小驰", "hi esp", "嘿你好呀", "嗨小智")
_WAKE_TEXTS_NORM = frozenset(
    _norm_text(w).lower() for w in _WAKE_WORDS_DEFAULT)


def _is_wake_word_text(text: str) -> bool:
    """detect 文本是否只是唤醒词本身（归一化后精确比对）。"""
    key = _norm_text(text).lower()
    return key in _WAKE_TEXTS_NORM


def _rms(pcm: bytes) -> float:
    """一帧 s16le 的均方根（诊断日志用：判断麦克风有没有真在收音）。"""
    import numpy as np
    x = np.frombuffer(pcm, dtype=np.int16)
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(x.astype(np.float32)))))


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


_THINK_BLOCK = re.compile(r"<think>.*?</think>|<\|think\|>.*?<\|end\|>", re.S)


def _strip_think(text: str) -> str:
    """剥掉模型混进正文的思考过程。

    带 thinking 能力的模型（qwen3、部分 GLM）即使显式关掉 think，偶尔还是
    会把"让我先分析一下…"写进 content。这类内容进摘要会污染后续上下文，
    进家居意图判断会让 JSON 解析失败，所以在所有"内部小任务"的输出上都剥一层。
    """
    if not text:
        return ""
    return _THINK_BLOCK.sub("", text).strip()


class VoiceSession:
    def __init__(self, ws, config: GatewayConfig,
                 llm: BaseLLM, asr: BaseASR, tts: BaseTTS, vad: BaseVAD,
                 device_id: str = "", client_id: str = ""):
        self.ws = ws
        self.config = config
        self.llm = llm
        self.asr = asr
        self.tts = tts
        self.vad = vad
        self.device_id = device_id
        self.client_id = client_id

        self.session_id = uuid.uuid4().hex
        self.voice = ""       # 本会话的音色（共享 TTS 单例，开口前对齐一次）
        self.state = "idle"  # idle/listening/thinking/speaking
        self.messages = [{"role": "system", "content": config.system_prompt}]
        self._tasks: set = set()  # 后台小任务（如在线音色表预热）

        self._collecting = False
        self.listen_mode = "auto"
        self._utterance = bytearray()
        self._barge = bytearray()
        self._barge_streak = 0    # 连续判语音的帧数（打断确认用）
        self._speech_ms = 0.0     # 本回合累计：VAD 判语音的时长（ms）
        self._utt_ms = 0.0        # 本回合累计：总时长（ms）
        self._trail_ms = 0.0      # 句尾连续静音时长（ms）：服务端自己的断句依据
        self._barge_until = 0.0   # 插话保护窗：期内 listen start 不清已捕获的开口语音
        self._last_abort_ts = 0.0  # 最近一次 abort 的时刻（区分真打断与兜底重发）
        self._last_spoken_ts = 0.0  # 最近一句 TTS 播完的时刻（自激活唤醒判定用）
        self._up_frames = 0       # 上行帧计数（诊断日志）
        self._listen_dup = 0      # 被忽略的重复 listen start 条数（诊断日志）
        # 语音段起止（字节偏移，s16le）：按停顿时长给识别结果补标点用
        self._spans: list[tuple[int, int]] = []
        self._in_speech = False
        self._span_start = 0
        self.vad_state = vad.new_state()

        self._decoder = UplinkDecoder(
            config.uplink_sample_rate, config.uplink_frame_size
        )
        self._response_task: Optional[asyncio.Task] = None
        self._closed = False
        # 自播放回声兜底：最近播报过的句子 (时间, 文本)
        self._spoken: list[tuple[float, str]] = []

        # ---- 记忆模式 ----
        self._summary = ""            # 本地总结模式下累积的历史摘要
        self._summary_llm = None      # 压缩专用的小模型（可选）

        # ---- 智能家居 ----
        self._ha = None
        if getattr(config, "ha_enabled", False):
            with contextlib.suppress(Exception):
                from gateway.providers.homeassistant import HomeAssistant
                self._ha = HomeAssistant(
                    getattr(config, "ha_base_url", ""),
                    getattr(config, "ha_token", ""),
                )

        # ---- 自动休眠 ----
        self._last_active = time.monotonic()
        self._sleeping = False
        # ---- 回声门（第一道：播放期/尾音期不吃上行）----
        # 设备是单麦无回采硬件，播放时麦克风会把自己的扬声器上行上来。
        # 闸门 = 播报期间不跑 VAD / 不累积 / 不切 listening，从源头掐掉
        # "被自己的回复唤醒"。详见 gateway/audio/aec.py 模块注释。
        self._echo_gate = bool(getattr(config, "echo_gate", True))
        self._gate_until = 0.0      # 尾音冷却窗结束时刻（monotonic）
        self._gate_active = False   # 上一帧是否被闸门拦下（放行时复位 VAD）
        self._gate_dropped = 0      # 闸门丢弃的上行帧数（诊断）
        self._gate_barges = 0       # AEC 判到的真打断次数（诊断）
        # ---- 回声消除（第二道：用自身 TTS 当参考，把回声减掉）----
        self._aec = self._make_aec()
        # far 参考必须是 PCM：下行帧是 opus 包，喂进 AEC 前先解码。
        # 只在 AEC 可用时才建（零 AEC 场景零开销）。
        self._down_decoder = DownlinkDecoder() if self._aec is not None else None
        # 本轮注入的联网检索上下文：(消息下标, 原始内容)，响应结束即还原
        self._search_restore: Optional[tuple[int, str]] = None
        # TTS 在线/本地状态（变化时给用户一条提示，免得"音色自己变了"摸不着头脑）
        self._tts_degraded = False

        # ---- MCP / 工具生态 ----
        # 三源合一：内置 self.gateway.* / 设备侧 MCP（对端）/ MCP endpoint
        self.tools = ToolRegistry()
        for tdef in build_builtin_tools(self):
            self.tools.register(tdef)
        self._device_mcp: Optional[DeviceMCPClient] = None
        self._endpoint_mgr = None
        self._mcp_enabled = bool(getattr(config, "mcp_enabled", True))
        # ---- 说话人识别（声纹）----
        self._voiceprint = None
        self._introduced_speakers: set = set()
        if getattr(config, "voiceprint_enabled", False):
            with contextlib.suppress(Exception):
                from gateway.providers.voiceprint import from_config as _vp
                self._voiceprint = _vp(
                    config, logger=lambda m: print(f"[voiceprint] {m}", flush=True))
                if self._voiceprint is not None:
                    print("[voiceprint] 说话人识别已启用", flush=True)
        # 协议版本（1=裸 opus / 2=带时间戳头 / 3=简化头）与对端能力声明
        self._proto_version = 1
        self._client_features: dict = {}
        self._codec = None            # binframe.codec.FrameCodec（懒建）
        self._down_ts_ms = 0          # 下行帧时间戳基准（v2 用）
        self._pending_speaker = ""    # 本回合声纹识别到的说话人（下轮响应注入）
        # ---- 端到端 Realtime（P2-D）----
        self._realtime = None
        self._realtime_task: Optional[asyncio.Task] = None

    # ---------- 主循环 ----------

    async def run(self):
        # 预热在线音色表：edge-tts 官方有 300+ 音色且会增删，这里拉一次让后续
        # set_voice 的合法性校验用得上全量列表（失败就用 config 里的精选）。
        self._tasks.add(asyncio.create_task(self._warm_voices()))
        # MCP endpoint（第三方 server）：后台并发连接，失败只告警不影响会话
        if self._mcp_enabled and getattr(self.config, "mcp_endpoints", None):
            self._tasks.add(asyncio.create_task(self._init_endpoints()))
        try:
            async for raw in self.ws:
                # 单条消息处理失败不能杀掉整场会话：旧实现只捕获
                # ConnectionClosed，任何未预期异常都会让会话无声断开、
                # 日志里什么都没有 —— 这正是"用着用着就没反应"的共同根因。
                try:
                    if isinstance(raw, (bytes, bytearray)):
                        await self._on_binary(bytes(raw))
                    else:
                        await self._on_text(raw)
                except websockets.exceptions.ConnectionClosed:
                    raise
                except Exception as exc:  # noqa: BLE001
                    import traceback
                    print(f"[gateway] 处理消息异常（跳过该条，会话继续）："
                          f"{type(exc).__name__}: {exc}", flush=True)
                    traceback.print_exc()
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            for t in list(self._tasks):
                t.cancel()
            await self._cleanup()

    async def _warm_voices(self):
        try:
            await official_voices()
        except Exception:  # noqa: BLE001
            pass

    async def _cleanup(self):
        self._closed = True
        task = self._response_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except Exception:
                pass
        # MCP：关掉设备客户端与 endpoint 连接
        if self._device_mcp is not None:
            with contextlib.suppress(Exception):
                await self._device_mcp.close()
        if self._endpoint_mgr is not None:
            with contextlib.suppress(Exception):
                await self._endpoint_mgr.close()
        if self._realtime is not None or self._realtime_task is not None:
            with contextlib.suppress(Exception):
                await self._exit_realtime()
        if self._aec is not None:
            with contextlib.suppress(Exception):
                print(f"[echo] 会话结束 {self.session_id[:8]}："
                      f"AEC {self._aec.stats()}，闸门丢弃 {self._gate_dropped} 帧，"
                      f"AEC 判打断 {self._gate_barges} 次", flush=True)
            self._aec.reset()

    # ---------- 回声门（防自激活）----------

    def _make_aec(self):
        """按配置构造本会话的 AEC；不可用时返回 None（退化为纯闸门）。"""
        mode = str(getattr(self.config, "echo_cancel", "auto") or "auto").lower()
        if mode == "off":
            print("[echo] AEC 已禁用（echo_cancel=off）：只用硬闸门", flush=True)
            return None
        try:
            from gateway.audio.aec import EchoCanceller
        except Exception as e:  # noqa: BLE001
            print(f"[echo] AEC 不可用（{type(e).__name__}: {e}），"
                  f"退化为纯闸门（播放期全部丢弃）", flush=True)
            return None
        aec = EchoCanceller(
            sample_rate=self.config.uplink_sample_rate,
            far_rate=self.config.downlink_sample_rate,
            delay_ms=int(getattr(self.config, "echo_aec_delay_ms", 180)),
        )
        if not aec.available:
            print(f"[echo] AEC 初始化失败（{aec.error}），"
                  f"退化为纯闸门（播放期全部丢弃）", flush=True)
            return None
        print(f"[echo] AEC 就绪：近端 {self.config.uplink_sample_rate}Hz / "
              f"远端参考 {self.config.downlink_sample_rate}Hz，"
              f"延迟初值 {aec.stream_delay_ms}ms", flush=True)
        return aec

    def _gate_on(self) -> bool:
        """闸门是否生效：正在播报，或还在 tts_stop 后的尾音冷却窗里。"""
        if not self._echo_gate:
            return False
        if self.state == "speaking":
            return True
        return time.monotonic() < self._gate_until

    def _open_cooldown(self):
        """tts_stop 之后留一段静默窗。

        服务端认为"播完了"和"设备真的播完了"之间还有一段：设备端 opus
        解码/播放队列 + 扬声器到麦克风的空气延迟 + 房间混响。这段时间里
        上行仍然全是自己的声音，不能开始识别。
        """
        ms = int(getattr(self.config, "echo_gate_cooldown_ms", 700) or 0)
        if ms > 0:
            self._gate_until = time.monotonic() + ms / 1000.0

    def _clear_cooldown(self):
        """用户已明确表态要说话（abort / 手动聆听 / 确认真打断）：
        尾音冷却立刻作废，别再用"防回声"的名义吞掉他的第一句。"""
        self._gate_until = 0.0

    async def _on_gated(self, pcm: bytes):
        """闸门期的一帧上行音频。

        默认什么都不做 —— 不跑 VAD、不累积、不切状态。这就是"防自激活"的
        核心：播放期网关侧根本不认为有人在说话，于是不会有 barge-in、不会
        触发 ASR、更不会生成新一轮回复。

        唯一例外：AEC 在位（回声已被抹掉）时，剩下的能量只可能是用户真在
        说话，此时放行用于**语音打断**。关掉 `echo_gate_barge` 可退回
        "播放期完全不判打断、只认 UI/按键打断"。
        """
        self._gate_dropped += 1
        aec = self._aec
        if (aec is None or not aec.available or not aec.active
                or not bool(getattr(self.config, "echo_gate_barge", True))):
            return
        # 冷却期不判打断：那时回复已经取消/结束，正在往 listening 走，
        # 打断没有意义（只会把开口音再塞进 _barge）。
        if self.listen_mode != "auto" or self.state != "speaking":
            return
        clean = aec.process(pcm)
        self._barge.extend(clean)
        if len(self._barge) > _BARGE_KEEP_BYTES:
            del self._barge[:len(self._barge) - _BARGE_KEEP_BYTES]
        speaking, _ = self.vad.process(self.vad_state, clean)
        if not speaking:
            self._barge_streak = 0
            return
        self._barge_streak += 1
        if self._barge_streak < _BARGE_CONFIRM_FRAMES:
            return
        self._gate_barges += 1
        print(f"[echo] AEC 确认打断（{self._barge_streak} 帧连续语音）",
              flush=True)
        await self._commit_barge()

    async def _commit_barge(self):
        """打断已确认：取消当前回复，并把开口语音转成下一轮的输入。"""
        self._clear_cooldown()
        await self._cancel_response()
        self._collecting = True
        self._utterance = bytearray(self._barge)
        self._barge = bytearray()
        self._barge_streak = 0
        # 已确认 300ms 语音，直接放行占比检查
        self._speech_ms = self._utt_ms = float(
            _BARGE_CONFIRM_FRAMES * self.config.frame_duration_ms)
        self._trail_ms = 0.0
        # 打断捕获到的都是开口语音：整段算一个语音段
        self._spans = [(0, len(self._utterance))]
        self._in_speech = True
        self.state = "listening"
        # 2s 保护窗：客户端收到 tts_stop 后会兜底重发 listen start，
        # 那时不能清掉刚捕获的开口语音（否则打断时第一句被丢）
        self._barge_until = time.monotonic() + 2.0

    # ---------- 发送 ----------

    async def _send(self, data) -> bool:
        if self._closed:
            return False
        # 下行协议版本 >1 时，音频帧要按协商的二进制头封装（v1 = 裸 opus）。
        # 注意：喂给 AEC 的始终是**未封装的原始负载**，保持与既有行为一致，
        # 否则 far 参考流会被协议头污染。
        out = data
        if (isinstance(data, (bytes, bytearray)) and self._codec is not None
                and self._codec.version > 1):
            out = self._codec.encode_audio(bytes(data), self._next_down_ts())
        try:
            await self.ws.send(out)
        except Exception:
            return False
        # 真正发出去的音频帧才是 AEC 的"远端参考"：以发出的字节为准，
        # 而不是以生成出来的为准（生成失败/发送失败的不该进参考流）。
        # 下行帧是 opus 压缩包，AEC3 需要的是真实播放的 PCM——先解码再喂，
        # 否则 opus 字节被当 int16 采样解释，far 流与回声不相关，AEC 不收敛。
        if self._aec is not None and isinstance(data, (bytes, bytearray)):
            pcm_far = (self._down_decoder.decode(bytes(data))
                       if self._down_decoder is not None else bytes(data))
            if pcm_far:
                self._aec.feed_far(pcm_far)
        return True

    def _next_down_ts(self) -> int:
        """下行帧时间戳（毫秒，v2 用；单调递增）。"""
        self._down_ts_ms += self.config.frame_duration_ms
        return self._down_ts_ms

    async def _send_custom(self, action: str, **fields):
        """发一条自定义控制消息给客户端（内置工具操作客户端行为时用）。"""
        msg = {"type": "custom", "action": action}
        msg.update(fields)
        await self._send(json.dumps(msg, ensure_ascii=False))

    def _persist_override(self, key: str, value):
        """把运行时覆盖写盘（.tmp/gw_overrides.json），重启后经 load_config 生效。"""
        import json
        import os

        try:
            from gateway.config import ROOT
            path = os.path.join(ROOT, ".tmp", "gw_overrides.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            data = {}
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            data[key] = value
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---------- 控制消息 ----------

    async def _on_text(self, raw: str):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        # 客户端可能发来非对象 JSON（如 123 / [] / "x"）：不校验的话
        # msg.get 直接抛 AttributeError，整场会话静默死亡。
        if not isinstance(msg, dict):
            return
        t = msg.get("type")
        self._touch()          # 任何控制消息都算一次交互（唤醒 / 重置空闲计时）
        if t == "hello":
            await self._on_hello(msg)
        elif t == "listen":
            await self._on_listen(msg)
        elif t == "abort":
            self._last_abort_ts = time.monotonic()
            # realtime 会话：abort = 取消本轮出声
            if self._realtime is not None:
                self._clear_cooldown()
                with contextlib.suppress(Exception):
                    await self._realtime.cancel()
                self.state = "listening"
                return
            # 用户明确要求"停下，我要说话"：尾音冷却立刻作废
            self._clear_cooldown()
            await self._cancel_response()
            self._barge = bytearray()
            self._utterance = bytearray()
            self._barge_streak = 0
            self._speech_ms = 0.0
            self._utt_ms = 0.0
            self._trail_ms = 0.0
            self._spans = []
            self._in_speech = False
            self.vad.reset_sentence(self.vad_state)
            if self.listen_mode == "manual":
                self._collecting = False
            else:
                # auto 模式：abort 只表示"别再说了"，客户端仍在推流，
                # 必须继续采集，否则打断后用户再说就被静默丢弃。
                self._collecting = True
            self.state = "listening"
        elif t == "ping":
            await self._send(json.dumps({"type": "pong"}))
        elif t == "set_search":
            on = bool(msg.get("enabled"))
            self.config.web_search = on
            self._persist_override("web_search", on)
            print(f"[session] 联网搜索: {'开' if on else '关'}", flush=True)
        elif t == "set_model":
            # 热切换离线大模型（下一句生效），并持久化
            model = (msg.get("model") or "").strip()
            if model and model != getattr(self.llm, "model", None):
                try:
                    self.llm.model = model
                    self._persist_override("llm_model", model)
                    print(f"[session] LLM 模型切换: {model}", flush=True)
                except Exception as e:  # noqa: BLE001
                    print(f"[session] 模型切换失败: {e}", flush=True)
        elif t == "set_prompt":
            # 更新人设/系统提示词（保留对话上下文），并持久化
            prompt = (msg.get("prompt") or "").strip()
            if prompt:
                if self.messages and self.messages[0].get("role") == "system":
                    self.messages[0]["content"] = prompt
                else:
                    self.messages.insert(0, {"role": "system", "content": prompt})
                self._persist_override("system_prompt", prompt)
                print(f"[session] 人设已更新（{len(prompt)} 字）", flush=True)
        elif t == "clear_history":
            # 清空对话上下文，保留人设
            keep = [m for m in self.messages if m.get("role") == "system"][:1]
            self.messages = keep
            print("[session] 对话上下文已清空", flush=True)
        elif t == "set_voice":
            # 热切换 TTS 音色（下一句生效）。
            # 在线音色是 edge 的 voice id；离线音色是 "local:<SAPI token>"。
            #
            # 只有**显式声明 persist** 的连接才写盘。桌面应用是音色的权威
            # （界面选什么就是什么），它每次开局/切换都会带 persist=true；
            # 其它连接（设备、测试脚本、旧客户端）不带这个字段 → 不写盘，
            # 也就不会把用户选的音色永久改掉。
            #
            # 不写盘为什么重要：网关自己的 .tmp/gw_overrides.json 与应用的
            # config.json 一旦不一致，网关重启后先用自己那份、客户端连上又被
            # 同步成另一份 —— 重连一次换一次，用户原话就是"音色在两个之间切换"。
            voice = (msg.get("voice") or "").strip()
            persist = bool(msg.get("persist", False))
            if known_voice(voice):
                self.voice = voice          # 本会话记住自己的音色
                if hasattr(self.tts, "set_voice"):
                    self.tts.set_voice(voice)
                else:
                    self.tts.voice = voice
                if persist:
                    self._persist_override("tts_voice", voice)
                print(f"[session] TTS 音色切换: {voice}"
                      f"（会话 {self.session_id[:8]}，"
                      f"{'持久化' if persist else '仅本次'}）", flush=True)
            else:
                print(f"[session] 未知音色，忽略: {voice!r}", flush=True)
        elif t == "set_tts_engine":
            # auto / online / local
            engine = (msg.get("engine") or "").strip().lower()
            if engine in ("auto", "online", "local") and \
                    hasattr(self.tts, "set_engine"):
                self.tts.set_engine(engine)
                self.config.tts_engine = engine
                # 与 set_voice 同规则：只有权威客户端（桌面应用）显式声明
                # persist 才写盘，避免别的连接把用户的选择永久改掉。
                if msg.get("persist"):
                    self._persist_override("tts_engine", engine)
                print(f"[session] TTS 引擎: {engine}", flush=True)
        elif t == "set_language":
            # 识别语言（SenseVoice 原生支持 auto/zh/yue/en/ja/ko）
            lang = (msg.get("language") or "auto").strip().lower()
            fn = getattr(self.asr, "set_language", None)
            if callable(fn):
                got = fn(lang)
                self.config.asr_language = got
                if msg.get("persist"):
                    self._persist_override("asr_language", got)
                print(f"[session] 识别语言: {got}", flush=True)
        elif t == "set_memory":
            # 记忆模式：full=超长记忆 / summary=本地总结 / none=无记忆
            mode = (msg.get("mode") or "").strip().lower()
            if mode in ("full", "summary", "none"):
                self.config.memory_mode = mode
                if mode != "summary":
                    self._summary = ""      # 换模式就丢掉旧摘要，别串味
                if msg.get("persist"):
                    self._persist_override("memory_mode", mode)
                print(f"[session] 记忆模式: {mode}", flush=True)
        # iot：官方已废弃（websocket 文档 §8.5 建议全部走 MCP），保持"记录+忽略"。
        elif t == "iot":
            print("[session] 收到 iot 消息（官方已废弃，忽略）", flush=True)
        elif t == "mcp":
            await self._on_mcp(msg)
        else:
            # 未知消息类型：静默丢弃会让"设备发了但网关没反应"无迹可查
            print(f"[session] 未知消息类型，已忽略: type={t!r}", flush=True)

    # ---------- MCP ----------

    async def _on_mcp(self, msg: dict):
        """处理设备侧发来的 MCP 消息（JSON-RPC 响应）。"""
        if not self._mcp_enabled:
            return
        payload = msg.get("payload")
        if not isinstance(payload, dict):
            print("[mcp] 消息缺少 payload 或格式错误", flush=True)
            return
        if self._device_mcp is None:
            # 设备发来 MCP 消息但 hello 未声明 features.mcp —— 兜底建一个
            self._ensure_device_mcp()
        if self._device_mcp is not None:
            await self._device_mcp.handle_message(payload)

    def _ensure_device_mcp(self) -> Optional[DeviceMCPClient]:
        if self._device_mcp is not None:
            return self._device_mcp
        if not self._mcp_enabled:
            return None

        async def _send_env(env: dict) -> bool:
            return await self._send(json.dumps(env, ensure_ascii=False))

        self._device_mcp = DeviceMCPClient(
            _send_env,
            on_tools_changed=self._on_device_tools_changed,
            logger=lambda m: print(f"[mcp] {m}", flush=True),
        )
        return self._device_mcp

    async def _on_hello_mcp(self):
        """hello 声明 mcp=true 后：建客户端并发 initialize。"""
        client = self._ensure_device_mcp()
        if client is None:
            return
        await client.start(vision=self._vision_capability())

    def _vision_capability(self):
        """给设备的视觉上传地址（对齐官方 capabilities.vision{url,token}）。"""
        if not getattr(self.config, "vision_enabled", False):
            return None
        try:
            from gateway.server import get_lan_ip
            ip = get_lan_ip()
        except Exception:  # noqa: BLE001
            ip = "127.0.0.1"
        port = int(getattr(self.config, "http_port", 8003) or 8003)
        token = ""
        if getattr(self.config, "auth_enabled", False):
            with contextlib.suppress(Exception):
                from gateway.auth import make_manager
                token = make_manager(self.config).generate_token(
                    self.client_id or self.device_id, self.device_id)
        return {"url": f"http://{ip}:{port}/mcp/vision/explain",
                "token": token}

    def _on_device_tools_changed(self):
        """设备工具列表就绪：注册进统一工具表（同步回调，必须非阻塞）。"""
        if self._device_mcp is None:
            return
        # 先清掉旧的设备工具，再注册新的（设备重连/工具变更）
        self.tools.remove_by_source("device")
        for tdef in self._device_mcp.tool_defs():
            self.tools.register(tdef)
        print(f"[mcp] 已注册设备工具 {len(self._device_mcp.tools)} 个；"
              f"工具表共 {len(self.tools)} 个 {self.tools.sources()}", flush=True)

    async def _init_endpoints(self):
        """连接配置里的 MCP endpoint（第三方 server），注册其工具。"""
        try:
            from gateway.tools.endpoint import MCPEndpointManager
        except Exception as e:  # noqa: BLE001
            print(f"[mcp-endpoint] 不可用：{type(e).__name__}: {e}", flush=True)
            return
        servers = {}
        raw = getattr(self.config, "mcp_endpoints", None) or {}
        if isinstance(raw, dict):
            servers = raw.get("mcpServers", raw) or {}
        if not isinstance(servers, dict) or not servers:
            return
        self._endpoint_mgr = MCPEndpointManager(
            servers, logger=lambda m: print(f"[mcp-endpoint] {m}", flush=True))
        defs = await self._endpoint_mgr.start()
        for tdef in defs:
            self.tools.register(tdef)
        if defs:
            print(f"[mcp-endpoint] 已注册 {len(defs)} 个工具；"
                  f"工具表共 {len(self.tools)} 个", flush=True)

    # ---------- 端到端 Realtime（P2-D）----------

    async def _enter_realtime(self):
        """切到端到端语音链路（listen mode=realtime）。"""
        if self._realtime is not None:
            return
        try:
            from gateway.providers.llm_realtime import from_config
            rt = from_config(self.config,
                             logger=lambda m: print(f"[realtime] {m}", flush=True))
        except Exception as e:  # noqa: BLE001
            rt = None
            print(f"[realtime] 不可用：{type(e).__name__}: {e}", flush=True)
        if rt is None:
            await self._send(protocol.notice(
                "未配置端到端实时语音，改用普通模式"))
            self.listen_mode = "auto"
            self._collecting = True
            return
        try:
            await rt.connect()
        except Exception as e:  # noqa: BLE001
            await self._send(protocol.notice(f"实时语音连接失败：{e}"))
            self.listen_mode = "auto"
            self._collecting = True
            return
        self._realtime = rt
        self.listen_mode = "realtime"
        self._collecting = True
        self._realtime_task = asyncio.create_task(self._run_realtime())
        print("[realtime] 已切换到端到端实时语音", flush=True)

    async def _run_realtime(self):
        """把 realtime 返回的音频/字幕转成对端能吃的协议消息。"""
        from gateway.audio.opus import DownlinkEncoder
        rt = self._realtime
        if rt is None:
            return
        sid = self.session_id
        enc = DownlinkEncoder(self.config.downlink_sample_rate,
                              self.config.downlink_frame_size)
        tts_started = False
        frame_bytes = self.config.downlink_frame_size * 2
        buf = bytearray()
        try:
            async for chunk in rt.audio_events():
                if not tts_started:
                    await self._send(protocol.tts_start(sid))
                    self.state = "speaking"
                    tts_started = True
                if chunk is None:
                    # 本轮结束：把残余 PCM 补齐一帧后送出，再收尾
                    if buf:
                        buf.extend(b"\x00" * (frame_bytes - len(buf)))
                        await self._send(enc.encode_frame(bytes(buf)))
                        buf.clear()
                    await self._send(protocol.tts_stop(sid))
                    self._open_cooldown()
                    tts_started = False
                    self._resume_collecting()
                    continue
                buf.extend(chunk)
                while len(buf) >= frame_bytes:
                    frame = bytes(buf[:frame_bytes])
                    del buf[:frame_bytes]
                    if not await self._send(enc.encode_frame(frame)):
                        return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[realtime] 运行异常：{type(e).__name__}: {e}", flush=True)
        finally:
            if tts_started:
                with contextlib.suppress(Exception):
                    await self._send(protocol.tts_stop(sid))

    async def _exit_realtime(self):
        rt, self._realtime = self._realtime, None
        task, self._realtime_task = self._realtime_task, None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(Exception):
                await task
        if rt is not None:
            with contextlib.suppress(Exception):
                await rt.close()

    # ---------- 内置：供工具使用的控制能力 ----------

    def _negotiate_version(self, msg: dict):
        """协商二进制协议版本（1 裸 opus / 2 带时间戳 / 3 简化头）。"""
        try:
            from gateway.binframe.codec import FrameCodec, clamp_version
        except Exception:  # noqa: BLE001
            self._proto_version = 1
            self._codec = None
            return
        declared = msg.get("version", 1)
        self._proto_version = clamp_version(declared)
        self._codec = FrameCodec(self._proto_version)
        if self._proto_version != 1:
            print(f"[session] 协议版本协商：v{self._proto_version}"
                  f"（对端声明 {declared!r}）", flush=True)

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
        # 协议版本协商（v1 裸 opus / v2 带时间戳头 / v3 简化头）
        self._negotiate_version(msg)
        await self._send(protocol.hello(self.session_id, audio_params,
                                        version=self._proto_version))
        # MCP：设备声明支持就主动发起 MCP 会话（对齐官方 helloHandle）
        feats = msg.get("features") or {}
        if isinstance(feats, dict) and feats.get("mcp"):
            self._client_features = feats
            await self._on_hello_mcp()

    async def _on_listen(self, msg: dict):
        state = msg.get("state")
        if state == "start":
            mode = msg.get("mode", "auto")
            # 端到端 realtime：设备声明 mode=realtime 且配置开启时切换链路
            if mode == "realtime" and bool(
                    getattr(self.config, "realtime_enabled", False)):
                await self._enter_realtime()
                return
            # 幂等：auto 模式下本来就在采集、也没有在播报 —— 这是客户端收到
            # tts_stop 后的兜底重发。旧实现每条都 reset_sentence，会把 VAD 的
            # 静音计时清零，句子永远凑不满 min_silence_ms → 永远不结束 → 永远
            # 不送 ASR（"说话没反应"）。重发直接忽略。
            if (mode == "auto" and self.listen_mode == "auto"
                    and self._collecting and not self._response_active()):
                self._listen_dup += 1
                if self._listen_dup == 1 or self._listen_dup % 10 == 0:
                    print(f"[session] listen start 重复重发，已忽略"
                          f"（累计 {self._listen_dup} 条）", flush=True)
                return
            # 正在播报中、且最近没有收到 abort —— 这条 listen start 不是打断
            # （真打断客户端会先发 abort），多半是旧客户端的兜底重发；直接取消
            # 会把助手刚说一半的回复掐掉。
            if (self._response_active()
                    and time.monotonic() - self._last_abort_ts > 2.0):
                self._listen_dup += 1
                return
            await self._cancel_response()
            self.listen_mode = mode
            self._collecting = True
            # 客户端主动进入聆听：尾音冷却作废（用户在按键说话/明确要求听）
            self._clear_cooldown()
            # 刚因插话取消过回复（保护窗内）：这条 listen start 是客户端收到
            # tts_stop 后的兜底重发，不能清掉已经捕获的开口语音，否则用户
            # "打断时说的第一句"会被丢掉。
            if time.monotonic() >= self._barge_until:
                self._utterance = bytearray()
                self._barge = bytearray()
                self._barge_streak = 0
                self._speech_ms = 0.0
                self._utt_ms = 0.0
                self._trail_ms = 0.0
                self._spans = []
                self._in_speech = False
            self.vad.reset_sentence(self.vad_state)
            self.state = "listening"
            print(f"[session] listen start mode={self.listen_mode} "
                  f"collecting={self._collecting}", flush=True)
        elif state == "stop":
            was_collecting = self._collecting
            self._collecting = False
            if was_collecting and self.listen_mode == "manual":
                await self._finish_utterance()
        elif state == "detect":
            text = (msg.get("text") or "").strip()
            if not text:
                return
            # 自激活防线：设备端把扬声器播放声误判成唤醒/口令时，会把自己
            # 刚说的话作为 detect 文本推回来（"自己接自己的话"的根源）。
            # 真打断一定先有 abort（用户按键/真唤醒走 AbortSpeaking）；而
            # 播报中 / tts_stop 尾音冷却窗内、又没有 abort 的 detect，几乎
            # 必然是设备听见了自己 —— 丢弃，不开新一轮对话。
            recent_abort = time.monotonic() - self._last_abort_ts < 2.0
            if (self._response_active() or self._gate_on()) and not recent_abort:
                print(f"[echo-guard] 丢弃播放期/冷却期设备端 detect: {text}",
                      flush=True)
                return
            if self._is_self_echo(text):
                # detect 文本与刚播报的句子高度重合 → 同样是自己听自己。
                print(f"[echo-guard] 丢弃自播放回声 detect: {text}", flush=True)
                return
            if self._is_self_activated_wake(text, recent_abort):
                # detect 文本只是唤醒词本身：协议语义上这是"激活通知"而非
                # 用户话语（官方服务端收到 detect 不开对话）。设备端把
                # 扬声器尾音/噪声误判成唤醒时，会纯文字推回唤醒词——
                # 刚说过话的窗口内尤其可疑，放行它就是"自己接自己的话"
                # 的文字回环。真用户唤醒若在窗口内说了唤醒词被误丢，
                # 继续说话仍会走 ASR 通道，不丢内容。
                print(f"[echo-guard] 丢弃自激活唤醒词 detect: {text}",
                      flush=True)
                return
            self._collecting = False
            await self._start_response(text)

    # ---------- 上行音频 ----------

    async def _on_binary(self, data: bytes):
        self._check_sleep()      # 空闲判定：每帧一次时间戳比较，开销可忽略
        if not self._collecting and not self._response_active():
            return
        # v2/v3：二进制帧里可能封装的是 JSON（type=1）——拆出来当文本消息处理。
        if self._codec is not None and self._codec.version > 1:
            # 畸形帧（长度不足 / 版本头错）不能抛：单包异常会杀掉整场会话。
            try:
                kind, payload, _ts = self._codec.decode(data)
            except Exception as exc:  # noqa: BLE001
                print(f"[gateway] 上行二进制帧解析失败（丢弃）：{exc!r}",
                      flush=True)
                return
            if kind == "json":
                with contextlib.suppress(Exception):
                    await self._on_text(payload.decode("utf-8", "replace"))
                return
            opus = payload
        else:
            opus = data
        try:
            pcm = self._decoder.decode(opus)
        except Exception as exc:  # noqa: BLE001
            # 单个损坏的 opus 包不该终结会话（旧实现会让异常冒到主循环）
            print(f"[gateway] 上行 opus 解码失败（丢弃该帧）：{exc!r}",
                  flush=True)
            return
        if not pcm:
            return

        # 端到端 realtime：上行直接喂模型，绕过 VAD/ASR/LLM/TTS 级联
        if self._realtime is not None:
            await self._realtime.append_audio(pcm)
            return

        # ---- 回声门：播放期 / 尾音冷却期不吃上行（防自激活的核心）----
        if self._gate_on():
            self._gate_active = True
            await self._on_gated(pcm)
            return
        if self._gate_active:
            # 闸门刚放行：闸门期的 VAD 判定不能带进新回合。Silero 的
            # ended/have_voice 是粘性的，压着不放会让新回合第一帧就"结束"
            # 一个空句子，进而整句被门限丢弃（"说话没反应"）。
            self._gate_active = False
            self.vad.reset_sentence(self.vad_state)
            self._trail_ms = 0.0
            self._in_speech = False

        if self._response_active():
            # 响应中但**还没开始播**（thinking：LLM 在生成）→ 没有回声，
            # 用户这时插话是正常的，按老规矩判打断。
            speaking, _ = self.vad.process(self.vad_state, pcm)
            self._barge.extend(pcm)
            if len(self._barge) > _BARGE_KEEP_BYTES:
                del self._barge[:len(self._barge) - _BARGE_KEEP_BYTES]
            if speaking and self.listen_mode == "auto":
                # 连续 _BARGE_CONFIRM_FRAMES 帧（≈300ms）都判语音才取消——
                # 单帧 VAD 误触（尾音泄漏/瞬时噪声）会把助手自己掐断
                self._barge_streak += 1
                if self._barge_streak >= _BARGE_CONFIRM_FRAMES:
                    await self._commit_barge()
            else:
                self._barge_streak = 0
            return

        if not self._collecting:
            return
        self._utterance.extend(pcm)
        self._up_frames += 1
        if self._up_frames % 100 == 1:  # 约每 6s 一条：确认上行音频真的在到
            print(f"[session] 上行帧={self._up_frames} "
                  f"RMS={_rms(pcm):.0f}", flush=True)
        if self.listen_mode == "manual":
            return
        speaking, _vad_ended = self.vad.process(self.vad_state, pcm)
        frame_ms = self.config.frame_duration_ms
        self._utt_ms += frame_ms
        if speaking:
            self._speech_ms += frame_ms
            self._trail_ms = 0.0
            if not self._in_speech:          # 语音段开始
                self._in_speech = True
                self._span_start = len(self._utterance) - len(pcm)
        else:
            self._trail_ms += frame_ms
            if self._in_speech:              # 语音段结束
                self._in_speech = False
                self._spans.append((self._span_start, len(self._utterance)))

        # 开口前的静音不计入本回合：一直等不到人说话就把缓冲区里的死静音
        # 丢掉（只留一点前置音），否则"等待时长"会把判据稀释掉。
        if self._speech_ms <= 0.0 and self._trail_ms >= _LEAD_SILENCE_MS:
            frame_bytes = int(self.config.uplink_frame_size) * 2
            pre_bytes = int(_PRE_ROLL_MS // frame_ms) * frame_bytes
            if len(self._utterance) > pre_bytes:
                cut = len(self._utterance) - pre_bytes
                del self._utterance[:cut]
                # 语音段偏移要跟着平移，否则标点会插错位置
                self._spans = [(max(0, s - cut), max(0, e - cut))
                               for (s, e) in self._spans if e > cut]
            self._utt_ms = self._trail_ms = float(
                int(_PRE_ROLL_MS // frame_ms) * frame_ms)
            self.vad.reset_sentence(self.vad_state)
            return

        # 断句：服务端自己按"句尾静音"判定，不依赖 Silero 的 ended
        # （ended 是粘性的，且 reset_sentence 会清零静音计时，容易被
        #  客户端的 listen start 重发打断）。
        end = ((self._speech_ms >= _MIN_UTT_MS
                and self._trail_ms >= self.config.min_silence_ms)
               or self._utt_ms >= _MAX_UTT_MS)
        if end:
            self._collecting = False
            await self._finish_utterance()

    # ---------- 识别 + 响应 ----------

    def _response_active(self) -> bool:
        return self._response_task is not None and not self._response_task.done()

    def _resume_collecting(self):
        """恢复上行采集（auto 模式）。

        一轮回应结束（或本回合被放弃：太短 / 识别失败 / 回声丢弃）后，客户端
        仍在按实时节奏推流；若不把 _collecting 置回 True，_on_binary 会把之后
        所有音频静默丢弃 —— 用户下一句话永远得不到回应（对讲变聋）。
        manual 模式由客户端按需发 listen start，这里只把状态复位。"""
        if self.listen_mode == "auto":
            self._collecting = True
            self.state = "listening"
        else:
            self.state = "idle"
        self._trail_ms = 0.0

    async def _finish_utterance(self):
        pcm = bytes(self._utterance)
        speech_ms = self._speech_ms
        utt_ms = self._utt_ms
        spans = list(self._spans)
        # 收尾时还张着嘴（句尾静音不够就被强制送走）：补上最后一段
        if self._in_speech and pcm:
            spans.append((self._span_start, len(pcm)))
        self._utterance = bytearray()
        self._speech_ms = 0.0
        self._utt_ms = 0.0
        self._trail_ms = 0.0
        self._spans = []
        self._in_speech = False
        # 无论命中哪条分支，都必须重置 VAD 句子状态：Silero 的 ended/have_voice
        # 是粘性的（只有 reset_sentence 能清）。漏掉这会让后续每一帧都立即"结束"
        # 一个空句子并被门限丢弃——无限丢弃循环，整场会话零识别。
        self.vad.reset_sentence(self.vad_state)
        dur_ms = len(pcm) / (self.config.uplink_sample_rate * 2 / 1000)
        # 噪声/误触防线：用"本回合累计语音时长"的绝对值判定，不用占比。
        # 占比会随用户开口前的等待时长被稀释（等 10s 说 2s → 占比 0.17），
        # 把正常说话判成噪声丢掉。manual 模式是用户显式操作的，只查总时长。
        if self.listen_mode != "manual" and (
                dur_ms < _MIN_UTT_MS or speech_ms < _MIN_SPEECH_MS):
            print(f"[session] 丢弃过短/噪声句子: dur={dur_ms:.0f}ms "
                  f"语音={speech_ms:.0f}ms", flush=True)
            self._resume_collecting()
            return
        # 声纹与 ASR **并发**跑：声纹慢也不拖识别（对齐官方做法）
        vp_task = None
        if self._voiceprint is not None:
            vp_task = asyncio.create_task(asyncio.to_thread(
                self._voiceprint.identify, pcm,
                self.config.uplink_sample_rate))
        try:
            text = await asyncio.to_thread(self.asr.transcribe, pcm, spans)
        except Exception:
            text = ""
        speaker = ""
        if vp_task is not None:
            try:
                speaker = await asyncio.wait_for(vp_task, 10.0) or ""
            except Exception:  # noqa: BLE001
                speaker = ""
        if not text:
            print(f"[session] ASR 无结果（dur={dur_ms:.0f}ms），恢复采集",
                  flush=True)
            self._resume_collecting()
            return
        print(f"[session] ASR: {text[:50]}", flush=True)
        if self._is_self_echo(text):
            # 客户端没做回声抑制时（外放拾音）的兜底：识别结果与刚说过的话
            # 高度重合 → 是自己播放的声音，丢弃，不触发新一轮对话。
            print(f"[echo-guard] 丢弃自播放回声: {text}", flush=True)
            self._resume_collecting()
            return
        self._pending_speaker = speaker
        await self._start_response(text)

    def _note_speaker(self, speaker: str):
        """说话人**首次出现**时注入人设（去重，避免每轮重复报名字）。

        架构约束：不能新增一条 system 插在对话中间（Qwen/Ollama 模板会 500），
        所以拼到最前面的 system（人设）文本后面 —— 与摘要同款做法。"""
        if not speaker or speaker in self._introduced_speakers:
            return
        self._introduced_speakers.add(speaker)
        from gateway.providers.voiceprint import speaker_label
        note = speaker_label(speaker)
        if not note:
            return
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = (
                self.messages[0].get("content", "") + "\n" + note).strip()
        print(f"[voiceprint] 识别到说话人：{speaker}", flush=True)

    # ---------- TTS 引擎状态提示 ----------

    async def _notify_tts_state(self):
        """在线合成掉了/回来了就告诉用户一声——否则听感上就是"音色自己变了"。"""
        deg = bool(getattr(self.tts, "degraded", False))
        if deg == self._tts_degraded:
            return
        self._tts_degraded = deg
        if deg:
            label = getattr(self.tts, "local_label", "本地音色")
            await self._send(protocol.notice(
                f"在线音色暂时不可用，本次对话先用本地音色「{label}」，"
                f"不会再中途变来变去"))
        else:
            await self._send(protocol.notice("在线神经音色已恢复"))

    # ---------- 自播放回声兜底 ----------

    def _note_spoken(self, sentence: str):
        """记录刚播报出去的句子（供自回声比对）。"""
        key = _norm_text(sentence)
        if len(key) >= 4:
            self._spoken.append((time.monotonic(), key))
        self._last_spoken_ts = time.monotonic()

    def _is_self_activated_wake(self, text: str, recent_abort: bool) -> bool:
        """唤醒词-only 的 detect 是否来自设备端自激活。

        真打断（abort）优先放行；其余情况下，刚播报完的自唤醒窗口内
        （默认 6s，覆盖唤醒引擎的滞后触发——旧的 700ms 冷却窗盖不住）
        收到的"只有唤醒词"的 detect 几乎必然是设备听见了尾音。"""
        if recent_abort:
            return False
        if not _is_wake_word_text(text):
            return False
        window = float(getattr(self.config, "selfwake_window_s", 6.0))
        return (time.monotonic() - self._last_spoken_ts) < window

    def _is_self_echo(self, text: str) -> bool:
        """判断识别结果是否来自本机扬声器的自播放回声。

        与最近 window 秒内播报过的句子做归一化比对：长文本高度重合、
        或短文本被整句包含 → 判为回声。阈值偏严，避免误丢用户正常的
        "重复一句"（如"好的"）。"""
        if not getattr(self.config, "echo_dedup", True):
            return False
        window = float(getattr(self.config, "echo_dedup_window_s", 8.0))
        now = time.monotonic()
        self._spoken = [(t, s) for (t, s) in self._spoken
                        if now - t <= window]
        key = _norm_text(text)
        if len(key) < 4:
            return False
        for _, spoken in self._spoken:
            if len(key) >= 6 and (key in spoken or spoken in key):
                return True
            if len(key) >= 8 and difflib.SequenceMatcher(
                    None, key, spoken).ratio() >= 0.8:
                return True
        return False

    def _sync_voice(self):
        """把共享 TTS 实例的音色对齐到本会话的选择。

        TTS 是全局单例（多个连接共用）：A 连接改了音色不该影响 B，但实例只有
        一个，所以每个会话在开口前把自己的音色刷上去（只换音色、不影响
        "在线是否可用"的判定——那套状态由显式切换和降级逻辑管）。"""
        if not self.voice:
            return
        fn = getattr(self.tts, "apply_voice", None)
        if callable(fn):
            with contextlib.suppress(Exception):
                fn(self.voice)

    # ---------- 记忆模式 ----------

    @property
    def memory_mode(self) -> str:
        m = str(getattr(self.config, "memory_mode", "full") or "full").lower()
        return m if m in ("full", "summary", "none") else "full"

    def _context_messages(self) -> list:
        """组装真正送给大模型的上下文。

        full    —— 超长记忆：完整历史，只按 memory_max_turns 截掉最老的
        summary —— 本地总结：早期历史被压成一段摘要挂在人设后面，只留最近几轮
        none    —— 无记忆：只带人设 + 当前这句话

        注意：联网检索的上下文是**拼进最后一条 user 消息**的（见
        _inject_context），所以这里必须带着 self.messages 里那条（已注入过的）
        user 走，不能另起一份，否则检索结果会丢。
        """
        mode = self.memory_mode
        sys_msgs = [m for m in self.messages if m.get("role") == "system"][:1]
        body = [m for m in self.messages if m.get("role") != "system"]

        if mode == "none" or not body:
            return sys_msgs + body[-1:]

        if mode == "summary":
            keep = int(getattr(self.config, "memory_keep_recent", 4) or 4) * 2
            out = self._with_summary(sys_msgs)
            out.extend(body[-keep:] if keep > 0 else body)
            return out

        # full：截断到最近的 N 轮（一轮 = user + assistant 两条）
        max_turns = int(getattr(self.config, "memory_max_turns", 40) or 40)
        limit = max(2, max_turns * 2)
        return sys_msgs + body[-limit:]

    def _with_summary(self, sys_msgs: list) -> list:
        """把历史摘要并进人设（**不新增一条 system**）。

        Ollama/Qwen 的 chat 模板要求 system 只能在最前面，插一条新的 system
        到中间会直接 500。所以摘要直接拼在人设文本后面，最稳。"""
        if not self._summary:
            return list(sys_msgs)
        head = dict(sys_msgs[0]) if sys_msgs else {"role": "system",
                                                   "content": ""}
        head["content"] = (head.get("content", "") +
                           "\n\n【更早对话的摘要】\n" + self._summary).strip()
        return [head]

    def _summary_backend(self):
        """压缩用的模型实例（配了 memory_summary_model 才另开一个）。"""
        name = str(getattr(self.config, "memory_summary_model", "") or "").strip()
        if not name or name == getattr(self.llm, "model", ""):
            return self.llm
        if self._summary_llm is None:
            try:
                backend = str(getattr(self.config, "llm_backend", "ollama")
                              or "ollama").lower()
                if backend == "openai":
                    from gateway.providers.llm_openai import OpenAICompatLLM
                    self._summary_llm = OpenAICompatLLM(
                        getattr(self.config, "llm_api_base", ""),
                        getattr(self.config, "llm_api_key", ""), name)
                else:
                    from gateway.providers.llm_ollama import OllamaLLM
                    self._summary_llm = OllamaLLM(
                        self.config.ollama_base_url, name)
            except Exception:  # noqa: BLE001
                self._summary_llm = self.llm
        return self._summary_llm

    async def _maybe_compress(self):
        """本地总结模式：历史太长就把早期部分压成摘要。

        放在开口之前做，压的是**上一轮之前**的内容，所以压缩本身不会让
        当前这句变慢多少（压缩用的是短输出、temperature 0.1）。"""
        if self.memory_mode != "summary":
            return
        after = int(getattr(self.config, "memory_summary_after", 12) or 12) * 2
        keep = int(getattr(self.config, "memory_keep_recent", 4) or 4) * 2
        body = [m for m in self.messages if m.get("role") != "system"]
        if len(body) <= max(after, keep):
            return
        old = body[:max(0, len(body) - keep)]
        if not old:
            return
        chat = "\n".join(
            f"{'用户' if m.get('role') == 'user' else '小D'}："
            f"{(m.get('content') or '')[:200]}" for m in old)
        prompt = (
            "把下面这段对话压缩成不超过 150 字的中文摘要。"
            "保留：用户的偏好与习惯、已经确认的事实、待办事项、正在聊的话题。"
            "去掉：寒暄、重复、闲聊。只输出摘要本身，不要解释。\n\n" + chat
        )
        try:
            out = await self._summary_backend().quick(prompt, max_tokens=240,
                                                      timeout=25.0)
        except Exception as e:  # noqa: BLE001
            print(f"[memory] 压缩失败：{type(e).__name__}: {e}", flush=True)
            return
        # 部分带"思考"的模型（如 qwen3 系列）即使传了 think=false 也会把
        # 推理过程写进正文，摘要里混进这些会把后续对话带偏 → 一律剥掉。
        out = _strip_think(out or "").strip()
        if not out:
            return
        self._summary = ((self._summary + "\n" + out).strip()
                         if self._summary else out)
        # 摘要只留最近 800 字，别让它自己无限长大
        if len(self._summary) > 800:
            self._summary = self._summary[-800:]
        drop = {id(m) for m in old}
        self.messages = [m for m in self.messages
                         if m.get("role") == "system" or id(m) not in drop]
        print(f"[memory] 已压缩 {len(old)} 条历史 → 摘要 {len(self._summary)} 字",
              flush=True)

    # ---------- 自动休眠 ----------

    def _touch(self):
        """任何交互都重置空闲计时；休眠中被叫醒则恢复正常。"""
        self._last_active = time.monotonic()
        if self._sleeping:
            self._sleeping = False
            print(f"[session] 唤醒（会话 {self.session_id[:8]}）", flush=True)

    def _check_sleep(self):
        """空闲超过 idle_sleep_s 就"睡下去"。

        休眠**不丢上行**：用户随时能一句话叫醒，只是不再预热模型，让它
        可以被系统回收（9B 模型常驻要占 6GB）。0 = 不休眠。"""
        limit = float(getattr(self.config, "idle_sleep_s", 0) or 0)
        if limit <= 0 or self._sleeping:
            return
        if time.monotonic() - self._last_active < limit:
            return
        self._sleeping = True
        self.state = "idle"
        # 真正停掉上行累积：休眠时音频字节仍被 WS 接收（不丢连接、不换会话），
        # 但不再进 VAD/ASR 累积，省 CPU 与内存；设备/客户端下次 listen
        # （固件唤醒词触发，属控制消息）会经 _touch() 复位并恢复处理。
        # realtime 是持续流式模式，休眠没有意义，跳过。
        if self._realtime is None:
            self._collecting = False
            self._utterance = bytearray()
            self._spans.clear()
            self._in_speech = False
            self._speech_ms = 0.0
        print(f"[session] 空闲 {limit:.0f}s，进入休眠（会话 "
              f"{self.session_id[:8]}）", flush=True)

    # ---------- 智能家居（HomeAssistant）----------

    async def _try_homeassistant(self, text: str) -> str:
        """命中家居指令就执行并回一句口语确认；非家居指令返回空串。"""
        if not self._ha or not self._ha.enabled:
            return ""
        try:
            return await self._ha.handle(text, self.llm) or ""
        except Exception as e:  # noqa: BLE001
            print(f"[ha] 意图执行失败：{type(e).__name__}: {e}", flush=True)
            return ""

    async def _speak_direct(self, text: str):
        """直接播一句话（不经过大模型），用于家居执行结果这类短回复。"""
        sid = self.session_id
        await self._send(protocol.tts_start(sid))
        self.state = "speaking"
        sentid = protocol.new_sentence_id()
        await self._send(protocol.sentence_start(text, sid, sentid))
        self._note_spoken(text)
        gen = self.tts.stream(text, None)
        while True:
            try:
                p = await asyncio.wait_for(
                    gen.__anext__(), timeout=_TTS_FRAME_TIMEOUT_S)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                break
            if not await self._send(p):
                break
        await self._send(protocol.sentence_end(sid, sentid))
        await self._send(protocol.tts_stop(sid))
        self._open_cooldown()
        self._resume_collecting()

    async def _start_response(self, text: str):
        self._touch()
        # 家居指令优先：命中就直接执行并播报结果，不再走一轮大模型。
        # 开了工具调用（tools_first）时改由 homeassistant.control 工具处理，
        # 跳过这里的 prompt 前置判定。
        ha_reply = "" if self._tools_first() else await self._try_homeassistant(text)
        if ha_reply:
            await self._send(protocol.stt(text, self._pending_speaker))
            self._note_speaker(self._pending_speaker)
            self.messages.append({"role": "user", "content": text})
            self.messages.append({"role": "assistant", "content": ha_reply})
            await self._speak_direct(ha_reply)
            return
        await self._maybe_compress()
        self._sync_voice()
        await self._send(protocol.stt(text, self._pending_speaker))
        self._note_speaker(self._pending_speaker)
        self.messages.append({"role": "user", "content": text})
        self._barge = bytearray()
        self._barge_streak = 0
        self._speech_ms = 0.0
        self._utt_ms = 0.0
        self._trail_ms = 0.0
        self._spans = []
        self._in_speech = False
        self.vad.reset_sentence(self.vad_state)
        self.state = "thinking"
        self._response_task = asyncio.create_task(self._run_response())

    async def _iter_sentences(self) -> AsyncIterator[str]:
        buf = ""
        async for chunk in self.llm.chat(self._context_messages()):
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

    # ---------- 工具调用（MCP / function calling）----------

    def _tools_first(self) -> bool:
        return bool(getattr(self.config, "tools_first", False))

    def _make_tool_loop(self) -> ToolCallLoop:
        self._register_capability_tools()
        return ToolCallLoop(
            self.llm, self.tools,
            max_turns=int(getattr(self.config, "tool_loop_max_turns", 4) or 4),
            ack_text=str(getattr(self.config, "tool_ack_text", "好的，我查一下。")),
            enable_ack=bool(getattr(self.config, "tool_ack_tts", True)),
            enable_tools=self._tools_first(),
            logger=lambda m: print(f"[tool-loop] {m}", flush=True),
        )

    async def _iter_response_sentences(self) -> AsyncIterator[str]:
        """响应正文来源：开了工具走工具循环，否则走普通对话（=改造前行为）。"""
        if self._tools_first():
            loop = self._make_tool_loop()
            async for sent in loop.run(self._context_messages()):
                yield sent
        else:
            async for sent in self._iter_sentences():
                yield sent

    def _register_capability_tools(self):
        """把家居 / 联网搜索包成工具（**始终注册**，与 tools_first 解耦）。

        内部逻辑完全复用现有实现，不新增一份——prompt 路径依旧保留作兜底。
        早期实现只在 tools_first 开启时注册，导致用户在界面打开工具开关后
        注册表里根本没有这些能力工具（要重启网关才补得上）。默认
        tools_first=False 时工具循环本就不启用，所以始终注册不改变既有行为。"""
        if getattr(self, "_cap_tools_done", False):
            return
        self._cap_tools_done = True

        if getattr(self.config, "web_search", False):
            max_n = int(getattr(self.config, "web_search_max", 4) or 4)

            async def _web_weather(args) -> ToolResult:
                q = str(args.get("query", "") or "").strip()
                if not q:
                    return ToolResult.error("请提供要查询天气的城市或地点")
                try:
                    ctx = await asyncio.to_thread(web_weather, q, 8.0)
                except Exception as e:  # noqa: BLE001
                    return ToolResult.error(f"天气查询失败：{e}")
                return ToolResult.ok(ctx or "没有查到该地点的天气，请如实说明未查到。")

            async def _web_search(args) -> ToolResult:
                q = str(args.get("query", "") or "").strip()
                if not q:
                    return ToolResult.error("请提供搜索关键词")
                try:
                    items = await asyncio.to_thread(web_search, q, max_n)
                except Exception as e:  # noqa: BLE001
                    return ToolResult.error(f"检索失败：{e}")
                if not items:
                    return ToolResult.ok(
                        "没有查到可用资料。如果用户在问实时信息，请如实说明"
                        "你没查到，不要编造。")
                return ToolResult.ok(web_search_as_context(q, items))

            self.tools.register(ToolDef(
                "web.weather", "查询某个城市/地点的实时天气",
                {"type": "object", "properties": {
                    "query": {"type": "string", "description": "城市或地点名"}},
                    "required": ["query"]}, "builtin", _web_weather))
            self.tools.register(ToolDef(
                "web.search", "联网搜索实时/外部信息（新闻、赛事、股价、价格等）",
                {"type": "object", "properties": {
                    "query": {"type": "string", "description": "搜索关键词"}},
                    "required": ["query"]}, "builtin", _web_search))

        if self._ha is not None and self._ha.enabled:
            async def _ha_control(args) -> ToolResult:
                text = str(args.get("text", "") or "").strip()
                if not text:
                    return ToolResult.error("请提供要执行的家居指令")
                try:
                    reply = await self._ha.handle(text, self.llm)
                except Exception as e:  # noqa: BLE001
                    return ToolResult.error(f"家居控制失败：{e}")
                return ToolResult.ok(reply or "没识别出要控制哪个设备，请说明设备名。")

            self.tools.register(ToolDef(
                "homeassistant.control",
                "控制 HomeAssistant 智能家居设备（开关灯、调空调温度、开合窗帘等）",
                {"type": "object", "properties": {
                    "text": {"type": "string",
                             "description": "自然语言家居指令，如：把客厅灯关了"}},
                    "required": ["text"]}, "builtin", _ha_control))

        if getattr(self.config, "vision_enabled", False):
            async def _take_photo(args) -> ToolResult:
                question = str(args.get("question", "") or "").strip()
                from gateway.providers.camera import Camera
                from gateway.providers.llm_vision import from_config
                if not Camera.is_available():
                    return ToolResult.error("未安装 OpenCV，摄像头不可用")
                try:
                    data = await asyncio.to_thread(
                        Camera(int(getattr(self.config, "vision_camera_index", 0))
                               ).capture_jpeg)
                except Exception as e:  # noqa: BLE001
                    return ToolResult.error(f"拍照失败：{e}")
                vlm = from_config(self.config)
                if vlm is None or not vlm.ready:
                    return ToolResult.error(
                        "视觉模型未配置（请在设置里选择支持看图的在线模型）")
                try:
                    text = await vlm.describe(
                        data, question or "请描述你看到的内容。")
                except Exception as e:  # noqa: BLE001
                    return ToolResult.error(f"看图失败：{e}")
                finally:
                    with contextlib.suppress(Exception):
                        await vlm.aclose()
                return ToolResult.ok(text or "(没有看清，换个角度再试试)")

            self.tools.register(ToolDef(
                "self.camera.take_photo",
                "用摄像头拍一张照片并让视觉模型看图回答（例如：这是什么？看看我手里拿的什么）",
                {"type": "object", "properties": {
                    "question": {"type": "string",
                                 "description": "想问的关于这张照片的问题"}},
                    "required": []}, "builtin", _take_photo))

    # ---------- 联网搜索 ----------

    # 模型偶尔会把提示里的示例短语（"用户""当前位置"）当成关键词输出，
    # 或干脆输出一整句解释。这里统一校验/清洗，宁可不搜也别拿垃圾词去搜。
    _BAD_KW = ("用户", "当前位置", "这句话", "关键词", "小d", "助手",
               "不需要", "无需")
    _NO_KW = ("无", "否", "没有", "不用", "不", "na", "none")

    def _clean_query(self, raw: str) -> str:
        line = (raw or "").strip()
        if not line:
            return ""
        line = line.splitlines()[0].strip()
        line = line.strip("：:。.，,、\"'“”「」【】[]()（） ")
        if not line:
            return ""
        if line.lower() in self._NO_KW:
            return ""
        low = line.lower()
        if any(b in low for b in self._BAD_KW):
            return ""
        if len(line) > 20:          # 太长的多半是解释而不是关键词
            return ""
        return line[:20]

    async def _decide_search(self, text: str) -> str:
        """让模型自己判断这句话要不要联网，要就返回搜索关键词（否则空串）。"""
        prompt = (
            "你是搜索调度器。判断用户这句话是否需要联网查实时或外部信息"
            "（天气、新闻、赛事、股价、价格、人物近况、最新事件、具体作品/"
            "机构信息等）。\n"
            "需要 → 只输出一行搜索关键词（不超过 20 字，不带标点，不要解释）；\n"
            "不需要（闲聊、寒暄、常识、写作、算数、讲道理）→ 只输出：无\n"
            "注意：只输出关键词本身，不要重复用户原话，不要输出\"用户\""
            "\"当前位置\"这类词。\n\n"
            f"用户说：{text}"
        )
        try:
            out = await self.llm.quick(prompt, max_tokens=40)
        except Exception as e:  # noqa: BLE001
            print(f"[search] 判定失败: {e}", flush=True)
            return ""
        return self._clean_query(out)

    def _inject_context(self, ctx: str):
        """把参考上下文拼到最后一条 user 消息里（响应结束后还原）。

        **不能**以 system 角色插到对话中间 —— Qwen 系列的 chat 模板会直接
        报 500（模板要求 system 只在最前）。拼进 user 内容兼容性最好。"""
        idx = None
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].get("role") == "user":
                idx = i
                break
        if idx is None:
            return
        self._search_restore = (idx, self.messages[idx].get("content", ""))
        self.messages[idx]["content"] = \
            f"{self.messages[idx].get('content', '')}\n\n{ctx}"

    def _restore_context(self):
        """还原被拼进 user 消息的检索上下文，避免上下文越滚越大。"""
        if not self._search_restore:
            return
        idx, original = self._search_restore
        self._search_restore = None
        if 0 <= idx < len(self.messages):
            self.messages[idx]["content"] = original

    # ---------- 语速 / 语气（让播报更像真人）----------

    async def _decide_style(self, user_text: str) -> dict:
        """让模型根据用户这句话的情绪，决定回复的语速/音调/音量。"""
        prompt = (
            "你是语音播报风格控制器。根据用户这句话的情绪和场景，决定小D"
            "回答时最合适的语速与语调。\n"
            "只输出一行 JSON，不要解释：\n"
            '{"rate": 语速倍数, "pitch": 音调倍数, "volume": 音量倍数, '
            '"emotion": "情绪词"}\n'
            "取值范围：rate 0.85~1.30，pitch 0.90~1.15，volume 0.90~1.10。\n"
            "参考：开心/兴奋/惊喜→rate 略快、pitch 略高；难过/疲惫/在倾诉→"
            "rate 放慢、pitch 略低；哄人/安慰/温柔→rate 稍慢、pitch 略低、"
            "volume 略降；正经提问/工作事务→接近 1.0 平稳；祝贺/鼓励→"
            "rate 稍快、pitch 稍高。\n"
            f"用户说：{user_text}"
        )
        try:
            out = await self.llm.quick(prompt, max_tokens=80)
        except Exception as e:  # noqa: BLE001
            print(f"[style] 判定失败: {e}", flush=True)
            return {}
        import json as _json
        import re as _re
        m = _re.search(r"\{.*?\}", out or "", _re.S)
        if not m:
            return {}
        try:
            d = _json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return {}
        style = {
            "rate": max(0.85, min(1.30, float(d.get("rate", 1.0) or 1.0))),
            "pitch": max(0.90, min(1.15, float(d.get("pitch", 1.0) or 1.0))),
            "volume": max(0.90, min(1.10, float(d.get("volume", 1.0) or 1.0))),
            "emotion": str(d.get("emotion") or "")[:12],
        }
        print(f"[style] {style}", flush=True)
        return style

    @staticmethod
    def _sentence_style(base: dict, sent: str) -> dict:
        """在一轮的整体情绪上，按句子类型做微调，避免整段像一个调子。"""
        s = dict(base or {})
        r = float(s.get("rate", 1.0) or 1.0)
        p = float(s.get("pitch", 1.0) or 1.0)
        if sent.endswith(("？", "?")):
            p *= 1.03           # 疑问句尾音上扬
            r *= 0.98
        elif sent.endswith(("！", "!")):
            r *= 1.05           # 感叹句更利落
            p *= 1.02
        if len(sent) <= 5:      # 短句放慢一点，更像在"说话"而不是念稿
            r *= 0.96
        s["rate"] = max(0.8, min(1.35, r))
        s["pitch"] = max(0.88, min(1.18, p))
        return s

    async def _maybe_web_search(self):
        """联网搜索：模型自行判定 → 检索 → 作为参考上下文注入本轮提问。

        天气类问题先走 wttr.in 快路径：Bing 搜"北京今天天气"返回的是
        百科/政府网页（实测），模型拿不到真实气温就只能编 —— 接专用
        数据源是唯一可靠的解法。"""
        if not getattr(self.config, "web_search", False):
            return
        last_user = next((m.get("content", "") for m in reversed(self.messages)
                          if m.get("role") == "user"), "")
        if not last_user:
            return
        query = await self._decide_search(last_user)
        if not query:
            return
        print(f"[search] 联网检索: {query}", flush=True)
        await self._send(protocol.search_state("start", query))
        max_n = int(getattr(self.config, "web_search_max", 4) or 4)
        # ---- 天气快路径 ----
        if web_looks_weather(query) or web_looks_weather(last_user):
            try:
                ctx = await asyncio.to_thread(web_weather, query, 8.0)
            except Exception as e:  # noqa: BLE001
                ctx = ""
                print(f"[search] 天气查询异常: {e}", flush=True)
            if ctx:
                await self._send(protocol.search_state("end", query, 1))
                print("[search] 命中天气快路径（wttr.in）", flush=True)
                self._inject_context(ctx)
                return
        # ---- 普通搜索 ----
        try:
            items = await asyncio.to_thread(web_search, query, max_n)
        except Exception as e:  # noqa: BLE001
            items = []
            print(f"[search] 检索异常: {e}", flush=True)
        await self._send(protocol.search_state("end", query, len(items)))
        if not items:
            print("[search] 没有检索到结果", flush=True)
            self._inject_context(
                "【联网检索】没有查到可用资料。如果用户问的是实时信息，"
                "请如实说明你没查到，不要编造。")
            return
        print(f"[search] 命中 {len(items)} 条", flush=True)
        self._inject_context(web_search_as_context(query, items))

    async def _run_response(self):
        sid = self.session_id
        full = ""
        tts_started = False
        style: dict = {}
        style_task = None
        try:
            # 联网搜索：开了工具调用就交给 web.search / web.weather 工具，
            # 否则沿用原来的"模型判定 + 注入上下文"路径
            if not self._tools_first():
                await self._maybe_web_search()
            # 语速语气判定与"生成第一句"并行跑：等真要合成时它多半已经好了，
            # 不额外增加首句延迟。
            _last_user = next((m.get("content", "") for m in reversed(self.messages)
                               if m.get("role") == "user"), "")
            if _last_user:
                style_task = asyncio.create_task(self._decide_style(_last_user))
            async for sent in self._iter_response_sentences():
                if not tts_started:
                    if style_task is not None:
                        try:
                            style = await asyncio.wait_for(style_task, 4.0) or {}
                        except Exception:  # noqa: BLE001
                            style = {}
                        style_task = None
                    await self._send(protocol.tts_start(sid))
                    tts_started = True
                    self.state = "speaking"
                sentid = protocol.new_sentence_id()
                await self._send(protocol.sentence_start(sent, sid, sentid))
                self._note_spoken(sent)
                # TTS 单帧看门狗：EdgeTTS 偶发挂起/403，不能把会话永久钉在
                # _response_active()（那之后所有音频都会进打断路径）
                tts_gen = self.tts.stream(
                    sent, self._sentence_style(style, sent))
                while True:
                    try:
                        p = await asyncio.wait_for(
                            tts_gen.__anext__(), timeout=_TTS_FRAME_TIMEOUT_S)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        print(f"[session-dbg] TTS 帧超时，放弃本句: "
                              f"{sent[:20]}", flush=True)
                        break
                    if not await self._send(p):
                        break
                await self._send(protocol.sentence_end(sid, sentid))
                await self._notify_tts_state()   # 在线掉了/回来了 → 提示用户
                full += sent
            if not tts_started:
                await self._send(protocol.tts_start(sid))
                hint = "哎呀，我刚刚没反应过来，你再说一遍呗。"
                sid2 = protocol.new_sentence_id()
                await self._send(protocol.sentence_start(hint, sid, sid2))
                self._note_spoken(hint)
                hint_gen = self.tts.stream(
                    hint, self._sentence_style(style, hint))
                while True:
                    try:
                        p = await asyncio.wait_for(
                            hint_gen.__anext__(), timeout=_TTS_FRAME_TIMEOUT_S)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        break
                    if not await self._send(p):
                        break
                await self._send(protocol.sentence_end(sid, sid2))
                full = hint
            await self._send(protocol.tts_stop(sid))
            if full:
                self.messages.append({"role": "assistant", "content": full})
            # 服务端认为"播完了"≠ 设备真的播完了：留一段尾音冷却窗，
            # 让设备端播放缓冲和房间混响先散掉，再开始听。
            self._open_cooldown()
            # auto 模式：回应结束 ≠ 对讲结束。客户端还在推流，必须恢复采集，
            # 否则用户的下一句话会被 _on_binary 静默丢弃。
            self._resume_collecting()
        except asyncio.CancelledError:
            # 取消路径由调用方（abort / listen start）决定采集状态，这里不插手
            await self._send(protocol.tts_stop(sid))
            raise
        except Exception as e:
            import traceback
            print(f"[session-dbg] _run_response 异常: {e}", flush=True)
            traceback.print_exc()
            await self._send(protocol.tts_stop(sid))
            # 异常也可能是"播到一半断了"：同样留冷却窗，别立刻吃上行
            self._open_cooldown()
            # 别让失败表现成"说完话没反应"——给一句人话，并说一下最常见的
            # 两种原因（模型服务没起来 / 合成失败），用户才知道该查什么。
            msg = str(e).lower()
            if "connect" in msg or "refused" in msg or "ollama" in msg:
                tip = "模型服务连不上，请确认 Ollama 已启动、模型已下载"
            elif "tts" in msg or "audio was received" in msg:
                tip = "语音合成失败，请稍后再说一次"
            else:
                tip = f"生成回复时出错（{type(e).__name__}），请再说一次"
            try:
                await self._send(protocol.notice(tip))
            except Exception:  # noqa: BLE001
                pass
            self._resume_collecting()
        finally:
            # 无论正常/取消/异常都要还原被拼进 user 消息的检索上下文
            self._restore_context()

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
