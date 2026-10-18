# -*- coding: utf-8 -*-
"""TTS 路由：在线（EdgeTTS 神经音色）与离线（Windows SAPI 本地音色）二选一。

统一音色 id 约定：
  * 在线音色：直接就是 edge-tts 的 voice id，例如 `zh-CN-XiaoxiaoNeural`
  * 离线音色：`local:` 前缀 + SAPI token id，
    例如 `local:HKEY_LOCAL_MACHINE\\...\\TTS_MS_ZH-CN_HUIHUI_11.0`

引擎模式（config.tts_engine）：
  * auto  —— 优先在线；在线失败/超时/断网时**自动降级**到本地离线音色，
             并进入一段冷却期，避免每句都先卡一次超时。
  * online—— 只用在线（失败就报错，不做降级）
  * local —— 只用本地离线
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import AsyncIterator, List, Optional

from gateway.providers.base import BaseTTS
from gateway.providers.tts_edge import EdgeTTS
from gateway.providers.tts_edge import official_voices_cached
from gateway.providers import tts_edge
from gateway.providers.tts_sapi import SapiTTS, list_voices as local_voices
from gateway.providers.tts_sapi import _guess_lang
from gateway.providers import tts_kokoro

LOCAL_PREFIX = "local:"
# 在线失败后进入冷却，这段时间直接用本地，不再逐句试在线。
# **指数退避**：第一次失败只冷却 45s（ EdgeTTS 的 NoAudioReceived 多为秒级
# 抖动，很快能恢复）；连续失败才逐步拉长（90→180→360→600s 封顶）。
# 旧实现固定 60s：网络一抖就换本地音色、60s 后又切回在线 —— 用户听到的就是
# "音色自己变来变去"。连续成功 3 句才重置退避。
_COOLDOWN_BASE = 45.0
_COOLDOWN_MAX = 600.0
_OK_STREAK_RESET = 3      # 连续成功这么多句 → 认定在线稳定，重置退避
# 首包失败重试：EdgeTTS 消费级端点偶发"No audio was received"，
# 一次抖动就换掉音色（用户听感是"音色自己变了"）比多等两次连接更难受。
_TTS_ATTEMPTS = 3
_TTS_RETRY_DELAY = 0.35   # 两次重试之间歇一下，别把端点打得更火
# **降级粘滞**：在线挂过一次后，冷却到期也不许在"对话进行中"切回在线——
# 上一句本地音色、下一句在线音色，用户听到的就是"两个音色来回换"（实测
# 用户原话："我发现你好像音色就是两个音色在切换"）。只有对话真的停了
# （距上次合成超过这么久）才允许回在线。
_STICKY_IDLE_S = 45.0

# 在线音色 → 本地兜底偏好（语言变体, 性别）。降级时尽量别把"说话的人"
# 换掉：台湾女声不该掉到简体男声头上，男声也不该用女声兜。
_LOCAL_PREF = {
    "zh-CN-XiaoxiaoNeural": ("zh-CN", "f"),
    "zh-CN-XiaoyiNeural": ("zh-CN", "f"),
    "zh-CN-Liaoning-XiaobeiNeural": ("zh-CN", "f"),
    "zh-CN-Shaanxi-XiaoniNeural": ("zh-CN", "f"),
    "zh-CN-YunxiaNeural": ("zh-CN", "m"),
    "zh-CN-YunxiNeural": ("zh-CN", "m"),
    "zh-CN-YunyangNeural": ("zh-CN", "m"),
    "zh-TW-HsiaoChenNeural": ("zh-TW", "f"),
    "zh-TW-HsiaoYuNeural": ("zh-TW", "f"),
    "zh-TW-YunJheNeural": ("zh-TW", "m"),
    "zh-HK-HiuGaaiNeural": ("zh-HK", "f"),
    "en-US-JennyNeural": ("en-US", "f"),
    "en-US-AriaNeural": ("en-US", "f"),
}

# SAPI 音色描述（"Microsoft Huihui Desktop - Chinese (Simplified)"）里的
# 名字 token → 性别。命中不了就按"同语言优先"回退，不猜性别。
_SAPI_FEMALE = {"huihui", "hanhan", "yaoyao", "tracy", "tingting",
                "xiaoxiao", "hui", "lili", "meng", "xiaoyi", "zira",
                "hazel", "susan", "eva"}
_SAPI_MALE = {"kangkang", "yunxi", "yunyang", "kang", "david", "danny",
              "yunjie", "wei"}


def _sapi_gender(desc: str) -> str:
    """从 SAPI 音色描述里猜性别（'Microsoft Huihui Desktop - ...' → 'f'）。"""
    toks = (desc or "").replace("-", " ").split()
    for t in toks:
        w = t.strip().lower()
        if w in _SAPI_FEMALE:
            return "f"
        if w in _SAPI_MALE:
            return "m"
    return ""


# ---------- 可朗读化：把 LLM 输出里"不是给人耳"的符号洗掉 ----------
# 用户原话："那个信号特殊符号和以些表情就不要再读出来了" —— 星号/井号/
# emoji/箭头这些 TTS 要么念名字要么跳不出声，念出来就是机器味。

# Markdown 链接 [文字](url) → 只留文字；图片 ![alt](url) → 删
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
# Markdown/装饰符号：星号、下划线、反引号、井号、波浪线、尖括号、竖线、反斜杠
_MD_CHARS = re.compile(r"[*_`#~^|\\<>]+")
# 行首列表符号（- / • / · / ▪ / *）→ 删（保留行内的连字符，如 Wi-Fi、-5）
_BULLET = re.compile(r"(?m)^\s*[-•·▪*]\s+")
# emoji / 表情符号 / 装饰符号（念出来全是"笑脸""星星"之类的噪音）：
#   补充符号区、杂项符号、杂项技术、几何图形、丁巴特、箭头、区域旗、
#   变体选择符、零宽连接符、组合键帽、专用区
_EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # emoji 主区（含表情/手势/物品/旗帜）
    "\U0001F900-\U0001F9FF"   # 补充符号
    "\U00002600-\U000027BF"   # 杂项符号 + 丁巴特（★☆♪✂…）
    "\U00002B00-\U00002BFF"   # 箭头/星形（⭐⬆…）
    "\U00002190-\U000021FF"   # 箭头（→←）
    "\U00002300-\U000023FF"   # 杂项技术（⏰⌘）
    "\U000025A0-\U000025FF"   # 几何图形（●○▪）
    "\U0000FE00-\U0000FE0F"   # 变体选择符
    "\U0000200B-\U0000200F"   # 零宽字符/方向标记
    "\U000020E3"              # 组合键帽
    "\U0000E000-\U0000F8FF"   # 私有区（部分客户端用这区放表情）
    "]+")
# 多余空白折叠
_SPACES = re.compile(r"[ \t]{2,}")


def _speakable(text: str) -> str:
    """把 LLM 回复洗成适合朗读的纯文本（不动中文标点与常规符号）。"""
    if not text:
        return text
    out = _MD_LINK.sub(r"\1", text)
    out = _EMOJI.sub("", out)
    out = _BULLET.sub("", out)
    out = _MD_CHARS.sub("", out)
    out = _SPACES.sub(" ", out)
    return out.strip()


# 在线音色 → 本地神经音色的直接对应（同名最好：降级后听感上"不太像换了
# 个人"）。没精确对应的按性别/语种落到最接近的那个。
_NEURO_MAP = {
    "zh-CN-XiaoxiaoNeural": "zf_xiaoxiao",
    "zh-CN-XiaoyiNeural": "zf_xiaoyi",
    "zh-CN-Liaoning-XiaobeiNeural": "zf_xiaobei",
    "zh-CN-Shaanxi-XiaoniNeural": "zf_xiaoni",
    "zh-TW-HsiaoChenNeural": "zf_xiaoni",
    "zh-TW-HsiaoYuNeural": "zf_xiaobei",
    "zh-HK-HiuGaaiNeural": "zf_xiaoni",
    "zh-CN-YunxiNeural": "zm_yunxi",
    "zh-CN-YunjianNeural": "zm_yunjian",
    "zh-CN-YunxiaNeural": "zm_yunxia",
    "zh-CN-YunyangNeural": "zm_yunyang",
    "zh-TW-YunJheNeural": "zm_yunjian",
    "en-US-JennyNeural": "af_heart",
    "en-US-AriaNeural": "af_bella",
    "en-US-AnaNeural": "af_bella",
    "en-US-JaneNeural": "bf_emma",
    "en-GB-SoniaNeural": "bf_emma",
    "en-GB-RyanNeural": "bm_george",
    "en-US-GuyNeural": "am_adam",
    "en-US-ChristopherNeural": "am_michael",
    "en-US-EricNeural": "am_liam",
    "en-US-DavisNeural": "am_liam",
}

# EDGE_VOICES 里地区名的大小写并不统一（liaoning / shaanxi 是小写，其余
# 地区名大写），而这里的 key 是按"读着顺"写的。查表一律用小写索引，
# 否则东北/陕西腔降级时会查不到、静默落到默认女声。
_NEURO_MAP_LOW = {k.lower(): v for k, v in _NEURO_MAP.items()}
_LOCAL_PREF_LOW = {k.lower(): v for k, v in _LOCAL_PREF.items()}

# 在线音色 id 里的性别线索（给 _NEURO_MAP 没覆盖到的音色兜底）
_GENDER_FEMALE = ("xiaoxiao", "xiaoyi", "xiaobei", "xiaoni", "xiaohan",
                  "hsiaochen", "hsiaoyu", "hiugaai", "hiulai", "jenny",
                  "aria", "jane", "nancy", "amber", "ashley", "emma",
                  "michelle", "ana", "sonia", "libby", "luna", "katie",
                  "sara", "lily", "elsa")
_GENDER_MALE = ("yunxi", "yunyang", "yunxia", "yunjian", "yunjie", "yunye",
                "yunjhe", "kangkang", "david", "danny", "guy", "brian",
                "christopher", "eric", "jacob", "tony", "roger", "ryan",
                "alan", "mark", "george", "liam", "adam", "michael")


def neuro_available() -> bool:
    """本机有没有本地神经合成模型（决定"离线"这一组显示什么）。"""
    try:
        return tts_kokoro.available()
    except Exception:  # noqa: BLE001
        return False


def _pick_neuro(voice: str) -> str:
    """在线音色 id → 本地神经音色 id；模型不可用返回空串。"""
    if not neuro_available():
        return ""
    v = (voice or "").strip()
    if not v:
        return "zf_xiaoxiao"
    if tts_kokoro.is_neuro_id(v):
        return v                      # 用户直接选的就是神经音色
    hit = _NEURO_MAP_LOW.get(v.lower())
    if hit:
        return hit
    low = v.lower()
    lang_en = low.startswith("en-")
    if any(k in low for k in _GENDER_MALE):
        return "am_adam" if lang_en else "zm_yunxi"
    if any(k in low for k in _GENDER_FEMALE):
        return "af_heart" if lang_en else "zf_xiaoxiao"
    # 认不出来：中文用温柔女声，其它语种用英语女声
    return "af_heart" if lang_en else "zf_xiaoxiao"


def _pick_local(voice: str) -> str:
    """在线音色 id → 语义最接近的本地音色 id（空串＝系统默认）。

    本机有神经合成模型（Kokoro）时优先用它：8 个中文音色 + 8 个英文音色，
    听感远比 SAPI 自然——SAPI 通常只有一两个机械音色，降级过去就是用户
    抱怨的"音色太生硬"。没有模型才退回 SAPI 的按语言变体 + 性别匹配。"""
    neuro = _pick_neuro(voice)
    if neuro:
        return neuro
    v = (voice or "")
    if not v or "-" not in v:
        return ""
    pref = _LOCAL_PREF_LOW.get(v.lower())
    if pref is None:
        # 表里没有的音色：按 id 前缀猜（zh-TW-xxx → 台湾、en-xxx → 英语）
        lang = "-".join(v.split("-")[:2])
        gender = "f" if any(k in v.lower() for k in
                            ("xiaoyi", "xiaoxiao", "hsiaochen", "hsiaoyu",
                             "hiugaai", "jenny", "aria", "han", "xia")) else ""
        pref = (lang, gender)
    want_lang, want_gender = pref
    try:
        cands = local_voices()
    except Exception:  # noqa: BLE001
        return ""
    if not cands:
        return ""

    def _rank(c):
        lg = _guess_lang(c.get("name") or "")
        gd = _sapi_gender(c.get("name") or "")
        score = 0
        if lg == want_lang:
            score -= 100
        elif lg and lg.split("-")[0] == want_lang.split("-")[0]:
            score -= 50           # 同语种不同变体（简体/繁体）
        if want_gender and gd == want_gender:
            score -= 20
        elif gd:
            score += 20           # 性别不符，作为减分项
        return score

    best = min(cands, key=_rank)
    return best["id"]


class TTSRouter(BaseTTS):
    def __init__(self, voice: str, sample_rate: int = 24000,
                 frame_size: int = 1440, engine: str = "auto"):
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self._online = EdgeTTS(self._edge_voice(voice), sample_rate, frame_size)
        self._local = LocalFallbackTTS(self._local_voice(voice), sample_rate,
                                       frame_size)
        self.voice = voice
        self.engine = (engine or "auto").lower()
        self._down_until = 0.0     # 在线引擎的冷却截止时间
        self._sticky_local = False  # 降级粘滞：对话没停就一直用本地
        self._last_synth_ts = 0.0   # 最近一次合成结束的时刻（判"对话是否停了"）
        self._fail_streak = 0      # 连续失败次数（决定退避时长）
        self._ok_streak = 0        # 连续成功次数（重置退避用）
        self.last_engine = ""      # 实际使用的引擎（诊断）
        self.online_failures = 0
        # 在线依赖缺失时直接标记降级，界面/日志能说明原因
        self.degraded = not tts_edge.available()
        self.last_error = (tts_edge.import_error()
                           if not tts_edge.available() else "")

    # ---------- 音色解析 ----------

    @staticmethod
    def _edge_voice(voice: str) -> str:
        v = voice or ""
        if v.startswith(LOCAL_PREFIX):
            return "zh-CN-XiaoxiaoNeural"     # 选的是离线音色，在线用默认兜底
        return v or "zh-CN-XiaoxiaoNeural"

    @staticmethod
    def _local_voice(voice: str) -> str:
        v = voice or ""
        if v.startswith(LOCAL_PREFIX):
            return v[len(LOCAL_PREFIX):]
        return _pick_local(v)      # 在线音色 → 同语言的本地音色，避免降级换语言

    def apply_voice(self, voice: str) -> None:
        """只换音色，不动冷却/退避/粘滞（**按会话**对齐音色时用这个）。

        网关的 TTS 是全局单例，多个连接（桌面应用 + 设备）共享。若每个会话
        开局都用 set_voice 去改它，两个来源不一致时就会互相覆盖 —— 用户听到
        的就是"交互中音色自己变了"。所以会话只记自己的音色，合成前用它把
        共享实例对齐一次，不碰"在线是否可用"的判定状态。"""
        if not voice or voice == self.voice:
            return
        self.voice = voice
        self._online.voice = self._edge_voice(voice)
        self._local.voice_id = self._local_voice(voice)

    def set_voice(self, voice: str) -> None:
        """用户**显式**换音色：换音色 + 给在线一次新机会（清冷却与退避）。"""
        self.voice = None          # 让 apply_voice 一定生效
        self.apply_voice(voice)
        self._down_until = 0.0
        self._sticky_local = False
        self._fail_streak = 0
        self._ok_streak = 0

    @property
    def cooldown_left(self) -> float:
        return max(0.0, self._down_until - time.monotonic())

    @property
    def local_label(self) -> str:
        """当前本地兜底音色的短名（提示用户"临时换成了谁"）。"""
        vid = self._local.voice_id
        if not vid:
            return "系统默认本地音色"
        if tts_kokoro.is_neuro_id(vid):
            item = tts_kokoro.ALL_VOICES.get(vid)
            return item[1] if item else "本地神经音色"
        try:
            for v in local_voices():
                if v.get("id") == vid:
                    return _short_local(v.get("name") or "")
        except Exception:  # noqa: BLE001
            pass
        return "本地音色"

    def set_engine(self, engine: str) -> None:
        if engine in ("auto", "online", "local"):
            self.engine = engine
            self._down_until = 0.0   # 显式改引擎＝用户要立刻生效，别卡在冷却里
            self._sticky_local = False
            self._fail_streak = 0
            self._ok_streak = 0

    @property
    def want_local(self) -> bool:
        """当前音色/引擎是否走本地离线。"""
        if self.engine == "local":
            return True
        # 在线依赖缺失（edge-tts / miniaudio 没装）→ 直接走本地：否则每句
        # 都要先试在线、失败再降级，用户多等几秒还刷一堆失败日志。
        if not tts_edge.available():
            return True
        if self.engine == "online":
            return False
        if (self.voice or "").startswith(LOCAL_PREFIX):
            return True
        if time.monotonic() < self._down_until:
            return True
        # 冷却过了，但对话还在继续 → 继续本地。等用户真的停下来再回在线，
        # 否则一句本地一句在线，听感就是"两个音色轮换"。
        if self._sticky_local and \
                (time.monotonic() - self._last_synth_ts) < _STICKY_IDLE_S:
            return True
        return False

    # ---------- 合成 ----------

    async def _stream_local(self, text: str, style: Optional[dict]):
        async for p in self._local.stream(text, style):
            yield p

    async def _stream_online(self, text: str, style: Optional[dict]):
        async for p in self._online.stream(text, style):
            yield p

    async def _first_packet(self, text: str, style: Optional[dict]):
        """取在线合成的首包；失败返回 (None, None)，并把原因记到 last_error。"""
        it = self._stream_online(text, style)
        try:
            return await it.__anext__(), it
        except StopAsyncIteration:
            self.last_error = "服务端没有返回音频"
            return None, None
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)[:120]
            return None, None

    async def stream(self, text: str, style: Optional[dict] = None):
        text = _speakable((text or "").strip())
        if not text:
            return
        try:
            async for p in self._stream_pick(text, style):
                yield p
        finally:
            # 记"这句说完了"的时刻：粘滞窗口从这里起算
            self._last_synth_ts = time.monotonic()

    async def _stream_pick(self, text: str, style: Optional[dict] = None):
        if self.want_local:
            self.last_engine = "local"
            async for p in self._stream_local(text, style):
                yield p
            return

        # 先拿"首包"再决定：在线合成可能已经吐了几包才断，若此时直接降级，
        # 用户会听到同一句被念两遍。首包成功就当在线可用，后续失败不再回退。
        for attempt in range(_TTS_ATTEMPTS):
            first, it = await self._first_packet(text, style)
            if first is None:
                print(f"[tts] 在线合成失败（第 {attempt + 1}/{_TTS_ATTEMPTS} 次）: "
                      f"{self.last_error}", flush=True)
                if attempt + 1 < _TTS_ATTEMPTS:
                    await asyncio.sleep(_TTS_RETRY_DELAY)
                continue
            self.last_engine = "online"
            self._sticky_local = False   # 在线确实通了，解除粘滞
            # 在线通了：累计成功次数，够 3 句就认为稳定、重置退避
            self._ok_streak += 1
            if self._ok_streak >= _OK_STREAK_RESET:
                self._fail_streak = 0
            if self.degraded:      # 上一句降级过，这一句又通了 → 报告恢复
                self.degraded = False
            yield first
            try:
                async for p in it:
                    yield p
            except Exception as e:  # noqa: BLE001
                print(f"[tts] 在线合成中断({e})，本句已发出的部分保留",
                      flush=True)
            return

        # 重试后仍拿不到首包 → 判定在线不可用，降级到本地并进入冷却
        self.online_failures += 1
        self._fail_streak += 1
        self._ok_streak = 0
        # 指数退避：45s → 90s → 180s → 360s → 600s 封顶。
        # 连续失败才越退越长，一次抖动只冷却一小会儿，音色不会来回翻。
        cool = min(_COOLDOWN_MAX,
                   _COOLDOWN_BASE * (2 ** max(0, self._fail_streak - 1)))
        self._down_until = time.monotonic() + cool
        self._sticky_local = True     # 对话没停之前不再切回在线
        self.degraded = True
        print(f"[tts] 在线合成不可用（重试 {_TTS_ATTEMPTS} 次仍无音频："
              f"{self.last_error}），改用本地音色「{self.local_label}」；"
              f"本次对话持续使用，停顿 {_STICKY_IDLE_S:.0f}s 后才回在线"
              f"（冷却 {cool:.0f}s，第 {self._fail_streak} 次连续失败）",
              flush=True)
        self.last_engine = "local"
        async for p in self._stream_local(text, style):
            yield p

    async def synthesize(self, text: str, style: Optional[dict] = None
                         ) -> List[bytes]:
        out: List[bytes] = []
        async for p in self.stream(text, style):
            out.append(p)
        return out


async def _preview(self, text: str, style: Optional[dict] = None) -> bytes:
    """试听用：返回 24k s16le PCM（在线失败自动用本地）。

    last_engine 一并记下，界面据此显示"这条样本实际是谁合成的"。"""
    text = _speakable((text or "").strip())
    if not self.want_local:
        self.last_engine = "online"
        try:
            mp3 = await self._online._mp3(text, style)
            if mp3:
                return await self._online._to_pcm(mp3)
        except Exception:  # noqa: BLE001
            pass
    self.last_engine = "local"
    try:
        return await self._local._pcm(text, style)
    except Exception:  # noqa: BLE001
        return b""


TTSRouter.preview_pcm = _preview


def all_voices() -> List[dict]:
    """全部可选音色（在线 + 本地神经 + 本地 SAPI），带分组标签。

    在线部分 = config 精选 + edge-tts 官方全量（连网时 300+），官方列表可能
    还没拉到（首帧/离线），那就只有精选部分，不会阻塞。"""
    from gateway.config import EDGE_VOICES
    out: List[dict] = []
    try:
        official = tts_edge.official_voices_cached()
    except Exception:  # noqa: BLE001
        official = {}
    seen = set()
    for k, label in list(EDGE_VOICES.items()) + sorted(official.items()):
        if k in seen:
            continue
        seen.add(k)
        out.append({"id": k, "name": label, "engine": "online",
                    "group": "在线 · 神经音色（需联网）"})
    # 本地神经音色排在前面：本机没模型时这一组为空，自然只剩 SAPI
    for v in tts_kokoro.list_voices():
        out.append({"id": LOCAL_PREFIX + v["id"], "engine": "local",
                    "group": v["group"], "name": v["name"]})
    for v in local_voices():
        out.append({"id": LOCAL_PREFIX + v["id"], "engine": "local",
                    "group": "离线 · 系统音色",
                    "name": f"{_short_local(v['name'])}（系统）"})
    return out


def _short_local(desc: str) -> str:
    d = (desc or "").replace("Desktop", "").replace("Microsoft", "").strip()
    d = d.replace(" - Chinese (Simplified)", "·简体").replace(
        " - Chinese (Traditional)", "·繁体")
    d = d.replace(" - English (United States)", "·英语").replace(
        " - Chinese (Taiwan)", "·台湾")
    return d.strip(" -·") or desc


class LocalFallbackTTS(BaseTTS):
    """本地离线的两级兜底：优先神经合成（Kokoro），没有模型才用 SAPI。

    SAPI 是拼接合成，本机通常也只有一两个音色（实测只有简体 Huihui + 英语
    Zira），降级过去就是用户抱怨的"音色太生硬"。Kokoro 有 8 个中文 +
    8 个英文音色且是神经网络合成，能用就优先用。
    """

    engine = "local"

    def __init__(self, voice_id: str = "", sample_rate: int = 24000,
                 frame_size: int = 1440):
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self._voice_id = ""
        self._neuro = tts_kokoro.KokoroTTS(voice_id, sample_rate, frame_size)
        self._sapi = SapiTTS(voice_id, sample_rate, frame_size)
        self.voice_id = voice_id or ""      # 走 setter，同步到两个实现

    @property
    def voice_id(self) -> str:
        return self._voice_id

    @voice_id.setter
    def voice_id(self, value: str) -> None:
        """必须同步到内部实现：Kokoro/SAPI 各持一份 voice_id，只改外层的话
        真正合成时仍用构造时的旧音色（用户听到的就是"切了音色没变"）。"""
        v = value or ""
        object.__setattr__(self, "_voice_id", v)
        for impl in (getattr(self, "_neuro", None),
                     getattr(self, "_sapi", None)):
            if impl is not None:
                impl.voice_id = v

    def _pick(self):
        """按当前音色 id 选择实现：神经音色 id 走 Kokoro，其余走 SAPI。"""
        if self.voice_id and tts_kokoro.is_neuro_id(self.voice_id) \
                and neuro_available():
            return self._neuro
        return self._sapi

    async def _pcm(self, text: str, style: Optional[dict]) -> bytes:
        impl = self._pick()
        pcm = await impl._pcm(text, style)
        if pcm:
            return pcm
        # 神经合成中途挂了（模型被删/OOM）→ 还能退回 SAPI 就别哑掉
        if impl is self._neuro:
            return await self._sapi._pcm(text, style)
        return b""

    async def synthesize(self, text: str, style: Optional[dict] = None
                         ) -> List[bytes]:
        return await self._pick().synthesize(text, style)

    async def stream(self, text: str, style: Optional[dict] = None):
        async for p in self._pick().stream(text, style):
            yield p
