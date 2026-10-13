# -*- coding: utf-8 -*-
"""SenseVoice（sherpa-onnx）离线 ASR。"""
from __future__ import annotations

import os
import threading

import numpy as np
import sherpa_onnx

from gateway.providers.base import BaseASR


class SenseVoiceASR(BaseASR):
    def __init__(self, model_dir: str, sample_rate: int = 16000):
        model = os.path.join(model_dir, "model.int8.onnx")
        tokens = os.path.join(model_dir, "tokens.txt")
        if not os.path.isfile(model) or not os.path.isfile(tokens):
            raise FileNotFoundError(f"SenseVoice 模型缺失: {model_dir}")
        self.sample_rate = sample_rate
        self._lock = threading.Lock()
        self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=model,
            tokens=tokens,
            num_threads=2,
            sample_rate=sample_rate,
            feature_dim=80,
            decoding_method="greedy_search",
            debug=False,
            use_itn=True,
        )

    def transcribe(self, pcm_s16le: bytes) -> str:
        audio = np.frombuffer(pcm_s16le, dtype=np.int16).astype(np.float32) / 32768.0
        if audio.size < int(self.sample_rate * 0.1):
            return ""
        if audio.size < int(self.sample_rate * 0.1):
            return ""
        with self._lock:
            stream = self._rec.create_stream()
            stream.accept_waveform(self.sample_rate, audio)
            self._rec.decode_stream(stream)
            return (stream.result.text or "").strip()
