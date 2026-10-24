# -*- coding: utf-8 -*-
"""打包 Release 资产：onedir 极速版 zip + 固件镜像 zip。"""
import os
import zipfile

STAGE = "/tmp/xiaod_release"
os.makedirs(STAGE, exist_ok=True)


def zip_dir(src, dst, skip_exts=()):
    n = 0
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, _dirs, files in os.walk(src):
            for f in files:
                if f.lower().endswith(skip_exts):
                    continue
                p = os.path.join(root, f)
                z.write(p, os.path.relpath(p, src))
                n += 1
    return n


# 1) 极速版（onedir）
n1 = zip_dir(r"D:/projects/xiaod-voice/dist/小D智能助手",
             os.path.join(STAGE, "小D智能助手-v1.0.0-极速版.zip"))
print("onedir files:", n1)

# 2) 固件镜像（bin + 烧录脚本，排除日志）
n2 = zip_dir(r"D:/projects/xiaod-voice/firmware_dist",
             os.path.join(STAGE, "firmware-v1.0.0.zip"),
             skip_exts=(".log",))
print("firmware files:", n2)

for f in sorted(os.listdir(STAGE)):
    p = os.path.join(STAGE, f)
    print(f"{f}  {os.path.getsize(p)/1048576:.1f} MB")
print("ZIPS_DONE")
