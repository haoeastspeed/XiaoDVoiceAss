# -*- coding: utf-8 -*-
"""本地神经语音合成（sherpa-onnx Kokoro）。

Windows SAPI 的本地音色是拼接/共振峰合成，听感机械；本机通常也只装了一两个
音色（实测只有简体 Huihui + 英语 Zira），降级过去就是用户抱怨的"台湾女声太生硬"。
Kokoro 是 82M 参数的小型神经网络 TTS，纯 ONNX 推理，完全离线：

  * 中文 8 个音色（4 女 4 男）+ 英文数十个，中英混读不用切引擎；
  * 原生输出 24kHz —— 正好等于网关下行采样率，不用重采样；
  * 实测 RTF ≈ 0.26（4 线程），一句 5 秒的话约 1.3 秒合成完。

模型不随包分发，按 SenseVoice 的既有约定放在外部模型目录，缺失时自动回退 SAPI。
"""
from __future__ import annotations

import asyncio
import os
import threading
from typing import List, Optional

from gateway.providers.base import BaseTTS

try:  # 下行 opus 编码
    from gateway.audio.opus import DownlinkEncoder
except Exception:  # noqa: BLE001
    DownlinkEncoder = None  # type: ignore[assignment]

SAMPLE_RATE = 24000  # Kokoro 固定输出 24k，与网关下行一致

# 中文音色（Kokoro 的 speaker id）。名字沿用 Edge 的叫法便于对齐理解，
# 但发音人是完全不同的另一套。
ZH_VOICES: dict = {
    "zf_xiaoxiao": (47, "晓晓 · 温柔女声（本地）", "f"),
    "zf_xiaoyi": (48, "晓伊 · 少女音（本地）", "f"),
    "zf_xiaobei": (45, "晓北 · 清爽女声（本地）", "f"),
    "zf_xiaoni": (46, "晓妮 · 知性女声（本地）", "f"),
    "zm_yunxi": (50, "云希 · 阳光男声（本地）", "m"),
    "zm_yunjian": (49, "云健 · 沉稳男声（本地）", "m"),
    "zm_yunxia": (51, "云夏 · 少年音（本地）", "m"),
    "zm_yunyang": (52, "云扬 · 播报男声（本地）", "m"),
}

# 英文音色挑常用的几个，给"英语外教"这类场景用
EN_VOICES: dict = {
    "af_heart": (3, "Heart · 美式女声（本地）", "f"),
    "af_bella": (2, "Bella · 动感女声（本地）", "f"),
    "af_sky": (10, "Sky · 轻柔女声（本地）", "f"),
    "am_adam": (11, "Adam · 美式男声（本地）", "m"),
    "am_liam": (15, "Liam · 磁性男声（本地）", "m"),
    "am_michael": (16, "Michael · 沉稳男声（本地）", "m"),
    "bf_emma": (21, "Emma · 英式女声（本地）", "f"),
    "bm_george": (26, "George · 英式男声（本地）", "m"),
}

ALL_VOICES: dict = {**ZH_VOICES, **EN_VOICES}

def _dir_candidates():
    """模型目录候选：运行目录优先（打包版自带/用户自己拷），再开发机路径。

    放在运行目录下，离线降级才会用神经音色而不是机械的 SAPI 音色——
    两者听感差别很大，用户听到"音色变了"多半就是这个落差。"""
    out = []
    try:
        from gateway.config import ROOT
        out.append(os.path.join(ROOT, "models", "kokoro"))
        out.append(os.path.join(os.path.dirname(ROOT.rstrip("\\/")),
                                "models", "kokoro"))
    except Exception:  # noqa: BLE001
        pass
    try:                       # 冻结版：exe 所在目录
        import sys
        if getattr(sys, "frozen", False):
            out.append(os.path.join(
                os.path.dirname(sys.executable), "models", "kokoro"))
    except Exception:  # noqa: BLE001
        pass
    out.append(os.path.join(os.path.expanduser("~"),
                            ".xiaod", "models", "kokoro"))
    out.extend((
        "D:/models/kokoro/kokoro-int8-multi-lang-v1_1",
        "D:/models/kokoro/kokoro-multi-lang-v1_1",
        "D:/models/kokoro/kokoro-multi-lang-v1_0",
    ))
    return out


MODEL_DEFAULT_DIRS = tuple(_dir_candidates())

_neuro: Optional[object] = None      # 全局单例（加载 1.2s，别每句重载）
_neuro_lock = threading.Lock()
_neuro_failed = False                # 加载失败就不再反复重试，直接走 SAPI


def _has_model(d: str) -> bool:
    d = d.replace("\\", "/").rstrip("/")
    return (os.path.isfile(d + "/model.onnx")
            and os.path.isfile(d + "/voices.bin")
            and os.path.isfile(d + "/tokens.txt"))


def _config_kokoro_dir() -> str:
    """gateway.json 里显式指定的模型目录（kokoro_dir）。

    为什么需要它：espeak-ng 的 C 层用 ANSI 打开文件，**路径里有中文就打不开**
    （报 `/usr/share/espeak-ng-data\\phontab` 不存在，实际是退回了默认路径）。
    运行目录叫"小D智能助手-极速版"，模型塞在里面必然失败，所以要把模型放
    在纯 ASCII 路径上（用户目录），再用这一项指过去。"""
    try:
        import json
        from gateway.config import ROOT
        p = os.path.join(ROOT, "gateway.json")
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                return str((json.load(f) or {}).get("kokoro_dir") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _find_model(dir_hint: str = "") -> str:
    """定位含 model.onnx / voices.bin / tokens.txt 的模型目录。

    既支持"目录里直接放文件"，也支持 `models/kokoro/<变体名>/` 这种一层
    子目录的摆法（拷模型时保留变体目录名更清楚，不必再展平一次）。"""
    cands = [dir_hint] if dir_hint else []
    env = os.environ.get("XIAOD_KOKORO_DIR", "")
    if env:
        cands.append(env)
    cfg_dir = _config_kokoro_dir()
    if cfg_dir:
        cands.append(cfg_dir)
    cands.extend(MODEL_DEFAULT_DIRS)
    for d in cands:
        if not d:
            continue
        d = d.replace("\\", "/").rstrip("/")
        if _has_model(d):
            return d
        try:
            subs = sorted(os.listdir(d))
        except Exception:  # noqa: BLE001
            continue
        for s in subs:
            p = d + "/" + s
            if os.path.isdir(p) and _has_model(p):
                return p
    return ""


def _load(model_dir: str = ""):
    """加载 Kokoro 模型，返回 OfflineTts；不可用返回 None。"""
    d = _find_model(model_dir)
    if not d:
        return None
    # 路径必须**统一成正斜杠**：sherpa 把 data_dir 交给 espeak-ng 的 C 代码
    # 打开，反斜杠会被当成转义（中文目录尤其明显，"D:\…\小D…\espeak-ng-data"
    # 直接打不开）→ 退回默认的 /usr/share/espeak-ng-data，报 "phontab 不存在"，
    # 表现为"本地神经音色突然不可用，降级成机械音"。
    d = os.path.abspath(d).replace("\\", "/")
    try:
        import sherpa_onnx
        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=d + "/model.onnx",
                    voices=d + "/voices.bin",
                    tokens=d + "/tokens.txt",
                    data_dir=d + "/espeak-ng-data",
                    lexicon=",".join(
                        p for p in (d + "/lexicon-us-en.txt",
                                    d + "/lexicon-zh.txt")
                        if os.path.isfile(p)),
                ),
                num_threads=4,
            ),
            max_num_sentences=1,
        )
        return sherpa_onnx.OfflineTts(cfg)
    except Exception:  # noqa: BLE001
        return None


def find(model_dir: str = ""):
    """取全局唯一的 OfflineTts 实例；不可用返回 None。"""
    global _neuro, _neuro_failed
    if _neuro_failed:
        return None
    if _neuro is not None:
        return _neuro
    with _neuro_lock:
        if _neuro is None and not _neuro_failed:
            obj = _load(model_dir)
            if obj is None:
                _neuro_failed = True
            else:
                _neuro = obj
    return _neuro


def available(model_dir: str = "") -> bool:
    """本机是否能用本地神经音色（供界面决定是否列出这一组）。"""
    return find(model_dir) is not None


def sid_of(voice_id: str) -> int:
    item = ALL_VOICES.get(voice_id or "")
    return item[0] if item else 47   # 默认 zf_xiaoxiao


def is_neuro_id(voice_id: str) -> bool:
    """音色 id 是不是本地神经音色（而非 SAPI 的注册表 token）。"""
    v = voice_id or ""
    if not v or "\\" in v or v.startswith("HKEY"):
        return False
    return v in ALL_VOICES


class KokoroTTS(BaseTTS):
    """本地神经合成（Kokoro），不可用时由上层退回 SAPI。"""

    engine = "local"

    def __init__(self, voice_id: str = "", sample_rate: int = SAMPLE_RATE,
                 frame_size: int = 1440, model_dir: str = ""):
        self.voice_id = voice_id or "zf_xiaoxiao"
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self.model_dir = model_dir

    async def _pcm(self, text: str, style: Optional[dict]) -> bytes:
        engine = find(self.model_dir)
        if engine is None:
            return b""
        style = style or {}
        # Kokoro 的 speed 直接控制语速；上层给的 rate 是倍率。
        # 沿用 SAPI 侧同样的 ×1.06 基础提速，让对话节奏不那么拖。
        try:
            speed = float(style.get("rate") or 1.0)
        except (TypeError, ValueError):
            speed = 1.0
        speed = max(0.6, min(2.0, speed * 1.06))

        def _run():
            audio = engine.generate(text, sid=sid_of(self.voice_id),
                                    speed=speed)
            return audio.samples, audio.sample_rate

        try:
            import numpy as np
            samples, src = await asyncio.to_thread(_run)
            x = np.asarray(samples, dtype=np.float32)
            if src != self.sample_rate:
                n = int(len(x) * self.sample_rate / src)
                x = np.interp(np.arange(n) * src / self.sample_rate,
                              np.arange(len(x)), x)
            return _level(x)
        except Exception:  # noqa: BLE001
            return b""

    async def synthesize(self, text: str, style: Optional[dict] = None
                         ) -> List[bytes]:
        text = (text or "").strip()
        if not text:
            return []
        pcm = await self._pcm(text, style)
        if not pcm or DownlinkEncoder is None:
            return []
        return DownlinkEncoder(self.sample_rate,
                               self.frame_size).encode_pcm(pcm)

    async def stream(self, text: str, style: Optional[dict] = None):
        for p in await self.synthesize(text, style):
            yield p


def _level(x) -> bytes:
    """把响度归一到与在线音色相当的水平，避免切换引擎时音量忽大忽小。"""
    try:
        import numpy as np
        if x.size == 0:
            return b""
        cur = float(np.sqrt(np.mean(np.square(x))))
        if cur > 1.0:
            g = min(3.0, 2800.0 / cur)
        else:
            g = 1.0
        if g != 1.0:
            x = x * g
        peak = float(np.abs(x).max()) if x.size else 0.0
        if peak > 32000:      # 防削波：顶到上限就整体回缩
            x = x * (32000 / peak)
        return np.clip(x, -32768, 32767).astype("<i2").tobytes()
    except Exception:  # noqa: BLE001
        return b""


def list_voices() -> List[dict]:
    """本地神经音色清单（界面下拉用）。模型不可用返回空，上层只显示 SAPI。"""
    if not available():
        return []
    out = []
    for vid, (_sid, name, gender) in ZH_VOICES.items():
        out.append({"id": vid, "name": name, "gender": gender,
                    "group": "离线 · 本地神经音色（中文）"})
    for vid, (_sid, name, gender) in EN_VOICES.items():
        out.append({"id": vid, "name": name, "gender": gender,
                    "group": "离线 · 本地神经音色（英语）"})
    return out
