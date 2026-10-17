# -*- coding: utf-8 -*-
"""视觉上传端点（对齐官方 core/api/vision_handler.py）。

  POST /mcp/vision/explain   multipart 表单：question(文本) + image(文件)

设备侧（ESP32 注册了 self.camera.take_photo 的板子）拍完照会把图 POST 到这里，
服务端交给视觉大模型回答后回文本。网关在 MCP initialize 里通过
capabilities.vision{url,token} 把这个地址告知设备。
"""
from __future__ import annotations

import re
from typing import Optional

from gateway.http.httpd import GatewayHTTP, json_response

_CT_RE = re.compile(r"boundary=\"?([^\";]+)\"?", re.I)
_ALLOWED = ("image/jpeg", "image/png", "image/gif", "image/bmp",
            "image/tiff", "image/webp")


def parse_multipart(body: bytes, content_type: str):
    """极简 multipart 解析：返回 {name: (filename, content_type, data)}。"""
    m = _CT_RE.search(content_type or "")
    if not m:
        return {}
    boundary = m.group(1).strip().encode("utf-8")
    delim = b"--" + boundary
    parts = {}
    for chunk in body.split(delim):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        if chunk.endswith(b"--"):
            chunk = chunk[:-2].rstrip(b"\r\n")
        head_end = chunk.find(b"\r\n\r\n")
        if head_end < 0:
            continue
        head = chunk[:head_end].decode("utf-8", "replace")
        data = chunk[head_end + 4:]
        name = ""
        filename = ""
        ptype = ""
        for line in head.split("\r\n"):
            low = line.lower()
            if low.startswith("content-disposition"):
                nm = re.search(r'name="([^"]*)"', line)
                if nm:
                    name = nm.group(1)
                fn = re.search(r'filename="([^"]*)"', line)
                if fn:
                    filename = fn.group(1)
            elif low.startswith("content-type"):
                ptype = line.split(":", 1)[1].strip()
        if name:
            parts[name] = (filename, ptype, data)
    return parts


class VisionHandler:
    def __init__(self, config, logger=None):
        self.config = config
        self._log = logger or (lambda m: print(f"[vision] {m}", flush=True))

    def register(self, app: GatewayHTTP):
        app.route("POST", r"/mcp/vision/explain/?$", self.handle_explain)

    def handle_explain(self, body, headers, match) -> tuple:
        import asyncio

        ctype = headers.get("content-type", "")
        if "multipart/form-data" not in ctype.lower():
            return json_response({"success": False,
                                  "message": "expect multipart/form-data"}, 400)
        parts = parse_multipart(body, ctype)
        if not parts:
            return json_response({"success": False,
                                  "message": "empty form"}, 400)

        question = b""
        if "question" in parts:
            question = parts["question"][2]
        image = None
        for key in ("image", "file", "photo"):
            if key in parts:
                image = parts[key]
                break
        if image is None:
            return json_response({"success": False,
                                  "message": "no image"}, 400)
        filename, ptype, data = image
        max_bytes = int(getattr(self.config, "vision_max_bytes",
                                5 * 1024 * 1024) or 5 * 1024 * 1024)
        if len(data) > max_bytes:
            return json_response({"success": False,
                                  "message": "image too large"}, 413)
        fmt = "jpeg"
        if ptype in _ALLOWED:
            fmt = ptype.split("/", 1)[1]
        elif filename.lower().endswith(".png"):
            fmt = "png"

        q = question.decode("utf-8", "replace").strip() or "请描述这张图片。"

        from gateway.providers.llm_vision import from_config
        vlm = from_config(self.config)
        if vlm is None or not vlm.ready:
            return json_response({"success": False,
                                  "message": "vision model not configured"}, 200)
        try:
            text = asyncio.run(self._describe(vlm, data, q, fmt))
        except Exception as e:  # noqa: BLE001
            self._log(f"视觉推理失败：{type(e).__name__}: {e}")
            return json_response({"success": False,
                                  "message": f"vision failed: {e}"}, 200)
        return json_response({"success": True, "text": text or "(模型没有返回内容)"})

    @staticmethod
    async def _describe(vlm, data, question, fmt):
        try:
            return await vlm.describe(data, question, fmt)
        finally:
            await vlm.aclose()
