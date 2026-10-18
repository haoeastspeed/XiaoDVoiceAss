# -*- coding: utf-8 -*-
"""本轮全功能排查的回归测试。

覆盖：
  * 鉴权调用签名（WS 握手）与 HTTP 端点鉴权/本机限制
  * session 主循环异常兜底、非 dict 消息、opus 解码容错、休眠真停采集
  * 本地音色同步、方言映射大小写、EdgeTTS 可降级
  * 工具链：能力工具始终注册、熔断会话级、original 反查、endpoint 超时
  * UDP 序号回绕
  * 智能家居单候选兜底
  * 试听不再回退进程内合成（静态）

跑法：
    export PATH="$PWD/reference/bin:$PATH"
    C:/Python314/python.exe tests/test_fullfix.py
"""
from __future__ import annotations

import inspect
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["PATH"] = (os.path.join(ROOT, "reference", "bin") + os.pathsep
                      + os.environ.get("PATH", ""))

PASS = 0
FAIL: list[str] = []


def ok(name: str, cond, note: str = "") -> bool:
    global PASS
    if cond:
        PASS += 1
        print(f"  ok   {name}" + (f"  {note}" if note else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}" + (f"  {note}" if note else ""))
    return bool(cond)


def _src(rel: str) -> str:
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


# ------------------------------------------------ 1. 鉴权
def t_auth() -> None:
    print("=" * 72)
    print("1) 设备鉴权：WS 签名 / HTTP 端点")
    from gateway.auth import AuthManager, verify_ws_auth

    class Off:
        auth_enabled = False

    class On:
        auth_enabled = True
        auth_key = "test-key"
        allowed_devices: list = []

    ok("关闭鉴权直接放行", verify_ws_auth(Off(), "dev", "cli", "") is True)
    # 旧实现只传 3 个参数 → TypeError；这里必须能正常返回 bool
    try:
        r = verify_ws_auth(On(), "dev", "cli", "")
        raised = False
    except TypeError as e:  # noqa: BLE001
        r, raised = None, True
        print(f"       TypeError: {e}")
    ok("四参数调用不抛 TypeError", not raised and isinstance(r, bool))

    mgr = AuthManager("test-key")
    tok = mgr.generate_token("cli", "dev")
    ok("合法 token 通过", verify_ws_auth(On(), "dev", "cli", f"Bearer {tok}"))
    ok("非法 token 拒绝",
       not verify_ws_auth(On(), "dev", "cli", "Bearer bad.123"))

    # HTTP 视觉端点在开启鉴权、无 token 时必须 401
    from gateway.http.vision import VisionHandler
    h = VisionHandler(On())
    status, _, _ = h.handle_explain(
        b"", {"content-type": "multipart/form-data; boundary=x",
              "authorization": ""}, None)
    ok("视觉端点缺 token → 401", status == 401)

    # 试听端点：只允许本机
    from gateway.http.tts import TTSPreviewHandler
    th = TTSPreviewHandler(On())
    st_lan, _, _ = th.handle_preview(
        b"{}", {"x-peer-ip": "192.168.1.9", "content-type": "application/json"},
        None)
    ok("非本机试听 → 403", st_lan == 403)
    # 本机放行：voice 缺失应走到 400，而不是被 403 拦下
    st_local, _, _ = th.handle_preview(
        b"{}", {"x-peer-ip": "127.0.0.1", "content-type": "application/json"},
        None)
    ok("本机请求不被 403", st_local == 400)


# ------------------------------------------------ 2. session 健壮性
def t_session() -> None:
    print("=" * 72)
    print("2) session：异常兜底 / 消息校验 / 解码容错 / 休眠")
    s = _src("gateway/session.py")
    ok("主循环有通用异常兜底",
       "except Exception as exc:" in s and "traceback.print_exc()" in s)
    ok("非 dict JSON 被拦截", "if not isinstance(msg, dict):" in s)
    ok("上行二进制帧解析有 try", "上行二进制帧解析失败" in s)
    ok("上行 opus 解码有 try", "上行 opus 解码失败" in s)
    ok("休眠真正停采集",
       "self._collecting = False" in s and "真正停掉上行累积" in s)
    ok("控制消息分发保留", 'elif t == "listen":' in s)


# ------------------------------------------------ 3. TTS
def t_tts() -> None:
    print("=" * 72)
    print("3) TTS：本地音色同步 / 方言大小写 / 依赖降级")
    from gateway.providers.tts_router import (
        LocalFallbackTTS, _LOCAL_PREF_LOW, _NEURO_MAP_LOW)

    t = LocalFallbackTTS("zf_xiaoxiao")
    t.voice_id = "zf_xiaoyi"
    ok("外层 voice_id 更新", t.voice_id == "zf_xiaoyi")
    ok("Kokoro 内部同步", t._neuro.voice_id == "zf_xiaoyi")
    ok("SAPI 内部同步", t._sapi.voice_id == "zf_xiaoyi")

    ok("东北腔映射大小写无关",
       _NEURO_MAP_LOW.get("zh-cn-liaoning-xiaobeineural") == "zf_xiaobei")
    ok("陕西腔 pref 大小写无关",
       _LOCAL_PREF_LOW.get("zh-cn-shaanxi-xiaonineural") == ("zh-CN", "f"))

    from gateway.providers import tts_edge
    ok("tts_edge.available() 可调用", isinstance(tts_edge.available(), bool))
    ok("import_error() 可调用", isinstance(tts_edge.import_error(), str))
    # 依赖缺失时 router 直接走本地，不逐句试错
    r = _src("gateway/providers/tts_router.py")
    ok("want_local 感知在线依赖缺失",
       "if not tts_edge.available():" in r)


# ------------------------------------------------ 4. 工具链
def t_tools() -> None:
    print("=" * 72)
    print("4) 工具链：注册解耦 / 熔断会话级 / 反查 / 超时")
    from gateway.tools.loop import ToolCallLoop
    from gateway.tools.registry import ToolRegistry
    from gateway.tools.types import ToolDef

    s = _src("gateway/session.py")
    ok("能力工具与 tools_first 解耦",
       "if not self._tools_first() or getattr(self, \"_cap_tools_done\""
       not in s)

    class FakeLLM:
        supports_tools = True

    llm = FakeLLM()
    loop = ToolCallLoop(llm, ToolRegistry())
    loop._tools_disabled = True
    ok("会话级熔断关闭本会话工具", loop._use_tools() is False)
    ok("熔断不污染全局 LLM", llm.supports_tools is True)

    reg = ToolRegistry()

    async def _h(args):
        return None

    reg.register(ToolDef(name="fs_read_file", description="读文件",
                         input_schema={"type": "object"},
                         source="endpoint:x", handler=_h,
                         original="fs.read_file"))
    ok("sanitize 名可查", reg.get("fs_read_file") is not None)
    ok("原始名可反查", reg.get("fs.read_file") is not None)

    from gateway.tools.endpoint import StdioEndpoint
    sig = inspect.signature(StdioEndpoint._request)
    ok("_request 支持 timeout 参数", "timeout" in sig.parameters)


# ------------------------------------------------ 5. UDP 传输
def t_udp() -> None:
    print("=" * 72)
    print("5) UDP：序号回绕 / 重放")
    from gateway.transport.udp import UdpAudioChannel

    key, nonce = b"k" * 16, b"n" * 16
    ch = UdpAudioChannel(key, nonce)
    ch.remote_sequence = 0xFFFFFFFE
    ch.local_sequence = 0xFFFFFFFF
    pkt = ch.encrypt(b"\x01\x02\x03")      # 回绕后 seq=0
    try:
        plain, _flags, seq = ch.decrypt(pkt)
        wrapped_ok = (seq == 0 and plain == b"\x01\x02\x03")
    except Exception as e:  # noqa: BLE001
        wrapped_ok = False
        print(f"       回绕解密异常: {e!r}")
    ok("序号回绕后仍接受", wrapped_ok)

    ch2 = UdpAudioChannel(key, nonce)
    ch2.remote_sequence = 100
    ch2.local_sequence = 50
    pkt2 = ch2.encrypt(b"x")               # seq=51 <= 100 → 重放
    try:
        ch2.decrypt(pkt2)
        replay_rejected = False
    except ValueError:
        replay_rejected = True
    ok("重放包被拒绝", replay_rejected)


# ------------------------------------------------ 6. 智能家居
def t_ha() -> None:
    print("=" * 72)
    print("6) 智能家居：实体匹配兜底")
    from gateway.providers.homeassistant import HomeAssistant

    ha = HomeAssistant("http://127.0.0.1:8123", "token")
    one = [{"entity_id": "light.living",
            "attributes": {"friendly_name": "客厅灯"}}]
    ok("关键词命中", ha._match_entity(one, "客厅", "light") == "light.living")
    ok("单候选兜底（空关键词）",
       ha._match_entity(one, "", "light") == "light.living")
    two = one + [{"entity_id": "light.bed",
                  "attributes": {"friendly_name": "卧室灯"}}]
    ok("多候选不硬猜", ha._match_entity(two, "", "light") == "")
    ok("domain 过滤生效",
       ha._match_entity(two, "卧室", "light") == "light.bed")


# ------------------------------------------------ 7. 客户端 / 试听
def t_client() -> None:
    print("=" * 72)
    print("7) 客户端：试听不再进程内合成 / 统一反馈")
    a = _src("app/api.py")
    ok("试听不再回退本进程合成", "via = \"in-process\"" not in a)
    ok("试听前自动拉起网关", "试听前自动拉起网关" in a)
    ok("rescan_devices 有异常处理", "重新扫描设备失败" in a)
    ok("set_option 返回 restarting", '"restarting": restarting' in a)
    ok("导出取消不再静默保存", '"cancelled": True' in a)

    js = _src("app/ui/app.js")
    ok("前端有统一配置写入 applyOpt", "async function applyOpt(" in js)
    ok("bootstrap 失败会重试", "for (let i = 0; i < 5; i++)" in js)
    ok("清空对话有二次确认", "确定清空当前对话记录吗" in js)
    ok("推荐音色走 set_voice", "await api().set_voice(rec)" in js)


# ------------------------------------------------ 8. MQTT 编排
def t_mqtt() -> None:
    print("=" * 72)
    print("8) MQTT：hello 注入 / 会话复用 / paho 兼容")
    s = _src("gateway/server.py")
    ok("MQTT 会话表存在", "_mqtt_sessions" in s)
    ok("hello 被喂进会话", "transport.feed(text)" in s)
    ok("控制消息路由给会话",
       'if isinstance(sess, dict):\n                sess["transport"].feed'
       in s)
    ok("并发 hello 有占位防护", '"__pending__"' in s)
    m = _src("gateway/transport/mqtt.py")
    ok("paho 2.x 回调版本兼容", "CallbackAPIVersion" in m)
    ok("UDP 端口占用自动兜底", "改用系统分配" in m)
    ok("端点支持 reset()", "def reset(self):" in m)


def main() -> int:
    t_auth()
    t_session()
    t_tts()
    t_tools()
    t_udp()
    t_ha()
    t_client()
    t_mqtt()
    print("=" * 72)
    total = PASS + len(FAIL)
    if FAIL:
        print(f"FULLFIX_FAIL {len(FAIL)}/{total}  失败项={FAIL}")
        return 1
    print(f"FULLFIX_PASS {PASS}/{total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
