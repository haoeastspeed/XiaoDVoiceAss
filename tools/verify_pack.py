# -*- coding: utf-8 -*-
"""核验打包产物里是否含本次修复的符号（从 onedir exe 的 PYZ 里解出字节码查字符串）。

用法： python tools/verify_pack.py "dist_fix/小D智能助手/小D智能助手.exe"
"""
from __future__ import annotations

import sys

from PyInstaller.archive.readers import CArchiveReader

TARGETS = {
    # session 里是以 web_weather / web_search 的别名用的，原始名 _weather
    # 不会出现在 session 的常量表里。
    "gateway.session": ["_notify_tts_state", "notice", "_tts_degraded",
                        "web_weather", "_maybe_web_search", "_restore_context",
                        # 回声门 / AEC（防设备自激活）
                        "_on_gated", "_gate_on", "echo_gate_cooldown_ms",
                        "_make_aec"],
    # 网关侧回声消除：类名与延迟 import 的原生包名都必须进包
    "gateway.audio.aec": ["EchoCanceller", "_resample", "pywebrtc_audio",
                          "stream_delay_ms", "feed_far", "_take_far"],
    "gateway.protocol": ["notice"],
    "gateway.providers.tts_router": ["local_label", "last_error", "degraded",
                                     "local:", "_ok_streak", "_fail_streak",
                                     "cooldown_left", "_pick_local"],
        "gateway.providers.tts_sapi": ["_jitter_style", "_normalize",
                                   # xml:lang 写死会让 SAPI 忽略 Voice 属性（音色切换失效）
                                   'xmlns="http://www.w3.org/2001/10/synthesis">'],
    # 注意：wttr 只出现在 f-string 拼出的 URL 里、回答规则是长字面量的
    # 一部分，都不是独立常量——用它们各自的相邻常量来验。
    "gateway.providers.websearch": ["looks_weather", "weather", "https://wttr.in/",
                                    "以上是刚刚联网查到的资料"],
    "gateway.providers.asr_sensevoice": ["add_punctuation", "_punct_by_tokens",
                                         "_energy_spans", "_local_mark",
                                         "_is_ascii_word_pair",
                                         # 多语言（中/粤/英/日/韩）
                                         "set_language", "_norm_lang",
                                         "yue"],
    # 在线大模型后端（DeepSeek / 百炼 / 智谱 …）与智能家居，都是延迟导入
    "gateway.providers.llm_openai": ["OpenAICompatLLM", "PRESETS",
                                     "_normalize_base", "list_models",
                                     "dashscope", "deepseek", "zhipu"],
    "gateway.providers.homeassistant": ["HomeAssistant", "_extract_json",
                                        "_guess_service", "_match_entity",
                                        "_INTENT_PROMPT", "_DOMAIN_ALIAS"],
    "app.voice_client": ["BARGE_REF_RATIO", "BARGE_ECHO_TOL", "_do_stop",
                         "play_volume", "_scale", "set_volume"],
    "app.api": ["toggle_fullscreen", "play_volume", "__no_ollama__",
                # 多模型后端 / 记忆模式 / 智能家居（界面与连通性自检）
                "LLM_BACKENDS", "ASR_LANGUAGES", "MEMORY_MODES",
                "LANG_VOICE_HINT", "test_llm", "ha_test", "memory_mode"],
    "app.app_config": ["play_volume", "哄睡轻语", "东北老铁", "二次元萌妹",
                       "echo_gate_cooldown_ms", "echo_cancel", "echo_gate"],
}


def _const_strings(obj, acc: set) -> None:
    """把任意深度的常量容器里的字符串都摊平收进来。"""
    for x in obj:
        if isinstance(x, str):
            acc.add(x)
        elif isinstance(x, (tuple, frozenset, list)):
            _const_strings(x, acc)
        elif hasattr(x, "co_names"):
            _strings(x, acc)


def _strings(code, acc: set) -> None:
    """递归收集 code 对象里的所有标识符 / 字符串常量。"""
    for name in getattr(code, "co_names", ()):
        acc.add(name if isinstance(name, str) else str(name))
    for name in getattr(code, "co_varnames", ()):
        acc.add(name if isinstance(name, str) else str(name))
    for name in getattr(code, "co_freevars", ()):
        acc.add(name if isinstance(name, str) else str(name))
    for c in getattr(code, "co_consts", ()):
        if isinstance(c, str):
            acc.add(c)
        elif isinstance(c, (tuple, frozenset, list)):
            # 常量折叠会把"字符串的元组"再套一层（如预设列表里的
            # (名称, 提示词)），必须递归到底，否则会漏判为"没进包"
            for x in c:
                if isinstance(x, str):
                    acc.add(x)
                elif isinstance(x, (tuple, frozenset, list)):
                    _const_strings(x, acc)
        elif hasattr(c, "co_names"):
            _strings(c, acc)
    for c in getattr(code, "co_consts", ()):
        pass


def main() -> int:
    exe = sys.argv[1]
    car = CArchiveReader(exe)
    toc = car.toc
    pyz_name = next((k for k in toc if str(k).endswith(".pyz")), None)
    if pyz_name is None:
        print("!! 归档里找不到 PYZ，toc 前 20 项：")
        print(list(toc)[:20])
        return 2
    print(f"PYZ = {pyz_name}")
    pyz = car.open_embedded_archive(pyz_name)
    entries = set(pyz.toc)
    ok = True
    for mod, needles in TARGETS.items():
        if mod not in entries:
            print(f"[缺失] {mod} 不在归档里")
            ok = False
            continue
        blob = pyz.extract(mod)
        acc: set = set()
        _strings(blob, acc)
        # 精确命中，或作为某个长字面量的子串命中（长提示词经常被折叠成
        # 一整句，逐字匹配会误报"没进包"）
        def hit(n):
            return n in acc or any(n in s for s in acc if len(s) > len(n))

        hits = [n for n in needles if hit(n)]
        miss = [n for n in needles if not hit(n)]
        flag = "OK  " if not miss else "FAIL"
        if miss:
            ok = False
        print(f"[{flag}] {mod:<32} 命中 {len(hits)}/{len(needles)}"
              + (f"  未命中={miss}" if miss else ""))
    print("\n==>", "打包产物包含本次全部修复" if ok else "打包产物不完整")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
