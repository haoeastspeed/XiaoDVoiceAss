# -*- coding: utf-8 -*-
"""端到端复现/验证：语音对讲"识别到自己播放的声音"（自激回声）。

做法：假麦克风的输入 = 真实用户说一句话 + 之后**把小D自己播出的声音回灌**
（模拟外放被麦克风拾取），跑真实网关（ASR/LLM/TTS 全链路）。

期望：
  A. 客户端回声门开 → 回声帧根本不上行：只有 1 次 STT、1 次回复；
  B. 客户端回声门关（旧行为）→ 网关侧自回声兜底把识别结果丢掉，
     不会出现"自问自答"（回复次数仍为 1，日志出现 [echo-guard]）。

运行（需先启动网关，端口用 .tmp/gw_echo_test.json 指定 8010）：
  reference\\...\\python.exe main.py --config .tmp/gw_echo_test.json
  C:\\Python314\\python.exe tests\\test_echo_loop.py
"""
import asyncio
import os
import sys
import time

ROOT = r"D:\projects\xiaod-voice"
BIN = os.path.join(ROOT, "reference", "bin")
SRC_WAV = ("D:/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09/"
           "test_wavs/zh.wav")
GW_LOG = os.path.join(ROOT, ".tmp", "gw_8010.log")
WS_URL = "ws://127.0.0.1:8010/xiaozhi/v1/"
sys.path.insert(0, r"D:\projects\localdesk-agent\src")
os.environ["PATH"] = BIN + os.pathsep + os.environ.get("PATH", "")

import numpy as np  # noqa: E402
import scipy.signal  # noqa: E402

from localdesk.realtime_voice import (  # noqa: E402
    RealtimeVoiceClient, UP_SR, UP_FRAME, DOWN_SR, DOWN_FRAME)

PRE_SILENCE = 0.6
POST_SILENCE = 2.0      # 说完后留一段静音（否则会立刻播回复）
ATTEN = 0.6             # 扬声器 → 麦克风的回声衰减（线性）


def load_user_frames() -> list[bytes]:
    import subprocess
    import tempfile
    tmp = tempfile.mkdtemp(prefix="echoe2e_")
    pcm_path = os.path.join(tmp, "u.pcm")
    subprocess.run([os.path.join(BIN, "ffmpeg.exe"), "-y", "-i", SRC_WAV,
                    "-ar", str(UP_SR), "-ac", "1", "-f", "s16le", pcm_path],
                   check=True, capture_output=True)
    with open(pcm_path, "rb") as f:
        speech = f.read()
    pcm = (b"\x00" * int(UP_SR * 2 * PRE_SILENCE) + speech
           + b"\x00" * int(UP_SR * 2 * POST_SILENCE))
    return [pcm[i:i + UP_FRAME * 2]
            for i in range(0, len(pcm) - UP_FRAME * 2 + 1, UP_FRAME * 2)]


def silence() -> bytes:
    return b"\x00" * (UP_FRAME * 2)


class LoopbackMic:
    """假麦克风：先播用户那句话，之后把下行（小D的声音）当麦克风输入回灌。

    真实麦克风听到的永远是"此刻正在外放"的那一帧，所以这里给假麦克风配了一个
    按 60ms/帧推进的**虚拟播放头**：下行解码帧先进待播队列，再由播放头逐帧
    交给麦克风（下行比实时快时不会一次性全灌进去）。
    """

    def __init__(self, client: RealtimeVoiceClient, user_frames: list[bytes]):
        self.client = client
        self.user = user_frames
        self.ui = 0
        self._buf24 = bytearray()        # 未转码的下行 24k PCM
        self._in_idx = 0
        self._pending: list[bytes] = []  # 已转 16k、排队等播出
        self._head = 0                   # 播放头（已播到第几帧）
        self._play_t0 = 0.0
        self._echo_q: list[bytes] = []
        self.echo_frames = 0

    def __call__(self) -> bytes:
        if self.ui < len(self.user):
            f = self.user[self.ui]
            self.ui += 1
            return f
        self._pump()
        if self._echo_q:
            self.echo_frames += 1
            return self._echo_q.pop(0)
        return silence()

    def _pump(self):
        """收集下行 → 转 16k 排队 → 按真实节奏把"此刻在播"的帧交给麦克风。"""
        buf = self.client.decoded_pcm
        while self._in_idx < len(buf):
            self._buf24 += buf[self._in_idx]
            self._in_idx += 1
        while len(self._buf24) >= DOWN_FRAME * 2:
            block = bytes(self._buf24[:DOWN_FRAME * 2])
            del self._buf24[:DOWN_FRAME * 2]
            x = np.frombuffer(block, dtype=np.int16).astype(np.float32)
            y = scipy.signal.resample_poly(x, 2, 3)   # 24k → 16k
            y = np.clip(y * ATTEN, -32768, 32767).astype(np.int16)
            self._pending.append(y.tobytes())
        if not self._pending:
            return
        now = time.monotonic()
        if self._play_t0 == 0.0:
            self._play_t0 = now
        played = int((now - self._play_t0) / 0.06) + 1
        idx = min(played, len(self._pending)) - 1
        if idx >= self._head:            # 播放头推进到新的一帧
            self._echo_q = [self._pending[idx]]
            self._head = idx + 1
        # 只保留最新的 1~2 帧，模拟"听到的就是当前这一小段"
        if len(self._echo_q) > 2:
            del self._echo_q[:-2]


async def run_case(name: str, echo_guard: bool, allow_barge: bool, seconds: float):
    from localdesk.realtime_voice import RealtimeVoiceClient as C
    user = load_user_frames()
    events: list[dict] = []
    holder = {}

    class Holder:
        pass

    client = C(WS_URL, on_event=lambda e: events.append(e), audio_feed=None,
               play_output=False, virtual_playback=True, debug_record=False,
               echo_guard=echo_guard, allow_barge_in=allow_barge)
    mic = LoopbackMic(client, user)
    client.audio_feed = mic
    holder["c"] = client

    print(f"\n=== {name} ===", flush=True)
    client.start()
    t0 = time.monotonic()
    await asyncio.sleep(seconds)
    client.stop()
    await asyncio.sleep(0.4)

    stt = [e["text"] for e in events if e.get("type") == "user"]
    starts = [e for e in events if e.get("type") == "assistant_start"]
    stops = [e for e in events if e.get("type") == "assistant_stop"]
    said = "".join(e.get("text", "") for e in events
                   if e.get("type") == "assistant_delta")
    print(f"   STT 次数={len(stt)}: {stt}")
    print(f"   回复次数 start={len(starts)} stop={len(stops)}")
    print(f"   小D 说: {said[:80]}")
    print(f"   回灌回声帧={mic.echo_frames}, 客户端丢弃={client.echo_dropped}, "
          f"本地插话={client.barge_count}")
    return {"stt": stt, "starts": len(starts), "stops": len(stops),
            "said": said, "echo": mic.echo_frames,
            "dropped": client.echo_dropped, "barge": client.barge_count}


def log_tail() -> str:
    try:
        with open(GW_LOG, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


async def main():
    t0 = time.monotonic()
    a = await run_case("A. 回声门开（默认半双工，本次修复）", True, False, 20.0)
    c = await run_case("C. 回声门开 + 允许插话（严格阈值）", True, True, 20.0)
    b = await run_case("B. 回声门关（旧行为）+ 网关兜底", False, False, 20.0)
    guard_lines_a = log_tail().count("[echo-guard]")
    guard_lines_b = log_tail().count("[echo-guard]")

    # A：回声根本不上行 → 只有 1 次识别、1 次回复，无自激
    ok_a = (len(a["stt"]) == 1 and a["starts"] == 1
            and a["echo"] > 10 and a["dropped"] > 10)
    # C：允许插话时最多被截断一次，不能自问自答（回复次数不膨胀）
    ok_c = (c["starts"] <= 2 and c["echo"] > 10 and c["dropped"] > 10)
    # B：旧客户端 + 网关兜底 → 不出现自问自答，且日志里有兜底拦截记录
    ok_b = (b["starts"] == 1 and b["echo"] > 10 and guard_lines_b > 0)
    print("\n=== 结果 ===")
    print(f"A 回声门开（默认）: STT={len(a['stt'])} 回复={a['starts']} → "
          f"{'通过 ✅' if ok_a else '不通过 ❌'}")
    print(f"C 回声门开+插话: STT={len(c['stt'])} 回复={c['starts']} "
          f"本地插话={c['barge']} → {'通过 ✅' if ok_c else '不通过 ❌'}")
    print(f"B 回声门关: 网关兜底拦下自播放回声 {guard_lines_b} 次，回复={b['starts']} → "
          f"{'通过 ✅' if ok_b else '不通过 ❌'}")
    print(f"总耗时 {time.monotonic() - t0:.1f}s")
    return 0 if (ok_a and ok_c and ok_b) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
