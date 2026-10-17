# -*- coding: utf-8 -*-
""""自己接自己的话"文字回环防线回归：唤醒词-only 的 detect 自激活丢弃。

背景：设备端把扬声器尾音误判成唤醒时，会把「你好小智」以 listen detect
文本推回来。旧防线两个漏洞：
  1. _is_self_echo 要求 >=6 字才比对 —— "你好小智"只有 4 字永远漏网；
  2. 播放/冷却窗只有 700ms —— 唤醒引擎滞后触发常在窗外。
新防线：detect 文本只是唤醒词本身时，若刚播报完（selfwake_window_s 内）
且无 abort → 丢弃。

跑法：
    C:/Python314/python.exe tests/test_selfwake_detect.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["PATH"] = (os.path.join(ROOT, "reference", "bin") + os.pathsep
                      + os.environ.get("PATH", ""))

PASS = 0
FAIL: list[str] = []


def ok(name: str, cond, note: str = "") -> bool:
    global PASS
    if cond:
        PASS += 1
        print(f"  ok   {name}" + (f"  {note}" if note else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}" + (f"  {note}" if note else ""))
    return bool(cond)


def _mk_session():
    """构造一个最小 VoiceSession（不连 ws，只测判定逻辑）。"""
    from gateway.config import GatewayConfig
    from gateway.session import VoiceSession
    cfg = GatewayConfig()
    s = VoiceSession.__new__(VoiceSession)
    s.config = cfg
    s._last_abort_ts = 0.0
    s._last_spoken_ts = 0.0
    s._spoken = []
    return s


def t_wake_text() -> None:
    print("=" * 72)
    print("1) 唤醒词文本识别（含标点/大小写归一化）")
    from gateway.session import _is_wake_word_text
    ok("你好小智 命中", _is_wake_word_text("你好小智"))
    ok("你好，小智。 命中（标点归一）", _is_wake_word_text("你好，小智。"))
    ok("HI ESP 命中（大小写）", _is_wake_word_text("HI ESP"))
    ok("真实话语不命中", not _is_wake_word_text("把客厅灯关了"))
    ok("带唤醒词的整句不命中（那是真话语）",
       not _is_wake_word_text("你好小智今天天气怎么样"))


def t_selfwake_window() -> None:
    print("=" * 72)
    print("2) 自激活窗口：刚播报完的唤醒词 detect 丢弃，窗口外/真打断放行")
    s = _mk_session()
    now = time.monotonic()
    s._last_spoken_ts = now - 1.0      # 1s 前刚说完话
    ok("窗口内 → 自激活丢弃",
       s._is_self_activated_wake("你好小智", recent_abort=False))
    s._last_spoken_ts = now - 10.0     # 窗口（6s）外
    ok("窗口外 → 放行（正常唤醒问候）",
       not s._is_self_activated_wake("你好小智", recent_abort=False))
    s._last_spoken_ts = now - 1.0
    ok("刚有 abort → 放行（真打断优先）",
       not s._is_self_activated_wake("你好小智", recent_abort=True))
    ok("非唤醒词文本 → 交给后续流程",
       not s._is_self_activated_wake("把客厅灯关了", recent_abort=False))


def t_detect_end2end() -> None:
    print("=" * 72)
    print("3) 端到端：回环场景 detect(你好小智) 不产生任何响应")
    from gateway.config import GatewayConfig
    from gateway.session import VoiceSession
    from gateway.providers.base import BaseLLM

    class _FakeTransport:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            self.sent.append(data)

    class _NoLLM(BaseLLM):
        async def chat(self, messages):
            raise AssertionError("不应调用 LLM")
            yield ""

    cfg = GatewayConfig()
    s = VoiceSession.__new__(VoiceSession)
    s.config = cfg
    s.ws = _FakeTransport()
    s._last_abort_ts = 0.0
    s._last_spoken_ts = time.monotonic() - 1.0
    s._spoken = []
    s._response_task = None
    s._gate_on = lambda: False   # 不在播放期/冷却窗（走第三道判定）
    # 最小依赖：_start_response 前的判定路径不触碰其它成员
    asyncio.run(VoiceSession._on_listen(s, {
        "type": "listen", "state": "detect", "text": "你好小智"}))
    ok("detect(你好小智) 被丢弃（未开响应、未发 stt）",
       not s.ws.sent, f"sent={s.ws.sent[:2]}")


if __name__ == "__main__":
    t_wake_text()
    t_selfwake_window()
    t_detect_end2end()
    print("=" * 72)
    print(f"SELFWAKE_{'PASS' if not FAIL else 'FAIL'} {PASS}/{PASS + len(FAIL)}")
    sys.exit(1 if FAIL else 0)
