# -*- coding: utf-8 -*-
"""小D语音陪聊助手 —— 统一启动入口（源码运行与打包都走这里）。

    python run_assistant.py              # 打开图形界面
    python run_assistant.py --gateway    # 只跑语音网关（应用会自动这样拉起自己）
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from app.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
