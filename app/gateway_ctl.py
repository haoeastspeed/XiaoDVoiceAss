# -*- coding: utf-8 -*-
"""语音网关进程管理：随应用启动/停止。

打包后是**单文件 exe 自拉起**：把同一个 exe 带上 `--gateway` 参数再跑一份，
那份进程只跑网关服务，不弹窗口。开发环境则用当前解释器跑 app/main.py。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time

from app import app_config as acfg

# 网关的 WebSocket 路径（与 gateway.config.GatewayConfig.ws_path 一致）
WS_PATH = "/xiaozhi/v1/"

# 网关找模型的默认位置（可在应用设置里覆盖）
DEFAULT_SENSEVOICE = (
    "D:/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09")
DEFAULT_SILERO = "D:/models/snakers4_silero-vad"

_proc: subprocess.Popen | None = None
_external = False          # 端口上是外部实例（独立服务/手动启动）→ 不回收
_lock = threading.Lock()


def log(msg: str) -> None:
    """写应用日志（自带小实现，避免与 app.api 循环 import）。"""
    try:
        import datetime as _d
        with open(os.path.join(acfg.data_dir(), "assistant.log"), "a",
                  encoding="utf-8") as f:
            f.write(f"[{_d.datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def _here() -> str:
    """app 包所在目录（打包后为解包目录）。"""
    return os.path.dirname(os.path.abspath(__file__))


def repo_root() -> str:
    return os.path.dirname(_here())


def gateway_config_path() -> str:
    return os.path.join(acfg.data_dir(), "gateway.json")


def write_gateway_config(cfg: dict) -> str:
    """把界面上的设置写成网关能读的 JSON。"""
    data = {
        # 关键：默认 0.0.0.0（局域网可达）。写死 127.0.0.1 会让 ESP32 等设备
        # 连接被"积极拒绝"——现象是设备一直连不上，而本机聊天完全正常。
        "host": _want_host(cfg),
        "port": int(cfg.get("gateway_port", 8000)),
        "llm_model": cfg.get("model"),
        "tts_voice": cfg.get("voice"),
        "tts_engine": cfg.get("tts_engine", "auto"),
        "system_prompt": cfg.get("prompt"),
        "sensevoice_dir": cfg.get("sensevoice_dir") or DEFAULT_SENSEVOICE,
        "silero_dir": cfg.get("silero_dir") or DEFAULT_SILERO,
        "ollama_base_url": cfg.get("ollama_base_url") or "http://localhost:11434",
        "echo_dedup": True,
        "echo_dedup_window_s": 8.0,
        # 回声门 / 网关侧 AEC（防设备被自己的回复唤醒，详见 gateway/audio/aec.py）
        "echo_gate": bool(cfg.get("echo_gate", True)),
        "echo_gate_cooldown_ms": int(cfg.get("echo_gate_cooldown_ms", 700) or 0),
        "echo_cancel": str(cfg.get("echo_cancel", "auto") or "auto"),
        "echo_aec_delay_ms": int(cfg.get("echo_aec_delay_ms", 180) or 180),
        "web_search": bool(cfg.get("web_search", False)),
        "web_search_max": int(cfg.get("web_search_max", 4) or 4),
        # ---- 语言模型后端（本地 Ollama / 在线 OpenAI 兼容）----
        "llm_backend": str(cfg.get("llm_backend") or "ollama"),
        "llm_api_base": str(cfg.get("llm_api_base") or ""),
        "llm_api_key": str(cfg.get("llm_api_key") or ""),
        "llm_preset": str(cfg.get("llm_preset") or ""),
        # ---- 识别语言（SenseVoice 原生支持 auto/zh/yue/en/ja/ko）----
        "asr_language": str(cfg.get("asr_language") or "auto"),
        # ---- 记忆模式 ----
        "memory_mode": str(cfg.get("memory_mode") or "full"),
        "memory_summary_model": str(cfg.get("memory_summary_model") or ""),
        # ---- 自动休眠（秒，0=不休眠）----
        "idle_sleep_s": int(cfg.get("idle_sleep_s", 300) or 0),
        # ---- 智能家居（HomeAssistant）----
        "ha_enabled": bool(cfg.get("ha_enabled", False)),
        "ha_base_url": str(cfg.get("ha_base_url") or ""),
        "ha_token": str(cfg.get("ha_token") or ""),
        # ---- MCP 工具生态 / 工具调用（对齐小智官方）----
        "mcp_enabled": bool(cfg.get("mcp_enabled", True)),
        "mcp_endpoints": cfg.get("mcp_servers") or {},
        "tools_first": bool(cfg.get("tools_first", False)),
        "tool_loop_max_turns": int(cfg.get("tool_loop_max_turns", 4) or 4),
        "tool_ack_tts": bool(cfg.get("tool_ack_tts", True)),
        # ---- HTTP 端点（OTA / 视觉）----
        "http_port": int(cfg.get("http_port", 8003) or 0),
        "ota_enabled": bool(cfg.get("ota_enabled", True)),
        "auth_enabled": bool(cfg.get("auth_enabled", False)),
        "auth_key": str(cfg.get("auth_key") or ""),
        # ---- 声纹 / 视觉 / 实时语音（P2）----
        "voiceprint_enabled": bool(cfg.get("voiceprint_enabled", False)),
        "voiceprint_mode": str(cfg.get("voiceprint_mode") or "remote"),
        "voiceprint_base_url": str(cfg.get("voiceprint_base_url") or ""),
        "voiceprint_key": str(cfg.get("voiceprint_key") or ""),
        "vision_enabled": bool(cfg.get("vision_enabled", False)),
        "vision_camera_index": int(cfg.get("vision_camera_index", 0) or 0),
        "vision_model": str(cfg.get("vision_model") or ""),
        "realtime_enabled": bool(cfg.get("realtime_enabled", False)),
        "realtime_base_url": str(cfg.get("realtime_base_url") or ""),
        "realtime_api_key": str(cfg.get("realtime_api_key") or ""),
        "realtime_model": str(cfg.get("realtime_model")
                              or "gpt-4o-realtime-preview"),
    }
    path = gateway_config_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def _want_host(cfg: dict) -> str:
    """用户配置的监听地址（缺省 0.0.0.0）。"""
    host = str(cfg.get("gateway_host") or "0.0.0.0").strip()
    return host or "0.0.0.0"


def _want_lan(cfg: dict) -> bool:
    """是否希望局域网设备能连（host 不是回环地址就是希望）。"""
    return _want_host(cfg) not in ("127.0.0.1", "localhost", "::1")


def endpoints(port: int) -> dict:
    """网关的完整访问地址。

    设备配网（ESP32 / 小智固件）要填的就是这里的 `ws_lan`，界面必须原样
    显示完整 URL —— 之前只显示一个端口号，用户还得自己拼 `ws://IP:端口/路径`，
    少打个斜杠就连不上。
    """
    path = WS_PATH if WS_PATH.endswith("/") else WS_PATH + "/"
    lan = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))      # 不发包，只为拿到出口网卡 IP
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                lan = ip
        finally:
            s.close()
    except Exception:  # noqa: BLE001
        pass
    return {
        "ws_path": path,
        "ws_local": f"ws://127.0.0.1:{port}{path}",
        "ws_lan": f"ws://{lan}:{port}{path}" if lan else "",
        "lan_ip": lan,
        "http_lan": f"http://{lan}:{port}" if lan else "",
    }


def port_up(port: int, timeout: float = 1.0) -> bool:
    """探活：发规范 HTTP 请求（裸 connect 会让网关打一堆握手异常堆栈）。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                      b"Connection: close\r\n\r\n")
            got = s.recv(64)
            while got and len(got) < 2048:
                more = s.recv(512)
                if not more:
                    break
                got += more
            return bool(got)
    except OSError:
        return False


def health(port: int, timeout: float = 1.5) -> dict | None:
    """向网关要一次自报状态（GET /healthz）。

    返回 {host, port, ready, models:{asr,vad,llm,tts}, error, ...}；
    老版本网关没有这个端点（返回 400/解析失败）→ None。
    """
    try:
        with socket.create_connection(("127.0.0.1", port),
                                      timeout=timeout) as s:
            s.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                      b"Connection: close\r\n\r\n")
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
                if len(data) > 65536:
                    break
        body = data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else b""
        obj = json.loads(body.decode("utf-8", "replace").strip())
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _build_cmd(cfg_path: str, port: int) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--gateway", "--port", str(port),
                "--config", cfg_path]
    return [sys.executable, os.path.join(_here(), "main.py"), "--gateway",
            "--port", str(port), "--config", cfg_path]


_firewall_done = False


def ensure_firewall(port: int) -> None:
    """best-effort 放行入站端口（局域网设备才能连）。

    需要管理员权限；失败只记一行日志，不打断启动。只在第一次尝试。
    """
    global _firewall_done
    if _firewall_done or os.name != "nt":
        return
    _firewall_done = True
    name = f"小D智能助手 语音网关 {port}"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        cp = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule",
             f"name={name}"],
            capture_output=True, timeout=8, creationflags=flags)
        if cp.returncode == 0 and (cp.stdout or b"").strip():
            return                     # 规则已存在
        cp = subprocess.run(
            ["netsh", "advfirewall", "firewall", "add", "rule",
             f"name={name}", "dir=in", "action=allow", "protocol=TCP",
             f"localport={port}"],
            capture_output=True, timeout=10, creationflags=flags)
        if cp.returncode != 0:
            log(f"未能自动放行防火墙端口 {port}（需要管理员权限）。"
                f"设备连不上时请手动允许入站 TCP {port}。")
    except Exception as e:  # noqa: BLE001
        log(f"防火墙放行尝试失败：{e!r}")


def ensure(cfg: dict, wait_s: float = 45.0) -> tuple[bool, str]:
    """确保网关在跑：已在跑就复用；旧实例只监听本机时自动重启为局域网可达。

    设备连不上最常见的原因就是旧配置写死了 127.0.0.1（本机聊天一切正常，
    ESP32 却一直"连接被拒绝"）。这里探一次监听地址，发现是回环地址就换掉。
    """
    global _proc
    port = int(cfg.get("gateway_port", 8000))
    want_lan = _want_lan(cfg)
    if port_up(port):
        addr = listen_addr(port)
        if want_lan and addr and addr.startswith("127."):
            log(f"网关仅监听本机（{addr}），设备无法连接 → 重启为局域网可达")
            stop_any(port, timeout=6.0)
            time.sleep(0.3)
        else:
            _mark_external()
            return True, "已复用运行中的网关"
    with _lock:
        if _proc is not None and _proc.poll() is None:
            pass  # 自己拉起的还在启动中，往下等待即可
        else:
            cfg_path = write_gateway_config(cfg)
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["XIAOD_IDLE_EXIT_S"] = "0"          # 生命周期由本应用掌握
            env["XIAOD_PARENT_PID"] = str(os.getpid())  # 应用挂了网关自动退出
            flags = 0
            if os.name == "nt":
                flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                         | getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            try:
                logf = open(os.path.join(acfg.data_dir(), "gateway.log"),
                            "a", encoding="utf-8")
            except Exception:  # noqa: BLE001
                logf = subprocess.DEVNULL
            try:
                _proc = subprocess.Popen(
                    _build_cmd(cfg_path, port), env=env, creationflags=flags,
                    stdin=subprocess.DEVNULL, stdout=logf,
                    stderr=subprocess.STDOUT, close_fds=True,
                    cwd=acfg.data_dir())
            except Exception as e:  # noqa: BLE001
                return False, f"网关启动失败：{e}"
            if want_lan:
                # 让 Windows 放行入站端口，否则设备能过路由却过不了防火墙
                threading.Thread(target=ensure_firewall, args=(port,),
                                 daemon=True).start()
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if port_up(port):
                return True, "网关已就绪"
            if _proc is not None and _proc.poll() is not None:
                return False, ("网关进程已退出，请查看日志："
                               + os.path.join(acfg.data_dir(), "gateway.log"))
            time.sleep(0.5)
    return False, "网关启动超时（首次加载模型可能需要更久，请重试）"


def _mark_external() -> None:
    global _external
    if _proc is None:
        _external = True


def kill_now() -> None:
    """发出终止信号后立刻返回（不在 UI/closing 线程里等待）。"""
    p = _proc
    if p is None or p.poll() is not None:
        return
    try:
        p.terminate()
    except Exception:  # noqa: BLE001
        pass


def wait_exit(timeout: float = 6.0) -> bool:
    """等网关退出；超时就强杀。返回是否已退出。"""
    global _proc
    p = _proc
    _proc = None
    if p is None:
        return True
    try:
        p.wait(timeout=timeout)
        return True
    except Exception:  # noqa: BLE001
        try:
            p.kill()
        except Exception:  # noqa: BLE001
            pass
        return False


def stop(timeout: float = 6.0) -> None:
    """停止本应用拉起的网关（如果端口上是别人的实例则不动）。"""
    kill_now()
    wait_exit(timeout)


def _listeners(port: int) -> list[tuple[str, int]]:
    """该端口的 (监听地址, PID) 列表（Windows netstat）。

    注意：中文 Windows 的 netstat 输出是 GBK，不能用 text=True（会按 UTF-8
    解码并抛 UnicodeDecodeError），必须按字节读回来自己解。
    """
    if os.name != "nt":
        return []
    raw = b""
    try:
        cp = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                            capture_output=True, timeout=8,
                            creationflags=getattr(subprocess,
                                                  "CREATE_NO_WINDOW", 0))
        raw = cp.stdout or b""
    except Exception:  # noqa: BLE001
        return []
    out = ""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            out = raw.decode(enc)
            break
        except Exception:  # noqa: BLE001
            continue
    if not out:
        out = raw.decode("utf-8", errors="replace")
    res: list[tuple[str, int]] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if parts[3].upper() != "LISTENING":
            continue
        local = parts[1]
        if not local.endswith(":" + str(port)):
            continue
        addr = local.rsplit(":", 1)[0].strip("[]")
        try:
            pid = int(parts[4])
        except ValueError:
            continue
        res.append((addr, pid))
    return res


def listen_addr(port: int) -> str:
    """网关实际监听的地址（0.0.0.0 = 局域网可达；127.0.0.1 = 仅本机）。"""
    for addr, _pid in _listeners(port):
        if addr:
            return addr
    return ""


def _listener_pid(port: int) -> int | None:
    """找出监听该端口的进程 PID（跳过自己）。"""
    me = os.getpid()
    for _addr, pid in _listeners(port):
        if pid and pid != me:
            return pid
    return None


def stop_any(port: int, timeout: float = 8.0) -> tuple[bool, str]:
    """停止网关：先停本应用拉起的；仍占端口就按端口找到监听进程停掉。

    用于设置里的「停止网关」——即使端口上跑的是手动启动/独立服务实例，
    用户点了停止也应该真的停掉。"""
    stop(timeout=3.0)
    if not port_up(port, 0.6):
        return True, "网关已停止"
    pid = _listener_pid(port)
    if not pid:
        return False, "找不到监听该端口的进程（可能端口被其它程序占用）"
    # 端口被白名单外的程序占用时不要动手，避免误杀
    name = _image_name(pid)
    if name and name.lower() not in [x.lower() for x in _ALLOW_IMAGE]:
        return False, f"端口 {port} 被其它程序占用（{name}），未强行结束"
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=15,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:  # noqa: BLE001
        return False, f"结束进程失败：{e}"
    for _ in range(int(timeout * 10)):
        if not port_up(port, 0.4):
            return True, f"网关已停止（PID {pid}）"
        time.sleep(0.1)
    return False, "进程已结束但端口仍被占用"


def external() -> bool:
    """端口上是不是"别人的"实例（非本应用拉起）。"""
    return bool(_external)


def running() -> bool:
    return _proc is not None and _proc.poll() is None


def pid() -> int | None:
    return _proc.pid if running() else None


# ---------- 端口占用排查 ----------

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
# 允许被「停止网关」结束的进程名 —— 万一该端口被别的程序占了，不要误杀。
_ALLOW_IMAGE = ("python.exe", "pythonw.exe", "小d智能助手.exe",
                "小d语音陪聊助手.exe")


def _image_name(p_: int) -> str:
    """进程映像名（Windows）。中文系统 tasklist 输出是 GBK，按字节解。"""
    if os.name != "nt":
        return ""
    try:
        cp = subprocess.run(
            ["tasklist", "/FI", f"PID eq {p_}", "/NH", "/FO", "CSV"],
            capture_output=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        raw = cp.stdout or b""
        out = ""
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                out = raw.decode(enc)
                break
            except Exception:  # noqa: BLE001
                continue
        if not out:
            out = raw.decode("utf-8", errors="replace")
        return out.strip().split('","')[0].strip().strip('"')
    except Exception:  # noqa: BLE001
        return ""


def listener_pid(port: int) -> int | None:
    """对外暴露的监听进程查询（界面显示实际 PID 用）。"""
    return _listener_pid(port)
