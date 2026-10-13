# -*- coding: utf-8 -*-
"""Opus 编解码封装（上行 16k / 下行 24k）。"""
from __future__ import annotations

import os

# opus.dll 必须在 import opuslib_next 之前可被找到
from gateway.config import BIN_DIR

if os.path.isdir(BIN_DIR):
    os.environ["PATH"] = BIN_DIR + os.pathsep + os.environ.get("PATH", "")

import opuslib_next  # noqa: E402


class UplinkDecoder:
    """上行 opus（16000Hz/1ch/60ms）→ s16le PCM。"""

    def __init__(self, sample_rate: int = 16000, frame_size: int = 960):
        self._dec = opuslib_next.Decoder(sample_rate, 1)
        self.frame_size = frame_size

    def decode(self, packet: bytes) -> bytes:
        if not packet:
            return b""
        return self._dec.decode(packet, self.frame_size)


class DownlinkEncoder:
    """s16le PCM（24000Hz/1ch/60ms）→ opus 包。"""

    def __init__(self, sample_rate: int = 24000, frame_size: int = 1440):
        self._enc = opuslib_next.Encoder(
            sample_rate, 1, opuslib_next.APPLICATION_VOIP
        )
        self.frame_size = frame_size
        self._frame_bytes = frame_size * 2

    def encode_pcm(self, pcm: bytes) -> list[bytes]:
        """把任意长度 s16le PCM 按帧编码为 opus 包列表。"""
        packets: list[bytes] = []
        fb = self._frame_bytes
        n = len(pcm)
        i = 0
        while i + fb <= n:
            packets.append(self._enc.encode(pcm[i:i + fb], self.frame_size))
            i += fb
        return packets
