# -*- coding: utf-8 -*-
"""参考小智补齐的能力：多语言 / 多模型后端 / 记忆模式 / 智能家居 / 休眠。

全部用假件驱动，不连真机、不加载模型、不发网络请求，秒级可跑。
"""
from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from gateway.config import GatewayConfig                     # noqa: E402
from gateway.session import VoiceSession                     # noqa: E402
from gateway.providers import homeassistant as ha            # noqa: E402
from gateway.providers import llm_openai                     # noqa: E402

PASS = 0
FAIL = 0


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {extra}")


# ---------------- 假件 ----------------

class FakeLLM:
    def __init__(self, model="qwen3.5:9b-q4km"):
        self.model = model
        self.seen = None
        self.quick_out = "这是摘要。"

    async def chat(self, messages):
        self.seen = messages
        if False:
            yield ""
        yield "好的。"

    async def quick(self, prompt, max_tokens=32, timeout=30.0):
        self.last_prompt = prompt
        return self.quick_out


class FakeVAD:
    def new_state(self):
        return {}

    def reset_sentence(self, state):
        pass

    def process(self, state, pcm):
        return (False, False)


def make_session(cfg=None, llm=None):
    cfg = cfg or GatewayConfig()
    l = llm or FakeLLM()
    s = VoiceSession(None, cfg, l, None, None, FakeVAD())
    # 造一段历史：system + 6 条对话（3 轮）
    s.messages = [{"role": "system", "content": "你是小D"}]
    for i in range(3):
        s.messages.append({"role": "user", "content": f"用户{i}"})
        s.messages.append({"role": "assistant", "content": f"回复{i}"})
    return s


# ---------------- 1. 记忆模式 ----------------

def test_memory():
    print("\n[1] 记忆模式")
    cfg = GatewayConfig(memory_mode="full", memory_max_turns=40)
    s = make_session(cfg)
    ctx = s._context_messages()
    ok(len(ctx) == 7, "full 模式保留完整历史（1 system + 6 条）",
       f"实际 {len(ctx)}")
    ok(ctx[0]["role"] == "system", "full 模式首条仍是 system")

    # 截断：max_turns=1 → 只留最后一轮
    cfg2 = GatewayConfig(memory_mode="full", memory_max_turns=1)
    s2 = make_session(cfg2)
    ctx2 = s2._context_messages()
    ok(len(ctx2) == 3, "full 模式按轮数截断（1 system + 2 条）",
       f"实际 {len(ctx2)}")
    ok(ctx2[-1]["content"] == "回复2", "截断保留的是最近的一轮")

    # none：只带人设 + 当前这句
    cfg3 = GatewayConfig(memory_mode="none")
    s3 = make_session(cfg3)
    ctx3 = s3._context_messages()
    ok(len(ctx3) == 2, "none 模式只带人设 + 最后一句", f"实际 {len(ctx3)}")
    ok(ctx3[-1]["content"] == "回复2", "none 模式带的是最后一条")

    # summary：摘要并入人设，只留最近几轮
    cfg4 = GatewayConfig(memory_mode="summary", memory_keep_recent=1)
    s4 = make_session(cfg4)
    s4._summary = "用户喜欢简短回答。"
    ctx4 = s4._context_messages()
    ok(len(ctx4) == 3, "summary 模式 = 人设(含摘要) + 最近 2 条",
       f"实际 {len(ctx4)}")
    ok("更早对话的摘要" in ctx4[0]["content"], "摘要被并入人设而不是新增一条 system")
    ok(all(m["role"] == "system" for m in ctx4[:1]),
       "摘要模式仍然只有一条开头的 system（Qwen 模板要求）")

    # 压缩：超阈值 → 早期历史被压进摘要并删除
    cfg5 = GatewayConfig(memory_mode="summary", memory_summary_after=2,
                         memory_keep_recent=1)
    s5 = make_session(cfg5)
    before = len(s5.messages)
    asyncio.run(s5._maybe_compress())
    ok(s5._summary.startswith("这是摘要"), "压缩后生成了摘要",
       repr(s5._summary[:20]))
    ok(len(s5.messages) < before, "压缩后早期历史被删除",
       f"{before} → {len(s5.messages)}")
    ok(len([m for m in s5.messages if m.get("role") != "system"]) == 2,
       "压缩后只保留最近一轮原文")

    # 非 summary 模式不该压缩
    s6 = make_session(GatewayConfig(memory_mode="full"))
    n = len(s6.messages)
    asyncio.run(s6._maybe_compress())
    ok(len(s6.messages) == n and not s6._summary, "full 模式不做压缩")

    # 压缩失败不能影响对话
    class BadLLM(FakeLLM):
        async def quick(self, prompt, max_tokens=32, timeout=30.0):
            raise RuntimeError("boom")

    s7 = make_session(GatewayConfig(memory_mode="summary",
                                    memory_summary_after=2,
                                    memory_keep_recent=1), BadLLM())
    n7 = len(s7.messages)
    asyncio.run(s7._maybe_compress())
    ok(len(s7.messages) == n7 and not s7._summary,
       "压缩失败时保留原历史，不吞用户对话")


# ---------------- 2. 多语言 ----------------

def test_language():
    print("\n[2] 多语言识别")
    from gateway.providers.asr_sensevoice import SenseVoiceASR
    ok(list(SenseVoiceASR.LANGS) == ["auto", "zh", "yue", "en", "ja", "ko"],
       "SenseVoice 支持 中/粤/英/日/韩 + 自动")
    ok(SenseVoiceASR._norm_lang("ZH") == "zh", "语言大小写不敏感")
    ok(SenseVoiceASR._norm_lang("fr") == "auto", "不支持的语种回落到 auto")
    ok(SenseVoiceASR._norm_lang("") == "auto", "空值回落到 auto")

    cfg = GatewayConfig(asr_language="yue")
    ok(cfg.asr_language == "yue", "配置里有识别语言字段")


# ---------------- 3. 语言模型后端 ----------------

def test_llm_backend():
    print("\n[3] 语言模型后端")
    from gateway.server import _make_llm
    cfg = GatewayConfig(llm_backend="ollama", llm_model="qwen3.5:9b-q4km")
    llm = _make_llm(cfg)
    ok(type(llm).__name__ == "OllamaLLM", "默认走本地 Ollama")

    cfg2 = GatewayConfig(llm_backend="openai", llm_api_base="",
                         llm_api_key="")
    llm2 = _make_llm(cfg2)
    ok(type(llm2).__name__ == "OllamaLLM",
       "选了在线但没填地址 → 退回本地，不让网关起不来")

    cfg3 = GatewayConfig(llm_backend="openai",
                         llm_api_base="https://api.deepseek.com/v1",
                         llm_api_key="sk-x", llm_model="deepseek-chat")
    llm3 = _make_llm(cfg3)
    ok(type(llm3).__name__ == "OpenAICompatLLM", "填了地址就走在线端点")
    ok(llm3._endpoint == "https://api.deepseek.com/v1/chat/completions",
       "端点拼接正确", llm3._endpoint)

    # base_url 归一化
    ok(llm_openai._normalize_base("https://x.cn/v1/") ==
       "https://x.cn/v1", "去掉结尾斜杠")
    ok(llm_openai._normalize_base("https://x.cn/v1/chat/completions") ==
       "https://x.cn/v1", "已带 /chat/completions 时不重复拼接")

    ids = {p["id"] for p in llm_openai.PRESETS}
    ok({"deepseek", "dashscope", "zhipu"} <= ids,
       "预设含 DeepSeek / 阿里百炼 / 智谱 ChatGLM")
    ok(llm_openai.preset_by_id("deepseek")["model"] == "deepseek-chat",
       "预设可反查")


# ---------------- 4. 智能家居 ----------------

def test_homeassistant():
    print("\n[4] 智能家居（HomeAssistant）")
    ok(ha._extract_json('```json\n{"control": true}\n```')["control"] is True,
       "能剥掉模型的 ```json 围栏")
    ok(ha._extract_json("我觉得不是 {\"control\": false} 就这样") ==
       {"control": False}, "能从夹带文字的输出里抠出 JSON")
    ok(ha._extract_json("") == {}, "空输出不炸")

    ok(ha._guess_service("把客厅灯关了") == "turn_off", "关灯 → turn_off")
    ok(ha._guess_service("打开空调") == "turn_on", "打开 → turn_on")

    h = ha.HomeAssistant("", "")
    ok(not h.enabled, "没配地址/令牌时不启用")
    ok(ha.HomeAssistant("http://1.2.3.4:8123", "tok").enabled,
       "配了就启用")

    h2 = ha.HomeAssistant("http://x", "tok")
    ents = [
        {"entity_id": "light.kitchen",
         "attributes": {"friendly_name": "厨房灯"}},
        {"entity_id": "light.living",
         "attributes": {"friendly_name": "客厅灯"}},
    ]
    ok(h2._match_entity(ents, "客厅") == "light.living",
       "设备名关键词能匹配到 entity_id")
    ok(h2._match_entity(ents, "灯") == "light.kitchen",
       "多个候选时取名字最短的那个")
    ok(h2._match_entity(ents, "车库") == "", "匹配不到返回空（交给上层报错）")

    ok(ha.HomeAssistant._fix_domain("", "客厅灯") == "light",
       "LLM 没给 domain 时按中文关键词兜底")
    ok(ha.HomeAssistant._fix_domain("switch", "") == "switch",
       "给了就用给的")


# ---------------- 5. 自动休眠 ----------------

def test_sleep():
    print("\n[5] 自动休眠")
    cfg = GatewayConfig(idle_sleep_s=60)
    s = make_session(cfg)
    s._last_active = 0.0        # 假装很久没动
    s._check_sleep()
    ok(s._sleeping is True, "闲置超时后进入休眠")
    ok(s.state == "idle", "休眠时状态为 idle")

    s._touch()
    ok(s._sleeping is False, "有交互就唤醒")

    cfg0 = GatewayConfig(idle_sleep_s=0)
    s0 = make_session(cfg0)
    s0._last_active = 0.0
    s0._check_sleep()
    ok(s0._sleeping is False, "配置为 0 时不休眠")


# ---------------- 6. 配置链路 ----------------

def test_config_chain():
    print("\n[6] 配置链路（应用 → 网关）")
    from app import app_config as acfg
    from app import gateway_ctl
    for k in ("llm_backend", "llm_api_base", "llm_api_key", "asr_language",
              "memory_mode", "memory_summary_model", "idle_sleep_s",
              "ha_enabled", "ha_base_url", "ha_token"):
        ok(k in acfg.DEFAULTS, f"应用默认值里有 {k}")

    cfg = dict(acfg.DEFAULTS)
    cfg.update({"llm_backend": "openai",
                "llm_api_base": "https://api.deepseek.com/v1",
                "asr_language": "yue", "memory_mode": "summary",
                "idle_sleep_s": 120, "ha_enabled": True,
                "ha_base_url": "http://192.168.1.50:8123"})
    data = gateway_ctl.write_gateway_config(cfg)
    import json
    with open(data, encoding="utf-8") as f:
        g = json.load(f)
    for k in ("llm_backend", "llm_api_base", "asr_language", "memory_mode",
              "idle_sleep_s", "ha_enabled", "ha_base_url"):
        ok(k in g, f"网关配置里有 {k}")
    ok(g["llm_backend"] == "openai" and g["asr_language"] == "yue"
       and g["memory_mode"] == "summary" and g["idle_sleep_s"] == 120,
       "值正确传到网关配置")

    # 网关侧字段存在且能被覆盖
    c = GatewayConfig()
    for k in ("llm_backend", "asr_language", "memory_mode", "idle_sleep_s",
              "ha_enabled"):
        ok(hasattr(c, k), f"GatewayConfig 有字段 {k}")

    from app import api as apimod
    ok(any(b["id"] == "openai" for b in apimod.LLM_BACKENDS),
       "界面能选到在线后端")
    ok(len(apimod.ASR_LANGUAGES) == 6, "语种选项含 auto + 五种语言")
    ok([m["id"] for m in apimod.MEMORY_MODES] == ["full", "summary", "none"],
       "记忆模式三选")


if __name__ == "__main__":
    test_memory()
    test_language()
    test_llm_backend()
    test_homeassistant()
    test_sleep()
    test_config_chain()
    print(f"\nFEATURE_PASS {PASS}/{PASS + FAIL}")
    sys.exit(0 if FAIL == 0 else 1)
