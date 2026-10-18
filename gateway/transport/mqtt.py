# -*- coding: utf-8 -*-
"""MQTT + UDP 传输（官方第二种传输，弱网/穿透友好）。

控制消息走 MQTT（hello / listen / abort / mcp / goodbye），音频走 UDP：
  * 设备发布到 `device-server`，服务端发布到 `devices/p2p/{mac}`
  * client_id = GID_{model}@@@{mac}@@@{mac}
  * username  = base64({"ip":"unknown"})
  * password  = base64(HMAC-SHA256(key, client_id + "|" + username))
  * hello 回包里带 udp{server, port, key, nonce}

paho-mqtt 是**可选依赖**：没装时本模块 `available=False`，网关照常以 WebSocket
模式工作（只打一行提示）。安装：`pip install paho-mqtt`。
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Callable, Optional

from gateway.auth import mqtt_password
from gateway.transport.base import (PUBLISH_TOPIC, mqtt_client_id,
                                    safe_mac, subscribe_topic)
from gateway.transport.udp import UdpAudioChannel, make_key, make_nonce

try:                                    # 可选依赖
    import paho.mqtt.client as mqtt     # noqa: N813
    _PAHO = True
except Exception:                       # noqa: BLE001
    mqtt = None
    _PAHO = False


def mqtt_username() -> str:
    """对齐官方：base64('{"ip":"unknown"}')。"""
    return base64.b64encode(
        json.dumps({"ip": "unknown"}).encode("utf-8")).decode("utf-8")


def mqtt_credentials(config, model: str, device_id: str) -> dict:
    """生成设备侧所需的 MQTT 连接三元组 + 主题。"""
    client_id = mqtt_client_id(model, device_id)
    username = mqtt_username()
    key = str(getattr(config, "mqtt_signature_key", "") or "")
    password = mqtt_password(config, client_id, username) if key else ""
    return {
        "client_id": client_id,
        "username": username,
        "password": password,
        "publish_topic": PUBLISH_TOPIC,
        "subscribe_topic": subscribe_topic(device_id),
    }


def parse_broker(endpoint: str):
    """'mqtt://host:1883' / 'host:1883' → (host, port)。"""
    s = (endpoint or "").strip()
    for pre in ("mqtt://", "tcp://"):
        if s.startswith(pre):
            s = s[len(pre):]
    if not s:
        return ("127.0.0.1", 1883)
    host, _, port = s.partition(":")
    try:
        return (host or "127.0.0.1", int(port) if port else 1883)
    except ValueError:
        return (host, 1883)


class UdpAudioEndpoint:
    """为某个设备建立一条 UDP 音频通道（收发 opus）。"""

    def __init__(self, config, logger=None):
        self.config = config
        self._log = logger or (lambda m: print(f"[udp] {m}", flush=True))
        self.key = make_key()
        self.nonce = make_nonce()
        self.channel = UdpAudioChannel(self.key, self.nonce, self._log)
        self._transport = None
        self._protocol = None
        self.port = int(getattr(config, "udp_port", 8001) or 8001)
        self.peer = None
        self.on_audio: Optional[Callable[[bytes], None]] = None

    def hello_block(self, host: str) -> dict:
        return {
            "server": host,
            "port": self.port,
            "key": self.key.hex(),
            "nonce": self.nonce.hex(),
        }

    async def start(self, loop):
        class _P(asyncio.DatagramProtocol):
            def __init__(self, outer):
                self.outer = outer

            def datagram_received(self, data, addr):
                self.outer.peer = addr
                try:
                    plain, _flags, _seq = self.outer.channel.decrypt(data)
                except Exception:
                    return
                if self.outer.on_audio:
                    self.outer.on_audio(plain)

            def error_received(self, exc):
                pass

        host = str(getattr(self.config, "host", "0.0.0.0"))
        try:
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: _P(self), local_addr=(host, self.port))
        except OSError as e:
            # 固定端口被占（第二台设备 / 上次会话没释放）→ 交给系统分配。
            # hello_block 会把实际端口告诉设备，所以不影响连接。
            self._log(f"UDP 端口 {self.port} 不可用（{e}），改用系统分配")
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: _P(self), local_addr=(host, 0))
            self.port = transport.get_extra_info("sockname")[1]
        self._transport, self._protocol = transport, protocol
        self._log(f"UDP 音频通道已监听 :{self.port}")
        return self

    def reset(self):
        """设备重连/重复 hello：设备侧序号从 0 重新开始，这里同步复位。"""
        self.channel.remote_sequence = 0
        self.peer = None

    def send_audio(self, opus: bytes):
        if self._transport is not None and self.peer is not None:
            try:
                self._transport.sendto(
                    self.channel.encrypt(opus, 0), self.peer)
            except Exception:  # noqa: BLE001
                pass

    def close(self):
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:  # noqa: BLE001
                pass
            self._transport = None


class MqttControlChannel:
    """MQTT 控制通道（paho 在独立线程，回调桥接回 asyncio）。"""

    def __init__(self, config, logger=None):
        self.config = config
        self._log = logger or (lambda m: print(f"[mqtt] {m}", flush=True))
        self.available = _PAHO
        self._client = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # on_message(kind, payload) —— 由上层处理（建会话等）
        self.on_message: Optional[Callable[[str, str, bytes], None]] = None

    def start(self, loop) -> bool:
        if not self.available:
            self._log("未安装 paho-mqtt，MQTT 传输不可用（pip install paho-mqtt）")
            return False
        endpoint = str(getattr(self.config, "mqtt_gateway", "") or "").strip()
        host, port = parse_broker(endpoint)
        self._loop = loop
        try:
            # paho-mqtt 2.x 必须显式给 callback_api_version，否则构造函数
            # 直接 ValueError（异常被下面 except 吞掉 → MQTT 静默不可用）。
            try:
                from paho.mqtt.client import CallbackAPIVersion
                cli = mqtt.Client(CallbackAPIVersion.VERSION2,
                                  client_id="xiaod-gateway", protocol=3)
            except Exception:  # noqa: BLE001
                cli = mqtt.Client(client_id="xiaod-gateway", protocol=3)
            cli.on_connect = self._on_connect
            cli.on_message = self._on_message
            cli.connect(host, port, keepalive=240)
            cli.loop_start()
            self._client = cli
            self._log(f"MQTT 控制通道已连接 {host}:{port}")
            return True
        except Exception as e:  # noqa: BLE001
            self._log(f"MQTT 连接失败（{type(e).__name__}: {e}）")
            return False

    def _on_connect(self, client, _userdata, _flags, *args):
        # *args 兼容 paho 1.x(rc) 与 2.x(reason_code, properties) 两种签名
        try:
            # 控制消息（tts:stop / abort / listen）用 QoS1，避免丢帧导致
            # 服务端状态卡在"正在说话"。
            client.subscribe(PUBLISH_TOPIC, qos=1)
        except Exception:  # noqa: BLE001
            pass

    def _on_message(self, client, _userdata, msg):
        if self.on_message is None or self._loop is None:
            return
        try:
            topic = msg.topic
            payload = bytes(msg.payload)
            if topic == PUBLISH_TOPIC:
                mac = ""
                try:
                    js = json.loads(payload.decode("utf-8", "replace"))
                    mac = str(js.get("device_id") or js.get("mac") or "")
                except Exception:  # noqa: BLE001
                    mac = ""
                self._loop.call_soon_threadsafe(
                    self.on_message, "device-server", mac, payload)
            elif topic.startswith("devices/p2p/"):
                mac = safe_mac(topic.split("/")[-1])
                self._loop.call_soon_threadsafe(
                    self.on_message, "device", mac, payload)
        except Exception:  # noqa: BLE001
            pass

    def publish(self, topic: str, payload: str):
        if self._client is None:
            return
        try:
            self._client.publish(topic, payload, qos=0)
        except Exception:  # noqa: BLE001
            pass

    def stop(self):
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
