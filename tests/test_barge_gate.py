# -*- coding: utf-8 -*-
"""语音打断判据回归：自激活 / 打不断 / 噪声防护 / 句间间隙。

不依赖真机：用合成的"下行播放帧 + 麦克风帧"直接驱动 `_gate_frame`。
跑法：C:/Python314/python.exe tests/test_barge_gate.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from app.voice_client import RealtimeVoiceClient

SR = 16000


def tone(rms: float, hz: int = 180, n: int = 960) -> bytes:
    t = np.arange(n) / SR
    x = np.sin(2 * np.pi * hz * t) * (rms * np.sqrt(2))
    return np.clip(x, -32768, 32767).astype(np.int16).tobytes()


def mic_frame(echo: float, speech: float = 0.0) -> bytes:
    f = np.frombuffer(tone(echo, 180), dtype=np.int16).astype(np.float32)
    if speech > 0:
        f += np.frombuffer(tone(speech, 320), dtype=np.int16).astype(np.float32)
    return np.clip(f, -32768, 32767).astype(np.int16).tobytes()


def new_client(factor: float | None = None, frames: int = 8,
               floor: float = 30.0) -> RealtimeVoiceClient:
    kw = dict(play_output=True, echo_guard=True, allow_barge_in=True,
              barge_frames=frames)
    if factor is not None:
        kw["barge_factor"] = factor
    c = RealtimeVoiceClient("ws://127.0.0.1:8000/xiaozhi/v1/", **kw)
    c._floor = floor
    c._resp_active = True            # 协议层：正在播报
    return c


def feed(c, echo: float, speech: float = 0.0, n: int = 8):
    """连续喂 n 帧麦克风；返回在第几帧触发（None＝没触发）。"""
    for i in range(n):
        c._barge_mute_until = 0.0
        c._barge_armed = True
        if c._gate_frame(mic_frame(echo, speech)):
            return i + 1
    return None


def learn(c, att: float, play: float, n: int = 24):
    """先让耦合估计从纯回声里学出来（模拟播放进行了几秒）。"""
    c._note_down_level(tone(play, 220))
    for _ in range(n):
        feed(c, att * play, 0.0, n=1)


PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = ""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def main() -> int:
    PLAY = 2084.0        # 下行播放电平（实测 TTS  RMS 量级）

    # ---------- 1) 纯回声绝不能触发（自激活防护） ----------
    print("\n1) 自激活防护：只放音、没人说话，任何耦合波动都不该触发")
    for label, att, jump in (("外放·耦合 0.20", 0.20, 1.0),
                             ("外放·回声突增 30%", 0.20, 1.3),
                             ("外放·回声突增 60%", 0.20, 1.6),
                             ("外放·回声突增 100%", 0.20, 2.0),
                             ("外放·回声突降 50%", 0.20, 0.5),
                             ("近讲·耦合 0.02", 0.02, 1.0)):
        c = new_client()
        learn(c, att, PLAY)
        fired = feed(c, att * PLAY * jump)
        check(f"{label}（att 已学到 {c._att:.3f}）", fired is None,
              f"mic≈{att * PLAY * jump:.0f} thr={c.last_gate['thr']:.0f}"
              + (f" 第{fired}帧误触发" if fired else ""))

    # ---------- 2) 戴耳机/近讲：正常说话必须能打断 ----------
    print("\n2) 戴耳机 / 近讲（回声极小）：说话就该能打断")
    c = new_client()
    learn(c, 0.02, PLAY)
    for _ in range(5):
        c._track_mic_ref(600.0)          # 用户平时说话电平
    for sp in (200, 400, 800, 1500):
        fired = feed(c, 0.02 * PLAY, sp)
        check(f"说话 RMS={sp}", fired is not None,
              f"mic={c.last_gate['mic']:.0f} thr={c.last_gate['thr']:.0f}")

    # ---------- 3) 外放：大声能打断；小声给提示而不是乱触发 ----------
    print("\n3) 外放（回声明显）：盖过回声能打断，盖不过就提示")
    c = new_client()
    learn(c, 0.20, PLAY)
    for _ in range(5):
        c._track_mic_ref(1500.0)
    for sp in (800, 1500):
        fired = feed(c, 0.20 * PLAY, sp)
        check(f"说话 RMS={sp}", fired is not None,
              f"mic={c.last_gate['mic']:.0f} thr={c.last_gate['thr']:.0f}")
    fired = feed(c, 0.20 * PLAY, 200)
    check("说话 RMS=200（盖不过回声）→ 不乱触发", fired is None,
          f"thr={c.last_gate['thr']:.0f}（改用按钮/Esc，会有不可用提示）")

    # ---------- 4) "有时打不断"：大喊之后轻声说话仍能打断 ----------
    print("\n4) 曾经大喊过，之后正常音量说话还能不能打断（分位数参考）")
    c = new_client()
    learn(c, 0.02, PLAY)
    for _ in range(40):
        c._track_mic_ref(500.0)          # 平时都是这个音量
    for _ in range(3):
        c._track_mic_ref(4000.0)         # 偶然喊了一嗓子
    ref = c._mic_ref
    fired = feed(c, 0.02 * PLAY, 500)
    check(f"大喊后正常说话（参考电平={ref:.0f}，未被打到 4000）", fired is not None,
          f"mic={c.last_gate['mic']:.0f} thr={c.last_gate['thr']:.0f}")

    # ---------- 5) 句间间隙：播放停了、房间还有声，不能触发 ----------
    print("\n5) 句间间隙（播放电平塌底）+ 房间噪声：不能自激活")
    c = new_client()
    learn(c, 0.20, PLAY)
    for _ in range(5):
        c._track_mic_ref(1500.0)
    # 播放进入停顿：下行只剩极低电平，麦克风是房间噪声 250
    for _ in range(30):
        c._note_down_level(tone(6.0, 220))
    fired = feed(c, 250.0, 0.0, n=10)
    check("播放塌底 + 噪声 250 持续 10 帧", fired is None,
          f"play={c.last_gate['play']:.0f} thr={c.last_gate['thr']:.0f}")
    # 间隙里的高比值不能把耦合估计打爆（否则下一句又打不断）
    att_after = c._att
    learn(c, 0.20, PLAY)
    check(f"间隙后耦合估计没被污染（att={att_after:.3f} → {c._att:.3f}）",
          abs(c._att - att_after) < 0.02)
    fired = feed(c, 0.20 * PLAY, 1500)
    check("间隙之后大声说话仍可打断", fired is not None)

    # ---------- 6) 持续噪声防护（底噪自适应下限） ----------
    print("\n6) 嘈杂环境（底噪 90）：持续噪声不该触发，说话可以")
    c = new_client(floor=90.0)
    learn(c, 0.20, PLAY)
    for _ in range(5):
        c._track_mic_ref(1500.0)
    fired = feed(c, 0.20 * PLAY, 0.0)     # 纯回声
    check("纯回声", fired is None)
    fired = feed(c, 0.20 * PLAY + 300, 0.0, n=10)   # 回声 + 持续噪声
    check("回声 + 持续噪声 300", fired is None,
          f"thr={c.last_gate['thr']:.0f}")
    fired = feed(c, 0.20 * PLAY + 300, 1500)
    check("回声 + 噪声 + 正常说话", fired is not None)

    # ---------- 7) 打断后的防抖 ----------
    print("\n7) 打断后短时间内不重复触发")
    c = new_client()
    learn(c, 0.20, PLAY)
    fired = feed(c, 0.20 * PLAY, 1500)
    check("第一次能触发", fired is not None)
    import time as _t
    c._barge_mute_until = _t.monotonic() + 1.0
    fired2 = None
    for _ in range(8):                      # 注意：不再重置 mute，模拟真实静音期
        if c._gate_frame(mic_frame(0.20 * PLAY, 1500)):
            fired2 = True
            break
    check("静音期内不重复触发", fired2 is None)

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print("  ✗", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
