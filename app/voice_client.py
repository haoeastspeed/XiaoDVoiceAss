# -*- coding: utf-8 -*-
"""实时语音（全双工）客户端：连接小D网关，做自然实时语音对话。

- 独立线程 + asyncio 事件循环，不阻塞 pywebview 主窗口
- 上行：麦克风 16k/60ms → opus 编码 → WebSocket
- 下行：WebSocket opus → 24k 解码 → 扬声器播放
- **回声抑制（播放期回声门）**：软件外放时麦克风必然拾到自己播放的声音，
  若照原样上行，网关会把它识别成"用户说话"，于是取消当前回复、再识别自己
  刚说过的话 → 自问自答死循环。这里的做法：
    1. 只要本机还有下行音频在播（含声卡缓冲区延迟与尾音），麦克风帧一律
       不上行（半双工对讲），播完 + 尾音窗口后自动恢复上行；
    2. 需要插话时（allow_barge_in=True）才在本机做"真实开口"能量检测，
       确认是用户说话再 abort 当前回复、重新 listen，并把开口起始帧补发上去；
    3. 打断后短暂丢弃仍在路上的下行音频，避免二次触发。
- 字幕 / 状态通过回调 on_event 推给前端
- audio_feed 可注入测试音频源（离屏测试），play_output=False 时不播放
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import queue
import sys
import threading
import time
from contextlib import suppress

import sounddevice as sd
import websockets
try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # websockets < 13
    from websockets import connect as ws_connect


# ---- 音频参数（与网关 / 小智协议对齐）----
UP_SR = 16000
DOWN_SR = 24000
FRAME_MS = 60
UP_FRAME = int(UP_SR * FRAME_MS / 1000)      # 960 samples
DOWN_FRAME = int(DOWN_SR * FRAME_MS / 1000)  # 1440 samples
DEVICE_ID = "xiaod-assistant"
DEVICE_NAME = "小D 语音陪聊助手"
DEVICE_MAC = "00:00:00:00:00:00"

# ---- 回声门参数 ----
POST_PLAY_MS = 350      # 播完后再多关这么久（尾音/房间混响，混响尾可达数百 ms）
# 插话判据的三个量都用**原始麦克风电平**（不加 AGC）—— 同一个增益同乘分子分母会
# 约掉，用原始量纲才能让"衰减估计"变成可解释的物理量（外放约 0.05~0.3，
# 耳机约 0.01~03），也便于日志里直接判读。
#
# 判据（三者取最大，全部要连续超阈 BARGE_FRAMES 帧才算插话）：
#   ① 回声上界估计 × BARGE_FACTOR —— 纯回声永远够不到（估计本身就是峰值上界），
#      倍数取 1.8 而不是 1.15：耦合会突变（调大音量/靠近音箱/换更响的语音样本），
#      1.15 时一次 +30% 的回跳就足以让纯回声连续超阈 → "语音打断会自激活"。
#   ② 用户典型语音电平 × BARGE_REF_RATIO —— 戴耳机/近讲时几乎没有回声，
#      此时该由"你平时说话多大声"定门槛。取**分位数**而不是历史最大值：
#      旧实现用 max()，偶然大声说过一句，之后半分钟轻声说话全都打不断
#      （"有时又打不断"的成因之一）。
#   ③ 噪声自适应下限 max(BARGE_FLOOR_MIN, BARGE_FLOOR_NOISE × 底噪) ——
#      播放句间间隙里没有回声可对抗，挡持续噪声（风扇/电流声）就靠它。
BARGE_FACTOR = 1.8      # 要高出"本机回声上界估计"多少倍才算用户在说话（≈+5dB）
BARGE_ECHO_TOL = 2.2    # 低于该倍数的帧仍算"疑似回声"进估计滑窗（自查自纠）
                         # 必须高于 BARGE_FACTOR：耦合突变（回声突然响一倍）时，
                         # 这些帧要能喂进估计器让门槛自己跟上，否则连续超阈 → 自激活
BARGE_FRAMES = 8        # 需连续几帧超阈（8×60ms≈480ms）才判为插话（宁漏勿误）
                         # 8 帧也是"自查自纠"的时间预算：耦合突变时估计器以
                         # 2%/帧 上爬，约 6 帧就能把门槛抬过突变后的回声电平，
                         # 连续超阈自然断掉 → 这就是"回声突然响一倍也不自激活"
BARGE_FLOOR_MIN = 120.0  # 绝对下限：安静房间底噪实测 RMS≈1~30，别被噪声触发
BARGE_FLOOR_NOISE = 6.0  # 下限还要高于底噪这么多倍（持续噪声防护）
BARGE_REF_RATIO = 0.35   # 还要达到"用户典型语音电平"的这个比例（戴耳机时靠它兜底）
BARGE_REF_WIN = 900      # 语音电平采样窗（≈54s，自然老化，不需要手动衰减）
BARGE_REF_Q = 0.35       # 取分位数（35%）：比"历史最大值"稳健得多
BARGE_ATT_INIT = 0.35    # 回声耦合初值（宁可先高，靠滑窗峰值快速降下来）
BARGE_ATT_RISE = 1.02    # 衰减估计每帧最多涨 2%：人声抖动混进来的污染涨不上去
BARGE_ATT_MIN = 0.005    # 耦合下界
BARGE_ATT_MAX = 0.90     # 耦合上界
BARGE_RATIO_WIN = 40     # 耦合估计的滑窗（40×60ms≈2.4s）
BARGE_PLAY_MIN = 0.10    # 播放低于近期峰值这个比例 → 判为"句间间隙"，不更新耦合
                         # 估计（否则 mic=底噪/play≈0 会把比值冲到天际 → 打不断）
BARGE_UNREACH_S = 3.0    # 门槛长期高于用户说话能力多久后提示"语音打断不可用"
DOWN_PEAK_TAU = 0.8      # 下行电平峰值保持的时间常数（秒）
POST_BARGE_MUTE = 0.4    # 打断后多久内不再重复触发
POST_BARGE_DROP = 0.5    # 打断后丢弃仍在路上的下行音频的时长（秒）
OUT_LATENCY_MIN = 0.15   # 声卡输出延迟的高估下限（PortAudio 上报值常低于实际）
_MAX_RECONNECTS = 30     # 意外断线后的最大重连次数（指数退避上限 8s，总计约 3 分钟）


def _ensure_opus_dll():
    """在 import opuslib_next 前保证 opus.dll 可被找到。"""
    here = os.path.dirname(os.path.abspath(__file__))
    meipass = getattr(sys, "_MEIPASS", "")
    candidates = [
        meipass,                                   # 打包后（PyInstaller 解包目录）
        os.path.join(meipass, "bin") if meipass else "",
        os.path.join(here, "bin"),
        os.path.dirname(here),                     # 开发环境：项目根/reference/bin
        os.path.join(os.path.dirname(here), "reference", "bin"),
        r"D:\projects\xiaod-voice\reference\bin",
    ]
    for d in candidates:
        if d and os.path.isfile(os.path.join(d, "opus.dll")):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            # Python 3.8+ 需要显式放行（否则 opuslib_next 的 C 扩展找不到依赖）
            try:
                os.add_dll_directory(d)
            except Exception:  # noqa: BLE001
                pass
            return


_ensure_opus_dll()
import opuslib_next  # noqa: E402


def _open_stream(factory, rate: int, blocks: int, callback, candidates,
                 to_ui=None):
    """按候选设备顺序尝试打开音频流。

    同一台物理设备在 PortAudio 里会以多个 hostapi 端点出现（MME /
    DirectSound / WASAPI / WDM-KS）。Windows 的 **WASAPI 共享模式**往往只接受
    设备原生采样率（常见 48kHz），拿它开 16k/24k 会直接报
    `Invalid sample rate [PaErrorCode -9997]` —— 表现就是"能选到设备但一通话
    就报错、没有声音"。这里按顺序试到成功为止：首选用户选中的端点，失败就换
    同名设备的其它 hostapi 端点，再不行退回系统默认。
    """
    errs: list[str] = []
    for dev in candidates:
        try:
            st = factory(samplerate=rate, blocksize=blocks, dtype="int16",
                         channels=1, device=dev, callback=callback)
            if errs and to_ui:
                # 说明用了哪个备用端点（带上设备名，便于排查）
                nm = ""
                try:
                    info = sd.query_devices(dev) if dev is not None else \
                        sd.query_devices(kind="input")
                    nm = (info or {}).get("name", "")
                except Exception:  # noqa: BLE001
                    pass
                name = (nm or "").replace("\r", " ").replace("\n", " ").strip()
                to_ui(f"已自动改用备用音频端点：{name or ('device=' + str(dev))}")
            return st
        except Exception as e:  # noqa: BLE001
            errs.append(f"device={dev}: {e}")
            continue
    raise RuntimeError("音频流打开失败（已尝试全部端点）：" + "；".join(errs[:4]))


def _rms(frame: bytes) -> float:
    """一帧 s16le 的均方根（int16 量纲），用于回声/人声能量对比。"""
    import numpy as np
    x = np.frombuffer(frame, dtype=np.int16)
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(x.astype(np.float32)))))


class RealtimeVoiceClient:
    """一次实时语音会话；start()/stop() 由前端按钮触发。

    audio_feed: 可选的无参可调用对象，每次返回一帧 60ms/16k s16le PCM，
                文件结束返回 None（用于离屏测试，替代麦克风）。
    play_output: 是否打开扬声器播放下行音频（测试可关）。
    echo_guard: 是否开启播放期回声门（默认开，解决"识别到自己播放的声音"）。
                对应 config.json 的 voice.echo_guard。
    allow_barge_in: 播放期是否允许本地检测到的真实开口打断回复。默认关：
                外放没有回声消除时，插话判定只能靠能量，容易把回声/噪声误判
                成插话；关掉即"半双工对讲"（对方说话时你说的话不会被送上去），
                最稳。要"随时打断"把 config.json 的 voice.allow_barge_in 设 true。
    barge_factor / barge_frames: 开口判定灵敏度（高于回声参考电平的倍数 / 连续帧数）。
    virtual_playback: 无扬声器时按真实时长消耗下行音频，仅供离屏测试
                      （play_output=False 时配合使用，用来验证回声门）。
    """

    def __init__(self, ws_url: str, on_event=None, mic_index=None,
                 speaker_index=None, audio_feed=None, play_output: bool = True,
                 debug_record: bool = True, echo_guard: bool = True,
                 allow_barge_in: bool = False,
                 barge_factor: float = BARGE_FACTOR,
                 barge_frames: int = BARGE_FRAMES, virtual_playback: bool = False,
                 voice: str | None = None, model: str | None = None,
                 prompt: str | None = None, search: bool = False,
                 tts_engine: str = "auto", play_volume: float = 1.0,
                 wake_mode: str = "always", wake_threshold: float = 0.5,
                 wake_models: list | None = None,
                 wake_followup_s: float = 8.0):
        self.ws_url = ws_url
        self.play_volume = max(0.3, min(1.6, float(play_volume or 1.0)))
        # ---- PC 端唤醒词（wake_mode=wake 时才启用）----
        # off/always = 现状（打开即听）；wake = 只有听到唤醒词才上行
        self.wake_mode = (wake_mode or "always").strip().lower()
        self._wake = None
        self._wake_gate = (self.wake_mode == "wake")
        self._wake_followup_s = float(wake_followup_s or 8.0)
        self._wake_followup_until = 0.0
        if self._wake_gate:
            try:
                from app.wakeword import WakeWord
                w = WakeWord(wake_models, wake_threshold)
                if w.available:
                    self._wake = w
                else:
                    self._wake_gate = False   # 不可用 → 降级为"打开即听"
                    print("[wake] 唤醒词不可用，降级为连续聆听", flush=True)
            except Exception as e:  # noqa: BLE001
                self._wake_gate = False
                print(f"[wake] 唤醒词初始化失败（{type(e).__name__}: {e}），"
                      f"降级为连续聆听", flush=True)
        self.on_event = on_event or (lambda e: None)
        self.mic_index = mic_index
        self.speaker_index = speaker_index
        self.audio_feed = audio_feed
        self.play_output = play_output
        self.debug_record = debug_record
        self.echo_guard = echo_guard
        self.allow_barge_in = allow_barge_in
        # 配置里可能还留着旧语义的值（4.0×播放电平 / 1.2×回声估计），这里夹到
        # 新语义的合理区间：低于 1.4 挡不住耦合突变（会自激活），高于 4.0 在
        # 外放场景基本打不断。一个陈旧的配置不该把语音打断锁死或放飞。
        self.barge_factor = max(1.4, min(4.0, float(barge_factor)))
        self.barge_frames = max(4, min(14, int(barge_frames)))
        self.virtual_playback = virtual_playback
        self._thread: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws = None  # 当前连接（interrupt 跨线程发送用）
        self.decoded_pcm: list[bytes] = []  # 下行解码出的 PCM（测试核对）
        self._up_pcm = bytearray()          # 实际上行麦克风 PCM（调试）
        self.debug_files: dict[str, str] = {}

        # ---- 回声门状态 ----
        self._p_lock = threading.Lock()
        self._out_pending = 0        # 已收到但还没交给声卡的下行字节数
        self._drain_ts = 0.0         # 最近一次"播完"的时刻
        self._play_end_ts = 0.0      # 离屏模式：推算的下行放完时刻
        self._out_latency = OUT_LATENCY_MIN  # 声卡输出缓冲延迟（秒），开流后取实际值（保留下限）
        self._flush_req = False      # 打断时请求立刻清空播放队列
        self._drop_down_until = 0.0  # 打断后短暂丢弃在下行路上的音频
        self._barge_mute_until = 0.0  # 打断后短时间内不再重复触发
        # 插话判定：用"正在播放的下行电平"当回声参考（比只看麦克风准得多）
        self._down_peak = 0.0        # 下行电平的峰值保持（含时间衰减）
        self._down_t = 0.0
        self._att = BARGE_ATT_INIT   # 扬声器→麦克风的耦合估计（滑窗峰值 + 限涨速）
        self._ratios: list[float] = []  # 播放期"麦克风/播放"比值滑窗（原始量纲）
        self._speech_levels: list[float] = []  # 用户说话电平采样（原始量纲）
        self._ref_cache = 0.0        # 分位数缓存（避免每帧都排序）
        self._ref_calc = 0.0         # 分位数上次计算的时刻
        self._play_peak_long = 0.0   # 下行电平的"长期峰值"（识别句间间隙用）
        self._barge_armed = True     # 打断后须等新播放内容到达才可再次判定
        self._barge_streak = 0
        self._barge_onset: list[bytes] = []  # 触发打断前保留的开口起始帧
        self._gate_log_ts = 0.0      # 插话判据诊断日志的节流时刻
        self._unreach_since = 0.0    # 门槛持续不可达的起始时刻
        self._unreach_notified = False
        self.echo_dropped = 0        # 被回声门挡下的上行帧数（诊断）
        self.barge_count = 0         # 本地插话打断次数（诊断）
        self.last_gate: dict = {}    # 最近一次插话判定的电平快照（诊断）
        self._listen_sent_ts = 0.0  # 最近一次自发 listen start 的时刻（防抖）
        self._lvl_ts = 0.0          # 上次上报音量电平的时刻（节流）
        self._up_frames = 0         # 上行帧计数（诊断日志）
        # 门控自动增益：只放大"明显高于底噪"的帧，沉默不被放大（不会喂大噪声）。
        # 麦克风输入音量低会直接拖累 ASR 准确率，这里把语音帧抬到目标电平。
        self._agc_target = 1200.0   # 目标语音 RMS（int16 量纲）
        self._agc_max_gain = 6.0    # 增益上限
        self._floor = 30.0          # 底噪估计（缓慢跟随）
        self._agc_gain = 1.0        # 当前增益（平滑变化）
        self._silent_streak = 0     # 连续静音帧计数（麦克风无音告警）
        self._mic_warned = False    # 本轮是否已告警过
        # 期望的 TTS 音色：会话开局要下发给网关，让界面列表里选的音色真正生效。
        # 必须是构造参数——早先这里被硬编码成 None，把传进来的 voice 丢掉了，
        # 于是 `if self.voice:` 恒为假 → 从不下发 set_voice → 网关一直用它自己
        # 存的旧音色（表现为"列表里是女声、回复却是男声"）。
        self.voice = voice or None
        self.model = model          # 期望的离线大模型（会话开始时下发）
        self.prompt = prompt        # 人设/系统提示词（会话开始时下发）
        self.search = bool(search)  # 是否允许模型自行联网搜索（会话开始时下发）
        self.tts_engine = tts_engine or "auto"
        # 协议驱动的播放期标记：tts start=True / tts stop=False。
        # 比"音频队列记账"权威——LLM/TTS 句间间隙（EdgeTTS 网络抖动可达数秒）
        # 里音频队列会排空，只有协议状态能覆盖整段回复。
        self._resp_active = False
        self._muted_emitted = None  # 最近一次向 UI 上报的静音状态（防重复）

    # ---------- 对外 ----------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def interrupt(self):
        """确定性打断（UI 按钮 / Esc 热键调用，线程安全）。

        与能量判据的插话检测无关：用户明确表达了"停下"，就直接
        abort 当前回复、停止本机播放、重新进入聆听。外放没有真回声
        消除时，这是唯一 100% 可靠的打断方式。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._do_interrupt(), loop)
        except Exception:  # noqa: BLE001
            pass

    async def _do_interrupt(self):
        self.barge_count += 1
        self._barge_mute_until = time.monotonic() + POST_BARGE_MUTE
        self._barge_armed = False
        self._barge_streak = 0
        self._barge_onset = []
        self._resp_active = False
        self._listen_sent_ts = time.monotonic()
        self._hard_mute_playback()
        try:
            ws = self._ws
            if ws is not None:
                await ws.send(json.dumps({"type": "abort"}))
                await ws.send(json.dumps({
                    "type": "listen", "state": "start", "mode": "auto"}))
        except Exception:  # noqa: BLE001
            pass

    def _hard_mute_playback(self):
        """立刻停止本机播放并重置播放记账（打断/插话共用）。

        声卡回调里待播的 buffer 由 `_flush_req` 清空；记账清零后回声门
        立即解除（不会再把麦克风当成"还在播"而继续关闭）。"""
        self._flush_req = True
        self._drop_down_until = time.monotonic() + POST_BARGE_DROP
        with self._p_lock:
            self._out_pending = 0
            self._drain_ts = 0.0
            self._play_end_ts = 0.0

    def set_allow_barge_in(self, enabled: bool):
        """热切换插话判据：播放期是否允许本地能量检测打断。

        按钮/Esc 打断（interrupt）不受此影响，任何模式下都可用。"""
        self.allow_barge_in = bool(enabled)

    def set_voice(self, voice: str, persist: bool = True):
        """切换 TTS 音色（线程安全，网关下一句生效）。

        persist=True 是**用户在界面里的显式选择**，网关会记盘；
        persist=False 只是"把界面当前值同步给网关"，不写盘（开局/重连用）。
        """
        # 记下来：重连或下一次会话开局要按最新选择重发，否则会退回旧音色。
        self.voice = voice or None
        loop = self._loop
        if loop is None or loop.is_closed():
            return False
        try:
            asyncio.run_coroutine_threadsafe(
                self._do_set_voice(voice, persist), loop)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _do_set_voice(self, voice: str, persist: bool = True):
        try:
            ws = self._ws
            if ws is not None:
                await ws.send(json.dumps({"type": "set_voice",
                                          "voice": voice,
                                          "persist": bool(persist)}))
        except Exception:  # noqa: BLE001
            pass

    def _send_ctl(self, payload: dict) -> bool:
        """线程安全地往网关发一条控制消息（通话中才有效）。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return False

        async def _do():
            try:
                ws = self._ws
                if ws is not None:
                    await ws.send(json.dumps(payload))
            except Exception:  # noqa: BLE001
                pass

        try:
            asyncio.run_coroutine_threadsafe(_do(), loop)
            return True
        except Exception:  # noqa: BLE001
            return False

    def set_model(self, model: str) -> bool:
        """热切换离线大模型（下一句生效）。"""
        self.model = model
        return self._send_ctl({"type": "set_model", "model": model})

    def set_prompt(self, prompt: str) -> bool:
        """更新人设/系统提示词。"""
        return self._send_ctl({"type": "set_prompt", "prompt": prompt})

    def set_tts_engine(self, engine: str) -> bool:
        """切换 TTS 引擎（auto/online/local）。"""
        self.tts_engine = engine
        return self._send_ctl({"type": "set_tts_engine", "engine": engine})

    def set_search(self, enabled: bool) -> bool:
        """开关联网搜索（网关侧实时生效）。"""
        self.search = bool(enabled)
        return self._send_ctl({"type": "set_search", "enabled": bool(enabled)})

    def clear_history(self) -> bool:
        """清空网关侧对话上下文（保留人设）。"""
        return self._send_ctl({"type": "clear_history"})

    def _cand_devices(self, primary, direction: str) -> list:
        """打开音频流时的候选端点（实现在 app.devices，通话与试听共用）。"""
        from app import devices as _dev
        try:
            return _dev.candidates(primary, direction)
        except Exception:  # noqa: BLE001
            return [primary, None]

    def _note_level(self, pcm: bytes, full_scale: float,
                    src: str = "mic") -> None:
        """节流上报音量（0~1），界面据此让光晕/形象随声音起伏。

        src 区分来源：mic＝麦克风输入，play＝正在播给用户的音频。
        界面口型只认 play（播放期麦克风回调仍在跑，喂进去会让嘴跟着
        环境噪声动）。"""
        now = time.monotonic()
        if now - self._lvl_ts < 0.06:
            return
        self._lvl_ts = now
        try:
            from math import sqrt
            n = len(pcm) // 2
            if n <= 0:
                return
            import numpy as np
            x = np.frombuffer(pcm[:n * 2], dtype=np.int16)
            if x.size == 0:
                return
            rms = float(np.sqrt(np.mean(np.square(x.astype(np.float32)))))
        except Exception:  # noqa: BLE001
            return
        self._emit({"type": "level", "src": src,
                    "value": min(1.0, rms / max(1.0, full_scale))})

    def _emit_stream_note(self, text: str):
        """音频端点回退这类内部提示只写日志，不打扰界面。"""
        import sys as _sys
        try:
            print(f"[rt] {text}", file=_sys.stderr, flush=True)
        except Exception:  # noqa: BLE001
            pass

    def stop(self):
        """结束通话（UI 线程调用）：立刻静音 + 收掉连接。

        只置 `_stop_flag` 是不够的 —— 下行协程还阻塞在 `recv()` 上，
        `asyncio.gather(uplink(), downlink())` 就不会返回，`finally` 里的
        关流也执行不到：已经收进 `out_q` 的音频会按实时速度继续放，
        长回复就表现为"点了结束聊天还在念"。所以这里主动清空播放队列，
        并把连接收掉，让阻塞的 recv 立刻结束。"""
        self._stop_flag.set()
        self._hard_mute_playback()      # 清空队列 + 记账清零
        loop = self._loop
        if loop is not None and not loop.is_closed():
            with suppress(Exception):
                asyncio.run_coroutine_threadsafe(self._do_stop(), loop)

    async def _do_stop(self):
        """主动结束：让网关别再加紧合成，并关掉连接。"""
        ws = self._ws
        if ws is None:
            return
        with suppress(Exception):
            await ws.send(json.dumps({"type": "abort"}))
        with suppress(Exception):
            await ws.close()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive()
                    and not self._stop_flag.is_set())

    # ---------- 事件 ----------
    def _emit(self, ev: dict):
        try:
            self.on_event(ev)
        except Exception:
            pass

    def _emit_mute(self, muted: bool):
        """上行静音状态变化时通知 UI（只在跳变时发，避免每帧都推）。"""
        if self._muted_emitted is muted:
            return
        self._muted_emitted = muted
        self._emit({"type": "muted" if muted else "unmuted"})

    # ---------- 线程入口 ----------
    def _run(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        attempt = 0
        try:
            while not self._stop_flag.is_set():
                try:
                    retry = loop.run_until_complete(self._main())
                except Exception as e:  # noqa: BLE001
                    if self._stop_flag.is_set():
                        break
                    retry = True
                    self._emit({"type": "error", "error": str(e)})
                if not retry:
                    break
                attempt += 1
                if attempt > _MAX_RECONNECTS:
                    self._emit({
                        "type": "error",
                        "error": f"重连 {_MAX_RECONNECTS} 次仍失败，请结束通话重试"})
                    break
                self._emit({"type": "reconnecting", "attempt": attempt})
                # 指数退避，上限 8s；等待期间用户点结束通话立即退出
                delay = min(0.5 * (2 ** min(attempt - 1, 5)), 8.0)
                if self._stop_flag.wait(delay):
                    break
        finally:
            self._emit({"type": "closed"})
            try:
                loop.close()
            except Exception:
                pass

    # ---------- 播放记账（回声门判定依据）----------
    def _note_queued(self, nbytes: int):
        with self._p_lock:
            self._out_pending += nbytes
            # 离屏（无扬声器）模式：按下行到达节奏推算"放完时刻"
            self._play_end_ts = (max(self._play_end_ts, time.monotonic())
                                 + nbytes / (2.0 * DOWN_SR))

    def _note_played(self, nbytes: int):
        with self._p_lock:
            if self._out_pending <= 0:
                return          # 空转（虚拟播放器/静音帧）不刷新播完时刻
            self._out_pending = max(0, self._out_pending - nbytes)
            if self._out_pending == 0:
                self._drain_ts = time.monotonic()

    def _playback_busy(self) -> bool:
        """本机是否还在放小D的声音（含声卡缓冲与尾音）。

        只要在下行音频的"播放期"内，麦克风听到的就可能是自己外放的声音，
        此时不上行 —— 这是杜绝"识别到自己播放的声音"的关键。

        判据分两层，取并集：
        1. **协议层（权威）**：`tts start`→`tts stop` 之间恒为播放期。
           覆盖 LLM/TTS 句间间隙——那时音频队列会排空，只靠记账会误开门，
           间隙里的任何声音都会被服务端当成打断/新语音（自激活）。
        2. **声卡层（兜尾音）**：stop 之后队列里/缓冲里的音频还要实际放完，
           按延迟下限 + 尾音窗保守估算。"""
        if not (self.echo_guard and (self.play_output or self.virtual_playback)):
            return False
        if self._resp_active:
            return True
        margin = max(self._out_latency, OUT_LATENCY_MIN) + POST_PLAY_MS / 1000.0
        if self.virtual_playback and not self.play_output:
            with self._p_lock:
                end = self._play_end_ts
            return time.monotonic() < end + margin
        with self._p_lock:
            pending = self._out_pending
            drain = self._drain_ts
        if pending > 0:
            return True
        if not drain:
            return False
        return (time.monotonic() - drain) < margin

    def _note_down_level(self, pcm: bytes):
        """记录下行电平（回声参考）：播放中的声音越大，麦克风里的回声就越大。"""
        level = _rms(pcm)
        now = time.monotonic()
        with self._p_lock:
            if self._down_t:
                self._down_peak *= math.exp(-(now - self._down_t) / DOWN_PEAK_TAU)
            self._down_t = now
            if level > self._down_peak:
                self._down_peak = level
            # 长期峰值：衰减极慢，用来判断"现在是不是句间间隙"
            self._play_peak_long = max(level, self._play_peak_long * 0.998)
            self._barge_armed = True   # 有新的播放内容 → 可以判定插话

    def _down_level(self) -> float:
        """当前"在播"的下行电平参考。

        注意：下行数据来得比实时快（TTS 一句会成批下发），扬声器在放缓冲里的
        音频时参考不能衰减，否则门槛会塌到回声之下 → 误判成插话。"""
        with self._p_lock:
            if not self._down_t:
                return 0.0
            peak = self._down_peak
            last = self._down_t
        if self._playback_busy():
            return peak
        return peak * math.exp(-(time.monotonic() - last) / DOWN_PEAK_TAU)

    # ---------- 播放期：本地"真实开口"检测 ----------
    def _update_echo_ratio(self, level: float, play: float) -> None:
        """维护"扬声器→麦克风"的耦合估计。

        取滑窗里的**峰值**（上界），不是均值/低分位数：TTS 的逐帧包络动态范围
        极大（实测 P20≈0、P50≈2000、P90≈5200），只有上界才能保证"纯回声永远
        不越界"；再用**每帧最多涨 2%** 限制上升速度，让用户人声混进来的污染帧
        最多把门槛抬高一丁点（0.36s 的人声 ≈ +2%），不会像旧实现那样"越说越
        打不断"（旧实现取最大值且立刻生效，属正反馈自锁）。下降则立即跟随，
        音量调小/戴耳机后马上就能打断。"""
        if play <= 0:
            return
        self._ratios.append(level / play)
        if len(self._ratios) > BARGE_RATIO_WIN:
            del self._ratios[:len(self._ratios) - BARGE_RATIO_WIN]
        peak = max(self._ratios)
        if peak > self._att:
            self._att = min(peak, self._att * BARGE_ATT_RISE)
        else:
            self._att = peak
        self._att = min(BARGE_ATT_MAX, max(BARGE_ATT_MIN, self._att))

    @property
    def _mic_ref(self) -> float:
        """用户"典型语音"电平（原始量纲），插话判据的绝对下限参考。

        戴耳机/近讲时几乎收不到自己的回声，判据就该由"你平时说话多大声"来定，
        而不是由播放电平定 —— 这样有回声的会议室和戴耳机的场景都能打断。

        取**分位数**（35%）而不是历史最大值：旧实现 `max(历史)`，用户偶然大声
        说一句（喊一嗓子），之后半分钟轻声说话全都打不断 —— 这就是"有时又
        打不断"的直接成因。分位数对离群值稳健，且窗口自然老化。"""
        now = time.monotonic()
        if now - self._ref_calc < 0.5:
            return self._ref_cache
        self._ref_calc = now
        if not self._speech_levels:
            self._ref_cache = 0.0
            return 0.0
        vals = sorted(self._speech_levels)
        i = min(int(len(vals) * BARGE_REF_Q), len(vals) - 1)
        self._ref_cache = vals[i]
        return self._ref_cache

    def _track_mic_ref(self, level: float) -> None:
        """记录用户说话时的电平（只在非播放期调用，因此绝不会混入回声）。"""
        if level <= max(self._floor * 3.0, 20.0):
            return
        self._speech_levels.append(level)
        if len(self._speech_levels) > BARGE_REF_WIN:
            del self._speech_levels[:len(self._speech_levels) - BARGE_REF_WIN]

    def _note_unreachable(self, thr: float) -> None:
        """门槛长期高于"你说多大声"时，明确告诉用户语音打断在这个环境用不了。"""
        ref = self._mic_ref
        now = time.monotonic()
        if not ref or thr <= 1.5 * ref:
            self._unreach_since = 0.0
            self._unreach_notified = False
            return
        if not self._unreach_since:
            self._unreach_since = now
            return
        if (not self._unreach_notified
                and now - self._unreach_since >= BARGE_UNREACH_S):
            self._unreach_notified = True
            self._emit({"type": "notice", "text":
                        "语音打断在当前环境不太灵：本机外放的回声比你说话还响。"
                        "戴耳机、调低音量或靠近麦克风后才能生效（按钮 / Esc 一直可用）"})

    def _gate_frame(self, frame: bytes) -> bool:
        """回声门开启期间处理一帧麦克风音频；返回 True 表示判定为用户插话。

        门槛 = max(回声上界估计 × 1.8, 你典型语音电平 × 0.35, 噪声自适应下限)：
          * **回声上界**：耦合估计（滑窗峰值、限涨速）× 当前播放电平 —— 纯回声
            因此永远够不到门槛，不会误判；倍数给到 1.8（而不是激进的 1.15）
            是为了容忍耦合突变（调大音量/靠近音箱），那是"自激活"的元凶；
          * **用户参考**：戴耳机没有回声时，靠"你平时说话多大声"来决定门槛；
          * **噪声下限**：播放的句间间隙里没有回声，挡持续噪声靠它；
          * **单位一致**：全用原始麦克风电平（旧实现给麦克风乘了 AGC 增益、却
            没给回声项乘，等效把门槛除以 3~6，判据完全是错的）。

        这仍是能量法：外放无真 AEC 时，人声必须明显盖过回声才判得出来。
        不够时宁可不打断（请用按钮 / Esc），也绝不把回声当人话补发上去。"""
        self.echo_dropped += 1
        if not self.allow_barge_in:
            return False
        now = time.monotonic()
        if now < self._barge_mute_until:
            return False
        if not self._barge_armed:        # 刚打断过且没有新的播放内容
            return False
        play = self._down_level()
        if play <= 0.0:                  # 没有播放参考（异常）→ 保守不打断
            return False
        with self._p_lock:
            play_peak_long = self._play_peak_long
        level = _rms(frame)
        echo = self._att * play
        ref = self._mic_ref
        floor_thr = max(BARGE_FLOOR_MIN, BARGE_FLOOR_NOISE * self._floor)
        thr = max(echo * self.barge_factor,
                  BARGE_REF_RATIO * ref,
                  floor_thr)
        self.last_gate = {"mic": level, "play": play, "echo": echo,
                          "att": self._att, "thr": thr, "ref": ref,
                          "floor": floor_thr}
        if now - self._gate_log_ts >= 2.0:      # 诊断：语音打断为什么没生效
            self._gate_log_ts = now
            print(f"[rt][barge] mic={level:.0f} echo={echo:.0f} att={self._att:.3f} "
                  f"ref={ref:.0f} floor={floor_thr:.0f} thr={thr:.0f} "
                  f"（要能打断，mic 需连续 {self.barge_frames} 帧超过 thr）",
                  flush=True)
        self._note_unreachable(thr)
        # 距离回声估计不远的帧（≤2.2×）照样喂给估计器：**容差必须高于触发倍数**，
        # 这样回声突然变响（调大音量/靠近音箱）时，这些帧能把耦合估计抬起来、
        # 门槛自己跟上，连续超阈自然断掉；真正的人声远高于回声，喂不进去，
        # 也就不会污染估计（旧实现正是被它污染的）。
        # 句间间隙（play 远低于长期峰值）不喂：此时 mic=底噪、play≈0，
        # 比值会冲到天际把耦合估计抬爆 → 之后整句话都打不断。
        if (level <= BARGE_ECHO_TOL * echo
                and play >= max(30.0, BARGE_PLAY_MIN * play_peak_long)):
            self._update_echo_ratio(level, play)
        if level > thr:
            self._barge_streak += 1
            self._barge_onset.append(frame)
            if len(self._barge_onset) > self.barge_frames:
                self._barge_onset.pop(0)
            if self._barge_streak >= self.barge_frames:
                self._barge_streak = 0
                return True
            return False
        # 判定为回声帧
        self._barge_streak = 0
        self._barge_onset.clear()
        return False

    def set_volume(self, value) -> None:
        """播放音量倍数（0.3~1.6）。运行期可调，下一帧生效。"""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        self.play_volume = max(0.3, min(1.6, v))

    def _scale(self, chunk: bytes) -> bytes:
        """按播放音量缩放下行 PCM。1.0 时原样返回，避免每帧都过一遍 numpy。"""
        g = getattr(self, "play_volume", 1.0)
        if abs(g - 1.0) < 0.01:
            return chunk
        try:
            import numpy as np
            x = np.frombuffer(chunk, dtype="<i2").astype(np.float32)
            return np.clip(x * g, -32768, 32767).astype("<i2").tobytes()
        except Exception:  # noqa: BLE001
            return chunk

    def _auto_gain(self, frame: bytes):
        """门控自动增益：双向把语音帧拉回目标电平区间。

        麦克风输入音量低会直接拖累 ASR 准确率；反过来离麦太近或习惯大嗓门
        时上行会顶到削波（实测 RMS 能到 6000+），同样伤识别。这里把明显高于
        底噪的帧抬到目标电平，把明显高于目标电平的帧压回来——两端都平滑变化，
        底噪附近的帧保持原样，避免喂大噪声触发更多误断句。
        返回 (帧, 增益, 帧RMS)。"""
        level = _rms(frame)
        # 底噪缓慢跟随：向下快、向上慢（安静房间底噪低）
        if level < self._floor:
            self._floor = max(8.0, 0.7 * self._floor + 0.3 * level)
        else:
            self._floor = min(60.0, self._floor * 1.0005 + 0.0005 * level)
        if level < self._floor * 2.5:
            # 底噪区：不动
            want = 1.0
        elif level < self._agc_target:
            # 偏轻：往上抬
            want = min(self._agc_max_gain, self._agc_target / max(level, 1.0))
        else:
            # 偏响：往下压（下限 0.25，别把人声压成耳语）
            want = max(0.25, self._agc_target / max(level, 1.0))
        self._agc_gain += 0.2 * (want - self._agc_gain)  # 平滑，避免抽动
        if 0.99 <= self._agc_gain <= 1.01:
            return frame, 1.0, level
        import numpy as np
        x = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        x = np.clip(x * self._agc_gain, -32768, 32767).astype(np.int16)
        return x.tobytes(), self._agc_gain, level

    async def _barge_in(self, ws, encoder, onset: list[bytes]):
        """用户插话：停播 → 让网关放弃当前回复并重新进入聆听 → 补发开口起始帧。"""
        self.barge_count += 1
        self._flush_req = True
        self._drop_down_until = time.monotonic() + POST_BARGE_DROP
        self._barge_mute_until = time.monotonic() + POST_BARGE_MUTE
        self._barge_armed = False
        self._barge_streak = 0
        sends = [json.dumps({"type": "abort"}),
                 json.dumps({"type": "listen", "state": "start",
                             "mode": "auto"})]
        self._listen_sent_ts = time.monotonic()
        for f in onset:
            if len(f) >= UP_FRAME * 2:
                try:
                    sends.append(encoder.encode(f, UP_FRAME))
                except Exception:  # noqa: BLE001
                    pass
        self._barge_onset = []
        self._resp_active = False
        self._hard_mute_playback()
        try:
            for s in sends:
                await ws.send(s)
        except Exception:  # noqa: BLE001
            pass

    # ---------- 主协程 ----------
    async def _main(self):
        # 每次（重）连都重置播放记账与协议状态：上次连接可能死在回复中途，
        # 残留的 _resp_active=True 会把回声门永久关着（重连后一直不能说话）。
        self._resp_active = False
        self._muted_emitted = None
        self._silent_streak = 0
        self._mic_warned = False
        with self._p_lock:
            self._out_pending = 0
            self._drain_ts = 0.0
            self._play_end_ts = 0.0
        self._emit({"type": "state", "state": "connecting"})
        # keepalive 放宽到 60s：本地网关在模型冷加载/系统卡顿时事件循环可能
        # 超过 20s 无响应，默认 20s 会把还活着的连接误杀（1011）。
        try:
            ws = await ws_connect(
                self.ws_url,
                additional_headers={"device-id": DEVICE_ID},
                max_size=None,
                ping_interval=20,
                ping_timeout=60,
            )
        except TypeError:  # 兼容旧版 legacy websockets
            ws = await ws_connect(
                self.ws_url,
                extra_headers={"device-id": DEVICE_ID},
                max_size=None,
                ping_interval=20,
                ping_timeout=60,
            )
        except Exception as e:  # noqa: BLE001
            # 连接失败也进入重连循环（网关可能正在重启）
            self._emit({"type": "error",
                        "error": f"无法连接语音网关：{e}"})
            return True

        self._ws = ws
        encoder = opuslib_next.Encoder(
            UP_SR, 1, opuslib_next.APPLICATION_VOIP)
        decoder = opuslib_next.Decoder(DOWN_SR, 1)

        in_q: queue.Queue = queue.Queue()    # 麦克风 PCM → 上行
        out_q: queue.Queue = queue.Queue()   # 下行 PCM → 播放
        out_buf = bytearray()

        # ---- PortAudio 回调（独立线程）----
        def in_cb(indata, _frames, _t, _s):
            if not self._stop_flag.is_set():
                raw = bytes(indata)
                in_q.put(raw)
                self._note_level(raw, 2600.0)   # 驱动界面光晕随音量脉动

        def out_cb(outdata, frames, _t, _s):
            need = frames * 2
            # 已结束通话：一律送静音。声卡回调是独立线程，可能比事件循环更晚
            # 才察觉停止（recv 还阻塞着），这里直接掐掉是"结束聊天立刻不响"的
            # 最后一道保证 —— 只靠关流会在 gather 返回前继续把队列放完。
            if self._stop_flag.is_set():
                outdata[:] = b"\x00" * need
                return
            if self._flush_req:            # 打断：立刻静音，丢弃未播
                self._flush_req = False
                out_buf.clear()
                while True:
                    try:
                        out_q.get_nowait()
                    except queue.Empty:
                        break
                with self._p_lock:
                    self._out_pending = 0
            while len(out_buf) < need:
                try:
                    out_buf.extend(out_q.get_nowait())
                except queue.Empty:
                    break
            if len(out_buf) >= need:
                chunk = bytes(out_buf[:need])
                outdata[:] = self._scale(chunk)
                del out_buf[:need]
                given = need
                self._note_level(chunk, 3200.0, "play")   # 口型看原始电平
            else:
                n = len(out_buf)
                outdata[:n] = self._scale(bytes(out_buf))
                outdata[n:] = b"\x00" * (need - n)
                out_buf.clear()
                given = n
            if given:
                self._note_played(given)   # 交给声卡 → 记账（回声门依据）

        # ---- 上行协程 ----
        async def uplink():
            while not self._stop_flag.is_set():
                paced = self.audio_feed is not None
                if paced:
                    frame = self.audio_feed()
                    if frame is None:
                        await asyncio.sleep(0.1)
                        continue
                else:
                    try:
                        frame = await asyncio.to_thread(in_q.get, True, 0.2)
                    except queue.Empty:
                        continue
                # 播放期：只做本地开口检测，不上行（否则会识别到自己放的声音）
                if self._playback_busy():
                    self._emit_mute(True)
                    if self._gate_frame(frame):
                        await self._barge_in(ws, encoder, self._barge_onset)
                    if paced:
                        await asyncio.sleep(FRAME_MS / 1000.0)
                    continue
                self._emit_mute(False)
                # ---- PC 端唤醒词门（wake_mode=wake）：未唤醒不上行 ----
                if self._wake is not None:
                    if self._wake_gate:
                        if self._wake.feed(frame):
                            self._wake_gate = False
                            self._wake_followup_until = (
                                time.monotonic() + self._wake_followup_s)
                            self._wake.reset()
                            self._emit({"type": "notice", "text": "我在听"})
                            await ws.send(json.dumps({
                                "type": "listen", "state": "start",
                                "mode": "auto"}))
                            self._listen_sent_ts = time.monotonic()
                        if paced:
                            await asyncio.sleep(FRAME_MS / 1000.0)
                        continue
                    # 已唤醒：回应结束后留一段跟进窗口，超时重新挂起唤醒词
                    if (time.monotonic() > self._wake_followup_until
                            and not self._playback_busy()):
                        self._wake_gate = True
                        self._wake.reset()
                        self._emit({"type": "notice",
                                    "text": "已休眠，说唤醒词叫我"})
                        if paced:
                            await asyncio.sleep(FRAME_MS / 1000.0)
                        continue
                if self.debug_record:
                    self._up_pcm.extend(frame)
                self._up_frames += 1
                frame, gain, level = self._auto_gain(frame)
                self._track_mic_ref(level)         # 插话判据的用户电平参考
                if self._up_frames % 100 == 1:
                    # 诊断：确认麦克风真的在出声、回声门确实开着
                    print(f"[rt] 上行帧={self._up_frames} mic_rms={level:.0f} "
                          f"gain={gain:.2f}", flush=True)
                # 麦克风持续无音（约 9s）：给 UI 明确告警，而不是假装在聆听
                if level < 5.0:
                    self._silent_streak += 1
                    if self._silent_streak >= 150 and not self._mic_warned:
                        self._mic_warned = True
                        self._emit({"type": "mic_warning",
                                    "error": "麦克风持续没有采集到声音，请检查"
                                             "设备选择/输入音量/静音"})
                else:
                    self._silent_streak = 0
                try:
                    pkt = encoder.encode(frame, UP_FRAME)
                    await ws.send(pkt)
                except Exception:  # noqa: BLE001
                    break
                if paced:
                    await asyncio.sleep(FRAME_MS / 1000.0)  # 实时节奏

        # ---- 下行协程 ----
        async def downlink():
            async for raw in ws:
                if self._stop_flag.is_set():
                    break
                if isinstance(raw, bytes):
                    if time.monotonic() < self._drop_down_until:
                        continue           # 打断后仍在路上的音频，直接丢掉
                    try:
                        pcm = decoder.decode(raw, DOWN_FRAME)
                    except Exception:  # noqa: BLE001
                        continue
                    self.decoded_pcm.append(pcm)
                    if self.play_output:
                        out_q.put(pcm)
                    if self.play_output or self.virtual_playback:
                        self._note_queued(len(pcm))
                        self._note_down_level(pcm)   # 插话判定的回声参考
                    continue
                await self._handle_text(raw, ws)

        # ---- 打开音频流（按需）----
        in_stream = None
        out_stream = None
        try:
            if self.audio_feed is None:
                in_stream = _open_stream(
                    sd.RawInputStream, UP_SR, UP_FRAME, in_cb,
                    self._cand_devices(self.mic_index, "in"),
                    to_ui=self._emit_stream_note)
            if self.play_output:
                out_stream = _open_stream(
                    sd.RawOutputStream, DOWN_SR, DOWN_FRAME, out_cb,
                    self._cand_devices(self.speaker_index, "out"),
                    to_ui=self._emit_stream_note)
            if in_stream:
                in_stream.start()
            if out_stream:
                out_stream.start()
                # 真实声卡缓冲延迟：回声门要把它算进"还在播"的窗口
                with suppress(Exception):
                    lat = getattr(out_stream, "latency", None)
                    if lat:
                        self._out_latency = max(float(lat), OUT_LATENCY_MIN)
            # 握手
            await ws.send(json.dumps({
                "type": "hello",
                "device_id": DEVICE_ID,
                "device_name": DEVICE_NAME,
                "device_mac": DEVICE_MAC,
                "token": "",
                "version": 1,
                # mcp=True：本客户端声明支持 MCP，网关会就设备侧工具发起
                # initialize / tools/list（PC 作为设备侧 MCP server）。
                "features": {"mcp": True, "emoji": False},
                "audio_params": {
                    "format": "opus",
                    "sample_rate": DOWN_SR,
                    "channels": 1,
                    "frame_duration": FRAME_MS,
                },
            }))
            self._emit({"type": "state", "state": "listening"})
            if not self._wake_gate:
                await ws.send(json.dumps({
                    "type": "listen", "state": "start", "mode": "auto"}))
                self._listen_sent_ts = time.monotonic()
            else:
                self._emit({"type": "notice",
                            "text": "说出唤醒词开始对话"})
            # 会话开局把用户在界面里的选择同步给网关（网关重启后也保持一致）。
            # persist=true：桌面应用是音色的权威，网关要记住它（否则网关下次
            # 启动用的是自己那份可能被别的连接改过的配置，连上又变成另一个 ——
            # 这就是"音色在两个之间切换"）。不带 persist 的连接不会写盘。
            if self.voice:
                await ws.send(json.dumps({"type": "set_voice",
                                          "voice": self.voice,
                                          "persist": True}))
            if self.model:
                with suppress(Exception):
                    await ws.send(json.dumps({"type": "set_model",
                                              "model": self.model}))
            if self.prompt:
                with suppress(Exception):
                    await ws.send(json.dumps({"type": "set_prompt",
                                              "prompt": self.prompt}))
            with suppress(Exception):
                await ws.send(json.dumps({"type": "set_search",
                                          "enabled": bool(self.search)}))
            with suppress(Exception):
                await ws.send(json.dumps({"type": "set_tts_engine",
                                          "engine": self.tts_engine,
                                          "persist": True}))

            await asyncio.gather(uplink(), downlink())
            # gather 正常结束且不是用户主动停 ⇒ 连接意外断开，需要重连
            return not self._stop_flag.is_set()
        finally:
            self._ws = None
            try:
                await ws.send(json.dumps({"type": "abort"}))
            except Exception:  # noqa: BLE001
                pass
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
            if in_stream:
                with suppress(Exception):
                    in_stream.close()
            if out_stream:
                with suppress(Exception):
                    out_stream.close()
            if self.debug_record:
                with suppress():
                    self._save_debug_wav()

    # ---------- 调试录音 ----------
    def _debug_dir(self) -> str:
        import tempfile
        d = os.path.join(tempfile.gettempdir(), "XiaodAssistant_debug")
        os.makedirs(d, exist_ok=True)
        return d

    def _write_wav(self, path: str, pcm: bytes, sr: int):
        import wave
        if not pcm:
            return
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm)

    def _save_debug_wav(self):
        import time as _t
        stamp = _t.strftime("%Y%m%d_%H%M%S")
        d = self._debug_dir()
        up = os.path.join(d, f"voice_up_{stamp}.wav")
        down = os.path.join(d, f"voice_down_{stamp}.wav")
        self._write_wav(up, bytes(self._up_pcm), UP_SR)
        self._write_wav(down, b"".join(self.decoded_pcm), DOWN_SR)
        self.debug_files = {"up": up, "down": down}


    async def _rearm_listen(self, ws):
        """双保险：回应结束后重发 listen start。

        网关在 auto 模式下本应自行恢复采集（_resume_collecting），但旧版
        网关不会；不重发会导致助手播完后的第一句话被静默丢弃（对讲变聋）。
        刚由插话路径发过 listen start 时跳过，避免把插话的开口起始帧冲掉。"""
        # 唤醒词模式：回应结束后续一小段跟进窗口（用户可以接着说），
        # 窗口过后由 uplink 重新挂起唤醒词。
        if self._wake is not None and not self._wake_gate:
            self._wake_followup_until = time.monotonic() + self._wake_followup_s
        if time.monotonic() - self._listen_sent_ts < 1.0:
            return
        if self._wake_gate:
            return          # 已重新挂起唤醒词，不再自动重发 listen start
        self._listen_sent_ts = time.monotonic()
        try:
            await ws.send(json.dumps({
                "type": "listen", "state": "start", "mode": "auto"}))
        except Exception:  # noqa: BLE001
            pass

    # ---------- 文本消息（字幕）----------
    async def _handle_text(self, raw: str, ws):
        try:
            msg = json.loads(raw)
        except Exception:  # noqa: BLE001
            return
        t = msg.get("type")
        if t == "hello":
            self._emit({"type": "ready",
                        "session_id": msg.get("session_id")})
        elif t == "stt":
            self._emit({"type": "user", "text": msg.get("text", "")})
        elif t == "search":
            st = msg.get("state")
            if st == "start":
                self._emit({"type": "searching", "query": msg.get("query", "")})
            elif st == "end":
                self._emit({"type": "search_done",
                            "query": msg.get("query", ""),
                            "count": int(msg.get("count") or 0)})
        elif t == "notice":
            # 要让用户知道的一条提示（TTS 降级/恢复等）。没有 text 的是纯内部
            # 提示（老协议），不当通知发出去。
            if msg.get("text"):
                self._emit({"type": "notice", "text": str(msg["text"])})
        elif t == "mcp":
            await self._handle_mcp(msg, ws)
        elif t == "custom":
            self._handle_custom(msg)
        elif t == "tts":
            state = msg.get("state")
            if state == "start":
                self._resp_active = True   # 协议层播放期开始（回声门权威依据）
                self._emit({"type": "assistant_start"})
            elif state == "sentence_start":
                self._emit({"type": "assistant_delta",
                            "text": msg.get("text", "")})
            elif state == "stop":
                self._resp_active = False  # 播放期结束，之后只按尾音窗关门
                self._emit({"type": "assistant_stop"})
                await self._rearm_listen(ws)

    # ---------- MCP（PC 作为设备侧 MCP server）----------
    #
    # 网关是 MCP 客户端，本客户端是它眼里的"设备"。收到 initialize 回
    # serverInfo，tools/list 回 PC 能力清单，tools/call 执行后回结果。
    # 与官方 ESP32 固件的设备侧 MCP 走同一条协议路径。

    def _pc_tools(self):
        return [
            {
                "name": "self.pc.get_status",
                "description": "查看当前 PC 语音助手的运行状态（播放音量、音色等）",
                "inputSchema": {"type": "object", "properties": {},
                                "required": []},
            },
            {
                "name": "self.pc.set_volume",
                "description": "调整 PC 端扬声器播放音量",
                "inputSchema": {"type": "object", "properties": {
                    "volume": {"type": "number",
                               "description": "音量倍数 0.3~1.6，1.0 为原始音量"}},
                    "required": ["volume"]},
            },
        ]

    async def _mcp_reply(self, ws, rid, result=None, error=None):
        payload = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result if result is not None else {}
        try:
            await ws.send(json.dumps({"type": "mcp", "payload": payload},
                                     ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass

    async def _handle_mcp(self, msg, ws):
        payload = msg.get("payload")
        if not isinstance(payload, dict) or "method" not in payload:
            return
        method = payload.get("method")
        rid = payload.get("id")
        if method == "initialize":
            await self._mcp_reply(ws, rid, result={
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "XiaoD-PC", "version": "1.0.0"},
            })
        elif method == "tools/list":
            await self._mcp_reply(ws, rid, result={"tools": self._pc_tools()})
        elif method == "tools/call":
            params = payload.get("params") or {}
            name = str(params.get("name") or "")
            args = params.get("arguments") or {}
            text, is_err = self._pc_call_tool(name, args)
            await self._mcp_reply(ws, rid, result={
                "content": [{"type": "text", "text": text}],
                "isError": bool(is_err),
            })
        else:
            await self._mcp_reply(ws, rid, error={
                "code": -32601, "message": f"未知方法：{method}"})

    def _pc_call_tool(self, name, args):
        """执行一个 PC 侧工具，返回 (文本结果, 是否错误)。"""
        if not isinstance(args, dict):
            args = {}
        if name == "self.pc.get_status":
            voice = getattr(self, "voice", "") or "(未指定)"
            vol = getattr(self, "play_volume", 1.0)
            return (f"PC 语音助手状态：播放音量 {vol:.2f}；音色 {voice}。", False)
        if name == "self.pc.set_volume":
            try:
                val = float(args.get("volume", args.get("value", 1.0)))
            except (TypeError, ValueError):
                return ("音量参数无效", True)
            self.set_volume(val)
            self._emit({"type": "notice",
                        "text": f"音量已调整为 {self.play_volume:.2f}"})
            return (f"音量已调整为 {self.play_volume:.2f}", False)
        return (f"未知工具：{name}", True)

    def _handle_custom(self, msg):
        """处理网关发来的自定义控制消息（内置工具操作客户端时用）。"""
        action = msg.get("action")
        if action == "set_volume":
            try:
                self.set_volume(float(msg.get("value", 1.0)))
            except (TypeError, ValueError):
                pass
