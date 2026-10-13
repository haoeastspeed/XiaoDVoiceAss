# -*- coding: utf-8 -*-
"""
诊断：SenseVoice 识别
A) 原始 zh.wav 直接识别
B) ffmpeg 转 16k PCM 后识别
C) PCM -> opus 编码 -> opus 解码 -> PCM（往返）后识别
"""
import os
import subprocess
import sys
import tempfile
import wave

import numpy as np
import sherpa_onnx

ROOT = "D:\\projects\\xiaod-voice"
BIN = os.path.join(ROOT, "reference", "bin")
MODEL_DIR = "D:/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
SRC = os.path.join(MODEL_DIR, "test_wavs", "zh.wav")

os.environ["PATH"] = BIN + os.pathsep + os.environ.get("PATH", "")
import opuslib_next

recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model=os.path.join(MODEL_DIR, "model.int8.onnx"),
    tokens=os.path.join(MODEL_DIR, "tokens.txt"),
    use_itn=True,
    num_threads=2,
    language="zh",
)


def recognize_pcm(pcm_bytes, sr=16000, label=""):
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    stream = recognizer.create_stream()
    stream.accept_waveform(sr, samples)
    recognizer.decode_stream(stream)
    print(f"[{label}] {stream.result.text}")
    return stream.result.text


# A) 原始 wav
with wave.open(SRC) as w:
    sr = w.getframerate()
    ch = w.getnchannels()
    raw = w.readframes(w.getnframes())
    print(f"原始 wav: sr={sr}, ch={ch}, frames={w.getnframes()}, {len(raw)/2/sr:.2f}s")
recognize_pcm(raw, sr, "A 原始wav")

# B) ffmpeg 转 PCM
tmp = tempfile.mkdtemp(prefix="diag_")
pcm = os.path.join(tmp, "x.pcm")
subprocess.run(
    [os.path.join(BIN, "ffmpeg.exe"), "-y", "-i", SRC, "-ar", "16000", "-ac", "1",
     "-f", "s16le", "-acodec", "pcm_s16le", pcm],
    check=True, capture_output=True,
)
with open(pcm, "rb") as f:
    b_pcm = f.read()
recognize_pcm(b_pcm, 16000, "B ffmpeg PCM")

# C) opus 往返
FRAME = 960
enc = opuslib_next.Encoder(16000, 1, opuslib_next.APPLICATION_VOIP)
dec = opuslib_next.Decoder(16000, 1)
fb = FRAME * 2
out_pcm = b""
for i in range(0, len(b_pcm) - fb + 1, fb):
    chunk = b_pcm[i:i + fb]
    packet = enc.encode(chunk, FRAME)
    out_pcm += dec.decode(packet, FRAME)
recognize_pcm(out_pcm, 16000, "C opus往返")
print(f"往返 PCM 长度: 原始 {len(b_pcm)} -> {len(out_pcm)}")
