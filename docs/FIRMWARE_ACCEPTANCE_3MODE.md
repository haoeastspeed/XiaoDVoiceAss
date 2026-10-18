# 三模式语音热切换固件 · 真机验收清单

固件：`firmware_dist/merged_16mb.bin`（2026-10-17 09:37 构建，commit 26bd890）
刷机：`powershell -File firmware_dist\flash.ps1 -Port COM5`（升级可不清 NVS；若要从头验配网用 `-Full`）

日志抓取：pyserial 连 COM5 115200，关注 `[GatewaySwitch]` / `Offline` / `LAN config` 标签。

---

## A. 语音口令热切换（核心闭环）

| # | 操作 | 期望 |
|---|------|------|
| A1 | 任意模式下说「切换到离线模式」 | 立即播「正在切换到 offline 模式」，设备**不重启**；串口 `[GatewaySwitch] switching to offline` → `[GatewaySwitch] applied mode=offline`；随后可离线语音控制（灯/音量/主题等） |
| A2 | 离线模式说「切换到本地网关」 | 播报后 ≤3s 重连本地网关；串口出现协议连接成功日志；**全程无 esp_restart** |
| A3 | 网关模式说「切换到官方服务器」 | 同上，连官方 ws；串口 `switching to official` → `applied mode=official` |
| A4 | 官方模式说「恢复自动模式」 | `applied mode=auto` |
| A5 | 当前已是某模式时再说该模式口令 | 播「当前已经是 xxx 模式」（`OGG_OFFLINE_GW_HEAD`+模式名），**不**播「切换成功」，不发生任何连接动作 |
| A6 | 说「你连的哪个服务器」 | 报当前模式；与 A1-A4 后的实际连接一致（auto 锁存不再显示错位） |
| A7 | 切换后紧接着再切（切换进行中重复口令） | 串口 `[GatewaySwitch] a switch is already in flight, dropping ...`，设备不卡死、不重启 |
| A8 | TTS 播报中说切换口令 | 口令能被打断生效（看门狗在 Listening 武装；若在 Speaking 中未识别不算失败，正常做法是先按打断键再说） |

## B. 离线窗口 / 看门狗互斥

| # | 操作 | 期望 |
|---|------|------|
| B1 | 离线模式下说唤醒词 → 说 20 条离线命令之一 | 正常执行；串口 `Offline command window open` |
| B2 | 切到网关/官方后说唤醒词 → 说「切换到离线模式」 | 看门狗匹配命中；串口 `Gateway command watch armed`（Listening 时） |
| B3 | A2 切换瞬间 | 串口先 `Gateway command watch`/`offline window` 的 teardown 日志，再重建，**绝无两者同时 armed** |

## C. 局域网配网页（任意模式下都可达）

| # | 操作 | 期望 |
|---|------|------|
| C1 | 官方 / 网关 / 离线三模式各测一次：浏览器开 `http://<设备IP>` | 页面正常打开（设备 IP 看串口 Got IP 或路由器） |
| C2 | 页面「Wi-Fi 配置」点扫描 | 列表**非空**；设备**不重启**；当前模式连接不断 |
| C3 | 高级选项改「语音网关」为另一模式 → 保存 | toast「已保存，语音网关设置即将生效」；≤2s 串口 `[GatewaySwitch] config changed` → `applied mode=...`；页面不闪断 |
| C4 | 高级选项改「本地 WebSocket 地址」→ 保存 | 同 C3；`local_url` 生效（可切 local 验证连新地址） |
| C5 | 同一局域网另一台电脑做浏览器连通性探测（访问不存在的路径） | 不再被 302 到 192.168.4.1（LAN 模式已不注册强制门户） |
| C6 | 改 NVS `websocket/mode` 后重启设备 | 重启后直接落在所选模式（持久化真源不变） |

## D. 回归（原有功能不破)

| # | 操作 | 期望 |
|---|------|------|
| D1 | 唤醒词 → 正常对话 | 三模式下均正常；启动日志不再出现 `total 2 channels(1 microphone, 1 playback)`（参考声道已关闭，变为单麦克风是**预期**） |
| D2 | 网关对话中 Esc 打断 | 与升级前一致（播放期唤醒检测已禁用，语音 barge-in 不再指望；Esc/按键打断是确定性通道） |
| D3 | 断网 → 恢复 | 离线兜底 → 网络恢复回正常模式（`Entering offline mode` / `Leaving offline mode`） |

## E. 唤醒与防自激活（2026-10-17 音频策略回归）

背景：曾启用「软件合成参考声道 + 设备端 AEC + WakeNet DET_MODE_95」，导致待机唤不醒、播放期自激活。
本次回退为官方 bread 板策略：无参考声道、`aec_mode_=kAecOff`、播放期彻底禁用唤醒检测。

| # | 操作 | 期望 |
|---|------|------|
| E1 | 三种模式各自待机时说「你好小智」 | 均能唤醒（串口 `Wake word detected`）；不再出现唤醒无响应 |
| E2 | 设备 TTS 播报期间（含音量调大、贴近扬声器） | **不自激活**；播报结束后约 1-2s 唤醒恢复可用（REARM 800ms + REFRACTORY 1500ms 兜回声尾） |
| E3 | 连续多轮对话 | 无「自说自话」循环；串口无连续 `Wake word detected ... (state: 3)` |
| E4 | 串口核对 | 无 `WakeNet detection mode: DET_MODE_95` 打印；无 `Device AEC requires a playback reference channel` 告警；`[AfeAudioEngine] Initialized FD AFE, detector: WakeNet` 仍在 |
| E5 | 本地网关模式：一轮回复播完后**直接说话**（不喊唤醒词） | **能接上**（持续聆听已恢复：`GetDefaultListeningMode()` 对 kLocal 返回 realtime，回声由网关硬闸门防）；串口 `SendStartListening(realtime)` |
| E6 | 官方模式：一轮回复播完后直接说话 | 唤不醒属**预期**（AutoStop，官方策略）；需再喊唤醒词 |
| E7 | 网关控制台核对 | 播报期间出现 `[echo-guard]`/闸门丢弃日志、`[session] listen start mode=realtime`；播放期不产生新一轮回复 |
| E8 | 离线模式：唤醒 → 口令「切换到本地网关」 | 唤醒恢复后此链路应自然恢复（B 类用例重跑一遍） |

注：日志中 esp-sr 打印的输入通道数从 `total 2 channels` 变为 `total 1 channels(1 microphone)` 属预期；如需恢复播放期语音打断（barge-in），未来可接硬件回环参考或在网关侧做 AEC（网关 opus 解码喂 far-end 的修复需另行部署）。

---

## F. 官方在线模式零异常专项（2026-10-17 代码审计批次）

背景：审计发现 `sdkconfig` 里 `CONFIG_USE_DEVICE_AEC` 被 Kconfig 的 `default y if AEC_DIGITAL_REFERENCE`
重新写回，导致固件 `aec_mode_=kAecOnDeviceSide` → 官方模式也走 realtime、播放期麦克风常开，而参考声道
已关闭、实际无 AEC。本轮已修正配置并加入运行时不变量（无参考通道时自动降级 kAecOff 并打 ERROR）。

| # | 操作 | 期望 |
|---|------|------|
| F1 | 开机串口核对 | **不得出现** `Device-side AEC requested but the codec provides no reference channel`（出现即配置又漂了）；`CONFIG_USE_DEVICE_AEC` 应为未定义 |
| F2 | 官方模式：一轮回复播完后直接说话 | 不响应属预期（AutoStop）；喊唤醒词后正常应答 |
| F3 | 官方模式：播报期间对着设备说话 | 不自激活、不自说自话（播放期唤醒解除 + 麦克风关闭） |
| F4 | 断网一次再恢复（官方模式） | 设备不重启；恢复后喊唤醒词可正常对话（修复了断网事件里的空指针解引用） |
| F5 | 拔掉/改错官方 OTA 域名后开机 | 最多约 60-90s 内给出明确提示并继续启动，**不再卡 85 分钟** |
| F6 | auto 模式下一次开机时网络抖动 | 不擦除官方 url/token、不永久锁存 local；本次开机走本地网关，下次开机仍尝试官方 |
| F7 | OTA 升级成功后重启 | 版本号保持新版本（`MarkCurrentVersionValid` 已改为开机无条件执行，不再被 bootloader 回滚） |
| F8 | 配网页把本地 WebSocket 地址写成非法端口（如 `:abc`） | 设备不重启，回落到默认端口并打印 WARN |
| F9 | 连续多轮快问快答 | 每轮都有上行（`listen start` 每轮只发一次，不重复不下漏）；不出现「说了没反应」 |
| F10 | 暂停说话（按 BOOT 打断） | 播报立即静音；不会继续把已排队的句子说完 |

---

## G. 收口批次（2026-10-17 第二批审计遗留项）

背景：上一轮审计留了 4 项未修（auto 回退锁存、MQTT 重连一次性、协议类型依赖 `ota_` 生命周期、显示析构
double-free）。本轮四项全部修掉。

| # | 操作 | 期望 |
|---|------|------|
| G1 | auto 模式：拔网线开机一次，再恢复网络重启 | **不再永久变 local**：串口出现 `Auto-mode official attempt failed (1/3...)`，模式仍为 auto，恢复网络后下次开机回到官方（连续 3 次失败才锁存） |
| G2 | auto 模式：官方域名不可达但本地网关在线 | 本次开机走本地网关并照常对话；串口出现 `Official gateway unreachable (1/3), staying in auto mode` |
| G3 | auto 模式连续 3 次失败后 | 串口 `Latching gateway mode to local after 3 consecutive failures`，NVS `websocket/mode` 变 local（这是设计行为，非故障） |
| G4 | 官方 MQTT 设备：拔网 60s 以上再恢复 | 自动重连恢复（串口 `MQTT disconnected, schedule reconnect` → `Reconnecting to MQTT server` → 正常对话），**不需要重启** |
| G5 | 官方 MQTT 设备：断线时正好在说话/聆听 | 忙时不重连但会继续排队重试（`MQTT reconnect deferred: device busy`），空闲后自动恢复 |
| G6 | 官方模式热切到本地网关，再切回官方（MQTT 设备） | 切回后仍用 MQTT 控制通道（串口 `Using the MQTT control channel provisioned by the official platform`），不再静默退化成 websocket |
| G7 | 长时间运行后重启多次 | 无 `double free` / `LoadProhibited` 类崩溃；显示 Init 失败也不会崩 |

---

## H. 官方模式「话说一半被接话」专项（2026-10-17 晚）

背景：官方在线模式说话说到一半设备就应答，STT 只识别出前半句。排查结论：官方模式断句由
**服务器侧 VAD** 决定，客户端只有两种途径会"截断"它听到的音频——上行断流（CPU 抢占/丢帧）或
本地误触发打断。本轮修复了最大的非上游负载：**MultiNet 口令识别此前在实时 AFE 任务里逐帧内联
推理**，与 opus 编码/WS 上行抢同一任务，官方模式的互联网链路比局域网更放大了帧缺失。

| # | 操作 | 期望 |
|---|------|------|
| H1 | 官方模式：完整说一句 10 字以上长句（中间自然停顿 < 1s） | 设备等说完才应答；STT 文本完整（串口 `>> ...`） |
| H2 | 官方模式：整轮对话期间看串口 | **无** `Encode queue is full, dropping oldest frame`；若仍出现请回传日志（还有别的抢占源） |
| H3 | 官方模式：说「切换到本地网关」 | 仍能本地切换（口令识别移到独立任务 `audio_mn` 后功能不变；串口 `Offline command detected` → `Gateway phrase detected`） |
| H4 | 官方模式：正常聊天中故意说与口令相近的句子 | 不应误触发本地切换（若 `Offline command detected` 误报，回传日志，调 `kCommandThreshold`） |
| H5 | 离线模式：口令控制（开灯/音量等） | 功能不变（识别路径整体平移到独立任务，无行为变化） |

### 若刷后仍复现，按串口日志对号入座

| 日志特征 | 结论 | 对策 |
|---|---|---|
| `Encode queue is full` / 上行队列丢帧 | 仍有 CPU 抢占或网络拥塞 | 回传完整日志 |
| `Offline command detected`（误报）后紧接 abort | 本地口令误触发打断 | 提高阈值或官方模式不装看门狗 |
| 无上述任何一行，`>> 半句` 后直接 `tts start` | 服务器侧 VAD 断句过激 | 客户端无解，需调整说话节奏或官方平台参数 |

---

## I. 官方 MQTT 控制通道（2026-10-18 凌晨，保留 NVS 升级后发现）

背景：保留 NVS 刷入新固件后抓启动日志，发现设备走的是 MQTT 控制通道（官方平台下发了 mqtt
段），却打印 `MQTT: Subscribed to null`——订阅了一个永不投递的主题，官方模式实质上是聋的。
根因：NVS 里存着早期实现写入的**字符串 "null"**（cJSON 把 JSON null 渲染成文本），它非空，
因此绕过了所有"是否配置过"的空值兜底，且 NVS 跨升级保留，一直污染至今。

| # | 操作 | 期望 |
|---|------|------|
| I1 | 保留 NVS 升级后开机（本次场景） | 串口出现 `Discarding placeholder MQTT topics` → `No subscribe_topic provisioned, using devices/p2p/<mac>`（mac 的 `:` 变 `_`）→ `Subscribed to devices/p2p/...`，**不再出现 `Subscribed to null`** |
| I2 | 官方模式：喊「你好小智」并说话 | 能收到服务器下行（tts/llm），有应答；不是"说话没反应" |
| I3 | 官方 OTA 回复里 mqtt 段字段为 null 时 | 不写入 NVS（`IsRealStringValue` 拦截）；`has_mqtt_config_` 只在 endpoint 非空时为真 → 空壳 mqtt 段不再让设备错选 MQTT |
| I4 | 配网页/OTA 写入 url 或 token 为 null 时 | 同样不落盘（websocket 段同保护） |
| I5 | 设备实际未开通 MQTT 时 | 不会因 OTA 回复里存在 mqtt 段就走进 MQTT 通道 |

刷机后请重点验证 I1、I2。若 I2 仍无应答，回传启动+对话时段完整串口日志。

---

## J. 音量键（2026-10-18 上午定稿）

背景：用户确认扩展板（小智 44P，GT-44P-V1.1）的音量键就是**官方小智标准接法**——
GPIO39 = 音量−、GPIO40 = 音量+（另一端接地）。之前"不起作用"的根因是**GPIO40 撞脚**：
面包板参考屏的 DC 正是 GPIO40，而固件里 `VOLUME_UP/DOWN_BUTTON_GPIO` 又是 NC，自学习又把 40
当保留脚跳过——同一个脚既要驱动屏 DC 又要读按键，软件上无解。

**修复 = 硬件动一根线 + 固件跟脚**：屏的 DC 跳线从 GPIO40 挪到空闲的 GPIO38，固件
`DISPLAY_DC_PIN` 同步改为 38；音量键改为编译期默认 GPIO39/40（去掉上电自学习，减少误认领）。
配网页保留「音量+ / 音量− GPIO」输入框（NVS 键 `board/vol_up_gpio`、`board/vol_down_gpio`）作为
接线不同的逃生门。

| # | 操作 | 期望 |
|---|------|------|
| J0 | **刷机前**：把屏的 DC 线从 GPIO40 改插到 GPIO38（只此一根，其余不动） | 屏照常显示；若忘改线直接刷，屏会花/黑——补改线后重启即恢复 |
| J1 | 刷机后直接按音量− / 音量+ | 单击 ±10、长按静音/最大；屏幕显示「音量 xx」；声音确实变小/变大 |
| J2 | 调完音量后重启 | 音量保持（codec 已持久化到 NVS `audio/output_volume`） |
| J3 | 配网页 → 高级设置 | 「音量+ GPIO / 音量− GPIO」默认显示 40 / 39；填 -1 表示未接并清除；**重启生效** |
| J4 | 误填被占用脚（如 18/38/47） | 串口 `Stored volume key pin N (vol_up_gpio) is unusable, discarding`，自动清除，不会抢走屏或音频脚 |
| J5 | 接线确实不同的板子 | 在配网页填实际引脚重启即可，不用重新编译 |

> 旧版自学习固件可能已把认领值写进 NVS 旧键 `board/vol_up`/`board/vol_down`；新固件读的是
> `*_gpio` 新键，旧值自动失效，不会覆盖默认的 39/40。

---

## 需要回传

上述任一项不符合预期时，回传对应时段完整串口日志（UTF-8/GBK 均可），重点保留 `[GatewaySwitch]`、`EnterOfflineMode`、`LAN config`、`esp_wifi` 相关行。

## 已知边界（非 Bug）

- 切到「官方在线」若设备从未绑定/无官方地址：明确播报失败并保持原模式（不再静默变离线）。
- Speaking 中口令识别依赖看门狗武装时机，个别情况下需先打断再下口令（A8）。
- 配网页 Wi-Fi 参数（发射功率/睡眠模式等）仍按原逻辑在下一次连接时生效。
