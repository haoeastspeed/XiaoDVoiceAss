# 工程笔记（Engineering Notes）

> 记录部署、联调中验证过的关键事实与踩坑，供后续里程碑直接复用。
> 每条均在 M1 实测中确认。

## 1. 运行库自包含（`reference/bin`）

- 目录必须包含：
  - `opus.dll`（libopus 1.6.1，conda-forge 提取）
  - `ffmpeg.exe` + `ffprobe.exe`（**两者都要**，pydub 处理 EdgeTTS 时需 ffprobe）
  - 7 个动态库 DLL：`avcodec-63 / avdevice-63 / avfilter-12 / avformat-63 / avutil-61 / swresample-7 / swscale-10`
- 该 ffmpeg 是**动态编译版**（exe 仅 0.5MB）：缺 DLL 运行报
  `0xC0000135 = STATUS_DLL_NOT_FOUND`（exit code 3221225781）。
- 启动前把 `reference/bin` 注入 `PATH`。

## 2. 音频参数（关键，勿混淆）

| 方向 | 采样率 | 声道 | 帧长 | 每帧 samples |
|---|---|---|---|---|
| 上行（麦克风） | **16000** | 1 | 60ms | **960** |
| 下行（TTS） | **24000** | 1 | 60ms | 1440 |

- 服务端上行解码器硬编码 `opuslib_next.Decoder(16000, 1)`，`decode(packet, 960)`。
- `opuslib_next` 在 Windows 用 `find_library("opus")` 加载：**PATH 注入必须在 `import opuslib_next` 之前**。

## 3. VAD / 录音模式

- **auto 模式**：VAD 靠 `time.time()` 真实时间判断静音时长（`min_silence_duration_ms`，默认 200ms）。
  - 测试时若瞬间发完所有帧，静音时长≈0，**不会触发识别**；必须按 60ms/帧实时发送。
  - VAD 触发识别后若再发 `listen stop`，残留帧可能被二次识别（如静音→"嗯"）并 abort 首轮。
- **manual 模式**：VAD 直接返回 True、缓存全部音频，`listen stop` 时**一次性识别**。
  - 自动化测试优先用 manual，只触发一次，干净可靠。
- 协议：`listen start`（带 mode）→ 二进制 opus 帧 → `listen stop`。

## 4. 配置机制

- `data/.config.yaml` 与默认 `config.yaml` 走**深合并**，custom 优先；缺该文件会 FileNotFoundError。
- `initialize_modules` 只初始化 TTS/LLM/Intent/Memory/VAD/ASR，**不初始化 VLLM**（视觉懒加载），不影响启动。
- 认证可 `server.auth.enabled=false`，但**连接必须带 device-id**（header `device-id` 或 `?device-id=`），否则收到欢迎语后被关。
- 该版本回复文本随 `tts sentence_start` 携带，**无独立 type=llm 消息**；统计回复需收集 sentence_start。
- 人设：顶层 `prompt`（注入 `agent-base-prompt.txt` 的 `{{base_prompt}}`）；语言取 TTS 配置的 language，默认中文。

## 5. 网络与下载

- 大仓库 GitHub 直连易卡死，克隆走镜像：`https://ghfast.top/https://github.com/<owner>/<repo>.git`。
- edge-tts 偶发 `403 Sec-MS-GEC`（token 版本/网络问题）；测试 ASR 可改用 SenseVoice 自带 `test_wavs/*.wav`，不依赖联网。
- SenseVoice 自带 `test_wavs/zh.wav` 内容为“开放时间早上九点至下午五点”（非学生自我介绍）。

## 6. 本地模型链路（已验证）

- VAD=SileroVAD（`D:/models/snakers4_silero-vad`）
- ASR=SherpaASR / SenseVoice（`D:/models/sherpa-onnx-sense-voice-...-int8`）
- LLM=Ollama（`qwen3.5:9b-q4km`，`http://localhost:11434`）
- TTS=EdgeTTS（`zh-CN-XiaoxiaoNeural`）
- Memory=mem_local_short（总结走本地 Ollama）
- 结果：文本链路 169 帧 opus、音频链路 116 帧 opus，均通过。
