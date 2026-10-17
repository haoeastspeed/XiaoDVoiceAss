# -*- coding: utf-8 -*-
"""PC 端本地唤醒词（openWakeWord）。

ESP32 有 ESP-SR 离线唤醒；PC 端此前只有"打开即听"。这里补上本地唤醒词：
WS 长连保持，只有听到唤醒词才把音频上行 —— 既省算力又避免误识别。

选型说明（写在这里供后来人参考）：
  * openWakeWord —— 纯 onnxruntime（项目已有该依赖），模型 ~10MB，社区活跃，
    预置 "hey jarvis" / "alexa" 等，也可自训。**选它。**
  * snowman / porcupine —— 需要额外原生库或商业授权，放弃。
  * sherpa-onnx KWS —— 可用但需另下 KWS 模型，且我们已用 sherpa 做 ASR/TTS，
    openWakeWord 更轻。作为备选（见 KwsBackend 注释）。

模型缺失或依赖不可用时 `available=False`，调用方应降级回"打开即听"。
"""
from __future__ import annotations

import os
from typing import List, Optional

# openWakeWord 推荐 80ms/16k（=1280 样本）
CHUNK = 1280


class WakeWord:
    """流式唤醒词检测：喂 16k s16le PCM，命中返回 True。"""

    def __init__(self, model_paths: Optional[List[str]] = None,
                 threshold: float = 0.5, sample_rate: int = 16000,
                 logger=None):
        self.sample_rate = int(sample_rate)
        self.threshold = float(threshold)
        self._log = logger or (lambda m: None)
        self.available = False
        self._model = None
        self._buf = bytearray()
        self._names: List[str] = []
        self.last_score = 0.0
        self.last_name = ""
        self._model_paths = list(model_paths or [])
        self._load()

    # ---------- 加载 ----------

    def _load(self):
        try:
            from openwakeword.model import Model  # noqa: PLC0415
        except Exception as e:  # noqa: BLE001
            self._log(f"openWakeWord 不可用：{type(e).__name__}: {e}")
            return
        try:
            paths = [p for p in self._model_paths if p and os.path.isfile(p)]
            if paths:
                self._model = Model(wakeword_models=paths,
                                    inference_framework="onnx")
            else:
                # 用内置预置模型（首次会自动下载到 openwakeword 的模型目录）
                self._model = Model(inference_framework="onnx")
            self._names = list(getattr(self._model, "models", {}).keys())
            self.available = True
            self._log(f"唤醒词引擎就绪：{self._names or '(预置)'}")
        except Exception as e:  # noqa: BLE001
            self._log(f"唤醒词模型加载失败：{type(e).__name__}: {e}")

    # ---------- 检测 ----------

    def feed(self, pcm_s16le: bytes) -> bool:
        """喂一帧音频；命中唤醒词返回 True（内部缓冲对齐 80ms）。"""
        if not self.available or not pcm_s16le:
            return False
        self._buf.extend(pcm_s16le)
        hit = False
        need = CHUNK * 2
        while len(self._buf) >= need:
            chunk = bytes(self._buf[:need])
            del self._buf[:need]
            if self._predict(chunk):
                hit = True
                self._buf.clear()      # 命中后丢弃残余，避免连续重复触发
                break
        return hit

    def _predict(self, chunk: bytes) -> bool:
        try:
            import numpy as np
            x = np.frombuffer(chunk, dtype=np.int16)
            pred = self._model.predict(x)
        except Exception as e:  # noqa: BLE001
            self._log(f"唤醒词推理失败：{type(e).__name__}: {e}")
            return False
        best_name, best_score = "", 0.0
        for name, score in (pred or {}).items():
            try:
                s = float(score)
            except (TypeError, ValueError):
                continue
            if s > best_score:
                best_name, best_score = name, s
        self.last_name, self.last_score = best_name, best_score
        return best_score >= self.threshold

    def reset(self):
        """清空缓冲与模型内部状态（回到监听前）。"""
        self._buf.clear()
        self.last_score = 0.0
        if self._model is not None:
            try:
                self._model.reset()
            except Exception:  # noqa: BLE001
                pass


# 备选后端（如需切换，把 KwsBackend 换成实现同名接口的类即可）
class KwsBackend:
    """sherpa-onnx KWS 的占位接口（未启用）。"""
    available = False
