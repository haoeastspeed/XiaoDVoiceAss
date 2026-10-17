# -*- coding: utf-8 -*-
"""网关侧回声门回归（防自激活）。

三层防线，本文件逐层验证：
  A/B/C  硬闸门 —— 播放期与尾音冷却期，上行音频被吃掉：不跑 VAD、
         不累积、不切 listening、不触发 ASR；
  D      开关兼容 —— echo_gate=False 时退回旧行为（播放期照跑 VAD 判打断）；
  E      冷却窗只能由 tts_stop 开启，由 abort / 主动聆听清除；
  F/G    AEC —— 合成回声被压制、用户语音保留；库不可用时安全旁路；
  H      集成 —— AEC 在位时回声不误打断，用户真说话能打断；
  H2     生产同构 —— far 走 opus 编码→_send→会话内解码后进 AEC 仍有效。

跑法：C:\\Python314\\python.exe tests/test_echo_gate.py
全部用假件驱动：不连真机、不加载真模型（H2 走真实 opus 编解码）。
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from gateway.audio.aec import EchoCanceller, _resample  # noqa: E402
from gateway.audio.opus import DownlinkDecoder, DownlinkEncoder  # noqa: E402
from gateway.config import GatewayConfig  # noqa: E402
from gateway.session import VoiceSession  # noqa: E402

UP_SR, DOWN_SR = 16000, 24000
UP_FRAME = 960    # 60ms @16k
DOWN_FRAME = 1440  # 60ms @24k

_pass: list[str] = []
_fail: list[str] = []


def check(name: str, cond: bool, extra: str = ""):
    (_pass if cond else _fail).append(name)
    tail = f"  [{extra}]" if extra else ""
    print(("  ok   " if cond else "  FAIL ") + name + tail)


# ---------------- 假件 ----------------

class FakeWS:
    def __init__(self):
        self.sent: list = []

    async def send(self, data):
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class PassthroughDecoder:
    """绕过 opus：上行包直接当 PCM（本文件只关心闸门，不关心封装格式）。"""

    def decode(self, packet):
        return bytes(packet)


class EnergyVAD:
    """按能量判语音的假 VAD：足够验证"回声有没有被消掉"。"""

    def __init__(self, thr: float = 800.0):
        self.thr = thr
        self.calls = 0
        self.resets = 0

    def new_state(self):
        return {"on": False}

    def reset_sentence(self, st):
        self.resets += 1
        st["on"] = False

    def process(self, st, pcm):
        self.calls += 1
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
        r = math.sqrt(float(np.mean(x * x))) if x.size else 0.0
        st["on"] = r > self.thr
        return st["on"], False


class FakeASR:
    def __init__(self):
        self.calls = 0

    def transcribe(self, pcm, spans):
        self.calls += 1
        return ""          # 不触发 LLM/TTS，测试只关心"有没有走到 ASR"


class FakeTTS:
    degraded = False
    voice = "fake"

    def stream(self, text, style=None):
        async def _gen():
            if False:      # pragma: no cover - 只为让它是个异步生成器
                yield b""
        return _gen()


class FakeLLM:
    model = "fake"

    async def chat(self, messages):
        if False:          # pragma: no cover
            yield ""

    async def quick(self, prompt, max_tokens=40):
        return "无"


# ---------------- 工具 ----------------

def tone_bytes(rms: float, hz: int, n: int, sr: int) -> bytes:
    t = np.arange(n) / sr
    x = np.sin(2 * np.pi * hz * t) * (rms * math.sqrt(2))
    return np.clip(x, -32768, 32767).astype(np.int16).tobytes()


def noise_bytes(rms: float, n: int, seed: int) -> bytes:
    rng = np.random.default_rng(seed)
    return np.clip(rng.normal(0, rms, n), -32768, 32767).astype(np.int16).tobytes()


def rms_of(x) -> float:
    a = np.asarray(x, dtype=np.float64)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


def make_session(**over):
    cfg = GatewayConfig()
    for k, v in over.items():
        setattr(cfg, k, v)
    vad, asr, ws = EnergyVAD(), FakeASR(), FakeWS()
    s = VoiceSession(ws, cfg, FakeLLM(), asr, FakeTTS(), vad)
    s._decoder = PassthroughDecoder()
    return s, ws, vad, asr


def speaking(s):
    """把会话摆成"正在播报"：闸门生效、响应活跃（与真机一致）。"""
    s.listen_mode = "auto"
    s._collecting = False
    s.state = "speaking"
    s._response_task = asyncio.get_running_loop().create_future()
    return s


def listening(s):
    """把会话摆成"采集中的空闲态"，响应已结束。"""
    s.listen_mode = "auto"
    s._response_task = None
    s._collecting = True
    s.state = "listening"
    return s


# ---------------- 用例 ----------------

async def case_hard_gate():
    print("A. 硬闸门：播放期上行全部丢弃（echo_cancel=off）")
    s, _ws, vad, asr = make_session(echo_cancel="off")
    check("echo_cancel=off 时不建 AEC", s._aec is None)
    speaking(s)
    v0 = vad.calls
    for _ in range(12):
        await s._on_binary(tone_bytes(3000, 300, UP_FRAME, UP_SR))
    check("播放期完全不跑 VAD", vad.calls == v0, f"多跑了 {vad.calls - v0} 次")
    check("播放期不触发 ASR", asr.calls == 0)
    check("播放期不累积音频", len(s._utterance) == 0)
    check("播放期状态不切走", s.state == "speaking")
    check("丢帧计数准确", s._gate_dropped == 12, str(s._gate_dropped))


async def case_cooldown():
    print("B. 尾音冷却窗：tts_stop 后仍然不吃上行")
    s, _ws, vad, asr = make_session(echo_cancel="off")
    listening(s)
    s._open_cooldown()
    check("冷却窗已开启", s._gate_until > time.monotonic())
    v0 = vad.calls
    for _ in range(5):
        await s._on_binary(tone_bytes(3000, 300, UP_FRAME, UP_SR))
    check("冷却期不跑 VAD", vad.calls == v0)
    check("冷却期不累积", len(s._utterance) == 0)
    # 手动把冷却窗拨到过去，模拟等够时间
    s._gate_until = 0.0
    await s._on_binary(tone_bytes(3000, 300, UP_FRAME, UP_SR))
    check("冷却结束后恢复跑 VAD", vad.calls == v0 + 1)
    check("放行时复位过 VAD 句状态", vad.resets > 0, f"resets={vad.resets}")


async def case_resume_recognize():
    print("C. 冷却结束后能正常识别（闸门不会把下一句一起吞掉）")
    s, _ws, vad, asr = make_session(echo_cancel="off", min_silence_ms=600)
    speaking(s)
    for _ in range(3):
        await s._on_binary(tone_bytes(3000, 300, UP_FRAME, UP_SR))
    check("播放期丢弃 3 帧", s._gate_dropped == 3)
    # 播完：冷却窗开启 + 响应结束
    s._open_cooldown()
    listening(s)
    s._gate_until = 0.0
    for _ in range(15):        # 900ms 语音
        await s._on_binary(tone_bytes(3000, 300, UP_FRAME, UP_SR))
    for _ in range(12):        # 720ms 静音 → 触发断句
        await s._on_binary(tone_bytes(5, 300, UP_FRAME, UP_SR))
    check("断句后调用了一次 ASR", asr.calls == 1, str(asr.calls))
    check("ASR 后恢复采集", s._collecting is True)


async def case_gate_disabled():
    print("D. echo_gate=False：退回旧行为（播放期照跑 VAD）")
    s, _ws, vad, asr = make_session(echo_gate=False, echo_cancel="off")
    speaking(s)
    v0 = vad.calls
    await s._on_binary(tone_bytes(3000, 300, UP_FRAME, UP_SR))
    check("旧行为下 VAD 照跑", vad.calls == v0 + 1)
    check("不丢帧", s._gate_dropped == 0)


async def case_cooldown_cleared():
    print("E. 冷却窗的清除条件")
    s, _ws, _vad, _asr = make_session(echo_cancel="off")
    s._open_cooldown()
    check("冷却窗已开", s._gate_until > 0)
    await s._on_text(json.dumps({"type": "abort"}))
    check("abort 清掉冷却窗", s._gate_until == 0.0)
    check("abort 后进入 listening", s.state == "listening")
    # 主动 listen start 同样清除
    s2, _ws2, _vad2, _asr2 = make_session(echo_cancel="off")
    s2._open_cooldown()
    await s2._on_text(json.dumps(
        {"type": "listen", "state": "start", "mode": "manual"}))
    check("手动 listen start 清掉冷却窗", s2._gate_until == 0.0)
    check("手动 listen start 后开始采集", s2._collecting is True)


async def case_aec_barge():
    print("H. AEC 在位：回声不误打断，用户说话能打断")
    s, _ws, vad, asr = make_session()      # echo_cancel=auto
    if s._aec is None:
        check("AEC 可用（集成用例前置）", False, "库不可用，跳过")
        return
    if s._down_decoder is None:
        check("会话持有下行解码器", False, "_down_decoder 为空")
        return
    check("会话持有下行解码器", True)
    enc = DownlinkEncoder()
    speaking(s)
    nf, delay = 40, 4
    # 生产同构：下行帧是 opus 包。设备扬声器播放的是"解码后的 PCM"，
    # 所以回声也必须从解码帧构造，far/echo 才真实相关。
    far_pcm = [noise_bytes(3000, DOWN_FRAME, 100 + i) for i in range(nf)]
    far_opus = [enc.encode_frame(p) for p in far_pcm]
    dec = DownlinkDecoder()
    far_played = [dec.decode(op) for op in far_opus]
    near = []
    for i in range(nf):
        if i < delay:
            near.append(tone_bytes(2, 300, UP_FRAME, UP_SR))
            continue
        a16 = _resample(np.frombuffer(far_played[i - delay], np.int16),
                        DOWN_SR, UP_SR)
        x = a16.astype(np.float32) * 0.5          # 扬声器→麦克风衰减
        near.append(np.clip(x, -32768, 32767).astype(np.int16).tobytes())
    for i in range(nf):
        await s._send(far_opus[i])     # 下行：opus 包进 _send，内部解码
        await s._on_binary(near[i])    # 上行：只有回声
    check("回声期没有被误打断", s.state == "speaking" and s._gate_barges == 0,
          f"barges={s._gate_barges}")
    check("回声期走了 AEC 路径", s._aec.calls >= nf - 2,
          f"calls={s._aec.calls}")
    # 用户真的开口：能量大、与远端不相关
    for _ in range(8):
        await s._send(enc.encode_frame(noise_bytes(3000, DOWN_FRAME, 999)))
        await s._on_binary(tone_bytes(6000, 440, UP_FRAME, UP_SR))
    check("用户说话触发了打断", s._gate_barges == 1, f"barges={s._gate_barges}")
    check("打断后切入 listening", s.state == "listening")
    check("打断后保留了开口语音", len(s._utterance) > 0,
          f"{len(s._utterance)} bytes")


async def case_aec_opus_far():
    """生产同构：下行帧是 opus 包，_send 内解码后才进 AEC。

    回归点：曾经直接 feed_far(opus 字节)，opus 被当 int16 采样解释，
    far 与回声不相关，AEC 不收敛。本用例验证解码往返正确、far 流与
    设备真实播放的信号一致（H 用例已覆盖端到端抑制，这里查解码本身）。
    """
    print("H2. AEC far 参考走 opus 编码路径仍有效")
    s, _ws, _vad, _asr = make_session()      # echo_cancel=auto
    if s._aec is None:
        check("AEC 可用（opus far 前置）", False, "库不可用，跳过")
        return
    if s._down_decoder is None:
        check("会话持有下行解码器", False, "_down_decoder 为空")
        return
    check("会话持有下行解码器", True)
    enc = DownlinkEncoder()
    dec = DownlinkDecoder()
    pcm = noise_bytes(3000, DOWN_FRAME, 7)
    rt = dec.decode(enc.encode_frame(pcm))
    check("opus 解码往返输出整帧 PCM", len(rt) == DOWN_FRAME * 2,
          f"{len(rt)} bytes")
    check("opus 解码往返非静音", rms_of(np.frombuffer(rt, np.int16)) > 100,
          f"rms={rms_of(np.frombuffer(rt, np.int16)):.0f}")
    # 会话内的解码器与这里的是同规格实例：同一 opus 字节序列应产出
    # 完全一致的 PCM（opus 解码按规范是确定性的）——这正是 far 参考
    # 能对上设备播放内容的前提。
    speaking(s)
    nf, delay = 30, 4
    far_opus = [enc.encode_frame(noise_bytes(3000, DOWN_FRAME, 300 + i))
                for i in range(nf)]
    far_played = [dec.decode(op) for op in far_opus]
    near = []
    for i in range(nf):
        if i < delay:
            near.append(tone_bytes(2, 300, UP_FRAME, UP_SR))
            continue
        a16 = _resample(np.frombuffer(far_played[i - delay], np.int16),
                        DOWN_SR, UP_SR)
        x = a16.astype(np.float32) * 0.5
        near.append(np.clip(x, -32768, 32767).astype(np.int16).tobytes())
    for i in range(nf):
        await s._send(far_opus[i])
        await s._on_binary(near[i])
    check("opus far：回声期没有被误打断",
          s.state == "speaking" and s._gate_barges == 0,
          f"barges={s._gate_barges}")
    check("opus far：回声期走了 AEC 路径", s._aec.calls >= nf - 2,
          f"calls={s._aec.calls}")


def case_aec_unit():
    print("F. AEC 单元：合成回声被压制、用户语音保留")
    aec = EchoCanceller(sample_rate=UP_SR, far_rate=DOWN_SR, delay_ms=180)
    if not aec.available:
        check("AEC 库可用", False, aec.error)
        return
    check("AEC 库可用", True)
    n_blk, delay = 60, 5
    N = UP_FRAME * n_blk
    rng = np.random.default_rng(1)
    far16 = rng.normal(0, 3000, N).astype(np.int16)
    near = np.zeros(N, dtype=np.int32)
    near[delay * UP_FRAME:] += (
        far16[:N - delay * UP_FRAME] * 0.6).astype(np.int32)
    usr_start = UP_FRAME * (n_blk - 15)
    t = np.arange(N - usr_start) / UP_SR
    near[usr_start:] += (np.sin(2 * np.pi * 440 * t) * 6000).astype(np.int32)
    near = np.clip(near, -32768, 32767).astype(np.int16)
    out = np.zeros(N, dtype=np.int16)
    for i in range(n_blk):
        blk = far16[i * UP_FRAME:(i + 1) * UP_FRAME]
        aec.feed_far(_resample(blk, UP_SR, DOWN_SR).tobytes())
        got = aec.process(near[i * UP_FRAME:(i + 1) * UP_FRAME].tobytes())
        out[i * UP_FRAME:(i + 1) * UP_FRAME] = np.frombuffer(got, np.int16)
    echo_zone = slice(delay * UP_FRAME, 40 * UP_FRAME)
    att = 20 * math.log10(max(rms_of(out[echo_zone]), 1e-6)
                          / max(rms_of(near[echo_zone]), 1e-6))
    check("回声被显著压制（< -20 dB）", att < -20, f"{att:.1f} dB")
    usr = slice(usr_start, usr_start + 15 * UP_FRAME)
    keep = rms_of(out[usr]) / max(rms_of(near[usr]), 1e-6)
    check("用户语音大部分保留（> 50%）", keep > 0.5, f"{keep * 100:.0f}%")
    check("输出长度与输入一致",
          len(aec.process(b"\x00" * (UP_FRAME * 2))) == UP_FRAME * 2)


def case_aec_bypass():
    print("G. AEC 不可用时安全旁路")
    aec = EchoCanceller(sample_rate=UP_SR, far_rate=DOWN_SR)
    aec.available = False
    aec._ap = None
    pcm = tone_bytes(3000, 300, UP_FRAME, UP_SR)
    check("process 原样返回输入", aec.process(pcm) == pcm)
    check("active 为假", aec.active is False)
    aec.feed_far(b"\x00" * 100)
    check("feed_far 不抛异常", True)
    # 没有 far 在途时也旁路（避免无谓失真）
    aec2 = EchoCanceller(sample_rate=UP_SR, far_rate=DOWN_SR)
    if aec2.available:
        check("无 far 在途 → active 为假", aec2.active is False)
        check("无 far 在途 → 原样返回", aec2.process(pcm) == pcm)


async def run_async():
    for fn in (case_hard_gate, case_cooldown, case_resume_recognize,
               case_gate_disabled, case_cooldown_cleared, case_aec_barge,
               case_aec_opus_far):
        await fn()


def main() -> int:
    asyncio.run(run_async())
    case_aec_unit()
    case_aec_bypass()
    print("")
    if _fail:
        print(f"ECHO_FAIL {len(_fail)}/{len(_pass) + len(_fail)}")
        for f in _fail:
            print("   - " + f)
        return 1
    print(f"ECHO_PASS {len(_pass)}/{len(_pass)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
