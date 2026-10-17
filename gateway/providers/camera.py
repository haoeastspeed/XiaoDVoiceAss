# -*- coding: utf-8 -*-
"""摄像头采集（OpenCV，延迟导入）。

只在真正拍图时才 import cv2 —— 打包版 cv2 体积大（~60MB），不能让"没配摄像头"
的用户也付出解包代价。摄像头用后立即释放，避免长期占用（很多设备只允许一个
进程占用摄像头）。
"""
from __future__ import annotations

import io
from typing import Optional


class Camera:
    def __init__(self, index: int = 0, max_bytes: int = 5 * 1024 * 1024):
        self.index = int(index)
        self.max_bytes = int(max_bytes)

    def capture_jpeg(self) -> bytes:
        """抓一帧并编码为 JPEG；失败抛异常（调用方转工具错误结果）。"""
        try:
            import cv2  # noqa: PLC0415
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("未安装 OpenCV，无法使用摄像头") from e

        cap = cv2.VideoCapture(self.index)
        if not cap or not cap.isOpened():
            raise RuntimeError("摄像头打开失败（可能被占用或无权限）")
        try:
            ok, frame = cap.read()
        finally:
            cap.release()
        if not ok or frame is None:
            raise RuntimeError("摄像头读取失败")
        quality = 85
        data = b""
        for _ in range(6):
            ok2, buf = cv2.imencode(".jpg", frame,
                                    [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            if not ok2:
                raise RuntimeError("图像编码失败")
            data = buf.tobytes()
            if len(data) <= self.max_bytes:
                break
            quality -= 15
        return data

    @staticmethod
    def is_available() -> bool:
        try:
            import cv2  # noqa: PLC0415  # noqa: F401
            return True
        except Exception:  # noqa: BLE001
            return False
