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

## 7. Windows 音频设备去重 / 采样率 / 蓝牙（M3 真机实测，确定性结论）

- **同一物理设备在 4 种 hostapi 下重复枚举**：MME / DirectSound / WASAPI / WDM-KS，
  不做去重时扬声器下拉会出现 5 项（扬声器 Realtek、Speakers 1/2、蓝牙 HFP、耳机()）。
- **采样率硬卡点**：
  - WASAPI / WDM-KS **不做任意采样率转换**：48k（ch1/ch2）能打开，
    **固定 24k/16k mono 报 `Invalid sample rate`**；WDM-KS 还报 `Blocking API not supported`（必须 callback）。
  - MME / DirectSound **支持自动重采样**：固定 16k/24k mono、固定 blocksize（960/1440）均可打开。
  - 结论：**固定 16k/24k 必须走 DirectSound/MME**；归并优先级 DirectSound=4 > MME=3 > WASAPI=1 > WDM-KS=0，只留 prio≥3。
- **蓝牙耳机（vivo TWS 5）**：
  - HFP（hands-free，`bthhfenum.sys`）= 16k 窄带 + 强降噪，"喂喂喂"被识别成 "BI REALLY"；
    干净设备名从 HFP 驱动路径正则提取。
  - A2DP（`耳机()`，WDM-KS 2ch/48k）音质好但**不能固定 16k**。
  - **最佳组合：麦克风用内置 Realtek 阵列，蓝牙只用来听（保持 A2DP）**。
  - 蓝牙层"已连接"但 AudioEndpoint 状态 Unknown（A2DP 常被手机占用）：
    WDM-KS 打开报 `GLE=0x48F (ERROR_DEVICE_NOT_CONNECTED)`，软件给 `bt_hint` 提示。
- 前端 HFP↔A2DP 联动约束：麦克风选 HFP→扬声器跟切 HFP；扬声器选 A2DP→麦克风若为 HFP 强制回本地。

## 8. 实时对讲"识别到自己播放的声音"（自激回声）修复（M3 真机问题）

### 现象

DeskWork 通话中，小D 说完一句后会**把自己刚播出的声音再识别一遍**：回复被莫名
截断、出现"五""我"这类碎片识别、自问自答越聊越乱。

### 根因（两端叠加）

- **客户端**（`localdesk-agent/src/localdesk/realtime_voice.py`）：麦与扬声器同时
  开、全双工上行，麦克风直接拾取外放；播放期间麦克风帧照常上行，无任何回声抑制。
- **网关**（`gateway/session.py`）：响应播放中收到"有人说话"就按打断处理
  （取消回复 → 把刚播出的语音当用户输入再识别），把回声放大成闭环。

### 修复

1. **客户端"播放期回声门"（主修复）**：本机还有下行音频在播（含声卡 latency +
   150ms 尾音）时，麦克风帧一律不上行；播完自动恢复 → 半双工对讲。
   - 播放记账：`_out_pending`（已下发未进声卡字节数）+ `_drain_ts` +
     `out_stream.latency`；离屏测试用 `virtual_playback`（队列模型，不依赖声卡）。
2. **可选插话**（`allow_barge_in`，默认关，本次已在 DeskWork config.json 打开）：
   判定用**播放参考电平**（客户端知道自己正在放多大声）而不是只看麦克风：
   - 参考 `_down_level()`＝下行 PCM 电平的峰值保持（TAU 0.8s）；**播放期不衰减**
     （TTS 一句会成批下发，扬声器还在放缓冲时参考不能掉，否则门槛塌到回声之下）。
   - 门槛 `= 衰减估计 × 参考 × 3`（约高于回声 +10dB），衰减估计**只升不降**
     （宁高勿低：门槛偏高只是少打断，偏低会把回声当人话）；下限 `BARGE_FLOOR=300`。
   - 人声需连续 5 帧（300ms）超阈才算插话；命中才 `abort` + 重新 `listen` +
     补发开口起始帧；打断后 0.5s 丢弃在途下行、且要等新播放内容到达才允许再次判定
     （防止连环误触发）。
   - 实测：纯回声 20s 无一次误判（旧能量法会误判 2~8 次）；受控 5 倍人声可正常打断。
3. **网关自回声兜底**（`_is_self_echo` + `[echo-guard]` 日志）：识别文本与最近
   8s 内播报过的句子归一化比对（长文本互含 / 相似度 ≥0.8）→ 丢弃。
   阈值偏严（≥4 字、≥6 字才做包含判定），避免误丢用户"重复一句"。
   已知边界：像"收到"这种 2 字碎片仍可能漏过。
4. **`abort` 语义修正**：auto 模式下 abort 只表示"别再说了"，客户端仍在推流，
   必须继续采集（原实现置 `_collecting=False`，打断后用户再说话会被静默丢弃）。

### 验证（可复跑）

- `localdesk-agent/tests/test_voice_echo.py`：假网关 + 假麦克风，无需模型。
  播放期上行 0 帧；播完 1s 内恢复上行 ≈16 帧（16.7 帧/s）；插话 → abort + listen start。
- `tests/test_echo_loop.py`：真网关 + 真 ASR/LLM/TTS，假麦克风带"虚拟播放头"
  （按 60ms/帧把下行当外放回灌，模拟外放拾音）。
  - A 默认半双工：STT=1、回复=1，166 帧回灌全部被门挡下 ✅
  - C 允许插话：STT=1、回复=1、本地插话 0 次（纯回声不误触发）✅
  - B 客户端门关（旧行为）：网关兜底拦下自回声 ✅
- 回归：客户端↔网关真实链路（ASR / 回复 / 下行 182 帧 / assistant_stop）通过；
  `test_bargein.py` 通过。注意 EdgeTTS 偶发卡住会让 barge 用例假失败（复跑即过），
  与代码无关。

### 开关与调参

- **DeskWork `config.json` → `voice` 段**（已显式写入，改完重启 DeskWork 生效）：
  - `echo_guard: true` —— 播放期回声门（关掉＝回到"会听到自己"的旧行为）
  - `allow_barge_in: true` —— true 可随时打断；false 为最稳的半双工对讲
- 网关 `GatewayConfig`：`echo_dedup`（默认 true）、`echo_dedup_window_s`（默认 8s）。
- 客户端常量（`realtime_voice.py`）：`POST_PLAY_MS`、`BARGE_FACTOR`、`BARGE_FRAMES`、
  `BARGE_FLOOR`、`DOWN_PEAK_TAU`、`BARGE_ATT_INIT`。
- 诊断字段：`client.echo_dropped`（被门挡下的上行帧数）、`client.barge_count`、
  `client.last_gate`（最近一次判定的 mic/play/att/thr 快照）。



# M3.1 修复：音色被"自动换掉" + 语音打断永远打不断

两个用户可见故障，根因与修法如下（2026-10-15）。

## 1. 音色会自己变（不是打断方式引起的）

- **切「打断方式」与 TTS 零耦合**：`api.set_interrupt` 只写 `cfg["interrupt_mode"]`
  + `client.set_allow_barge_in()`（一个 bool），网关收不到任何消息。用户感受到的
  "切完打断方式音色变了"是时间上的巧合。
- 真正的两个来源：
  1. **在线合成偶发失败 → 立刻降级本地 + 60s 冷却**（`gateway.log` 里
     `[tts] 在线合成失败(No audio was received…)`）。`NoAudioReceived` 是 edge-tts
     在"ws 连上但一条 audio 都没收到"时抛的，属服务侧偶发；**实测连续 3 次中途
     aclose 也不会把在线引擎弄坏**，与 abort/打断无关。60s 后自动切回在线 →
     音色来回变两次。
  2. **降级不映射音色**：`_local_voice("zh-CN-*")` 原来返回空 → 系统默认（惠惠），
     实测强制引擎 local 时晓晓/云希/云扬输出特征完全一致（73 包/4.38s/rms 0.0516/
     质心 2120/f0 210）—— 看起来"所有音色都变成同一个"。
  3. 另有每轮 LLM 语气微调（`_decide_style` → rate/pitch/volume → edge-tts 的
     ±30% 语速 / ±30Hz 音高）叠加听感差异。
- **修法**（`gateway/providers/tts_router.py` + `gateway/session.py` + 前端）：
  - 首包失败先 **重试 2 次** 再降级（`_TTS_ATTEMPTS`），一次网络抖动不再换嗓音；
  - `degraded` / `last_error` / `local_label` 三个状态位，会话在**状态跳变时**发
    `protocol.notice(...)`（"在线音色暂时不可用，已临时改用本地音色「Huihui·简体」，
    稍后自动切回" / "在线神经音色已恢复"），客户端 `notice` 事件 → 界面系统气泡；
  - 降级目标按语言映射：`zh-*`→Huihui、`en-*`→Zira（原来英文音色也会落到中文嗓子）；
  - `set_voice` / `set_engine` 会清掉在线冷却（换音色＝重新给一次机会）。

## 2. 语音打断打不断：判据单位是错的 + 门槛自锁

`app/voice_client.py::_gate_frame` 原判据 `thr = max(att*play*4, 500)`：
- **单位不一致**：麦克风电平乘了 AGC 增益，回声项没乘 → 等效把门槛除以 3~6；
- **`_att` 只升不降且用被判成回声的帧更新**：用户说话恰好被判成回声，越用力说
  门槛越高（mic 1500→att 0.51→thr 5995；3000→att 1.00→thr 11796），正反馈自锁；
- 实测（真实 TTS 下行 rms≈0.09 满量程 → play≈2949）：门槛 4129，而用户麦克风说话
  原始 rms 只有 100~600 → 差 7~40 倍，**数学上打不断**。
- 网关侧 VAD 打断本身可用（`tests/test_bargein.py` 过），但回声门播放期不上行，
  网关拿不到帧 → 只剩这条死掉的本地判据。

**修法**（全部改用**原始麦克风电平**，同尺度可解释）：
- 回声参考 = `att × play`，`att`（扬声器→麦克风耦合）用**滑窗峰值 + 每帧最多涨 2%**
  估计：上界保证"纯回声永不越界"，涨速限制让混进来的人声污染最多 +2%/0.36s
  （旧的"立即取最大值"才会自锁）；下降立即跟随（调低音量/戴耳机马上能打断）。
- 门槛 = `max(回声上界 × 1.15, 用户平时说话电平 × 0.45, 150)`；`mic_ref` 由上行
  非播放期的原始 RMS 峰值保持得到（戴耳机没有回声时靠它决定门槛）。
- 疑似回声（≤1.6×回声估计）的帧照样喂给估计器 → 若门槛被估低，它自己会跟上、
  连续超阈自然断掉；真正的人声远高于回声，不会被喂进去污染估计。
- 门槛长期高于"你说多大声"（>1.8×ref 持续 3s）→ 主动提示"语音打断在当前环境
  不可用：本机外放回声比说话还响，戴耳机或调低音量"。外放+小声说话本来就没有
  可靠的能量判据（无真 AEC），宁可明确告知，也不静默失败。
- 新增诊断日志（语音模式每 2s 一行）：`[rt][barge] mic=… echo=… att=… ref=…
  thr=…`，可直接判读"差多少"。
- 配置迁移：`barge_factor` 4.0→1.2、`barge_frames` 8→6，`acfg.load()` 里对旧值
  一次性改写（旧语义×播放电平，留着必然打不断）。客户端常量 `BARGE_FACTOR=1.15`、
  `app_config.DEFAULTS=1.2`；构造器把配置值夹到 `≥1.2`，所以装出来的实际倍数是 1.2。

### 实测（`tests/test_voice_switch.py`，无需网关与大模型）

| 场景 | 真实耦合 | 门槛 | 结果 |
|---|---|---|---|
| 纯回声连放 8 帧 | 0.20 | — | 未触发 ✅ |
| 回声突然响 30%（新句子更响） | 0.20 | 自动跟上 | 未触发 ✅ |
| 耳机 / 近讲，说话 RMS 200~1500 | 0.02 | 180 | 全部可打断 ✅ |
| 外放，说话 RMS 800 / 1500 | 0.20 | ~933 | 可打断 ✅ |
| 外放，说话 RMS 200 / 400 | 0.20 | ~739/845 | 打不断（人声与回声同量级，靠提示告知） |

旧判据在以上**所有**场景都是"打不断"（thr=4129）。

### 已知边界

- 外放时人声必须明显盖过回声才能判出来（无 AEC 的能量法天花板）。耳机/近讲最稳。
- 按钮 / Esc 打断（`interrupt()`）不受任何阈值影响，任何模式都 100% 生效。

## 6. 打包交付与「改完真的进包了吗」的核验（2026-10-15）

### 6.1 构建命令与 `--distpath` 的坑

```bash
XIAOD_BUILD_MODE=onedir C:/Python314/python.exe -m PyInstaller app/build/xiaod.spec \
  --noconfirm --distpath dist_fix --workpath app/build/work_fix
```

- `--distpath` 指向**已存在**的输出目录时，PyInstaller 会先清空它（954 个文件）→ 撞上
  环境的 safe-delete 批量删除护栏（阈值 50）→ 构建直接失败。**必须换新目录名**，
  或先手工把旧目录搬走。
- onedir 产物：`dist_fix/小D智能助手/小D智能助手.exe`（约 14.3MB）+ `_internal/`（约 159MB）。

### 6.2 `tools/verify_pack.py` —— 从产物里核验符号（推荐做法）

时间戳/体积都不足以证明"新代码进包了"。用 PyInstaller 自己的归档读取器直接把 PYZ 里的
**code 对象**取出来，递归收集 `co_names / co_varnames / co_freevars / co_consts`，
再按符号名比对：

```bash
python tools/verify_pack.py "dist_fix/小D智能助手/小D智能助手.exe"
```

- `CArchiveReader(exe).toc` 里 endswith `.pyz` 的那项即 PYZ（本工程叫 `PYZ.pyz`）；
  `car.open_embedded_archive(name)` → `ZlibArchiveReader`。
- ⚠️ `pyz.extract(mod)` 返回的是**已反序列化的 code 对象**，不是 `bytes`——
  对它做 `bytes()` 会 `TypeError: cannot convert 'code' object to bytes`。
- 本工程被核验的符号：`gateway.session`(`_notify_tts_state`/`notice`/`_tts_degraded`)、
  `gateway.protocol`(`notice`)、`gateway.providers.tts_router`(`local_label`/
  `last_error`/`degraded`/`local:`)、`app.voice_client`(`BARGE_REF_RATIO`/
  `BARGE_ECHO_TOL`/`BARGE_UNREACH_S`/`_note_unreachable`)。

### 6.3 原地升级运行目录（保住用户数据）

`dist/小D智能助手-极速版/` 里混着**用户数据**：`config.json`、`gateway.json`、
`.tmp/gw_overrides.json`、`assistant.log`、`gateway.log`、`runtime.log`、`transcripts/`。
**绝不能整目录替换**。

先比对两棵 `_internal`，只覆盖真正变化的文件：

```bash
diff -rq "dist/小D智能助手-极速版/_internal" "dist_fix/小D智能助手/_internal"
```

本次结果：文件清单 951 vs 951 **完全一致**，只有 3 个内容不同 —— `app/ui/app.js`、
`app/ui/index.html`（前端改动）、`base_library.zip`。所以升级 = 覆盖 exe + 这 3 个文件，
用户设置与对话记录零损失。（`gateway/*`、`app/voice_client.py` 都在 exe 内的 PYZ 里，
所以 exe 必须一起换。）

### 6.4 冻结版 windowed exe 的日志去向（别被 0 字节骗了）

- `os.environ.setdefault("PYTHONUNBUFFERED", "1")` 在 `main.py` 里、解释器启动之后才执行，
  **对已经创建好的 `sys.stdout` 没有任何效果**。
- 直接 `./xxx.exe --gateway > gw.log 2>&1`：stdout 是继承来的重定向句柄（非 tty）→
  Python 用**块缓冲**（8192）→ 启动横幅那几行 `print`（无 `flush=True`）一直在缓冲里；
  `kill`(SIGTERM) 不会走 Python 的退出刷盘 → 日志文件**看起来是空的**。
- `session`/`tts` 的日志都带 `flush=True`，所以只要真有客户端连上来，立刻能看到。
- 结论：这不是缺陷。想现场看日志，就照 GUI 的做法由父进程把子进程 stdout 重定向到
  `gateway.log`（`app/gateway_ctl.py` 就是这么做的）。
- 另外网关有 `XIAOD_PARENT_PID` 看门狗（宿主没了就 `os._exit(0)`）→ 用 `(… &)` 脱离 shell
  拉起的网关会在**发起命令结束的瞬间自杀**，冒烟测试必须让父进程/终端全程存活
  （用 `run_in_background` 或同一条命令里 `sleep`）。表现为"上一秒端口在听、下一秒
  ConnectionRefused"。

### 6.5 打包版端到端冒烟（同一条命令内完成）

```bash
./dist/小D智能助手-极速版/小D智能助手.exe --gateway > .tmp/gw_probe.log 2>&1 &
sleep 10
# 带 device-id 头（缺了会被 1008 拒掉）、用新版 asyncio 客户端
python - <<'PY'
from websockets.asyncio.client import connect   # 13.1 里 additional_headers 只在新 API 有
...
PY
```

实测输出：`hello` → `{"type":"hello","session_id":…,"audio_params":{"format":"opus",
"sample_rate":24000…}}`；随后 `[session] TTS 音色切换: zh-CN-YunxiNeural`、
`[session] TTS 引擎: auto` 均落盘。

### 6.6 配置迁移的验证姿势

`app/app_config.CONFIG_PATH` 是 **import 时算好的模块级常量**。想对别处的 `config.json`
跑一遍 `load()`，光 monkeypatch `acfg.data_dir` 是**无效**的（不会生效，也不会写回），
必须直接改常量：`acfg.CONFIG_PATH = <path>`。

实测（拿用户真实 `config.json` 的副本跑）：`barge_factor 4.0 → 1.2`、
`barge_frames 8 → 6` 并写回文件；`mic_index / voice / prompt_name / auto_start_call`
等用户设置原样保留。

# M3.2 修复：界面选的音色没生效 + 结束聊天停不下来

两个都是 `app/voice_client.py` 里的**局部疏漏**，与 TTS/网关逻辑无关。

## 7.1 症状与根因

**症状 A**：「列表里显示的是女声，回复却是男声」；开机（会话刚开始）就是错的。
**根因**：`RealtimeVoiceClient.__init__` 收了 `voice` 参数，却写成

```python
self.voice = None           # ← 参数被丢掉了
```

`api.py` 明明传了 `voice=self.cfg.get("voice")`。于是 `_main()` 里

```python
if self.voice:                                  # 恒为假
    await ws.send(json.dumps({"type": "set_voice", "voice": self.voice}))
```

**永远不发**。网关只能用自己存着的音色：`.tmp/gw_overrides.json` 的 `tts_voice`
优先级高于 `gateway.json`，而那个文件可能来自很久以前的一次 `set_voice`
（任何人、任何客户端写进去都会永久生效）。界面 `config.json` 与网关实际音色
因此可以长期背离，且**没有任何报错**。

附带修掉两处同类隐患：
- `set_voice()`（界面切换音色）只发消息、**不更新 `self.voice`** → 重连后又回落。
- `api.py` 里 `barge_factor` 的兜底默认值还是旧语义的 `4.0`（夹到 4.0 就等于
  门槛又变回打不断），已改 `1.2 / 6`，与 `app_config.DEFAULTS` 对齐。

**症状 B**：「较长的语音回复，朗读过程中点结束聊天不会停止。」
**根因**：`stop()` 只做 `self._stop_flag.set()`。而 `_main()` 是

```python
await asyncio.gather(uplink(), downlink())   # downlink 是 `async for raw in ws`
```

`downlink` 只在**收到下一条消息之后**才检查 `_stop_flag`。当服务端这一句已经
发完、或正在生成下一句（没有数据在途）时，它就是**阻塞在 `recv()` 上**：
`gather` 不返回 → `finally` 里的 `out_stream.close()` 执行不到 → 声卡回调继续
按实时速度把 `out_q` 里已经收到的音频放完。长回复的音频可以整段堆在队列里
（实测 30 个包 = 86400 字节全在队列中），所以"点了结束还在念好几秒"。

## 7.2 修法

1. `self.voice = voice or None`（构造器）；`set_voice()` 里也写 `self.voice`。
   → 每次会话开局都会把界面的选择下发，界面成为唯一权威，网关存的旧值被覆盖。
2. `stop()` 不再只置标志位：
   - 立刻 `_hard_mute_playback()`（清空播放队列 + 记账清零）；
   - `run_coroutine_threadsafe(self._do_stop(), loop)` → 先发 `abort`（让网关别再
     合成）再 `ws.close()`，阻塞的 `recv()` 得以结束，`gather` 立即返回。
3. 声卡输出回调 `out_cb` 顶部加守卫：`_stop_flag` 已置就整帧写 0 并返回。
   这是"结束聊天立刻不响"的最后一道保证 —— 回调在独立线程上，可能比事件循环
   更晚察觉停止，只靠关流会在 `gather` 返回前继续放。

## 7.3 回归测试（`tests/test_stop_and_voice.py`，13 项）

假网关（`websockets.asyncio.server.serve` 绑 0 端口）+ 假声卡流
（monkeypatch `voice_client._open_stream` 拿到真实闭包回调），不需要真设备：

- 构造保留 `voice`、`set_voice` 更新 `self.voice`、未指定时保持 `None`；
- 真会话里开局消息序列确实含 `{"type":"set_voice","voice":...}`；
- 停止**前**回调输出真实音频（对照）→ 停止**后**回调整帧全 0；
- `stop()` 后连接被收掉、`abort` 已发出、客户端线程退出。

实测输出对照：修好后开局消息 =
`hello → listen → set_voice → set_search → set_tts_engine`（此前**没有** set_voice）；
停止前队列积压 86400 字节、停止后回调非零字节数 = 0。

---

# 8. 全面优化一轮（2026-10-15 ~ 16）

用户一次提了 9 个问题：「打断自激活 / 音色自己变 / 打断有时打不断 / 混乱时
降级本地 / 台湾女声生硬 / 识别文本没标点 / 人设太少 / 要全屏 / 虚拟形象不真人
且嘴只适配默认音色」。逐条修完，记录关键结论。

## 8.1 联网搜索"开了也没用"

三重根因，缺一不可：

1. **模型把提示短语当关键词输出**——日志里关键词是「用户当前位置」。`_decide_search`
   增加 `_clean_query()`：包含「用户/当前位置/关键词/小d/助手」或长度 >20 一律拒绝。
2. **Bing 搜天气返回百科/政府网页**，模型拿不到实时数据只能编造（实测编出
   "10到19度"）。加 **wttr.in 快路径**：`_looks_weather()` 命中天气词就走
   `wttr.in/<city>?format=j1`，直接给出"现在 22°C，体感 19°C，霾，湿度 21%"。
   城市名从查询里剔停用词后取 2~10 字连续中文。
3. **没有禁编造指令**。`as_context()` 结尾加"回答规则"：查不到必须明说，
   禁止编造数字/时间/人名/价格。

## 8.2 音色自己变 / 降级音色不符 / 本地音色生硬

- **固定 60s 冷却** → 网络一抖就切本地、60s 后再切回，来回翻。改成
  **指数退避** 45→90→180→360→600s 封顶，且 `stream()` 内重试 3 次（间隔 0.35s），
  连续成功 3 句才重置计数。
- **降级音色乱配**：`_LOCAL_PREF` 按（语言变体, 性别）打分排序
  （同变体 -100 / 同语种 -50 / 同性别 -20）；`_sapi_gender()` 从音色描述里
  认女声/男声关键词。台湾女声降级时优先找 zh-TW 女声，找不到再退简体女声。
- **生硬**：`tts_sapi` 加 crc32 确定性韵律微扰（±3% 语速 / ±1.5% 音高，同一句
  每次一样，不会听起来飘）+ ×1.06 基础提速 + RMS 归一到 2800（峰值 >32000 回缩
  防削波）。本机只有 2 个 SAPI 音色（Huihui 简体女 / Zira 英语女），用户听到的
  "生硬台湾女声"就是降级后的惠惠。

## 8.3 语音打断：自激活 + 打不断（判据重写）

| 参数 | 旧 | 新 | 理由 |
|---|---|---|---|
| `BARGE_FACTOR` | 1.15 | **1.8** | 耦合 +30% 时纯回声就能连续超阈 → 自激活 |
| `BARGE_ECHO_TOL` | — | **2.2** | 必须**高于**触发倍数，突变帧才喂得进估计器自查自纠 |
| `BARGE_FRAMES` | 6 | **8** | 8 帧（≈480ms）是"自查自纠"的时间预算 |
| `BARGE_REF_RATIO` | 0.45 | **0.35** | 典型语音电平的 35% 作为参考门 |

- `_mic_ref` 原来是**历史最大值** → 大喊一句后半分钟内轻声说话全打不断。
  改成 **35 分位数**（窗口 900 帧自然老化）+ 0.5s 缓存。
- 句间间隙（播放电平低）**不喂**耦合估计器，避免把间隙噪声记成回声上界。
- 回归测试 `tests/test_barge_gate.py` 22 项：纯回声/突增 30%/60%/100%/突降 50%
  都不触发；耳机 200~1500 全可打断；外放大声可打断小声不误触；大喊后正常音量
  仍可打断；嘈杂环境（底噪 90）持续噪声不触发。

## 8.4 ASR 标点恢复

SenseVoice 输出**完全不带标点**，但给 **token 级时间戳**。做法：

- 服务端采集期用 VAD 记录语音段起止（字节偏移），`transcribe(pcm, spans)`
  把 spans 传进去；前置静音裁剪时同步平移。
- **主路径**：`bisect` 把停顿中点对齐到最近的 token 边界，≥480ms 插句号、
  ≥220ms 插逗号。
- **兜底**：没有 token 时间戳时走能量包络自查（`_energy_spans`，
  <300ms 合并碎段、<120ms 段丢弃）+ 语音时长比例折算。
- 三个守卫：英文单词内部不插（`_is_ascii_word_pair`）、已有标点处跳过、
  紧跟逗号要插句号时"升级"而非叠加。英文语境用半角标点并吃掉多余空格
  （"BOY ，AND" → "BOY, AND"）。
- `_energy_spans` 的 20 分位底噪在**语音占比 >90%** 时会被抬成语音电平导致全段
  被拒——改用更低的 5 分位。
- 回归测试 `tests/test_asr_punct.py` 21 项；端到端实测
  「开饭时间早上九点至下午五点」+ 0.7s 停顿 +「两只小企鹅都有嘢食」
  → 正确补句号，识别耗时 0.15s。

## 8.5 虚拟形象（真人化 + 按音色切换 + 口型）

`app/ui/avatar.js` 重写成**参数化肖像生成器**：8 个角色（晓晓/云希/HSIAO-CHEN/
YUN-JHE/JENNY/MARK/知语/娜娜），各带肤色/发型/瞳色/服饰/配饰/腮红。

- 音色 → 角色：先查 `VOICE_MAP` 精确表，未命中按（性别, 语种）哈希到对应池；
  **语言未识别时默认 zh-CN**（原来会漂到英文角色）。
- **口型改成音频电平驱动**：原来只认 CSS 类（`.s-speaking` 的 keyframes），
  跟音色无关但也跟实际声音无关，看起来"只适配第一个音色"。现在订阅 `level`
  事件，`mouth-drive` 组按电平 `scale(0.78+0.5v, 0.12+1.12v)`，牙齿/舌头透明度
  同步。
- **level 事件加来源标记**：播放期麦克风回调仍在跑，只认 `src="play"` 的电平，
  否则用户说话会驱动小D的嘴。
- 无头渲染测试 `tests/test_avatar_render.js`（Node + 最小 DOM 垫片）覆盖角色
  映射、口型三档、状态文字。

## 8.6 交互与整体

- **人设预设 5 → 12**：新增哄睡轻语/吐槽搭子/东北老铁/英语外教/冷笑话大王/
  工作秘书/二次元萌妹，全部带"口语化、不用 markdown、回答简洁"约束。
- **双击主界面空白处全屏切换**：`api.toggle_fullscreen()` →
  `Window.toggle_fullscreen()`（pywebview 6.2.1）；`.stage` 上加
  `dblclick`，命中 button/select/textarea/input/a/label/.bubble 时不触发。
  pywebview 没有 `fullscreen` 只读属性，自己记账。
- **播放音量可调**（0.3~1.6）：外放太吵/耳机太轻时调，`_scale()` 在声卡回调里
  缩放 PCM；**口型看原始电平**，不受音量影响。
- **AGC 双向**：原来只放大不衰减，实测上行 RMS 能到 6480（削波伤识别）。
  现在 `level > target` 时往下压（下限 0.25），实测 6480 → 1581、峰值 6335 不削波。
- **失败不再静默**：`_run_response` 异常时发 `notice` 给界面
  （区分"模型服务连不上"/"合成失败"/"其他"）；`_fetch_models` 拿不到列表时
  返回"未检测到模型服务（请先启动 Ollama）"占位项，而不是空下拉。

## 8.7 验证

```
tests/test_barge_gate.py      22 项通过
tests/test_asr_punct.py       21 项通过
tests/test_avatar_render.js   全部通过
tests/smoke_gateway.py        端到端 SMOKE_PASS
   （识别「今天天气不错我们出去玩吧。」+ 156 个下行音频包）
```

冒烟脚本固定在 `--config` 指向的临时目录（端口 8137）上跑，**绝不连用户真实
运行目录**，避免污染 `.tmp/gw_overrides.json`。

## 9. 固件"自激活"（设备自己把自己唤醒）

### 9.1 根因：这块板子没有回声参考通道

> **2026-10-16 更新**：这一节描述的是当时的硬件事实。§11.1 已经用**软件造的
> 数字域回声参考**把这个前提补上了（`CONFIG_AEC_DIGITAL_REFERENCE=y` +
> `USE_DEVICE_AEC=y`），设备侧 AEC 现在是真的在跑。下面三道闸仍然保留，
> 它们是"AI 不可能 100% 干净"的兜底。

- 板型 `bread-compact-wifi-lcd` → `GetAudioCodec()` 返回
  `NoAudioCodecSimplex/Duplex`，**都不设置 `input_reference_`**（基类
  `main/audio/audio_codec.h` 里默认 `false`，第 42/57 行）。
- 后果链：
  1. `afe_audio_engine.cc` 里 `afe_config->aec_init = codec_->input_reference()`
     → **false**，AFE 根本没有 AEC；
  2. `aec_mode_` 编译期由 `CONFIG_USE_DEVICE_AEC` / `CONFIG_USE_SERVER_AEC` 决定，
     两个都没开 → `kAecOff`；
  3. `GetDefaultListeningMode()` = `kListeningModeAutoStop`；
  4. 上游 `HandleStateChangedEvent()` 在 **Speaking** 态写的是
     `audio_service_.EnableWakeWordDetection(audio_service_.IsAfeWakeWord())`
     —— 播放期间唤醒词**照样开着**，等的是硬件 AEC 把喇叭声消掉。
     我们没有 AEC，于是设备"听见自己"：回复把自己叫醒 → 又一轮回复 → 又醒来。
- 所以这不是"调参问题"，是**前提不成立**。官方没有单独的"防自激活模块"，
  它的方案就是「有参考通道的硬件 AEC + 播放期只允许 AFE 唤醒 + 单麦用
  DET_MODE_95」。我们能抄的是后两条。

### 9.2 我们加的三道闸（`CONFIG_WAKE_WORD_ANTI_SELF_ACTIVATION`，默认 y）

| 闸 | 位置 | 作用 |
|---|---|---|
| 播放期硬闸 | `CanDetectWakeWordDuringPlayback()` | 只有「设备侧 AEC **且** codec 真的有参考通道」才允许播放期开唤醒词；否则整个播放期关掉。默认 aec_mode_=kAecOff → 播放期唤醒词全关 |
| 播放后冷却 | `ScheduleWakeWordRearm()` + `wake_word_rearm_timer_` | Speaking/Notifying → Idle 不立即开唤醒词，等 `CONFIG_WAKE_WORD_REARM_DELAY_MS`(默认 800ms) 让喇叭尾音/混响散掉 |
| 唤醒不应期 | `HandleWakeWordDetectedEvent()` 开头 | Idle 下距上次唤醒 < `CONFIG_WAKE_WORD_REFRACTORY_MS`(默认 1500ms) 的命中直接丢弃，并按冷却重挂，一次误命中不会滚成连环唤醒 |

外加 AFE 侧：单麦输入时 `afe_config->wakenet_mode = DET_MODE_95`
（`CONFIG_WAKE_WORD_DET_MODE_95`，默认 y）。90→95 是提高判定分数，代价是
轻声唤醒略难，收益是噪声/音乐/回声误命中大幅下降。

**代价**：无 AEC 板子上「语音打断」（播放中说唤醒词打断）不可用，改用 BOOT 键
或上位机「打断」按钮（`HandleToggleChatEvent` → `AbortSpeaking`，不依赖唤醒词）。
要做语音打断就得给板子接回采（参考通道）再开 `USE_DEVICE_AEC`。

### 9.3 构建脚本的坑（`build_firmware.ps1`）

- **`idf.py build` 不会因为 `main/Kconfig.projbuild` 变了就重新生成
  `build/config/sdkconfig.h`**。新增的 `CONFIG_*` 在头文件里不存在 →
  所有 `#if CONFIG_XXX` 都按 0 编译 → **构建成功但新功能完全没生效**，
  而且没有任何警告。脚本现在固定先跑一次 `idf.py reconfigure`。
- 验证方法：改完 Kconfig 一定 `grep` 一下
  `build/config/sdkconfig.h`，确认符号真的出现了。

### 9.4 调参入口

```
idf.py menuconfig
  → XiaoZhi Assistant / Anti self-activation (local wake) hardening
      [*] Use the aggressive WakeNet mode (DET_MODE_95)
      [*] Keep the wake word off while speaking unless device-side AEC is on
      Delay before re-arming the wake word after playback (ms) ....... 800
      Minimum gap between two accepted wake words (ms) ................ 1500
```

误唤醒仍多 → 先把 rearm delay 提到 1200、refractory 提到 2500；唤醒不灵 →
先把 DET_MODE_95 关掉，再降 refractory。

## 10. 网关侧回声门与 AEC（2026-10-16）

### 10.1 为什么网关侧也要防

§9 修的是**设备自己把自己唤醒**（设备侧唤醒词 / 本地命令窗口）。但设备还有
第二条把回声送出来的路：**上行音频**。它在播报时麦克风照样开着 —— 于是自己
的回复被 Opus 编码后送到了网关，网关的 VAD 一判"有人说话"就 barge-in / 断句
→ 送 ASR → 生成新一轮回复 → 再播 → 循环。

三层防线（本项目的约定：**客户端是第一责任方，网关是兜底**，见 §8）：

| 层 | 在哪 | 做什么 |
| --- | --- | --- |
| 客户端 `echo_guard` | `app/voice_client.py` | 播放期不上行（本机麦克风场景的主防线） |
| **网关硬闸门** | `gateway/session.py::_on_gated` | 播放期/尾音期**不跑 VAD、不累积、不切 listening、不触发 ASR** |
| **网关 AEC** | `gateway/audio/aec.py` | 用自己下发的 TTS 当参考，把回声从上行里减掉 |
| 文本级兜底 | `session._is_self_echo` | 识别结果与刚播报过的句子高度重合 → 丢弃（§8 老逻辑，保留） |

ESP32 设备**没有**客户端 `echo_guard` 那一层，所以第二三层就是它的主防线。

### 10.2 硬闸门（默认开，确定性收益）

- 判据：`state == "speaking"` **或** `now < _gate_until`（尾音冷却窗）。
- 闸门期**唯一**允许做的事：如果 AEC 在位，用**去回声后**的信号跑 VAD 判
  语音打断（`echo_gate_barge`，默认开）。AEC 不在位时纯丢弃，一行 VAD 都不跑。
- 冷却窗由 `tts_stop` 打开（`echo_gate_cooldown_ms`，默认 700ms）。它存在的
  理由：**服务端认为"播完了"≠ 设备真的播完了**，中间还隔着设备端 Opus
  解码/播放队列 + 扬声器到麦克风的空气延迟 + 房间混响。
- 冷却窗的**清除**条件只有三个（否则会吞掉用户的第一句）：`abort`、
  手动 `listen start`、AEC 确认真打断。
- 闸门放行的**第一帧**必须 `vad.reset_sentence()`：Silero 的 `ended`/
  `have_voice` 是粘性的，压着不放会让新回合第一帧就"结束"一个空句子，
  整句被门限丢弃（这正是 §8 里"说话没反应"的老坑，不要再踩）。

### 10.3 AEC：为什么这次能做，以及和 py-xiaozhi 的区别

py-xiaozhi 的 AEC 是**端侧**的（麦克风/扬声器与应用同机），它用
`set_stream_delay(40ms + N × 帧长)` 一个常量就能对齐。本项目麦克风在 ESP32
上、TTS 从 PC 经 WiFi 下发，回路延迟 = 设备播放缓冲 + 空传/混响 + 采集 +
网络，量级 200~500ms **且抖动**，设备端播放缓冲深度我们控制不了 —— 按常理
常量对齐在这里必然失败。

**但实测结论是：不需要精确对齐。** WebRTC APM 的 AEC3 自带延迟估计：
把 far 按**发送顺序连续**喂、near 按**到达顺序**配对（两者相差 300ms 也不
错位补偿），合成回声仍被压掉约 **70dB**（`tests/test_echo_gate.py` 的 F
用例断言 < -20dB，实测 -68.7dB）。`stream_delay_ms` 只作为收敛初值。

由此得到的实现约束：
- `EchoCanceller.feed_far()` **只能在 `_send()` 里、真正发出去的音频帧上**
  调用 —— 以"发出的字节"为准，不是"生成出来的字节"。
- **far 必须是解码后的 PCM**：下行帧是 opus 压缩包，`_send()` 里先经
  `DownlinkDecoder`（24k/1440）解码再喂（`aec.feed_far` 会把字节直接当
  int16 采样解释）。曾经直接喂 opus 字节，far 流与回声完全不相关，AEC
  形同虚设、靠硬闸门兜底（2026-10-17 修复，`tests/test_echo_gate.py`
  的 H/H2 用例按生产同构回归）。opus 解码按规范是确定性的：网关解码
  结果 == 设备解码结果 == 扬声器真实播放内容，far 参考因此成立。
- near 与 far 的**到达速率必须一致**（都是 60ms 一帧）。far 缓冲积压超过
  3s 说明设备断流太久，直接丢最老的（对应关系已经不存在了）。
- 采样率不一致：near 16k / far 24k（下行），内部用线性插值重采样
  （`_resample`，py-xiaozhi 同做法）。**不做** AGC（上行 AGC 会把残留回声
  一起放大）。
- **没有 far 在途时 `active` 为假、直接旁路**：省一次无谓处理，也避免 AEC
  在"纯近端"上的无谓失真。
- 每个会话一个实例（不同设备/摆位的回声路径不同，共用会互相污染自适应滤波器）。

依赖：`pywebrtc-audio`（pip，Windows x86_64 wheel，支持到 Python 3.14）。
**它是延迟 import 的**，PyInstaller 静态分析看不到 → `app/build/xiaod.spec`
的 `hiddenimports` 里必须显式写 `pywebrtc_audio` 并 `collect_all`，否则打包
后 AEC 会静默旁路（功能退化但不报错，最难查）。

### 10.4 配置项

| 键（`config.json` / 界面） | 默认 | 说明 |
| --- | --- | --- |
| `echo_gate` | `true` | 硬闸门总开关。关掉就回到"播放期照跑 VAD"的旧行为 |
| `echo_gate_cooldown_ms` | `700` | `tts_stop` 后再静默多久（等设备缓冲 + 混响） |
| `echo_cancel` | `auto` | `auto`=库可用就用（保留语音打断）/ `off`=只用硬闸门 |
| `echo_aec_delay_ms` | `180` | AEC 收敛初值，只当起点 |

界面：设置抽屉 →「语音网关」→ **回声消除** + **说完后的静默**。改完会
**自动重启网关**（参数在网关/会话启动时读入）；网关没在跑时只写配置、
不替你启动。

调参方向：
- 自激活/自问自答仍频繁 → `echo_gate_cooldown_ms` 调大（1000~1500）；
  仍不行就把 `echo_cancel` 改成 `off`（播放期彻底不判打断，只认按钮/Esc）。
- 打断不灵、第一句老被吃掉 → 冷却调小（300~500），确认 `echo_cancel=auto`。
- 日志里 `[echo] AEC 初始化失败…退化为纯闸门` → 库没装上/没进包。

### 10.5 回归

```bash
C:/Python314/python.exe tests/test_echo_gate.py      # ECHO_PASS 35/35
C:/Python314/python.exe tests/test_echo_config.py    # ECHOCFG_PASS 23/23
C:/Python314/python.exe tests/test_barge_gate.py     # 客户端侧打断判据（22 项）
# 打包产物必须单跑（延迟 import 的库不进包时是静默旁路）
C:/Python314/python.exe tools/smoke_echo_pack.py [exe 路径]   # SMOKE_PASS 5/5
```

`test_echo_gate.py` 用假件驱动（不连真机、不走 opus、不加载模型），
覆盖：闸门丢弃 / 冷却窗开与清 / 放行后能正常识别 / `echo_gate=false` 兼容
/ AEC 合成回声压制 + 语音保留 / 库不可用旁路 / **AEC 在位时回声不误打断
且用户说话能打断**。

`tools/smoke_echo_pack.py` 是**唯一**能证明"打包后 AEC 真的在跑"的用例：
临时配置起 exe 的 `--gateway` → 等 `/healthz` ready → 带 `Device-Id` 头握手
建会话 → 在 `runtime.log` 里找 `[echo] AEC 就绪` 与 `_cleanup` 打出的
`available: True`。少了它，"库没进包"会以"功能悄悄退化"的形式上线。

⚠️ 读这些日志时要按 **GBK** 解（直启 exe 时 stdout 是控制台 locale；只有
经 `gateway_ctl` 拉起才是 UTF-8）。用固定 `utf-8` 解码会把断言变成"日志里
没有"，看着像功能没生效，白白查半天。

### 10.6 快照（2026-10-16 交付）

- 运行目录 `dist/小D智能助手-极速版/` 已原地升级。与新产物
  `dist_echo/小D智能助手/` 的 `_internal` 差异：`app/ui/app.js`、
  `app/ui/index.html`、`base_library.zip`、`vcruntime140*.dll` 覆盖，
  **新增** `pywebrtc_audio/` + `pywebrtc_audio-0.2.0.dist-info/`。
  用户的 `config.json` / `gateway.json` / `transcripts/` 未动。
- `tools/verify_pack.py` 的符号表已补 `gateway.audio.aec` 与 `echo_*`
  相关符号（`_take_far` 属 aec 模块，别填进 `gateway.session`）。

### 10.7 已知边界

- AEC 只对"网关自己下发的音频"有效（py-xiaozhi 也这样）。设备上同时放
  音乐/别的 App 的声音，网关这边无法消除。
- ESP32 单麦 + 小喇叭的物理耦合很强，回声非线性失真大，实际抑制量会低于
  合成测试的 70dB。真正稳的做法还是给板子接回采通道（`input_reference()`）
  开设备侧 AEC —— §9 已说明为什么当前硬件做不到。
- 语音打断在 AEC 模式下靠 AEC 输出判 VAD，AEC 没收敛好的头几帧可能漏判；
  按钮 / Esc 打断（`interrupt()`）不受影响，任何时候都 100% 有效。

---

## 11. 固件三项改造（2026-10-16 夜）

这一轮只改固件，源码在 `firmware/xiaozhi-esp32`。三件事互不相干，但都在
`build_firmware.ps1` 一次构建里出片。

### 11.1 设备侧 AEC：给面包板数字域回声参考（替代 §9 的"无参考"结论）

§9 的结论是"`bread-compact-wifi-lcd` 是纯 simplex I2S 接线（独立麦克风总线 +
独立喇叭总线，没有 codec 回环），AFE 只拿到 `M`，AEC 无参考可减"。这一轮把
参考**在软件里造出来**，不再依赖硬件回采。

**改动**

| 文件 | 内容 |
| --- | --- |
| `main/audio/codecs/no_audio_codec.{h,cc}` | 新增 `NoAudioCodecSimplexWithReference`（继承 `NoAudioCodecSimplex`） |
| `main/Kconfig.projbuild` | 新增 `AEC_DIGITAL_REFERENCE`(默认 y) / `AEC_DIGITAL_REF_DELAY_MS`(默认 150) |
| `main/boards/bread-compact-wifi-lcd/compact_wifi_board_lcd.cc` | 开关打开时改用上面的类 |
| `sdkconfig` / `sdkconfig.defaults` | 三个开关（含 `USE_DEVICE_AEC`）全开 |

**原理**

`Write()` 在写 I2S 之前，把**后音量**的样本（`int32 >> 16`，与采集侧
`>>12 + clamp` 同标度）存进 12288 样本环形缓冲；`Read()` 读 160 个麦克风字之后，
按 24k→16k 线性插值取 `AEC_DIGITAL_REF_DELAY_MS` 之前的信号，**交错输出 `M,R`**。
于是 `input_reference()=true`、`input_channels()=2`，AFE 的 `input_format` 变成
`"MR"`、`aec_init=true`。

参考窗用**独立互斥锁**（不复用 `data_if_mutex_`），锁只护窗口计算，I2S 阻塞读写在
锁外——否则一次阻塞读就能把播放路径堵死。窗口锚点按写头自动重合，漂移超过
240 样本（一个 10ms 输出 DMA 帧）才重锚，播放暂停后也不会滞留在后面饿死 AEC。

**为什么光有参考还不够**：`USE_DEVICE_AEC` 的板型白名单原本**不含**
`BOARD_TYPE_BREAD_COMPACT_WIFI_LCD`，所以 `aec_mode_` 一直是 `kAecOff`，
`CanDetectWakeWordDuringPlayback()` 恒为 false → 播放期唤醒词被强制关掉 →
AFE 在播放时根本不跑 AEC。已把该板补进白名单并加
`default y if AEC_DIGITAL_REFERENCE`。补上之后：播放期唤醒词保持开启 →
AFE 带 AEC 运行 → 回声被消掉，同时**语音打断恢复可用**。

**算法验证（无需硬件）**：用 Python 复刻参考窗逻辑做仿真（4s、200Hz 正弦、
160 帧/块、150ms 延迟），`max|ref − ideal| = 0.000000`（信号幅度 20000），
锚点零漂移——插值、取窗、头位置都对。真机仍要按实际喇叭→麦克风回路微调
`AEC_DIGITAL_REF_DELAY_MS`（120–200ms 起步）。

**调参**：回声仍在 → 加大延迟或开 `AEC_DIGITAL_REFERENCE=n` 退回旧行为；
说话被削 → 减小延迟。

> 构建必须先 `reconfigure`（`build_firmware.ps1` 已内置）：只改 Kconfig 时
> `idf.py build` 不会重新生成 `build/config/sdkconfig.h`，新 `CONFIG_*` 全是
> 未定义 → `#if CONFIG_XXX` 静默按 0 编译 → **构建成功但功能没生效且无警告**。
> 改完 Kconfig 一定 `grep AEC_DIGITAL build/config/sdkconfig.h` 确认符号在。

### 11.2 语音切回官方服务器（在线时的网关命令看门狗）

**问题**：设备切到本地网关、本地模型不支持 tool call 时，"切换到官方服务器"
这句话会被上传给本地 LLM，而它调不动 `self.gateway.set_mode` → 语音切不回去，
只能进配网页。

**根因**：网关模式决定"谁来回答"，把这件事委托给**当前那个**后端是循环依赖。

**修法**：新增一层"在线看门狗"——**只要连着远端网关，也让设备自己匹配这 4 条**：

```
切换到官方服务器 / 切换到本地网关 / 恢复自动模式 / 你连的哪个服务器
```

- `OfflineIntentManager::GetGatewayWatchCommands()` 返回这 4 条的 pinyin 与
  对应的命令 id（`command_ids`），槽位→id 映射由调用方按值捕获，
  避免音频任务读到主任务正在改的成员。
- `Application::BeginGatewayCommandWatch()` 在**进入 `Listening` 时**武装
  （`EnableCommandRecognition`，复用 AFE 输出，和 ASR 上传共用一路音频），
  离开 `Listening` 时 `EndGatewayCommandWatch()` 拆掉。每次听音一回合一建一拆。
- 命中后 `HandleGatewayCommand()`：**掐上行**（`SetUpstreamEnabled(false)`）→
  丢发送队列 → 拆看门狗 → `SendAbortSpeaking` + `SendStopListening` →
  回 `Idle` → 走 `OfflineIntentManager::HandleCommand()` 播本地预录应答。
  所以那句话不会进网关的转写，也不会和它的回复抢喇叭。

**三条不能省的约束**

1. `esp_mn_commands_*` 是**进程级单例**：谁后注册谁覆盖。所以看门狗与离线命令
   窗口**必须互斥**——`BeginGatewayCommandWatch()` 里挡掉
   `offline_mode_ || offline_window_active_`，且 `EnterOfflineMode()` 第一件事就是
   `EndGatewayCommandWatch()`。漏了这步，离线窗口会以为 20 条都注册上了，
   实际表里只有 4 条。
2. `PlayOfflineOutcome()` 会把识别器**置为暂停**；看门狗下一轮武装前必须
   `SetCommandRecognitionPaused(false)`，否则新实例把音频吃进黑洞、永不命中。
3. 一条命令的应答音在**重启前**必须播完。`Reboot()` 原来立刻
   `audio_service_.Stop()`，把"即将重启"的语音拦腰砍断；现在先等播放队列排空
   （上限 3s，正常瞬间返回）再拆音频。

**顺带**：因为看门狗在所有模式下都武装，官方 → 本地 方向也一并可用了，
不再依赖官方云是否支持这个工具。

### 11.3 配网页常驻：局域网可访问（LAN config server）

**需求**：连上 WiFi 之后配网页要一直能改参数，而不是只有进 AP 配网模式才存在。

**做法**：复用 config-AP 的那套 web 服务器，但**不起 AP、不起 DNS**，站台链路
原样不动。

| 位置 | 改动 |
| --- | --- |
| `managed_components/78__esp-wifi-connect/wifi_configuration_ap.{h,cc}` | 新增 `StartLanMode()` / `StopLanMode()` / `EnsureScanRecords()` |
| `.../wifi_manager.{h,cc}` | 新增 `StartLanServer()` / `StopLanServer()` / `IsLanServerRunning()` / `GetLanWebUrl()` |
| `main/boards/common/wifi_board.cc` | `Connected` → 启；`Disconnected` / `ConfigModeEnter` → 停；并在屏幕上显示 URL |

要点：

- `StartLanMode()` **只**注册事件处理器 + `StartWebServer()`，不碰
  `esp_wifi_set_mode` / `esp_wifi_start`；`/advanced/config` 需要的 NVS 读取
  从 `StartAccessPoint()` 里搬了一份过来，否则读到的全是空值。
- `StopLanMode()` **只**拆 HTTP 服务器和事件处理器。绝不能用 `Stop()`——那里面
  有 `esp_wifi_stop()` 和 AP netif 销毁，会把站台一起打死。
- 端口 80 只能有一个主人：`WifiManager::StartConfigAp()` 在 `config_ap_->Start()`
  之前先停 LAN 服务器，否则 `httpd_start()` 返回 `ESP_ERR_INVALID_STATE`，
  而外面套着 `ESP_ERROR_CHECK` → **直接 abort 重启**。
- LAN 模式没有 AP 的 10s 周期扫描定时器，`/scan` 改成**首次按需阻断扫描**；
  `SCAN_DONE` 分支加 `if (self->scan_timer_)` 守卫。
- 阻断扫描**不能持 `mutex_`**：`esp_wifi_scan_start(..., true)` 返回前
  `SCAN_DONE` 已经派发，而它的处理函数要拿同一把锁 → 自锁死。
- 触发点从 WiFi 事件任务（`got IP` 回调）改成 `Application::Schedule()` 落到主
  循环：`httpd_start` + 20 个 handler 注册不该在事件任务栈上做；`httpd_stop()`
  最长可阻塞十几秒，同理不能在事件任务里调。
- **没有鉴权**（和 AP 配网页一样，本来就是开放热点 + 开放 HTTP）：能改 WiFi
  密码、网关地址。仅限可信局域网；要收紧就得加 token 或只在 AP 模式开放。
- 屏幕会弹一次 `配置页 <url>`（`Lang::Strings::ACCESS_VIA_BROWSER`），否则这个
  地址在设备上没有任何地方能查到。

> ⚠️ `managed_components/` 是托管目录：`idf.py update-dependencies` 或组件升级
> 会**覆盖上述补丁**（`wifi_configuration_ap.cc` / `wifi_manager.cc`）。
> 升级后必须重打，并重跑一次定点回归。


## 12. 能力对齐：参考小智补齐的功能（2026-10-17）

用户给出小智官方固件的八项能力清单，要求参照补齐。先盘点**本机能力与现状**，
再决定哪些直接做、哪些要挑模型。

### 12.1 本机承载实测（`tools/bench_tts.py`）

| 项目 | 实测 |
| --- | --- |
| CPU / 内存 | i7-14650HX 16C24T / 31.7 GB（可用 ~11.7 GB） |
| GPU | RTX 5060 Laptop（+ Intel UHD）；**onnxruntime 只有 CPU provider** |
| 本地神经 TTS（Kokoro，int8） | 模型加载 ~0.5s，**平均 RTF 0.346**（合成 1 秒语音 0.35 秒） |
| 磁盘 | C: 30.5 GB / D: 242 GB / E: 123 GB 可用 |

**结论：本机跑本地语音合成完全没问题**——RTF 0.35 意味着约 3 倍实时，流式
播报时合成永远追得上播放。本地 9B 大模型也扛得住（32 GB 内存）。

一个可优化点：`onnxruntime` 当前只有 `CPUExecutionProvider` / Azure，
没有 CUDA / DirectML。上 GPU 能再快一截，但要装 `onnxruntime-directml`
（与现有 onnxruntime 并存有冲突风险），收益不大，**暂不动**。

### 12.2 下载导入的两个本地语言模型

| 模型 | 大小 | 定位 | 实测首答 |
| --- | --- | --- | --- |
| `glm4:9b` | 5.5 GB | 对标小智"默认 ChatGLM"，中文对话强 | 8.2s（含冷加载） |
| `qwen3:4b` | 2.5 GB | 轻量快速，给"本地总结"压缩历史 / 意图判断 | 4.0s |

原本已有：`qwen3.5:9b-q4km`（默认）、`qwen3.5:9b-q5km`、`qwen3.5-9b-uncensored`、
`qwen2.5vl:7b`（视觉）、`reasoning:8b`、`coder:7b`、`bge-m3`（向量）。

**坑**：qwen3 系列即使传 `think=false`，偶尔仍把推理过程写进 `content`。
进摘要会污染后续上下文，进家居意图会让 JSON 解析抠错对象 →
`session._strip_think()` + `homeassistant._extract_json()` 都先剥
`<think>…</think>` 块再处理，并在测试里钉住。

### 12.3 已落地的能力

| 小智能力 | 本项目现状 | 本次改动 |
| --- | --- | --- |
| WebSocket 实时通信 | 早就有（`/xiaozhi/v1/`） | — |
| 唤醒 / 手动对话 / 实时打断 | 已有 | — |
| 长时间自动休眠 | **缺** | 新增 `idle_sleep_s`（默认 300s，0=关） |
| 意图识别 + 函数调用 | 只有联网搜索 | 新增 HomeAssistant 意图层 |
| 多语言（中粤英日韩） | SenseVoice 模型**已支持**，但没暴露 | 新增 `asr_language` + 界面 |
| 语言模型切换 | 只有 Ollama | 新增 OpenAI 兼容后端 + 6 家预设 |
| 语音合成 | EdgeTTS + 本地 Kokoro/SAPI | 已够用，实测 RTF 0.35 |
| 记忆模式三选 | **缺**（只有完整历史） | 新增 full / summary / none |

### 12.4 记忆模式（`memory_mode`）

- `full`（默认）：完整历史，`memory_max_turns=40` 截断最老的。
- `summary`：历史超过 `memory_summary_after`(12) 轮就把早期部分压成 ≤150 字
  摘要，只留最近 `memory_keep_recent`(4) 轮原文；摘要**并入人设文本**而不是
  新增一条 system —— Qwen/Ollama 的 chat 模板要求 system 只能在最前，插一条
  中间会直接 500（这个坑记忆里已经踩过一次）。
- `none`：只带人设 + 当前这句。

压缩用 `memory_summary_model`（空＝用对话模型）。压缩失败**必须保留原历史**，
不能让用户的话凭空消失。

### 12.5 语言模型后端（`llm_backend`）

`gateway/providers/llm_openai.py` 一个实现接住一整片 OpenAI 兼容端点：
DeepSeek / 阿里百炼（兼容模式）/ 智谱 GLM / Kimi / 硅基流动 / OpenAI / 自建聚合。

- 端点 = `base_url + /chat/completions`；用户填了完整路径也不重复拼接。
- SSE 流式；遇到 `content-type` 不是 `event-stream`（网关把 SSE 缓冲成整块）
  自动当普通 JSON 收，不至于"能连上但一个字不出"。
- **只取 `delta.content`，不取 `reasoning_content`** —— 否则会把模型的思考
  过程念出来。
- 选了在线但没填 base_url → **退回本地 Ollama 并打日志**，绝不因为配置写了
  一半就让网关起不来。
- `Api.test_llm()` 给界面一个"点一下就知道通不通"的按钮（填错 Key 是最常见的
  故障，等说话时才发现体验很差）。

### 12.6 智能家居（HomeAssistant）

`gateway/providers/homeassistant.py`，**默认关闭**，只配了地址+令牌才介入
（不给每句话都多一次模型调用）。

流程：LLM 抽结构化意图（domain/service/entity/value）→ `/api/states` 里模糊
匹配真实 `entity_id` → `/api/services/<domain>/<service>` → 结果变一句口语确认。
匹配不到设备就明说"没找到名叫 X 的设备"，**不假装执行成功**。

### 12.7 回归

```bash
C:/Python314/python.exe tests/test_features_xiaozhi.py   # FEATURE_PASS 69/69
C:/Python314/python.exe tests/test_echo_gate.py          # 35/35（未受影响）
C:/Python314/python.exe tests/test_echo_config.py        # 23/23
C:/Python314/python.exe tests/test_barge_gate.py         # 22/22
```

覆盖：三种记忆模式的上下文组装 / 摘要压缩与"压缩失败保留原历史" / 语种归一化 /
后端选择与端点拼接 / HA 的 JSON 抽取与设备匹配 / 休眠与唤醒 / 配置链路贯通。

### 12.8 模型存放目录：`D:/OllamaModels`（2026-10-17）

用户的 Ollama 模型**早就在 D 盘**，`OLLAMA_MODELS` 原本只有**用户级**变量；
本次补写了**系统级**，两级都是 `D:/OllamaModels`，避免别的运行上下文（服务 /
提权进程 / 新会话）读不到而退回 `C:/Users/Administrator/.ollama`。

- 验证方式：重启 Ollama 后看 `server.log` 里的 `OLLAMA_MODELS:D:/OllamaModels`
  与 `total blobs: N`，再跑一次真实推理看 `load_duration`。
- `C:/Users/Administrator/.ollama` 现在 **0 MB**（只剩空 cache 壳），模型一个
  都不在 C 盘。
- C 盘仍占位的**不是模型**：`AppData\Local\Ollama\updates_v2`（Ollama 自动更新
  缓存约 1.5 GB）和 `AppData\Local\Programs\Ollama`（程序本体约 2.8 GB）。
- **Ollama 进程无法由本环境常驻拉起**：`Start-Process` 起的进程会随工具进程组
  回收（约 6 秒后消失）；`schtasks` 与 WMI `Win32_Process.Create` 都被安全策略
  拦截。已改为写入 `HKCU\...\Run\Ollama` 开机自启 —— 用户开机后自动带起，
  不想要就在"任务管理器 → 启动"里禁用。
- **附带发现**：Ollama 0.34.3 这边 `library=CUDA ... RTX 5060 Laptop GPU,
  total 7.9 GiB` —— **Ollama 大模型是跑在 GPU 上的**（跟语音侧 onnxruntime
  只有 CPU provider 是两回事，别混为一谈）。
