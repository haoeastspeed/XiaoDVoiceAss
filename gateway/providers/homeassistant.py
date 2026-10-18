# -*- coding: utf-8 -*-
"""HomeAssistant 智能家居接入（REST API）。

小智的做法是在服务端用大模型识别用户意图、再调 HA 的 service。这里同思路，
但**默认关闭、且只在用户配置了 HA 地址和令牌时才启用**——没配就完全不介入，
不会给每句话都多一次模型调用。

流程：
  1. 一句话进来，先让 LLM 判断"这是不是一条家居控制指令"并抽出结构化意图
     （domain / service / entity 关键词 / 值）。这一步用 `quick()`，短、便宜。
  2. 拿 entity 关键词到 HA 的 `/api/states` 里做模糊匹配，找到真实 entity_id。
  3. 调 `/api/services/<domain>/<service>` 执行。
  4. 执行结果变成一句口语提示返回给用户（成功/失败都说清楚，不假装成功）。

HA 令牌是长期访问令牌（个人资料页最下方创建），走 `Authorization: Bearer`。
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple

import httpx

# 让 LLM 只回答 JSON，别寒暄。temperature 由调用方压到 0.1。
_INTENT_PROMPT = """判断用户这句话是不是一条智能家居控制指令。

只输出 JSON，不要任何其它文字：
如果是，输出：
{"control": true, "domain": "<HA domain，如 light/switch/climate/cover/media_player/fan>",
 "service": "<turn_on/turn_off/toggle/set_temperature/set_volume 等>",
 "entity": "<设备名关键词，如 客厅灯/卧室空调，没有就空字符串>",
 "value": <数值，没有就 null>,
 "reply": "<执行成功后你要对用户说的那句口语确认，10字以内>"}
如果不是家居控制，输出：
{"control": false}

用户说：{text}"""

# 常见 domain 的中文别名 → HA domain，给 LLM 抽错时兜一层
_DOMAIN_ALIAS = {
    "灯": "light", "灯光": "light", "照明": "light",
    "开关": "switch", "插座": "switch", "电源": "switch",
    "空调": "climate", "温度": "climate", "暖气": "climate",
    "窗帘": "cover", "卷帘": "cover", "车库": "cover",
    "风扇": "fan", "扇": "fan",
    "电视": "media_player", "音响": "media_player", "播放器": "media_player",
    "扫地机": "vacuum", "扫地机器人": "vacuum",
    "锁": "lock", "门锁": "lock",
}


def _extract_json(text: str) -> dict:
    """从 LLM 输出里抠出第一个 JSON 对象（模型常爱包 ```json  fences）。"""
    if not text:
        return {}
    # 带 thinking 的模型会把推理过程写进正文，其中可能夹着示例 JSON，
    # 直接抠会抠到错的那个 → 先剥掉思考块。
    t = re.sub(r"<think>.*?</think>|<\|think\|>.*?<\|end\|>", "", text,
               flags=re.S)
    t = t.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"\{[^{}]*\}", t, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return {}
    return {}


class HomeAssistant:
    def __init__(self, base_url: str, token: str, timeout: float = 6.0):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.token = (token or "").strip()
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    # ---------- 生命周期 ----------

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.token)

    def _cli(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout, trust_env=False)
        return self._client

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json"}

    async def aclose(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ---------- HA 能力 ----------

    async def check(self) -> Tuple[bool, str]:
        """探活：返回 (是否可用, 说明)。"""
        if not self.enabled:
            return False, "未配置 HomeAssistant"
        try:
            r = await self._cli().get(f"{self.base_url}/api/",
                                      headers=self._headers())
            if r.status_code == 200:
                return True, "HomeAssistant 已连接"
            return False, f"HomeAssistant 返回 {r.status_code}"
        except Exception as e:  # noqa: BLE001
            return False, f"连接失败：{str(e)[:60]}"

    async def states(self) -> List[dict]:
        if not self.enabled:
            return []
        try:
            r = await self._cli().get(f"{self.base_url}/api/states",
                                      headers=self._headers())
            if r.status_code != 200:
                return []
            return r.json() or []
        except Exception:  # noqa: BLE001
            return []

    def _match_entity(self, entities: List[dict], keyword: str,
                      domain: str = "") -> str:
        """设备名关键词 → entity_id。

        先按 friendly_name 包含关键词匹配，再退到 entity_id 本身；都匹配不上
        但**该 domain 下只有一个**设备时就用它——"把灯关了"这种话用户不会
        报全名，旧实现这里直接返回空，于是带空 entity_id 调服务被 HA 打 400。"""
        kw = (keyword or "").strip()
        pool = entities
        if domain:
            pool = [e for e in entities
                    if (e.get("entity_id") or "").startswith(domain + ".")]
        cands = []
        if kw:
            for e in pool:
                fn = ((e.get("attributes") or {}).get("friendly_name") or "")
                eid = e.get("entity_id") or ""
                if kw in fn or kw in eid:
                    cands.append((len(fn), eid))
        if not cands:
            if len(pool) == 1:
                return pool[0].get("entity_id") or ""
            return ""
        cands.sort()
        return cands[0][1]

    async def call_service(self, domain: str, service: str, entity_id: str,
                           value=None) -> Tuple[bool, str]:
        if not self.enabled:
            return False, "HomeAssistant 未配置"
        if not domain or not service:
            return False, "意图不完整（缺 domain/service）"
        url = f"{self.base_url}/api/services/{domain}/{service}"
        payload: Dict = {}
        if entity_id:
            payload["entity_id"] = entity_id
        if value is not None:
            # 不同 domain 的数值字段名不同，把常见的都塞上，HA 只认自己那个
            if isinstance(value, (int, float)):
                payload["temperature"] = value
                payload["volume_level"] = value / 100.0 \
                    if 0 < value <= 100 else value
                payload["position"] = int(value)
                payload["brightness_pct"] = int(value)
        try:
            r = await self._cli().post(url, headers=self._headers(),
                                       json=payload)
            if 200 <= r.status_code < 300:
                return True, ""
            return False, f"HA 返回 {r.status_code}: {r.text[:80]}"
        except Exception as e:  # noqa: BLE001
            return False, f"调用失败：{str(e)[:60]}"

    # ---------- 意图 → 执行 ----------

    @staticmethod
    def _fix_domain(domain: str, entity: str) -> str:
        d = (domain or "").strip().lower()
        if d:
            return d
        for k, v in _DOMAIN_ALIAS.items():
            if k in (entity or ""):
                return v
        return ""

    async def handle(self, text: str, llm=None) -> Optional[str]:
        """识别并执行一条家居指令。

        返回 None 表示"这不是家居指令"（调用方继续走普通对话）；
        返回字符串则是**已经执行完**的结果提示（调用方直接念给用户）。
        """
        if not self.enabled or not text:
            return None
        intent = await self._intent(text, llm)
        if not intent or not intent.get("control"):
            return None
        domain = self._fix_domain(intent.get("domain", ""),
                                  intent.get("entity", ""))
        service = (intent.get("service") or "").strip() or _guess_service(text)
        entity_kw = (intent.get("entity") or "").strip()
        if not domain or not service:
            return None          # 抽不出来就当普通对话，别瞎执行
        entity_id = ""
        ents = await self.states()
        if entity_kw:
            entity_id = self._match_entity(ents, entity_kw, domain)
            if not entity_id:
                return f"没找到名叫「{entity_kw}」的设备。"
        else:
            # 模型没给设备名（"把灯关了"）：该 domain 下只有一个设备就用它；
            # 多个则不带 entity_id，由 HA 对整类执行（light.turn_off 合法）。
            same = [e for e in ents
                    if (e.get("entity_id") or "").startswith(domain + ".")]
            if len(same) == 1:
                entity_id = same[0].get("entity_id") or ""
        ok, err = await self.call_service(domain, service, entity_id,
                                          intent.get("value"))
        if ok:
            reply = (intent.get("reply") or "").strip()
            return reply or "好的，已经执行了。"
        return f"控制失败：{err}"

    async def _intent(self, text: str, llm=None) -> dict:
        if llm is None or not hasattr(llm, "quick"):
            return {}
        prompt = _INTENT_PROMPT.replace("{text}", text[:120])
        try:
            out = await llm.quick(prompt, max_tokens=120, timeout=12.0)
        except Exception:  # noqa: BLE001
            return {}
        return _extract_json(out)


def _guess_service(text: str) -> str:
    """LLM 没给 service 时按关键词猜（关/开/切换）。"""
    t = text or ""
    if any(k in t for k in ("关", "闭", "停", "熄灭", "关掉")):
        return "turn_off"
    if any(k in t for k in ("开", "启", "打开", "亮")):
        return "turn_on"
    if "切换" in t or "toggle" in t.lower():
        return "toggle"
    return ""
