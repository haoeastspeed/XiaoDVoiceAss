# -*- coding: utf-8 -*-
"""说话人识别（声纹）——对齐官方 core/utils/voiceprint_provider.py。

两种 provider：
  * RemoteVoiceprint —— 走官方同款 HTTP API（POST {base}/voiceprint/identify，
    multipart: speaker_ids + file=audio.wav，Bearer key），零新依赖，配好即用。
  * LocalVoiceprint  —— 本地 sherpa-onnx 说话人嵌入模型（CAM++ / ERes2Net），
    cosine 相似度 + 阈值判定（默认 0.4，同官方）。

用途：认出"这句话是谁说的"，把结果作为背景信息注入（同一位说话人**只注入一次**，
对齐官方 system_introduced_speakers 的去重策略），让助手能对不同人称呼不同。
"""
from __future__ import annotations

import io
import struct
import wave
from typing import List, Optional, Tuple

import httpx


def pcm_to_wav(pcm_s16le: bytes, sample_rate: int = 16000) -> bytes:
    """s16le 单声道 PCM → 内存 WAV。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(pcm_s16le)
    return buf.getvalue()


def _cosine(a, b) -> float:
    import numpy as np
    va = np.asarray(a, dtype="float32").ravel()
    vb = np.asarray(b, dtype="float32").ravel()
    na = float(np.linalg.norm(va)) or 1.0
    nb = float(np.linalg.norm(vb)) or 1.0
    return float(np.dot(va, vb) / (na * nb))


class BaseVoiceprint:
    available = False

    def identify(self, pcm_s16le: bytes, sample_rate: int = 16000) -> str:
        """返回识别到的说话人名（认不出/不可用返回空串）。"""
        return ""


class RemoteVoiceprint(BaseVoiceprint):
    def __init__(self, base_url: str, key: str = "",
                 speaker_ids: Optional[List[str]] = None,
                 timeout: float = 10.0):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.key = (key or "").strip()
        self.speaker_ids = list(speaker_ids or [])
        self.timeout = timeout
        self.available = bool(self.base_url)

    def identify(self, pcm_s16le: bytes, sample_rate: int = 16000) -> str:
        if not self.available or not pcm_s16le:
            return ""
        try:
            wav = pcm_to_wav(pcm_s16le, sample_rate)
            headers = {}
            if self.key:
                headers["Authorization"] = f"Bearer {self.key}"
            data = {}
            if self.speaker_ids:
                data["speaker_ids"] = ",".join(self.speaker_ids)
            files = {"file": ("audio.wav", wav, "audio/wav")}
            with httpx.Client(timeout=self.timeout, trust_env=False) as c:
                r = c.post(f"{self.base_url}/voiceprint/identify",
                           headers=headers, data=data, files=files)
            if r.status_code != 200:
                return ""
            js = r.json()
            if isinstance(js, dict):
                if js.get("success") is False:
                    return ""
                name = js.get("speaker") or js.get("name") or ""
                return str(name).strip() if name else ""
        except Exception:  # noqa: BLE001
            return ""
        return ""


class LocalVoiceprint(BaseVoiceprint):
    """本地 sherpa-onnx 说话人嵌入 + 已注册声纹比对。"""

    def __init__(self, model_dir: str, threshold: float = 0.4,
                 logger=None):
        self.model_dir = str(model_dir or "")
        self.threshold = float(threshold)
        self._log = logger or (lambda m: None)
        self._extractor = None
        self._enrolled = {}      # name -> embedding
        self.available = False
        if not self.model_dir:
            return
        try:
            import os
            onnx = None
            for fn in os.listdir(self.model_dir):
                if fn.endswith(".onnx"):
                    onnx = os.path.join(self.model_dir, fn)
                    break
            if not onnx:
                return
            import sherpa_onnx  # noqa: PLC0415
            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=onnx)
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
            self.available = True
        except Exception as e:  # noqa: BLE001
            self._log(f"本地声纹不可用：{type(e).__name__}: {e}")

    def _embed(self, pcm_s16le: bytes, sample_rate: int):
        import numpy as np
        samples = np.frombuffer(pcm_s16le, dtype=np.int16).astype("float32")
        samples /= 32768.0
        if sample_rate != 16000 and samples.size:
            n = int(samples.size * 16000 / sample_rate)
            idx = np.linspace(0, samples.size - 1, n)
            samples = np.interp(idx, np.arange(samples.size), samples) \
                .astype("float32")
        stream = self._extractor.create_stream()
        stream.accept_waveform(16000, samples)
        stream.input_finished()
        return self._extractor.compute(stream)

    def enroll(self, name: str, pcm_s16le: bytes,
               sample_rate: int = 16000) -> bool:
        if not self.available or not name or not pcm_s16le:
            return False
        try:
            self._enrolled[name] = self._embed(pcm_s16le, sample_rate)
            return True
        except Exception:  # noqa: BLE001
            return False

    def identify(self, pcm_s16le: bytes, sample_rate: int = 16000) -> str:
        if not self.available or not self._enrolled or not pcm_s16le:
            return ""
        try:
            emb = self._embed(pcm_s16le, sample_rate)
        except Exception:  # noqa: BLE001
            return ""
        best, best_score = "", 0.0
        for name, ref in self._enrolled.items():
            s = _cosine(emb, ref)
            if s > best_score:
                best, best_score = name, s
        return best if best_score >= self.threshold else ""


def from_config(config, logger=None) -> Optional[BaseVoiceprint]:
    """按配置构造声纹 provider；未启用/不可用返回 None。"""
    if not getattr(config, "voiceprint_enabled", False):
        return None
    mode = str(getattr(config, "voiceprint_mode", "remote") or "remote").lower()
    threshold = float(getattr(config, "voiceprint_similarity", 0.4) or 0.4)
    if mode == "local":
        vp = LocalVoiceprint(
            str(getattr(config, "voiceprint_model_dir", "") or ""),
            threshold, logger)
        return vp if vp.available else None
    base = str(getattr(config, "voiceprint_base_url", "") or "")
    if not base:
        return None
    ids = []
    for item in (getattr(config, "voiceprint_speakers", None) or []):
        if isinstance(item, str) and item:
            ids.append(item.split(",")[0].strip())
    vp = RemoteVoiceprint(base, str(getattr(config, "voiceprint_key", "") or ""),
                          ids)
    return vp if vp.available else None


def speaker_label(name: str) -> str:
    """把说话人名转成可注入的一句话（第一次出现时用）。"""
    n = (name or "").strip()
    return f"当前和你说话的人叫「{n}」。" if n else ""
