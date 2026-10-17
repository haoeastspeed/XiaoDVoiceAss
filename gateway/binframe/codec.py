# -*- coding: utf-8 -*-
"""二进制帧编解码（协议版本 1 / 2 / 3）。

对齐官方 firmware 文档 docs/websocket_zh.md §3：

  版本1（默认）：直接发 Opus，无额外元数据。WebSocket 用 text/binary 区分类型。

  版本2：BinaryProtocol2（packed 16 字节头 + payload）
      uint16 version      协议版本
      uint16 type         0=OPUS, 1=JSON
      uint32 reserved     保留
      uint32 timestamp    时间戳(ms)，用于服务器端 AEC
      uint32 payload_size 负载长度
      uint8  payload[]

  版本3：BinaryProtocol3（packed 4 字节头 + payload）
      uint8  type
      uint8  reserved
      uint16 payload_size
      uint8  payload[]

解码输出统一为 (kind, payload, timestamp_ms)：
  kind —— "opus" / "json"
  timestamp_ms —— v2 才有值，其余为 None
"""
from __future__ import annotations

import json
import struct
from typing import Optional, Tuple

SUPPORTED_VERSIONS = (1, 2, 3)

_TYPE_OPUS = 0
_TYPE_JSON = 1

# v2: >HHIII = version, type, reserved, timestamp, payload_size（16 字节）
_V2_HEADER = struct.Struct(">HHIII")
# v3: >BBH = type, reserved, payload_size（4 字节）
_V3_HEADER = struct.Struct(">BBH")


def clamp_version(v) -> int:
    """把对端声明的版本夹到网关支持的集合 {1,2,3}，非法值回退 1。"""
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return 1
    return iv if iv in SUPPORTED_VERSIONS else 1


class FrameCodec:
    """按协商版本编解码上行/下行二进制帧。"""

    def __init__(self, version: int = 1):
        self.version = clamp_version(version)

    # ---------- 解码（上行）----------

    def decode(self, data: bytes) -> Tuple[str, bytes, Optional[int]]:
        """解码一帧上行二进制数据 → (kind, payload, timestamp_ms)。

        畸形/截断数据返回 ("opus"/"json", b"", None) 或尽力解析出的 payload，
        绝不抛异常打断会话。
        """
        if not data:
            return ("opus", b"", None)
        if self.version == 1:
            # 裸 opus：整帧都是负载
            return ("opus", bytes(data), None)
        if self.version == 2:
            if len(data) < _V2_HEADER.size:
                return ("opus", b"", None)
            _ver, mtype, _reserved, timestamp, size = \
                _V2_HEADER.unpack_from(data, 0)
            payload = data[_V2_HEADER.size:_V2_HEADER.size + int(size)]
            kind = "json" if int(mtype) == _TYPE_JSON else "opus"
            return (kind, bytes(payload), int(timestamp))
        # version 3
        if len(data) < _V3_HEADER.size:
            return ("opus", b"", None)
        mtype, _reserved, size = _V3_HEADER.unpack_from(data, 0)
        payload = data[_V3_HEADER.size:_V3_HEADER.size + int(size)]
        kind = "json" if int(mtype) == _TYPE_JSON else "opus"
        return (kind, bytes(payload), None)

    # ---------- 编码（下行）----------

    def encode_audio(self, opus: bytes, timestamp_ms: int = 0) -> bytes:
        """把 opus 包封装成当前版本的二进制帧。"""
        if self.version == 1:
            return bytes(opus)
        if self.version == 2:
            head = _V2_HEADER.pack(self.version, _TYPE_OPUS, 0,
                                   int(timestamp_ms) & 0xFFFFFFFF, len(opus))
            return head + bytes(opus)
        head = _V3_HEADER.pack(_TYPE_OPUS, 0, len(opus))
        return head + bytes(opus)

    def encode_json(self, obj: dict) -> Optional[bytes]:
        """把 JSON 消息封装成二进制帧（v1 返回 None —— 走 text 帧）。"""
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        if self.version == 1:
            return None
        if self.version == 2:
            head = _V2_HEADER.pack(self.version, _TYPE_JSON, 0, 0, len(raw))
            return head + raw
        head = _V3_HEADER.pack(_TYPE_JSON, 0, len(raw))
        return head + raw
