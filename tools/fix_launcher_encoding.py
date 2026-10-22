# -*- coding: utf-8 -*-
"""把启动器脚本统一为 GBK + CRLF（wscript/cmd 在中文 Windows 的正统格式）。

背景：.vbs 由 wscript 按 ANSI(GBK) 解码、按 CRLF 断行；UTF-8 无 BOM + LF 的文件
会被错配成 GBK 对，把后续 ASCII 引号/换行一起吞掉，报"未结束的字符串常量"。
本脚本幂等：已转换（非 UTF-8）的文件按 GBK 读入后重写，不会重复转换。
"""
import io

TARGETS = [
    "启动小D助手.vbs",
    "启动网关服务.vbs",
    "停止网关服务.vbs",
    "启动网关服务.bat",
    "停止网关服务.bat",
]


def read_text(p):
    raw = io.open(p, "rb").read()
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise SystemExit(f"无法解码 {p}（既非 UTF-8 也非 GBK）")


ok = True
for p in TARGETS:
    text = read_text(p)
    text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    with io.open(p, "wb") as f:
        f.write(text.encode("gbk"))

    raw = io.open(p, "rb").read()
    try:
        raw.decode("gbk")
        gbk_ok = True
    except UnicodeDecodeError:
        gbk_ok = False
        ok = False
    lone_lf = raw.count(b"\n") - raw.count(b"\r\n")
    if lone_lf:
        ok = False
    print(f"{p:22} GBK={gbk_ok}  CRLF={raw.count(b'\r\n')}  loneLF={lone_lf}")

print("ALL_OK" if ok else "HAS_PROBLEM")
