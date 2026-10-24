# 小D智能语音助手 · XiaoDVoiceAss

本地优先的实时语音助手：**ESP32 固件 + 自建语音网关 + PC 客户端**三位一体，完整对齐小智（xiaozhi-esp32）官方协议，同时保留全部本地/离线能力。

![logo](app/ui/assets/logo256.png)

## 功能特性

- **三种模式**：本地小D网关 / 小智官方云 / 离线命令（MultiNet 闭集识别），固件内自由切换
- **语音链路**：Opus 上行 + VAD 断句 + 网关 AEC 回声消除 + 协议层硬闸门（播放期不上行，根治自激活/自接话）
- **TTS**：在线 Edge-TTS + 本地 Kokoro / SAPI，音色热切换、失败自动降级、合成前自动剥离 Markdown 与表情符号
- **LLM**：Ollama / OpenAI 兼容端点，MCP 工具调用（网页搜索、天气、智能家居、设备端工具、视觉解释）
- **传输**：WebSocket / MQTT + UDP(AES-CTR) 双通道、二进制帧 v1–v3、OTA 升级、声纹识别、Realtime 全双工
- **PC 客户端**：pywebview 界面，设备扫描、音色试听、网关与 Ollama 自托管、崩溃黑匣子（faulthandler）

## 下载编译成品

Release 页面：<https://github.com/haoeastspeed/XiaoDVoiceAss/releases>

| 资产 | 大小 | 用途 | 直链 |
| --- | --- | --- | --- |
| `XiaoDVoiceAss-v1.0.0-portable.exe` | 69 MB | 免安装单文件，拷贝即用 | [下载](https://github.com/haoeastspeed/XiaoDVoiceAss/releases/download/v1.0.0/XiaoDVoiceAss-v1.0.0-portable.exe) |
| `XiaoDVoiceAss-v1.0.0-windows-x64.zip` | 70 MB | onedir 版，启动更快，推荐日常使用 | [下载](https://github.com/haoeastspeed/XiaoDVoiceAss/releases/download/v1.0.0/XiaoDVoiceAss-v1.0.0-windows-x64.zip) |
| `firmware-v1.0.0.zip` | 10 MB | ESP32 完整可刷镜像 + 烧录脚本 | [下载](https://github.com/haoeastspeed/XiaoDVoiceAss/releases/download/v1.0.0/firmware-v1.0.0.zip) |

## 快速开始

### PC 客户端

1. 下载 Release 中的「单文件版」或「极速版」
2. 双击 `小D智能助手.exe`（或 `启动小D助手.vbs`，零控制台启动）
3. 首次启动自动拉起本地网关与 Ollama；在界面选择音色与模型即可开始语音对话
4. 想换构建不换配置：直接覆盖 exe 所在目录，配置在 `data/` 下自动保留

### ESP32 固件

烧录与接线见 [docs/FLASHING.md](docs/FLASHING.md)；镜像在 Release 的 `firmware-v1.0.0.zip` 内。
屏幕为 240×240；音量键接 GPIO39/40，屏 DC 已改接 GPIO38（见工程笔记）。

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/PLAN.md](docs/PLAN.md) | 项目规划与里程碑 |
| [docs/GATEWAY_PROTOCOL.md](docs/GATEWAY_PROTOCOL.md) | 网关协议（消息/二进制帧/MCP） |
| [docs/GATEWAY_DESIGN.md](docs/GATEWAY_DESIGN.md) | 网关架构与回声防御设计 |
| [docs/OFFLINE_MODE.md](docs/OFFLINE_MODE.md) | 离线命令模式 |
| [docs/ENGINEERING_NOTES.md](docs/ENGINEERING_NOTES.md) | 工程笔记（踩坑与约定） |
| [docs/FIRMWARE_ACCEPTANCE_3MODE.md](docs/FIRMWARE_ACCEPTANCE_3MODE.md) | 固件三模式验收清单 |

## 自行打包

```bat
打包助手应用.bat
```

产物输出到 `dist\`（极速版 + 单文件版）；打包后可用 `python tools\verify_pack.py <exe>` 校验完整性。

## 目录结构

```
app/          PC 客户端（pywebview UI + 网关托管 + 配置）
gateway/      语音网关（协议/会话/AEC/TTS/ASR/LLM/MCP 工具链）
firmware/     ESP32 固件（fork 自 xiaozhi-esp32，双模式）
docs/         设计文档与验收清单
tools/        打包校验、音色生成等辅助脚本
tests/        回归测试
```

## 致谢

- [78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) —— 协议与固件上游
- Edge-TTS / Kokoro / sherpa-onnx / Ollama —— 语音与大模型能力

## 许可

本项目含第三方组件，请遵循各自许可；固件部分继承上游 LICENSE。
