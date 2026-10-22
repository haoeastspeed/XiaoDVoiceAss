# -*- coding: utf-8 -*-
"""音色稳定性回归：交互过程中音色不许自己变。

用户实测现象（原话）："我发现你好像音色就是两个音色在切换"。
三条成因，这里逐条钉死：

  1. 在线合成偶发失败 → 降级本地 → 冷却到期又切回在线 → 对话中反复换人。
     修法：降级后**粘滞**，对话没停（距上次合成 < _STICKY_IDLE_S）就一直本地。
  2. TTS 是全局单例，多连接共享：任何一个连接下发 set_voice 都会改所有人的
     音色。修法：会话持有自己的 voice，开口前 apply_voice 对齐（不碰退避）。
  3. 客户端开局同步被当成"用户选择"写进 .tmp/gw_overrides.json，与真正的主
     人配置互相覆盖。修法：set_voice 带 persist，开局同步 persist=false。

用法：python tests/test_voice_stability.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway.providers.tts_router import (  # noqa: E402
    TTSRouter, _STICKY_IDLE_S, _OK_STREAK_RESET)

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (
        ("  | " + str(extra)) if extra else ""))


class FakeTTS(TTSRouter):
    """把在线/本地两条支路换成可控的假实现。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.online_ok = True
        self.engine_log = []      # 每句实际用的引擎

    async def _stream_online(self, text, style):
        if not self.online_ok:
            raise RuntimeError("No audio was received")
        self.engine_log.append("online")
        for i in range(3):
            yield b"\x00\x01" * 8

    async def _stream_local(self, text, style):
        self.engine_log.append("local")
        for i in range(3):
            yield b"\x00\x02" * 8


async def say(r, text="你好。"):
    n = 0
    async for _ in r.stream(text):
        n += 1
    return n


async def main():
    print("== 1. 降级粘滞：对话进行中不许切回在线 ==")
    r = FakeTTS("zh-CN-XiaoxiaoNeural")
    r.online_ok = True
    await say(r)
    check("一开始走在线", r.engine_log[-1] == "online", r.engine_log)

    # 在线挂一次（模拟 EdgeTTS 偶发 No audio was received）
    r.online_ok = False
    await say(r)
    check("在线失败 → 降级本地", r.engine_log.count("local") == 1, r.engine_log)
    check("降级后进入粘滞", r._sticky_local)

    # 关键：在线"看起来恢复了"，但对话还在继续 —— 不许切回在线
    r.online_ok = True
    r._down_until = 0.0          # 强行让冷却到期（最坏情况）
    for _ in range(5):
        await say(r)
    check("对话中连续 5 句仍是本地（不再两个音色轮换）",
          r.engine_log.count("local") == 6 and r.engine_log[-1] == "local",
          r.engine_log)

    print("== 2. 对话停了才回在线 ==")
    r._last_synth_ts = time.monotonic() - (_STICKY_IDLE_S + 5)
    await say(r)
    check("空闲超过阈值 → 恢复在线", r.engine_log[-1] == "online", r.engine_log)
    check("恢复后粘滞解除", not r._sticky_local)

    print("== 3. 用户显式换音色 → 立刻给在线新机会，不再粘着本地 ==")
    r2 = FakeTTS("zh-CN-XiaoxiaoNeural")
    r2.online_ok = False
    await say(r2)
    r2.online_ok = True
    r2.set_voice("zh-CN-YunxiNeural")
    await say(r2)
    check("显式换音色后立刻回到在线", r2.engine_log[-1] == "online",
          r2.engine_log)
    check("音色已切换", r2.voice == "zh-CN-YunxiNeural")

    print("== 4. 按会话对齐音色：不影响在线可用性判定 ==")
    r3 = FakeTTS("zh-CN-XiaoxiaoNeural")
    r3.online_ok = False
    await say(r3)                       # 降级
    before = (r3.voice, r3._down_until, r3._sticky_local)
    r3.apply_voice("zh-TW-HsiaoChenNeural")   # 另一个会话同步自己的音色
    check("apply_voice 换了音色", r3.voice == "zh-TW-HsiaoChenNeural")
    check("apply_voice 不动冷却", r3._down_until == before[1])
    check("apply_voice 不动粘滞", r3._sticky_local == before[2])
    check("本地兜底音色同步跟着换",
          r3._local.voice_id and r3._local.voice_id != "", r3._local.voice_id)
    # 会话自己的音色不会被别人的 set_voice 带走（session 层每句前 apply）
    r3.set_voice("en-US-JennyNeural")
    r3.apply_voice("zh-TW-HsiaoChenNeural")
    check("共享实例可被会话重新对齐回来",
          r3.voice == "zh-TW-HsiaoChenNeural")

    print("== 5. 在线连续成功才重置退避（不会被单句抖动骗过去）==")
    r4 = FakeTTS("zh-CN-XiaoxiaoNeural")
    r4.online_ok = False
    await say(r4)
    fs = r4._fail_streak
    r4.online_ok = True
    # 模拟"用户停了一会儿"：冷却过 + 粘滞窗口过 → 允许回在线
    r4._down_until = 0.0
    r4._sticky_local = False
    r4._last_synth_ts = time.monotonic() - (_STICKY_IDLE_S + 5)
    for _ in range(_OK_STREAK_RESET):
        await say(r4)
    check("连续成功后失败计数归零", r4._fail_streak == 0, f"{fs} → {r4._fail_streak}")

    print("== 6. 每句都有音频产出（降级不会哑掉）==")
    r5 = FakeTTS("zh-CN-XiaoxiaoNeural")
    r5.online_ok = False
    n = await say(r5)
    check("降级句仍有音频包", n == 3, n)

    print()
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
    print("VOICE_STABLE_PASS" if not FAIL else "VOICE_STABLE_FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
