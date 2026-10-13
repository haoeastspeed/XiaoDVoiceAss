# ============================================================
# 小D语音 M2 —— 启动自研轻量小D网关
# 作用：注入自带 opus.dll / ffmpeg / ffprobe 到 PATH，用 M1 的 .venv 运行 main.py
# 用法：powershell -ExecutionPolicy Bypass -File scripts\start_gateway.ps1
# 注意：网关与 M1 服务都用 8000 端口，运行前请先停止 M1 服务
# ============================================================
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"

# ---- 路径 ----
$proj = "D:\projects\xiaod-voice"
$venv = Join-Path $proj "reference\xiaozhi-server\main\xiaozhi-server"
$bin  = Join-Path $proj "reference\bin"

# ---- 注入 PATH（opus.dll / ffmpeg / ffprobe） ----
$env:PATH = "$bin;$env:PATH"

# ---- 基本校验 ----
$py = Join-Path $venv ".venv\Scripts\python.exe"
if (-not (Test-Path $py))                           { throw "未找到 venv：$py" }
if (-not (Test-Path (Join-Path $bin "opus.dll")))   { throw "未找到 opus.dll：$bin" }
if (-not (Test-Path (Join-Path $bin "ffmpeg.exe"))) { throw "未找到 ffmpeg.exe：$bin" }

# ---- 启动 ----
Set-Location $proj
Write-Host "正在启动小D自研网关（WS 8000）..." -ForegroundColor Cyan
& $py main.py
