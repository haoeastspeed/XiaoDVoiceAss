# -*- coding: utf-8 -*-
"""Ollama 伴随服务：软件启动时检测并自动拉起 `ollama serve`。

为什么要在客户端里管这件事：模型列表、离线对话、网关的大模型预热全都依赖
Ollama 在跑，而用户视角里"Ollama 没启动"就是"软件坏了"。装好 Ollama 的
机器上它只是个后台服务，这里替用户把它带起来，失败（没装/起不来）时把
明确原因交还给界面。

注意：
  * 访问 localhost 必须绕代理（httpx 一律 trust_env=False），否则挂在
    系统代理上的 127.0.0.1 请求会直接失败，表现为"明明在跑却检测不到"。
  * 拉起用 DETACHED_PROCESS + CREATE_NO_WINDOW：不弹黑窗、不随本进程退出。
"""
from __future__ import annotations

import os
import subprocess
import time
from typing import Optional, Tuple

_PROC: Optional[subprocess.Popen] = None


def _exe() -> str:
    """定位 ollama.exe：PATH → 常见安装位置。"""
    import shutil
    p = shutil.which("ollama")
    if p:
        return p
    cands = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     "Programs", "Ollama", "ollama.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""),
                     "Ollama", "ollama.exe"),
        "D:/Program Files/Ollama/ollama.exe",
        "C:/Ollama/ollama.exe",
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


def reachable(base_url: str, timeout: float = 1.5) -> bool:
    try:
        import httpx
        r = httpx.get(f"{base_url.rstrip('/')}/api/tags",
                      timeout=timeout, trust_env=False)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def ensure(base_url: str = "", wait_s: float = 12.0,
           logger=None) -> Tuple[bool, str]:
    """确保 Ollama 可用：在跑就直接返回，没跑就拉起并等它就绪。

    返回 (ok, msg)；msg 供界面/日志直接展示。"""
    base = (base_url or "http://localhost:11434").rstrip("/")
    log = logger or (lambda m: None)
    if reachable(base):
        return True, "Ollama 已在运行"
    exe = _exe()
    if not exe:
        return False, ("未找到 Ollama，请安装后重试"
                       "（https://ollama.com）或手动启动")
    global _PROC
    try:
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 | getattr(subprocess, "DETACHED_PROCESS", 0))
        # 双保险：即使某些 Windows 版本对 CREATE_NO_WINDOW 处理不一致，
        # 也用 STARTUPINFO 把窗口强制隐藏，绝不弹黑窗。
        si = None
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
            si.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        _PROC = subprocess.Popen(
            [exe, "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags, startupinfo=si)
        log(f"已拉起 ollama serve：{exe} (pid={_PROC.pid})")
    except Exception as e:  # noqa: BLE001
        return False, f"启动 Ollama 失败：{e}"
    deadline = time.monotonic() + max(3.0, wait_s)
    while time.monotonic() < deadline:
        if reachable(base, timeout=1.0):
            return True, "Ollama 已启动"
        time.sleep(0.4)
    return False, "Ollama 启动超时，请手动打开 Ollama 后重试"
