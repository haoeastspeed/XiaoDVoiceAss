# 小D 网关协议实现说明

> 对齐官方 [xiaozhi-esp32 WebSocket 协议](https://github.com/78/xiaozhi-esp32/blob/main/docs/websocket_zh.md) /
> [MQTT+UDP 协议](https://github.com/78/xiaozhi-esp32/blob/main/docs/mqtt-udp_zh.md) /
> [MCP 协议](https://github.com/78/xiaozhi-esp32/blob/main/docs/mcp-protocol_zh.md)。
> 本文只描述**本网关实际实现的协议子集与扩展**；每条断言在 `tests/` 里都有对应用例
> （见文末对照表）。

## 1. 传输

| 传输 | 状态 | 端点 / 参数 |
| --- | --- | --- |
| WebSocket | ✅ 默认 | `ws://<host>:8000/xiaozhi/v1/`，必须带 `Device-Id` 头（或 `?device-id=`） |
| MQTT + UDP | ✅ 可选 | 控制走 MQTT（`device-server` / `devices/p2p/{mac}`），音频走 UDP |
| HTTP | ✅ 可选 | OTA / 视觉上传，默认端口 8003 |

握手头：`Device-Id`（MAC）、`Client-Id`、`Authorization: Bearer <token>`（仅开启
`auth_enabled` 时校验）、`Protocol-Version`。

## 2. 二进制帧（版本协商）

客户端在 `hello` 里带 `version`（1/2/3），网关夹到支持集并**在 hello 响应里回显**；
之后双向二进制帧都按该版本封装。非法值回退 1。

| 版本 | 布局 |
| --- | --- |
| 1（默认） | 裸 Opus，无头 |
| 2 | `>HHIII` 16 字节头：version(u16) / type(u16, 0=OPUS 1=JSON) / reserved(u32) / timestamp(u32 ms) / payload_size(u32) + payload |
| 3 | `>BBH` 4 字节头：type(u8) / reserved(u8) / payload_size(u16) + payload |

* v2 的 `timestamp` 用于服务器端 AEC 对时；本网关的 AEC 依赖 AEC3 自带延迟估计，
  不使用该字段做对齐（保持既有行为）。
* v2/v3 里的 JSON 类型帧（type=1）会被拆出来当文本消息处理。

实现：`gateway/binframe/codec.py` · 测试：`tests/test_binframe.py`

## 3. JSON 消息

### 客户端 → 服务端

| type | 字段 | 说明 |
| --- | --- | --- |
| `hello` | `version` `features{mcp,emoji}` `audio_params` `device_id` | 握手 |
| `listen` | `state=start/stop/detect`，`mode=auto/manual/realtime` | start 幂等处理 |
| `abort` | `reason` | 打断（auto 下只表示"别说了"，继续采集） |
| `mcp` | `payload` = JSON-RPC 2.0 | 设备侧 MCP 响应（见 §5） |
| `ping` | — | 心跳 |
| `iot` | — | 官方已废弃；本网关**记录并忽略** |

**扩展**（客户端控制网关，官方无此消息，但设备侧会安全忽略）：
`set_voice` / `set_tts_engine` / `set_model` / `set_prompt` / `set_search` /
`set_language` / `set_memory` / `clear_history`。
其中只有显式带 `"persist": true` 的连接才写盘（桌面应用是权威来源）。

### 服务端 → 客户端

| type | 字段 | 说明 |
| --- | --- | --- |
| `hello` | `session_id` `version` `transport` `audio_params` | 握手响应 |
| `stt` | `text`，**扩展** `speaker` | 识别结果；`speaker` 为声纹识别到的说话人 |
| `tts` | `state=start/sentence_start/sentence_end/stop`，`text` `sentence_id` | 播报控制 |
| `goodbye` | `session_id` | 结束 |
| `pong` | — | 心跳应答 |
| `search`（扩展） | `state=start/end` `query` `count` | 联网搜索进度 |
| `notice`（扩展） | `text` | 给用户的一条提示（TTS 降级等） |
| `custom`（扩展） | `action` + 参数 | 网关下达的客户端动作（如 `set_volume`） |
| `mcp` | `payload` = JSON-RPC 2.0 | 网关发起的 MCP 请求（见 §5） |

**音频参数**：上行 `opus / 16000 / 1 / 60ms`（960 样本），下行 `opus / 24000 / 1 / 60ms`
（1440 样本）。客户端声明的 `audio_params.format` 会在 hello 响应里沿用。

## 4. 状态机与回声抑制

状态：`idle → listening → thinking → speaking → listening`（auto 模式）。

回声门（防自激活，设备单麦无回采的主防线）：
1. 协议层：`tts start` 起进入播放期，播放期**不跑 VAD / 不累积 / 不切 listening**；
2. 尾音窗：`tts stop` 后再静默 `echo_gate_cooldown_ms`（默认 700ms）；
3. 冷却窗只由 `abort` / 手动 `listen start` / AEC 确认的真打断清除；
4. 闸门放行的第一帧必须重置 VAD 句子状态（Silero 的 ended 是粘性的）；
5. 文本级兜底：识别结果与近期播报内容高度重合 → 判为自播放回声丢弃。

断句：服务端按"句尾静音 ≥ `min_silence_ms`"且"累计语音 ≥ `_MIN_SPEECH_MS`（绝对值，
不用占比）"判句末，另有 20s 兜底。

## 5. MCP（Model Context Protocol）

信令：`{"type":"mcp","payload":<JSON-RPC 2.0>}`。网关是 **MCP 客户端**，对端
（PC 客户端 / ESP32）是 **MCP server**。

流程（`docs/mcp-protocol_zh.md`）：
1. 设备 `hello` 带 `features.mcp=true`
2. 网关 → 设备 `initialize`（**id=1**；`params.capabilities` 含
   `vision{url,token}`（开启视觉时）与 `clientInfo`）
3. 设备 → 网关 `result.serverInfo`
4. 网关 → 设备 `tools/list`（**id=2**，`params.cursor` 分页；`nextCursor` 非空继续拉）
5. 设备 → 网关 `result.tools[]`
6. 调用：网关 → 设备 `tools/call`（id≥100，`params{name,arguments}`）→ 设备回
   `result{content:[{type:text,text}],isError}` 或 `error{code,message}`

工具名一律 `sanitize_tool_name`（保留 `[a-zA-Z0-9_\-]` 与中文，其余 → `_`），
调用时用 `name_mapping` 还原对端原名。

**三源合一**（`gateway/tools/`）：
| 来源 | 命名 | 说明 |
| --- | --- | --- |
| 设备侧 | 原名 sanitize，如 `self_get_device_status` | 对端能力 |
| 网关内置 | `self_gateway_*` | 网关自身控制（状态/音量/音色/模型/记忆/休眠） |
| MCP endpoint | `{endpoint}_{tool}` | 第三方 MCP server（stdio / streamable-http / sse） |

**endpoint 配置**（`mcpServers` 格式，同官方 `mcp_server_settings.json`）：
```json
{"mcpServers": {
  "fs":   {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]},
  "web":  {"url": "http://127.0.0.1:9000/mcp", "transport": "streamable-http"}
}}
```

**工具调用循环**：`tools_first=true` 时把工具交给大模型（function calling），
最多 `tool_loop_max_turns` 轮；调工具前可先播 `tool_ack_text`；工具失败或模型不
支持 tools 时自动回落"普通对话 + prompt 路径"（HA/搜索的 prompt 实现**始终保留**）。

实现：`gateway/tools/*` · 测试：`tests/test_mcp_tools.py`、`tests/test_tool_loop.py`

## 6. MQTT + UDP

### MQTT（控制）
* 设备发布 `device-server`，服务端发布 `devices/p2p/{mac}`（`:`→`_`）
* `client_id = GID_{model}@@@{mac}@@@{mac}`
* `username  = base64({"ip":"unknown"})`
* `password  = base64(HMAC-SHA256(mqtt_signature_key, client_id + "|" + username))`
* hello 响应带 `udp{server, port, key, nonce}`

### UDP（音频）
16 字节计数器块（**就是 AES-CTR 的初始计数块**）：

| 偏移 | 长度 | 字段 |
| --- | --- | --- |
| 0 | 1 | type，固定 `0x01`（接收端校验） |
| 1 | 1 | flags |
| 2 | 2 | payload_len（大端） |
| 4 | 4 | ssrc |
| 8 | 4 | timestamp（大端） |
| 12 | 4 | sequence（大端） |

其后为密文。**服务端下发的 `nonce[0]` 必须是 `0x01`**；设备发送时以该 nonce 为
模板，只覆写 `[2:4]` / `[8:12]` / `[12:16]`。服务端解密用原始 16 字节头作为计数块。

序号：`sequence <= remote_sequence` 直接丢弃（防重放）；跳号仅告警。

实现：`gateway/transport/{base,udp,mqtt}.py` · 测试：`tests/test_transport_udp.py`

## 7. OTA + 设备激活

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/xiaozhi/ota/` | 设备上报型号/版本 → 返回接入配置与固件更新 |
| GET | `/xiaozhi/ota/` | 文本健康检查 |
| GET | `/xiaozhi/ota/download/{file}` | 下载固件（`data/bin/{model}_{version}.bin`） |
| POST | `/mcp/vision/explain` | 视觉上传（multipart: question + image） |

POST 请求头：`Device-Id`、`Client-Id`（缺任一 → `{"success":false,"message":"request error."}`）、
`Device-Model`、`Device-Version`（缺失时回退 body 的 `board.type` / `application.version`）。

响应：
```json
{
  "server_time": {"timestamp": 1730000000000, "timezone_offset": 480},
  "firmware": {"version": "1.0.0", "url": ""},
  "websocket": {"url": "ws://192.168.1.9:8000/xiaozhi/v1/", "token": ""}
}
```
配了 `mqtt_gateway` 时改为下发 `mqtt{endpoint, client_id, username, password,
publish_topic, subscribe_topic}`。

版本比较用"数字元组"（`1.2.10 > 1.2.9`）；下载端点做 basename 清洗 + realpath
防目录穿越，文件名必须匹配 `^[A-Za-z0-9\.\-_]+\.bin$`。

CORS：`Access-Control-Allow-Headers: client-id, content-type, device-id, authorization`。

实现：`gateway/http/{httpd,ota,vision}.py`、`gateway/auth.py` · 测试：`tests/test_ota.py`

## 8. 鉴权（可选）

`auth_enabled=false`（默认）：局域网信任，全部放行。
开启后：

* WS 握手校验 `Authorization: Bearer <token>`
* OTA 通过 `POST /xiaozhi/ota/` 签发 token 给设备
* token = `base64url(HMAC-SHA256(auth_key, "client_id|username|ts")).ts`，默认 30 天有效
* `allowed_devices` 白名单命中则直放行（不校验 token）

## 9. 能力开关

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `mcp_enabled` | true | MCP 工具生态总开关 |
| `tools_first` | **false** | 是否把工具交给大模型自主调用（关=改造前行为） |
| `ota_enabled` / `http_port` | true / 8003 | HTTP 端点 |
| `auth_enabled` | false | 设备鉴权 |
| `voiceprint_enabled` | false | 说话人识别 |
| `vision_enabled` | false | 摄像头看图（需 OpenCV + 多模态模型） |
| `realtime_enabled` | false | 端到端实时语音 |
| `wake_mode`（客户端） | always | always=打开即听 / wake=唤醒词 |

## 10. 测试对照表

| 协议断言 | 用例 |
| --- | --- |
| v1/v2/v3 帧字节布局、畸形帧容错 | `tests/test_binframe.py` |
| MCP id 约定、分页、参数清洗、内置工具 | `tests/test_mcp_tools.py` |
| tool_calls 累积、多轮循环、熔断回落 | `tests/test_tool_loop.py` |
| UDP 加解密字节布局、防重放、topic/凭据 | `tests/test_transport_udp.py` |
| OTA 响应结构、版本比较、路径穿越、CORS、token | `tests/test_ota.py` |
| 声纹/视觉/Realtime 格式与开关 | `tests/test_p2_providers.py` |
| 延迟 import 全部写进打包配置 | `tests/test_packaging_imports.py` |
| 不回归：回声门/打断/音色/多语言/记忆/家居 | `tests/test_echo_gate.py` 等 |
