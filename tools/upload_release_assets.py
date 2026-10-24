# -*- coding: utf-8 -*-
"""上传 Release 资产到 GitHub（curl 子进程，全程避免中文 argv 编码问题）。

要点：
- 文件名只以百分号编码形式出现在 URL 里（纯 ASCII），不经 argv 传中文；
- 文件内容经 stdin 管道给 curl（--data-binary @-），绕开 @路径 的编码链路；
- 用法：python tools/upload_release_assets.py <release_id> <资产文件...>
"""
import json
import subprocess
import sys
import urllib.parse

REPO = "haoeastspeed/XiaoDVoiceAss"


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    release_id = sys.argv[1]
    files = sys.argv[2:]
    base = (f"https://uploads.github.com/repos/{REPO}/releases/"
            f"{release_id}/assets")
    rc = 0
    for path in files:
        name = path.replace("\\", "/").split("/")[-1]
        url = f"{base}?name={urllib.parse.quote(name)}"
        with open(path, "rb") as f:
            proc = subprocess.run(
                ["curl", "-sS", "-m", "1800", "-X", "POST",
                 "-H", "Authorization: Bearer " + TOKEN,
                 "-H", "Accept: application/vnd.github+json",
                 "-H", "Content-Type: application/octet-stream",
                 "--data-binary", "@-", url],
                stdin=f, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out = proc.stdout.decode("utf-8", "replace")
        try:
            d = json.loads(out)
        except json.JSONDecodeError:
            print(f"FAIL {name}: 非 JSON 响应 {out[:200]}")
            rc = 1
            continue
        if "id" in d:
            print(f"OK   {d.get('name')}  {d.get('size')}B  "
                  f"state={d.get('state')}")
        else:
            print(f"FAIL {name}: {d.get('message')}")
            rc = 1
    return rc


if __name__ == "__main__":
    # token 从环境变量读，避免写进脚本/历史
    import os
    TOKEN = os.environ.get("GITHUB_TOKEN", "")
    if not TOKEN:
        print("请先设置环境变量 GITHUB_TOKEN")
        sys.exit(2)
    sys.exit(main())
