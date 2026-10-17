# -*- coding: utf-8 -*-
"""离线（本地）音色的试听与切换 实测。

跑法：
    C:/Python314/python.exe tests/test_local_voice_preview.py

用户报的是"离线音色既试听不了、也切不动"。三个真实根因各自留一个用例：
  1. 试听**因为设备端点打不开而失败**——选中的 WASAPI/WDM-KS 端点只吃原生
     采样率，开 24k 直接报 Invalid device。通话链路有回退、试听以前没有 →
     修在 app.devices.candidates，两条链路共用。这里验证候选链。
  2. **SAPI 音色切换是假的**——SSML 根元素写死 xml:lang="zh-CN"，SAPI 会按
     语种重挑音色，覆盖 ISpVoice::Voice 属性：选英语 Zira 出来的还是中文女声
     （两个音色 md5 完全一样）。这里做"不同音色必须不同音频"的回归。
  3. 离线清单本身要对（神经音色 + 系统音色），且每个都能合成出声音。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["PATH"] = (os.path.join(ROOT, "reference", "bin") + os.pathsep
                      + os.environ.get("PATH", ""))

PASS = 0
FAIL: list[str] = []


def ok(name: str, cond, note: str = "") -> bool:
    global PASS
    if cond:
        PASS += 1
        print(f"  ok   {name}" + (f"  {note}" if note else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}" + (f"  {note}" if note else ""))
    return bool(cond)


def md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()[:10]


# ---------------------------------------------------------------- 1. 设备回退
def t_devices() -> None:
    print("=" * 72)
    print("1) 试听播放端点：必须有回退链（选中端点 → 同名其它 hostapi → 默认）")
    print("=" * 72)
    from app import devices

    cands = devices.candidates(-1, "out")
    ok("候选里以选中端点开头", cands[0] == -1, str(cands))
    ok("末尾兜底是 None（交给 PortAudio 自选）", cands[-1] is None, str(cands))
    ok("候选无重复", len(cands) == len(set(cands)), str(cands))
    # 真实列表的第一台设备：应当能带出同名设备的其它 hostapi 端点
    try:
        devs = devices.query()
        outs = devs.get("outputs") or []
        spk = devices.resolve(outs, None, "", devs.get("default_output"), "spk")
        c2 = devices.candidates(spk, "out")
        print(f"  默认扬声器 index={spk} → 候选 {c2}")
        ok("真实设备也能展开多个候选", len(c2) >= 2, str(c2))
    except Exception as e:  # noqa: BLE001
        ok("真实设备也能展开多个候选", False, repr(e))


# ------------------------------------------------------ 2. SAPI 音色真的能换
def t_sapi_switch() -> None:
    print()
    print("=" * 72)
    print("2) 系统（SAPI）音色切换：不同音色必须出不同的音频")
    print("=" * 72)
    from gateway.providers import tts_sapi
    from gateway.providers.tts_router import local_voices

    root = tts_sapi.SSML_ROOT
    ok("SSML 根元素不再写死 xml:lang（它会覆盖 Voice 属性）",
       "xml:lang" not in root, root)

    voices = local_voices()
    ok("本机至少有一个系统音色", len(voices) >= 1, f"{len(voices)} 个")

    async def run():
        seen: dict[str, str] = {}
        for v in voices:
            en = v["lang"].startswith("en")
            text = ("Hello there, how are you today?" if en
                    else "你好，我是小D，很高兴认识你。")
            pcm = await tts_sapi.SapiTTS(v["id"], 24000, 1440)._pcm(
                text, {"rate": 1.0, "pitch": 1.0, "volume": 1.0})
            h = md5(pcm)
            dup = seen.get(h)
            seen[h] = v["name"]
            print(f"  {v['lang']:6s}{v['name'][:34]:36s} {len(pcm):7d}B {h}")
            ok(f"  {v['name'][:24]} 合成出声", len(pcm) > 5000, f"{len(pcm)}B")
            ok(f"  {v['name'][:24]} 音色与他人不同", not dup,
               f"与 {dup} 相同" if dup else "")
    asyncio.run(run())


# -------------------------------------------- 3. 离线全部音色：出声 + 可区分
def t_offline_list() -> None:
    print()
    print("=" * 72)
    print("3) 离线音色清单：每个都能试听出声，且两两不同")
    print("=" * 72)
    from gateway.providers import tts_kokoro
    from gateway.providers.tts_router import TTSRouter, all_voices, neuro_available

    locals_ = [v for v in all_voices() if v.get("engine") == "local"]
    ok("离线音色不少于 3 个", len(locals_) >= 3, f"{len(locals_)} 个")
    ok("每条都有 id/name/group",
       all(v.get("id") and v.get("name") and v.get("group") for v in locals_))
    print(f"  神经模型可用: {neuro_available()}（共 {len(locals_)} 条）")

    groups: dict[str, int] = {}

    async def run():
        seen: dict[str, str] = {}
        for v in locals_[:8]:          # 前 8 条足够覆盖"切换生效"这件事
            vid = v["id"]
            en = tts_kokoro.is_neuro_id(vid[len("local:"):]) and not \
                vid.startswith("local:z")
            text = ("Hello there, how are you today?" if en
                    else "你好，我是小D，很高兴认识你。")
            r = TTSRouter(vid, 24000, 1440, engine="auto")
            pcm = await r.preview_pcm(text,
                                      {"rate": 1.0, "pitch": 1.0, "volume": 1.0})
            h = md5(pcm)
            dup = seen.get(h)
            seen[h] = vid
            groups[v["group"]] = groups.get(v["group"], 0) + 1
            print(f"  {vid[:34]:36s} → {r.last_engine} {len(pcm):7d}B {h}")
            ok(f"  {vid[:28]} 试听有声音", len(pcm) > 5000, f"{len(pcm)}B")
            ok(f"  {vid[:28]} 与其它音色不同", not dup, f"重复 {dup}" if dup else "")
    asyncio.run(run())
    print(f"  分组: {groups}")


# ------------------------------------------- 4. 试听 API 端到端（不真放声音）
def t_preview_api() -> None:
    print()
    print("=" * 72)
    print("4) Api.preview_voice 端到端（打桩 sd.play，不真的外放）")
    print("=" * 72)
    import sounddevice as sd

    played: list[tuple] = []

    def fake_play(x, rate, device=None, **kw):
        played.append((rate, device, len(x)))
        return None

    def fake_wait(*a, **k):
        return None

    real_play, real_wait = sd.play, sd.wait
    sd.play, sd.wait = fake_play, fake_wait
    try:
        from app.api import Api
        api = Api()
        api.cfg["tts_engine"] = "local"       # 强制走离线，避免依赖网络
        api.cfg["voice"] = "local:zf_xiaoxiao"
        r = api.preview_voice()
        print(f"  返回: {r}")
        ok("试听成功返回 ok", bool(r.get("ok")), str(r.get("error", "")))
        ok("返回里带实际设备名", bool(r.get("device")), str(r.get("device")))
        ok("确实调用了播放", len(played) >= 1, str(played))
        if played:
            ok("播放采样率 24000", played[0][0] == 24000, str(played[0][0]))
            ok("放出来的 PCM 有长度", played[0][2] > 5000, str(played[0][2]))

        # 端点全拔的情形：候选逐个失败也不能抛异常，必须给出明确错误
        played.clear()

        def dead_play(*a, **k):
            raise RuntimeError("Invalid device")

        sd.play = dead_play
        sd.wait = fake_wait
        r2 = api.preview_voice()
        print(f"  全端点失败: {r2}")
        ok("所有端点都失败时返回错误而不是崩溃",
           isinstance(r2, dict) and "ok" in r2, str(r2))
    finally:
        sd.play, sd.wait = real_play, real_wait


if __name__ == "__main__":
    t_devices()
    t_sapi_switch()
    t_offline_list()
    t_preview_api()
    print()
    if FAIL:
        print(f"LOCAL_VOICE_FAIL {len(FAIL)}/{PASS + len(FAIL)}")
        for f in FAIL:
            print("   - " + f)
        sys.exit(1)
    print(f"LOCAL_VOICE_PASS {PASS}/{PASS}")
