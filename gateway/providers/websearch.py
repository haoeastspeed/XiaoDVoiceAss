# -*- coding: utf-8 -*-
"""联网搜索（无需 API key）。

主用 **Bing**（cn.bing.com，国内可直连、HTML 结构稳定），失败再退到
DuckDuckGo（需要能访问外网时才有用）。任何异常都吞掉返回空列表 ——
联网搜索只是"锦上添花"，绝不能因为它让整轮对话报错或卡住。
"""
from __future__ import annotations

import html
import re

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_TAG = re.compile(r"<[^>]+>")

# Bing
_B_ITEM = re.compile(r'<li class="b_algo".*?(?=<li class="b_algo"|</ol>)', re.S)
_B_LINK = re.compile(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_B_SNIP = re.compile(r"<p[^>]*>(.*?)</p>", re.S)

# DuckDuckGo（html 端点 / lite 端点）
_D_RESULT = re.compile(
    r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_D_SNIPPET = re.compile(
    r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.S)
_D_LITE_LINK = re.compile(r'<a[^>]+class="result-link"[^>]*href="([^"]+)"'
                          r'[^>]*>(.*?)</a>', re.S)
_D_LITE_SNIP = re.compile(r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>', re.S)


def _text(s: str) -> str:
    return html.unescape(_TAG.sub("", s or "")).strip()


def _unwrap(url: str) -> str:
    """DuckDuckGo 会把真实地址包在 /l/?uddg=... 里。"""
    m = re.search(r"[?&]uddg=([^&]+)", url or "")
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1))
    return url


def _search_bing(query: str, max_results: int, timeout: float) -> list[dict]:
    import httpx
    headers = {"User-Agent": _UA,
               "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
               "Accept": "text/html,application/xhtml+xml"}
    out: list[dict] = []
    for host in ("https://cn.bing.com/search", "https://www.bing.com/search"):
        try:
            r = httpx.get(host, params={"q": query, "setlang": "zh-CN"},
                          headers=headers, timeout=timeout,
                          follow_redirects=True)
            if r.status_code != 200:
                continue
            for block in _B_ITEM.findall(r.text):
                m = _B_LINK.search(block)
                if not m:
                    continue
                url, title = m.group(1), _text(m.group(2))
                sn = _B_SNIP.search(block)
                out.append({"title": title,
                            "snippet": _text(sn.group(1)) if sn else "",
                            "url": html.unescape(url)})
                if len(out) >= max_results:
                    break
            if out:
                return out
        except Exception:  # noqa: BLE001
            continue
    return out


def _search_ddg(query: str, max_results: int, timeout: float) -> list[dict]:
    import httpx
    headers = {"User-Agent": _UA,
               "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    out: list[dict] = []
    try:
        r = httpx.post("https://html.duckduckgo.com/html/", data={"q": query},
                       headers=headers, timeout=timeout, follow_redirects=True)
        if r.status_code == 200:
            links = _D_RESULT.findall(r.text)
            snips = _D_SNIPPET.findall(r.text)
            for i, (url, title) in enumerate(links[:max_results]):
                out.append({"title": _text(title),
                            "snippet": _text(snips[i]) if i < len(snips) else "",
                            "url": _unwrap(html.unescape(url))})
    except Exception:  # noqa: BLE001
        out = []
    if not out:
        try:
            r = httpx.get("https://lite.duckduckgo.com/lite/",
                          params={"q": query}, headers=headers,
                          timeout=timeout, follow_redirects=True)
            if r.status_code == 200:
                links = _D_LITE_LINK.findall(r.text)
                snips = _D_LITE_SNIP.findall(r.text)
                for i, (url, title) in enumerate(links[:max_results]):
                    out.append({"title": _text(title),
                                "snippet": (_text(snips[i])
                                            if i < len(snips) else ""),
                                "url": _unwrap(html.unescape(url))})
        except Exception:  # noqa: BLE001
            pass
    return out


def search(query: str, max_results: int = 4, timeout: float = 8.0) -> list[dict]:
    """返回 [{'title','snippet','url'}]；失败/无结果返回 []。"""
    query = (query or "").strip()
    if not query:
        return []
    try:
        import httpx  # noqa: F401
    except Exception:  # noqa: BLE001
        return []
    for fn in (_search_bing, _search_ddg):
        try:
            items = fn(query, max_results, timeout)
        except Exception:  # noqa: BLE001
            items = []
        items = [x for x in items if x.get("title") or x.get("snippet")]
        if items:
            return items[:max_results]
    return []


def as_context(query: str, items: list[dict], max_chars: int = 1400) -> str:
    """把检索结果整理成给模型看的上下文。

    结尾的"回答规则"是关键：9B 级别的本地模型在资料不相关时**会编造**
    （实测：搜"北京今天天气"返回的是百科/政府网页，模型照样编出
    "10 到 19 度"）。必须把"查不到就明说、禁止编造"写死。"""
    lines = [f"【联网检索】关键词：{query}"]
    used = 0
    seen = set()
    for it in items:
        title = (it.get("title") or "").strip()
        snippet = (it.get("snippet") or "").strip()
        if title in seen:
            continue
        seen.add(title)
        piece = f"{len(seen)}. {title}：{snippet}"
        if used + len(piece) > max_chars:
            break
        used += len(piece)
        lines.append(piece)
    lines.append(
        "（以上是刚刚联网查到的资料。回答规则：一、资料里有的信息优先采用，"
        "用你自己的话简洁说，不要逐条照念；二、资料里没有或与问题无关时，"
        "必须明说\"这个我联网也没查到\"，禁止编造任何数字、时间、人名、"
        "价格；三、不要提\"资料\"\"检索\"这些词，就当自己刚查完。）")
    return "\n".join(lines)


# ---------- 天气快路径 ----------

# 命中这些词就认为是天气类问题：走 wttr.in 拿真实数据，
# 而不是拿 Bing 的百科/政府网页（实测那种结果模型只能靠编）。
_WEATHER_WORDS = ("天气", "气温", "温度", "几度", "下雨", "降雨", "降水",
                  "台风", "冷吗", "热吗", "防晒", "风力", "湿度", "雪吗",
                  "下雪", "晴吗", "阴天")
# 从关键词里剔掉这些再猜城市名
_WEATHER_STOP = ("今天", "明天", "后天", "现在", "实时", "当地", "本地",
                 "这边", "那里", "哪里", "外面", "天气", "气温", "温度",
                 "怎么样", "如何", "多少", "几度", "是什", "么", "了",
                 "吗", "呢", "啊啊", "嗯", "查", "看", "想", "知道", "请问",
                 "一下", "我", "你", "他", "她", "的", "现在", "当前")

# wttr.in 的英文天气描述 → 中文
_WTT_DESC = {
    "sunny": "晴", "clear": "晴", "partly cloudy": "多云",
    "cloudy": "阴", "overcast": "阴", "mist": "薄雾", "fog": "雾",
    "light rain": "小雨", "moderate rain": "中雨", "heavy rain": "大雨",
    "patchy rain nearby": "局部小雨", "patchy light rain": "零星小雨",
    "light drizzle": "毛毛雨", "thundery outbreaks": "雷阵雨",
    "light snow": "小雪", "moderate snow": "中雪", "heavy snow": "大雪",
    "haze": "霾", "smoke": "烟雾", "smoky haze": "霾",
    "sandstorm": "沙尘暴", "dust": "浮尘",
}


def _looks_weather(query: str) -> bool:
    q = query or ""
    return any(w in q for w in _WEATHER_WORDS)


def _guess_city(query: str) -> str:
    """从关键词里猜城市名：剔掉停用词后剩下的连续中文（2~8 字）。"""
    import re
    q = query or ""
    for w in sorted(_WEATHER_STOP, key=len, reverse=True):
        q = q.replace(w, " ")
    q = q.strip()
    m = re.search(r"[一-龥A-Za-z]{2,10}", q)
    return (m.group(0) if m else "").strip()


def _weather(query: str, timeout: float = 8.0) -> str:
    """天气类问题：wttr.in → 中文摘要（拿不到返回空串，交给普通搜索）。"""
    import json
    from urllib.parse import quote
    import httpx
    city = _guess_city(query)
    # 没猜到城市就让它按 IP 自动定位（可能不准，但比没有强）
    url = f"https://wttr.in/{quote(city)}" if city else "https://wttr.in/"
    try:
        r = httpx.get(url, params={"format": "j1"},
                      headers={"User-Agent": _UA,
                               "Accept-Language": "zh-CN,zh;q=0.9"},
                      timeout=timeout, follow_redirects=True)
        if r.status_code != 200:
            return ""
        d = r.json()
    except Exception:  # noqa: BLE001
        return ""
    try:
        cur = (d.get("current_condition") or [{}])[0]
        day = (d.get("weather") or [{}])[0]
        area = city or (((d.get("nearest_area") or [{}])[0].get("areaName")
                         or [{}])[0].get("value") or "当地")
        desc_en = ((cur.get("weatherDesc") or [{}])[0].get("value")
                   or "").strip().lower()
        desc = _WTT_DESC.get(desc_en, desc_en or "晴")
        chance = 0
        try:
            chance = max(int(h.get("chanceofrain") or 0)
                         for h in (day.get("hourly") or [])[:8])
        except Exception:  # noqa: BLE001
            chance = 0
        return (f"【实时天气】{area}：现在 {cur.get('temp_C', '?')}°C，"
                f"体感 {cur.get('FeelsLikeC', '?')}°C，{desc}，"
                f"湿度 {cur.get('humidity', '?')}%，"
                f"风 {cur.get('windspeedKmph', '?')}km/h；"
                f"今日 {day.get('mintempC', '?')}~{day.get('maxtempC', '?')}°C，"
                f"降水概率 {chance}%。"
                f"（这是刚查到的真实天气，直接说，不要改数字。）")
    except Exception:  # noqa: BLE001
        return ""


# 对外暴露的名字（session 用）
looks_weather = _looks_weather
weather = _weather
