#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RoseLink 写操作 CLI（Linux / BlueZ / 经典蓝牙 SPP-RFCOMM）。

实现 CERAMICS-MK2 所有**已确认支持**的写操作（不含游戏 EQ 预设 a0~a4 与
清亮/Light 02 2a 03，App 不提供）。命令字节均从 rs_common_v2_sender.dart 逐条
反汇编验证，与只读 reader 的抓包交叉核对一致。

与只读 reader 的关系：
- reader 保持纯只读、其只读白名单（rs_protocol.ALLOWED_QUERIES）护栏不动。
- writer 通过 `import roselink_reader` 复用其 Connection / FrameParser /
  decode_frame 做连接与 ACK 接收。
- 写帧通过 Connection 的 socket 直接发送（_send 本就是通用发送；reader 自身只
  调用只读查询，不违反其只读行为）。

会触发耳机重启的写（LDAC 编解码切换 02 2b、多设备开关 02 32）默认需交互式二次
确认（--force 跳过）。LDAC（02 2b）与多设备连接（02 32）切换前均须两只耳机
均已出仓（读电量帧 04 0c 左/右字节 bit7 均为 0），与 App 行为一致。

用法示例:
    # 自测（离线，无需设备）
    python3 roselink_writer.py --selftest

    # 演练（不发送，仅打印各帧 hex + 解码意图）
    python3 roselink_writer.py --mac AA:BB:CC:DD:EE:FF eq pop --dry-run

    # 实写并等待 ACK
    python3 roselink_writer.py --mac AA:BB:CC:DD:EE:FF game-mode on
    python3 roselink_writer.py --mac ... eq custom
    python3 roselink_writer.py --mac ... custom-eq 6 5 4 3 2 1 0 -1 -2 -3
    python3 roselink_writer.py --mac ... ldac on          # 重启 + 出仓拦截
    python3 roselink_writer.py --mac ... anc-cycle 1 0 0 1 # 降噪+通透
"""

from __future__ import annotations

import argparse
import sys
import time

import rs_protocol as proto
import rs_writer as wproto
import roselink_reader as reader   # 复用 Connection / DeviceState / 解析

ACK_TIMEOUT = 1.5     # 写后等待 ACK 的秒数
BATTERY_WAIT = 2.0    # 出仓前置（LDAC/多设备）：监听电量帧的最长秒数


# ---------------------------------------------------------------------------
# 写执行
# ---------------------------------------------------------------------------
class WriterSession:
    """封装一次写会话：连接、出仓前置、发送与 ACK。"""

    def __init__(self, conn):
        self.conn = conn

    # -- 出仓前置（LDAC 02 2b / 多设备 02 32）--------------------------------
    def check_both_out_of_case(self):
        """监听 ~2s 捕获最新电量帧 04 0c，返回 (ok: bool|None, detail: str)。

        ok=True：两耳均出仓（左右字节 bit7 均为 0）。
        ok=False：任一在仓（bit7=1）。
        ok=None：未能在窗口内捕获电量帧。
        """
        latest = {"bat": None}

        def on_frame(d):
            if d.get("kind") == "INIT":
                for group, enum, values in d.get("init", {}).get("modules", []):
                    if (group, enum) == (0x04, 0x0c):
                        latest["bat"] = proto.decode_battery(values)
            if d.get("kind") == "MODULE" and (d.get("group"), d.get("enum")) == (0x04, 0x0c):
                latest["bat"] = proto.decode_battery(d["values"])

        self.conn.read_frames(BATTERY_WAIT, on_frame)
        bat = latest["bat"]
        if not bat or "left_pct" not in bat:
            return None, "未能在 %.0fs 内捕获电量帧" % BATTERY_WAIT
        l_in = bat["left_charging"]    # bit7 = 在仓标志（与 reader 的 charging 同位）
        r_in = bat["right_charging"]
        detail = "左耳 %d%%%s / 右耳 %d%%%s" % (
            bat["left_pct"], "(在仓)" if l_in else "(出仓)",
            bat["right_pct"], "(在仓)" if r_in else "(出仓)")
        if l_in or r_in:
            return False, detail
        return True, detail

    # -- 发送序列 ----------------------------------------------------------
    def send_sequence(self, frames_with_labels):
        """发送 [(label, frame), ...]，每帧之间短延时。"""
        for label, frame in frames_with_labels:
            if self.conn.raw:
                print("[send %-10s] %s" % (label, frame.hex()))
            self.conn.sock.send(bytes(frame))
            time.sleep(0.15)

    # -- 等待 ACK ----------------------------------------------------------
    def wait_ack(self, expected_count):
        """在 ACK_TIMEOUT 内等待本次写入后的 ACK（dd <seq> 01 fe <cs> aa）。

        C2H ACK 的 seq 与 H2C 命令 seq 不共用计数器，故仅按收到的 ACK 数量收集；
        ACK 是本 CLI 的成功判据。返回收到的 ACK 序号列表。
        """
        matched = []
        deadline = time.time() + ACK_TIMEOUT

        def on_frame(d):
            if d.get("kind") == "ACK":
                matched.append(d["seq"])

        while time.time() < deadline and len(matched) < expected_count:
            remaining = deadline - time.time()
            self.conn.read_frames(max(0.2, remaining), on_frame)
        return matched

# ---------------------------------------------------------------------------
# 单次写执行流程
# ---------------------------------------------------------------------------
def execute_write(args, op_id, op, op_frames, intents):
    """op_frames: [(mod_hi, mod_lo, values), ...]；intents: 人类可读意图文本列表。"""
    reboot = bool(op.get("reboot"))
    hard_precond = op.get("hard_precond")
    precond_hint = op.get("precond_hint")

    # -- dry-run：仅打印，不连接/发送 --------------------------------------
    if args.dry_run:
        print("演练模式（不发送）：")
        for i, (mod_hi, mod_lo, values) in enumerate(op_frames):
            tag = "命令 %d" % i if len(op_frames) > 1 else "命令"
            print("  %s  %02x %02x  %s" % (tag, mod_hi, mod_lo, values.hex()))
            print("    解码意图: %s" % intents[i])
        # 预演实际发送的 ff 命令帧序列
        seq_gen = _seq_factory()
        full = wproto.build_write_sequence(lambda: seq_gen(), op_frames)
        print("  完整线序列（不含前导帧，序号从 02 起示例）:")
        for label, frame in full:
            print("    %-12s %s" % (label, frame.hex()))
        if reboot:
            print("  ⚠ 此操作会触发耳机重启。")
        if hard_precond == "out_of_case":
            print("  ⚠ 实写时前置：须两只耳机均已出仓。")
        if precond_hint:
            print("  提示: %s" % precond_hint)
        return 0

    # -- 真机：连接 -------------------------------------------------------
    conn = reader.Connection(args.mac, channel=args.channel,
                             timeout=1.0, raw=args.raw)
    conn.connect()
    sess = WriterSession(conn)
    try:
        # 0. 出仓硬前置（LDAC 02 2b、多设备 02 32 均需两耳出仓）
        if hard_precond == "out_of_case" and not args.force:
            ans = _confirm("操作需要两只耳机均已出仓，是否主动请求电量信息？（耳机会上报回复）[y/N] ")
            if not ans:
                print("已取消。")
                return 1
            conn._send(proto.build_capability_query(conn._next_seq()), "能力查询")
            time.sleep(0.5)
            ok, detail = sess.check_both_out_of_case()
            if ok is not True:
                print("✗ 拒绝发送：%s 须两只耳机均已出仓。" % op["desc"].split("（")[0])
                print("  电量帧: %s" % detail)
                print("  请将两只耳机都从充电仓取出后重试"
                      "（或用 --force 跳过本拦截）。")
                return 2

        # 1/2. 打印意图 + 软提示
        for i, (mod_hi, mod_lo, values) in enumerate(op_frames):
            tag = "命令 %d" % i if len(op_frames) > 1 else "命令"
            print("%s  %02x %02x  %s  → %s" % (tag, mod_hi, mod_lo,
                                                 values.hex(), intents[i]))
        if precond_hint:
            print("提示: %s" % precond_hint)

        # 3. 重启项交互确认
        if reboot and not args.force:
            extra = "（注意：此操作会触发耳机重启"
            if hard_precond == "out_of_case":
                extra += "；且须两只耳机都已出仓"
            extra += "）"
            ans = _confirm("确认执行 %s？%s [y/N] " % (op["desc"], extra))
            if not ans:
                print("已取消。")
                return 1

        # 4. 构造并发送实际写命令帧（不发送 01 fe 前导）
        full = wproto.build_write_sequence(conn._next_seq, op_frames)
        print("发送:")
        for label, frame in full:
            print("  %-12s %s" % (label, frame.hex()))
        sess.send_sequence(full)

        # 5. 等待 ACK（C2H seq 不与命令帧 seq 对应）
        matched = sess.wait_ack(len(full))
        if len(matched) == len(full):
            print("✓ 设备已确认（ACK seq=%s）" %
                  ", ".join("%02x" % s for s in sorted(matched)))
        else:
            print("✗ 未收到全部命令 ACK（%d/%d，等待 %.1fs）。"
                  "本 CLI 不将此视为成功。" %
                  (len(matched), len(full), ACK_TIMEOUT))
            if reboot:
                print("  该操作可能已触发重启并断开连接，但无法仅凭当前会话确认。")
            return 2

        if reboot:
            print("ℹ 此操作会触发耳机重启；连接可能断开属正常现象。")
        return 0
    finally:
        conn.close()


def _confirm(prompt):
    try:
        ans = input(prompt)
    except EOFError:
        return False
    return ans.strip().lower() in ("y", "yes")


def _seq_factory(start=2):
    """离线演练用的序号生成器（模拟 Connection._next_seq）。"""
    state = {"seq": start}

    def gen():
        s = state["seq"]
        state["seq"] = (state["seq"] + 1) & 0xFF
        if state["seq"] == 0:
            state["seq"] = 2
        return s
    return gen


# ---------------------------------------------------------------------------
# 意图文本（把参数转成人类可读描述，用于打印与 dry-run）
# ---------------------------------------------------------------------------
def _intents_of(op_frames):
    return [_intents_of_one(mh, ml, v) for mh, ml, v in op_frames]


def _intents_of_one(mod_hi, mod_lo, values):
    key = (mod_hi, mod_lo)
    if key == (0x02, 0x2a) and values == bytes([0x04]):
        return "进入自定义 EQ 模式"
    if key == (0x0b, 0x3e):
        gains = proto.decode_custom_eq(values)
        bands = proto.EQ_BANDS
        return "自定义 EQ: " + ", ".join(
            "%s%+d" % (bands[i] + "=", g) if i < len(bands) else "%+d" % g
            for i, g in enumerate(gains))
    return proto.decode_module(mod_hi, mod_lo, values)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _hex_byte(s, name):
    v = int(s, 16)
    if v < 0 or v > 0xFF:
        raise argparse.ArgumentTypeError("%s 需为 00~ff" % name)
    return v


def _parse_args(argv):
    p = argparse.ArgumentParser(
        description="RoseLink 写操作 CLI（修改设备设置；reader 仍保持只读）")
    # 全局参数
    p.add_argument("--mac", help="目标耳机 MAC 地址（实写必填）")
    p.add_argument("--channel", type=int, default=reader.SPP_CHANNEL,
                   help="RFCOMM 通道（默认 6）")
    p.add_argument("--dry-run", action="store_true",
                   help="仅打印各帧 hex + 解码意图，不发送并退出")
    p.add_argument("--force", action="store_true",
                   help="跳过重启确认与出仓前置拦截")
    p.add_argument("--raw", action="store_true", help="打印收发原始 hex")
    p.add_argument("--list", action="store_true",
                   help="列出所有支持的写操作并退出")
    p.add_argument("--list-touch", action="store_true",
                   help="列出触控手势位置与动作并退出")

    sub = p.add_subparsers(dest="op", metavar="<操作>")

    s = sub.add_parser("eq", help="EQ 预设：hifi|pop|rock|custom")
    s.add_argument("preset", choices=["hifi", "pop", "rock", "custom"])

    s = sub.add_parser("custom-eq", help="自定义 EQ：10 段增益 dB（-6~+6）")
    s.add_argument("gains", type=int, nargs=10,
                   help="10 个整数（顺序: 20Hz/100Hz/300Hz/500Hz/1kHz/2kHz/3kHz/5kHz/8kHz/15kHz），范围 -6~+6")

    s = sub.add_parser("ldac", help="LDAC 无损音频协议开关（⚠ 重启；⚠ 须出仓）")
    s.add_argument("state", choices=["on", "off"])

    s = sub.add_parser("anc-mode", help="ANC 模式：anc|normal|transparency|wind")
    s.add_argument("mode", choices=["anc", "normal", "transparency", "wind"])

    s = sub.add_parser("anc-level", help="降噪等级 1/3/5（轻/中/深）")
    s.add_argument("level", type=int)

    s = sub.add_parser("trans-level", help="通透等级 1/3/5（舒适/人声/标准）")
    s.add_argument("level", type=int)

    s = sub.add_parser("anc-cycle", help="降噪触控循环（顺序: 降噪 风噪 普通 通透，各 0/1）")
    s.add_argument("anc", type=int, choices=[0, 1])
    s.add_argument("wind", type=int, choices=[0, 1])
    s.add_argument("normal", type=int, choices=[0, 1])
    s.add_argument("transparency", type=int, choices=[0, 1])

    s = sub.add_parser("game-mode", help="游戏模式 on|off")
    s.add_argument("state", choices=["on", "off"])

    s = sub.add_parser("touch", help="触控开关 on|off（注意 ON=00/OFF=01）")
    s.add_argument("state", choices=["on", "off"])

    s = sub.add_parser("gesture", help="触控手势：位置(hex) 动作(hex)，如 01 03")
    s.add_argument("pos", nargs="?", help="手势位置（hex，见 --list-touch）")
    s.add_argument("action", nargs="?", help="手势动作（hex，见 --list-touch）")
    s.add_argument("--list-touch", dest="list_touch_sub", action="store_true",
                   help="列出手势位置与动作并退出")

    s = sub.add_parser("language", help="语音语言 cn|en")
    s.add_argument("lang", choices=["cn", "en"])

    s = sub.add_parser("prompt-tone", help="提示音音量 0~5（0=关）")
    s.add_argument("level", type=int)

    s = sub.add_parser("find", help="查找耳机 left|right|both|off")
    s.add_argument("target", choices=["left", "right", "both", "off"])

    s = sub.add_parser("multi-device",
                       help="多设备连接开关（⚠ 重启；⚠ 须两只耳机出仓）")
    s.add_argument("state", choices=["on", "off"])

    return p.parse_args(argv)


def _print_list():
    print("支持的写操作（CERAMICS-MK2 已确认）:")
    for op_id, op in wproto.WRITE_OPS.items():
        flags = []
        if op.get("reboot"):
            flags.append("⚠重启")
        if op.get("hard_precond") == "out_of_case":
            flags.append("⚠须出仓")
        tag = ("  [%s]" % " ".join(flags)) if flags else ""
        print("  %-14s %s%s" % (op_id, op["desc"], tag))
    print("\n触控手势位置/动作见: python3 roselink_writer.py --list-touch")


def _print_touch_list():
    print("触控手势位置（pos）:")
    for value, label in proto.TOUCH_POS.items():
        print("  %02x  %s" % (value, label))
    print("\n触控手势动作（action）:")
    for value, label in proto.TOUCH_ACTION.items():
        print("  %02x  %s" % (value, label))


def main(argv=None):
    args = _parse_args(argv)

    if args.list:
        _print_list()
        return 0
    if args.list_touch or getattr(args, "list_touch_sub", False):
        _print_touch_list()
        return 0
    if not args.op:
        # 无操作：打印帮助与可用操作
        _print_list()
        print("\n用 --selftest 自测；用 <操作> 子命令执行（先 --dry-run 演练）。")
        return 0

    # 解析子命令参数为 op.build() 关键字
    if args.op == "gesture" and (args.pos is None or args.action is None):
        print("✗ gesture 需要 pos 和 action；请先用 --list-touch 查看可用值。")
        return 2
    kwargs = _kwargs_for_op(args, args.op)
    op = wproto.WRITE_OPS[args.op]
    try:
        op_frames = op["build"](**kwargs)
    except wproto.WriteError as ex:
        print("✗ 参数非法: %s" % ex)
        return 2
    intents = _intents_of(op_frames)

    # dry-run 无需 MAC
    if not args.dry_run and not args.mac:
        print("✗ 实写需要 --mac <地址>（或用 --dry-run 演练）。")
        return 2

    return execute_write(args, args.op, op, op_frames, intents)


def _kwargs_for_op(args, op_id):
    """把 argparse 的 Namespace 映射为对应 op.build() 的关键字参数。"""
    if op_id == "eq":
        return {"preset": args.preset}
    if op_id == "custom-eq":
        return {"gains": args.gains}
    if op_id == "ldac":
        return {"state": args.state}
    if op_id == "anc-mode":
        return {"mode": args.mode}
    if op_id in ("anc-level", "trans-level"):
        return {"level": args.level}
    if op_id == "anc-cycle":
        return {"anc": args.anc, "wind": args.wind,
                "normal": args.normal, "transparency": args.transparency}
    if op_id in ("game-mode", "touch"):
        return {"state": args.state}
    if op_id == "gesture":
        try:
            pos = _hex_byte(args.pos, "pos")
            action = _hex_byte(args.action, "action")
        except ValueError:
            print("✗ gesture 的 pos/action 需为 hex（如 01 03）。")
            raise
        return {"pos": pos, "action": action}
    if op_id == "language":
        return {"lang": args.lang}
    if op_id == "prompt-tone":
        return {"level": args.level}
    if op_id == "find":
        return {"target": args.target}
    if op_id == "multi-device":
        return {"state": args.state}
    raise SystemExit("未知操作 %r" % op_id)


if __name__ == "__main__":
    sys.exit(main())
