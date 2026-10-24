# -*- coding: utf-8 -*-
"""小D语音网关 —— 独立进程服务管理器。

用法（系统 Python 即可，不依赖网关 venv）：

    python scripts/gateway_service.py start     # 后台拉起（脱离父进程，独立常驻）
    python scripts/gateway_service.py stop      # 按 PID 文件停止
    python scripts/gateway_service.py restart
    python scripts/gateway_service.py status    # 打印运行状态 + 端口 + PID

与 DeskWork 内嵌拉起的区别：
  * 内嵌拉起（默认）：网关是 DeskWork 的子进程，DeskWork 退出时一并回收，
    并带 XIAOD_IDLE_EXIT_S=300（5 分钟无会话自动退出）。
  * 本脚本：进程脱离父进程、写 PID 文件、不设空闲退出（XIAOD_IDLE_EXIT_S=0），
    开机/登录后手动或计划任务拉起一次即可长期常驻；DeskWork 检测到端口已在
    监听就会直接复用，不会重复拉起，也不会在退出时把它杀掉。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ensure_std_streams() -> None:
    """用 pythonw / VBS 无窗口启动时 sys.stdout 是 None，print 会直接抛异常。

    这里把标准输出重定向到日志文件，保证"无黑窗"也不会跑挂。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        os.makedirs(os.path.join(ROOT, ".tmp"), exist_ok=True)
        f = open(os.path.join(ROOT, ".tmp", "gateway_service_console.log"),
                 "a", encoding="utf-8", buffering=1)
    except Exception:  # noqa: BLE001
        import io
        f = io.StringIO()
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


_ensure_std_streams()
VENV_PY = os.path.join(
    ROOT, "reference", "xiaozhi-server", "main", "xiaozhi-server",
    ".venv", "Scripts", "python.exe")
BIN_DIR = os.path.join(ROOT, "reference", "bin")
TMP = os.path.join(ROOT, ".tmp")
PID_FILE = os.path.join(TMP, "gateway.pid")
LOG_FILE = os.path.join(TMP, "gateway_service.log")

DEFAULT_PORT = 8000


def _port_alive(port: int) -> bool:
    """探活：发规范 HTTP 请求。裸 connect 立刻断开会让网关打一堆
    InvalidMessage 堆栈（日志噪音）。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.5) as s:
            s.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                      b"Connection: close\r\n\r\n")
            got = s.recv(64)
            # 把响应读干净再关，避免服务端回包时对端已断开而报错
            while got and len(got) < 4096:
                more = s.recv(512)
                if not more:
                    break
                got += more
            return bool(got)
    except OSError:
        return False


def _read_pid() -> int | None:
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
    except Exception:
        return None
    return pid if _pid_alive(pid) else None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # tasklist 无外部依赖；用 kernel32 更轻
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _kill(pid: int) -> bool:
    if not _pid_alive(pid):
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10,
                           creationflags=getattr(subprocess,
                                                "CREATE_NO_WINDOW", 0))
        else:
            os.kill(pid, 15)
    except Exception:
        pass
    for _ in range(30):
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return not _pid_alive(pid)


def _spawn(port: int) -> int | None:
    if not os.path.isfile(VENV_PY):
        print(f"[错误] 找不到网关解释器: {VENV_PY}")
        return None
    os.makedirs(TMP, exist_ok=True)
    env = os.environ.copy()
    env["PATH"] = BIN_DIR + os.pathsep + env.get("PATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    env["XIAOD_IDLE_EXIT_S"] = "0"        # 常驻服务：不做空闲自动退出
    env["XIAOD_PORT"] = str(port)
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    log = open(LOG_FILE, "a", encoding="utf-8")
    try:
        p = subprocess.Popen(
            [VENV_PY, "main.py", "--port", str(port)],
            cwd=ROOT, env=env, creationflags=flags,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            close_fds=True)
    except Exception as e:  # noqa: BLE001
        print(f"[错误] 拉起失败: {e}")
        return None
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(p.pid))
    return p.pid


def cmd_start(port: int) -> int:
    if _port_alive(port):
        print(f"[信息] 端口 {port} 已在监听，网关已在运行（PID 文件: "
              f"{_read_pid() or '无'}）")
        return 0
    old = _read_pid()
    if old:
        print(f"[信息] 旧 PID {old} 仍存活但端口未开，先停止")
        _kill(old)
    pid = _spawn(port)
    if pid is None:
        return 1
    for _ in range(60):
        if _port_alive(port):
            print(f"[OK] 网关已启动：ws://127.0.0.1:{port}/xiaozhi/v1/ "
                  f"（PID {pid}，日志 {LOG_FILE}）")
            return 0
        time.sleep(0.5)
    print(f"[警告] 已拉起（PID {pid}）但 30s 内端口未开，请查看 {LOG_FILE}")
    return 1


def cmd_stop(port: int) -> int:
    pid = _read_pid()
    ok = False
    if pid:
        ok = _kill(pid)
        print(f"[OK] 已停止网关 PID {pid}" if ok
              else f"[警告] PID {pid} 未能停止")
    else:
        print("[信息] 没有由本脚本拉起的网关实例")
    try:
        os.remove(PID_FILE)
    except Exception:
        pass
    if _port_alive(port):
        print(f"[警告] 端口 {port} 仍在监听（可能是 DeskWork 内嵌拉起的实例）")
    return 0 if (ok or not pid) else 1


def cmd_status(port: int) -> int:
    up = _port_alive(port)
    pid = _read_pid()
    print(f"端口 {port}: {'监听中' if up else '未监听'}")
    print(f"服务 PID: {pid if pid else '无（未由脚本拉起 / 已退出）'}")
    print(f"日志: {LOG_FILE}")
    return 0 if up else 2


def main(argv: list[str]) -> int:
    cmd = (argv[1] if len(argv) > 1 else "status").lower()
    port = DEFAULT_PORT
    if len(argv) > 2:
        try:
            port = int(argv[2])
        except ValueError:
            print("用法: gateway_service.py start|stop|restart|status [port]")
            return 2
    if cmd == "start":
        return cmd_start(port)
    if cmd == "stop":
        return cmd_stop(port)
    if cmd == "restart":
        cmd_stop(port)
        time.sleep(0.5)
        return cmd_start(port)
    if cmd == "status":
        return cmd_status(port)
    print("用法: gateway_service.py start|stop|restart|status [port]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
