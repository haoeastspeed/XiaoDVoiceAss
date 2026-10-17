# -*- coding: utf-8 -*-
"""OTA + 设备激活端点（对齐官方 core/api/ota_handler.py）。

  POST /xiaozhi/ota/            —— 设备上报型号/版本；服务端返回 server_time、
                                   固件更新信息，以及 websocket 或 mqtt 接入配置
  GET  /xiaozhi/ota/            —— 文本健康检查
  GET  /xiaozhi/ota/download/{f} —— 下载 data/bin 下的固件（防目录穿越）

固件命名规则：`{model}_{version}.bin`，放在 `<数据根>/data/bin/` 下。版本用
"数字元组比较"（对齐官方 _parse_version / _is_higher_version）。
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from gateway.auth import make_manager
from gateway.http.httpd import GatewayHTTP, json_response, text_response

_BIN_RE = re.compile(r"^[A-Za-z0-9\.\-_]+\.bin$")
_NAME_RE = re.compile(r"^(.+?)_([0-9][A-Za-z0-9\.\-_]*)\.bin$")


def _parse_version(ver: str) -> Tuple[int, ...]:
    parts = re.findall(r"\d+", ver or "")
    return tuple(int(p) for p in parts) if parts else (0,)


def _is_higher_version(a: str, b: str) -> bool:
    ta, tb = _parse_version(a), _parse_version(b)
    for i in range(max(len(ta), len(tb))):
        ai = ta[i] if i < len(ta) else 0
        bi = tb[i] if i < len(tb) else 0
        if ai > bi:
            return True
        if ai < bi:
            return False
    return False


class OTAHandler:
    def __init__(self, config, get_lan_ip, logger=None):
        self.config = config
        self.get_lan_ip = get_lan_ip
        self._log = logger or (lambda m: print(f"[ota] {m}", flush=True))
        from gateway.config import ROOT
        self.bin_dir = os.path.join(ROOT, "data", "bin")
        self._cache: Dict[str, Any] = {"updated_at": 0, "files_by_model": {}}
        self._cache_ttl = int(getattr(config, "firmware_cache_ttl", 30) or 30)

    # ---------- 注册路由 ----------

    def register(self, app: GatewayHTTP):
        app.route("POST", r"/xiaozhi/ota/?$", self.handle_post)
        app.route("GET", r"/xiaozhi/ota/?$", self.handle_get)
        app.route("OPTIONS", r"/xiaozhi/ota/?$",
                  lambda b, h, m: (204, {}, b""))
        app.route("GET", r"/xiaozhi/ota/download/(?P<name>[^/]+)$",
                  self.handle_download)

    # ---------- 固件索引 ----------

    def _refresh_cache(self):
        now = int(time.time())
        if (now - int(self._cache.get("updated_at", 0)) < self._cache_ttl
                and self._cache.get("files_by_model")):
            return
        by_model: Dict[str, List[Tuple[str, str]]] = {}
        try:
            os.makedirs(self.bin_dir, exist_ok=True)
            for fn in os.listdir(self.bin_dir):
                if not fn.lower().endswith(".bin"):
                    continue
                m = _NAME_RE.match(fn)
                if not m:
                    continue
                by_model.setdefault(m.group(1), []).append((m.group(2), fn))
            for items in by_model.values():
                items.sort(key=lambda it: _parse_version(it[0]), reverse=True)
            self._cache["files_by_model"] = by_model
            self._cache["updated_at"] = now
        except Exception as e:  # noqa: BLE001
            self._log(f"刷新固件缓存失败：{type(e).__name__}: {e}")

    # ---------- 端点 ----------

    def handle_get(self, body, headers, match):
        return text_response("xiaod gateway ota ok\n")

    def handle_post(self, body, headers, match) -> tuple:
        device_id = (headers.get("device-id") or "").strip()
        client_id = (headers.get("client-id") or "").strip()
        if not device_id or not client_id:
            return json_response({"success": False,
                                  "message": "request error."}, 200)

        try:
            data_json = json.loads(body.decode("utf-8", "replace")) if body \
                else {}
        except Exception:  # noqa: BLE001
            data_json = {}

        cfg = self.config
        ws_port = int(getattr(cfg, "port", 8000))
        local_ip = self.get_lan_ip()

        # 型号：优先头，其次 body.board.type / body.model
        model = ""
        for h in ("device-model", "device_model", "model"):
            if headers.get(h):
                model = headers[h].strip()
                break
        if not model:
            board = data_json.get("board")
            if isinstance(board, dict):
                model = str(board.get("type") or "")
            model = model or str(data_json.get("model") or "")
        model = model or "default"

        # 版本：优先头，其次 body.application.version
        version = ""
        for h in ("device-version", "device_version", "firmware-version",
                  "app-version", "application-version"):
            if headers.get(h):
                version = headers[h].strip()
                break
        if not version:
            app = data_json.get("application")
            if isinstance(app, dict):
                version = str(app.get("version") or "")
        version = version or "0.0.0"

        out: Dict[str, Any] = {
            "server_time": {
                "timestamp": int(round(time.time() * 1000)),
                "timezone_offset": int(getattr(cfg, "timezone_offset", 8)) * 60,
            },
            "firmware": {"version": version, "url": ""},
        }

        mqtt_gw = str(getattr(cfg, "mqtt_gateway", "") or "").strip()
        if mqtt_gw:
            out["mqtt"] = self._mqtt_block(model, device_id, mqtt_gw)
        else:
            out["websocket"] = {
                "url": self._ws_url(local_ip, ws_port),
                "token": self._token(client_id, device_id),
            }

        # 固件更新
        self._refresh_cache()
        for ver, fname in self._cache["files_by_model"].get(model, []):
            if _is_higher_version(ver, version):
                out["firmware"]["version"] = ver
                out["firmware"]["url"] = self._download_url(local_ip, fname)
                break

        self._log(f"OTA：设备 {device_id} 型号 {model} 版本 {version} → "
                  f"固件 {out['firmware']['version'] or '(无)'}")
        return json_response(out)

    def handle_download(self, body, headers, match) -> tuple:
        name = os.path.basename(match.group("name") or "")
        if not name or not _BIN_RE.match(name):
            return text_response("bad filename\n", 400)
        path = os.path.realpath(os.path.join(self.bin_dir, name))
        if not path.startswith(os.path.realpath(self.bin_dir) + os.sep):
            return text_response("forbidden\n", 403)
        if not os.path.isfile(path):
            return text_response("not found\n", 404)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return text_response("read error\n", 500)
        return (200, {"Content-Type": "application/octet-stream",
                      "Content-Disposition": f'attachment; filename="{name}"'},
                data)

    # ---------- 辅助 ----------

    def _ws_url(self, local_ip: str, port: int) -> str:
        cfg_url = str(getattr(self.config, "websocket_url", "") or "").strip()
        if cfg_url and "你的" not in cfg_url:
            return cfg_url
        ws_path = str(getattr(self.config, "ws_path", "/xiaozhi/v1/"))
        return f"ws://{local_ip}:{port}{ws_path}"

    def _download_url(self, local_ip: str, fname: str) -> str:
        http_port = int(getattr(self.config, "http_port", 8003) or 8003)
        base = str(getattr(self.config, "ota_download_base", "") or "").strip()
        if base:
            return base.rstrip("/") + "/" + fname
        return f"http://{local_ip}:{http_port}/xiaozhi/ota/download/{fname}"

    def _token(self, client_id: str, device_id: str) -> str:
        if not getattr(self.config, "auth_enabled", False):
            return ""
        from gateway.auth import device_allowed
        if device_allowed(self.config, device_id):
            return ""
        return make_manager(self.config).generate_token(client_id, device_id)

    def _mqtt_block(self, model: str, device_id: str, endpoint: str) -> dict:
        from gateway.auth import mqtt_password
        group = f"GID_{model}".replace(":", "_").replace(" ", "_")
        mac = device_id.replace(":", "_")
        client_id = f"{group}@@@{mac}@@@{mac}"
        username = base64.b64encode(
            json.dumps({"ip": "unknown"}).encode("utf-8")).decode("utf-8")
        key = str(getattr(self.config, "mqtt_signature_key", "") or "")
        password = mqtt_password(self.config, client_id, username) if key else ""
        return {
            "endpoint": endpoint,
            "client_id": client_id,
            "username": username,
            "password": password,
            "publish_topic": "device-server",
            "subscribe_topic": f"devices/p2p/{mac}",
        }
