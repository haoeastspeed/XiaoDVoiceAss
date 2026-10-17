# -*- coding: utf-8 -*-
"""传输抽象：把不同底层（WebSocket / MQTT 控制 + UDP 音频）统一成
VoiceSession 能直接使用的"类 ws"对象。

VoiceSession 只依赖对端对象的两件事：
  * `await obj.send(data)`  —— 发文本(JSON)或二进制(opus)帧
  * `async for raw in obj`  —— 收帧（文本为 str，二进制为 bytes）

`QueueTransport` 用一个 asyncio 队列把任意事件源接成上面这个形状，于是
MQTT/USDT 会话可以复用同一个状态机，session.py 不用改。
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator, Optional


class QueueTransport:
    """队列驱动的 'ws' 替身（MQTT/UDP 会话复用 VoiceSession 用）。

    子类/使用方通过 feed()/close_inbox() 投递入站帧，通过覆盖 send() 处理出站。
    """

    def __init__(self, maxsize: int = 0):
        self._inbox: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.sent: list = []          # 出站记录（测试/诊断）
        self.closed = False

    # ---- 入站 ----

    def feed(self, data):
        if not self.closed:
            self._inbox.put_nowait(data)

    def close_inbox(self):
        self.closed = True
        self._inbox.put_nowait(None)

    def __aiter__(self) -> AsyncIterator:
        return self._iter()

    async def _iter(self) -> AsyncIterator:
        while True:
            item = await self._inbox.get()
            if item is None:
                return
            yield item

    # ---- 出站（子类覆盖）----

    async def send(self, data) -> bool:
        self.sent.append(data)
        return True


def parse_device_id_from_topic(topic: str) -> str:
    """从 `devices/p2p/{mac}` 取回 mac（把 `_` 还原成 `:` 交给上层归一化）。"""
    if not topic:
        return ""
    parts = topic.split("/")
    if len(parts) >= 3 and parts[0] == "devices" and parts[1] == "p2p":
        return parts[2]
    return ""


def safe_mac(device_id: str) -> str:
    """device-id → MQTT 主题里安全的 mac（`:` → `_`）。"""
    return (device_id or "").replace(":", "_").replace(" ", "_")


def mqtt_client_id(model: str, device_id: str) -> str:
    """对齐官方：GID_{model}@@@{mac}@@@{mac}。"""
    group = f"GID_{model or 'default'}".replace(":", "_").replace(" ", "_")
    mac = safe_mac(device_id)
    return f"{group}@@@{mac}@@@{mac}"


def subscribe_topic(device_id: str) -> str:
    return f"devices/p2p/{safe_mac(device_id)}"


PUBLISH_TOPIC = "device-server"
