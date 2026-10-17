# -*- coding: utf-8 -*-
"""音频设备枚举：把 PortAudio 的设备列表整理成"人能看懂"的麦克风/扬声器列表。

PortAudio 会把同一台物理设备的每种 hostapi（MME / DirectSound / WASAPI /
WDM-KS）各列一遍，还会混进"声音映射器""主声音驱动"这类虚拟端点，甚至把
WDM-KS 的环回采集端点（"xxx output with SST"）当成麦克风。这里统一归类、
去重、排序，并在蓝牙 HFP 上打上"蓝牙·通话"的标记。
"""
from __future__ import annotations

import re

import sounddevice as sd

# hostapi 优先级（越小越好）。
# 实测（本机 Realtek）：16kHz 采集 / 24kHz 播放只有 **MME 与 DirectSound**
# 能打开；WASAPI 共享模式只吃设备原生采样率（报 -9997），WDM-KS 直接失败。
# 所以优先 DirectSound（延迟比 MME 低），把 WASAPI/WDM-KS 排到最后兜底。
_PRIO = {"Windows DirectSound": 0, "MME": 1,
         "Windows WASAPI": 8, "Windows WDM-KS": 9}

_VIRTUAL = ("声音映射器", "主声音", "sound mapper", "primary sound",
            "立体声混音", "stereo mix", "line in", "what u hear")
# 名字里带这些词、但其实是"输出端点"的，不能当麦克风
_OUT_LIKE = ("output", "speakers", "扬声器", "电脑扬声器")


def _bt_name(raw: str) -> str:
    """从蓝牙 HFP 驱动路径里提取干净的耳机名。"""
    m = re.search(r"\(([^()]*?(?:TWS|AirPods|FreeBuds|Buds?|耳机|Ear)[^()]*?)\)",
                  raw, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    groups = re.findall(r"\(([^()]{2,})\)", raw)
    return groups[-1].strip() if groups else ""


def query() -> dict:
    """返回 {inputs, outputs, default_input, default_output[, error]}。"""
    try:
        devs = list(sd.query_devices())
        apis = list(sd.query_hostapis())
    except Exception as e:  # noqa: BLE001
        return {"inputs": [], "outputs": [], "default_input": None,
                "default_output": None,
                "error": f"无法访问音频设备：{e}"}

    ins: dict[str, dict] = {}
    outs: dict[str, dict] = {}
    for i, d in enumerate(devs):
        name = (d.get("name") or "").replace("\r", " ").replace("\n", " ").strip()
        name = re.sub(r"\s+", " ", name)
        low = name.lower()
        if not name:
            continue
        if any(v in name or v in low for v in _VIRTUAL):
            continue
        api_i = d.get("hostapi", -1)
        api_name = (apis[api_i].get("name", "")
                    if isinstance(api_i, int) and 0 <= api_i < len(apis) else "")
        ch_in = int(d.get("max_input_channels", 0) or 0)
        ch_out = int(d.get("max_output_channels", 0) or 0)
        is_bt = ("bthhfenum" in low or "hands-free" in low)

        # 名字像输出设备的采集端点（WDM-KS 环回）不算麦克风
        if ch_in > 0 and any(v in low for v in _OUT_LIKE):
            ch_in = 0
        if ch_in <= 0 and ch_out <= 0:
            continue

        label = name
        if is_bt:
            bt = _bt_name(name) or "蓝牙耳机"
            label = f"{bt}（蓝牙·通话）"
        elif re.fullmatch(r"耳机\s*\(\s*\)", name):
            label = "耳机（蓝牙·高音质）"

        rec = {"index": i, "name": label, "bluetooth": is_bt, "api": api_name}
        prio = _PRIO.get(api_name, 9)
        key = label.lower()
        if ch_in > 0:
            old = ins.get(key)
            if old is None or prio < old["_prio"]:
                ins[key] = dict(rec, _prio=prio)
        if ch_out > 0:
            old = outs.get(key)
            if old is None or prio < old["_prio"]:
                outs[key] = dict(rec, _prio=prio)

    def _finish(m: dict[str, dict], bluetooth_last: bool) -> list[dict]:
        out = sorted(m.values(),
                     key=lambda x: (x["_prio"], x["bluetooth"] if bluetooth_last
                                    else not x["bluetooth"], x["name"]))
        for x in out:
            x.pop("_prio", None)
        return out

    di, do = (sd.default.device if isinstance(sd.default.device, (list, tuple))
              else (None, None))
    inputs = _finish(ins, bluetooth_last=True)
    outputs = _finish(outs, bluetooth_last=True)
    return {"inputs": inputs, "outputs": outputs,
            "default_input": _match(inputs, di),
            "default_output": _match(outputs, do)}


def _match(items: list[dict], idx) -> int | None:
    if isinstance(idx, int) and idx >= 0:
        for it in items:
            if it["index"] == idx:
                return idx
    return items[0]["index"] if items else None


def resolve(items: list[dict], saved_index, saved_name: str,
            default_index, role: str) -> int | None:
    """按 保存的名字 → 保存的 index → 默认设备 → 规则（内置阵列麦）解析。"""
    if saved_name:
        for it in items:
            if it["name"] == saved_name:
                return it["index"]
    if isinstance(saved_index, int):
        for it in items:
            if it["index"] == saved_index:
                return saved_index
    if default_index is not None:
        for it in items:
            if it["index"] == default_index:
                return default_index
    if role == "mic":
        arrive = [it for it in items
                  if "阵列" in it["name"] and not it["bluetooth"]]
        if arrive:
            return arrive[0]["index"]
    if items:
        return items[0]["index"] if role == "mic" else items[0]["index"]
    return None


def candidates(primary, direction: str) -> list:
    """打开音频流时的候选端点：选中端点 → 同名设备其它 hostapi → 系统默认 → None。

    同一物理设备在 PortAudio 里会以多个 hostapi 端点出现（MME / DirectSound /
    WASAPI / WDM-KS），实测只有 DirectSound / MME 能开 16k/24k（WASAPI 共享
    模式报 -9997、WDM-KS 直接失败）。**必须按序试到成功为止**——通话链路
    （voice_client）一直有这个回退所以能出声；试听此前直接拿选中索引去开流，
    没有回退 → 用户看到的就是"试听永远失败：Invalid device"。
    通话与试听共用这一份，避免两条链路行为不一致。"""
    out: list = []

    def add(d):
        if d is not None and d not in out:
            out.append(d)

    add(primary)
    try:
        devs = list(sd.query_devices())
        apis = list(sd.query_hostapis())
        if isinstance(primary, int) and 0 <= primary < len(devs):
            key = devs[primary]["name"].strip().lower()[:20]
            alts = []
            for i, d in enumerate(devs):
                ch = (d["max_output_channels"] if direction == "out"
                      else d["max_input_channels"])
                if ch > 0 and d["name"].strip().lower()[:20] == key:
                    ai = d.get("hostapi", -1)
                    an = (apis[ai]["name"] if isinstance(ai, int)
                          and 0 <= ai < len(apis) else "")
                    alts.append((_PRIO.get(an, 5), i))
            for _, i in sorted(alts):
                add(i)
    except Exception:  # noqa: BLE001
        pass
    try:
        di, do = sd.default.device
        add(do if direction == "out" else di)
    except Exception:  # noqa: BLE001
        pass
    out.append(None)   # 最后交给 PortAudio 自己选默认
    return out
