# Build the ESP32-S3 firmware (xiaoZhi fork used by the xiaod-voice project).
#
# The environment is set explicitly instead of via export.ps1: this box has the
# IDF at D:\esp and idf.py refuses to run under MSYS/Git Bash, so the build has
# to go through PowerShell with the tool paths injected by hand.
#
# Two environment variables are mandatory here:
#   IDF_SKIP_CHECK_SUBMODULES=1  otherwise cmake tries to clone the esp32c2-bt-lib
#                                submodule from gitee and fails the configure step.
#   XD_KEEP_TEMP_BUILD=1         otherwise build_default_assets.py deletes
#                                build/temp_build, tripping the safe-delete guard
#                                and freezing the whole build with 0% CPU.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File build_firmware.ps1 [extra idf.py args]
param(
    [switch]$Clean,
    [string]$DistDir = "firmware_dist",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
)

$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
$proj = Join-Path $root "firmware\xiaozhi-esp32"

$env:IDF_PATH              = "D:\esp\esp-idf"
$env:IDF_TOOLS_PATH        = "D:\esp\tools"
$env:IDF_PYTHON_ENV_PATH   = "D:\esp\tools\python_env\idf6.1_py3.14_env"
$env:ESP_IDF_VERSION       = "6.1"
$env:ESP_ROM_ELF_DIR       = "D:\esp\tools\tools\esp-rom-elfs\20241011"
$env:IDF_SKIP_CHECK_SUBMODULES = "1"
$env:XD_KEEP_TEMP_BUILD    = "1"

$toolDirs = @(
    "D:\esp\tools\tools\xtensa-esp-elf\esp-15.2.0_20251204\xtensa-esp-elf\bin",
    "D:\esp\tools\tools\cmake\4.0.3\bin",
    "D:\esp\tools\tools\ninja\1.12.1",
    "D:\esp\tools\tools\idf-exe\1.0.3",
    "D:\esp\tools\tools\esp-rom-elfs\20241011"
)
foreach ($d in $toolDirs) {
    if (Test-Path $d) { $env:PATH = "$d;$env:PATH" }
}

$python = Join-Path $env:IDF_PYTHON_ENV_PATH "Scripts\python.exe"
if (-not (Test-Path $python)) { throw "IDF python env not found: $python" }
if (-not (Test-Path $proj))   { throw "Firmware project not found: $proj" }

Push-Location $proj
try {
    if ($Clean) {
        Write-Host "=== fullclean ==="
        & $python "$env:IDF_PATH\tools\idf.py" fullclean
        if ($LASTEXITCODE -ne 0) { throw "fullclean failed" }
    }

    Write-Host "=== reconfigure ==="
    # Must be explicit: idf.py build does NOT re-run the Kconfig step when only
    # main/Kconfig.projbuild changed, so a new option silently stays undefined
    # in build/config/sdkconfig.h and every `#if CONFIG_...` around it compiles
    # to 0. Skipping this produces a build that looks successful but ignores the
    # new config. Reconfigure is cheap when nothing changed.
    & $python "$env:IDF_PATH\tools\idf.py" reconfigure
    if ($LASTEXITCODE -ne 0) { throw "reconfigure failed" }

    Write-Host "=== build ==="
    & $python "$env:IDF_PATH\tools\idf.py" build @ExtraArgs
    if ($LASTEXITCODE -ne 0) { throw "build failed" }

    # Collect the artefacts the flasher needs.
    $out = Join-Path $root $DistDir
    New-Item -ItemType Directory -Force -Path $out | Out-Null
    $files = @{
        "xiaozhi.bin"            = "xiaozhi.bin"
        "bootloader\bootloader.bin" = "bootloader.bin"
        "partition_table\partition-table.bin" = "partition-table.bin"
        "ota_data_initial.bin"   = "ota_data_initial.bin"
        "generated_assets.bin"   = "generated_assets.bin"
    }
    foreach ($k in $files.Keys) {
        $src = Join-Path $proj "build\$k"
        if (-not (Test-Path $src)) { throw "missing build artefact: $src" }
        Copy-Item $src (Join-Path $out $files[$k]) -Force
    }

    # Merged image: partitions/mode in one shot, i.e. what a blank board needs.
    Write-Host "=== merge-bin ==="
    & $python -m esptool --chip esp32s3 merge-bin -o (Join-Path $out "merged_16mb.bin") `
        --flash-mode dio --flash-freq 80m --flash-size 16MB `
        0x0     (Join-Path $out "bootloader.bin") `
        0x8000  (Join-Path $out "partition-table.bin") `
        0xd000  (Join-Path $out "ota_data_initial.bin") `
        0x20000 (Join-Path $out "xiaozhi.bin") `
        0x800000 (Join-Path $out "generated_assets.bin")
    if ($LASTEXITCODE -ne 0) { throw "merge-bin failed" }

    Get-ChildItem $out | Select-Object Name, Length, LastWriteTime | Format-Table -AutoSize
    Write-Host "Artefacts in $out"
}
finally {
    Pop-Location
}
