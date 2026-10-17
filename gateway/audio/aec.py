# -*- coding: utf-8 -*-
"""网关侧回声消除（WebRTC APM / AEC3）。

## 为什么要在网关侧做

设备（`bread-compact-wifi-lcd`）是**单麦、无回采**的硬件：`NoAudioCodecSimplex`
不设置 `input_reference_`，启动日志实测
`AFE: Input PCM Config: total 1 channels(1 microphone, 0 playback)`
—— 0 个播放参考通道，设备侧 AFE 永远开不了硬件 AEC（`aec_init = false`）。
于是设备在播放自己的回复时，麦克风把扬声器的声音一并上行，网关就会把它
当成"用户开口"：轻则误打断，重则自问自答。

## 做法

与 py-xiaozhi 同源：用 WebRTC APM 的 AEC3，把**我们自己下发的 TTS 音频**当
远端参考（far），把它从上行（near）里减掉。AEC 后的信号里只剩"用户真说
的话"，可以放心地拿去跑 VAD —— 语音打断因此可以恢复。

## 与 py-xiaozhi 的差异（本模块存在的理由）

py-xiaozhi 的麦克风/扬声器与应用**同机**，回路延迟固定，所以它用
`set_stream_delay(40ms + N × 帧长)` 一个常量就能对齐。

本项目：麦克风在 ESP32 上、TTS 从 PC 经 WiFi 下发，回路延迟 =
设备播放缓冲 + 空气传播/混响 + 采集 + 网络，量级 200~500ms **且抖动**，
设备端播放缓冲深度我们控制不了。

所幸 AEC3 **自带延迟估计**：实测把 far 按发送顺序连续喂、near 按到达顺序
配对（两者相差 300ms 也不错位对齐），合成回声仍被压掉约 70dB
（`.tmp/probe_aec.py`，回归见 `tests/test_echo_gate.py`）。
所以这里**不做精确对齐**，只保证 far 流"按序、连续"，`stream_delay_ms`
仅作为收敛初值。

## 失配时的行为（重要）

`active` 为假（库缺失 / 初始化失败 / 当前没有 far 在途）时，`process()`
原样返回输入 —— 调用方必须自己有兜底（见 `session.py` 的硬闸门），
**不能**把"AEC 不可用"当成"音频是干净的"。
"""
from __future__ import annotations

import numpy as np

#: far 缓冲上限（秒）。超过说明 near 断流太久（设备在说话期间不上行），
#: 再留着的 far 已经没有对应关系，只会污染延迟估计。
_FAR_CAP_S = 3.0


def _resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    """int16 单声道线性重采样（语音带宽下够用，py-xiaozhi 同做法）。"""
    if src == dst or x.size == 0:
        return x
    n = int(round(x.size * dst / src))
    if n <= 0:
        return np.zeros(0, dtype=np.int16)
    idx = np.linspace(0.0, x.size - 1, n)
    y = np.interp(idx, np.arange(x.size, dtype=np.float64),
                  x.astype(np.float32))
    return np.clip(y, -32768, 32767).astype(np.int16)


class EchoCanceller:
    """把一个会话的下行 TTS 当参考，抵消上行里的自播放回声。

    每个会话一个实例：不同设备/摆位的回声路径不同，共用实例会互相污染
    自适应滤波器。
    """

    def __init__(self, sample_rate: int = 16000, far_rate: int = 24000,
                 delay_ms: int = 180, noise_suppression: bool = False):
        self.sample_rate = int(sample_rate)
        self.far_rate = int(far_rate)
        self.stream_delay_ms = int(delay_ms)
        self.available = False          # 库能用且初始化成功
        self.error = ""
        self._ap = None
        self._far = bytearray()         # 已下发、尚未与 near 配对的 far（目标采样率）
        self._far_cap = int(self.sample_rate * 2 * _FAR_CAP_S)
        # 诊断计数（gate_log 用）
        self.calls = 0
        self.far_bytes = 0
        try:
            import pywebrtc_audio  # noqa: PLC0415
            self._ap = pywebrtc_audio.AudioProcessor(
                sample_rate=self.sample_rate,
                echo_cancellation=True,
                noise_suppression=bool(noise_suppression),
                auto_gain_control=False,   # 上行 AGC 会放大残留回声，不开
                stream_delay_ms=self.stream_delay_ms,
            )
            self.available = True
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"

    # ---------- 对外 ----------

    @property
    def active(self) -> bool:
        """当前是否真的在抵消：库可用 **且** 有 far 在途。

        没有 far 的时候走一遍 AEC 只会白白引入失真（AEC3 会把"没有被参考
        解释的能量"当近端语音，问题不大，但不必要），所以直接旁路。
        """
        return self.available and len(self._far) > 0

    def feed_far(self, pcm: bytes) -> None:
        """登记一段"刚下发给设备"的音频（远端参考）。

        调用点必须只有一处：`VoiceSession._send()` 里真正发出去的音频帧。
        采样率按 `far_rate` 给（下行 24k），这里会重采样到 `sample_rate`。
        """
        if not self.available or not pcm:
            return
        a = np.frombuffer(pcm, dtype=np.int16)
        if self.far_rate != self.sample_rate:
            a = _resample(a, self.far_rate, self.sample_rate)
        if a.size == 0:
            return
        self._far.extend(a.tobytes())
        self.far_bytes += a.size * 2
        if len(self._far) > self._far_cap:      # 只留最近的一段
            del self._far[:len(self._far) - self._far_cap]

    def process(self, pcm: bytes) -> bytes:
        """near（上行 s16le）→ 去掉自播放回声后的 s16le。

        不可用时原样返回。长度不变。
        """
        if not self.available or not pcm:
            return pcm
        n = len(pcm)
        far = self._take_far(n)
        try:
            near = np.frombuffer(pcm, dtype=np.int16)
            out = self._ap.process(near, far)
            self.calls += 1
            res = np.asarray(out, dtype=np.int16)
            return res.tobytes() if res.size == near.size else pcm
        except Exception:  # noqa: BLE001
            # 单帧失败不该毁掉整场会话：旁路这一帧，下一帧继续
            return pcm

    def reset(self) -> None:
        """清空自适应滤波器与 far 缓冲（会话重开 / 长时间静默后）。"""
        self._far.clear()
        if self._ap is not None:
            try:
                self._ap.reset()
            except Exception:  # noqa: BLE001
                pass

    def stats(self) -> dict:
        return {"available": self.available, "active": self.active,
                "calls": self.calls, "far_kb": round(self.far_bytes / 1024, 1),
                "delay_ms": self.stream_delay_ms, "error": self.error}

    # ---------- 内部 ----------

    def _take_far(self, n: int) -> np.ndarray:
        """取与 near 等长的 far 段并消费掉；不足则补零（等价"远端静音"）。

        far 是**按发送顺序**消费的：TCP/WS 下 near 与 far 的到达速率一致，
        队列长度因此稳定在"回路延迟"对应的帧数上，等于把一个固定长度的
        延迟线交给了 AEC3 后面的延迟估计器。
        """
        if n <= 0:
            return np.zeros(0, dtype=np.int16)
        buf = self._far
        if len(buf) >= n:
            chunk = bytes(buf[:n])
            del buf[:n]
            return np.frombuffer(chunk, dtype=np.int16)
        chunk = bytes(buf)
        buf.clear()
        head = (np.frombuffer(chunk, dtype=np.int16) if chunk
                else np.zeros(0, dtype=np.int16))
        return np.concatenate([head, np.zeros(n - head.size, dtype=np.int16)])
