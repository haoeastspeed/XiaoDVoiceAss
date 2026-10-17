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
| D1 | 唤醒词 → 正常对话 | 三模式下均正常，AEC 验收行 `total 2 channels(1 microphone, 1 playback)` 仍在 |
| D2 | 网关对话中 barge-in / Esc 打断 | 与升级前一致 |
| D3 | 断网 → 恢复 | 离线兜底 → 网络恢复回正常模式（`Entering offline mode` / `Leaving offline mode`） |

---

## 需要回传

上述任一项不符合预期时，回传对应时段完整串口日志（UTF-8/GBK 均可），重点保留 `[GatewaySwitch]`、`EnterOfflineMode`、`LAN config`、`esp_wifi` 相关行。

## 已知边界（非 Bug）

- 切到「官方在线」若设备从未绑定/无官方地址：明确播报失败并保持原模式（不再静默变离线）。
- Speaking 中口令识别依赖看门狗武装时机，个别情况下需先打断再下口令（A8）。
- 配网页 Wi-Fi 参数（发射功率/睡眠模式等）仍按原逻辑在下一次连接时生效。
