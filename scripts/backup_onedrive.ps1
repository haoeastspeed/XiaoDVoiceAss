# xiaod-voice -> OneDrive 源码备份
# 只同步 docs/scripts/tests/gateway/firmware 与根文件，
# 排除 reference/（第三方服务端+运行库，体积大）、.venv、__pycache__、模型。
# robocopy 退出码 <8 视为成功。
param(
  [string]$src = "D:\projects\xiaod-voice",
  [string]$dst = "E:\工作文档\OneDrive\02_软件工具\xiaod-voice",
  [switch]$SourcesOnly
)
$ErrorActionPreference = "Stop"

# 校验源路径非空且在预期范围内
if ([string]::IsNullOrWhiteSpace($src)) { throw "src 为空" }
$srcFull = [System.IO.Path]::GetFullPath($src)
if (-not $srcFull.StartsWith("D:\projects\", [StringComparison]::OrdinalIgnoreCase)) {
  throw "源路径越界: $srcFull"
}
if (-not (Test-Path $srcFull)) { throw "源路径不存在: $srcFull" }
if (-not (Test-Path $dst)) { New-Item -ItemType Directory -Force -Path $dst | Out-Null }

$common = @("/MIR", "/R:1", "/W:1", "/NP", "/NFL", "/NDL",
            "/XD", "__pycache__", ".venv",
            "/XF", "*.pyc", "*.pyo")

# 仅同步源码目录（reference/ 刻意不包含）
$dirs = @("docs", "scripts", "tests", "gateway", "firmware")
foreach ($d in $dirs) {
  $s = Join-Path $srcFull $d
  if (Test-Path $s) {
    $t = Join-Path $dst $d
    robocopy $s $t @common | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy $d 失败 code=$LASTEXITCODE" }
    Write-Host "synced dir: $d"
  }
}

# 根文件
$rootFiles = @("README.md", ".gitignore", "AGENTS.md")
foreach ($f in $rootFiles) {
  $s = Join-Path $srcFull $f
  if (Test-Path $s) { Copy-Item $s (Join-Path $dst $f) -Force }
}
Write-Host "synced root files"
Write-Host "BACKUP ONEDRIVE DONE"
