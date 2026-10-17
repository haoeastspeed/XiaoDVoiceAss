# -*- coding: utf-8 -*-
"""回归测试：界面选的音色必须下发 + 结束聊天必须立刻不响。

跑法：
    C:/Python314/python.exe tests/test_stop_and_voice.py

不需要真麦克风、真扬声器、真网关、大模型（假声卡流 + 假网关）。

覆盖两个已修 bug：
  1. `RealtimeVoiceClient.__init__` 把 `voice` 参数丢掉了（硬编码成 None），
     于是 `if self.voice:` 恒为假 → 会话开局从不下发 `set_voice` → 网关一直用
     它自己存的旧音色（表现为"列表里是女声、回复却是男声"）。
  2. `stop()` 只置 `_stop_flag`：下行协程还阻塞在 `recv()` 上，
     `asyncio.gather(uplink(), downlink())` 不会返回，`finally` 里的关流执行
     不到 → 已经收进播放队列的音频按实时速度继续放完（"点了结束聊天还在念"）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["PATH"] = (os.path.join(ROOT, "reference", "bin") + os.pathsep
                      + os.environ.get("PATH", ""))

import numpy as np  # noqa: E402
import opuslib_next  # noqa: E402
from websockets.asyncio.server import serve  # noqa: E402

import app.voice_client as vc  # noqa: E402
from app.voice_client import RealtimeVoiceClient  # noqa: E402

WANT = "zh-CN-XiaoyiNeural"
OTHER = "zh-CN-YunxiNeural"
RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((bool(cond), name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))
    return bool(cond)


# ---------------------------------------------------------------- 假网关
class FakeGateway:
    """只做两件事：记录客户端发来的控制消息；往下行灌音频。"""

    def __init__(self, packets: int = 30):
        self.msgs: list[dict] = []
        self.packets = packets
        self.closed = False
        self.audio_sent = 0
        self.port = 0
        self._server = None
        self._enc = opuslib_next.Encoder(24000, 1, opuslib_next.APPLICATION_VOIP)

    def _pkt(self) -> bytes:
        n = 1440                                   # 60ms @24k
        x = (np.sin(2 * np.pi * 440 * np.arange(n) / 24000) * 12000)
        return self._enc.encode(x.astype(np.int16).tobytes(), n)

    async def _handler(self, ws):
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                m = json.loads(raw)
                self.msgs.append(m)
                if (m.get("type") == "listen" and m.get("state") == "start"
                        and self.audio_sent == 0):
                    await ws.send(json.dumps({"type": "tts", "state": "start"}))
                    for _ in range(self.packets):
                        await ws.send(self._pkt())
                        self.audio_sent += 1
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.closed = True

    async def start(self):
        self._server = await serve(self._handler, "127.0.0.1", 0, max_size=None)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        self._server.close()
        await self._server.wait_closed()


class FakeStream:
    """替掉真实声卡流：只把回调留下来，供测试手动喂缓冲。"""

    def __init__(self, is_out: bool, callback):
        self.is_out = is_out
        self.callback = callback
        self.latency = 0.02
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def close(self):
        self.closed = True


async def _wait(pred, timeout: float = 8.0, step: float = 0.05) -> bool:
    for _ in range(int(timeout / step)):
        if pred():
            return True
        await asyncio.sleep(step)
    return False


# ---------------------------------------------------------------- 1 & 2 纯对象
def sec_object_state() -> None:
    print("\n== 1) 音色状态：构造 / set_voice 都必须记住 ==")
    c = RealtimeVoiceClient("ws://127.0.0.1:1/xiaozhi/v1/", voice=WANT)
    check("构造时保留传入的 voice", c.voice == WANT, f"voice={c.voice!r}")

    c.set_voice(OTHER)
    check("set_voice 同步更新 self.voice（重连要重发）",
          c.voice == OTHER, f"voice={c.voice!r}")

    c2 = RealtimeVoiceClient("ws://127.0.0.1:1/xiaozhi/v1/")
    check("未指定音色时保持 None（不会瞎下发）", c2.voice is None,
          f"voice={c2.voice!r}")


# ---------------------------------------------------------------- 3 & 4 真链路
async def sec_live() -> None:
    print("\n== 2) 真会话（假网关 + 假声卡流）：开局下发 set_voice ==")
    gw = FakeGateway()
    await gw.start()

    captured: dict[str, FakeStream] = {}
    real_open = vc._open_stream

    def fake_open(factory, rate, blocks, callback, candidates, to_ui=None):
        s = FakeStream(factory is vc.sd.RawOutputStream, callback)
        captured["out" if s.is_out else "in"] = s
        return s

    vc._open_stream = fake_open
    events: list[dict] = []

    def feed():
        return b"\x00" * 1920                      # 16k/60ms 静音帧

    cli = RealtimeVoiceClient(
        f"ws://127.0.0.1:{gw.port}/xiaozhi/v1/",
        on_event=events.append, audio_feed=feed,
        play_output=True, echo_guard=False, voice=WANT)
    try:
        cli.start()
        got = await _wait(lambda: any(m.get("type") == "set_voice" for m in gw.msgs)
                          and cli._out_pending > 0)
        voices = [m.get("voice") for m in gw.msgs if m.get("type") == "set_voice"]
        check("会话开局确实下发了 set_voice", bool(voices), f"收到的消息={gw.msgs}")
        check("下发的是界面选的那个音色", voices and voices[0] == WANT,
              f"下发={voices}")
        check("下行音频已进入播放队列", cli._out_pending > 0,
              f"out_pending={cli._out_pending}")
        check("拿到声卡输出回调（假流）", "out" in captured,
              f"captured={list(captured)}")
        if not got or "out" not in captured:
            return
        cb = captured["out"].callback

        # --- 停止前的对照：回调必须真的在写声音（否则下面的"静音"断言没意义）
        buf = bytearray(1440 * 2)
        cb(buf, 1440, None, None)
        played = bytes(buf) != b"\x00" * len(buf)
        check("停止前声卡回调输出的是真实音频（对照）", played)

        print("\n== 3) 结束聊天：必须立刻静音 + 收掉连接 ==")
        cli.stop()
        check("stop() 后 is_running() 变 False", not cli.is_running())

        buf2 = bytearray(1440 * 2)
        cb(buf2, 1440, None, None)
        check("停止后声卡回调一律输出静音",
              bytes(buf2) == b"\x00" * len(buf2),
              f"非零字节={sum(1 for b in buf2 if b)}")

        closed = await _wait(lambda: gw.closed)
        check("连接被主动收掉（阻塞的 recv 得以结束）", closed)
        check("结束前先发了 abort（网关停止继续合成）",
              any(m.get("type") == "abort" for m in gw.msgs),
              f"消息类型={[m.get('type') for m in gw.msgs]}")
        cli._thread.join(timeout=3)
        check("客户端线程已退出", not cli._thread.is_alive())
    finally:
        vc._open_stream = real_open
        try:
            cli.stop()
        except Exception:  # noqa: BLE001
            pass
        await gw.stop()


def main() -> int:
    print("=" * 62)
    print("回归：音色下发 / 结束聊天立刻静音")
    print("=" * 62)
    sec_object_state()
    asyncio.run(sec_live())

    bad = [r for r in RESULTS if not r[0]]
    print("\n" + "=" * 62)
    print(f"共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    for _, name, detail in bad:
        print(f"  FAIL  {name}   {detail}")
    print("==>", "全部通过" if not bad else "存在失败项")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
