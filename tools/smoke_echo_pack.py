# -*- coding: utf-8 -*-
"""打包产物冒烟：网关能不能在冻结环境里把 AEC 拉起来。

为什么单独做这一条：`pywebrtc_audio` 是**延迟 import** 的（在
`EchoCanceller.__init__` 里），PyInstaller 静态分析看不到它。要是没进包，
AEC 会**静默旁路**——功能退化、日志只有一个"初始化失败"、不报错，最容易漏。

流程：临时配置起 exe 的 --gateway → 等 /healthz ready → 带 Device-Id 头
握手建会话 → 查 runtime.log 里的 AEC 就绪行 → 收尾 kill。

跑法：
  C:\\Python314\\python.exe tools/smoke_echo_pack.py [exe 路径]
默认 exe 为打包输出 dist_echo/小D智能助手/小D智能助手.exe。
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

EXE = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                      else r"D:\projects\xiaod-voice\dist_echo"
                           r"\小D智能助手\小D智能助手.exe")
RUN_DIR = os.path.dirname(EXE)
PORT = 8031
TMP = tempfile.mkdtemp(prefix="echosmoke_")
CFG = os.path.join(TMP, "gw.json")
LOG = os.path.join(RUN_DIR, "runtime.log")
TMPLOG = os.path.join(TMP, "gw_stdout.log")

fails = []
okn = 0


def check(name, cond, extra=""):
    global okn
    if cond:
        okn += 1
        print("  ok   " + name + (f"  [{extra}]" if extra else ""))
    else:
        fails.append(name)
        print("  FAIL " + name + (f"  [{extra}]" if extra else ""))


def read_tail(path, start, limit=200_000):
    """读日志新增部分。

    网关的 stdout 编码取决于它拿到的句柄：直启时是控制台 locale（Windows
    上 GBK），由 gateway_ctl 拉起时才是 UTF-8。所以这里挨个试，别用固定
    encoding —— 编码不对会把断言变成"日志里没有"，看着像功能没生效。
    """
    try:
        with open(path, "rb") as f:
            f.seek(start)
            raw = f.read(limit)
    except Exception:
        return ""
    for enc in ("utf-8", "gbk", "mbcs"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


async def handshake(port):
    import websockets
    url = f"ws://127.0.0.1:{port}/xiaozhi/v1/"
    kw = {"max_size": None}
    try:
        return await _hs(url, "extra_headers", kw)
    except TypeError:
        return await _hs(url, "additional_headers", kw)


async def _hs(url, key, kw):
    import websockets
    extra = {"Device-Id": "echo-smoke-device", "Protocol-Version": "1"}
    async with websockets.connect(url, **{key: extra}, **kw) as ws:
        await ws.send(json.dumps({
            "type": "hello", "version": 1, "transport": "websocket",
            "audio_params": {"format": "opus", "sample_rate": 24000,
                             "channels": 1, "frame_duration": 60},
        }))
        raw = await asyncio.wait_for(ws.recv(), timeout=25)
        return raw


def main():
    if not os.path.isfile(EXE):
        print("找不到打包产物：" + EXE)
        return 1
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump({
            "host": "127.0.0.1", "port": PORT, "ws_path": "/xiaozhi/v1/",
            "echo_gate": True, "echo_gate_cooldown_ms": 700,
            "echo_cancel": "auto", "echo_aec_delay_ms": 180,
        }, f)

    log_start = os.path.getsize(LOG) if os.path.isfile(LOG) else 0
    env = dict(os.environ)
    env["XIAOD_DATA_DIR"] = TMP          # 隔离 gw_overrides.json
    env.pop("XIAOD_PARENT_PID", None)
    env.pop("XIAOD_IDLE_EXIT_S", None)
    with open(TMPLOG, "wb") as out:
        p = subprocess.Popen([EXE, "--gateway", "--config", CFG,
                              "--port", str(PORT), "--host", "127.0.0.1"],
                             cwd=RUN_DIR, stdout=out, stderr=subprocess.STDOUT,
                             env=env)
    try:
        print("1. 等网关就绪（加载离线模型，最长 150s）")
        ready = False
        t0 = time.time()
        while time.time() - t0 < 150:
            if p.poll() is not None:
                break
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{PORT}/healthz", timeout=2) as r:
                    h = json.loads(r.read().decode("utf-8"))
                if h.get("ready"):
                    ready = True
                    print(f"     ready；models={h.get('models')} "
                          f"host={h.get('host')} err={h.get('error')!r}")
                    break
            except Exception:
                pass
            time.sleep(1.0)
        check("网关就绪（离线模型已加载）", ready, f"{time.time() - t0:.0f}s")
        if not ready:
            print(read_tail(TMPLOG, 0, 4000))
            print(read_tail(LOG, log_start, 4000))

        print("2. 握手建会话（AEC 在 VoiceSession 初始化时构造）")
        raw = ""
        try:
            raw = asyncio.run(handshake(PORT))
        except Exception as e:  # noqa: BLE001
            check("设备握手成功", False, f"{type(e).__name__}: {e}")
        if raw:
            try:
                msg = json.loads(raw)
            except Exception:
                msg = {}
            check("握手返回 hello", msg.get("type") == "hello", str(raw)[:60])

        time.sleep(1.5)
        print("3. 冻结环境里 AEC 真的起来了（不是静默旁路）")
        blob = read_tail(LOG, log_start) + read_tail(TMPLOG, 0)
        check("日志出现 [echo] AEC 就绪", "[echo] AEC 就绪" in blob,
              re.findall(r"\[echo\][^\n]{0,90}", blob)[:3].__str__())
        check("没有退化到纯闸门", "退化为纯闸门" not in blob,
              (re.findall(r"\[echo\] 初始化失败[^\n]{0,80}", blob) or [""])[0])
        check("设备接入日志在", "设备接入" in blob)
    finally:
        # 只收掉自己起的那个进程：网关是直启的、没有看门狗，terminate 足够；
        # 不要用 taskkill /IM —— 那会把用户自己正在用的同名实例一起杀掉。
        try:
            p.terminate()
            p.wait(timeout=12)
        except Exception:
            try:
                p.kill()
                p.wait(timeout=8)
            except Exception:
                pass

    print("")
    if fails:
        print(f"SMOKE_FAIL {len(fails)}/{okn + len(fails)}")
        for f in fails:
            print("   - " + f)
        return 1
    print(f"SMOKE_PASS {okn}/{okn}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
