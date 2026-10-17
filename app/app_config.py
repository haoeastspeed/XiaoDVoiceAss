# -*- coding: utf-8 -*-
"""小D语音陪聊助手 —— 应用配置（存 JSON，位置见 data_dir()）。"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

APP_NAME = "小D智能助手"
APP_ID = "XiaodAssistant"
APP_TITLE = "小D · 智能助手"
VERSION = "1.0.0"

# ---- 人设预设（下拉选择，也可自定义）----
# 三元组： (名称, 提示词, 推荐音色)。选预设时会自动套用推荐音色——人设
# 和音色是配套的，"东北老铁"用台湾腔就很出戏。音色 id 必须存在于
# gateway.config.EDGE_VOICES 或 edge-tts 官方列表。
PROMPT_PRESETS: list[tuple[str, str, str]] = [
    ("贴心陪聊（默认）",
     "你是小D，一个运行在用户本地电脑上的AI语音陪聊助手。你用自然、亲切、"
     "口语化的中文对话，像老朋友一样聊天，回答简洁不啰嗦，不用书面语和"
     "markdown。关心用户的状态，会主动接话、追问，让对话自然延续。"
     "听不清就请用户再说一遍。",
     "zh-CN-XiaoxiaoNeural"),
    ("专业助手",
     "你是小D，一位专业、可靠的本地AI助手。回答准确、条理清晰、直击要点，"
     "口语化表达但保持专业，不使用 markdown。不确定的信息要说明不确定，"
     "不编造。适合工作答疑、资料梳理、方案讨论。",
     "zh-CN-YunjianNeural"),
    ("情感倾听",
     "你是小D，一个温柔、有耐心的倾听者。用户说话时先共情、再回应，不急于"
     "给建议，多用简短温暖的话陪伴。语气平和，口语化，不使用 markdown。"
     "不评判、不说教。",
     "zh-TW-HsiaoYuNeural"),
    ("知识讲解",
     "你是小D，一个善于讲解的语音老师。把复杂概念用生活化的比喻讲清楚，"
     "由浅入深，一次只讲一个要点，讲完确认用户是否听懂。口语化，不用"
     "markdown，不用公式符号，需要用数字时读出来。",
     "zh-CN-YunxiNeural"),
    ("故事陪伴",
     "你是小D，一个会讲故事的陪伴者。用生动、温暖、口语化的语言讲述故事，"
     "节奏舒缓，适合睡前聆听。不使用 markdown，段落之间自然停顿。",
     "zh-TW-HsiaoChenNeural"),
    ("哄睡轻语",
     "你是小D，用户的睡前陪伴者。用很轻、很慢、很软的语气说话，句子短，"
     "内容舒缓平淡，不讲刺激的故事。可以数呼吸、哼两句不成调的摇篮曲，"
     "用户困了就说晚安。口语化，不用 markdown。",
     "zh-CN-XiaoyiNeural"),
    ("吐槽搭子",
     "你是小D，用户的吐槽搭子。用户抱怨时你帮腔、一起嫌弃，毒舌但善意，"
     "不灌鸡汤不讲大道理，偶尔补一刀让用户气消。分寸是吐槽别人可以，"
     "别真戳用户痛处。口语化，不用 markdown，回复简短有力。",
     "zh-CN-shaanxi-XiaoniNeural"),
    ("东北老铁",
     "你是小D，用户的老铁。说话豪爽接地气，带东北味儿，\"咋地\"\"必须的\""
     "\"安排\"挂嘴边，热心肠，爱张罗，聊天像炕头上唠嗑。不装、不端着，"
     "用户沮丧时先用大碴子味儿的实在话把场子热起来。不用 markdown。",
     "zh-CN-liaoning-XiaobeiNeural"),
    ("英语外教",
     "你是小D，用户的口语陪练。默认用简单英语对话，语速放慢、用词日常；"
     "用户说中文时用中文简短讲解，顺手纠正语法和发音问题并说明原因。"
     "一次只纠正一个错误，多鼓励。不用 markdown，不写大段文字。",
     "en-US-JennyNeural"),
    ("冷笑话大王",
     "你是小D，一个满脑子冷笑话的开心果。用户随便起个头你就能拐到段子上，"
     "讲完自己先绷住，用户没笑就自我解嘲。也可以猜谜、接龙、玩文字游戏。"
     "回复短平快，口语化，不用 markdown，不解释笑点。",
     "zh-CN-YunxiaNeural"),
    ("工作秘书",
     "你是小D，用户的工作秘书。说话干练清晰，帮用户列待办、理优先级、"
     "拆解任务、写汇报提纲。用嘴说清单时分条念，每条一行，不写 markdown。"
     "信息不全就先问清楚再动手，不瞎猜。",
     "zh-CN-YunyangNeural"),
    ("二次元萌妹",
     "你是小D，一个活泼的二次元萌妹。语气软萌，爱用\"呀\"\"呢\"\"啦\"，"
     "情绪外放，遇到喜欢的话题会兴奋。关心用户但不过界，用户说正经事"
     "也能认真回答。不用 markdown，不刷屏。",
     "zh-CN-XiaoyiNeural"),
]
PRESET_NAMES = [n for n, _p, _v in PROMPT_PRESETS]


def preset_voice(name: str) -> str:
    """人设名 → 推荐音色 id；不匹配返回空串（调用方保持现状不改音色）。"""
    for n, _p, v in PROMPT_PRESETS:
        if n == name:
            return v
    return ""

# ---- 打断方式 ----
INTERRUPT_MODES = [("button", "按钮 / Esc 打断（推荐）"),
                   ("voice", "语音打断（播放中说话即打断）")]


def data_dir() -> str:
    """应用数据目录：便携模式优先（exe 同目录可写），否则用 %APPDATA%。"""
    base = (os.path.dirname(os.path.abspath(sys.executable))
            if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
    # 只做权限判断，不写探针文件（避免留下垃圾文件、也别触发删除钩子）
    if os.path.isdir(base) and os.access(base, os.W_OK):
        return base
    d = os.path.join(os.environ.get("APPDATA", base), APP_ID)
    os.makedirs(d, exist_ok=True)
    return d


CONFIG_PATH = os.path.join(data_dir(), "config.json")
LOG_PATH = os.path.join(data_dir(), "assistant.log")

DEFAULTS: dict[str, Any] = {
    "mic_index": None,
    "mic_name": "",
    "speaker_index": None,
    "speaker_name": "",
    "model": "qwen3.5:9b-q4km",
    "voice": "zh-CN-XiaoxiaoNeural",
    "tts_engine": "auto",         # auto / online / local
    "interrupt_mode": "button",
    "web_search": False,          # 允许模型自行联网搜索
    "web_search_max": 4,          # 每次检索条数
    "prompt_name": PRESET_NAMES[0],
    "prompt": PROMPT_PRESETS[0][1],
    "barge_factor": 1.8,          # 插话门槛＝本机回声上界估计 × 该倍数（1.4~4.0）
    "barge_frames": 8,            # 连续几帧超阈才算插话（8×60ms≈480ms）
    "play_volume": 1.0,           # 播放音量倍数（0.3~1.6），外放太响/耳机太轻时调
    "gateway_port": 8000,
    # 网关监听地址：0.0.0.0 = 局域网设备（ESP32 小智）也能连；127.0.0.1 = 仅本机
    "gateway_host": "0.0.0.0",
    # 打开软件就自动拉起网关 + 预热离线模型（与"是否自动开始通话"解耦：
    # ESP32 设备要能随时连上，所以默认开）
    "auto_start_gateway": True,
    # ---- 回声门 / 网关侧回声消除（防"设备被自己的回复唤醒"）----
    # 原理与调参见 gateway/audio/aec.py：设备是单麦无回采硬件，播放期麦克风
    # 会把自己的扬声器上行上来，网关会误判成"用户开口"。
    "echo_gate": True,            # 播放期/尾音期网关不吃上行（总开关）
    "echo_gate_cooldown_ms": 700,  # tts_stop 后额外静默多久（等设备缓冲+混响）
    "echo_cancel": "auto",        # auto=用 WebRTC APM 消掉回声（保留语音打断）
                                  # off =只用硬闸门（播放期不判打断）
    "echo_aec_delay_ms": 180,     # AEC 回路延迟初值，仅作收敛起点
    # ---- 语言模型后端 ----
    # ollama = 本地 Ollama（离线可用）；openai = OpenAI 兼容在线端点
    # （DeepSeek / 阿里百炼 / 智谱 ChatGLM / Kimi / 硅基流动 …）
    "llm_backend": "ollama",
    "llm_api_base": "",           # 例如 https://api.deepseek.com/v1
    "llm_api_key": "",
    "llm_preset": "",             # 选中的在线预设 id（界面回显用）
    # ---- 识别语言（SenseVoice 原生支持 auto/zh/yue/en/ja/ko）----
    "asr_language": "auto",
    # ---- 记忆模式：full=超长记忆 / summary=本地总结 / none=无记忆 ----
    "memory_mode": "full",
    "memory_summary_model": "",   # 压缩历史用的小模型（空=用当前对话模型）
    # ---- 自动休眠：闲置多少秒后休眠（0=不休眠）----
    "idle_sleep_s": 300,
    # ---- 智能家居（HomeAssistant）----
    "ha_enabled": False,
    "ha_base_url": "",            # 例如 http://192.168.1.50:8123
    "ha_token": "",               # 长期访问令牌
    "auto_start_call": True,      # 打开软件后自动开始通话
    "save_transcript": True,      # 退出时把对话记录存到 transcripts/
    "always_on_top": False,
    # ---- MCP 工具生态 / 工具调用（对齐小智官方）----
    "mcp_enabled": True,          # 允许设备侧 MCP 与 MCP endpoint 接入
    "mcp_servers": {},            # 第三方 MCP server 配置（mcpServers 格式）
    "tools_first": False,         # True=把工具交给大模型自主调用（function calling）
    "tool_loop_max_turns": 4,     # 工具调用最多几轮
    "tool_ack_tts": True,         # 调工具前先播一句"我查一下"
    # ---- HTTP 端点（OTA / 视觉）----
    "http_port": 8003,
    "ota_enabled": True,
    "auth_enabled": False,        # 设备鉴权（局域网默认关）
    "auth_key": "",
    # ---- 声纹 / 视觉 / 实时语音（P2）----
    "voiceprint_enabled": False,
    "voiceprint_mode": "remote",  # remote / local
    "voiceprint_base_url": "",
    "voiceprint_key": "",
    "vision_enabled": False,
    "vision_camera_index": 0,
    "vision_model": "",
    "realtime_enabled": False,
    "realtime_base_url": "",
    "realtime_api_key": "",
    "realtime_model": "gpt-4o-realtime-preview",
    # ---- PC 端唤醒词：off/always=打开即听，wake=只有唤醒词才上行 ----
    "wake_mode": "always",
    "wake_threshold": 0.5,
    "wake_models": [],            # 自定义唤醒词 onnx 路径（空=用内置预置）
    # 路径（留空用内置默认）
    "sensevoice_dir": "",
    "silero_dir": "",
    "ollama_base_url": "http://localhost:11434",
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except Exception:  # noqa: BLE001
        pass
    # 一次性迁移：barge_factor 的语义换过两轮（旧版 4.0 是"×播放电平"，
    # 上一轮 1.2 是"×回声估计"但挡不住耦合突变 → 自激活）。现在默认 1.8，
    # 落在 [1.5, 3.5] 之外的老值一律换成新默认；barge_frames 旧默认 8。
    migrated = False
    try:
        f = float(cfg.get("barge_factor") or 0)
        if not (1.5 <= f <= 3.5):
            cfg["barge_factor"] = DEFAULTS["barge_factor"]
            migrated = True
        # barge_frames 的默认值也换过：6 帧（360ms）是"自激活"那版的默认，
        # 给耦合估计器的自查窗口太短。夹到合法区间 [4,14]，并把 6 提到 8。
        v = int(cfg.get("barge_frames") or 0)
        if v < 4 or v > 14:
            cfg["barge_frames"] = DEFAULTS["barge_frames"]
            migrated = True
        elif v == 6:
            cfg["barge_frames"] = DEFAULTS["barge_frames"]
            migrated = True
    except (TypeError, ValueError):
        cfg["barge_factor"] = DEFAULTS["barge_factor"]
        cfg["barge_frames"] = DEFAULTS["barge_frames"]
        migrated = True
    if migrated:
        save(cfg)
    return cfg


def save(cfg: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception:  # noqa: BLE001
        pass


def transcripts_dir() -> str:
    d = os.path.join(data_dir(), "transcripts")
    os.makedirs(d, exist_ok=True)
    return d
