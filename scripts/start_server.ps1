# ============================================================
# 小D语音 M1 —— 启动本地 xiaozhi-server
# 作用：注入自带 opus.dll / ffmpeg / ffprobe 到 PATH，激活 .venv 后运行 app.py
# 用法：powershell -ExecutionPolicy Bypass -File scripts\start_server.ps1
# ============================================================
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"

# ---- 路径 ----
$proj = "D:\projects\xiaod-voice"
$srv  = Join-Path $proj "reference\xiaozhi-server\main\xiaozhi-server"
$bin  = Join-Path $proj "reference\bin"   # 自带 opus.dll / ffmpeg.exe / ffprobe.exe

# ---- 若自带 ffmpeg 缺失，则从机器上已有目录补一个（取第一个同时含 ffmpeg/ffprobe 的） ----
if (-not (Test-Path (Join-Path $bin "ffmpeg.exe"))) {
    $ffCandidates = @(
        "D:\Program Files\万能工具箱\resources\bin",
        "D:\LuminaCut\third_party\ffmpeg\bin",
        "D:\LuminaCut\dist\app"
    )
    $ff = $ffCandidates | Where-Object {
        (Test-Path (Join-Path $_ "ffmpeg.exe")) -and (Test-Path (Join-Path $_ "ffprobe.exe"))
    } | Select-Object -First 1
    if ($ff) {
        Copy-Item (Join-Path $ff "ffmpeg.exe")  $bin -Force
        Copy-Item (Join-Path $ff "ffprobe.exe") $bin -Force
        Write-Host "已从 $ff 补入 ffmpeg/ffprobe" -ForegroundColor Yellow
    }
}

# ---- 注入 PATH（opus.dll 必须在 PATH，find_library 才能命中；ffmpeg/ffprobe 同理） ----
$env:PATH = "$bin;$env:PATH"

# ---- 基本校验（三件套） ----
$py = Join-Path $srv ".venv\Scripts\python.exe"
if (-not (Test-Path $py))                          { throw "未找到 venv：$py" }
if (-not (Test-Path (Join-Path $bin "opus.dll")))  { throw "未找到 opus.dll：$bin" }
if (-not (Test-Path (Join-Path $bin "ffmpeg.exe"))){ throw "未找到 ffmpeg.exe：$bin" }
if (-not (Test-Path (Join-Path $bin "ffprobe.exe"))){throw "未找到 ffprobe.exe：$bin" }

# ---- 启动 ----
Set-Location $srv
Write-Host "正在启动小D语音服务（WS 8000 / OTA 8003）..." -ForegroundColor Cyan
& $py app.py
