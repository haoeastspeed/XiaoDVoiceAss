# -*- coding: utf-8 -*-
"""小D网关配置：默认值 + 可选覆盖文件。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict

# 项目根目录（gateway 的上一级）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 自包含运行库（opus.dll / ffmpeg / ffprobe）
BIN_DIR = os.path.join(ROOT, "reference", "bin")

# 本地模型目录（本机已就位）
SENSEVOICE_DIR = (
    "D:/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
)
SILERO_DIR = "D:/models/snakers4_silero-vad"


@dataclass
class GatewayConfig:
    # 服务器
    host: str = "0.0.0.0"
    port: int = 8000
    ws_path: str = "/xiaozhi/v1/"

    # 上行 / 下行音频参数
    uplink_sample_rate: int = 16000
    downlink_sample_rate: int = 24000
    channels: int = 1
    frame_duration_ms: int = 60

    # 模型
    ollama_base_url: str = "http://localhost:11434"
    llm_model: str = "qwen3.5:9b-q4km"
    tts_voice: str = "zh-CN-XiaoxiaoNeural"
    sensevoice_dir: str = SENSEVOICE_DIR
    silero_dir: str = SILERO_DIR

    # VAD 断句
    vad_threshold: float = 0.5
    vad_threshold_low: float = 0.3
    min_silence_ms: int = 400

    # 人设（小D）
    system_prompt: str = (
        "你是小D，一个运行在用户本地电脑上的AI语音助手，和桌面办公助手"
        "是同一个智能体。你用自然、亲切、口语化的中文对话，回答简洁，"
        "不啰嗦，不使用书面语和markdown。可以陪用户聊天、答疑、查信息、"
        "处理简单任务。听不清就请用户再说一遍。"
    )

    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def uplink_frame_size(self) -> int:
        return int(self.uplink_sample_rate * self.frame_duration_ms / 1000)

    @property
    def downlink_frame_size(self) -> int:
        return int(self.downlink_sample_rate * self.frame_duration_ms / 1000)


def load_config(path: str | None = None) -> GatewayConfig:
    """加载配置；可选用 JSON 覆盖默认值。"""
    import json

    cfg = GatewayConfig()
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
            else:
                cfg.extra[k] = v
    return cfg
