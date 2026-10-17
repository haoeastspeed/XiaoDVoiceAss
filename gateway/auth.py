# -*- coding: utf-8 -*-
"""设备鉴权（对齐官方 core/auth.py）。

token 结构：`{signature}.{ts}`，signature = base64url(HMAC-SHA256(key, "client|user|ts"))。
token 本身不含明文设备信息，client_id / device_id 在连接时随请求携带。

用法（默认关闭，`auth_enabled=false` 时全部直接放行）：
  * WebSocket 握手：`Authorization: Bearer <token>` + Device-Id + Client-Id 头
  * OTA：同一个 token（由 /xiaozhi/ota/ 签发，设备存下来）
  * MQTT：password = base64(HMAC-SHA256(mqtt_signature_key, client_id+"|"+username))
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Optional

# 未配置 auth_key 时的内置默认密钥（局域网信任场景够用；公网务必自配）
_DEFAULT_KEY = "xiaod-local-gateway"
_DEFAULT_EXPIRE = 60 * 60 * 24 * 30     # 30 天


class AuthManager:
    def __init__(self, secret_key: str, expire_seconds: Optional[int] = None):
        self.secret_key = secret_key or _DEFAULT_KEY
        if not expire_seconds or expire_seconds < 0:
            self.expire_seconds = _DEFAULT_EXPIRE
        else:
            self.expire_seconds = int(expire_seconds)

    # ---------- 基础签名 ----------

    def _sign(self, content: str) -> str:
        sig = hmac.new(self.secret_key.encode("utf-8"),
                       content.encode("utf-8"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(sig).decode("utf-8").rstrip("=")

    def generate_token(self, client_id: str, username: str) -> str:
        ts = int(time.time())
        return f"{self._sign(f'{client_id}|{username}|{ts}')}.{ts}"

    def verify_token(self, token: str, client_id: str, username: str) -> bool:
        try:
            sig_part, ts_str = token.split(".")
            ts = int(ts_str)
        except (ValueError, AttributeError):
            return False
        if int(time.time()) - ts > self.expire_seconds:
            return False
        expected = self._sign(f"{client_id}|{username}|{ts}")
        return hmac.compare_digest(sig_part, expected)


# ---------------------------------------------------------------------------
# 配置驱动的便捷入口
# ---------------------------------------------------------------------------


def effective_key(config) -> str:
    key = str(getattr(config, "auth_key", "") or "").strip()
    return key or _DEFAULT_KEY


def make_manager(config) -> AuthManager:
    return AuthManager(effective_key(config))


def auth_enabled(config) -> bool:
    return bool(getattr(config, "auth_enabled", False))


def device_allowed(config, device_id: str) -> bool:
    """白名单直放行（对齐官方：allowed_devices 命中就不校验 token）。"""
    allowed = getattr(config, "allowed_devices", None) or []
    if not allowed:
        return False
    return device_id in set(str(d) for d in allowed)


def verify_ws_auth(config, device_id: str, client_id: str,
                   authorization: str) -> bool:
    """校验 WebSocket 握手的 Bearer token。未开启鉴权/白名单命中 → 放行。"""
    if not auth_enabled(config):
        return True
    if device_allowed(config, device_id):
        return True
    header = (authorization or "").strip()
    if header.lower().startswith("bearer "):
        header = header[7:].strip()
    if not header:
        return False
    return make_manager(config).verify_token(header, client_id or device_id,
                                             device_id)


def mqtt_password(config, client_id: str, username: str) -> str:
    """生成 MQTT 密码签名（base64 标准编码，与官方 ota_handler 一致）。"""
    key = str(getattr(config, "auth_key", "") or "").strip() or _DEFAULT_KEY
    sig = hmac.new(key.encode("utf-8"),
                   f"{client_id}|{username}".encode("utf-8"),
                   hashlib.sha256).digest()
    return base64.b64encode(sig).decode("utf-8")
