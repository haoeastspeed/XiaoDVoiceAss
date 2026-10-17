# -*- coding: utf-8 -*-
"""小D网关配置：默认值 + 可选覆盖文件。"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict


def _root() -> str:
    """数据/日志根目录。

    - 环境变量 XIAOD_DATA_DIR 优先（独立应用会把可写目录指过去）；
    - 打包成 exe（PyInstaller onefile）时用 exe 所在目录（便携）；
    - 否则用源码仓库根目录。
    """
    env = os.environ.get("XIAOD_DATA_DIR")
    if env:
        return env
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bin_dir() -> str:
    """opus.dll / ffmpeg 所在目录（打包后随包解包到 _MEIPASS）。"""
    meipass = getattr(sys, "_MEIPASS", "")
    for d in ([os.path.join(meipass, "bin"), meipass] if meipass else []) + \
             [os.path.join(ROOT, "reference", "bin")]:
        if d and os.path.isdir(d):
            return d
    return os.path.join(ROOT, "reference", "bin")


ROOT = _root()
# 自包含运行库（opus.dll / ffmpeg / ffprobe）
BIN_DIR = _bin_dir()

# 本地模型目录（本机已就位）
SENSEVOICE_DIR = (
    "D:/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
)
SILERO_DIR = "D:/models/snakers4_silero-vad"

# 可选在线音色（EdgeTTS voice id → 显示名）。
#
# id 必须用 edge_tts.list_voices() 返回的**权威拼写**——这东西大小写敏感而且
# 不统一：`liaoning` / `shaanksi` 是**小写**，其余地区名是大写。写错不会报错，
# 只会表现为"选了没反应"。扩充或改动请先用 list_voices() 核对一遍。
#
# 这里只放精选常用的一部分；运行时还会用 edge_tts.list_voices() 拉取官方全量
# 列表做补充（见 tts_edge.online_voices()），所以不是硬上限。
EDGE_VOICES = {
    # ---- 普通话 ----
    "zh-CN-XiaoxiaoNeural": "晓晓（女声·温暖亲切）",
    "zh-CN-XiaoyiNeural": "晓伊（女声·活泼甜美）",
    "zh-CN-liaoning-XiaobeiNeural": "晓北（女声·东北腔）",
    "zh-CN-shaanxi-XiaoniNeural": "晓妮（女声·陕西腔）",
    "zh-CN-YunxiNeural": "云希（男声·清朗）",
    "zh-CN-YunxiaNeural": "云夏（男声·少年）",
    "zh-CN-YunjianNeural": "云健（男声·沉稳有力）",
    "zh-CN-YunyangNeural": "云扬（男声·新闻播报）",
    # ---- 台湾 / 粤语 ----
    "zh-TW-HsiaoChenNeural": "曉臻（女声·台湾）",
    "zh-TW-HsiaoYuNeural": "曉雨（女声·台湾）",
    "zh-TW-YunJheNeural": "雲哲（男声·台湾）",
    "zh-HK-HiuGaaiNeural": "曉佳（女声·粤语）",
    "zh-HK-HiuMaanNeural": "曉漫（女声·粤语）",
    "zh-HK-WanLungNeural": "彎龍（男声·粤语）",
    # ---- 英语（美式）----
    "en-US-JennyNeural": "Jenny（女声·美式·亲切）",
    "en-US-AriaNeural": "Aria（女声·美式·明快）",
    "en-US-AvaNeural": "Ava（女声·美式·富有感染力）",
    "en-US-EmmaNeural": "Emma（女声·美式·开朗）",
    "en-US-MichelleNeural": "Michelle（女声·美式·知性）",
    "en-US-AnaNeural": "Ana（女声·美式·可爱）",
    "en-US-GuyNeural": "Guy（男声·美式·热情）",
    "en-US-BrianNeural": "Brian（男声·美式·温和）",
    "en-US-ChristopherNeural": "Christopher（男声·美式·稳重）",
    "en-US-EricNeural": "Eric（男声·美式·理性）",
    "en-US-RogerNeural": "Roger（男声·美式·活泼）",
    # ---- 英语（英式）----
    "en-GB-SoniaNeural": "Sonia（女声·英式）",
    "en-GB-LibbyNeural": "Libby（女声·英式·亲切）",
    "en-GB-RyanNeural": "Ryan（男声·英式）",
    "en-GB-ThomasNeural": "Thomas（男声·英式）",
    # ---- 日语 / 韩语 ----
    "ja-JP-NanamiNeural": "Nanami（女声·日语）",
    "ja-JP-KeitaNeural": "Keita（男声·日语）",
    "ko-KR-SunHiNeural": "SunHi（女声·韩语）",
}


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
    # 语言模型后端：ollama=本地 Ollama；openai=OpenAI 兼容在线端点
    # （DeepSeek / 阿里百炼 / 智谱 ChatGLM / Kimi / 硅基流动 …）
    llm_backend: str = "ollama"
    llm_api_base: str = ""
    llm_api_key: str = ""
    llm_preset: str = ""          # 选中的在线预设 id（仅用于界面回显）
    tts_voice: str = "zh-CN-XiaoxiaoNeural"
    # TTS 引擎：auto=在线优先、断网自动降级本地离线；online=只用在线；local=只用本地
    tts_engine: str = "auto"
    sensevoice_dir: str = SENSEVOICE_DIR
    silero_dir: str = SILERO_DIR

    # 识别语言：auto=自动判断，或指定 zh / yue / en / ja / ko
    # （SenseVoice 原生支持这五种，指定后识别准确率高于自动）
    asr_language: str = "auto"

    # ---- 记忆模式 ----
    # full    = 超长记忆：完整保留对话历史（最多 memory_max_turns 轮）
    # summary = 本地总结：超出窗口后用本地模型把早期历史压成摘要，只留摘要
    # none    = 无记忆：每轮独立，只带人设，不带上文
    memory_mode: str = "full"
    memory_max_turns: int = 40      # full 模式保留的最大对话轮数
    # 摘要触发/保留：历史超过这么多轮就开始压缩，压缩后保留最近这么多轮原文
    memory_summary_after: int = 12
    memory_keep_recent: int = 4
    # 压缩用的模型（空=用当前对话模型）。想省时间就指一个小的本地模型。
    memory_summary_model: str = ""

    # ---- 自动休眠 ----
    # 多久没有任何交互就把会话"睡下去"（停采集、释放上下文之外的资源）。
    # 0 = 不休眠。设备/客户端下一次说话或手动开始即唤醒。
    idle_sleep_s: int = 300

    # ---- 智能家居（HomeAssistant）----
    ha_enabled: bool = False
    ha_base_url: str = ""          # 例如 http://192.168.1.50:8123
    ha_token: str = ""             # 长期访问令牌

    # VAD 断句
    vad_threshold: float = 0.5
    vad_threshold_low: float = 0.3
    min_silence_ms: int = 600

    # 自播放回声兜底（客户端未做回声抑制时，丢弃与自己播报内容重合的识别结果）
    echo_dedup: bool = True
    echo_dedup_window_s: float = 8.0

    # ---- 回声门（网关侧第一道闸：播放期/尾音期不吃上行）----
    #
    # 设备是单麦无回采硬件（0 个播放参考通道），播放期麦克风会把自己扬声器
    # 的声音上行上来。闸门在播报期间直接不跑 VAD、不累积、不切 listening，
    # 从源头掐掉"设备自问自答"和"被自己的尾音打断"。
    echo_gate: bool = True
    # tts_stop 之后还要静默多久（毫秒）。设备端播放缓冲 + 房间混响的尾巴
    # 通常比"服务端认为播完了"晚 200~600ms。
    echo_gate_cooldown_ms: int = 700
    # 唤醒词-only 的 detect 自激活判定窗口：刚播报完这么多秒内收到
    # "只有唤醒词"的 detect 视为设备自激活丢弃（见 session._is_self_activated_wake）
    selfwake_window_s: float = 6.0
    # 闸门期是否允许用 AEC 处理后的信号检测语音打断（仅 echo_cancel 生效时）
    echo_gate_barge: bool = True

    # ---- 网关侧回声消除（第二道：用自身 TTS 当参考，把回声减掉）----
    #
    # off=纯闸门（不跑 AEC）；on=强制启用；auto=库可用就用（推荐）。
    # 详见 gateway/audio/aec.py 的模块注释。
    echo_cancel: str = "auto"
    # 回路延迟初值（毫秒）：只作为 AEC3 收敛的起点，它自己会继续估计。
    echo_aec_delay_ms: int = 180

    # 联网搜索：开启后，模型会自行判断本轮是否需要查实时/外部信息，
    # 需要就先检索再回答（检索结果作为参考上下文注入，不直接念出来）。
    web_search: bool = False
    web_search_max: int = 4

    # ---- MCP / 工具生态（对齐官方 xiaozhi-esp32）----
    # 总开关：允许设备侧 MCP（对端 PC 客户端 / ESP32 暴露的 self.* 工具）
    # 与 MCP endpoint（第三方 server）接入。
    mcp_enabled: bool = True
    # MCP endpoint 配置，格式对齐官方 mcp_server_settings.json：
    #   {"mcpServers": {"name": {"command":..., "args":[...]}
    #                          | {"url":..., "transport":"streamable-http"|"sse",
    #                             "headers":{...}}}}
    # 默认空：本地零外部进程，只有用户显式配置才连接。
    mcp_endpoints: Dict[str, Any] = field(default_factory=dict)

    # 工具优先（function calling）：把工具交给大模型由它自己决定何时调用。
    #   False（默认）—— 完全退回改造前的行为：不发 tools，家居/搜索走 prompt
    #                    模拟路径。MCP 工具会被发现并注册，但暂不交给模型。
    #   True          —— 开启工具调用：MCP / 内置 self.gateway.* / 家居 / 搜索
    #                    都作为工具交给模型；工具失败自动回落 prompt 路径。
    tools_first: bool = False
    # 工具调用循环最多几轮（防止模型反复调工具不收敛）
    tool_loop_max_turns: int = 4
    # 调工具前是否先播一句"好的，我查一下"（降低"卡住了"的错觉）
    tool_ack_tts: bool = True
    tool_ack_text: str = "好的，我查一下。"

    # ---- HTTP 端点（OTA / 视觉，P1-C / P2-B）----
    http_port: int = 8003          # OTA / vision 用；0 = 不启 HTTP 服务
    ota_enabled: bool = True       # 是否启用 /xiaozhi/ota/ 端点
    tts_preview_enabled: bool = True   # /tts/preview/ 试听合成端点（客户端用）
    # 设备鉴权：默认关（局域网信任）。开了之后 WS 握手与 OTA 需带 Bearer token。
    auth_enabled: bool = False
    auth_key: str = ""             # HMAC 签名密钥（空则用内置默认）
    allowed_devices: list = field(default_factory=list)  # 白名单 device-id
    # 对外暴露的 WS 地址（设备 OTA 下发用；留空则用 lan_ip:port 自动拼）
    websocket_url: str = ""
    # 固件下载地址前缀（留空则用 http://lan_ip:http_port/xiaozhi/ota/download/）
    ota_download_base: str = ""
    timezone_offset: int = 8          # OTA server_time 时区偏移（小时）
    firmware_cache_ttl: int = 30      # 固件目录扫描缓存 TTL（秒）

    # ---- MQTT 网关（P1-B：MQTT+UDP 传输用）----
    # mqtt_gateway 非空时，OTA 会给设备下发 mqtt 配置而不是 websocket；
    # 值形如 "mqtt://host:1883" 或 "host:1883"（对齐官方 server.mqtt_gateway）
    mqtt_gateway: str = ""
    mqtt_signature_key: str = ""      # MQTT 密码 HMAC 密钥
    mqtt_broker_host: str = "0.0.0.0"  # 内置/外置 broker 监听地址
    mqtt_broker_port: int = 1883
    mqtt_use_builtin_broker: bool = False  # True=内置 amqtt（可选，默认用外置）
    udp_port: int = 8001              # UDP 音频通道端口

    # ---- 说话人识别（声纹，P2-A）----
    voiceprint_enabled: bool = False
    # remote = 走官方同款 HTTP API；local = 本地 sherpa-onnx 声纹模型
    voiceprint_mode: str = "remote"
    voiceprint_base_url: str = ""  # remote 模式的 API 地址
    voiceprint_key: str = ""       # remote 模式的鉴权 key
    voiceprint_similarity: float = 0.4
    voiceprint_model_dir: str = ""   # local 模式的 sherpa-onnx 声纹模型目录
    voiceprint_speakers: list = field(default_factory=list)  # ["id,name,desc"]

    # ---- 摄像头视觉（P2-B）----
    vision_enabled: bool = False
    vision_camera_index: int = 0
    vision_model: str = ""         # 空 = 用当前 llm_model（多模态时）
    vision_max_bytes: int = 5 * 1024 * 1024

    # ---- 端到端 Realtime（P2-D）----
    realtime_enabled: bool = False
    realtime_base_url: str = ""    # OpenAI Realtime 兼容 ws 地址
    realtime_api_key: str = ""
    realtime_model: str = "gpt-4o-realtime-preview"

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
    # 运行时覆盖（TTS 音色等）：由 set_voice 控制消息写入，重启后仍生效
    ov_path = os.path.join(ROOT, ".tmp", "gw_overrides.json")
    if os.path.isfile(ov_path):
        try:
            with open(ov_path, "r", encoding="utf-8") as f:
                ov = json.load(f)
            for k, v in ov.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        except Exception:
            pass
    # 环境变量覆盖（独立进程服务 / 多实例部署用）：XIAOD_PORT、XIAOD_HOST
    for key, attr in (("XIAOD_PORT", "port"), ("XIAOD_HOST", "host")):
        v = os.environ.get(key)
        if v:
            try:
                setattr(cfg, attr, int(v) if attr == "port" else v)
            except ValueError:
                pass
    return cfg
