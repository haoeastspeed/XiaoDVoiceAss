# -*- coding: utf-8 -*-
"""音色热切换 + 插话判据 实测（无需网关、无需大模型）。

跑法：
    C:/Python314/python.exe tests/test_voice_switch.py

覆盖：
  1. 在线音色热切换是否实时生效（同一个 TTSRouter 连续 set_voice）
  2. 降级映射：在线音色在本地引擎里落成什么
  3. 在线合成稳定性（复现 No audio was received）
  4. 离线本地音色清单
  5. 插话判据：需要多大的麦克风电平才可能触发（新旧判据对比）
  6. 打断式取消对在线引擎的影响
  7. 在线首包失败的重试与降级状态（degraded / local_label）
"""
from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["PATH"] = (os.path.join(ROOT, "reference", "bin") + os.pathsep
                      + os.environ.get("PATH", ""))

import numpy as np  # noqa: E402
import opuslib_next  # noqa: E402

from gateway.providers.tts_router import TTSRouter, all_voices  # noqa: E402

TEXT = "你好，我是小D，很高兴认识你。"
EMOJI_TEXT = "还是想跟我多喊两声呀？😄"
LONG_TEXT = ("每次咱们只攻克一个核心要点。我讲完一段，你就告诉我听懂了没有，"
             "咱们再继续往下走，绝不让你一个人憋着消化。")

_dec = opuslib_next.Decoder(24000, 1)


def feats(packets: list[bytes]) -> dict:
    """一串 opus 包（真实下发内容）→ 解回 PCM 后取音色代理特征。"""
    pcm = b""
    for pkt in packets:
        try:
            pcm += _dec.decode(pkt, 1440)
        except Exception:  # noqa: BLE001
            pass
    if not pcm:
        return {"bytes": 0}
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if x.size < 512:
        return {"pkts": len(packets), "rms": 0.0}
    win = x[: len(x) // 512 * 512].reshape(-1, 512)
    spec = np.abs(np.fft.rfft(win * np.hanning(512), axis=1))
    freqs = np.fft.rfftfreq(512, 1 / 24000.0)
    centroids = (spec * freqs).sum(axis=1) / np.maximum(spec.sum(axis=1), 1e-9)
    ac = np.fft.irfft(spec ** 2, n=512, axis=1)
    f0s = []
    for row in ac:
        lo, hi = int(24000 / 400), int(24000 / 70)     # 70~400Hz
        if hi >= row.size:
            continue
        seg = row[lo:hi]
        if seg.size and seg.max() > 0:
            f0s.append(24000.0 / (lo + int(np.argmax(seg))))
    return {
        "pkts": len(packets),
        "dur_s": round(x.size / 24000.0, 2),
        "rms": round(float(np.sqrt(np.mean(x ** 2))), 4),
        "centroid_hz": int(np.median(centroids)),
        "f0_hz": int(np.median(f0s)) if f0s else 0,
    }


async def synth(router: TTSRouter, text: str) -> list[bytes]:
    return await router.synthesize(text, {})


def tone(rms: float, n: int = 960, sr: int = 24000, hz: float = 220.0):
    """生成一段 RMS≈rms 的 int16 单声道 PCM（模拟人声/回声）。"""
    amp = rms * 2 ** 0.5
    t = np.linspace(0, n / sr, n, endpoint=False)
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.int16)


def mic_frame(echo_rms: float, speech_rms: float = 0.0) -> bytes:
    """外放回声 + 用户人声叠加成的一帧麦克风音频（int16）。"""
    f = tone(echo_rms, hz=180).astype(np.float32)
    if speech_rms > 0:
        f = f + tone(speech_rms, hz=320).astype(np.float32)
    return np.clip(f, -32768, 32767).astype(np.int16).tobytes()


async def main() -> None:
    print(f"可选音色共 {len(all_voices())} 个\n")

    # ---- 1) 同一个 router 上连续切音色，看是否实时改变 ----
    print("=" * 72)
    print("1) 在线音色热切换（同一个 router 实例，连续切）")
    print("=" * 72)
    r = TTSRouter("zh-CN-XiaoxiaoNeural", 24000, 1440, engine="auto")
    for vid in ["zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural",
                "zh-TW-HsiaoChenNeural", "en-US-JennyNeural"]:
        r.set_voice(vid)                     # ← 会话里 set_voice 控制消息走这里
        pcm = await synth(r, TEXT)
        print(f"{vid:32s} engine={r.last_engine:6s} "
              f"online={r._online.voice:28s} {feats(pcm)}")

    # ---- 2) 在线失败 → 本地降级的音色映射 ----
    print()
    print("=" * 72)
    print("2) 降级映射：在线音色在本地引擎里落成什么")
    print("=" * 72)
    for vid in ["zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural",
                "zh-TW-HsiaoChenNeural", "en-US-JennyNeural",
                "local:HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Speech\\Voices"
                "\\Tokens\\TTS_MS_ZH-CN_HUIHUI_11.0"]:
        r.set_voice(vid)
        print(f"{vid:32s} → online={r._online.voice:28s} "
              f"local={os.path.basename(r._local.voice_id) or '（空＝系统默认）'}")

    print()
    print("强制只用本地（engine=local）：同一条文本，各在线音色听起来是否一样")
    r.set_engine("local")
    for vid in ["zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural",
                "zh-CN-YunyangNeural"]:
        r.set_voice(vid)
        print(f"{vid:32s} engine={r.last_engine:6s} local={r.local_label:14s} "
              f"{feats(await synth(r, TEXT))}")
    r.set_engine("auto")

    # ---- 3) 复现"在线合成失败" ----
    print()
    print("=" * 72)
    print("3) 在线合成稳定性（复现 No audio was received）")
    print("=" * 72)
    r.set_voice("zh-CN-XiaoxiaoNeural")
    for name, t in [("普通句", TEXT), ("含 emoji", EMOJI_TEXT),
                    ("长句", LONG_TEXT)]:
        for k in range(2):
            r._down_until = 0.0          # 清掉冷却，强制走在线
            pcm = await synth(r, t)
            print(f"{name} 第{k + 1}次: engine={r.last_engine:6s} "
                  f"failures={r.online_failures} {feats(pcm)}")

    # ---- 4) 本地（SAPI）音色清单 ----
    print()
    print("=" * 72)
    print("4) 离线本地音色（Windows SAPI）")
    print("=" * 72)
    for v in [x for x in all_voices() if x["engine"] == "local"]:
        print(f"  {v['name']}  id={v['id'][:70]}…")

    # ---- 5) 插话判据：新旧对比 ----
    print()
    print("=" * 72)
    print("5) 插话判据：需要多大的麦克风电平才可能触发")
    print("=" * 72)
    from app.voice_client import RealtimeVoiceClient

    def new_client():
        c = RealtimeVoiceClient("ws://127.0.0.1:8000/xiaozhi/v1/",
                                play_output=True, echo_guard=True,
                                allow_barge_in=True,
                                barge_factor=1.2, barge_frames=6)
        c._floor = 30.0
        c._resp_active = True            # 协议层：正在播报
        c._note_down_level(tone(int(0.09 * 32768), n=1440).tobytes())
        return c

    def feed(c, echo_rms, speech_rms=0.0):
        c._barge_streak = 0
        c._barge_onset = []
        c._barge_mute_until = 0.0
        c._barge_armed = True
        for _ in range(8):                   # 8 帧 ≥ barge_frames(6)
            if c._gate_frame(mic_frame(echo_rms, speech_rms)):
                return True
        return False

    for scene, att in (("外放（回声明显）", 0.20), ("耳机 / 近讲", 0.02)):
        c = new_client()
        play = c._down_level()
        for _ in range(20):                  # 让耦合估计从纯回声里学出来
            c._gate_frame(mic_frame(att * play))
        for _ in range(5):                   # 用户平时说话的电平参考
            c._track_mic_ref(400.0)
        print(f"\n[{scene}] 真实耦合≈{att} → 学到 att={c._att:.3f}，"
              f"播放电平={play:.0f}，用户参考 ref={c._mic_ref:.0f}")
        for sp in (200, 400, 800, 1500):
            fired = feed(c, att * play, sp)
            g = c.last_gate
            raw = float(np.sqrt((att * play) ** 2 + sp ** 2))
            print(f"   说话原始 RMS={sp:5d} → 新判据 mic={g['mic']:6.0f} "
                  f"thr={g['thr']:6.0f} {'可打断 ✅' if fired else '打不断 ❌'}"
                  f" ｜ 旧判据 thr=4129 / mic={raw:4.0f} → 打不断")

    # ---- 误触发护栏：纯回声 / 回声突然变响，都不该被判成插话 ----
    print("\n[误触发护栏] 只放音、没人说话时绝不能触发（否则会把回声当人话）")
    c3 = new_client()
    p3 = c3._down_level()
    for _ in range(20):
        c3._gate_frame(mic_frame(0.2 * p3))
    print(f"   纯回声连续 8 帧      : "
          f"{'误触发 ❌' if feed(c3, 0.2 * p3) else '未触发 ✅'}")
    for _ in range(10):
        c3._gate_frame(mic_frame(0.2 * p3))
    fired = feed(c3, 0.2 * p3 * 1.3)
    print(f"   回声突然响 30%（新句子更响）: "
          f"{'误触发 ❌' if fired else '未触发 ✅'}"
          f"（估计自动跟上：att={c3._att:.3f}）")

    # ---- 6) 频繁打断（中途取消在线合成）会不会把在线引擎弄坏 ----
    print()
    print("=" * 72)
    print("6) 打断式取消对在线引擎的影响（模拟连续 abort）")
    print("=" * 72)
    r2 = TTSRouter("zh-CN-XiaoxiaoNeural", 24000, 1440, engine="auto")
    for i in range(3):
        gen = r2._online.stream("这是一句会被中途打断的测试文本，用来模拟用户插话。")
        got = 0
        async for _ in gen:
            got += 1
            if got >= 3:
                break
        await gen.aclose()                  # 网关 cancel 时生成器就是这样被丢弃的
        print(f"  第{i + 1}次中途取消（已收 {got} 包）")
    print(f"  取消后再合成: engine={r2.last_engine} failures={r2.online_failures} "
          f"degraded={r2.degraded} {feats(await synth(r2, TEXT))}")

    # ---- 7) 首包失败重试 / 降级状态 ----
    print()
    print("=" * 72)
    print("7) 在线首包失败：先重试再降级，且状态可上报")
    print("=" * 72)
    rr = TTSRouter("zh-CN-XiaoxiaoNeural", 24000, 1440, engine="auto")
    real = rr._online.stream
    state = {"n": 0}

    async def flaky(text, style):
        state["n"] += 1
        if state["n"] == 1:                # 第一次抖一下，第二次正常
            raise RuntimeError("No audio was received. Please verify that your "
                               "parameters are correct.")
        async for p in real(text, style):
            yield p

    rr._online.stream = flaky
    pk = await synth(rr, TEXT)
    print(f"  抖动 1 次: 尝试={state['n']} engine={rr.last_engine} "
          f"degraded={rr.degraded} {feats(pk)}   ← 不该降级")

    async def dead(text, style):
        state["n"] += 1
        raise RuntimeError("No audio was received. Please verify that your "
                           "parameters are correct.")
        yield b""

    rr._online.stream = dead
    state["n"] = 0
    pk = await synth(rr, TEXT)
    print(f"  一直失败: 尝试={state['n']} engine={rr.last_engine} "
          f"degraded={rr.degraded} label={rr.local_label} {feats(pk)}")

    rr._online.stream = real
    rr.set_voice("en-US-JennyNeural")
    print(f"  英文音色降级目标: {os.path.basename(rr._local.voice_id)}")

    # ---- 8) 降级/恢复提示的端到端管线（网关 session → 协议 → 客户端） ----
    print()
    print("=" * 72)
    print("8) 降级/恢复提示能否送到界面（不进通话链路）")
    print("=" * 72)
    import json

    from gateway.config import GatewayConfig
    from gateway.session import VoiceSession

    class FakeWs:
        def __init__(self):
            self.sent: list[str] = []

        async def send(self, data):
            self.sent.append(data)

    class FakeTts:
        def __init__(self):
            self.degraded = False
            self.local_label = "Huihui ·简体"

    ws = FakeWs()
    sess = VoiceSession(ws, GatewayConfig(), object(), object(), FakeTts(),
                        type("V", (), {"new_state": staticmethod(lambda: 0),
                                       "reset_sentence": staticmethod(
                                           lambda s: None)})())
    fake = FakeTts()
    sess.tts = fake
    await sess._notify_tts_state()               # 状态没变 → 不该发
    fake.degraded = True
    await sess._notify_tts_state()               # 降级 → 发一条
    await sess._notify_tts_state()               # 重复 → 不重复发
    fake.degraded = False
    await sess._notify_tts_state()               # 恢复 → 发一条
    for s in ws.sent:
        print(f"  网关 → {s}")

    from app.voice_client import RealtimeVoiceClient as C2

    got: list[dict] = []
    c9 = C2("ws://x/", on_event=lambda e: got.append(e))
    await c9._handle_text(json.dumps(
        {"type": "notice", "text": "在线音色暂时不可用"}), None)
    print(f"  客户端解析 → {got}")


if __name__ == "__main__":
    asyncio.run(main())
