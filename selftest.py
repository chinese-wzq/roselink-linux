#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RoseLink 协议与 reader/GUI 修复离线自测（无需蓝牙设备）。

覆盖解码器、编码器、帧构造、校验和、参数校验，以及 fake socket 下的
JSON、连接生命周期、GUI 写入、坐标换算，以及 CLI 异常边界与扫描线程
生命周期回归。
"""
from __future__ import annotations

import contextlib
import errno
import io
import json
import sys
import threading
import time
from types import SimpleNamespace

import rs_protocol as proto
import rs_writer as wproto
import roselink_reader as reader
import roselink_writer as writer


def _check(ok, fail, name, cond):
    if cond:
        ok[0] += 1
        print("  ✅ %s" % name)
    else:
        fail[0] += 1
        print("  ❌ %s" % name)


def _seq_factory(start=2):
    state = {"seq": start}
    def gen():
        s = state["seq"]
        state["seq"] = (state["seq"] + 1) & 0xFF
        if state["seq"] == 0:
            state["seq"] = 2
        return s
    return gen


# ── 只读协议解码 ─────────────────────────────────────────────────────

def _test_reader():
    ok, fail = [0], [0]

    print("\n=== 解码器自测（rs_protocol）===")

    # 1. 校验和公式 (dd 格式): 01 fe seq=04 → e0
    _check(ok, fail, "checksum_dd(01 fe, seq=04) == 0xe0",
           proto.checksum_dd(0x04, 0x01, bytes([0xfe])) == 0xe0)

    # 2. 真实 ACK 帧 dd 04 01 fe e0 aa
    ack = bytes([0xdd, 0x04, 0x01, 0xfe, 0xe0, 0xaa])
    _check(ok, fail, "ACK 帧校验通过", proto.verify_response_checksum(ack))
    _check(ok, fail, "ACK 帧解码 kind==ACK", proto.decode_frame(ack)["kind"] == "ACK")

    # 3. 电量 04 0c e4 e4 61 → 左100充电/右100充电/仓97
    bat = proto.decode_battery(bytes([0xe4, 0xe4, 0x61]))
    _check(ok, fail, "电量 e4 → 100% 充电中",
           bat["left_pct"] == 100 and bat["left_charging"])
    _check(ok, fail, "电量 61 → 仓 97%", bat["case_pct"] == 97)

    # 4. 固件 04 0d 01 05 05 → v155
    _check(ok, fail, "固件 01 05 05 → v155",
           proto.decode_firmware(bytes([0x01, 0x05, 0x05])) == "v155")

    # 5. 自定义 EQ 06 05 04 03 02 01 00 81 82 83
    gains = proto.decode_custom_eq(
        bytes([0x06, 0x05, 0x04, 0x03, 0x02, 0x01, 0x00, 0x81, 0x82, 0x83]))
    _check(ok, fail, "自定义 EQ signed-magnitude 解码",
           gains == [6, 5, 4, 3, 2, 1, 0, -1, -2, -3])

    # 6. 完整 init (type 15) 帧
    init_data = bytes([
        0x01, 0x01, 0x08, 0x02, 0x01, 0x03, 0x01, 0x04, 0x01, 0x05, 0x00,
        0x11, 0x01, 0x12, 0x01, 0x13, 0x01, 0x14, 0x01, 0x15, 0x00,
        0x02, 0x07, 0x00, 0x02, 0x08, 0x01, 0x02, 0x09, 0x03,
        0x04, 0x0c, 0xe4, 0xe4, 0x61, 0x04, 0x0d, 0x01, 0x05, 0x04,
        0x02, 0x0e, 0x00, 0x02, 0x12, 0x01, 0x02, 0x2a, 0x04,
        0x02, 0x2b, 0x00, 0x02, 0x2c, 0x01, 0x02, 0x2d, 0x05,
        0x02, 0x2e, 0x00, 0x02, 0x2f, 0x00, 0x02, 0x31, 0x00,
        0x02, 0x32, 0x00, 0x02, 0x33, 0x00, 0x05, 0x36, 0x01, 0x01, 0x01, 0x01,
    ])
    cs = proto.checksum_dd(0x00, 0x15, init_data)
    init_frame = bytes([0xdd, 0x00, 0x15]) + init_data + bytes([cs, 0xaa])
    fp = proto.FrameParser()
    frames = fp.feed(init_frame)
    _check(ok, fail, "帧解析器切出 1 帧", len(frames) == 1)
    dec = proto.decode_frame(frames[0]) if frames else {}
    init = dec.get("init", {})
    mods = {(g, e): v for g, e, v in init.get("modules", [])}
    _check(ok, fail, "init 触控块解析出 10 项", len(init.get("touch", [])) == 10)
    _check(ok, fail, "init 模块数==17", len(mods) == 17)
    _check(ok, fail, "init 02 09 == 03 (通透)", mods.get((0x02, 0x09)) == bytes([0x03]))
    _check(ok, fail, "init 04 0c 电量 3 字节", mods.get((0x04, 0x0c)) == bytes([0xe4, 0xe4, 0x61]))
    _check(ok, fail, "init 05 36 循环 4 字节",
           mods.get((0x05, 0x36)) == bytes([0x01, 0x01, 0x01, 0x01]))

    # 7. 只读白名单
    try:
        proto.build_capability_query(2)
        _check(ok, fail, "能力查询 1e fa 可构造", True)
    except Exception:
        _check(ok, fail, "能力查询 1e fa 可构造", False)
    try:
        proto.build_query(2, 0x02, 0x09, bytes([0x01]))
        _check(ok, fail, "写入模块 02 09 被拒绝", False)
    except proto.UnsafeCommandError:
        _check(ok, fail, "写入模块 02 09 被拒绝", True)

    # 8. checksum_ff 直接验证
    _check(ok, fail, "checksum_ff(seq=04, 02 07, val=01) == 0x0d",
           proto.checksum_ff(0x04, 0x02, 0x07, bytes([0x01])) == 0x0d)

    # 9. build_query dd 格式（01 fe）
    q = proto.build_query(4, 0x01, 0xfe)
    _check(ok, fail, "build_query(01 fe) 使用 dd 格式",
           q.hex() == "dd0401fee0aa")

    # 10. build_custom_eq_query 可构造且格式正确
    ceq = proto.build_custom_eq_query(3)
    _check(ok, fail, "custom-eq 查询 ff 03 02 fa 3e … aa",
           ceq.hex() == "ff0302fa3e3caa")

    # 11. verify_response_checksum 负向测试
    _check(ok, fail, "太短的帧被拒绝",
           not proto.verify_response_checksum(bytes([0xdd, 0x00])))
    _check(ok, fail, "错误起始字节被拒绝",
           not proto.verify_response_checksum(bytes([0xff, 0x00, 0x01, 0xfe, 0x00, 0xaa])))
    _check(ok, fail, "缺少结束 aa 被拒绝",
           not proto.verify_response_checksum(bytes([0xdd, 0x00, 0x01, 0xfe, 0xe0])))

    # 12. decode_frame 未知类型
    unk = proto.decode_frame(bytes([0xdd, 0x00, 0x99, 0x01, 0x9b, 0xaa]))
    _check(ok, fail, "未知 type 0x99 → kind=UNKNOWN",
           unk["kind"] == "UNKNOWN")

    # 13. FrameParser：噪声前缀被丢弃
    noise_cs = (0xdd + 0x00 + 0x01 + 0xfe) & 0xff
    fp = proto.FrameParser()
    frames = fp.feed(bytes([0x00, 0x01, 0x02, 0xdd, 0x00, 0x01, 0xfe, noise_cs, 0xaa]))
    _check(ok, fail, "帧解析器丢弃 3 字节噪声前缀", len(frames) == 1)
    _check(ok, fail, "噪声后的帧解码为 ACK",
           proto.decode_frame(frames[0])["kind"] == "ACK")

    # 14. FrameParser：data 内含 0xAA（不结束帧）
    bat_cs = (0xdd + 0x02 + 0x04 + 0x0c + 0xe4 + 0xaa + 0x61) & 0xff
    bat_frame = bytes([0xdd, 0x02, 0x04, 0x0c, 0xe4, 0xaa, 0x61, bat_cs, 0xaa])
    fp2 = proto.FrameParser()
    frames2 = fp2.feed(bat_frame)
    _check(ok, fail, "data 内 0xAA 不误切帧", len(frames2) == 1)
    dec2 = proto.decode_frame(frames2[0])
    _check(ok, fail, "含 0xAA 的帧解码为电量模块",
           dec2.get("kind") == "MODULE" and dec2.get("group") == 0x04 and dec2.get("enum") == 0x0c)

    # 15. FrameParser：两帧拼接
    fp3 = proto.FrameParser()
    ack1 = bytes([0xdd, 0x04, 0x01, 0xfe, 0xe0, 0xaa])
    ack2 = bytes([0xdd, 0x05, 0x01, 0xfe, 0xe1, 0xaa])
    frames3 = fp3.feed(ack1 + ack2)
    _check(ok, fail, "两帧拼接切出 2 帧", len(frames3) == 2)
    _check(ok, fail, "第一帧 seq=0x04", frames3[0][1] == 0x04)
    _check(ok, fail, "第二帧 seq=0x05", frames3[1][1] == 0x05)

    return ok[0], fail[0]


# ── 写命令 ──────────────────────────────────────────────────────────

def _test_writer():
    ok, fail = [0], [0]

    print("\n=== 写命令自测（rs_writer）===")

    def cap(seq, mod_hi, mod_lo, values):
        return wproto.build_write_frame(seq, mod_hi, mod_lo, bytes(values)).hex()

    # 1. build_write_frame
    _check(ok, fail, "触控关 ff 04 02 07 01 0d aa",
           cap(0x04, 0x02, 0x07, [0x01]) == "ff040207010daa")
    _check(ok, fail, "语言英文 ff 02 02 31 01 35 aa",
           cap(0x02, 0x02, 0x31, [0x01]) == "ff0202310135aa")
    _check(ok, fail, "提示音音量2 ff 06 02 2e 02 37 aa",
           cap(0x06, 0x02, 0x2e, [0x02]) == "ff06022e0237aa")
    _check(ok, fail, "降噪循环 ff 0c 05 36 00 00 01 01 48 aa",
           cap(0x0c, 0x05, 0x36, [0x00, 0x00, 0x01, 0x01]) == "ff0c05360000010148aa")

    # 2. encode_eq_gains
    eq = wproto.encode_eq_gains([6, 5, 4, 3, 2, 1, 0, -1, -2, -3])
    _check(ok, fail, "encode_eq_gains 正负混合 → signed-magnitude",
           eq == bytes([0x06, 0x05, 0x04, 0x03, 0x02, 0x01, 0x00, 0x81, 0x82, 0x83]))

    # 3. encode/decode 往返一致
    for sample in (
        [0] * 10,
        [6, 6, 6, 6, 6, -6, -6, -6, -6, -6],
        [6, 5, 4, 3, 2, 1, 0, -1, -2, -3],
        [-1, -2, -3, -4, -5, -6, 0, 1, 2, 3],
    ):
        enc = wproto.encode_eq_gains(sample)
        dec = proto.decode_custom_eq(enc)
        _check(ok, fail, "encode_eq_gains/decode_custom_eq 往返 %s" % sample, dec == sample)

    # 4. 各写操作
    def single(op_id, **kw):
        return wproto.WRITE_OPS[op_id]["build"](**kw)

    _check(ok, fail, "eq pop → 02 2a 01",
           single("eq", preset="pop") == [(0x02, 0x2a, bytes([0x01]))])
    _check(ok, fail, "eq custom → 02 2a 04",
           single("eq", preset="custom") == [(0x02, 0x2a, bytes([0x04]))])
    _check(ok, fail, "anc-mode anc → 02 09 01",
           single("anc-mode", mode="anc") == [(0x02, 0x09, bytes([0x01]))])
    _check(ok, fail, "anc-level 5 → 02 2c 05",
           single("anc-level", level=5) == [(0x02, 0x2c, bytes([0x05]))])
    _check(ok, fail, "game-mode on → 02 0e 01",
           single("game-mode", state="on") == [(0x02, 0x0e, bytes([0x01]))])
    _check(ok, fail, "touch on → 02 07 00（ON=00）",
           single("touch", state="on") == [(0x02, 0x07, bytes([0x00]))])
    _check(ok, fail, "ldac on → 02 2b 01",
           single("ldac", state="on") == [(0x02, 0x2b, bytes([0x01]))])
    _check(ok, fail, "multi-device on → 02 32 01",
           single("multi-device", state="on") == [(0x02, 0x32, bytes([0x01]))])
    _check(ok, fail, "anc-cycle 1 0 0 1 → 05 36 01 00 00 01",
           single("anc-cycle", anc=1, wind=0, normal=0, transparency=1) ==
           [(0x05, 0x36, bytes([0x01, 0x00, 0x00, 0x01]))])
    _check(ok, fail, "gesture 01 03 → 03 01 01 03",
           single("gesture", pos=0x01, action=0x03) ==
           [(0x03, 0x01, bytes([0x01, 0x03]))])

    # 5. 越界/非法值被拒
    def raises(fn):
        try:
            fn()
            return False
        except wproto.WriteError:
            return True

    _check(ok, fail, "anc-level 4 非法被拒", raises(lambda: single("anc-level", level=4)))
    _check(ok, fail, "anc-level 0 越界被拒", raises(lambda: single("anc-level", level=0)))
    _check(ok, fail, "prompt-tone 6 越界被拒", raises(lambda: single("prompt-tone", level=6)))
    _check(ok, fail, "EQ 段 +7 越界被拒",
           raises(lambda: single("custom-eq", gains=[7, 0, 0, 0, 0, 0, 0, 0, 0, 0])))
    _check(ok, fail, "EQ 非法预设被拒", raises(lambda: single("eq", preset="jazz")))
    _check(ok, fail, "EQ 段数不足被拒", raises(lambda: single("custom-eq", gains=[0, 0, 0])))
    _check(ok, fail, "anc-cycle 非 0/1 被拒",
           raises(lambda: single("anc-cycle", anc=2, wind=0, normal=0, transparency=0)))
    _check(ok, fail, "gesture 预留位置 05 被拒",
           raises(lambda: single("gesture", pos=0x05, action=0x01)))
    _check(ok, fail, "gesture 非法位置被拒",
           raises(lambda: single("gesture", pos=0x99, action=0x01)))
    # encode_eq_gains 类型异常
    _check(ok, fail, "EQ bool True 被拒",
           raises(lambda: wproto.encode_eq_gains([True] + [0] * 9)))
    _check(ok, fail, "EQ float 被拒",
           raises(lambda: wproto.encode_eq_gains([1.5] + [0] * 9)))
    _check(ok, fail, "EQ 负值 -7 越界被拒",
           raises(lambda: wproto.encode_eq_gains([-7, 0, 0, 0, 0, 0, 0, 0, 0, 0])))
    # _level 类型异常（prompt-tone 底层走 _level）
    _check(ok, fail, "提示音音量负数被拒",
           raises(lambda: single("prompt-tone", level=-1)))
    _check(ok, fail, "提示音音量字符串被拒",
           raises(lambda: single("prompt-tone", level="abc")))
    _check(ok, fail, "anc-level 字符串被拒",
           raises(lambda: single("anc-level", level="xyz")))
    # build_write_frame 边界序号
    _check(ok, fail, "命令帧 seq=0x00",
           wproto.build_write_frame(0, 0x02, 0x07, bytes([0x01]))[1] == 0)
    _check(ok, fail, "命令帧 seq=0xff",
           wproto.build_write_frame(0xff, 0x02, 0x07, bytes([0x01]))[1] == 0xff)
    # 校验和溢出边界（全 0xff 累加验证 & 0xff 截断）
    _check(ok, fail, "checksum_ff 溢出截断",
           proto.checksum_ff(0xff, 0xff, 0xff, bytes([0xff])) == (0xff * 5) & 0xff)
    _check(ok, fail, "checksum_dd 溢出截断",
           proto.checksum_dd(0xff, 0xff, bytes([0xff, 0xff])) == (0xdd + 0xff + 0xff + 0xff + 0xff) & 0xff)

    # 6. reboot 标志
    _check(ok, fail, "ldac 带 reboot 标志", bool(wproto.WRITE_OPS["ldac"].get("reboot")))
    _check(ok, fail, "multi-device 带 reboot 标志",
           bool(wproto.WRITE_OPS["multi-device"].get("reboot")))
    _check(ok, fail, "game-mode 无 reboot 标志",
           not wproto.WRITE_OPS["game-mode"].get("reboot"))
    _check(ok, fail, "ldac 带出仓硬前置",
           wproto.WRITE_OPS["ldac"].get("hard_precond") == "out_of_case")
    _check(ok, fail, "multi-device 带出仓硬前置",
           wproto.WRITE_OPS["multi-device"].get("hard_precond") == "out_of_case")

    # 7. build_write_sequence
    seq = _seq_factory()
    full = wproto.build_write_sequence(lambda: seq(), single("game-mode", state="on"))
    _check(ok, fail, "单写序列仅含 1 命令帧", len(full) == 1)
    _check(ok, fail, "单写序列直接以 ff 命令帧开始",
           full[0] == ("02 0e", bytes.fromhex("ff02020e0112aa")))

    # 8. build_write_frame 零长度值
    zf = wproto.build_write_frame(2, 0x02, 0x07, b"")
    _check(ok, fail, "零长度值帧格式正确（不含值段）",
           len(zf) == 6 and zf[0] == 0xff and zf[-1] == 0xaa)
    _check(ok, fail, "零长度值帧校验和正确",
           zf[-2] == (0xff + 0x02 + 0x02 + 0x07) & 0xff)

    return ok[0], fail[0]


# ── 本次修复回归 ───────────────────────────────────────────────────────

def _reg_frame(seq, type_byte, data):
    data = bytes(data)
    return bytes([0xDD, seq, type_byte, *data,
                  proto.checksum_dd(seq, type_byte, data), 0xAA])


def _test_regressions():
    """覆盖 JSON、连接生命周期、GUI 写入和坐标换算边界。"""
    # GUI 依赖 Flet，但不需要真实 Page 或蓝牙设备。
    import roselink_gui as gui

    ok, fail = [0], [0]
    print("\n=== 修复回归自测（reader/GUI）===")

    class FakeSocket:
        def __init__(self, *chunks):
            self.sent = []
            self.chunks = list(chunks)
            self.timeout = None
            self.closed = False

        def send(self, data):
            self.sent.append(bytes(data))

        def recv(self, size):
            if self.chunks:
                return self.chunks.pop(0)
            return b""

        def settimeout(self, value):
            self.timeout = value

        def close(self):
            self.closed = True

    class FakeConnection:
        def __init__(self, sock, start_seq=4):
            self.sock = sock
            self.io_lock = threading.RLock()
            self._seq = start_seq

        def _next_seq(self):
            value = self._seq
            self._seq = 2 if value == 0xFF else value + 1
            return value

    class SlowSocket(FakeSocket):
        def connect(self, address):
            time.sleep(0.1)

    class FakeBluetooth:
        RFCOMM = object()

        def __init__(self, sock):
            self.sock = sock

        def BluetoothSocket(self, protocol):
            return self.sock

    # 1. INIT 内嵌 bytes 能安全编码
    init = _reg_frame(1, 0x15, bytes([0x02, 0x09, 0x03]))
    decoded = proto.decode_frame(init)
    try:
        safe = reader._json_safe(decoded)
        encoded = json.dumps(safe, ensure_ascii=False)
        cond = ('"modules": [[2, 9, "03"]]' in encoded and
                safe["init"]["modules"][0][2] == "03")
    except (TypeError, KeyError, IndexError):
        cond = False
    _check(ok, fail, "JSON 递归转换 INIT bytes", cond)

    # 2. watch JSON 只输出 JSON Lines
    conn = object.__new__(reader.Connection)
    conn.sock = FakeSocket(init)
    conn.io_lock = threading.RLock()
    conn.raw = False
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        reader._watch_loop(conn, reader.DeviceState(),
                           SimpleNamespace(json=True))
    lines = [line for line in output.getvalue().splitlines() if line]
    try:
        cond = (len(lines) == 1 and
                json.loads(lines[0])["init"]["modules"][0][2] == "03")
    except (json.JSONDecodeError, KeyError, IndexError):
        cond = False
    _check(ok, fail, "watch JSON 输出合法 JSON Lines", cond)

    # 3. 文本报告同样隐藏 02 2f
    state = reader.DeviceState()
    state.consume(proto.decode_frame(_reg_frame(1, 0x02, bytes([0x2F, 0x00]))))
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        reader.print_report(state)
    _check(ok, fail, "文本报告隐藏查找耳机模块", "查找耳机" not in output.getvalue())

    # 4. GUI 写入沿用连接级序号，并保留同时到达的状态帧
    state_frame = _reg_frame(5, 0x02, bytes([0x09, 0x03]))
    ack_frame = _reg_frame(6, 0x01, bytes([0xFE]))
    sock = FakeSocket(state_frame + ack_frame)
    app = object.__new__(gui.RoseLinkApp)
    app.conn = FakeConnection(sock, start_seq=4)
    app._sock_lock = threading.RLock()
    app._watch_stop = threading.Event()
    messages = []
    app._post = messages.append
    try:
        result = app._do_write_inner("game-mode", {"state": "on"})
        frames = [m["decoded"] for m in messages if m.get("type") == "frame"]
        cond = (result == "操作成功" and sock.sent[0][1] == 0x04 and
                len(frames) == 1 and frames[0]["kind"] == "MODULE" and
                frames[0]["values"] == bytes([0x03]))
    except (IndexError, KeyError, RuntimeError):
        cond = False
    _check(ok, fail, "GUI 写入序号与状态帧分发", cond)

    # 5. 多设备重启全帧发出但无 ACK 时按预期成功
    sock = FakeSocket(b"")
    app = object.__new__(gui.RoseLinkApp)
    app.conn = FakeConnection(sock, start_seq=4)
    app._sock_lock = threading.RLock()
    app._watch_stop = threading.Event()
    app._post = lambda message: None
    try:
        result = app._do_write_inner("multi-device", {"state": "on"})
        cond = "等待重连" in result and sock.sent[0][1] == 0x04
    except (IndexError, RuntimeError):
        cond = False
    _check(ok, fail, "多设备重启无 ACK 路径", cond)

    # 6. EQ 拖动按真实绘图区上下边界换算
    _check(ok, fail, "EQ 坐标 ±6dB 边界",
           gui.RoseLinkApp._gain_from_canvas_y(gui._EQ_PAD_T) == 6 and
           gui.RoseLinkApp._gain_from_canvas_y(
               gui._EQ_CANVAS_H - gui._EQ_PAD_B) == -6 and
           gui.RoseLinkApp._gain_from_canvas_y(0) == 6 and
           gui.RoseLinkApp._gain_from_canvas_y(gui._EQ_CANVAS_H) == -6)

    # 7. 设备上报 02 2f=00 会清除左右按钮状态
    app = object.__new__(gui.RoseLinkApp)
    app._find_left_active = True
    app._find_right_active = True
    app._find_btn_ui = {}
    app._update_find_state(bytes([0x00]))
    _check(ok, fail, "查找耳机 0x00 清除按钮状态",
           not app._find_left_active and not app._find_right_active)

    # 8. connect_timeout 确实限制阻塞连接
    sock = SlowSocket()
    conn = object.__new__(reader.Connection)
    conn.bluetooth = FakeBluetooth(sock)
    conn.mac = "00:11:22:33:44:55"
    conn.channel = 6
    conn.timeout = 1.0
    conn.raw = False
    conn.sock = None
    conn._seq = 2
    conn._seq_lock = threading.Lock()
    conn.io_lock = threading.RLock()
    try:
        conn.connect(connect_timeout=0.01)
        cond = False
    except RuntimeError as ex:
        cond = "连接超时" in str(ex) and conn.sock is None
    _check(ok, fail, "connect_timeout 生效", cond)

    # 9. 自动扫描重启不会复活旧线程（每个线程持有自己的停止事件）
    app = object.__new__(gui.RoseLinkApp)
    app._auto_scan_thread = None
    app._auto_scan_stop = threading.Event()
    app._post = lambda msg: None
    orig_scan = gui._scan_devices
    orig_sleep = gui.time.sleep
    gui._scan_devices = lambda: []
    gui.time.sleep = lambda s: None
    try:
        app._start_auto_scan()
        old_stop = app._auto_scan_stop
        old_thread = app._auto_scan_thread
        app._stop_auto_scan()
        # 旧线程仍在收尾时立即重启：新事件必须独立且未被设置，
        # 旧事件保持已设置（不会被 clear "复活"）。
        app._start_auto_scan()
        new_stop = app._auto_scan_stop
        cond = (old_stop.is_set() and new_stop is not old_stop and
                not new_stop.is_set())
        app._stop_auto_scan()
        old_thread.join(2)
        cond = cond and not old_thread.is_alive()
    finally:
        gui._scan_devices = orig_scan
        gui.time.sleep = orig_sleep
    _check(ok, fail, "自动扫描重启不复活旧线程", cond)

    # 10. 空值模块帧不会让 GUI 状态同步抛 IndexError
    class DummyControl:
        selected = []

    app = object.__new__(gui.RoseLinkApp)
    app.state = reader.DeviceState()
    app._find_left_active = app._find_right_active = False
    app._find_btn_ui = {}
    app._ref_dev_info = None
    app._batt_ui = {}
    app._ref_anc_mode_sb = DummyControl()
    app._ref_anc_level_sb = DummyControl()
    app._ref_trans_level_sb = DummyControl()
    app._ref_anc_chks = []
    app._ref_eq_radio = None
    app._ref_custom_eq_section = None
    app._ref_ldac_switch = app._ref_game_switch = app._ref_touch_switch = None
    app._ref_lang_sb = app._ref_multi_switch = None
    app._ref_prompt_slider = None
    app._gesture_dropdowns = {}
    app._eq_gains = None
    app._eq_readout_texts = None
    empty_vals = bytes([0xdd, 0x01, 0x02, 0x09,
                        (0xdd + 0x01 + 0x02 + 0x09) & 0xff, 0xaa])
    app.state.consume(proto.decode_frame(empty_vals))
    try:
        app._sync_controls_from_state()
        cond = True
    except IndexError:
        cond = False
    _check(ok, fail, "空值模块帧不使状态同步崩溃", cond)

    # 11. CLI watch 模式设备异常断开：友好提示，无 traceback
    class BoomWatchConn:
        def watch_frames(self, on_frame, stop_event=None):
            raise ConnectionError("测试断开")

    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        reader._watch_loop(BoomWatchConn(), reader.DeviceState(),
                           SimpleNamespace(json=False))
    text = output.getvalue()
    _check(ok, fail, "watch 断连提示无 traceback",
           "连接已断开" in text and "Traceback" not in text)

    # 12. gesture 非法参数：干净报错退出，无 traceback
    for bad_args in (["gesture", "zz", "03"], ["gesture", "100", "03"]):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            rc = writer.main(bad_args)
        text = output.getvalue()
        cond = rc == 2 and "✗" in text and "Traceback" not in text
        _check(ok, fail, "gesture 非法参数 %s 干净报错" % bad_args[1], cond)

    # 13. 写 CLI 发送阶段蓝牙异常：返回错误码，无 traceback
    class BoomSock:
        def send(self, data):
            raise OSError("模拟发送失败")

    class BoomConn:
        raw = False

        def __init__(self, *args, **kwargs):
            self.sock = BoomSock()

        def connect(self, connect_timeout=None):
            pass

        def _next_seq(self):
            return 2

        def close(self):
            pass

    orig_conn_cls = writer.reader.Connection
    writer.reader.Connection = BoomConn
    output = io.StringIO()
    try:
        args = SimpleNamespace(mac="00:00:00:00:00:00", channel=6,
                               connect_timeout=1.0, raw=False,
                               dry_run=False, force=True)
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            rc = writer.execute_write(
                args, "game-mode", wproto.WRITE_OPS["game-mode"],
                wproto.WRITE_OPS["game-mode"]["build"](state="on"),
                ["游戏模式 开启"])
    finally:
        writer.reader.Connection = orig_conn_cls
    text = output.getvalue()
    _check(ok, fail, "写 CLI 发送异常返回错误码且无 traceback",
           rc == 1 and "✗" in text and "Traceback" not in text)

    # 15. 系统正在连接设备（EALREADY）时自动重试并成功
    class FlakyConnectSocket(FakeSocket):
        def __init__(self, failures=2):
            super().__init__()
            self.attempts = 0
            self.failures = failures

        def connect(self, address):
            self.attempts += 1
            if self.attempts <= self.failures:
                raise OSError(errno.EALREADY, "Operation already in progress")

    sock15 = FlakyConnectSocket(failures=2)
    conn15 = object.__new__(reader.Connection)
    conn15.bluetooth = FakeBluetooth(sock15)
    conn15.mac = "00:11:22:33:44:55"
    conn15.channel = 6
    conn15.timeout = 1.0
    conn15.raw = False
    conn15.sock = None
    conn15._seq = 2
    conn15._seq_lock = threading.Lock()
    conn15.io_lock = threading.RLock()
    orig_delay = reader.CONNECT_RETRY_DELAY
    reader.CONNECT_RETRY_DELAY = 0.01
    try:
        conn15.connect(connect_timeout=5)
        cond = sock15.attempts == 3 and conn15.sock is sock15
    except Exception:
        cond = False
    finally:
        reader.CONNECT_RETRY_DELAY = orig_delay
    _check(ok, fail, "EALREADY 自动重试后连接成功", cond)

    # 16. 持续 EALREADY 时按预算超时，并提示已自动重试
    sock16 = FlakyConnectSocket(failures=1000)
    conn16 = object.__new__(reader.Connection)
    conn16.bluetooth = FakeBluetooth(sock16)
    conn16.mac = "00:11:22:33:44:55"
    conn16.channel = 6
    conn16.timeout = 1.0
    conn16.raw = False
    conn16.sock = None
    conn16._seq = 2
    conn16._seq_lock = threading.Lock()
    conn16.io_lock = threading.RLock()
    reader.CONNECT_RETRY_DELAY = 0.01
    try:
        try:
            conn16.connect(connect_timeout=0.3)
            cond = False
        except RuntimeError as ex:
            text = str(ex)
            cond = ("连接超时" in text and "重试" in text and
                    sock16.attempts > 5)
    finally:
        reader.CONNECT_RETRY_DELAY = orig_delay
    _check(ok, fail, "持续 EALREADY 超时并提示已重试", cond)

    return ok[0], fail[0]


def main():
    r_ok, r_fail = _test_reader()
    w_ok, w_fail = _test_writer()
    g_ok, g_fail = _test_regressions()
    total_ok = r_ok + w_ok
    total_fail = r_fail + w_fail
    print("\n─── 汇总 ──────────────────────────────────────")
    print("  解码器: %d 通过, %d 失败" % (r_ok, r_fail))
    print("  写命令: %d 通过, %d 失败" % (w_ok, w_fail))
    print("  修复回归: %d 通过, %d 失败" % (g_ok, g_fail))
    total_ok += g_ok
    total_fail += g_fail
    print("  总计:   %d 通过, %d 失败" % (total_ok, total_fail))
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
