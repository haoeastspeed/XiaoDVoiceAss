# -*- coding: utf-8 -*-
"""小D网关独立入口。

用法（用 M1 的 venv，依赖齐全）：
  reference\\xiaozhi-server\\main\\xiaozhi-server\\.venv\\Scripts\\python.exe main.py
可选配置覆盖文件：
  python main.py --config gateway.json
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# 确保项目根目录在 sys.path（便于 gateway.* 导入）
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from gateway.config import load_config  # noqa: E402
from gateway.server import XiaoDGateway  # noqa: E402


async def amain(config_path: str | None):
    cfg = load_config(config_path)
    gateway = XiaoDGateway(cfg)
    await gateway.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="小D实时语音网关")
    parser.add_argument("--config", default=None, help="JSON 配置覆盖文件")
    args = parser.parse_args()
    try:
        asyncio.run(amain(args.config))
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
