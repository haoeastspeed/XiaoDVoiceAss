# -*- coding: utf-8 -*-
"""回声门配置链路回归：界面默认值 → gateway.json → 网关的 GatewayConfig。

这条链路以前断过（配置项写不进 gateway.json，界面改了没反应），所以单独
用一条用例钉住。同时做一次 HTML/JS 的静态一致性检查（控件 id 与后端键名）。

跑法：C:\\Python314\\python.exe tests/test_echo_config.py
用临时数据目录隔离，不碰用户真实配置。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_KEYS = ("echo_gate", "echo_gate_cooldown_ms", "echo_cancel", "echo_aec_delay_ms")
_ok = 0
_bad: list[str] = []


def check(name: str, cond: bool, extra: str = ""):
    global _ok
    if cond:
        _ok += 1
        print("  ok   " + name + (f"  [{extra}]" if extra else ""))
    else:
        _bad.append(name)
        print("  FAIL " + name + (f"  [{extra}]" if extra else ""))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="echocfg_")
    os.environ["XIAOD_DATA_DIR"] = tmp       # 必须在 import 前设：ROOT 由它决定
    sys.path.insert(0, REPO)

    from app import app_config as acfg            # noqa: PLC0415
    from app import gateway_ctl                   # noqa: PLC0415
    from gateway.config import load_config         # noqa: PLC0415

    print("1. DEFAULTS 里必须有回声门字段")
    for k in _KEYS:
        check(f"DEFAULTS 含 {k}", k in acfg.DEFAULTS, str(acfg.DEFAULTS.get(k)))

    print("2. 默认值写进 gateway.json 且网关能读到")
    path = gateway_ctl.write_gateway_config(dict(acfg.DEFAULTS))
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    for k in _KEYS:
        check(f"gateway.json 含 {k}", k in raw, str(raw.get(k)))
    cfg = load_config(path)
    for k in _KEYS:
        check(f"GatewayConfig.{k} 生效",
              getattr(cfg, k) == acfg.DEFAULTS[k], f"{getattr(cfg, k)!r}")

    print("3. 非默认值同样贯通（模拟用户在配置/界面里改了）")
    custom = dict(acfg.DEFAULTS)
    custom.update({"echo_gate_cooldown_ms": 1200, "echo_cancel": "off",
                   "echo_aec_delay_ms": 260, "echo_gate": False})
    cfg2 = load_config(gateway_ctl.write_gateway_config(custom))
    check("冷却窗改为 1200", cfg2.echo_gate_cooldown_ms == 1200,
          str(cfg2.echo_gate_cooldown_ms))
    check("AEC 改为 off", cfg2.echo_cancel == "off", cfg2.echo_cancel)
    check("延迟初值改为 260", cfg2.echo_aec_delay_ms == 260,
          str(cfg2.echo_aec_delay_ms))
    check("总开关可关", cfg2.echo_gate is False, str(cfg2.echo_gate))

    print("4. 界面控件与后端键名一致（静态检查）")
    with open(os.path.join(REPO, "app", "ui", "index.html"),
              encoding="utf-8") as f:
        html = f.read()
    with open(os.path.join(REPO, "app", "ui", "app.js"), encoding="utf-8") as f:
        js = f.read()
    for ident in ("echoCancelSel", "echoCoolSel"):
        check(f"index.html 有 #{ident}", f'id="{ident}"' in html)
        check(f"app.js 引用了 {ident}", ident in js)
    check("app.js 写 echo_cancel", '"echo_cancel"' in js)
    check("app.js 写 echo_gate_cooldown_ms", '"echo_gate_cooldown_ms"' in js)
    check("app.js 回填 echoCancelSel", "echoCancelSel" in js)

    print("")
    if _bad:
        print(f"ECHOCFG_FAIL {len(_bad)}/{_ok + len(_bad)}")
        for b in _bad:
            print("   - " + b)
        return 1
    print(f"ECHOCFG_PASS {_ok}/{_ok}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
