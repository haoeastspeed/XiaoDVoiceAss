# -*- coding: utf-8 -*-
"""离线 TTS：Windows SAPI（系统自带语音合成）。

完全离线、零下载，用系统已安装的语音包发声。音质不如在线神经音色，
但断网/无外网时是可靠兜底（界面上标为"离线"音色）。

实现要点（都是踩过的坑）：
  * `SpFileStream.Format.Type = ...` 在 comtypes 下会抛
    `0x80045002`，**不要**去设格式；用文件流默认格式（22050/16bit/mono），
    读 WAV 头拿到真实采样率后自己重采样到 24k。
  * `SpMemoryStream.GetData()` 返回的是**没有 RIFF 头**的裸 PCM，拿不到
    采样率，所以这里坚持用文件流。
  * SSML 必须带 `<speak version=...>` 根元素，否则报
    "SSML input not valid; cannot find 'speak' element"。
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import wave
from typing import List, Optional

from gateway.audio.opus import DownlinkEncoder
from gateway.providers.base import BaseTTS

_SVSF_IS_XML = 8
_SVSF_PURGE = 2
# **不要写死 xml:lang**：SAPI 的 SSML 里 <speak xml:lang> 会覆盖 ISpVoice::Voice
# 属性、按语种重新挑音色——写死 zh-CN 的结果就是选了英语的 Zira 也照样是中文女
# 声（实测两个音色出来 md5 完全相同，表现为"离线音色切换无效"）。去掉 xml:lang，
# 音色由 voice.Voice = token 决定；若 speak 仍要求该属性（老系统）再补一次。
SSML_ROOT = ('<speak version="1.0" '
             'xmlns="http://www.w3.org/2001/10/synthesis">{}</speak>')

_voices_cache: Optional[List[dict]] = None


def _com_init() -> None:
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:  # noqa: BLE001
        pass


def list_voices() -> List[dict]:
    """枚举系统本地（离线）音色：[{id, name, lang}]。"""
    global _voices_cache
    if _voices_cache is not None:
        return _voices_cache
    out: List[dict] = []
    try:
        _com_init()
        import comtypes.client as cc
        v = cc.CreateObject("SAPI.SpVoice")
        toks = v.GetVoices()
        for i in range(toks.Count):
            tok = toks.Item(i)
            desc = tok.GetDescription()
            out.append({"id": tok.Id, "name": desc, "lang": _guess_lang(desc)})
    except Exception:  # noqa: BLE001
        out = []
    _voices_cache = out
    return out


def _guess_lang(desc: str) -> str:
    d = (desc or "").lower()
    if "taiwan" in d or "zh-tw" in d or "台灣" in desc or "台湾" in desc:
        return "zh-TW"
    if "chinese" in d or "zh-cn" in d:
        return "zh-CN"
    if "english" in d:
        return "en-US"
    return ""


def _rate_pct(rate: float) -> int:
    """1.0 倍速 → SAPI 的百分比增量（-50%..+80%）。"""
    r = max(0.5, min(1.8, float(rate or 1.0)))
    return int(round((r - 1.0) * 100))


def _pitch_st(pitch: float) -> float:
    """1.0 倍音高 → 半音数（SAPI 的 st）。"""
    import math
    p = max(0.7, min(1.4, float(pitch or 1.0)))
    return round(math.log(p, 2) * 12, 1)


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _jitter_style(style: dict, text: str) -> dict:
    """每句一点点确定性微扰，别让离线音色每句都一个调子（"太生硬"的主因）。

    同一个文本永远得到同一个扰动（crc32），所以重听同一句不会变；
    不同句子之间有 ±3% 语速、±1.5% 音高的差别，听起来像在"说话"而不是复读。
    SAPI 中文音色默认语速偏慢（实测 11 字要 3.3s），整体再提 6%，听着不拖。"""
    import zlib
    h = zlib.crc32((text or "").encode("utf-8")) & 0xFFFF
    s = dict(style or {})
    s["rate"] = float(s.get("rate", 1.0) or 1.0) * 1.06 * (
        1.0 + ((h % 7) - 3) / 100.0)
    s["pitch"] = float(s.get("pitch", 1.0) or 1.0) * (
        1.0 + (((h >> 3) % 7) - 3) / 200.0)
    return s


def _normalize(pcm: bytes, target: float = 2800.0,
               lo: float = 0.5, hi: float = 2.5) -> bytes:
    """把整句音量归一到目标 RMS：不同 SAPI 音色（甚至同一音色的不同句）
    响度差得很多，不归一化就会出现"忽大忽小"。"""
    import numpy as np
    x = np.frombuffer(pcm, dtype=np.int16)
    if x.size == 0:
        return pcm
    xf = x.astype(np.float32)
    rms = float(np.sqrt(np.mean(xf * xf)))
    if rms < 50.0:            # 基本静音：别把噪声放大
        return pcm
    g = max(lo, min(hi, target / rms))
    if abs(g - 1.0) < 0.05:
        return pcm
    y = xf * g
    peak = float(np.abs(y).max()) if y.size else 0.0
    if peak > 32000.0:          # 别把波形顶到削波
        y = y * (32000.0 / peak)
    return np.clip(y, -32768, 32767).astype(np.int16).tobytes()


def _resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """线性插值重采样（语音够用，spec 也不高）。"""
    if src_rate == dst_rate or not pcm:
        return pcm
    import numpy as np
    x = np.frombuffer(pcm, dtype=np.int16)
    if x.size == 0:
        return b""
    n = int(round(x.size * dst_rate / src_rate))
    idx = np.linspace(0.0, x.size - 1.0, n)
    i0 = np.floor(idx).astype(np.int64)
    i1 = np.minimum(i0 + 1, x.size - 1)
    frac = (idx - i0).astype(np.float32)
    y = (x[i0].astype(np.float32) * (1 - frac)
         + x[i1].astype(np.float32) * frac)
    return np.clip(y, -32768, 32767).astype(np.int16).tobytes()


def _synth_wav(voice_id: str, text: str, style: dict) -> tuple[bytes, int]:
    """在工作线程里跑：返回 (PCM 字节, 采样率)。"""
    _com_init()
    import comtypes.client as cc
    voice = cc.CreateObject("SAPI.SpVoice")
    if voice_id:
        try:
            toks = voice.GetVoices()
            for i in range(toks.Count):
                if toks.Item(i).Id == voice_id:
                    voice.Voice = toks.Item(i)
                    break
        except Exception:  # noqa: BLE001
            pass
    try:
        voice.Volume = max(0, min(100, int(round(
            float(style.get("volume", 1.0)) * 100))))
    except Exception:  # noqa: BLE001
        pass

    rate_pct = _rate_pct(style.get("rate", 1.0))
    pitch = _pitch_st(style.get("pitch", 1.0))
    tag = ""
    if rate_pct or pitch:
        attrs = ""
        if rate_pct:
            attrs += f' rate="{rate_pct:+d}%"'
        if pitch:
            attrs += f' pitch="{pitch:+}st"'
        tag = f"<prosody{attrs}>{_xml_escape(text)}</prosody>"
    payload = SSML_ROOT.format(tag or _xml_escape(text))

    fd, path = tempfile.mkstemp(suffix=".wav", prefix="xiaod_tts_")
    os.close(fd)
    try:
        fs = cc.CreateObject("SAPI.SpFileStream")
        fs.Open(path, 3)          # SSFMCreateForWrite
        voice.AudioOutputStream = fs
        used_ssml = False
        try:
            voice.Speak(payload, _SVSF_IS_XML | _SVSF_PURGE)
            used_ssml = True
        except Exception:  # noqa: BLE001
            pass
        if not used_ssml:
            try:
                voice.Rate = max(-10, min(10, int(round(rate_pct / 10))))
            except Exception:  # noqa: BLE001
                pass
            voice.Speak(text, _SVSF_PURGE)
        fs.Close()
        with wave.open(path, "rb") as w:
            rate = w.getframerate()
            pcm = w.readframes(w.getnframes())
        return pcm, rate
    finally:
        try:
            os.remove(path)
        except Exception:  # noqa: BLE001
            pass


class SapiTTS(BaseTTS):
    """离线本地合成（Windows SAPI）。"""

    engine = "local"

    def __init__(self, voice_id: str = "", sample_rate: int = 24000,
                 frame_size: int = 1440):
        self.voice_id = voice_id or ""
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self._frame_bytes = frame_size * 2

    async def _pcm(self, text: str, style: Optional[dict]) -> bytes:
        style = _jitter_style(style or {}, text)
        pcm, src_rate = await asyncio.to_thread(
            _synth_wav, self.voice_id, text, style)
        pcm = _resample(pcm, src_rate, self.sample_rate)
        return _normalize(pcm)

    async def synthesize(self, text: str, style: Optional[dict] = None
                         ) -> List[bytes]:
        text = (text or "").strip()
        if not text:
            return []
        pcm = await self._pcm(text, style)
        if not pcm:
            return []
        return DownlinkEncoder(self.sample_rate,
                               self.frame_size).encode_pcm(pcm)

    async def stream(self, text: str, style: Optional[dict] = None):
        for p in await self.synthesize(text, style):
            yield p
