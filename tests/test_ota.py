# -*- coding: utf-8 -*-
"""OTA + 设备激活回归：真起 HTTP 服务，验证端点行为与安全边界。

覆盖：POST 头校验 / 响应结构 / 固件更新判定 / 下载端点防目录穿越 / CORS /
auth 开启时的 token 签发。

跑法：python tests/test_ota.py
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import httpx                                                    # noqa: E402

from gateway.auth import AuthManager                            # noqa: E402
from gateway.config import GatewayConfig                        # noqa: E402
from gateway.http.httpd import GatewayHTTP                      # noqa: E402
from gateway.http.ota import (OTAHandler, _is_higher_version,   # noqa: E402
                              _parse_version)

PASS = 0
FAIL = 0
BASE = None


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_pure():
    print("\n== 版本比较 ==")
    ok(_parse_version("1.2.3") == (1, 2, 3), "解析 1.2.3")
    ok(_parse_version("v1.2") == (1, 2), "解析 v1.2")
    ok(_parse_version("") == (0,), "空串 → (0,)")
    ok(_is_higher_version("1.2.10", "1.2.9"), "1.2.10 > 1.2.9")
    ok(not _is_higher_version("1.2.9", "1.2.10"), "1.2.9 不 > 1.2.10")
    ok(not _is_higher_version("1.2", "1.2.0"), "1.2 == 1.2.0")
    ok(_is_higher_version("2.0", "1.9.9"), "2.0 > 1.9.9")


def start_server(cfg):
    app = GatewayHTTP(cfg, logger=lambda m: None)
    OTAHandler(cfg, lambda: "192.168.1.9", logger=lambda m: None).register(app)
    assert app.start(), "HTTP 启动失败"
    return app


def test_endpoints():
    print("\n== 端点 ==")
    global BASE
    port = free_port()
    cfg = GatewayConfig()
    cfg.http_port = port
    cfg.auth_enabled = False
    app = start_server(cfg)
    BASE = f"http://127.0.0.1:{port}"
    cli = httpx.Client(trust_env=False, timeout=5.0)
    try:
        # GET 健康
        r = cli.get(f"{BASE}/xiaozhi/ota/")
        ok(r.status_code == 200 and "ok" in r.text, "GET /xiaozhi/ota/ 健康检查",
           r.text[:40])

        # POST 缺头
        r = cli.post(f"{BASE}/xiaozhi/ota/", json={})
        j = r.json()
        ok(r.status_code == 200 and j.get("success") is False,
           "缺 device-id/client-id → success=false", j)

        # POST 正常
        r = cli.post(f"{BASE}/xiaozhi/ota/", json={},
                     headers={"device-id": "AA:BB:CC", "client-id": "cid",
                              "device-model": "bread-compact-wifi-lcd",
                              "device-version": "1.0.0"})
        j = r.json()
        ok("server_time" in j and j["server_time"]["timezone_offset"] == 480,
           "返回 server_time（时区 8h）", j)
        ws = j.get("websocket") or {}
        ok(ws.get("url", "").startswith("ws://192.168.1.9"), "下发 websocket url",
           ws)
        ok(ws.get("token") == "", "未开鉴权 → token 为空")
        ok(j["firmware"]["version"] == "1.0.0", "无新固件时版本回显自身")
        ok(j["firmware"]["url"] == "", "无新固件时 url 为空")

        # CORS 头
        r = cli.options(f"{BASE}/xiaozhi/ota/")
        ok("client-id" in (r.headers.get("access-control-allow-headers") or ""),
           "CORS 允许 client-id 头")

        # body 兜底取型号/版本
        r = cli.post(f"{BASE}/xiaozhi/ota/",
                     json={"board": {"type": "m"}, "application": {"version": "0.9"}},
                     headers={"device-id": "AA", "client-id": "c"})
        j = r.json()
        ok(j["firmware"]["version"] == "0.9", "body 兜底取版本", j["firmware"])

        # 404
        r = cli.get(f"{BASE}/nope")
        ok(r.status_code == 404, "未知路径 404")
    finally:
        cli.close()
        app.stop()


def test_download():
    print("\n== 固件下载与防穿越 ==")
    port = free_port()
    cfg = GatewayConfig()
    cfg.http_port = port
    app = start_server(cfg)
    base = f"http://127.0.0.1:{port}"
    cli = httpx.Client(trust_env=False, timeout=5.0)
    try:
        from gateway.config import ROOT as DATA_ROOT
        bin_dir = os.path.join(DATA_ROOT, "data", "bin")
        os.makedirs(bin_dir, exist_ok=True)
        name = "bread-compact-wifi-lcd_1.2.3.bin"
        path = os.path.join(bin_dir, name)
        with open(path, "wb") as f:
            f.write(b"FIRMWARE")
        try:
            r = cli.get(f"{base}/xiaozhi/ota/download/{name}")
            ok(r.status_code == 200 and r.content == b"FIRMWARE",
               "正常下载固件", r.status_code)

            # 版本判定：请求 1.0.0 应拿到 1.2.3 的下载地址
            r = cli.post(f"{base}/xiaozhi/ota/", json={},
                         headers={"device-id": "AA", "client-id": "c",
                                  "device-model": "bread-compact-wifi-lcd",
                                  "device-version": "1.0.0"})
            j = r.json()
            ok(j["firmware"]["version"] == "1.2.3" and name in j["firmware"]["url"],
               "检测到更高版本并给出下载地址", j["firmware"])

            # 目录穿越
            for bad in ["..%2F..%2Fetc%2Fpasswd", "%2e%2e%2fsecret.bin",
                        "a%2Fb.bin", "x.txt"]:
                r = cli.get(f"{base}/xiaozhi/ota/download/{bad}")
                ok(r.status_code in (400, 403, 404),
                   f"非法文件名被拒：{bad}", r.status_code)
            r = cli.get(f"{base}/xiaozhi/ota/download/notexist.bin")
            ok(r.status_code == 404, "不存在的固件 404")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    finally:
        cli.close()
        app.stop()


def test_auth():
    print("\n== 鉴权 token ==")
    port = free_port()
    cfg = GatewayConfig()
    cfg.http_port = port
    cfg.auth_enabled = True
    cfg.auth_key = "secret-key"
    app = start_server(cfg)
    base = f"http://127.0.0.1:{port}"
    cli = httpx.Client(trust_env=False, timeout=5.0)
    try:
        r = cli.post(f"{base}/xiaozhi/ota/", json={},
                     headers={"device-id": "AA:BB", "client-id": "cid"})
        token = (r.json().get("websocket") or {}).get("token", "")
        ok(bool(token), "开启鉴权时签发 token")
        a = AuthManager("secret-key")
        ok(a.verify_token(token, "cid", "AA:BB"), "签发的 token 可校验通过")
        ok(not a.verify_token(token, "cid", "OTHER"), "token 与 device 绑定")
    finally:
        cli.close()
        app.stop()


if __name__ == "__main__":
    test_pure()
    test_endpoints()
    test_download()
    test_auth()
    print(f"\nOTA_PASS {PASS}/{PASS + FAIL}")
    sys.exit(0 if FAIL == 0 else 1)
