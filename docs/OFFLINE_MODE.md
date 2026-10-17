# 固件离线交互模式（Firmware Offline Mode）

> 目标：**WiFi / 网关都连不上时，设备不再变砖**。保留唤醒词，把 CPU 侧的
> ESP-SR MultiNet 命令词识别当作"本地意图层"，直接调用与云端同一套 MCP 工具，
> 全程零网络。响应方式为**预录语音 + 屏幕文字**。
>
> 明确的边界：**不做自由聊天 / 问答**。MultiNet 是闭集识别器，能说的话必须
> 出现在下面的命令表里；表外的话只会回一句"这个指令我还不支持"。

---

## 1. 为什么需要它

改动前的行为链（xiaoZhi 上游逻辑）：

1. `WifiBoard::TryWifiConnect()` 60s 超时 → `StartWifiConfigMode()` → 起 AP 热点，
   同时 `EnableWakeWordDetection(false)`；
2. 未联网时 `Application::ActivationTask()` 永远不会跑，
   `Assets::Apply()`（把 `srmodels.bin` 里的 SR 模型加载进内存）也就永远不执行。

结果：**连不上 WiFi 的设备既听不到唤醒词，也做不了任何事**，只能靠手机连它的热点
重新配网。

改动后：超时不再强推配网，而是进入「离线就绪」——加载本地资源、唤醒词可用、
本地命令可执行、后台继续重连，网络恢复后自动回到在线链路。

---

## 2. 四种触发路径

| 场景 | 进入点 | 行为 |
| --- | --- | --- |
| WiFi 连不上（20s 超时） | `WifiBoard::OnWifiConnectTimeout()` | 不进配网，`EnterOfflineMode("wifi connection timeout")` + 30s 后自动重试 |
| 网关/音频通道打不开 | `Application::ContinueOpenAudioChannel()` / `ContinueWakeWordInvoke()` | `EnterOfflineMode("audio channel unavailable", true)`，立刻开一个命令窗口 |
| 有唤醒词但没有 protocol 对象 | `HandleWakeWordDetectedEvent()` | `EnterOfflineMode("no protocol", true)`，丢掉唤醒词太浪费 |
| 手动 | BOOT 键短按 | 离线时等同"按住说话"：开/关命令窗口 |
| 手动 | BOOT 键长按 | 强制进配网模式（逃生入口，离线时唯一回在线配网的途径） |

网络恢复：`HandleNetworkConnectedEvent()` 检测到 `offline_mode_` 为真 →
`ExitOfflineMode()` + 状态回到 `Idle` → 走 `Activating` 正常激活链路。

---

## 3. 命令窗口（Command Window）

离线模式下唤醒词不再"上传音频"，而是**开一个 10 秒的本地区命令窗口**：

```
唤醒词 / BOOT 短按
      ↓
BeginOfflineCommandWindow()
      ├─ audio_service_.SetUpstreamEnabled(false)   // 麦克风数据不再进 Opus 上传队列
      ├─ EnableCommandRecognition(命令词表, 回调)     // 建独立 MultiNet 实例
      ├─ SetListeningMode(AutoStop) + SetDeviceState(Listening)
      │     → StartListeningAudio() → EnableVoiceProcessing(true)  // AFE 才会产出 voice 声道
      └─ 起 10s 定时器
      ↓  识别到命令（连击可续窗口）
HandleOfflineCommand()
      ├─ OfflineIntentManager::HandleCommand()  → 调 MCP 工具
      ├─ PlayOfflineOutcome()  预录音频 + 屏幕文字
      └─ 重开 10s 定时器（"打开电灯" → "关掉它" 落在同一窗口内）
      ↓  10s 无新命令
EndOfflineCommandWindow()  → 关识别器、恢复上行、回 Idle（唤醒词重新武装）
```

**状态机必须真的进 `Listening`**：`EnableVoiceProcessing(true)` 只在
`StartListeningAudio()` 里调用，而它由 `kDeviceStateListening` 的状态变更触发。
只 `SetListeningMode()` 不 `SetDeviceState(Listening)` 的话，AFE 只会为唤醒词运行，
`HandleVoiceResult()` 永不执行，命令识别器拿不到任何音频——这是一个"编译通过、
跑起来毫无反应"的坑。

**回放期间仍然暂停识别**：喇叭放的预录音会绕一圈回到麦克风，`打开电灯` 的应答
可能被识别成另一条命令。所以 `PlayOfflineOutcome()` 会
`SetCommandRecognitionPaused(true)`，等 `MAIN_EVENT_PLAYBACK_DRAINED` 事件确认
播放队列真的空了再解暂停。

> 2026-10-16 起这块板子已经有了**数字域回声参考**（`CONFIG_AEC_DIGITAL_REFERENCE`，
> AFE 拿到 `M,R` 两路、设备侧 AEC 真的在跑），见
> [`ENGINEERING_NOTES.md` §11.1](./ENGINEERING_NOTES.md)。因为 AEC 不可能 100%
> 干净，这里"播放期不认命令"的保险仍然保留。

### 在线时的网关命令看门狗（Gateway Watch）

离线命令窗口的前提是"没有服务器"。但**切到本地网关之后恰恰切不回来**——那句话
会照常上传给本地大模型，而本地模型往往不支持 tool call，调不动
`self.gateway.set_mode`，于是"语音切回官方"就断了。

修法是在**连着远端网关时也自己认这 4 条**：

| 说法 | 动作 |
| --- | --- |
| 切换到本地网关 | 切 local 并重启 |
| 切换到官方服务器 | 切 official 并重启 |
| 恢复自动模式 | 切 auto 并重启 |
| 你连的哪个服务器 | 播报当前模式 |

* 进入 `Listening` 时武装（`Application::BeginGatewayCommandWatch()`），离开
  `Listening` 时拆掉；和 ASR 上传共用 AFE 输出，不额外吃音频。
* 命中后**先掐上行**再 `AbortSpeaking` + `SendStopListening` 回 `Idle`，
  然后走同一套 `OfflineIntentManager::HandleCommand()` 播本地应答——
  所以这句话不会进网关的转写，也不会和它的回复抢喇叭。
* 因为不依赖后端，**官方 → 本地**方向也一并可用。

> 两条硬约束：`esp_mn_commands_*` 是进程级单例，看门狗与离线窗口**必须互斥**
> （`EnterOfflineMode()` 第一件事就是拆看门狗）；`PlayOfflineOutcome()` 会把
> 识别器置为暂停，下一轮武装前必须解暂停。细节见
> [`ENGINEERING_NOTES.md` §11.2](./ENGINEERING_NOTES.md)。

---

## 4. 命令表（20 条）

`pinyin` 是交给 MultiNet 的发音串（与 `CONFIG_CUSTOM_WAKE_WORD` 同格式），
`display` 是日志和屏幕上的显示文本。

| # | 说法 | 动作 | 调用的 MCP 工具 |
| --- | --- | --- | --- |
| 1 | 打开电灯 | 点亮 LAMP_GPIO | `self.lamp.turn_on` |
| 2 | 关闭电灯 | 熄灭 LAMP_GPIO | `self.lamp.turn_off` |
| 3 | 音量大一点 | 音量 +10 | `self.audio_speaker.set_volume` |
| 4 | 音量小一点 | 音量 −10 | `self.audio_speaker.set_volume` |
| 5 | 音量最大 | 音量 = 100 | `self.audio_speaker.set_volume` |
| 6 | 音量一半 | 音量 = 50 | `self.audio_speaker.set_volume` |
| 7 | 静音 | 音量 = 0 | `self.audio_speaker.set_volume` |
| 8 | 屏幕亮一点 | 亮度 +20 | `self.screen.set_brightness` |
| 9 | 屏幕暗一点 | 亮度 −20 | `self.screen.set_brightness` |
| 10 | 屏幕最亮 | 亮度 = 100 | `self.screen.set_brightness` |
| 11 | 换成深色 | 深色主题 | `self.screen.set_theme` |
| 12 | 换成浅色 | 浅色主题 | `self.screen.set_theme` |
| 13 | 切换到本地网关 | 切 local 并重启 | `self.gateway.set_mode` |
| 14 | 切换到官方服务器 | 切 official 并重启 | `self.gateway.set_mode` |
| 15 | 恢复自动模式 | 切 auto 并重启 | `self.gateway.set_mode` |
| 16 | 电量还有多少 | 播报电量百分比 | `self.get_device_status` |
| 17 | 你连的哪个服务器 | 播报当前网关模式 | `self.gateway.get_status` |
| 18 | 关掉它 | 关掉上一个操作的设备 | 取决于上下文 |
| 19 | 再大一点 | 上一个设备 +1 档 | 取决于上下文 |
| 20 | 再小一点 | 上一个设备 −1 档 | 取决于上下文 |

相对量词（3/4/8/9）先读 `self.get_device_status` 拿当前值再算目标值，不瞎猜。

### 上下文指代（18/19/20）

`OfflineIntentManager` 记一个 `last_device_`（灯 / 音量 / 屏幕），在上面这些
命令命中时更新。所以：

```
你：小智小智        → 打开命令窗口
你：打开电灯         → 灯亮，"已打开电灯"（last_device_ = 灯）
你：关掉它           → 灯灭                 ← 不用再说"电灯"
你：音量大一点        → 音量 +10（last_device_ = 音量）
你：再大一点         → 音量再 +10           ← 不用再说"音量"
```

指代不到目标（比如开机第一句就说"关掉它"）时回"这个指令我还不支持"，
而不是猜。

### 动态数值播报

电量这种任意数字不可能预录每一种组合。做法是拆成音频片段拼接：
`OGG_0..OGG_9`（语言包自带数字音）+ `offline_ten`（"十"）+ `offline_hundred`（"百"）。

```
BatteryLevel = 78
→ "当前电量百分之" + "七" + "十" + "八"        （≤20% 再追加 "电量偏低，请及时充电"）
```

中文的 10~19 读作"十X"而不是"一十X"，`NumberSounds()` 里单独处理了这一档。
这块板子没有电量计，`get_device_status` 的 `battery` 字段缺失，
此时回"这台设备没有电池"，而不是播报一个假的 0%。

---

## 5. 代码结构与扩展点

| 文件 | 职责 |
| --- | --- |
| `main/offline_intent.{h,cc}` | 本地意图层：命令表、槽位/上下文解析、MCP 工具调用、应答音频选择 |
| `main/mcp_server.{h,cc}` | 新增 `McpServer::RunTool()` / `RunToolText()` 与 `McpTool::CallTool()`，把 JSON-RPC 与本地直调两条路径收敛到同一个 `BuildToolArguments()` |
| `main/audio/engines/afe_audio_engine.{h,cc}` | 独立于 WakeNet 的 MultiNet 命令识别实例；`HandleVoiceResult()` 里把 AFE 输出分给识别器 |
| `main/audio/audio_service.{h,cc}` | 透传接口 + `SetUpstreamEnabled(bool)`（离线时掐断上行编码） |
| `main/application.{h,cc}` | 离线就绪模式编排：进/出离线、命令窗口开合、应答播放；**在线时的网关命令看门狗**（`BeginGatewayCommandWatch()` / `HandleGatewayCommand()`） |
| `main/boards/common/wifi_board.cc` | WiFi 超时兜底（不推配网）；连上网络后起**局域网配网页**（见 `ENGINEERING_NOTES.md` §11.3） |
| `managed_components/78__esp-wifi-connect/*` | 本地定制（会被 `update-dependencies` 覆盖）：配网页高级项写 `websocket` 命名空间；LAN 服务器（`StartLanMode()` / `StopLanMode()`） |
| `main/boards/bread-compact-wifi-lcd/compact_wifi_board_lcd.cc` | BOOT 短按开窗 / 长按进配网 |
| `main/assets/common/offline_*.ogg` | 27 条预录应答（SAPI → OGG/opus 48k） |

### 唤醒词与命令词共存的关键

两者都来自 ESP-SR，但：

* WakeNet 走 `wakenet_model_name`，是常驻的唤醒检测器；
* MultiNet 命令识别走 `multinet_model_name`，**只在命令窗口内按需创建**，
  窗口关闭就 `destroy()` 掉。

原因有两个：`esp_mn_commands_*` 是进程级单例（谁后注册谁覆盖），
以及 MultiNet 自己的 8 秒检测窗会打断中文指令。所以引擎里把 WakeNet 的优先级
提到首位，MultiNet 仅做命令窗口期的临时实例。

### 只 Apply 一次资源

`Assets::Apply()` 会 `esp_srmodel_deinit()` 掉正在被音频引擎引用的 SR 模型。
离线路径可能在"服务端资源版本检查"之前就走到 `Apply()`，所以用
`assets_applied_` 标志把 `Apply()` 收敛成**每次启动最多一次**（`ApplyLocalAssets()`）。
代价是：新下载的资源包要等下次重启才生效——这比"把正在用的模型抽走"要好得多。

### 新增一条命令

1. 在 `offline_intent.cc` 的 `Intent` 枚举与 `kCommands[]` 里各加一项
   （`pinyin` 音节要合法，否则模型会静默丢掉这一条）；
2. 在 `HandleCommand()` 的 `switch` 里加分支，调 MCP 工具、返回
   `MakeOutcome(声音, 屏幕文字)`；
3. 需要新应答音时，往 `tools/offline_clips.json` 加文本，
   跑 `tools/gen_offline_clips.ps1`，产出 `main/assets/common/offline_*.ogg`；
4. 重新编译。`lang_config.h` 里的 `OGG_OFFLINE_*` 由 `scripts/gen_lang.py`
   **按文件名**自动生成，不用手工登记。

> 命令数量上限受 MultiNet 模型与 RAM 限制。当前 20 条 + 10s 窗口在
> ESP32-S3 N16R8 上余量充足；加到 50 条以上要重新评估 PSRAM 占用。

---

## 6. 构建、烧录与验证

### 构建

```powershell
# 必须用 PowerShell（idf.py 拒绝 MSYS/Git Bash）
powershell -ExecutionPolicy Bypass -File D:\projects\xiaod-voice\build_firmware.ps1
```

脚本会显式注入 IDF 环境（`D:\esp`）并设置两个必需变量：

* `IDF_SKIP_CHECK_SUBMODULES=1` —— 否则 cmake 尝试从 gitee 克隆
  `esp32c2-bt-lib` 子模块并失败；
* `XD_KEEP_TEMP_BUILD=1` —— 否则 `build_default_assets.py` 删
  `build/temp_build` 会触发环境的批量删除守卫，把整棵构建进程树冻成 0% CPU。

产物落到 `firmware_dist/`：`xiaozhi.bin`、`generated_assets.bin`、
`bootloader.bin`、`partition-table.bin`、`ota_data_initial.bin`、`merged_16mb.bin`。

### 构建时必须看到的三个证据

```
multinet models: mn7_cn, fst (will be packaged)     # assets 打包阶段
Added multinet model: mn7_cn
Added multinet model: fst
```

以及 `sdkconfig` 里：

```
# CONFIG_SR_MN_CN_NONE is not set
CONFIG_SR_MN_CN_MULTINET7_QUANT=y
CONFIG_OFFLINE_INTENT=y
```

`CONFIG_OFFLINE_INTENT` 由 `main/Kconfig.projbuild` 声明（esp32s3/p4/s31 + SPIRAM，
默认 y）。没有多模型被选中时 `build_default_assets.py` 会直接报错，
不会安静地产出一个"离线其实不可用"的固件。

### 烧录

```powershell
# 分文件刷入（保留 NVS：WiFi 凭据、网关模式等）
firmware_dist\flash.ps1 -Port COM3

# 整片刷入（等于恢复出厂）
firmware_dist\flash.ps1 -Port COM3 -Full
```

完整步骤（查串口号、进下载模式、GUI 工具、连不上的排查）见
[`FLASHING.md`](./FLASHING.md)。

> 注意：离线模型与应答语音在 **assets 分区（0x800000）**，只刷 `xiaozhi.bin`
> 会得到一台"能开机但一句离线命令都听不懂"的设备。

### 每次唤醒都会重建识别器，盯住堆

识别器（MultiNet 实例）是**每个命令窗口创建、关闭时销毁**的，也就是**每次唤醒
一次**。日志里因此会有成对的两行：

```
I (xxxx) AfeAudioEngine: MultiNet instance created: cost 123456 B (psram+internal), free 4567890/123456 -> 4444434/98765
I (xxxx) AfeAudioEngine: MultiNet instance destroyed: reclaimed 123456 B, free 4444434 -> 4567890
```

创建开销 ≈ 销毁回收，且 `free` 回到原点。如果每次唤醒后 `free` 单调下降、
回收量明显小于开销，`destroy()` 就在漏 —— 唤醒十几次后会 OOM 重启。

### 断网全链路验证清单

1. 拔掉/改错 WiFi 密码 → 设备 20s 后应显示「离线模式」并播「离线模式已就绪」，
   **不进配网热点**；
2. 说唤醒词 → 听见 popup 音（进入命令窗口）；
3. 说「打开电灯」→ 灯亮 + 「好的，已打开电灯」+ 屏幕文字；
4. 紧接着说「关掉它」→ 灯灭（验证上下文指代）；
5. 说「你连的哪个服务器」→ 播报网关模式；
6. 说「电量还有多少」→ 播「这台设备没有电池」（本板无电量计）；
7. 静置 10s 不说话 → 窗口自动关闭；再说唤醒词仍能重新开窗（验证唤醒词没死）；
8. 恢复 WiFi → 设备自动退出离线模式并走激活；再唤醒应回到在线问答；
9. 离线时**长按 BOOT** → 能进配网页（验证逃生入口）。

---

## 7. 已知限制

* **不能自由对话**。只能命中下面这 20 条；表外的话统一回「这个指令我还不支持」。
  这是 MultiNet（闭集命令词识别）的固有限制，不是实现偷懒。
* **不联网就查不了任何外部信息**（天气、时间、新闻、知识问答）。
* **回放期间短暂"听不见"**。为避免喇叭回声自触发，播放应答时会暂停识别；
  应答较长时这段时间内的说话会被忽略。
* **上下文只在窗口内有效**。窗口一关（10s 无命令）`last_device_` 由
  `ClearContext()` 清掉，下次开窗"关掉它"就指代不到了。
* **`Assets::Apply()` 每次启动只跑一次**，新下载的资源包下次重启才生效。
* 语音切换网关（13/14/15）会 `Reboot()`，这是复用 `self.gateway.set_mode`
  的既有行为，离线也照旧。
* **配网页常驻局域网，且没有鉴权**（和 AP 配网页一样，但覆盖面从"能连上热点
  的人"扩大到"同一局域网内的任何人"）。仅适合可信网络。
