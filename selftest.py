#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RoseLink 协议离线自测（无需蓝牙设备）。

覆盖解码器、编码器、帧构造、校验和、参数校验等全部核心逻辑。
"""
from __future__ import annotations

import sys

import rs_protocol as proto
import rs_writer as wproto


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


def main():
    r_ok, r_fail = _test_reader()
    w_ok, w_fail = _test_writer()
    total_ok = r_ok + w_ok
    total_fail = r_fail + w_fail
    print("\n─── 汇总 ──────────────────────────────────────")
    print("  解码器: %d 通过, %d 失败" % (r_ok, r_fail))
    print("  写命令: %d 通过, %d 失败" % (w_ok, w_fail))
    print("  总计:   %d 通过, %d 失败" % (total_ok, total_fail))
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
