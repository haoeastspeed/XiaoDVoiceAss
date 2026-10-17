# -*- coding: utf-8 -*-
"""本轮 UI 修复回归：启动状态兜底 / Ollama 伴随 / 试听走网关 / 引擎随音色。

跑法：
    C:/Python314/python.exe tests/test_ui_fixes.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["PATH"] = (os.path.join(ROOT, "reference", "bin") + os.pathsep
                      + os.environ.get("PATH", ""))

PASS = 0
FAIL: list[str] = []


def ok(name: str, cond, note: str = "") -> bool:
    global PASS
    if cond:
        PASS += 1
        print(f"  ok   {name}" + (f"  {note}" if note else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}" + (f"  {note}" if note else ""))
    return bool(cond)


# ------------------------------------------------ 1. 启动状态：准备中兜底
def t_status() -> None:
    print("=" * 72)
    print("1) 网关状态：prepare 未跑之前不报'未运行'")
    from app.api import Api
    api = Api()
    api.cfg["auto_start_gateway"] = True
    api.cfg["gateway_port"] = 59999          # 保证探不到
    st = api._gw_status()
    ok("prepare 前显示准备中", (not st["up"]) and st["action"] == "starting"
       and st["kind"] == "准备中", f"{st['kind']}/{st['action']}")
    api._gw_prepare_started = True
    st2 = api._gw_status()
    ok("prepare 已跑且没起 → 如实显示未运行",
       st2["kind"] == "未运行" and st2["action"] == "", f"{st2['kind']}")
    api.cfg["auto_start_gateway"] = False
    api._gw_prepare_started = False
    st3 = api._gw_status()
    ok("关闭自动启动时如实显示未运行", st3["kind"] == "未运行",
       f"{st3['kind']}")
    # 竞态回归：gateway_prepare 内部先做端口探测（最长 1.2s）后才置
    # "启动中"的话，轮询会落进窗口看到一瞬间的"未运行"。
    # ensure 慢 2s，0.3s 时状态必须是 starting，不允许出现 未运行。
    api2 = Api()
    api2.cfg["auto_start_gateway"] = True
    api2.cfg["gateway_port"] = 59997
    from app import gateway_ctl
    real_ensure = gateway_ctl.ensure
    gateway_ctl.ensure = lambda cfg, wait_s=45.0: (time.sleep(2.0) or
                                                   (False, "stub"))
    try:
        th = threading.Thread(target=api2.gateway_prepare, daemon=True)
        th.start()
        time.sleep(0.3)
        st4 = api2._gw_status()
        ok("prepare 探测窗口内仍是启动中（无未运行闪跳）",
           st4["action"] == "starting" and st4["kind"] != "未运行",
           f"{st4['kind']}/{st4['action']}")
        th.join(timeout=5)
    finally:
        gateway_ctl.ensure = real_ensure


# ------------------------------------------------ 2. Ollama 伴随服务
def t_ollama() -> None:
    print("=" * 72)
    print("2) Ollama：能定位 exe；ensure 幂等且不误报")
    from app import ollama_ctl
    exe = ollama_ctl._exe()
    ok("能定位 ollama.exe", bool(exe), exe[-40:] if exe else "未找到")
    # reachable 对一个必然不通的端口要返回 False（代理环境也不许误判）
    ok("对死端口 reachable=False",
       ollama_ctl.reachable("http://127.0.0.1:59998", timeout=0.5) is False)
    ok2, msg = ollama_ctl.ensure("http://127.0.0.1:59998", wait_s=1.0,
                                 logger=lambda m: None)
    # 59998 上没有 ollama，serve 起来也不会让它监听这个端口 → 必然 False
    ok("对错误端口 ensure 如实失败", ok2 is False, msg[:40])


# ------------------------------------------------ 3. 试听走网关 HTTP
def _start_http(port: int):
    from gateway.config import GatewayConfig
    from gateway.http.httpd import GatewayHTTP
    from gateway.http.tts import TTSPreviewHandler
    cfg = GatewayConfig()
    cfg.host = "127.0.0.1"
    cfg.http_port = port
    app = GatewayHTTP(cfg)
    TTSPreviewHandler(cfg).register(app)
    t = threading.Thread(target=app.start, daemon=True)
    t.start()
    for _ in range(50):
        if app._server is not None:
            break
        time.sleep(0.1)
    return app


def t_preview_http() -> None:
    print("=" * 72)
    print("3) /tts/preview/：网关进程合成返回合法 WAV")
    port = 58123
    app = _start_http(port)
    ok("HTTP 服务起来了", app._server is not None)
    # 缺 voice → 400
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/tts/preview/",
        data=json.dumps({}).encode(), method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
        ok("缺 voice 应 400", False)
    except urllib.error.HTTPError as e:
        ok("缺 voice 应 400", e.code == 400)
    # 正常：离线音色
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/tts/preview/",
        data=json.dumps({"voice": "local:zf_xiaoxiao"}).encode(),
        method="POST")
    import wave as _wave
    import io as _io
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = resp.read()
        eng = resp.headers.get("X-Engine", "")
        ctype = resp.headers.get("Content-Type", "")
    ok("返回 audio/wav", ctype.startswith("audio/wav"), ctype)
    with _wave.open(_io.BytesIO(body), "rb") as w:
        rate, frames = w.getframerate(), w.getnframes()
    ok("采样率 24k 且有音频", rate == 24000 and frames > 8000,
       f"rate={rate} frames={frames}")
    ok("引擎标记 local", eng == "local", eng)
    # 在线音色 + 引擎 auto（不校验能否联网，只看端点结构是否正常响应）
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/tts/preview/",
        data=json.dumps({"voice": "zh-CN-XiaoxiaoNeural"}).encode(),
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            b2 = resp.read()
            eng2 = resp.headers.get("X-Engine", "")
        ok("在线音色也返回 WAV", resp.status == 200 and len(b2) > 8000,
           f"engine={eng2}")
    except Exception as e:  # noqa: BLE001
        # 断网时 auto 会兜底本地合成，仍应 200；到不了这里才算失败
        ok("在线音色也返回 WAV", False, repr(e)[:80])
    app.stop()


# ------------------------------------------------ 4. 引擎随音色
def t_engine_follow() -> None:
    print("=" * 72)
    print("4) set_voice：引擎跟随音色自动切换")
    from app.api import Api
    api = Api()
    api.cfg["voice"] = ""
    r = api.set_voice("local:zf_xiaoxiao")
    ok("离线音色 → 引擎 local", r.get("ok") and
       api.cfg.get("tts_engine") == "local",
       f"engine={api.cfg.get('tts_engine')}")
    r = api.set_voice("zh-CN-XiaoxiaoNeural")
    ok("在线音色 → 引擎 auto", r.get("ok") and
       api.cfg.get("tts_engine") == "auto",
       f"engine={api.cfg.get('tts_engine')}")
    # 还原，别污染用户配置
    api.cfg["voice"] = "zh-CN-XiaoxiaoNeural"
    api.cfg["tts_engine"] = "auto"


if __name__ == "__main__":
    t_status()
    t_ollama()
    t_preview_http()
    t_engine_follow()
    print("=" * 72)
    print(f"UI_FIXES_{'PASS' if not FAIL else 'FAIL'} {PASS}/{PASS + len(FAIL)}")
    sys.exit(1 if FAIL else 0)
