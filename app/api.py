# -*- coding: utf-8 -*-
"""小D语音陪聊助手 —— 前端（pywebview）JS API。"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import threading
import time

from app import app_config as acfg
from app import devices, gateway_ctl

# 内置兜底：即使 Ollama 没起来也能显示
FALLBACK_MODELS = [
    ("qwen3.5:9b-q4km", "Qwen3.5 9B（4bit·快·推荐）"),
    ("qwen3.5:9b-q5km", "Qwen3.5 9B（5bit·更准）"),
    ("reasoning:8b", "Reasoning 8B（推理强）"),
    ("coder:7b", "Coder 7B（写代码）"),
    ("qwen2.5vl:7b", "Qwen2.5-VL 7B（多模态）"),
]
# 明显不是聊天模型的（向量/重排）不入列表
_EXCLUDE_HINT = ("bge", "embed", "rerank", "minilm", "nomic")

# ---- 语言模型后端 ----
# openai 兼容端点覆盖国内绝大多数在线大模型，所以只做两套实现。
LLM_BACKENDS = [
    {"id": "ollama", "name": "本地 Ollama（离线可用）"},
    {"id": "openai", "name": "在线 API（OpenAI 兼容）"},
]
# ---- 识别语言（SenseVoice 原生支持的五种 + 自动）----
ASR_LANGUAGES = [
    {"id": "auto", "name": "自动判断（默认）"},
    {"id": "zh", "name": "中文普通话"},
    {"id": "yue", "name": "粤语"},
    {"id": "en", "name": "英语"},
    {"id": "ja", "name": "日语"},
    {"id": "ko", "name": "韩语"},
]
# 指定语言 → 推荐音色（切换语言时界面直接推荐，不强制改用户选择）
LANG_VOICE_HINT = {
    "auto": "", "zh": "zh-CN-XiaoxiaoNeural",
    "yue": "zh-HK-HiuGaaiNeural", "en": "en-US-JennyNeural",
    "ja": "ja-JP-NanamiNeural", "ko": "ko-KR-SunHiNeural",
}
# ---- 记忆模式 ----
MEMORY_MODES = [
    {"id": "full", "name": "超长记忆（保留完整对话，默认）"},
    {"id": "summary", "name": "本地总结（早期对话压成摘要）"},
    {"id": "none", "name": "无记忆（每轮独立）"},
]


def log(msg: str) -> None:
    """写应用日志（排障用，不影响界面）。"""
    try:
        import datetime as _d
        with open(acfg.LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{_d.datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:  # noqa: BLE001
        pass


class Api:
    def __init__(self):
        self.cfg = acfg.load()
        self._win = None  # 下划线开头：pywebview 枚举 js_api 成员时会跳过，
        # 否则它会把整个 Window 对象序列化，刷一屏 native 递归报错
        self._client = None
        self._events: list[dict] = []
        self._transcript: list[dict] = []
        self._lock = threading.Lock()
        self._devices: dict = {}
        self._gw_msg = ""
        self._gw_busy = False
        # 当前正在做的网关动作："" / starting / stopping / restarting
        self._gw_action = ""
        # gateway_prepare() 是否已被调用。界面的 bootstrap 比后台初始化跑得
        # 早，这窗口期若直接报"未运行"，用户会看到 未运行→启动中 的闪跳；
        # 只要自动拉起还排在队里，就统一显示"启动中"。
        self._gw_prepare_started = False
        self._models_cache = None
        self._models_at = 0.0
        self._models_fetching = False
        # 网关健康/监听地址缓存（界面 3s 轮询，避免每次状态查询都打一次 HTTP）
        self._hw_at = 0.0
        self._hw_health: dict | None = None
        self._hw_addr = ""

    # ---------- 基础信息 ----------

    def _voice_list(self) -> list[dict]:
        """在线（EdgeTTS 神经音色）+ 离线（Windows 本地音色），带分组。"""
        try:
            from gateway.providers.tts_router import all_voices
            out = all_voices()
            if out:
                return out
        except Exception as e:  # noqa: BLE001
            log(f"枚举音色失败：{e!r}")
        try:
            from gateway.config import EDGE_VOICES
        except Exception:  # noqa: BLE001
            EDGE_VOICES = {}
        return [{"id": k, "name": v, "engine": "online",
                 "group": "在线 · 神经音色（需联网）"}
                for k, v in EDGE_VOICES.items()]

    def set_tts_engine(self, engine: str) -> dict:
        if engine not in ("auto", "online", "local"):
            return {"ok": False, "error": "engine 必须是 auto/online/local"}
        self.cfg["tts_engine"] = engine
        acfg.save(self.cfg)
        c = self._client
        if c is not None:
            try:
                c.set_tts_engine(engine)
            except Exception:  # noqa: BLE001
                pass
        log(f"TTS 引擎 -> {engine}")
        return {"ok": True}

    def _model_list(self, allow_fetch: bool = True) -> list[dict]:
        """从 Ollama 拉本地模型列表；失败则用兜底列表。

        启动时**不能**同步去请求 Ollama（最坏要等 2.5s 超时），否则界面会
        白等。这里改成：有缓存先用缓存立刻返回，后台线程去刷新。"""
        now = time.time()
        cached = getattr(self, "_models_cache", None)
        if cached and now - getattr(self, "_models_at", 0) < 600:
            return cached
        if cached and allow_fetch:
            self._refresh_models_async()
            return cached
        if not allow_fetch:
            return cached or [{"id": i, "name": n} for i, n in FALLBACK_MODELS]
        out = self._fetch_models()
        if out:
            self._models_cache, self._models_at = out, now
        return out or [{"id": i, "name": n} for i, n in FALLBACK_MODELS]

    def _refresh_models_async(self) -> None:
        if getattr(self, "_models_fetching", False):
            return
        self._models_fetching = True

        def _work():
            try:
                out = self._fetch_models()
                if out:
                    self._models_cache = out
                    self._models_at = time.time()
            finally:
                self._models_fetching = False
        threading.Thread(target=_work, daemon=True).start()

    def _fetch_models(self) -> list[dict]:
        out: list[dict] = []
        base = (self.cfg.get("ollama_base_url")
                or "http://localhost:11434").rstrip("/")
        try:
            import httpx
            r = httpx.get(f"{base}/api/tags", timeout=2.5, trust_env=False)
            for m in (r.json().get("models") or []):
                name = m.get("name") or ""
                if not name or any(h in name.lower() for h in _EXCLUDE_HINT):
                    continue
                size = float(m.get("size") or 0) / (1024 ** 3)
                out.append({"id": name,
                            "name": f"{name}（{size:.1f} GB）"})
        except Exception:  # noqa: BLE001
            # 拿不到模型列表，八成是 Ollama 没起来。别默默返回空列表——
            # 那在界面上表现为"下拉只有当前一项"，用户根本不知道要查什么。
            cur = self.cfg.get("model")
            return ([{"id": cur, "name": f"{cur}（当前）"}] if cur else []) + [
                {"id": "__no_ollama__",
                 "name": "未检测到模型服务（请先启动 Ollama）"}]
        # 把当前选中的放到最前面，避免它不在列表里时下拉为空
        cur = self.cfg.get("model")
        out.sort(key=lambda x: (x["id"] != cur, x["id"]))
        return out

    def _llm_presets(self) -> list[dict]:
        """在线后端预设（DeepSeek / 百炼 / 智谱 …）。

        从网关的 provider 里读，保证界面和网关用的是同一份；万一导入失败
        （打包裁剪等）就退回一份内置的最小列表。"""
        try:
            from gateway.providers.llm_openai import PRESETS
            return [dict(p) for p in PRESETS]
        except Exception:  # noqa: BLE001
            return [{"id": "deepseek", "name": "DeepSeek（深度求索）",
                     "base_url": "https://api.deepseek.com/v1",
                     "model": "deepseek-chat", "key_hint": "sk- 开头"},
                    {"id": "zhipu", "name": "智谱 ChatGLM（GLM-4）",
                     "base_url": "https://open.bigmodel.cn/api/paas/v4",
                     "model": "glm-4-flash", "key_hint": "控制台 API Key"}]

    def test_llm(self) -> dict:
        """测一下当前配置的语言模型端点通不通。

        在线后端要填 Key，用户很容易填错；与其等说话时才发现，不如给个
        "点一下就知道"的按钮。本地后端就查 Ollama 在不在。"""
        backend = str(self.cfg.get("llm_backend") or "ollama")
        if backend == "openai":
            base = str(self.cfg.get("llm_api_base") or "").strip()
            model = str(self.cfg.get("model") or "").strip()
            if not base:
                return {"ok": False, "msg": "还没填接口地址"}
            if not str(self.cfg.get("llm_api_key") or "").strip():
                return {"ok": False, "msg": "还没填 API Key"}
            try:
                import asyncio
                from gateway.providers.llm_openai import OpenAICompatLLM
                llm = OpenAICompatLLM(base, self.cfg.get("llm_api_key") or "",
                                      model)

                async def _go():
                    ms = await llm.list_models()
                    if ms:
                        return {"ok": True,
                                "msg": f"连接正常，服务端有 {len(ms)} 个模型"}
                    # /models 拿不到不代表不能用（有些网关不暴露这个接口），
                    # 真发一句最短的对话确认一次
                    try:
                        out = await llm.quick("说：好", max_tokens=8,
                                              timeout=20.0)
                    except Exception as e:  # noqa: BLE001
                        return {"ok": False, "msg": f"调用失败：{str(e)[:80]}"}
                    return ({"ok": True, "msg": f"调用正常（回复：{out[:20]}）"}
                            if out else {"ok": False, "msg": "没有返回内容"})

                return asyncio.run(_go())
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "msg": f"连接失败：{str(e)[:80]}"}
        # 本地 Ollama
        base = (self.cfg.get("ollama_base_url")
                or "http://localhost:11434").rstrip("/")
        try:
            import httpx
            r = httpx.get(f"{base}/api/tags", timeout=3.0,
                          trust_env=False)
            data = r.json().get("models") or []
            names = [m.get("name") for m in data]
            cur = str(self.cfg.get("model") or "")
            if cur and cur not in names:
                return {"ok": False,
                        "msg": f"Ollama 正常，但没有模型「{cur}」，"
                               f"请先 ollama pull {cur}"}
            return {"ok": True, "msg": f"Ollama 正常，本地已装 {len(names)} 个模型"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "msg": f"连不上 Ollama（{str(e)[:60]}），"
                                        f"请确认它已启动"}

    def ha_test(self) -> dict:
        """测 HomeAssistant 连通性（填错地址/令牌时给用户一个明确说法）。"""
        base = str(self.cfg.get("ha_base_url") or "").strip()
        token = str(self.cfg.get("ha_token") or "").strip()
        if not base or not token:
            return {"ok": False, "msg": "地址和访问令牌都要填"}
        try:
            import asyncio
            from gateway.providers.homeassistant import HomeAssistant
            ha = HomeAssistant(base, token)
            ok, msg = asyncio.run(ha.check())
            return {"ok": ok, "msg": msg}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "msg": f"失败：{str(e)[:70]}"}

    def bootstrap(self) -> dict:
        """首次加载/重开窗口时拉全量状态。"""
        log("bootstrap() 被调用")
        self._warm_neuro()      # 本地神经 TTS 后台预热（试听/降级别干等）
        try:
            self._devices = devices.query()
        except Exception as e:  # noqa: BLE001
            self._devices = {"inputs": [], "outputs": [], "error": str(e)}
        if self._devices.get("inputs"):
            self._sync_device_names()
        return {
            "ok": True,
            "version": acfg.VERSION,
            "devices": self._devices,
            "models": self._model_list(),
            "voices": self._voice_list(),
            "presets": [{"name": n, "voice": v}
                        for n, _p, v in acfg.PROMPT_PRESETS],
            "interrupt_modes": [{"id": i, "name": n}
                                for i, n in acfg.INTERRUPT_MODES],
            "tts_engines": [{"id": "auto", "name": "自动（在线优先，断网自动转本地）"},
                            {"id": "online", "name": "仅在线（神经音色，需联网）"},
                            {"id": "local", "name": "仅本地离线"}],
            "llm_backends": LLM_BACKENDS,
            "llm_presets": self._llm_presets(),
            "asr_languages": ASR_LANGUAGES,
            "memory_modes": MEMORY_MODES,
            "cfg": self.cfg,
            "running": self._running(),
            "gateway": self._gw_status(),
            "data_dir": acfg.data_dir(),
            "autostart": self.get_autostart(),
        }

    def _running(self) -> bool:
        c = self._client
        try:
            return bool(c and c.is_running())
        except Exception:  # noqa: BLE001
            return False

    def _models_msg(self, h: dict | None, up: bool) -> str:
        """离线模型的一句话状态（界面显示用）。"""
        if not up or not h:
            return ""
        err = h.get("error") or ""
        if err:
            return "离线模型加载失败：" + err
        if h.get("ready"):
            m = h.get("models") or {}
            names = []
            if m.get("asr"):
                names.append("SenseVoice 识别")
            if m.get("vad"):
                names.append("Silero 断句")
            if h.get("warm"):
                names.append("大模型已常驻")
            return "离线模型已加载" + ("（" + "、".join(names) + "）"
                                     if names else "")
        return "正在加载离线模型…"

    def _gw_status(self) -> dict:
        """网关状态。

        注意：**会话在跑就说明网关一定在跑**，这时不去探端口 —— 网关正在做
        ASR/LLM/TTS，事件循环可能几十毫秒到几秒不响应，探活很容易超时误报
        "未运行"（上一版就是这样把正常通话显示成红色的）。
        """
        port = int(self.cfg.get("gateway_port", 8000))
        live = self._running()
        up = live or gateway_ctl.port_up(port, timeout=1.2)
        # 外部实例（独立服务/手动启动）也要显示真实监听 PID
        pid = gateway_ctl.pid()
        if pid is None and up:
            pid = gateway_ctl.listener_pid(port)
        # 监听地址：127.0.0.1 时局域网设备（ESP32）连不上，界面要提醒
        addr, h = "", None
        if up:
            now = time.time()
            if now - self._hw_at > 1.5:      # 1.5s TTL，避免高频轮询打爆
                self._hw_addr = gateway_ctl.listen_addr(port)
                self._hw_health = gateway_ctl.health(port)
                self._hw_at = now
            addr, h = self._hw_addr, self._hw_health
        action = self._gw_action
        if up and live:
            kind = "通话中 · 服务正常"
        elif up and gateway_ctl.external():
            kind = "独立服务（外部实例）"
        elif up:
            kind = "已启动（本应用拉起）"
        elif not self._gw_prepare_started and \
                self.cfg.get("auto_start_gateway", True):
            # 界面先于后台初始化醒来：这时还没轮到自动拉起，别报"未运行"
            kind = "准备中"
            action = "starting"
        elif action == "starting":
            kind = "启动中"
        else:
            kind = "未运行"
        want_lan = str(self.cfg.get("gateway_host") or "0.0.0.0") \
            not in ("127.0.0.1", "localhost", "::1")
        return {"up": up, "port": port, "pid": pid, "kind": kind,
                "busy": self._gw_busy, "msg": self._gw_msg,
                "action": action,
                "listen_addr": addr,
                # 设备可达 = 网关在跑 且 监听的不是回环地址
                "lan_ok": bool(up and addr and not addr.startswith("127.")),
                "lan_wanted": want_lan,
                "models": (h or {}).get("models") or {},
                "models_ready": bool((h or {}).get("ready")),
                "models_msg": self._models_msg(h, up),
                **gateway_ctl.endpoints(port)}

    # ---------- 网关手动管理 ----------

    def gateway_status(self) -> dict:
        """供界面轮询的轻量状态接口。"""
        return {"ok": True, **self._gw_status()}

    def gateway_prepare(self) -> dict:
        """应用启动时后台准备网关：拉起（或自愈旧实例）+ 等离线模型加载完。

        与"是否自动开始通话"解耦——ESP32 设备随时要能连上，所以软件一开就
        把网关和离线模型备好（默认行为，可在设置里关）。
        """
        if self._gw_busy:
            return {"ok": True, "already": True, "msg": "正在启动中"}
        self._gw_prepare_started = True
        # 先置"启动中"再探测：_gw_status 里的端口探测最长要 1.2s，若先探测
        # 后置位，界面轮询会落进这个窗口，看到一瞬间的"未运行"（实测就是
        # 启动时 未运行→启动中 闪跳的第二个来源）。
        self._gw_busy = True
        self._gw_action = "starting"
        self._gw_msg = "正在启动语音网关…"
        st = self._gw_status()
        if st["up"] and st.get("lan_ok"):
            self._gw_busy = False
            self._gw_action = ""
            return {"ok": True, "already": True, "msg": "网关已在运行"}

        def _work():
            try:
                port = int(self.cfg.get("gateway_port", 8000))
                ok, msg = gateway_ctl.ensure(self.cfg)
                self._gw_msg = msg if ok else f"启动失败：{msg}"
                log(f"启动时自动拉起网关：ok={ok} {msg}")
                if not ok:
                    return
                # 等离线模型就绪（网关后台加载，通常几秒）
                self._gw_msg = "正在加载离线模型…"
                for _ in range(160):          # 最多约 80s
                    h = gateway_ctl.health(port, 1.2)
                    if h and (h.get("ready") or h.get("error")):
                        break
                    time.sleep(0.5)
                self._gw_msg = self._models_msg(h, True) or "网关已就绪"
                self._refresh_models_async()  # Ollama 可能刚被带起，重拉模型列表
                log(f"离线模型状态：{h.get('models') if h else None} "
                    f"error={(h or {}).get('error')}")
                if h is None:
                    self._gw_msg = "已复用旧版网关（无健康接口，建议点「重启网关」）"
            except Exception as e:  # noqa: BLE001
                self._gw_msg = f"启动失败：{e}"
                log(f"自动拉起网关异常：{e!r}")
            finally:
                self._gw_busy = False
                self._gw_action = ""

        threading.Thread(target=_work, daemon=True).start()
        return {"ok": True, "starting": True}

    def gateway_start(self) -> dict:
        if self._gw_status()["up"]:
            return {"ok": True, "already": True, "msg": "网关已在运行"}
        if self._gw_busy:
            return {"ok": False, "error": "正在启动中，请稍候…"}
        self._gw_busy = True
        self._gw_action = "starting"
        self._gw_msg = "正在启动网关…"

        def _work():
            try:
                ok, msg = gateway_ctl.ensure(self.cfg)
                self._gw_msg = msg if ok else f"启动失败：{msg}"
                log(f"手动启动网关：ok={ok} {msg}")
            except Exception as e:  # noqa: BLE001
                self._gw_msg = f"启动失败：{e}"
                log(f"手动启动网关异常：{e!r}")
            finally:
                self._gw_busy = False
                self._gw_action = ""
        self._models_cache = None
        self._models_at = 0.0
        self._models_fetching = False
        threading.Thread(target=_work, daemon=True).start()
        return {"ok": True, "starting": True}

    def gateway_stop(self) -> dict:
        if self._running():
            return {"ok": False, "error": "正在通话中，请先「结束聊天」再停止网关"}
        self._gw_busy = True
        self._gw_action = "stopping"
        self._gw_msg = "正在停止网关…"
        try:
            ok, msg = gateway_ctl.stop_any(
                int(self.cfg.get("gateway_port", 8000)))
            self._gw_msg = msg
            log(f"手动停止网关：ok={ok} {msg}")
            return {"ok": ok, "error": None if ok else msg, "msg": msg}
        except Exception as e:  # noqa: BLE001
            self._gw_msg = f"停止失败：{e}"
            return {"ok": False, "error": str(e)}
        finally:
            self._gw_busy = False
            self._gw_action = ""

    def gateway_restart(self) -> dict:
        if self._gw_busy:
            return {"ok": False, "error": "正在处理中，请稍候…"}
        had_call = self._running()
        self._gw_busy = True
        self._gw_action = "restarting"
        self._gw_msg = "正在重启网关…"

        def _work():
            try:
                if had_call:
                    self.stop_call()
                    for _ in range(50):
                        if not self._running():
                            break
                        time.sleep(0.1)
                gateway_ctl.stop_any(int(self.cfg.get("gateway_port", 8000)))
                time.sleep(0.4)
                ok, msg = gateway_ctl.ensure(self.cfg)
                self._gw_msg = msg if ok else f"重启失败：{msg}"
                log(f"重启网关：ok={ok} {msg}")
            except Exception as e:  # noqa: BLE001
                self._gw_msg = f"重启失败：{e}"
                log(f"重启网关异常：{e!r}")
            finally:
                self._gw_busy = False
                self._gw_action = ""
        self._models_cache = None
        self._models_at = 0.0
        self._models_fetching = False
        threading.Thread(target=_work, daemon=True).start()
        return {"ok": True, "starting": True}

    def copy_text(self, text: str) -> dict:
        """把文本放进系统剪贴板（UI 复制网关地址时的降级路径）。

        clip.exe 要 GBK 管道才不乱码：它按 ANSI 代码页读 stdin，喂 UTF-8
        会把 ws:// 之外的中文全变成乱符。
        """
        try:
            import subprocess
            p = subprocess.Popen(["clip"], stdin=subprocess.PIPE,
                                 creationflags=getattr(
                                     subprocess, "CREATE_NO_WINDOW", 0))
            p.communicate((text or "").encode("gbk", errors="replace"))
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def open_gateway_log(self) -> dict:
        path = os.path.join(acfg.data_dir(), "gateway.log")
        try:
            if not os.path.isfile(path):
                open(path, "a", encoding="utf-8").close()
            os.startfile(path)  # noqa: S606
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    # ---------- 事件 ----------

    def _on_event(self, ev: dict) -> None:
        t = ev.get("type")
        if t in ("error", "ready", "mic_warning", "closed") or \
                (t == "state" and ev.get("state") in ("starting", "listening")):
            log(f"[event] {t} {ev.get('error') or ev.get('state') or ''}")
        with self._lock:
            self._events.append(ev)
            self._transcript.append(ev)
            if len(self._events) > 400:
                del self._events[:-200]
            if len(self._transcript) > 4000:
                del self._transcript[:-2000]

    def poll(self) -> dict:
        with self._lock:
            evs = self._events
            self._events = []
        base = int(time.monotonic() * 1000)
        for i, e in enumerate(evs):
            e["seq"] = base + i
        return {"events": evs, "running": self._running()}

    # ---------- 通话控制 ----------

    def _sync_device_names(self) -> None:
        """把当前生效设备的真实名字回填到配置（设备换端口后仍能按名字找回）。"""
        ins = self._devices.get("inputs") or []
        outs = self._devices.get("outputs") or []
        ci, co = self.cfg.get("mic_index"), self.cfg.get("speaker_index")
        for it in ins:
            if it["index"] == ci and it["name"] != self.cfg.get("mic_name"):
                self.cfg["mic_name"] = it["name"]
        for it in outs:
            if it["index"] == co and it["name"] != self.cfg.get("speaker_name"):
                self.cfg["speaker_name"] = it["name"]

    def _ensure_devices(self) -> None:
        """设备列表按需加载（"打开即通话"会在前端 bootstrap 之前就调用）。"""
        if not self._devices:
            try:
                self._devices = devices.query()
                log(f"按需枚举设备：mic {len(self._devices.get('inputs') or [])} / "
                    f"spk {len(self._devices.get('outputs') or [])}")
            except Exception as e:  # noqa: BLE001
                self._devices = {}
                log(f"设备枚举失败：{e!r}")

    def start_call(self, mic_index=None, speaker_index=None) -> dict:
        log(f"start_call(mic={mic_index}, spk={speaker_index})")
        self._ensure_devices()
        if self._running():
            return {"ok": True, "already": True}
        old = self._client
        if old is not None:
            try:
                old.stop()
            except Exception:  # noqa: BLE001
                pass
            for _ in range(40):
                try:
                    if not old.is_running():
                        break
                except Exception:  # noqa: BLE001
                    break
                time.sleep(0.1)

        inv = self._devices.get("inputs") or []
        outv = self._devices.get("outputs") or []
        mic = devices.resolve(inv, mic_index if mic_index is not None
                              else self.cfg.get("mic_index"),
                              self.cfg.get("mic_name", ""),
                              self._devices.get("default_input"), "mic")
        spk = devices.resolve(outv, speaker_index if speaker_index is not None
                              else self.cfg.get("speaker_index"),
                              self.cfg.get("speaker_name", ""),
                              self._devices.get("default_output"), "spk")
        if mic is None or spk is None:
            log(f"设备解析失败 mic={mic} spk={spk}；"
                f"devices={self._devices}")
            return {"ok": False, "error": "没有找到可用的麦克风或扬声器"}
        log(f"设备已解析 mic={mic} spk={spk}")
        # 记住解析结果
        self.cfg["mic_index"] = mic
        self.cfg["mic_name"] = next((d["name"] for d in inv
                                     if d["index"] == mic), "")
        self.cfg["speaker_index"] = spk
        self.cfg["speaker_name"] = next((d["name"] for d in outv
                                         if d["index"] == spk), "")
        acfg.save(self.cfg)
        self._events = [{"type": "state", "state": "starting"}]
        self._transcript = []
        threading.Thread(target=self._start_worker, args=(mic, spk),
                         daemon=True).start()
        return {"ok": True}

    def _start_worker(self, mic, spk) -> None:
        port = int(self.cfg.get("gateway_port", 8000))
        self._gw_msg = "正在启动语音网关…"
        self._gw_action = "starting"      # 界面据此显示"正在启动网关"
        log("正在拉起/复用网关…")
        ok, msg = gateway_ctl.ensure(self.cfg)
        self._gw_action = ""
        log(f"网关结果 ok={ok} msg={msg}")
        self._gw_msg = msg
        if not ok:
            self._on_event({"type": "error", "error": msg})
            return
        try:
            from app.voice_client import RealtimeVoiceClient
        except Exception as e:  # noqa: BLE001
            log(f"语音模块加载失败：{e!r}")
            self._on_event({"type": "error", "error": f"语音模块加载失败：{e}"})
            return
        self._client = RealtimeVoiceClient(
            f"ws://127.0.0.1:{port}/xiaozhi/v1/",
            on_event=self._on_event,
            mic_index=mic, speaker_index=spk,
            echo_guard=True,
            allow_barge_in=(self.cfg.get("interrupt_mode") == "voice"),
            barge_factor=float(self.cfg.get("barge_factor", 1.8)),
            barge_frames=int(self.cfg.get("barge_frames", 6)),
            voice=self.cfg.get("voice"),
            tts_engine=self.cfg.get("tts_engine", "auto"),
            model=self.cfg.get("model"),
            prompt=self.cfg.get("prompt"),
            search=bool(self.cfg.get("web_search", False)),
            play_volume=float(self.cfg.get("play_volume", 1.0) or 1.0),
            wake_mode=str(self.cfg.get("wake_mode", "always") or "always"),
            wake_threshold=float(self.cfg.get("wake_threshold", 0.5) or 0.5),
            wake_models=(self.cfg.get("wake_models") or None),
            debug_record=False)
        log(f"语音客户端创建完成，开始连接 ws://127.0.0.1:{port}")
        self._client.start()
        log("客户端线程已启动")

    def stop_call(self) -> dict:
        c = self._client
        if c is not None:
            try:
                c.stop()
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=self._after_stop, daemon=True).start()
        return {"ok": True}

    def _after_stop(self) -> None:
        c = self._client
        for _ in range(40):
            try:
                if not (c and c.is_running()):
                    break
            except Exception:  # noqa: BLE001
                break
            time.sleep(0.1)
        if self.cfg.get("save_transcript", True):
            self._save_transcript()

    def interrupt(self) -> dict:
        c = self._client
        if c is not None:
            try:
                c.interrupt()
            except Exception:  # noqa: BLE001
                return {"ok": False}
        return {"ok": True}

    # ---------- 设置 ----------

    def set_model(self, model: str) -> dict:
        model = (model or "").strip()
        if not model:
            return {"ok": False, "error": "模型为空"}
        self.cfg["model"] = model
        acfg.save(self.cfg)
        c = self._client
        if c is not None:
            try:
                c.set_model(model)
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True}

    def set_voice(self, voice: str) -> dict:
        """切换音色：写配置 + 通知网关（下一句生效）。

        返回里带 engine/label，界面能告诉用户"这条音色实际走在线还是本地"——
        之前只说"已切换"，用户选了离线音色却听到在线声（或反之）时无从判断。"""
        voice = (voice or "").strip()
        self.cfg["voice"] = voice
        acfg.save(self.cfg)
        c = self._client
        if c is not None:
            try:
                c.set_voice(voice)
            except Exception:  # noqa: BLE001
                pass
        engine, label = "", ""
        try:
            from gateway.providers import tts_kokoro
            from gateway.providers.tts_router import LOCAL_PREFIX, _pick_local
            if voice.startswith(LOCAL_PREFIX):
                vid = voice[len(LOCAL_PREFIX):]
                if tts_kokoro.is_neuro_id(vid):
                    engine, label = "local", "本地神经"
                else:
                    engine, label = "local", "系统离线"
            else:
                engine = "online"
                neuro = _pick_local(voice)
                label = "在线神经（离线时自动换本地）" if neuro \
                    else "在线神经"
        except Exception:  # noqa: BLE001
            pass
        # 合成引擎跟随音色：选了离线音色就走本地；在线音色保持 auto
        #（联网在线合成，断网自动换本地）。界面上不再单独提供引擎选择。
        if engine:
            want = "local" if engine == "local" else "auto"
            if str(self.cfg.get("tts_engine") or "auto") != want:
                self.cfg["tts_engine"] = want
                acfg.save(self.cfg)
                log(f"TTS 引擎随音色 -> {want}")
            cc = self._client
            if cc is not None:
                try:
                    cc.set_tts_engine(want)
                except Exception:  # noqa: BLE001
                    pass
        log(f"音色 -> {voice}（{engine} {label}）")
        return {"ok": True, "engine": engine, "label": label}

    def set_interrupt(self, mode: str) -> dict:
        if mode not in ("button", "voice"):
            return {"ok": False, "error": "mode 必须是 button/voice"}
        self.cfg["interrupt_mode"] = mode
        acfg.save(self.cfg)
        c = self._client
        if c is not None:
            try:
                c.set_allow_barge_in(mode == "voice")
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True}

    def set_search(self, enabled: bool) -> dict:
        """开关联网搜索：写配置 + 通知网关（实时生效）。"""
        self.cfg["web_search"] = bool(enabled)
        acfg.save(self.cfg)
        c = self._client
        if c is not None:
            try:
                c.set_search(bool(enabled))
            except Exception:  # noqa: BLE001
                pass
        log(f"联网搜索 -> {bool(enabled)}")
        return {"ok": True}

    def set_prompt(self, name: str = "", text: str = "",
                   apply_voice: bool = True) -> dict:
        """设人设：给了 name 就从预设取，否则用自定义 text。

        apply_voice=True 时顺带套用该预设的推荐音色——人设和音色是配套的
        （"东北老铁"用台湾腔会很出戏）。返回里带上实际用了哪个音色，界面
        据此同步下拉，不用用户再手动切一次。"""
        text = (text or "").strip()
        if name and not text:
            for n, p, _v in acfg.PROMPT_PRESETS:
                if n == name:
                    text = p
                    break
        if not text:
            return {"ok": False, "error": "人设内容为空"}
        self.cfg["prompt_name"] = name or "自定义"
        self.cfg["prompt"] = text

        voice = ""
        if apply_voice and name:
            want = acfg.preset_voice(name)
            # 只在该人设确实配了推荐音色时才覆盖，避免把用户自选的弄丢
            if want and want != self.cfg.get("voice"):
                v = self.set_voice(want)
                if v.get("ok"):
                    voice = want
        acfg.save(self.cfg)
        c = self._client
        if c is not None:
            try:
                c.set_prompt(text)
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "voice": voice}

    def toggle_fullscreen(self) -> dict:
        """双击主界面空白处在全屏 / 还原之间切换。"""
        win = self._win
        if win is None:
            return {"ok": False, "error": "窗口未就绪"}
        try:
            if hasattr(win, "toggle_fullscreen"):
                win.toggle_fullscreen()
            else:  # 老版本 pywebview：手动置反 fullscreen 属性
                cur = bool(getattr(win, "fullscreen", False))
                win.fullscreen = not cur
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        # pywebview 没有 fullscreen 只读属性，返回码自己记账
        self._fullscreen = not getattr(self, "_fullscreen", False)
        return {"ok": True, "fullscreen": self._fullscreen}

    def set_option(self, key: str, value) -> dict:
        if key not in acfg.DEFAULTS:
            return {"ok": False, "error": f"未知选项 {key}"}
        old = self.cfg.get(key)
        self.cfg[key] = value
        acfg.save(self.cfg)
        if key == "always_on_top" and self._win is not None:
            try:
                self._win.on_top = bool(value)
            except Exception:  # noqa: BLE001
                pass
        restarting = False
        if key == "gateway_host" and str(old) != str(value):
            # 监听地址只在网关启动时生效 → 改了要重启。但**只在网关本来就跑着**
            # 时才重启：不能因为改个设置就把"没打算开网关"的机器启动起来，
            # 更不能在通话中无声无息把网关重启掉（会挂断正在进行的对话）。
            port = int(self.cfg.get("gateway_port", 8000) or 8000)
            if gateway_ctl.port_up(port):
                log(f"监听地址改为 {value}，重启网关生效")
                threading.Thread(target=self.gateway_restart,
                                 daemon=True).start()
                restarting = True
            else:
                log(f"监听地址改为 {value}（网关未运行，下次启动生效）")
        elif key in ("echo_gate", "echo_cancel", "echo_gate_cooldown_ms",
                     "echo_aec_delay_ms",
                     # 语言模型后端 / 识别语言 / 记忆模式 / 休眠 / 家居
                     # 都是网关启动时读入的配置 → 改了同样要重启才生效
                     "llm_backend", "llm_api_base", "llm_api_key",
                     "asr_language", "memory_mode", "memory_summary_model",
                     "idle_sleep_s", "ha_enabled", "ha_base_url",
                     "ha_token",
                     # MCP / 工具调用 / HTTP 端点 / 声纹 / 视觉 / 实时语音
                     # 同样是网关侧启动时读入的配置
                     "mcp_enabled", "mcp_servers", "tools_first",
                     "tool_loop_max_turns", "tool_ack_tts",
                     "http_port", "ota_enabled", "auth_enabled", "auth_key",
                     "voiceprint_enabled", "voiceprint_mode",
                     "voiceprint_base_url", "voiceprint_key",
                     "vision_enabled", "vision_camera_index", "vision_model",
                     "realtime_enabled", "realtime_base_url",
                     "realtime_api_key", "realtime_model"
                     ) and str(old) != str(value):
            # 回声门 / AEC 参数同样是网关启动时读入 → 改了要重启生效。
            # 但**只在网关本来就跑着**时才重启：不能因为用户改了个设置
            # 就把"没打算开网关"的机器给启动起来。
            port = int(self.cfg.get("gateway_port", 8000) or 8000)
            if gateway_ctl.port_up(port):
                log(f"{key} 改为 {value}，重启网关生效")
                threading.Thread(target=self.gateway_restart,
                                 daemon=True).start()
                restarting = True
            else:
                log(f"{key} 改为 {value}（网关未运行，下次启动生效）")
        if key == "play_volume":
            c = self._client
            if c is not None:
                try:
                    c.set_volume(value)
                except Exception:  # noqa: BLE001
                    pass
        # restarting 供界面提示"正在重启网关…"：这类配置要几十秒才生效，
        # 不提示的话用户只会觉得"点了没反应"。
        return {"ok": True, "restarting": restarting}

    def clear_history(self) -> dict:
        with self._lock:
            self._transcript = []
        c = self._client
        if c is not None:
            try:
                c.clear_history()
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True}

    def rescan_devices(self) -> dict:
        """重新扫描麦克风/扬声器：失败要把原因交还界面。

        旧实现 `devices.query()` 没有 try，扫描失败时异常变成未捕获的 Promise
        rejection，前端拿到 undefined 后「重新扫描」点了完全没反应。
        """
        try:
            self._devices = devices.query()
        except Exception as e:  # noqa: BLE001
            log(f"重新扫描设备失败：{e!r}")
            return {"ok": False, "error": f"扫描设备失败：{e}"}
        return {"ok": True, "devices": self._devices}

    # ---------- 记录 ----------

    def _render_text(self) -> str:
        out: list[str] = []
        bot: str | None = None

        def flush():
            nonlocal bot
            if bot is not None and bot.strip():
                out.append("小D：" + bot.strip())
            bot = None

        for ev in self._transcript:
            t = ev.get("type")
            if t == "user" and ev.get("text"):
                flush()
                out.append("我：" + str(ev["text"]).strip())
            elif t == "assistant_start":
                flush()
                bot = ""
            elif t == "assistant_delta":
                bot = (bot or "") + (ev.get("text") or "")
            elif t == "assistant_stop":
                flush()
        flush()
        return "\n".join(out)

    def _save_transcript(self) -> str | None:
        text = self._render_text()
        if not text.strip():
            return None
        try:
            name = _dt.datetime.now().strftime("对话_%Y%m%d_%H%M%S.txt")
            path = os.path.join(acfg.transcripts_dir(), name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            return path
        except Exception:  # noqa: BLE001
            return None

    def export_transcript(self) -> dict:
        text = self._render_text()
        if not text.strip():
            return {"ok": False, "error": "还没有对话内容"}
        win = self._win
        path = None
        try:
            if win is not None:
                path = win.create_file_dialog(
                    __import__("webview").SAVE_DIALOG,
                    save_filename=_dt.datetime.now().strftime(
                        "对话_%Y%m%d_%H%M%S.txt"))
        except Exception:  # noqa: BLE001
            path = None
        if isinstance(path, (list, tuple)):
            path = path[0] if path else None
        if not path:
            # 用户点了「取消」→ 不再静默保存。旧实现这里会偷偷存一份到
            # transcripts/ 再提示"已导出"，等于取消了也存了文件。
            return {"ok": False, "error": "已取消导出", "cancelled": True}
        else:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"保存失败：{e}"}
        return {"ok": True, "path": path or ""}

    # ---------- 退出 ----------

    def quit_app(self) -> dict:
        def _work():
            self.stop_call()
            time.sleep(0.3)
            gateway_ctl.stop()
            time.sleep(0.2)
            if self._win is not None:
                try:
                    self._win.destroy()
                except Exception:  # noqa: BLE001
                    pass
            os._exit(0)
        threading.Thread(target=_work, daemon=True).start()
        return {"ok": True}

    def _preview_text(self, voice: str) -> str:
        """试听语句跟音色语言走：英语音色听中文样本判断不了音色。"""
        try:
            from gateway.providers import tts_kokoro
            from gateway.providers.tts_router import LOCAL_PREFIX
            low = voice.lower()
            vid = voice[len(LOCAL_PREFIX):] if \
                voice.startswith(LOCAL_PREFIX) else voice
            if low.startswith("en-") or tts_kokoro.is_neuro_id(vid) and \
                    vid[:3] in ("af_", "am_", "bf_", "bm_"):
                return ("Hello, I am XiaoD, your local voice assistant. "
                        "How is your day going so far?")
        except Exception:  # noqa: BLE001
            pass
        return "你好，我是小D，很高兴认识你。今天过得怎么样？"

    def _preview_via_gateway(self, voice: str, text: str) -> tuple:
        """让网关进程合成试听，返回 (pcm16, engine)。

        GUI 进程里跑 sherpa/SAPI 原生栈在打包环境会崩掉整个程序（实测
        "离线音色试听无声 + 闪退"），而网关进程里同一套合成一直稳定，
        所以合成一律先走网关 HTTP，失败才回退本进程。"""
        import io
        import wave
        import httpx
        port = int(self.cfg.get("http_port", 8003) or 8003)
        r = httpx.post(f"http://127.0.0.1:{port}/tts/preview/",
                       json={"voice": voice, "text": text},
                       timeout=60.0, trust_env=False)
        if r.status_code != 200:
            try:
                msg = r.json().get("message") or ""
            except Exception:  # noqa: BLE001
                msg = ""
            raise RuntimeError(msg or f"网关试听端点返回 {r.status_code}")
        with wave.open(io.BytesIO(r.content), "rb") as w:
            rate = w.getframerate()
            pcm = w.readframes(w.getnframes())
        if rate != 24000 and pcm:
            import numpy as np
            x = np.frombuffer(pcm, dtype=np.int16)
            n = int(round(x.size * 24000 / rate))
            idx = np.linspace(0.0, x.size - 1.0, n)
            i0 = np.floor(idx).astype(np.int64)
            i1 = np.minimum(i0 + 1, x.size - 1)
            frac = (idx - i0).astype(np.float32)
            y = (x[i0].astype(np.float32) * (1 - frac)
                 + x[i1].astype(np.float32) * frac)
            pcm = np.clip(y, -32768, 32767).astype(np.int16).tobytes()
        return pcm, (r.headers.get("x-engine") or "")

    def preview_voice(self) -> dict:
        """试听当前音色：合成（优先网关进程）+ 本机播放（不经过通话链路）。

        几个曾经的坑，都修在这里：
        1. **播放设备必须走回退链**（app.devices.candidates）：界面里选中的
           端点未必打得开 24kHz（WDM-KS 直接失败 / WASAPI 拒绝非原生采样率），
           通话链路有回退所以能出声，试听此前直接拿选中索引开流 → 用户看到的
           就是"试听失败：Invalid device"。
        2. **合成不在本进程做**：GUI 进程加载 sherpa/SAPI 原生栈在打包环境
           会整进程崩溃（无声 + 闪退）；合成放网关进程，GUI 只负责播放。
        3. 网关没起会自动拉起再试听；合成失败只给明确提示，**绝不回退本进程**。"""
        # 通话中别抢：它会占用同一条扬声器流，混着放既听不清也可能报错
        if self._running():
            return {"ok": False, "error": "通话中不能试听，请先结束聊天"}
        try:
            import numpy as np
            import sounddevice as sd
            self._ensure_devices()
            voice = self.cfg.get("voice") or ""
            text = self._preview_text(voice)
            # ---- 合成：只在网关进程做，绝不回退本进程 ----
            # 旧实现在网关没起时回退"本进程合成"，而 GUI 进程加载 sherpa/SAPI
            # 原生栈会整进程崩溃（实测"离线音色试听无声 + 闪退"）。这里宁可
            # 自动把网关带起来，也不在本进程加载合成栈。
            port = int(self.cfg.get("gateway_port", 8000) or 8000)
            if not gateway_ctl.port_up(port):
                ok, msg = gateway_ctl.ensure(self.cfg)
                log(f"试听前自动拉起网关：ok={ok} {msg}")
                if ok:
                    for _ in range(60):       # 最多约 30s 等网关就绪
                        if gateway_ctl.port_up(port, timeout=0.6):
                            break
                        time.sleep(0.5)
            try:
                pcm, engine_tag = self._preview_via_gateway(voice, text)
            except Exception as e:  # noqa: BLE001
                log(f"网关试听失败：{e!r}")
                return {"ok": False,
                        "error": "网关没有回应试听请求，请点「重启网关」后重试"}
            if not pcm:
                return {"ok": False,
                        "error": "网关没能合成出试听音频，请打开网关日志查看原因"}
            x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            used = ""
            dev = devices.resolve(
                self._devices.get("outputs") or [],
                self.cfg.get("speaker_index"), self.cfg.get("speaker_name", ""),
                self._devices.get("default_output"), "spk")
            last_err = None
            for cand in devices.candidates(dev, "out"):
                try:
                    sd.play(x, 24000, device=cand)
                    sd.wait()
                    used = self._dev_label(cand)
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = e
            if not used:
                # 兜底：所有端点都拒绝 24k（纯 WASAPI 环境）→
                # 重采样到系统默认采样率再用默认设备试一次
                try:
                    do = sd.default.device
                    idx = do[1] if isinstance(do, (list, tuple)) else None
                    native = 48000
                    try:
                        native = int(sd.query_devices(idx)["default_samplerate"])
                    except Exception:  # noqa: BLE001
                        pass
                    if native != 24000:
                        n = int(len(x) * native / 24000)
                        x2 = np.interp(np.arange(n) * 24000.0 / native,
                                       np.arange(len(x)), x).astype(np.float32)
                        sd.play(x2, native, device=None)
                        sd.wait()
                        used = self._dev_label(None) + "（48k 重采样）"
                except Exception as e:  # noqa: BLE001
                    last_err = last_err or e
            if not used:
                return {"ok": False, "error": f"播放失败：{last_err}"}
            engine_final = "local" if engine_tag == "local" else "online"
            log(f"试听完成：voice={voice} engine={engine_tag} "
                f"via=gateway device={used}")
            return {"ok": True,
                    "engine": engine_final,
                    "device": used,
                    "voice": voice}
        except Exception as e:  # noqa: BLE001
            log(f"试听失败：{e!r}")
            return {"ok": False, "error": str(e)}

    def _dev_label(self, idx) -> str:
        try:
            import sounddevice as sd
            info = sd.query_devices(idx) if idx is not None \
                else sd.query_devices(kind="output")
            return (info or {}).get("name", "") or f"device={idx}"
        except Exception:  # noqa: BLE001
            return f"device={idx}"

    def _warm_neuro(self) -> None:
        """后台预热本地神经合成模型（首句加载要几秒，别让试听/降级干等）。"""

        def _work():
            try:
                from gateway.providers import tts_kokoro
                tts_kokoro.available()
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=_work, daemon=True).start()

    def open_transcripts(self) -> dict:
        try:
            os.startfile(acfg.transcripts_dir())  # noqa: S606
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def open_data_dir(self) -> dict:
        try:
            os.startfile(acfg.data_dir())  # noqa: S606
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    # ---------- 开机自启 ----------

    _RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    _RUN_NAME = "XiaodVoiceAssistant"

    def _autostart_target(self) -> str:
        exe = os.path.abspath(sys.executable)
        if getattr(sys, "frozen", False):
            return f'"{exe}"'
        return (f'"{exe}" '
                f'"{os.path.join(os.path.dirname(acfg.__file__), "main.py")}"')

    def get_autostart(self) -> bool:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._RUN_KEY) as k:
                v, _ = winreg.QueryValueEx(k, self._RUN_NAME)
                return bool(v)
        except Exception:  # noqa: BLE001
            return False

    def set_autostart(self, enabled: bool) -> dict:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                if enabled:
                    winreg.SetValueEx(k, self._RUN_NAME, 0, winreg.REG_SZ,
                                      self._autostart_target())
                else:
                    try:
                        winreg.DeleteValue(k, self._RUN_NAME)
                    except FileNotFoundError:
                        pass
        except Exception as e:  # noqa: BLE001
            log(f"设置开机自启失败：{e!r}")
            return {"ok": False, "error": str(e)}
        log(f"开机自启 -> {enabled}")
        return {"ok": True}

    def quit_app_soft(self) -> dict:
        """最小化到后台？这里直接退出（保持行为简单可预期）。"""
        return self.quit_app()
