# -*- coding: utf-8 -*-
"""小D语音陪聊助手 —— 单一入口。

同一个可执行文件承担两个角色：
  * 不带参数：启动图形界面（GUI）；
  * 带 `--gateway`：只跑语音网关服务（GUI 会用同一份 exe 再拉起一个自己，
    这样"单文件"同时满足"网关独立进程 + 随应用启停"）。
"""
from __future__ import annotations

import argparse
import os
import sys


def _bootstrap_path() -> str:
    """把代码根目录与 opus.dll 目录准备好（源码运行 / 打包运行都适用）。"""
    meipass = getattr(sys, "_MEIPASS", "")
    base = meipass or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    if base and base not in sys.path:
        sys.path.insert(0, base)
    here = os.path.dirname(os.path.abspath(__file__))
    for d in filter(None, [os.path.join(meipass, "bin") if meipass else "",
                           meipass, os.path.join(here, "bin"),
                           os.path.join(base, "reference", "bin")]):
        if os.path.isfile(os.path.join(d, "opus.dll")):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            try:
                os.add_dll_directory(d)
            except Exception:  # noqa: BLE001
                pass
            break
    return base


_BASE = _bootstrap_path()

from app import app_config as acfg          # noqa: E402
from app.api import log                     # noqa: E402
from app import gateway_ctl                 # noqa: E402

# 网关读配置/写覆盖文件的目录统一到应用数据目录
os.environ.setdefault("XIAOD_DATA_DIR", acfg.data_dir())
os.environ.setdefault("PYTHONUNBUFFERED", "1")


# ---------- 角色二：网关 ----------

def run_gateway(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="小D语音网关（由助手应用拉起）")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--host", default=None)
    ap.add_argument("--config", default=None)
    a = ap.parse_args(argv)

    import asyncio
    from gateway.config import load_config
    from gateway.server import XiaoDGateway
    cfg = load_config(a.config)
    if a.port:
        cfg.port = a.port
    if a.host:
        cfg.host = a.host
    try:
        asyncio.run(XiaoDGateway(cfg).serve_forever())
    except KeyboardInterrupt:
        pass
    return 0


# ---------- 角色一：图形界面 ----------

def _ui_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")


def _ui_path() -> str:
    return os.path.join(_ui_dir(), "index.html")


def _icon_path() -> str:
    return os.path.join(_ui_dir(), "assets", "icon.ico")


def _apply_window_icon(title: str) -> None:
    """给窗口设置任务栏/标题栏图标。

    pywebview 的 icon 参数在 Windows(EdgeChromium) 上不一定生效，这里直接
    发 WM_SETICON 兜底。放后台线程做，避免阻塞 GUI 启动。"""
    def _work():
        import time as _t
        _t.sleep(0.6)
        try:
            import ctypes
            u32 = ctypes.windll.user32
            ico = _icon_path()
            if not os.path.isfile(ico):
                return
            LR_LOADFROMFILE, IMAGE_ICON, WM_SETICON = 0x10, 1, 0x80
            h = u32.LoadImageW(None, ico, IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
            if not h:
                return
            hwnd = u32.FindWindowW(None, title)
            if not hwnd:
                return
            u32.SendMessageW(hwnd, WM_SETICON, 0, h)   # ICON_SMALL
            u32.SendMessageW(hwnd, WM_SETICON, 1, h)   # ICON_BIG
            log("窗口图标已应用")
        except Exception as e:  # noqa: BLE001
            log(f"设置窗口图标失败：{e!r}")
    try:
        import threading
        threading.Thread(target=_work, daemon=True).start()
    except Exception:  # noqa: BLE001
        pass


def run_gui() -> int:
    import webview
    from app.api import Api

    log("=== 小D语音陪聊助手启动 ===")
    api = Api()
    win = webview.create_window(
        acfg.APP_TITLE,
        url=_ui_path(),
        js_api=api,
        width=1000, height=740,
        min_size=(720, 560),
        background_color="#0f172a",
        text_select=True,
    )
    api._win = win

    def _on_closing():
        """关窗：**立即**停掉网关与通话，绝不在这里等待/操作窗口。

        等待会把处理关闭的线程钉死（窗口关不掉的经典原因）。网关进程收到
        terminate 就会退；即使它没退干净，服务端还有宿主 PID 看门狗自杀。"""
        try:
            if api._client is not None:
                api._client.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            gateway_ctl.kill_now()
        except Exception:  # noqa: BLE001
            pass
        return True

    try:
        win.events.closing += _on_closing
    except Exception:  # noqa: BLE001
        pass

    def _post_start():
        log("webview 已启动，执行初始化")
        _apply_window_icon(acfg.APP_TITLE)
        try:
            api.bootstrap()          # 预热设备/模型/音色列表
        except Exception as e:       # noqa: BLE001
            log(f"bootstrap 预热失败：{e!r}")
        try:
            from app import ollama_ctl
            if str(api.cfg.get("llm_backend") or "ollama") == "ollama":
                ok, msg = ollama_ctl.ensure(
                    str(api.cfg.get("ollama_base_url") or ""), logger=log)
                log(f"Ollama 伴随服务：{msg}")
                if ok:
                    api._refresh_models_async()   # 替换掉"未检测到"的兜底列表
        except Exception as e:  # noqa: BLE001
            log(f"Ollama 伴随服务异常：{e!r}")
        # 启动即拉起网关并加载离线模型（默认开）——与"自动开始通话"解耦，
        # ESP32 等设备随时能连上；不阻塞窗口，后台完成。
        if api.cfg.get("auto_start_gateway", True):
            try:
                log(f"自动启动网关：{api.gateway_prepare()}")
            except Exception as e:  # noqa: BLE001
                log(f"自动启动网关失败：{e!r}")
        if api.cfg.get("always_on_top"):
            try:
                win.on_top = True
            except Exception:  # noqa: BLE001
                pass
        if api.cfg.get("auto_start_call", True):
            try:
                import time as _t
                _t.sleep(0.3)
                r = api.start_call()
                log(f"自动开始通话：{r}")
            except Exception as e:  # noqa: BLE001
                log(f"自动开始通话失败：{e!r}")

    try:
        try:
            webview.start(_post_start, debug=False, icon=_icon_path())
        except TypeError:
            webview.start(_post_start, debug=False)
    except Exception as e:  # noqa: BLE001
        log(f"webview.start 异常: {e!r}")
        raise
    # GUI 已退出：再温柔地等网关收尾
    try:
        gateway_ctl.wait_exit(3.0)
    except Exception:  # noqa: BLE001
        pass
    return 0


def _ensure_std_streams() -> None:
    """打包成 windowed exe 后 sys.stdout/stderr 是 None，print 会直接抛异常
    （网关模式下打印很多，会整个跑不起来）。这里把它们兜到日志文件。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        path = os.path.join(acfg.data_dir(), "runtime.log")
        f = open(path, "a", encoding="utf-8", buffering=1)
    except Exception:  # noqa: BLE001
        import io
        f = io.StringIO()
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


def main() -> int:
    _ensure_std_streams()
    argv = sys.argv[1:]
    if "--gateway" in argv:
        argv = [a for a in argv if a != "--gateway"]
        return run_gateway(argv)
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
