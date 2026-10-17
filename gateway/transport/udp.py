# -*- coding: utf-8 -*-
"""UDP 音频通道（MQTT+UDP 混合协议的音频部分）。

字节布局严格对齐固件 `main/protocols/mqtt_protocol.cc`：
  * 每包 16 字节"计数器块"就是 AES-CTR 的初始计数块
      [0]      type         固定 0x01（接收端校验，非 0x01 直接丢弃）
      [1]      flags
      [2:4]    payload_len  大端
      [4:8]    ssrc
      [8:12]   timestamp     大端
      [12:16]  sequence      大端
  * 其后紧跟密文；服务端下发 hello 时给的 nonce 必须以 [0]=0x01 起头
    （固件发送时以它为模板，只覆写 [2:4]/[8:12]/[12:16]）。

序号：`sequence <= remote_sequence` 直接丢弃（防重放）；不连续只记一次告警。
"""
from __future__ import annotations

import struct
from typing import Callable, Optional, Tuple

_HDR = struct.Struct(">BBHIIII")   # 1+1+2+4+4+4+4 = 20? 见下
# 实际布局是 1+1+2+4+4+4 = 16 字节
_HDR = struct.Struct(">BBHIII")
_HDR_SIZE = 16
_TYPE = 0x01


def _aes_ctr(key: bytes, counter_block: bytes, data: bytes) -> bytes:
    """AES-CTR：以整块 16 字节为初始计数（与 mbedtls 的用法一致）。"""
    try:
        from Crypto.Cipher import AES            # pycryptodome
        iv = int.from_bytes(counter_block, "big")
        cipher = AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=iv)
        return cipher.encrypt(data)
    except Exception:  # noqa: BLE001
        from cryptography.hazmat.primitives.ciphers import (
            Cipher, algorithms, modes)
        iv = counter_block
        c = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
        return c.update(data) + c.finalize()


class UdpAudioChannel:
    """一个设备的 UDP 音频加解密通道。"""

    def __init__(self, key: bytes, nonce: bytes,
                 logger: Optional[Callable[[str], None]] = None,
                 max_payload: int = 4096):
        if len(key) != 16 or len(nonce) != 16:
            raise ValueError("key / nonce 必须是 16 字节")
        self.key = bytes(key)
        self.nonce = bytes(nonce)
        self._log = logger or (lambda m: None)
        self.remote_sequence = 0
        self.local_sequence = 0
        self.max_payload = int(max_payload)
        self._warned_gap = False

    # ---------- 接收 ----------

    def decrypt(self, datagram: bytes) -> Tuple[bytes, int, int]:
        """解密一个 UDP 包 → (opus_payload, flags, sequence)。

        非法/重放包抛 ValueError（调用方丢弃即可）。
        """
        if len(datagram) < _HDR_SIZE:
            raise ValueError("包太短")
        mtype, flags, plen, _ssrc, _ts, seq = _HDR.unpack_from(datagram, 0)
        if mtype != _TYPE:
            raise ValueError(f"类型不是 0x01（{mtype:#x}）")
        payload = datagram[_HDR_SIZE:]
        if plen and plen > len(payload):
            raise ValueError("声明长度超过实际负载")
        if seq <= self.remote_sequence:
            raise ValueError(f"重放/乱序丢弃（seq={seq} <= "
                             f"{self.remote_sequence}）")
        if seq != self.remote_sequence + 1 and not self._warned_gap:
            self._warned_gap = True
            self._log(f"UDP 序号不连续：{self.remote_sequence} → {seq}")
        # 计数器块 = 原始 16 字节头
        plain = _aes_ctr(self.key, datagram[:_HDR_SIZE], payload[:plen or None])
        self.remote_sequence = seq
        return (plain, flags, seq)

    # ---------- 发送 ----------

    def encrypt(self, opus: bytes, timestamp: int = 0,
                flags: int = 0) -> bytes:
        """打包一个下行 UDP 包。"""
        self.local_sequence = (self.local_sequence + 1) & 0xFFFFFFFF
        seq = self.local_sequence
        # 计数器块：以 nonce 为模板，覆写长度/时间戳/序号（与固件对称）
        block = bytearray(self.nonce)
        block[0] = _TYPE
        block[1] = flags & 0xFF
        struct.pack_into(">H", block, 2, len(opus) & 0xFFFF)
        struct.pack_into(">I", block, 8, int(timestamp) & 0xFFFFFFFF)
        struct.pack_into(">I", block, 12, seq)
        head = bytes(block)
        return head + _aes_ctr(self.key, head, bytes(opus))


def make_nonce() -> bytes:
    """生成一个合法的服务端 nonce（[0] 固定 0x01，其余随机）。"""
    import os
    b = bytearray(os.urandom(16))
    b[0] = _TYPE
    return bytes(b)


def make_key() -> bytes:
    import os
    return os.urandom(16)
