# -*- coding: utf-8 -*-
"""TTS 可朗读化回归：LLM 回复里的符号/表情不再被念出来。

用户原话："播报的时候那个特殊符号和表情就不要再读出来了，不像真人聊天"。

跑法：
    C:/Python314/python.exe tests/test_tts_sanitize.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["PATH"] = (os.path.join(ROOT, "reference", "bin") + os.pathsep
                      + os.environ.get("PATH", ""))

PASS = 0
FAIL: list[str] = []


def ok(name: str, got, want) -> bool:
    global PASS
    cond = got == want
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name}\n       got ={got!r}\n       want={want!r}")
    return cond


from gateway.providers.tts_router import _speakable  # noqa: E402


def t_sanitize() -> None:
    print("=" * 72)
    print("1) Markdown 符号")
    ok("星号强调", _speakable("*重要*事项"), "重要事项")
    ok("粗体双星", _speakable("**注意**安全"), "注意安全")
    ok("井号标题", _speakable("# 今日新闻"), "今日新闻")
    ok("反引号代码", _speakable("运行`npm start`即可"), "运行npm start即可")
    ok("链接只留文字", _speakable("详情[点击这里](http://x.com/a)查看"),
       "详情点击这里查看")
    ok("波浪线删除线", _speakable("~~旧版~~新版"), "旧版新版")
    ok("竖线/尖括号", _speakable("A|B <tag>"), "AB tag")

    print("2) 列表符号")
    ok("行首短横", _speakable("- 第一条\n- 第二条"), "第一条\n第二条")
    ok("行首圆点", _speakable("• 要点一"), "要点一")
    ok("行内连字符保留", _speakable("打开 Wi-Fi 热点"), "打开 Wi-Fi 热点")
    ok("负数连字符保留", _speakable("气温 -5 度"), "气温 -5 度")

    print("3) 表情/符号")
    ok("emoji 表情", _speakable("好的😊马上办"), "好的马上办")
    ok("符号星星", _speakable("★ 优质 ★"), "优质")
    ok("箭头", _speakable("从 A → B"), "从 A B")
    ok("音符几何", _speakable("♪♪music●"), "music")
    ok("变体选择/零宽", _speakable("好\uFE0F\u200D\u200B吧"), "好吧")
    ok("私有区表情", _speakable("hi\ue000\ue001!"), "hi!")

    print("4) 不误伤")
    ok("纯中文", _speakable("你好，我是小D！"), "你好，我是小D！")
    ok("数字与小数", _speakable("气温 23.5℃，湿度 60%"), "气温 23.5℃，湿度 60%")
    ok("英文单词", _speakable("Hello World"), "Hello World")
    ok("人民币", _speakable("价格是 ¥99"), "价格是 ¥99")
    ok("中文书名号保留", _speakable("歌曲《晴天》"), "歌曲《晴天》")
    ok("空串安全", _speakable(""), "")


def t_router_end2end() -> None:
    """走一遍真实 Router 流：带符号的文本不应把符号送给合成引擎。"""
    print("=" * 72)
    print("5) 端到端：Router.stream 输入带符号文本可正常出包")
    import asyncio
    from gateway.providers.tts_router import TTSRouter
    from gateway.providers.tts_router import LOCAL_PREFIX
    from gateway.providers import tts_kokoro, tts_sapi

    engine = "local" if (tts_kokoro.available() or tts_sapi.list_voices()) \
        else "auto"

    async def _go():
        r = TTSRouter("zh-CN-XiaoxiaoNeural", 24000, 1440, engine=engine)
        chunks = []
        async for p in r.stream("**重要**通知：气温 23.5℃ 😊\n- 请注意"):
            chunks.append(p)
        return b"".join(chunks)

    pcm = asyncio.run(_go())
    ok("带符号文本合成出音频", len(pcm) > 800, True)


if __name__ == "__main__":
    t_sanitize()
    t_router_end2end()
    print("=" * 72)
    print(f"TTS_SANITIZE_{'PASS' if not FAIL else 'FAIL'} "
          f"{PASS}/{PASS + len(FAIL)}")
    sys.exit(1 if FAIL else 0)
