# -*- coding: utf-8 -*-
"""Silero VAD（ONNX，每会话独立状态）。"""
from __future__ import annotations

import os
import time
from collections import deque
from typing import Tuple

import numpy as np
import onnxruntime

from gateway.providers.base import BaseVAD


class SileroVAD(BaseVAD):
    def __init__(self, model_dir: str, threshold: float = 0.5,
                 threshold_low: float = 0.3, min_silence_ms: int = 400,
                 sample_rate: int = 16000):
        path = os.path.join(
            model_dir, "src", "silero_vad", "data", "silero_vad.onnx"
        )
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Silero VAD 缺失: {path}")
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._sess = onnxruntime.InferenceSession(
            path, providers=["CPUExecutionProvider"], sess_options=opts
        )
        self.threshold = threshold
        self.threshold_low = threshold_low
        self.min_silence_ms = min_silence_ms
        self.sample_rate = sample_rate
        self._window_need = 3

    def new_state(self) -> dict:
        return {
            "vad_state": np.zeros((2, 1, 128), dtype=np.float32),
            "context": np.zeros((1, 64), dtype=np.float32),
            "buffer": bytearray(),
            "last_is_voice": False,
            "window": deque(maxlen=5),
            "have_voice": False,
            "last_voice_time": 0.0,
            "ended": False,
        }

    def reset_sentence(self, state: dict) -> None:
        state["have_voice"] = False
        state["ended"] = False
        state["window"].clear()
        state["buffer"] = bytearray()
        # 保留 vad_state/context 以维持连续性

    def process(self, state: dict, pcm_s16le: bytes) -> Tuple[bool, bool]:
        state["buffer"].extend(pcm_s16le)
        speaking = False
        buf = state["buffer"]
        while len(buf) >= 512 * 2:
            chunk = bytes(buf[:512 * 2])
            del buf[:512 * 2]
            x = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
            inp = np.concatenate(
                [state["context"], x.reshape(1, -1)], axis=1
            ).astype(np.float32)
            out, new_vad_state = self._sess.run(
                None,
                {
                    "input": inp,
                    "state": state["vad_state"],
                    "sr": np.array(16000, dtype=np.int64),
                },
            )
            state["vad_state"] = new_vad_state
            state["context"] = inp[:, -64:]
            prob = out.item()
            if prob >= self.threshold:
                is_voice = True
            elif prob <= self.threshold_low:
                is_voice = False
            else:
                is_voice = state["last_is_voice"]
            state["last_is_voice"] = is_voice
            state["window"].append(is_voice)
            speaking = state["window"].count(True) >= self._window_need

            if state["have_voice"] and not speaking:
                gap = time.time() * 1000 - state["last_voice_time"]
                if gap >= self.min_silence_ms:
                    state["ended"] = True
            if speaking:
                state["have_voice"] = True
                state["last_voice_time"] = time.time() * 1000
        return speaking, state["ended"]
