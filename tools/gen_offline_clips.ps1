# Generate the offline reply clips used by the firmware offline intent layer.
#
# Text lives in offline_clips.json (UTF-8) so this script itself stays pure
# ASCII: Windows PowerShell 5.1 reads .ps1 as ANSI unless it has a BOM, which
# would mangle Chinese literals.
#
# Pipeline: Windows SAPI (offline, no network) -> WAV -> ffmpeg/libopus -> OGG
# 16 kHz mono, the format the firmware's OggDemuxer expects.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File tools\gen_offline_clips.ps1
param(
    [string]$ClipsJson = "$PSScriptRoot\offline_clips.json",
    [string]$OutDir,
    [string]$Ffmpeg = "D:\projects\xiaod-voice\reference\bin\ffmpeg.exe",
    [string]$VoiceName = "Microsoft Huihui Desktop"
)

$ErrorActionPreference = "Stop"

if (-not $OutDir) {
    $OutDir = Join-Path (Split-Path $PSScriptRoot -Parent) "firmware\xiaozhi-esp32\main\assets\common"
}
if (-not (Test-Path $OutDir)) {
    throw "Output directory not found: $OutDir"
}
if (-not (Test-Path $Ffmpeg)) {
    throw "ffmpeg not found: $Ffmpeg"
}

$clips = Get-Content -Raw -Encoding UTF8 $ClipsJson | ConvertFrom-Json
$work = Join-Path $env:TEMP "xiaod_offline_clips"
if (Test-Path $work) { Remove-Item $work -Recurse -Force }
New-Item -ItemType Directory -Path $work | Out-Null

$voice = New-Object -ComObject SAPI.SpVoice
$installed = @($voice.GetVoices() | ForEach-Object { $_.GetDescription() })
$pick = $installed | Where-Object { $_ -like "*$VoiceName*" } | Select-Object -First 1
if (-not $pick) {
    throw "Voice '$VoiceName' not installed. Available: $($installed -join ', ')"
}
$voice.Voice = $voice.GetVoices() | Where-Object { $_.GetDescription() -eq $pick } | Select-Object -First 1
Write-Host "Using voice: $pick"

$names = $clips.PSObject.Properties.Name
$done = 0
foreach ($name in $names) {
    $text = $clips.$name
    $wav = Join-Path $work "$name.wav"
    $ogg = Join-Path $OutDir "$name.ogg"

    $stream = New-Object -ComObject SAPI.SpFileStream
    # Leave the format at the SAPI default; ffmpeg resamples to 16 kHz anyway.
    $stream.Open($wav, 3, $false)   # 3 = SSFMCreateForWrite
    $voice.AudioOutputStream = $stream
    $voice.Speak($text) | Out-Null
    $stream.Close()
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($stream)

    & $Ffmpeg -hide_banner -loglevel error -y -i $wav `
        -c:a libopus -b:a 16k -ac 1 -ar 16000 -frame_duration 60 $ogg
    if ($LASTEXITCODE -ne 0) {
        throw "ffmpeg failed for $name"
    }
    $size = (Get-Item $ogg).Length
    Write-Host ("  {0,-28} {1,6} bytes" -f "$name.ogg", $size)
    $done++
}

[void][Runtime.InteropServices.Marshal]::ReleaseComObject($voice)
Remove-Item $work -Recurse -Force
Write-Host "Generated $done clips into $OutDir"
