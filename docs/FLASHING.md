# 固件刷入指南（bread-compact-wifi-lcd / ESP32-S3 N16R8）

本文针对 `firmware_dist/` 下的产物。刷入前先理解一件事：**这套改动把
MultiNet 命令词模型和 27 条离线应答音频放进了 assets 分区（0x800000）**，
所以只刷 `xiaozhi.bin` 是不够的 —— 那样设备能开机，但离线命令识别 =
没有模型 = 一句都听不懂。

> **关于"以前只刷一个文件就行"**：可启动的 ESP32 镜像从来就不只是 app，它至少
> 需要 bootloader(0x0) + 分区表(0x8000) + app(0x20000)。如果你以前只刷一个
> `xiaozhi.bin` 到 `0x0` 就成功，那个文件其实是**已经合并好的完整镜像**
> （bootloader+分区表+app 一体），不是裸 app——裸 app 单独放 `0x0` 是
> 起不来的。本构建里对应的完整镜像叫 **`merged_16mb.bin`**，单文件刷 `0x0`
> 即可，与以前体验一致。本构建同时产出裸 app（`xiaozhi.bin`，用于保留 NVS 的
> 升级）和完整镜像（`merged_16mb.bin`，用于单文件/整片刷）。

## 1. 先确认串口号

```powershell
Get-CimInstance Win32_PnPEntity | Where-Object { $_.Name -match 'COM[0-9]+' } |
    Select-Object Name, Status | Format-Table -AutoSize
```

插上板子前后各跑一次，多出来的那个就是。常见两种：

| 设备名 | 说明 |
| --- | --- |
| `USB-SERIAL CH340 (COMx)` / `Silicon Labs CP210x (COMx)` | 外挂 USB-UART 桥，esptool 能自动进下载模式 |
| `USB JTAG/serial debug unit (COMx)` | S3 原生 USB，**必须手动进下载模式** |

本机此前只看到蓝牙的 COM3/COM4，说明当时没有板子接入 —— 插上后再执行一次。

## 2. 方式 A：一键脚本（推荐）

```powershell
# 保留 NVS：WiFi 密码、网关模式、音量等都还在
powershell -ExecutionPolicy Bypass -File D:\projects\xiaod-voice\firmware_dist\flash.ps1 -Port COM3

# 整片刷入 merged_16mb.bin，等同恢复出厂（NVS 也会被清掉）
powershell -ExecutionPolicy Bypass -File D:\projects\xiaod-voice\firmware_dist\flash.ps1 -Port COM3 -Full
```

脚本内直接用本机 IDF 的 Python（`D:\esp\tools\python_env\idf6.1_py3.14_env\Scripts\python.exe`），
不需要先跑 `export.ps1`，也不需要把 esptool 加进 PATH。

### 两种刷法怎么选

| | 分文件 | `-Full` 整片 |
| --- | --- | --- |
| 写入量 | ~6.7 MB（4 个文件） | 12.3 MB（1 个文件） |
| 耗时 | 更短 | 更长 |
| NVS | **保留** | 清空 |
| 适用 | 日常升级 | 首次、分区表变过、NVS 损坏 |

**日常升级用分文件即可**；只有换过分区表布局或怀疑 NVS 坏了才用 `-Full`。

### 刷入的偏移

```
0x000000  bootloader.bin
0x008000  partition-table.bin
0x00d000  ota_data_initial.bin
0x020000  xiaozhi.bin          ← app（ota_0，4MB）
0x800000  generated_assets.bin ← 离线模型 + 语音，3.9MB
```

与 `partitions/v2/16m.csv` 一致。

## 3. 方式 B：直接调 esptool

脚本之外，等价的手工命令：

```powershell
$py = "D:\esp\tools\python_env\idf6.1_py3.14_env\Scripts\python.exe"
$d  = "D:\projects\xiaod-voice\firmware_dist"

# 分文件
& $py -m esptool --chip esp32s3 -p COM3 -b 460800 `
    --before default-reset --after hard-reset write-flash `
    --flash-mode dio --flash-size 16MB --flash-freq 80m `
    0x0 "$d\bootloader.bin" 0x8000 "$d\partition-table.bin" `
    0xd000 "$d\ota_data_initial.bin" 0x20000 "$d\xiaozhi.bin" `
    0x800000 "$d\generated_assets.bin"

# 整片
& $py -m esptool --chip esp32s3 -p COM3 -b 460800 `
    --before default-reset --after hard-reset write-flash `
    --flash-mode dio --flash-size 16MB --flash-freq 80m 0x0 "$d\merged_16mb.bin"
```

## 4. 方式 C：乐鑫 Flash Download Tool（GUI）

不想碰命令行时用它（[下载页](https://www.espressif.com.cn/en/support/download/other-tools)，
3.9.x 支持 ESP32-S3）。两种方式任选其一：

### 4a. 分文件刷（保留 NVS，推荐日常升级）

顶部第一行：`Chip = ESP32S3`、`SPI SPEED = 80 MHz`、`SPI MODE = DIO`、
`FLASH SIZE = 16MB`。然后逐行添加并**勾选**下面的文件（不要勾 NVS）：

| 文件 | Offset |
| --- | --- |
| `bootloader.bin` | `0x0` |
| `partition-table.bin` | `0x8000` |
| `ota_data_initial.bin` | `0xd000` |
| `xiaozhi.bin` | `0x20000` |
| `generated_assets.bin` | `0x800000` |

底部选 COM 口、Baud 拉到 `921600`，点 **START**。

### 4b. 整片刷（清空 NVS，等同 `-Full`）

只加一个文件，最简单，适合换新板或怀疑 NVS 损坏：

| 文件 | Offset |
| --- | --- |
| `merged_16mb.bin` | `0x0` |

顶部同样设 `ESP32S3 / 80 MHz / DIO / 16MB`。

> **两种方式的区别**：4a 不碰 `nvs`（0x9000，0x4000），WiFi 密码、网关模式、
> 音量都保留；4b 的 merged 内含 nvs，会清掉。4a 写 ~6.7MB、4b 写 ~12.3MB。

### 进下载模式（关键）

工具连不上时几乎都是没进下载模式：**按住 BOOT(GPIO0) → 点一下 RESET →
松开 BOOT**，再点 START。外挂 CH340/CP210x 小板通常自动进；S3 原生 USB
（设备名 `USB JTAG/serial debug unit`）必须手动按键。充电线不通数据，先换线。

### ⚠️ 必须刷 `generated_assets.bin`

离线的 MultiNet 命令词模型 + 27 条应答语音全在这 3.9MB 里（地址 `0x800000`）。
只刷 `xiaozhi.bin` 会装出一台「能开机、能联网正常用，但离线一句命令都听不懂」
的设备，而且不报任何错。用 4b 整片刷则天然包含它。

## 5. 连不上时怎么办

### `A fatal error occurred: Failed to connect to ESP32-S3`

这是最常见的，99% 是没进下载模式。按这个顺序试：

1. **确认是数据线**，不是纯充电线（手机自带的充电线通常不通讯）；
2. **手动进下载模式**：按住 **BOOT(GPIO0)** → 点一下 **RESET** → 松开
   BOOT，立刻开始刷。原生 USB 的板子必须这么做；
3. 拔插 USB，换口（机箱前置口供电/信号都差）；
4. 如果设备管理器里根本不出现 COM 口，是 CP2102/CH34x 驱动没装。

### 能连上但中途超时

把波特率降下来：`-b 115200`（改成 `460800` 以下）。

### `Wrong boot mode detected (0x13)`

已经进了下载模式但芯片没复位，直接重跑一次脚本即可。

### 刷完不启动 / 反复重启

`-Full` 整片刷一遍排除 NVS 残留。

## 6. 刷完后看日志

```powershell
$py = "D:\esp\tools\python_env\idf6.1_py3.14_env\Scripts\python.exe"
& $py -m esp_idf_monitor -p COM3 -b 115200
```

（或用 IDF：先 `export.ps1`，再 `idf.py -p COM3 monitor`。）

### 必须看到的几行

```
I (xxxx) Application: Local assets applied without contacting a server
I (xxxx) Application: Entering offline mode: wifi connection timeout
I (xxxx) Application: Offline command window open (10000 ms, 20 phrases)
I (xxxx) AfeAudioEngine: Offline command detected: id=1, string=..., prob=0.9xx
```

如果只看到前三行、说话永远没有第四行，说明 AFE 输出没喂进命令识别器 ——
检查 `EnableVoiceProcessing` 是否在 `kDeviceStateListening` 时被调用了。

顺带盯一下成对出现的堆日志（识别器每次唤醒都重建）：

```
I (xxxx) AfeAudioEngine: MultiNet instance created: cost 123456 B ... free 4567890 -> 4444434
I (xxxx) AfeAudioEngine: MultiNet instance destroyed: reclaimed 123456 B, free 4444434 -> 4567890
```

回收量应≈开销，`free` 应回到原点；单调下降就是在漏内存。

### 打包链路的自检项

```
multinet models: mn7_cn, fst (will be packaged)   ← 构建期，缺了就是离线不可用
```

---

## 7. 功能验证

刷入后按 [`OFFLINE_MODE.md` 第 6 节](./OFFLINE_MODE.md#6-构建烧录与验证)
的 9 步清单走一遍断网全链路。
