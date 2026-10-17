# -*- coding: utf-8 -*-
"""打包对账：把"延迟 import 必须写进 xiaod.spec"这条口头约定变成回归测试。

原理：AST 扫描 gateway/ 与 app/ 下所有 .py，找出**函数体内的 import**
（=延迟导入，静态分析最容易漏的那类），逐个核对它是否在 spec 的
hiddenimports 里声明过；同时检查有没有"既声明又排除"的矛盾。

跑法：python tests/test_packaging_imports.py
"""
from __future__ import annotations

import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SPEC = os.path.join(ROOT, "app", "build", "xiaod.spec")
SCAN_DIRS = ["gateway", "app"]

# 第三方可选依赖：允许"延迟导入 + 未打包 + 未装"（缺了功能降级，不影响主链路）
OPTIONAL_PREFIXES = ("paho", "openwakeword", "cv2", "amqtt")

PASS = 0
FAIL = 0


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def _module_of(node) -> str:
    if isinstance(node, ast.Import):
        return node.names[0].name if node.names else ""
    if isinstance(node, ast.ImportFrom):
        return node.module or ""
    return ""


def collect_lazy_imports(path: str) -> set:
    """函数体内的 import（延迟导入）→ 顶层模块名集合。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
    except (SyntaxError, OSError):
        return set()

    module_depth2 = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = _module_of(node)
                if mod:
                    module_depth2.add(mod)
    return module_depth2


def main():
    print("\n== 收集延迟导入 ==")
    lazy = set()
    for base in SCAN_DIRS:
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, base)):
            if "__pycache__" in dirpath:
                continue
            for fn in files:
                if fn.endswith(".py"):
                    lazy |= collect_lazy_imports(os.path.join(dirpath, fn))
    internal = {m for m in lazy if m.startswith(("gateway", "app"))}
    thirdparty = {m for m in lazy
                  if not m.startswith(("gateway", "app"))
                  and not m.startswith(".")}
    print(f"  延迟导入：内部 {len(internal)} 个，第三方 {len(thirdparty)} 个")
    ok(len(internal) > 0, "确实扫到了函数内 import")

    with open(SPEC, "r", encoding="utf-8") as f:
        spec_text = f.read()
    spec_consts = set(re.findall(r'["\']([A-Za-z0-9_.]+)["\']', spec_text))

    def declared(mod: str) -> bool:
        """spec 里是否声明了该模块（含其子模块/父包任一层）。"""
        if mod in spec_consts:
            return True
        return any(c.startswith(mod + ".") or mod.startswith(c + ".")
                   for c in spec_consts)

    print("\n== 内部模块必须在 spec 里声明 ==")
    missing = sorted(m for m in internal if not declared(m))
    ok(not missing,
       f"全部 {len(internal)} 个内部延迟导入都已写进 spec",
       f"缺失：{missing}")

    print("\n== 第三方依赖（跳过标准库；可选依赖允许缺失）==")
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    for m in sorted(thirdparty):
        top = m.split(".")[0]
        if top in stdlib:
            continue
        if top.startswith(OPTIONAL_PREFIXES):
            ok(True, f"{m} 属可选依赖（缺失自动降级）")
        else:
            ok(declared(m) or declared(top),
               f"第三方 {m} 已在 spec 声明")

    print("\n== 矛盾检查：不得既声明又排除 ==")
    excludes_block = ""
    mm = re.search(r"_EXCLUDES\s*=\s*\[(.*?)\]", spec_text, re.S)
    if mm:
        excludes_block = mm.group(1)
    excluded = set(re.findall(r'["\']([A-Za-z0-9_.]+)["\']', excludes_block))
    conflicts = sorted(m for m in internal if m in excluded)
    ok(not conflicts, "没有内部模块被 exclude", f"冲突：{conflicts}")

    print(f"\nPACK_PASS {PASS}/{PASS + FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
