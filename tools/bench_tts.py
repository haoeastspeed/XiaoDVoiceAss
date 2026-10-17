# -*- coding: utf-8 -*-
"""本机 TTS / 本地模型承载能力实测（选型与调参依据）。

跑法：C:////Python314////python.exe tools/bench_tts.py（需 PYTHONPATH 指向仓库根）

测两件事：
  1. 本地神经合成（Kokoro，sherpa-onnx）的实际 RTF —— 合成 1 秒语音要花多少
     秒。RTF < 1 才谈得上"边说边生成"，远小于 1 才算"跑得动"。
  2. 本机 CPU / 内存的余量，用来判断还能不能同时扛一个 9B 大模型。
"""
from __future__ import annotations

import asyncio
import os
import time

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

SAMPLES = [
    "你好，我是小D，一个运行在你电脑上的本地语音助手。",
    "今天天气不错，要不要一起听首歌？",
    "The quick brown fox jumps over the lazy dog, and then keeps running.",
    "这是一段用来测量语音合成速度的中文测试文本，长度大概有四十多个字，"
    "相当于日常一句话的量级，可以比较真实地反映实际使用时的合成耗时。",
]


def probe_kokoro():
    from gateway.providers import tts_kokoro as K
    print("== Kokoro 本地神经合成 ==")
    print("  available:", K.available())
    if not K.available():
        print("  模型不可用（目录没找到或 sherpa_onnx 缺失）")
        return None
    t0 = time.perf_counter()
    if not K.find():
        print("  find() 失败")
        return None
    load = time.perf_counter() - t0
    print(f"  模型加载: {load:.2f}s")

    tts = K.KokoroTTS("zf_xiaoxiao", 24000, 1440)
    total_audio = 0.0
    total_cost = 0.0
    print("  --- 逐句实测 ---")
    for s in SAMPLES:
        t1 = time.perf_counter()
        pcm = asyncio.run(tts._pcm(s, None))
        cost = time.perf_counter() - t1
        secs = len(pcm) / 2 / 24000 if pcm else 0.0
        if not pcm:
            print(f"  [空] {s[:20]}...")
            continue
        total_audio += secs
        total_cost += cost
        print(f"  音频 {secs:5.2f}s | 耗时 {cost:5.2f}s | RTF {cost / secs:5.3f}")
    if total_audio:
        rtf = total_cost / total_audio
        print(f"  --- 合计 ---\n  音频 {total_audio:.2f}s | 耗时 {total_cost:.2f}s "
              f"| 平均 RTF {rtf:.3f}")
        print(f"  结论: 合成 1 秒语音需要 {rtf:.3f} 秒 → "
              f"{'流畅（远快于实时）' if rtf < 0.3 else ('可用' if rtf < 1.0 else '吃力')}")
        return rtf
    return None


def probe_hw():
    print("\n== 本机硬件 ==")
    try:
        import platform
        print("  CPU:", platform.processor() or platform.machine())
        print("  核心:", os.cpu_count())
    except Exception:
        pass
    try:
        import psutil
        vm = psutil.virtual_memory()
        print(f"  内存: 总 {vm.total/2**30:.1f}GB / 可用 {vm.available/2**30:.1f}GB "
              f"({vm.percent}%)")
    except Exception:
        pass
    try:
        import onnxruntime as ort
        print("  onnxruntime providers:", ort.get_available_providers())
    except Exception as e:
        print("  onnxruntime:", e)


def probe_ollama():
    print("\n== Ollama（本地大模型）==")
    import urllib.request
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags",
                                    timeout=3) as r:
            import json
            data = json.loads(r.read().decode("utf-8"))
        for m in data.get("models", []):
            d = m.get("details") or {}
            print(f"  {m['name']:<34} {m['size']/2**30:5.2f}GB  "
                  f"{d.get('parameter_size','')} {d.get('quantization_level','')}")
    except Exception as e:
        print("  不可达:", e)


if __name__ == "__main__":
    probe_hw()
    probe_kokoro()
    probe_ollama()
