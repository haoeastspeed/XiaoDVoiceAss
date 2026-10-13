# 小D网关（XiaoD Gateway）设计 · M2

> 自研轻量、小智协议兼容的实时语音网关。目标：**全双工、低延迟、可打断、可复用**。
> 同时服务 DeskWork 实时语音（M3）与 ESP32 本地模式（M4/M5）。

## 1. 目标与边界

- **做**：协议兼容、全双工音频管道、状态机、打断、流式 LLM/TTS、模型适配层。
- **不做**：不深度 fork 开源服务端；不做官方云转发（固件直连）。
- 可独立运行（main.py），也可作为模块被 DeskWork 进程内嵌（M3）。

## 2. 协议兼容（对齐小智 v1）

- WS：`/xiaozhi/v1/`；连接必须带 `device-id`（header 或 query）。
- 握手：客户端 `hello` → 服务端返回 `{type:hello, session_id, version, transport, audio_params}`。
- 客户端：`listen`（start/mode、stop、detect/text）、`abort`、`iot`、`mcp`、`ping`。
- 服务端：`stt`、`tts`（start/sentence_start/sentence_end/stop）、二进制 opus、`mcp`、`goodbye`。
- 音频：**上行 opus 16000/1/60ms/960 samples**；**下行 opus 24000/1/60ms/1440 samples**。

## 3. 状态机

```
        hello                      VAD断句
 IDLE ──────────▶ LISTENING ───────────────▶ THINKING
  ▲                  ▲  ▲                       │
  │                  │  │ abort/唤醒词          │ 首句就绪
  │  tts stop        │  └───────────────────────┼──▶ SPEAKING
  └──────────────────┴──────────────────────────┘
```

- 打断（SPEAKING 中检测到新语音/唤醒词/abort）：立即取消 TTS 发送 → LISTENING。
- 状态由会话持有，配合 asyncio.Event 协调，避免阻塞。

## 4. 全双工音频管道（核心）

两个并发方向，互不阻塞：

- **上行任务**：收二进制 → opus 解码 → VAD（持续）→ 按句缓存 → 断句触发 ASR。
- **下行任务**：ASR 文本 → LLM 流式（按句聚合）→ TTS 流式（按句合成 opus）→ 发送。
- 关键：首句 TTS 不等全文；用户一旦开口，下行立即 abort。

## 5. 模型适配层（统一接口、可替换）

| 能力 | 默认实现 | 接口 |
|---|---|---|
| LLM | Ollama（qwen3.5:9b-q4km） | `chat(messages, stream) -> chunks` |
| ASR | SenseVoice（sherpa-onnx） | `transcribe(pcm) -> text` |
| TTS | EdgeTTS（可扩展离线） | `synthesize(text) -> opus packets` |
| VAD | Silero VAD | `is_speech(pcm) -> state` |

- 所有 provider 实现统一基类，配置可切换；重资源懒加载、单例复用。

## 6. 目录结构

```
gateway/
  __init__.py
  config.py          # 配置加载（默认值 + 可选覆盖）
  protocol.py        # 消息构造/解析（无业务）
  session.py         # 会话状态机 + 全双工编排
  server.py          # WS 服务器 / 握手
  providers/
    base.py          # 统一接口
    llm_ollama.py
    asr_sensevoice.py
    tts_edge.py
    vad_silero.py
  audio/
    opus.py          # 编解码（16k 上行 / 24k 下行）
main.py              # 独立入口
```

## 7. 低延迟与稳定性

- 句级流式：LLM 按标点切句，TTS 逐句，首句延迟最小化。
- 打断：asyncio task 取消 + 队列清空，发送 tts stop。
- 容错：单连接异常不影响服务；解码/识别失败降级为提示语，不崩。
- 资源：组件按设备共享、懒加载；连接关闭即清理。

## 8. 验收（M2）

- 复用 M1 文本/音频测试：握手、文本、音频、打断均通过。
- 独立 `main.py` 可启动监听；多连接互不干扰。
- 首句延迟、打断响应在本机可接受（实测记录）。
