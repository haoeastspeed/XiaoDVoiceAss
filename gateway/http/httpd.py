# -*- coding: utf-8 -*-
"""轻量 HTTP 服务（OTA / 视觉上传用）。

用标准库 `ThreadingHTTPServer`：零新依赖、头控制完整。跑在独立线程，与
WebSocket 网关进程共存，端口默认 8003（对齐官方 server.http_port 语义）。
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional, Tuple

# 允许的自定义头（对齐官方 OTA 的 CORS 配置）
_CORS_HEADERS = "client-id, content-type, device-id, authorization"

RouteResult = Tuple[int, Dict[str, str], bytes]


class GatewayHTTP:
    """极简路由 + 后台线程服务。"""

    def __init__(self, config, logger: Optional[Callable[[str], None]] = None):
        self.config = config
        self._log = logger or (lambda m: print(f"[http] {m}", flush=True))
        self._routes: List[tuple] = []   # (method, regex, fn)
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ---------- 路由 ----------

    def route(self, method: str, pattern: str, fn):
        self._routes.append((method.upper(), re.compile(pattern), fn))
        return fn

    def get(self, pattern: str):
        return lambda fn: self.route("GET", pattern, fn)

    def post(self, pattern: str):
        return lambda fn: self.route("POST", pattern, fn)

    def dispatch(self, method: str, path: str, body: bytes, headers: dict):
        """返回 (status, headers, body)；未命中返回 None。"""
        path = path.split("?", 1)[0]
        if not path.endswith("/") and "." not in path.rsplit("/", 1)[-1]:
            path = path + "/"
        for m, rx, fn in self._routes:
            if m != method.upper():
                continue
            match = rx.match(path)
            if match:
                return fn(body, headers, match)
        return None

    # ---------- 服务 ----------

    def start(self) -> bool:
        port = int(getattr(self.config, "http_port", 0) or 0)
        if port <= 0:
            self._log("http_port=0，未启动 HTTP 服务")
            return False
        outer = self

        class _H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):   # 静音默认访问日志
                pass

            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                headers = {k.lower(): v for k, v in self.headers.items()}
                # 把来源地址塞进 headers：本地工具端点（如 /tts/preview/）要判断
                # "是不是本机 GUI 在调"，塞进 headers 就不用给所有 handler 改签名。
                headers["x-peer-ip"] = (self.client_address[0]
                                        if self.client_address else "")
                try:
                    res = outer.dispatch(method, self.path, body, headers)
                except Exception as e:  # noqa: BLE001
                    outer._log(f"处理 {method} {self.path} 异常："
                               f"{type(e).__name__}: {e}")
                    res = (500, {"Content-Type": "application/json"},
                           json.dumps({"success": False,
                                       "message": "internal error"}).encode())
                if res is None:
                    res = (404, {"Content-Type": "text/plain"},
                           b"not found\n")
                status, extra, payload = res
                self.send_response(status)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", _CORS_HEADERS)
                self.send_header("Access-Control-Allow-Methods",
                                 "GET, POST, OPTIONS")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                if self.command == "HEAD":
                    payload = b""
                self.end_headers()
                if payload and self.command != "HEAD":
                    self.wfile.write(payload)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_OPTIONS(self):
                self._handle("OPTIONS")

        try:
            self._server = ThreadingHTTPServer(
                (str(getattr(self.config, "host", "0.0.0.0")), port), _H)
        except Exception as e:  # noqa: BLE001
            self._log(f"HTTP 服务启动失败（端口 {port}）："
                      f"{type(e).__name__}: {e}")
            self._server = None
            return False
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="xiaod-http", daemon=True)
        self._thread.start()
        self._log(f"HTTP 服务已启动：http://0.0.0.0:{port}/"
                  f"（OTA 端点 /xiaozhi/ota/）")
        return True

    def stop(self):
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:  # noqa: BLE001
                pass
            self._server = None


# ---------- 通用响应辅助 ----------

def json_response(obj: dict, status: int = 200) -> RouteResult:
    return (status, {"Content-Type": "application/json; charset=utf-8"},
            json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def text_response(text: str, status: int = 200) -> RouteResult:
    return (status, {"Content-Type": "text/plain; charset=utf-8"},
            text.encode("utf-8"))
