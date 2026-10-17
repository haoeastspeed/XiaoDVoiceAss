# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：小D语音陪聊助手（单文件、无控制台窗口）。

在仓库根目录执行：
    pyinstaller app/build/xiaod.spec --noconfirm

产物：dist/小D语音陪聊助手.exe
"""
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

SPEC_DIR = os.path.abspath(SPECPATH)
REPO = os.path.abspath(os.path.join(SPEC_DIR, "..", ".."))
APP_DIR = os.path.join(REPO, "app")

# ---------- 资源 ----------
datas = [
    (os.path.join(APP_DIR, "ui"), os.path.join("app", "ui")),
]
binaries = []

# opus.dll（网关与客户端编解码都要用）
for cand in (os.path.join(REPO, "reference", "bin", "opus.dll"),):
    if os.path.isfile(cand):
        binaries.append((cand, "bin"))

# Silero VAD 模型（约 2MB）：外部模型目录缺失时用它兜底
for cand in ("D:/models/snakers4_silero-vad/src/silero_vad/data/silero_vad.onnx",
             "D:/models/silero_vad.onnx"):
    if os.path.isfile(cand):
        datas.append((cand, "."))
        break

# ---------- 隐式导入 ----------
hiddenimports = [
    "app", "app.main", "app.api", "app.app_config", "app.devices",
    "app.gateway_ctl", "app.voice_client",
    "gateway", "gateway.config", "gateway.server", "gateway.session",
    "gateway.protocol", "gateway.audio.opus", "gateway.audio.aec",
    # 网关侧回声消除用的 WebRTC APM（延迟 import，静态分析看不到）
    "pywebrtc_audio",
    "gateway.providers.asr_sensevoice", "gateway.providers.llm_ollama",
    "gateway.providers.tts_edge", "gateway.providers.vad_silero",
    "gateway.providers.tts_sapi", "gateway.providers.tts_router",
    "gateway.providers.websearch", "gateway.providers.base",
    # 在线大模型后端 / 智能家居（都是按配置**延迟导入**的，静态分析看不到）
    "gateway.providers.llm_openai", "gateway.providers.homeassistant",
    # ---- MCP 工具生态 / 二进制帧 / HTTP 端点 / 传输（全部延迟导入）----
    "gateway.tools", "gateway.tools.types", "gateway.tools.registry",
    "gateway.tools.device", "gateway.tools.endpoint", "gateway.tools.builtin",
    "gateway.tools.loop",
    "gateway.binframe", "gateway.binframe.codec",
    "gateway.http", "gateway.http.httpd", "gateway.http.ota",
    "gateway.http.vision", "gateway.auth",
    "gateway.transport", "gateway.transport.base", "gateway.transport.udp",
    "gateway.transport.mqtt",
    # ---- P2 能力（声纹 / 视觉 / Realtime / 唤醒词）----
    "gateway.providers.voiceprint", "gateway.providers.llm_vision",
    "gateway.providers.llm_realtime", "gateway.providers.camera",
    "app.wakeword",
    # miniaudio：进程内 MP3 解码（替代 ffmpeg，见 tts_edge 注释）
    "miniaudio", "_miniaudio",
    "sounddevice", "opuslib_next", "edge_tts", "edge_tts.communicate",
    "httpx", "websockets", "websockets.asyncio.server", "numpy",
    "webview.platforms.edgechromium", "clr_loader", "pythonnet",
    "clr_loader.util.find", "comtypes",
]

# AES-CTR（UDP 音频）：pycryptodome 或 cryptography，至少装一个
hiddenimports += ["Crypto", "Crypto.Cipher", "Crypto.Cipher.AES"]
try:
    import cryptography  # noqa: F401
    hiddenimports += ["cryptography", "cryptography.hazmat.primitives.ciphers"]
except Exception:
    pass
# MQTT 传输（可选，装了才打包）
try:
    import paho.mqtt.client  # noqa: F401
    hiddenimports += ["paho", "paho.mqtt", "paho.mqtt.client"]
except Exception:
    pass
# 唤醒词（可选，装了才打包）
try:
    import openwakeword  # noqa: F401
    hiddenimports += ["openwakeword", "openwakeword.model"]
except Exception:
    pass

# 摄像头视觉（OpenCV 约 60MB）：默认不打进包，需要时设 XIAOD_WITH_VISION=1
_WITH_VISION = os.environ.get("XIAOD_WITH_VISION", "0") == "1"
if _WITH_VISION:
    hiddenimports.append("cv2")
hiddenimports += collect_submodules("webview")

# 原生库体量较大，直接整包收集，避免漏 DLL；但要剔掉用不上的东西
_SKIP_EXT = (".lib", ".exp", ".pdb")          # 链接期产物，运行时不需要
_SKIP_DIR = ("transformers", "tools", "quantization", "test", "training")
for pkg in ("sherpa_onnx", "onnxruntime", "pywebrtc_audio"):
    try:
        d, b, h = collect_all(pkg)
        for src, dst in d:
            if not any(part in dst.replace("\\", "/").split("/")
                       for part in _SKIP_DIR):
                datas.append((src, dst))
        for src, dst in b:
            low = src.lower()
            if low.endswith(_SKIP_EXT):
                continue
            if any(part in dst.replace("\\", "/").split("/")
                   for part in _SKIP_DIR):
                continue
            binaries.append((src, dst))
        hiddenimports += [m for m in h
                          if not any(f".{k}" in m for k in _SKIP_DIR)]
    except Exception:
        pass

# ---------- 排除 ----------
_EXCLUDES = [
    "tkinter", "matplotlib", "PyQt5", "PyQt6", "PySide2", "PySide6",
    "pandas", "scipy", "IPython", "jupyter", "notebook", "pytest",
    "setuptools", "pip", "torch", "tensorflow",
    # 下面这些是 collect_all/隐式依赖顺带拖进来的，运行期用不到：
    # PIL/lxml 各占十几 MB，排除后单文件解包明显更快
    "PIL", "lxml", "sympy", "transformers", "onnxruntime.transformers",
    "onnxruntime.tools", "onnxruntime.quantization", "onnxruntime.training",
    "webview.platforms.gtk", "webview.platforms.qt",
    "webview.platforms.cef", "webview.platforms.cocoa",
    "webview.platforms.android", "webview.platforms.mshtml",
    "gi", "cairo", "gi.repository", "PyGObject",
]
if not _WITH_VISION:
    # 没开视觉就不打 OpenCV（约 60MB）。要摄像头看图就设 XIAOD_WITH_VISION=1
    _EXCLUDES.append("cv2")

# ---------- 分析 ----------
a = Analysis(
    [os.path.join(REPO, "run_assistant.py")],
    pathex=[REPO],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=_EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)

_ONEDIR = os.environ.get("XIAOD_BUILD_MODE", "onefile").lower() == "onedir"

if _ONEDIR:
    # 极速版：不做自解包，启动只要几百毫秒（推荐日常使用）
    exe = EXE(
        pyz, a.scripts, [], exclude_binaries=True,
        name="小D智能助手", debug=False, strip=False, upx=False,
        console=False, disable_windowed_traceback=False,
        icon=os.path.join(APP_DIR, "ui", "assets", "icon.ico"),
    )
    coll = COLLECT(
        exe, a.binaries, a.datas,
        strip=False, upx=False, name="小D智能助手",
    )
else:
    exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="小D智能助手",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 关键：不弹黑色控制台窗口
    disable_windowed_traceback=False,
    icon=os.path.join(APP_DIR, "ui", "assets", "icon.ico"),
)
