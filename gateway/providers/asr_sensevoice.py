# -*- coding: utf-8 -*-
"""SenseVoice（sherpa-onnx）离线 ASR + 基于停顿的标点恢复。

**为什么要自己加标点**：SenseVoice 的 `use_itn` 只做数字/单位规范化，
输出**完全不带标点**（实测英文全大写无句读、中文无标点）。没有标点的文本
直接念出来是一口气糊下来的，断句也全乱 —— 而下游 LLM 看到的用户输入同样
受益（"我说的话输入进去的文字没有标点符号断句"）。

**怎么加**：模型本身不猜标点，改用**停顿时长**这个客观信号 ——
采集期上层已经把 VAD 判定的语音段起止传下来了（spans），间隙越长越是
句子边界：≥220ms 补逗号，≥480ms 补句号。字符位置按"时间占比"折算
（中文基本一字一音，比例映射足够准）。没有 spans 时用能量包络自查。
"""
from __future__ import annotations

import bisect
import os
import threading
from typing import List, Optional, Tuple

import numpy as np
import sherpa_onnx

from gateway.providers.base import BaseASR

# 停顿 → 标点 的映射（毫秒）
_COMMA_GAP_MS = 220      # 换气/意群停顿 → 逗号
_PERIOD_GAP_MS = 480     # 句子停顿 → 句号
_MIN_CHARS = 4           # 太短的识别结果不加标点（"好的"就是"好的"）
_MAX_PUNCT_DENSITY = 3   # 最多每 3 个字一个标点，避免密恐
_LONG_NOGAP_CHARS = 22   # 全程无停顿的长句：在中点补一个逗号，别一口气糊完
_ENERGY_MERGE_MS = 300   # 能量自查时，间隔小于此值的两段合成一段
                         # （20ms 窗的能量判据会把一句话切碎，不合并就会满篇逗号）
_ENERGY_MIN_SEG_MS = 120 # 能量自查的语音段最短时长

_PUNCT_CHARS = "，。！？；、,.!?;：:~…"


def _is_ascii_word_pair(a: str, b: str) -> bool:
    """插入点两侧都是英文字母 → 这是在英文单词内部，不能插标点。"""
    return (a and b and a.isascii() and a.isalpha()
            and b.isascii() and b.isalpha())


def _snap_boundary(text: str, pos: int) -> int:
    """把插入点吸附到最近的自然边界（只对英文有效）。

    英文按空格切词，比例折算落进 "G|OODBYE" 这种词中间时，向两侧找
    最近的空格贴上去；中文没有空格，保持原位（中文任意字符边界都是
    合法的词边界）。"""
    for d in range(0, 4):
        for q in (pos - d, pos + d):
            if q < 1 or q >= len(text):
                continue
            left, right = text[q - 1], text[q]
            if left.isascii() and left.isalnum() and right == " ":
                return q
            if left == " " and right.isascii() and right.isalnum():
                return q
    return pos


def _local_mark(mark: str, text: str, p: int) -> Tuple[int, str]:
    """按插入点上下文挑标点样式，并吃掉英文句子前的多余空格。

    英文语境（最近一个非空字符是 ASCII 字母数字）用半角标点，并把
    "WORLD ，AND" 纠正成 "WORLD, AND"；中文语境保持全角。"""
    left = text[p - 1] if p >= 1 else ""
    half = "." if mark == "。" else ","
    if left == " " and p >= 2 and text[p - 2].isascii() \
            and text[p - 2].isalnum():
        return p - 1, half
    if left and left.isascii() and left.isalnum():
        return p, half
    return p, mark


def _energy_spans(pcm: bytes, sample_rate: int, frame_ms: int = 20
                  ) -> List[Tuple[int, int]]:
    """没有 VAD spans 时的兜底：用能量包络找语音段。

    20ms 窗 RMS > max(底噪×3, 200) 判为语音；过短的段当噪声丢掉，
    间隔不足 _ENERGY_MERGE_MS 的两段合并（能量判据会把一句话切碎）。"""
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if x.size == 0:
        return []
    n = int(sample_rate * frame_ms / 1000)
    if n <= 0:
        return []
    frames = x[:len(x) // n * n].reshape(-1, n)
    rms = np.sqrt((frames * frames).mean(axis=1)) if frames.size else np.array([])
    if rms.size == 0:
        return []
    floor = float(np.percentile(rms, 20))
    loud = float(np.percentile(rms, 90))
    # 语音占比很高时（>80%），20 分位数已经落在语音电平上，会把整段判成
    # 静音；用"安静段估计 / 10"封顶，保证 speech-dominant 音频也有得救。
    floor = min(floor, 0.1 * loud)
    thr = max(floor * 3.0, 200.0)
    speech = rms > thr
    raw: List[Tuple[int, int]] = []
    i = 0
    while i < len(speech):
        if not speech[i]:
            i += 1
            continue
        j = i
        while j < len(speech) and speech[j]:
            j += 1
        if (j - i) * frame_ms >= 80:
            raw.append((i * n, j * n))
        i = j
    if not raw:
        return []
    # 合并碎段 + 丢掉太短的
    merged: List[Tuple[int, int]] = [raw[0]]
    for s, e in raw[1:]:
        if (s - merged[-1][1]) * 1000.0 / sample_rate < _ENERGY_MERGE_MS:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return [(s, e) for (s, e) in merged
            if (e - s) * 1000.0 / sample_rate >= _ENERGY_MIN_SEG_MS]


def _token_offsets(text: str, tokens: List[str]) -> List[int]:
    """每个 token 在 text 里的起始字符下标（对不上就保持原位）。

    SenseVoice 的 text 基本等于 token 顺序拼接（含 ITN 后的写法），
    顺序 find 足够对齐；个别 token 因 ITN 变形找不到时退化为"不偏移"，
    顶多少插一个标点，不会错位到别处。"""
    offsets: List[int] = []
    pos = 0
    for tok in tokens:
        t = (tok or "").strip()
        if not t:
            offsets.append(pos)
            continue
        idx = text.find(t, pos)
        if idx < 0:
            offsets.append(pos)
        else:
            offsets.append(idx)
            pos = idx + len(t)
    return offsets


def _mid_comma(text: str) -> str:
    """全程无停顿的长句：在中点补一个逗号，别一口气糊完。"""
    if len(text) < _LONG_NOGAP_CHARS or any(c in text for c in _PUNCT_CHARS):
        return text
    mid = _snap_boundary(text, len(text) // 2)
    mid = max(1, min(len(text) - 1, mid))
    mid, mark = _local_mark("，", text, mid)
    return text[:mid] + mark + text[mid:]


def _punct_by_tokens(text: str, tokens: List[str], stamps: List[float],
                     spans: List[Tuple[int, int]], sample_rate: int) -> str:
    """用 token 时间戳把停顿**精确**对齐到字符边界，再插标点。

    比"按字数比例折算"准得多：停顿中心落在哪个 token 之前，标点就插在
    哪个 token 后面 —— 永远不会出现"我们，出去玩"这种把词切开的插法。"""
    if len(tokens) != len(stamps) or not tokens:
        return add_punctuation(text, spans, 0, sample_rate)
    offsets = _token_offsets(text, tokens)
    stripped = [(t or "").strip() for t in tokens]
    inserts: List[Tuple[int, str, int]] = []      # (位置, 标点, 替换长度)
    last_pos = -_MAX_PUNCT_DENSITY
    for k in range(1, len(spans)):
        gap_ms = (spans[k][0] - spans[k - 1][1]) * 1000.0 / sample_rate
        if gap_ms < _COMMA_GAP_MS:
            continue
        mark = "。" if gap_ms >= _PERIOD_GAP_MS else "，"
        center = ((spans[k - 1][1] + spans[k][0]) / 2.0) / sample_rate
        j = bisect.bisect_right(stamps, center) - 1
        if j < 0 or j >= len(offsets):
            continue
        p = offsets[j] + len(stripped[j])
        p = max(1, min(len(text), p))
        p, mark = _local_mark(mark, text, p)
        p = max(1, min(len(text), p))
        if p - last_pos < _MAX_PUNCT_DENSITY:
            continue
        # 英文单词内部不插（"CHIE,F"）；已有标点处跳过，
        # 紧跟逗号且要插句号时把逗号"升级"，别叠成"。。"
        if _is_ascii_word_pair(text[p - 1] if p >= 1 else "",
                               text[p] if p < len(text) else ""):
            continue
        if p < len(text) and text[p] in _PUNCT_CHARS:
            continue
        if text[p - 1] in _PUNCT_CHARS:
            if mark in ("。", ".") and text[p - 1] in ("，", ","):
                inserts.append((p - 1, mark, 1))
                last_pos = p
            continue
        inserts.append((p, mark, 0))
        last_pos = p
    out = text
    for p, mark, rep in sorted(inserts, key=lambda t: -t[0]):
        out = out[:p] + mark + out[p + rep:]
    if not inserts:
        out = _mid_comma(out)
    return out


def add_punctuation(text: str, spans: List[Tuple[int, int]],
                    total_samples: int, sample_rate: int) -> str:
    """按停顿时长给识别文本补标点（无 token 时间戳时的比例折算版）。

    spans 必须是升序、不重叠的语音段。**位置折算用语音时长占比**而不是绝对
    时间占比：文本长度对应的是"说了多少话"，与静音多长无关；开口前的静音
    已经被上层裁掉，但句间的静音仍在，用占比才不会把标点插得太偏。"""
    text = (text or "").strip()
    if len(text) < _MIN_CHARS:
        return text
    if total_samples <= 0 or len(spans) < 2:
        # 没有任何停顿信息：长句在中点补个逗号，别一口气糊完
        return _mid_comma(text)

    durs = [max(0, e - s) for (s, e) in spans]
    total_speech = sum(durs)
    if total_speech <= 0:
        return text

    inserts: List[Tuple[int, str]] = []
    last_pos = -_MAX_PUNCT_DENSITY
    cum = 0
    for k in range(1, len(spans)):
        cum += durs[k - 1]
        gap_ms = (spans[k][0] - spans[k - 1][1]) * 1000.0 / sample_rate
        if gap_ms < _COMMA_GAP_MS:
            continue
        mark = "。" if gap_ms >= _PERIOD_GAP_MS else "，"
        pos = int(len(text) * cum / total_speech)
        pos = max(1, min(len(text) - 1, pos))
        pos, mark = _local_mark(mark, text, pos)
        pos = max(1, min(len(text) - 1, pos))
        pos = _snap_boundary(text, pos)
        if pos - last_pos < _MAX_PUNCT_DENSITY:
            continue
        # 英文单词内部不插；附近已有标点就跳过（紧跟逗号且要插句号则升级）
        if _is_ascii_word_pair(text[pos - 1], text[pos]):
            continue
        window = text[max(0, pos - 2):pos + 2]
        if text[pos - 1] in _PUNCT_CHARS or text[pos] in _PUNCT_CHARS:
            if mark in ("。", ".") and text[pos - 1] in ("，", ","):
                inserts.append((pos - 1, mark))
                last_pos = pos
            continue
        if any(c in _PUNCT_CHARS for c in window):
            continue
        inserts.append((pos, mark))
        last_pos = pos

    out = text
    for pos, mark in sorted(inserts, key=lambda t: -t[0]):
        out = out[:pos] + mark + out[pos:]
    if not inserts:
        out = _mid_comma(out)
    return out


class SenseVoiceASR(BaseASR):
    # SenseVoice 支持的语言。auto 对应 sherpa 的""（模型自己判断语种）。
    LANGS = ("auto", "zh", "yue", "en", "ja", "ko")

    def __init__(self, model_dir: str, sample_rate: int = 16000,
                 language: str = "auto"):
        model = os.path.join(model_dir, "model.int8.onnx")
        tokens = os.path.join(model_dir, "tokens.txt")
        if not os.path.isfile(model) or not os.path.isfile(tokens):
            raise FileNotFoundError(f"SenseVoice 模型缺失: {model_dir}")
        self.sample_rate = sample_rate
        self._lock = threading.Lock()
        self._model = model
        self._tokens = tokens
        # 识别器按语言缓存：切换语言不必重新加载模型文件，但每种语言得有
        # 自己的实例（language 是构造期参数，改不了）。六个实例共占的模型
        # 权重是共享的，额外开销主要是各自的计算图缓存，实测可接受。
        self._recs: dict = {}
        self.language = self._norm_lang(language)
        self._rec = self._get(self.language)

    @classmethod
    def _norm_lang(cls, lang: str) -> str:
        v = (lang or "auto").strip().lower()
        return v if v in cls.LANGS else "auto"

    def _get(self, lang: str):
        rec = self._recs.get(lang)
        if rec is None:
            rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=self._model,
                tokens=self._tokens,
                num_threads=2,
                sample_rate=self.sample_rate,
                feature_dim=80,
                decoding_method="greedy_search",
                debug=False,
                use_itn=True,
                # 空串＝自动判断语种；指定语言能明显提升该语种的准确率，
                # 代价是其它语种基本识别不出来（所以默认还是 auto）。
                language=("" if lang == "auto" else lang),
            )
            self._recs[lang] = rec
        return rec

    def set_language(self, language: str) -> str:
        """切换识别语言（下一句生效）；返回实际生效的语言。"""
        lang = self._norm_lang(language)
        if lang != self.language:
            with self._lock:
                self.language = lang
                self._rec = self._get(lang)
        return self.language

    def transcribe(self, pcm_s16le: bytes,
                   spans: Optional[List[Tuple[int, int]]] = None) -> str:
        audio = np.frombuffer(pcm_s16le, dtype=np.int16).astype(np.float32) \
            / 32768.0
        if audio.size < int(self.sample_rate * 0.1):
            return ""
        with self._lock:
            stream = self._rec.create_stream()
            stream.accept_waveform(self.sample_rate, audio)
            self._rec.decode_stream(stream)
            result = stream.result
            text = (result.text or "").strip()
            # SenseVoice 顺带给出 token 级时间戳：停顿对齐到字符边界就靠它
            tokens = list(getattr(result, "tokens", []) or [])
            stamps = [float(t) for t in (getattr(result, "timestamps", [])
                                         or [])]
        if not text:
            return ""
        if spans is None:
            spans = _energy_spans(pcm_s16le, self.sample_rate)
        if len(tokens) == len(stamps) and tokens:
            return _punct_by_tokens(text, tokens, stamps, spans,
                                    self.sample_rate)
        return add_punctuation(text, spans, len(pcm_s16le) // 2,
                               self.sample_rate)
