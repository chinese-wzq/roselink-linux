#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RoseLink 只读 CLI（Linux / BlueZ / 经典蓝牙 SPP-RFCOMM）。

只读地读取 CERAMICS-MK2 的所有已知能力与当前状态。
仅发送非破坏性查询指令（1e fa / 02 fa 3e），绝不写入任何设备设置。
（`01 fe` 仅作写命令前导，与 App 一致；本工具无写操作，故不发送。）

用法:
    python3 roselink_reader.py --scan                 # 扫描附近蓝牙设备
    python3 roselink_reader.py --mac AA:BB:CC:DD:EE:FF # 连接并读取一次
    python3 roselink_reader.py --mac ... --watch       # 持续监听周期状态
    python3 roselink_reader.py --mac ... --json         # JSON 输出
    python3 selftest.py                                 # 离线自测（无需蓝牙设备）
"""

from __future__ import annotations

import argparse
import errno
import json
import sys
import threading
import time

import rs_protocol as proto

# 通道 6 是耳机固件在 SDP 服务记录里注册的固定值（HCI 抓包 +
# RFCOMM PN 协商确认）。App 通过 UUID 00001101-0000-1000-8000-
# 00805f9b34fb 连接，SDP 解析结果同为通道 6。勿改：其他型号
# 若注册不同通道，仅需在此调整并同步注释。
SPP_CHANNEL = 6

# BlueZ 在系统/其他客户端正在连接同一设备时，RFCOMM connect 会返回
# EALREADY（errno 114, "Operation already in progress"），属瞬时状态，
# 等待片刻后重试通常即可成功。重试在 connect_timeout 预算内进行。
CONNECT_RETRY_DELAY = 0.5

# JSON 输出中不包含的模块（文本模式也未展示的噪声条目）
_HIDDEN_MODULES = {
    (0x02, 0x08),  # 入耳检测(本机无此硬件, 占位)
    (0x02, 0x12),  # 硬件能力标记
    (0x02, 0x2f),  # 查找耳机(读取时恒为 off, 无实际意义)
    (0x02, 0x33),  # 空间音频(本机不支持)
}

# ---------------------------------------------------------------------------
# 设备状态聚合
# ---------------------------------------------------------------------------
class DeviceState:
    """消费解码后的帧，聚合为当前设备状态。"""

    def __init__(self):
        self.name = None
        self.mac = None
        self.modules = {}      # (group, enum) -> {"values": bytes, "decoded": str}
        self.touch = []        # [(pos, action), ...]
        self.battery = None    # decode_battery 结构
        self.firmware = None
        self.unknown = []      # [(group, enum, values), ...]
        self.acks = 0

    def consume(self, d):
        kind = d.get("kind")
        if kind == "ACK":
            self.acks += 1
        elif kind == "INIT":
            init = d["init"]
            if init["touch"]:
                self.touch = init["touch"]
            for group, enum, values in init["modules"]:
                self._record(group, enum, values)
        elif kind == "MODULE":
            self._record(d["group"], d["enum"], d["values"])

    def _record(self, group, enum, values):
        key = (group, enum)
        decoded = proto.decode_module(group, enum, values)
        self.modules[key] = {"values": bytes(values), "decoded": decoded}
        if key == (0x04, 0x0c):
            self.battery = proto.decode_battery(values)
        elif key == (0x04, 0x0d):
            self.firmware = proto.decode_firmware(values)
        elif key == (0x03, 0x01) and len(values) >= 2:
            self._merge_touch(values[0], values[1])
        elif key not in proto.MODULES:
            self.unknown.append((group, enum, bytes(values)))

    def _merge_touch(self, pos, action):
        for i, (p, _) in enumerate(self.touch):
            if p == pos:
                self.touch[i] = (pos, action)
                return
        self.touch.append((pos, action))

    # -- 输出 --------------------------------------------------------------
    def to_dict(self):
        mods = {}
        for (g, e), v in sorted(self.modules.items()):
            if (g, e) in _HIDDEN_MODULES:
                continue
            mods["%02x%02x" % (g, e)] = {
                "name": proto.module_name(g, e),
                "raw": v["values"].hex(),
                "decoded": v["decoded"],
            }
        return {
            "name": self.name,
            "mac": self.mac,
            "firmware": self.firmware,
            "battery": self.battery,
            "touch": [
                {"pos": "%02x" % p, "pos_name": proto.TOUCH_POS.get(p, "?"),
                 "action": "%02x" % a,
                 "action_name": proto.TOUCH_ACTION.get(a, "?")}
                for p, a in self.touch
                if p not in (0x05, 0x15)
            ],
            "modules": mods,
        }


# ---------------------------------------------------------------------------
# 人类可读输出
# ---------------------------------------------------------------------------
def _line(title):
    print("\n" + "─" * 3 + " " + title + " " + "─" * max(3, 40 - len(title)))


def _json_safe(value):
    """把协议内部对象转换为 JSON 可传输值。

    解码层故意保留 bytes，供 DeviceState 和 GUI 继续按字节处理；只有
    输出边界才把 bytes 转成无歧义的十六进制字符串。tuple 统一转 list，
    以保证 INIT 内嵌结构也能直接编码。
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def print_report(st):
    print("=" * 56)
    print(" RoseLink 只读状态报告  (CERAMICS-MK2)")
    print("=" * 56)

    _line("设备")
    print("  名称    : %s" % (st.name or "(未知)"))
    print("  MAC     : %s" % (st.mac or "(未知)"))
    print("  固件版本: %s" % (st.firmware or "(未上报)"))

    _line("电量")
    if st.battery and "left_pct" in st.battery:
        b = st.battery
        print("  左耳: %d%%%s" % (b["left_pct"],
                                 "  ⚡充电中" if b["left_charging"] else ""))
        print("  右耳: %d%%%s" % (b["right_pct"],
                                 "  ⚡充电中" if b["right_charging"] else ""))
        case_suffix = " (缓存)" if b.get("case_cached") else ""
        print("  充电仓: %d%%%s" % (b["case_pct"], case_suffix))
    else:
        print("  (未上报)")

    def show(group, enum, label):
        if (group, enum) in _HIDDEN_MODULES:
            return
        m = st.modules.get((group, enum))
        if m:
            print("  %-12s: %s  (raw %s)" % (label, m["decoded"],
                                             m["values"].hex()))

    _line("音频与降噪")
    show(0x02, 0x09, "ANC 模式")
    show(0x02, 0x2c, "降噪等级")
    show(0x02, 0x2d, "通透等级")
    show(0x02, 0x2b, "连接模式")
    show(0x02, 0x2a, "EQ 预设")
    show(0x0b, 0x3e, "自定义 EQ")
    show(0x02, 0x0e, "游戏模式")
    show(0x05, 0x36, "降噪循环")

    _line("交互与其他")
    show(0x02, 0x07, "触控开关")
    show(0x02, 0x2e, "提示音音量")
    show(0x02, 0x31, "语音语言")
    show(0x02, 0x32, "多设备连接")
    show(0x02, 0x2f, "查找耳机")

    if st.touch:
        _line("触控手势配置")
        for pos, act in st.touch:
            if pos not in (0x05, 0x15):
                print("  %-10s → %s" % (proto.TOUCH_POS.get(pos, "pos%02x" % pos),
                                        proto.TOUCH_ACTION.get(act, "act%02x" % act)))

    if st.unknown:
        _line("未知模块 (原始 hex)")
        for g, e, v in st.unknown:
            print("  %02x %02x = %s" % (g, e, v.hex()))


# ---------------------------------------------------------------------------
# 蓝牙连接（PyBluez）
# ---------------------------------------------------------------------------
def _import_bluetooth():
    try:
        import bluetooth
        return bluetooth
    except ImportError:
        sys.exit("错误: 未找到 PyBluez。请先安装: pip install PyBluez")


def _rose_tag(name):
    if name and any(k in name.lower() for k in ("rose", "ceramics")):
        return "  ← 可能是 RoseLink 设备"
    return ""


def _list_known_devices():
    """通过 BlueZ (bluetoothctl) 列出已配对/已连接设备（只读）。

    已连接的设备不会出现在 inquiry 扫描里，因此这才是实际可用的列表。
    返回 (devices, connected_addrs)，失败返回 (None, set())。
    """
    import subprocess
    try:
        out = subprocess.run(["bluetoothctl", "devices"],
                             capture_output=True, text=True, timeout=10)
        conn = subprocess.run(["bluetoothctl", "devices", "Connected"],
                              capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.SubprocessError):
        return None, set()

    def parse(text):
        result = []
        for line in text.splitlines():
            parts = line.split(" ", 2)
            if len(parts) >= 3 and parts[0] == "Device":
                result.append((parts[1], parts[2]))
        return result

    connected = {a for a, _ in parse(conn.stdout)}
    return parse(out.stdout), connected


def do_scan():
    # 1. 已配对/已连接设备（对已连接耳机唯一有效的方式）
    known, connected = _list_known_devices()
    if known:
        print("已配对设备 (BlueZ):")
        for addr, name in known:
            state = "  [已连接]" if addr in connected else ""
            print("  %s  %s%s%s" % (addr, name, state, _rose_tag(name)))
        print("\n提示: 耳机已连接系统时不会出现在下方 inquiry 扫描中，"
              "直接用上面的 MAC 执行 --mac 即可。")
    elif known is None:
        print("(未能调用 bluetoothctl 列出已配对设备，跳过)")

    # 2. inquiry 扫描（仅能发现处于可被发现状态的设备）
    bluetooth = _import_bluetooth()
    print("\n正在 inquiry 扫描附近可被发现的设备（约 10 秒）...")
    try:
        devices = bluetooth.discover_devices(duration=10, lookup_names=True)
    except Exception as ex:  # BlueZ 未就绪/无适配器
        print("inquiry 扫描失败: %s" % ex)
        devices = []
    if devices:
        print("发现 %d 个可被发现的设备:" % len(devices))
        for addr, name in devices:
            print("  %s  %s%s" % (addr, name or "(无名称)", _rose_tag(name)))
    else:
        print("inquiry 未发现新设备（已连接设备属正常，见上方已配对列表）。")
    print("\n用 --mac <地址> 连接目标设备。")


class Connection:
    """只读 RFCOMM 连接封装。"""

    def __init__(self, mac, channel=SPP_CHANNEL, timeout=1.0, raw=False):
        self.bluetooth = _import_bluetooth()
        self.mac = mac
        self.channel = channel
        self.timeout = timeout
        self.raw = raw
        self.sock = None
        self._seq = 2  # 命令序号从 02 起（与抓包一致）
        self._seq_lock = threading.Lock()
        # GUI 的 watch、写入和刷新共用这一把锁，保证同一时刻只有一个
        # 线程从 RFCOMM socket 取数据或向其写数据。
        self.io_lock = threading.RLock()

    def connect(self, connect_timeout=15):
        if connect_timeout is None or connect_timeout <= 0:
            raise ValueError("connect_timeout 必须为正数")
        bt = self.bluetooth
        deadline = time.time() + connect_timeout
        retries = 0
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise self._connect_timeout_error(connect_timeout, retries)
            # 每次尝试都用新 socket：EALREADY 后旧 socket 的连接状态可能
            # 已不可用，不能复用。
            sock = bt.BluetoothSocket(bt.RFCOMM)
            with self.io_lock:
                self.sock = sock

            # PyBluez/BlueZ 在部分平台上若在 connect 前设置 socket timeout
            # 会报 EBADFD，因此用可取消的后台 connect 线程实现上层超时。
            error = []

            def do_connect():
                try:
                    sock.connect((self.mac, self.channel))
                except BaseException as ex:  # 保证等待线程一定能结束
                    error.append(ex)

            worker = threading.Thread(target=do_connect, daemon=True)
            worker.start()
            worker.join(remaining)
            if worker.is_alive():
                self._drop_socket(sock)
                raise self._connect_timeout_error(connect_timeout, retries)

            if not error:
                break
            ex = error[0]
            self._drop_socket(sock)
            if not self._is_retryable_connect_error(ex):
                hint = ("（此前系统正在连接该设备，已自动重试 %d 次）\n"
                        % retries) if retries else ""
                raise RuntimeError(
                    "连接失败: %s\n%s排查建议:\n"
                    "  1. 确认耳机已在系统层配对 (bluetoothctl pair/trust)\n"
                    "  2. 确认通道 6 未被 App 占用 (关闭 RoseLink App)\n"
                    "  3. 确认当前用户有蓝牙权限\n"
                    "  4. 确认 MAC 号正确" % (ex, hint)) from ex
            # EALREADY：系统正在连接同一设备，稍候重试（消耗总预算）。
            retries += 1
            time.sleep(min(CONNECT_RETRY_DELAY, deadline - time.time()))
        try:
            with self.io_lock:
                sock.settimeout(self.timeout)
        except Exception as ex:
            self.close()
            raise RuntimeError("连接建立后设置读超时失败: %s" % ex) from ex

    @staticmethod
    def _is_retryable_connect_error(ex):
        """EALREADY（Operation already in progress）表示系统/其他客户端
        正在连接同一设备，等待片刻后重试通常即可成功。"""
        if getattr(ex, "errno", None) == errno.EALREADY:
            return True
        return "operation already in progress" in str(ex).lower()

    def _connect_timeout_error(self, connect_timeout, retries):
        if retries:
            return RuntimeError(
                "连接超时（%.1f 秒）: %s（期间系统可能一直在连接该设备，"
                "已自动重试 %d 次）" % (connect_timeout, self.mac, retries))
        return RuntimeError("连接超时（%.1f 秒）: %s" %
                            (connect_timeout, self.mac))

    def _drop_socket(self, sock):
        """关闭一次连接尝试的 socket；若 self.sock 仍指向它则还原为 None。"""
        try:
            sock.close()
        except Exception:
            pass
        with self.io_lock:
            if self.sock is sock:
                self.sock = None

    def _next_seq(self):
        with self._seq_lock:
            s = self._seq
            self._seq = (self._seq + 1) & 0xFF
            if self._seq == 0:
                self._seq = 2
            return s

    def _send(self, frame, label):
        """只读发送：frame 必须由 proto.build_* 生成（已过白名单校验）。"""
        with self.io_lock:
            if not self.sock:
                raise RuntimeError("连接已关闭")
            if self.raw:
                print("[send %-12s] %s" % (label, frame.hex()))
            self.sock.send(bytes(frame))

    def send_queries(self):
        """发送全部安全查询指令以触发完整能力/状态上报。

        不发 `01 fe`：它是写命令的前导（App 在改设置前才发），
        本工具纯只读、无写操作，故与 App 行为一致地不发送。
        """
        with self.io_lock:
            for builder, label in (
                (proto.build_capability_query, "能力查询"),
                (proto.build_custom_eq_query, "自定义EQ查询"),
            ):
                try:
                    self._send(builder(self._next_seq()), label)
                    time.sleep(0.5)
                except proto.UnsafeCommandError as ex:
                    print("跳过不安全指令: %s" % ex)

    @staticmethod
    def _is_timeout_error(ex):
        err = getattr(ex, "errno", None)
        if err in (errno.EAGAIN, errno.EWOULDBLOCK, errno.ETIMEDOUT):
            return True
        msg = str(ex).lower()
        return ("timed out" in msg or "timeout" in msg or
                "resource temporarily unavailable" in msg)

    def read_frames(self, duration, on_frame):
        """在 duration 秒内读取并解析帧，对每帧回调 on_frame(decoded)。"""
        parser = proto.FrameParser()
        deadline = time.time() + duration
        while time.time() < deadline:
            try:
                with self.io_lock:
                    if not self.sock:
                        break
                    chunk = self.sock.recv(1024)
            except Exception as ex:
                if self._is_timeout_error(ex):
                    continue  # 读超时，继续等
                break
            if not chunk:
                break
            frames = parser.feed(chunk)
            if self.raw:
                # 一次 socket recv 可能包含多条协议帧；按帧打印，避免把
                # 两个 ACK 拼在同一行而误认为只有一条。
                if frames:
                    for frame in frames:
                        print("[recv] %s" % bytes(frame).hex())
                else:
                    print("[recv] %s" % bytes(chunk).hex())
            for frame in frames:
                on_frame(proto.decode_frame(frame))

    def watch_frames(self, on_frame, stop_event=None):
        """持续读取帧直到 stop_event 被设置或连接断开。

        与 read_frames 不同，watch_frames 会一直阻塞循环读取，
        每次收到帧立即回调 on_frame(decoded)。
        适合 watch 模式或 GUI 后台线程使用。

        Args:
            on_frame: 每帧回调，接收 decoded dict。
            stop_event: threading.Event，设置后退出循环。
        """
        # 兼容早期计划文档中的 `(stop_event, on_frame)` 调用顺序，避免
        # GUI/外部脚本升级时因参数位置不同而静默失效。
        if (hasattr(on_frame, "is_set") and callable(stop_event)):
            on_frame, stop_event = stop_event, on_frame
        parser = proto.FrameParser()
        while not (stop_event and stop_event.is_set()):
            try:
                with self.io_lock:
                    if not self.sock:
                        break
                    chunk = self.sock.recv(4096)
            except Exception as ex:
                if stop_event and stop_event.is_set():
                    break
                if self._is_timeout_error(ex):
                    continue
                raise ConnectionError("蓝牙接收失败: %s" % ex) from ex
            if not chunk:
                break
            frames = parser.feed(chunk)
            if self.raw and frames:
                for raw_frame in frames:
                    print("[recv] %s" % bytes(raw_frame).hex())
            for raw_frame in frames:
                on_frame(proto.decode_frame(raw_frame))

    def close(self):
        with self.io_lock:
            sock = self.sock
            self.sock = None
        if sock:
            try:
                sock.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 读取流程
# ---------------------------------------------------------------------------
def do_read(args):
    conn = Connection(args.mac,
                      timeout=1.0, raw=args.raw and not args.json)
    st = DeviceState()
    st.mac = args.mac
    try:
        conn.connect(connect_timeout=getattr(args, "connect_timeout", 15.0))
    except Exception as ex:
        print(ex, file=sys.stderr)
        sys.exit(1)
    try:
        # 尝试获取设备名（只读）
        try:
            st.name = conn.bluetooth.lookup_name(args.mac, timeout=5)
        except Exception:
            pass

        conn.send_queries()

        if args.watch:
            if not args.json:
                print("持续监听中（Ctrl-C 退出）...\n")
            _watch_loop(conn, st, args)
        else:
            conn.read_frames(args.duration, st.consume)
            _output(st, args)
    except KeyboardInterrupt:
        print("\n已停止。", file=sys.stderr if args.json else sys.stdout)
    finally:
        conn.close()


def _watch_loop(conn, st, args):
    use_json = args.json

    def on_frame(d):
        st.consume(d)
        if use_json:
            print(json.dumps(_json_safe(d), ensure_ascii=False), flush=True)
        elif d.get("kind") == "MODULE":
            ts = time.strftime("%H:%M:%S")
            if (d["group"], d["enum"]) == (0x04, 0x0c):
                print("[%s] 电量: %s" % (ts, d["decoded"]))
            else:
                print("[%s] %s: %s" % (ts, d["name"], d["decoded"]))
    try:
        conn.watch_frames(on_frame)
    except KeyboardInterrupt:
        raise
    except ConnectionError as ex:
        # 监听期间设备异常断开：走用户可控的提示路径，不产生 traceback。
        # JSON 模式仍保持 stdout 纯净，断连提示输出到 stderr。
        print("连接已断开: %s" % ex, file=sys.stderr)


def _output(st, args):
    if args.json:
        print(json.dumps(_json_safe(st.to_dict()), ensure_ascii=False, indent=2))
    else:
        print_report(st)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(
        description="RoseLink 只读 CLI（仅读取能力与状态，不修改任何设置）")
    p.add_argument("--scan", action="store_true", help="扫描附近蓝牙设备")
    p.add_argument("--mac", help="目标耳机 MAC 地址，连接并读取")
    p.add_argument("--connect-timeout", type=float, default=15.0,
                   help="连接超时秒数（默认 15）")
    p.add_argument("--watch", action="store_true",
                   help="持续监听周期状态（Ctrl-C 退出）")
    p.add_argument("--duration", type=float, default=6.0,
                   help="一次性读取的采集秒数（默认 6）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.add_argument("--raw", action="store_true", help="打印收发原始 hex")
    args = p.parse_args(argv)

    if args.scan:
        do_scan()
        return 0
    if args.mac:
        do_read(args)
        return 0
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
