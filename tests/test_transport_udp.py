# -*- coding: utf-8 -*-
"""MQTT+UDP 传输回归：UDP 加解密字节布局 / 序号防重放 / 主题与凭据 / 队列传输。

UDP 部分完全按固件 `main/protocols/mqtt_protocol.cc` 的发送/接收逻辑构造向量，
双方必须能互相解开（这是"能不能跟真设备通话"的唯一硬判据）。

跑法：python tests/test_transport_udp.py
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from gateway.config import GatewayConfig                                 # noqa: E402
from gateway.transport.base import (QueueTransport, mqtt_client_id,      # noqa: E402
                                    safe_mac, subscribe_topic)
from gateway.transport.mqtt import (mqtt_credentials, parse_broker,      # noqa: E402
                                    UdpAudioEndpoint)
from gateway.transport.udp import (UdpAudioChannel, make_key,            # noqa: E402
                                   make_nonce, _aes_ctr)

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


def device_send(ch: UdpAudioChannel, opus: bytes, ts: int, seq: int) -> bytes:
    """复刻固件 SendAudio：以服务端 nonce 为模板，只覆写长度/时间戳/序号。"""
    block = bytearray(ch.nonce)
    block[0] = 0x01
    block[1] = 0x00                      # flags（固件默认 0）
    struct.pack_into(">H", block, 2, len(opus))
    struct.pack_into(">I", block, 8, ts)
    struct.pack_into(">I", block, 12, seq)
    head = bytes(block)
    return head + _aes_ctr(ch.key, head, opus)


def test_layout():
    print("\n== UDP 包字节布局 ==")
    key, nonce = make_key(), make_nonce()
    ok(len(key) == 16 and len(nonce) == 16, "key/nonce 各 16 字节")
    ok(nonce[0] == 0x01, "服务端 nonce[0] 固定 0x01（固件接收端会校验）")
    ch = UdpAudioChannel(key, nonce, logger=lambda m: None)
    pkt = ch.encrypt(b"\xaa" * 10, timestamp=777, flags=0)
    ok(pkt[0] == 0x01, "包 [0] = type 0x01")
    ok(struct.unpack_from(">H", pkt, 2)[0] == 10, "包 [2:4] = payload 长度")
    ok(struct.unpack_from(">I", pkt, 12)[0] == 1, "包 [12:16] = 序号（从 1 起）")
    ok(len(pkt) == 16 + 10, "总长 = 16 字节头 + 载荷")


def test_roundtrip():
    print("\n== 双向往返 ==")
    key, nonce = make_key(), make_nonce()
    server = UdpAudioChannel(key, nonce, logger=lambda m: None)
    opus = os.urandom(60)

    # 设备 → 服务端（固件侧构造）
    pkt = device_send(server, opus, ts=1234, seq=1)
    plain, flags, seq = server.decrypt(pkt)
    ok(plain == opus, "设备包被服务端正确解密")
    ok(seq == 1 and flags == 0, "flags/seq 解析正确")

    # 服务端 → 设备（服务端加密，用同一密钥/算法复算）
    out = server.encrypt(b"\xbb" * 8, timestamp=5)
    head, body = out[:16], out[16:]
    plain2 = _aes_ctr(key, head, body)
    ok(plain2 == b"\xbb" * 8, "服务端下行包设备可解")
    ok(head[0] == 0x01, "下行包 [0] 也是 0x01")


def test_replay():
    print("\n== 序号与防重放 ==")
    key, nonce = make_key(), make_nonce()
    server = UdpAudioChannel(key, nonce, logger=lambda m: None)
    o1 = os.urandom(10)
    p1 = device_send(server, o1, ts=1, seq=10)
    server.decrypt(p1)
    ok(True, "seq=10 正常接收")
    # 重放同一个包
    try:
        server.decrypt(p1)
        ok(False, "重放应被拒绝")
    except ValueError:
        ok(True, "重放（seq 相同）被拒")
    # 更旧的序号
    try:
        server.decrypt(device_send(server, b"x", ts=1, seq=5))
        ok(False, "旧序号应被拒绝")
    except ValueError:
        ok(True, "旧序号被拒")
    # 新的（允许跳号，仅告警）
    plain, _f, seq = server.decrypt(device_send(server, b"y", ts=2, seq=20))
    ok(seq == 20, "跳号包仍接收（仅告警）")

    # 错误类型
    try:
        server.decrypt(b"\x02" + bytes(15))
        ok(False, "type 非 0x01 应被拒")
    except ValueError:
        ok(True, "type 非 0x01 被拒")
    # 太短
    try:
        server.decrypt(b"\x01\x02")
        ok(False, "短包应被拒")
    except ValueError:
        ok(True, "过短包被拒")


def test_wrong_key():
    print("\n== 错误密钥 ==")
    nonce = make_nonce()
    server = UdpAudioChannel(make_key(), nonce, logger=lambda m: None)
    other = UdpAudioChannel(make_key(), nonce, logger=lambda m: None)
    pkt = device_send(other, b"secret", ts=1, seq=1)   # 用别的 key 加密
    plain, _f, _s = server.decrypt(pkt)
    ok(plain != b"secret", "不同密钥解出来不是明文（不会误信）")


def test_topics_creds():
    print("\n== 主题与凭据（对齐官方）==")
    cfg = GatewayConfig()
    cfg.mqtt_signature_key = "sk"
    ok(safe_mac("AA:BB:CC") == "AA_BB_CC", "mac 冒号 → 下划线")
    ok(subscribe_topic("AA:BB") == "devices/p2p/AA_BB", "订阅主题格式")
    cid = mqtt_client_id("bread-compact", "AA:BB")
    ok(cid == "GID_bread-compact@@@AA_BB@@@AA_BB", "client_id 格式", cid)
    cred = mqtt_credentials(cfg, "bread-compact", "AA:BB")
    ok(cred["publish_topic"] == "device-server", "发布主题 = device-server")
    import base64
    ok(base64.b64decode(cred["username"]).decode() == '{"ip": "unknown"}',
       "username = base64({\"ip\":\"unknown\"})")
    ok(len(cred["password"]) == 44, "password 为 base64(HMAC) 44 字符")
    ok(parse_broker("mqtt://1.2.3.4:1884") == ("1.2.3.4", 1884),
       "broker 端点解析")
    ok(parse_broker("host") == ("host", 1883), "缺端口默认 1883")


def test_queue_transport():
    print("\n== 队列传输（VoiceSession 复用底座）==")
    asyncio.run(_queue())


async def _queue():
    t = QueueTransport()
    t.feed('{"type":"hello"}')
    t.feed(b"\x01\x02")
    t.close_inbox()
    got = [x async for x in t]
    ok(got == ['{"type":"hello"}', b"\x01\x02"], "入站帧按序产出（str/bytes 混合）",
       got)
    ok(await t.send("x") is True and t.sent == ["x"], "出站 send 记录正确")


def test_udp_endpoint():
    print("\n== UDP 端点 hello 块 ==")
    asyncio.run(_udp_ep())


async def _udp_ep():
    cfg = GatewayConfig()
    cfg.udp_port = 0        # 让系统自选端口
    ep = UdpAudioEndpoint(cfg, logger=lambda m: None)
    await ep.start(asyncio.get_event_loop())
    blk = ep.hello_block("127.0.0.1")
    ok(len(bytes.fromhex(blk["key"])) == 16, "hello 块 key 16 字节 hex")
    ok(len(bytes.fromhex(blk["nonce"])) == 16, "hello 块 nonce 16 字节 hex")
    ok(bytes.fromhex(blk["nonce"])[0] == 0x01, "hello 块 nonce[0]=0x01")
    ok(isinstance(blk["port"], int), "hello 块带端口")
    ep.close()


if __name__ == "__main__":
    test_layout()
    test_roundtrip()
    test_replay()
    test_wrong_key()
    test_topics_creds()
    test_queue_transport()
    test_udp_endpoint()
    print(f"\nTRANSPORT_PASS {PASS}/{PASS + FAIL}")
    sys.exit(0 if FAIL == 0 else 1)
