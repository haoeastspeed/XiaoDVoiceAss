# -*- coding: utf-8 -*-
"""网关可连接性回归测试（真起进程、真连端口）。

覆盖 2026-10-16 用户反馈的三个问题：
  1. 旧配置写死 host=127.0.0.1 → ESP32 等设备"连接被拒绝"
     ⇒ ensure() 必须能自愈：发现只监听本机就重启为局域网可达
  2. 网关要能自报状态（GET /healthz）——界面据此显示"离线模型已加载"
  3. 设备式握手（路径 /xiaozhi/v1/ + Device-Id 头）在**局域网地址**上必须成功

跑法：
    python tests/test_gateway_lan.py        （退出码 0 = 全通过）
需要本机装了依赖（websockets / sherpa_onnx / onnxruntime / edge-tts 可选）。
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import gateway_ctl as gctl  # noqa: E402

PORT = 18011
MODEL_ASR = ("D:/models/"
             "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09")
MODEL_VAD = "D:/models/snakers4_silero-vad"

pass_n = 0
fails: list[str] = []


def ok(name: str, cond: bool, extra: str = "") -> None:
    global pass_n
    if cond:
        pass_n += 1
        print("  ok   " + name)
    else:
        fails.append(name)
        print("  FAIL " + name + ((" — " + extra) if extra else ""))


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return "127.0.0.1"
    finally:
        s.close()


def write_cfg(path: Path, host: str) -> None:
    path.write_text(json.dumps({
        "host": host, "port": PORT,
        "llm_model": "qwen3.5:9b-q4km",
        "tts_voice": "zh-CN-XiaoxiaoNeural",
        "tts_engine": "auto",
        "system_prompt": "你是小D。",
        "sensevoice_dir": MODEL_ASR,
        "silero_dir": MODEL_VAD,
        "web_search": False,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def wait_port(port: int, timeout: float = 60.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if gctl.port_up(port, 0.5):
            return True
        time.sleep(0.4)
    return False


def spawn(host: str, cfg: Path) -> subprocess.Popen:
    write_cfg(cfg, host)
    env = os.environ.copy()
    env["XIAOD_IDLE_EXIT_S"] = "0"
    env.pop("XIAOD_PARENT_PID", None)
    env["PYTHONUNBUFFERED"] = "1"
    log = open(cfg.with_suffix(".log"), "a", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, str(ROOT / "app" / "main.py"), "--gateway",
         "--config", str(cfg)],
        env=env, stdout=log, stderr=subprocess.STDOUT,
        cwd=str(ROOT))


def esp32_handshake(url: str) -> str:
    import websockets  # 延迟导入：没装依赖时给出清晰错误

    async def _go() -> str:
        headers = {"Device-Id": "aa:bb:cc:dd:ee:ff", "Client-Id": "regress",
                   "Protocol-Version": "1"}
        try:
            kw = {"extra_headers": headers}
            async with websockets.connect(url, ping_interval=None,
                                          open_timeout=8, **kw) as ws:
                await ws.send(json.dumps({
                    "type": "hello", "device_id": "esp32-regress",
                    "audio_params": {"format": "opus", "sample_rate": 24000,
                                     "channels": 1, "frame_duration": 60}}))
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=25))
                return str(m.get("type") or "")
        except TypeError:
            # 老版 websockets 用 additional_headers
            headers2 = dict(headers)
            async with websockets.connect(url, ping_interval=None,
                                          open_timeout=8,
                                          additional_headers=headers2) as ws:
                await ws.send(json.dumps({
                    "type": "hello", "device_id": "esp32-regress",
                    "audio_params": {"format": "opus", "sample_rate": 24000,
                                     "channels": 1, "frame_duration": 60}}))
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=25))
                return str(m.get("type") or "")
        except Exception as e:  # noqa: BLE001
            return f"error:{type(e).__name__}:{e}"
    return asyncio.run(_go())


def main() -> int:
    print("== 网关可连接性回归 ==")
    tmp = Path(tempfile.mkdtemp(prefix="xiaod-gwtest-"))
    cfg = tmp / "gateway.json"
    proc = None
    try:
        # ---- 1. 故意用"仅本机"启动：复现 ESP32 连不上的现场 ----
        proc = spawn("127.0.0.1", cfg)
        up = wait_port(PORT)
        ok("网关（仅本机）已起监听", up)
        addr = gctl.listen_addr(PORT)
        ok("复现：只监听 127.0.0.1", addr.startswith("127."), f"addr={addr}")
        ok("复现：局域网地址连接被拒绝",
           esp32_handshake(f"ws://{lan_ip()}:{PORT}/xiaozhi/v1/").startswith(
               "error:"),
           "本机地址却连上了？环境不适用")

        # ---- 2. ensure() 自愈：改成 0.0.0.0 后应重启并可被局域网访问 ----
        user_cfg = {"gateway_port": PORT, "gateway_host": "0.0.0.0",
                    "model": "qwen3.5:9b-q4km",
                    "voice": "zh-CN-XiaoxiaoNeural", "tts_engine": "auto",
                    "prompt": "你是小D。", "web_search": False,
                    "sensevoice_dir": MODEL_ASR, "silero_dir": MODEL_VAD}
        good, msg = gctl.ensure(user_cfg, wait_s=60)
        ok("ensure() 自愈成功（重启为局域网可达）", good, msg)
        addr = gctl.listen_addr(PORT)
        ok("监听地址已变为 0.0.0.0", addr == "0.0.0.0", f"addr={addr}")

        # ---- 3. /healthz 自报状态（界面据此显示模型是否加载） ----
        h = None
        for _ in range(160):
            h = gctl.health(PORT, 2.0)
            if h and (h.get("ready") or h.get("error")):
                break
            time.sleep(0.5)
        ok("网关有 /healthz 健康接口", isinstance(h, dict), str(h)[:120])
        if isinstance(h, dict):
            ok("健康接口报告 host=0.0.0.0", h.get("host") == "0.0.0.0",
               str(h.get("host")))
            ok("健康接口带 model 就绪状态", isinstance(h.get("models"), dict))
            if Path(MODEL_ASR).is_dir() and Path(MODEL_VAD).is_dir():
                m = h.get("models") or {}
                ok("离线模型已加载（ASR+VAD）",
                   bool(h.get("ready")) and m.get("asr") and m.get("vad"),
                   json.dumps(h, ensure_ascii=False)[:160])
            else:
                print("  skip 离线模型目录不存在，跳过就绪断言")

        # ---- 4. 设备式握手：局域网地址必须成功 ----
        r = esp32_handshake(f"ws://{lan_ip()}:{PORT}/xiaozhi/v1/")
        ok("局域网地址 + Device-Id 握手成功", r == "hello", r)
        r2 = esp32_handshake(f"ws://127.0.0.1:{PORT}/xiaozhi/v1/")
        ok("本机地址握手依然成功", r2 == "hello", r2)
    finally:
        try:
            gctl.stop_any(PORT, timeout=6.0)
        except Exception:  # noqa: BLE001
            pass
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        try:
            for f in tmp.glob("*"):
                f.unlink()
            tmp.rmdir()
        except Exception:  # noqa: BLE001
            pass

    print("")
    if fails:
        print(f"GATEWAY_FAIL {len(fails)}/{pass_n + len(fails)}")
        for f in fails:
            print("   - " + f)
        return 1
    print(f"GATEWAY_PASS {pass_n}/{pass_n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
