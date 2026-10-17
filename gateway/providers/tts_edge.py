# -*- coding: utf-8 -*-
"""EdgeTTS：在线神经音色 → MP3 → 24k PCM → opus。

**为什么不用 ffmpeg**：edge-tts 只吐 MP3（消费级端点不接受 raw PCM），
以前靠 `reference/bin/ffmpeg.exe` 转码，但那个 ffmpeg 是共享库版，
旁边一堆 av*.dll 加起来 148MB，根本塞不进单文件 exe。打包版里
`_MEIPASS/bin/` 只有 opus.dll → ffmpeg 拉不起来 → 在线合成必然失败，
于是每句都静默降级到本地离线音色（表现为"音色切换不生效/被强制离线"）。

改用 **miniaudio**（wheel 仅 281KB，自带 dr_mp3）在进程内解码：
  * 39KB MP3 整段解码只要 ~9ms；
  * **截断的 MP3 也能解出已完整的帧**，所以可以"边收边解"，
    每收到一块就解码、只产出新增的那段 PCM，首包延迟和以前一样低。

stream() 为真流式：ws 收到音频块 → 增量解码 → 边编码 opus 边 yield。
"""
from __future__ import annotations

import asyncio
import re
from typing import List

import edge_tts
import edge_tts.communicate
import miniaudio

from gateway.audio.opus import DownlinkEncoder
from gateway.providers.base import BaseTTS

# edge-tts 7.2.8 旧端点 403，切换到新端点（M1 已验证）
_NEW_WSS_URL = (
    "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
    f"?Ocp-Apim-Subscription-Key={edge_tts.constants.TRUSTED_CLIENT_TOKEN}"
)
edge_tts.constants.WSS_URL = _NEW_WSS_URL
edge_tts.communicate.WSS_URL = _NEW_WSS_URL


_official: dict = {}          # {voice_id: 显示名}，来自官方 list_voices()
_official_at: float = 0.0
_OFFICIAL_TTL = 1800.0        # 官方列表半小时刷一次就够
_WANT_LOCALES = ("zh-", "en-", "ja-", "ko-", "yue-")


def official_voices_cached() -> dict:
    """拿已经缓存好的官方音色表（可能为空——还没拉到或离线）。

    这是给同步代码用的：`all_voices()` 这种要在轮询里即时返回的场景，
    不能为了补全列表去 await 一次网络请求。"""
    return _official


async def official_voices() -> dict:
    """从 edge-tts 官方接口拉全量音色（带缓存），失败返回空 dict。

    config.EDGE_VOICES 只是人工精选的一部分；官方有 300+ 音色而且会增删，
    这里运行时补一次，保证下拉里选得到的 id 都能被网关接受。
    """
    global _official, _official_at
    import time
    if _official and time.monotonic() - _official_at < _OFFICIAL_TTL:
        return _official
    try:
        vs = await edge_tts.list_voices()
    except Exception:  # noqa: BLE001
        return _official or {}
    out = {}
    for v in vs or ():
        short = v.get("ShortName") or ""
        if not short or not short.startswith(_WANT_LOCALES):
            continue
        loc = v.get("Locale") or ""
        gender = (v.get("Gender") or "").lower()
        who = "女声" if gender.startswith("female") else (
            "男声" if gender.startswith("male") else "")
        tag = ""
        try:
            p = (v.get("VoiceTag") or {}).get("VoicePersonalities") or []
            if p:
                tag = "·" + str(p[0])
        except Exception:  # noqa: BLE001
            pass
        out[short] = f"{short.replace('Neural', '')}（{loc}{who}{tag}）"
    if out:
        _official = out
        _official_at = time.monotonic()
    return _official


_VOICE_ID_RE = re.compile(
    r"^[a-z]{2}-[A-Za-z]{2,3}(-[A-Za-z]+)?-[A-Za-z0-9_]+Neural$")


def known_voice(voice: str) -> bool:
    """音色 id 是否被网关认可（静态精选 或 官方列表 或 本地前缀）。

    **不能只认已知列表**：官方列表是联网后异步拉取的，没拉到时（刚启动/
    断网）客户端下发的合法音色会被判"未知"而丢弃 —— 网关继续用旧音色，
    界面却显示新音色，用户听到的就是"音色自己和界面对不上"。所以形态
    合法的 id 一律放行（真无效的话合成时会失败并降级，不会卡死）。"""
    v = (voice or "").strip()
    if not v:
        return False
    if v.startswith("local:"):
        return True
    from gateway.config import EDGE_VOICES
    if v in EDGE_VOICES:
        return True
    if v in _official:
        return True
    return bool(_VOICE_ID_RE.match(v))


def _decode_mp3(data: bytes, sample_rate: int = 24000) -> bytes:
    """MP3 → s16le 单声道 PCM（miniaudio，纯进程内，无外部进程）。

    传入被截断的 MP3 也不会抛异常，只会解出已经完整的那些帧 ——
    这正是增量解码依赖的特性。"""
    if not data:
        return b""
    try:
        dec = miniaudio.decode(
            data,
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=1,
            sample_rate=sample_rate,
        )
    except Exception:  # noqa: BLE001
        return b""
    return bytes(dec.samples) if dec.samples else b""


class EdgeTTS(BaseTTS):
    def __init__(self, voice: str, sample_rate: int = 24000,
                 frame_size: int = 1440):
        self.voice = voice
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self._frame_bytes = frame_size * 2

    @staticmethod
    def _ssml_kwargs(style: dict | None) -> dict:
        """把 1.0 基准的倍数翻译成 edge-tts 的 rate/pitch/volume 字符串。"""
        st = style or {}
        r = float(st.get("rate", 1.0) or 1.0)
        p = float(st.get("pitch", 1.0) or 1.0)
        v = float(st.get("volume", 1.0) or 1.0)
        out = {}
        if abs(r - 1.0) >= 0.02:
            out["rate"] = f"{(r - 1.0) * 100:+.0f}%"
        if abs(p - 1.0) >= 0.02:
            # 1.0 → 0Hz；每 1% 变化约 2Hz（听感自然）
            out["pitch"] = f"{(p - 1.0) * 200:+.0f}Hz"
        if abs(v - 1.0) >= 0.02:
            out["volume"] = f"{(v - 1.0) * 100:+.0f}%"
        return out

    def _communicate(self, text: str, style: dict | None):
        return edge_tts.Communicate(text, voice=self.voice,
                                    **self._ssml_kwargs(style))

    async def _mp3(self, text: str, style: dict | None = None) -> bytes:
        comm = self._communicate(text, style)
        buf = bytearray()
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                buf.extend(chunk["data"])
        return bytes(buf)

    async def _to_pcm(self, mp3: bytes) -> bytes:
        """保持旧接口名：MP3 → 24k PCM（内部已换成 miniaudio）。"""
        if not mp3:
            return b""
        return await asyncio.to_thread(_decode_mp3, mp3, self.sample_rate)

    async def synthesize(self, text: str, style: dict | None = None
                         ) -> List[bytes]:
        text = text.strip()
        if not text:
            return []
        mp3 = await self._mp3(text, style)
        if not mp3:
            return []
        pcm = await self._to_pcm(mp3)
        if not pcm:
            return []
        # 每次合成独立 encoder（句子内帧连续；跨句无需共享，避免多会话状态串扰）
        encoder = DownlinkEncoder(self.sample_rate, self.frame_size)
        return encoder.encode_pcm(pcm)

    async def stream(self, text: str, style: dict | None = None):
        """真流式：ws 音频块 → 增量解码 → opus，逐包 yield。

        每收到一块就把"到目前为止的整段 mp3"解一遍，只产出比上次多的那部分
        PCM。整段解码不到 10ms，且 MP3 帧彼此独立，重复解码不会有问题。"""
        text = text.strip()
        if not text:
            return
        encoder = DownlinkEncoder(self.sample_rate, self.frame_size)
        comm = self._communicate(text, style)
        mp3 = bytearray()
        pcm_buf = bytearray()
        emitted = 0
        async for chunk in comm.stream():
            if chunk["type"] != "audio":
                continue
            mp3.extend(chunk["data"])
            pcm = await asyncio.to_thread(_decode_mp3, bytes(mp3),
                                          self.sample_rate)
            if len(pcm) <= emitted:
                continue
            pcm_buf.extend(pcm[emitted:])
            emitted = len(pcm)
            while len(pcm_buf) >= self._frame_bytes:
                frame = bytes(pcm_buf[:self._frame_bytes])
                del pcm_buf[:self._frame_bytes]
                yield encoder.encode_frame(frame)
        # 收尾：不足一帧的余量补零成整帧，避免句尾被吞掉
        if pcm_buf:
            pcm_buf.extend(b"\x00" * (self._frame_bytes - len(pcm_buf)))
            yield encoder.encode_frame(bytes(pcm_buf))
