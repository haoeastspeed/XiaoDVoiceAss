# -*- coding: utf-8 -*-
"""二进制帧编解码（协议 v1/v2/v3）。"""
from gateway.binframe.codec import FrameCodec, clamp_version, SUPPORTED_VERSIONS

__all__ = ["FrameCodec", "clamp_version", "SUPPORTED_VERSIONS"]
