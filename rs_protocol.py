# -*- coding: utf-8 -*-
"""RoseLink rs_common_v2 协议解析层（只读）。

依据 docs/protocol.md 与 docs/overview.md 还原的协议常量、校验和、帧拆分与响应解码器。
本模块**只做解析与只读查询帧构造**，不提供任何写入/设置设备的能力。

线格式（设备 → 手机，经 RFCOMM socket 收到时已被 BlueZ 剥离外层）:
    dd <seq> <type> <data...> <checksum> aa
    checksum = (0xDD + seq + type + sum(data)) & 0xFF

只读查询帧（手机 → 设备）:
    ff <seq> <mod_hi> <mod_lo> <values...> <checksum> aa   (1e fa / 02 fa)
    checksum = (0xFF + seq + mod_hi + mod_lo + sum(values)) & 0xFF
    dd <seq> 01 fe <checksum> aa                            (写命令前导 01 fe，App 在下发写命令前先发一帧)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 帧标记
# ---------------------------------------------------------------------------
CMD_START = 0xFF          # ff 命令起始（手机→设备）
RESP_START = 0xDD         # dd 响应起始（设备→手机）
FRAME_END = 0xAA          # 包结束标记

# ---------------------------------------------------------------------------
# 频段（自定义 EQ 10 段）
# ---------------------------------------------------------------------------
EQ_BANDS = ["20Hz", "100Hz", "300Hz", "500Hz", "1kHz",
            "2kHz", "3kHz", "5kHz", "8kHz", "15kHz"]

# ---------------------------------------------------------------------------
# 触控手势位置 / 动作（模块 03 01 + init 触控块）
# ---------------------------------------------------------------------------
TOUCH_POS = {
    0x01: "左耳-单击", 0x02: "左耳-双击", 0x03: "左耳-三击",
    0x04: "左耳-长按",
    0x11: "右耳-单击", 0x12: "右耳-双击", 0x13: "右耳-三击",
    0x14: "右耳-长按",
}
# init 触控块中位置的固定顺序；05/15 是 App 不使用的预留槽位，仍须消费以保持解析偏移。
TOUCH_POS_ORDER = [0x01, 0x02, 0x03, 0x04, 0x05, 0x11, 0x12, 0x13, 0x14, 0x15]

TOUCH_ACTION = {
    0x00: "无作用", 0x01: "播放/暂停", 0x02: "上一曲", 0x03: "下一曲",
    0x04: "增大音量", 0x05: "降低音量", 0x06: "游戏模式",
    0x07: "语音助手", 0x08: "降噪控制",
}


# ---------------------------------------------------------------------------
# 值解码器（单字节值 → 中文语义）
# ---------------------------------------------------------------------------
def _map(d):
    """返回一个把单字节值映射为文字的解码器（未知值回退为 raw hex）。"""
    def dec(values):
        if len(values) != 1:
            return _hex(values)
        v = values[0]
        return d.get(v, "未知(0x%02x)" % v)
    return dec


def _hex(values):
    return " ".join("%02x" % b for b in values)


def _dec_prompt_tone(values):
    if len(values) != 1:
        return _hex(values)
    v = values[0]
    if v == 0:
        return "关闭"
    if 1 <= v <= 5:
        return "开启-音量%d" % v
    return "未知(0x%02x)" % v


def decode_battery(values):
    """周期状态 04 0c：3 字节 [左耳, 右耳, 仓]。

    低 7 位=电量百分比; bit7=**在仓标志**（App 内部命名"人仓L/R"，=1 即在仓充电，
    物理上充电与入仓同状态）—— LDAC（02 2b）切换的前置条件即此位为 0（出仓）；
    第三字节整字节=仓电量。返回键仍叫 *_charging 以兼容旧版 reader 输出。
    """
    if len(values) < 3:
        return {"raw": _hex(values)}
    l, r, case = values[0], values[1], values[2]
    return {
        "left_pct": l & 0x7F,
        "left_charging": bool(l & 0x80),   # bit7 = 在仓标志（=充电中）
        "right_pct": r & 0x7F,
        "right_charging": bool(r & 0x80),
        "case_pct": case,
        "case_cached": not (l & 0x80) and not (r & 0x80),
    }


def _dec_battery_str(values):
    b = decode_battery(values)
    if "raw" in b:
        return b["raw"]
    case_suffix = "(缓存)" if b.get("case_cached") else ""
    return "左 %d%%%s / 右 %d%%%s / 仓 %d%%%s" % (
        b["left_pct"], "(充电中)" if b["left_charging"] else "",
        b["right_pct"], "(充电中)" if b["right_charging"] else "",
        b["case_pct"], case_suffix,
    )


def decode_firmware(values):
    """固件版本 04 0d：3 字节按十进制拼接，01 05 05 → v155。"""
    if len(values) < 3:
        return _hex(values)
    return "v%d%d%d" % (values[0], values[1], values[2])


def decode_custom_eq(values):
    """自定义 EQ 0b 3e：10 段 signed-magnitude 增益（dB）。

    编码: bit7 为符号位，低 7 位为幅度。正值直接为原值，负值为 0x80|abs。
    实际有效范围 **+6 ~ -6 dB**（每频段最大 +6、最小 -6，超出范围固件不会接受）：
        0x00=0dB, 0x01~0x06=+1~+6dB, 0x81~0x86=-1~-6dB。
    """
    gains = []
    for b in values[:10]:
        gains.append(-(b & 0x7F) if (b & 0x80) else b)
    return gains


def _dec_custom_eq_str(values):
    gains = decode_custom_eq(values)
    return ", ".join("%s%+ddB" % (EQ_BANDS[i], g) if i < len(EQ_BANDS)
                     else "%+ddB" % g for i, g in enumerate(gains))


def decode_anc_list(values):
    """降噪循环列表 05 36：4 字节 bitmask [降噪, 风噪, 普通, 通透]。
    字节序由抓包 #8（其他小设置步骤8）四种组合联立反推 + 实机校准得出。"""
    labels = ["降噪", "风噪", "普通", "通透"]
    if len(values) < 4:
        return _hex(values)
    on = [labels[i] for i in range(4) if values[i]]
    return "循环含: " + ("、".join(on) if on else "无")


# ---------------------------------------------------------------------------
# 模块字典: (group, enum) -> {name, vlen, decode}
#   vlen: value 段字节数（用于 init TLV 逐条切分）
#   decode: values(bytes) -> str | 结构
# ---------------------------------------------------------------------------
MODULES = {
    (0x02, 0x07): {"name": "触控开关", "vlen": 1,
                   "decode": _map({0x00: "开启", 0x01: "关闭"})},
    (0x02, 0x08): {"name": "入耳检测(本机无此硬件, 占位)", "vlen": 1,
                   "decode": _map({0x00: "关", 0x01: "占位=01"})},
    (0x02, 0x09): {"name": "ANC 模式", "vlen": 1,
                   "decode": _map({0x01: "降噪", 0x02: "普通",
                                   0x03: "通透", 0x04: "风噪"})},
    (0x02, 0x0e): {"name": "游戏模式", "vlen": 1,
                   "decode": _map({0x00: "关闭", 0x01: "开启"})},
    (0x02, 0x12): {"name": "硬件能力标记", "vlen": 1,
                   "decode": _map({0x01: "01(能力位)"})},
    (0x02, 0x2a): {"name": "EQ 预设", "vlen": 1,
                   "decode": _map({0x00: "HIFI", 0x01: "POP",
                                   0x02: "ROCK", 0x03: "清亮(Light, 本机UI未提供)",
                                   0x04: "自定义(使用 0b 3e 自定义曲线)",
                                   0xa0: "游戏1", 0xa1: "游戏2", 0xa2: "游戏3",
                                   0xa3: "游戏4", 0xa4: "游戏5"})},
    (0x02, 0x2b): {"name": "连接模式(编解码)", "vlen": 1,
                   "decode": _map({0x00: "AAC/SBC", 0x01: "LDAC"})},
    (0x02, 0x2c): {"name": "降噪等级", "vlen": 1,
                   "decode": _map({0x01: "轻度降噪", 0x03: "中度降噪",
                                   0x05: "深度降噪"})},
    (0x02, 0x2d): {"name": "通透等级", "vlen": 1,
                   "decode": _map({0x01: "舒适通透", 0x03: "人声通透",
                                   0x05: "标准通透"})},
    (0x02, 0x2e): {"name": "提示音音量", "vlen": 1, "decode": _dec_prompt_tone},
    (0x02, 0x2f): {"name": "查找耳机", "vlen": 1,
                   "decode": _map({0x00: "关闭", 0x04: "0x04"})},
    (0x02, 0x31): {"name": "语音语言", "vlen": 1,
                   "decode": _map({0x00: "中文", 0x01: "英文"})},
    (0x02, 0x32): {"name": "多设备连接", "vlen": 1,
                   "decode": _map({0x00: "关闭/未连接", 0x01: "开启/已连接"})},
    (0x02, 0x33): {"name": "空间音频(本机不支持)", "vlen": 1,
                   "decode": _map({0x00: "关闭"})},
    (0x03, 0x01): {"name": "触控手势", "vlen": 2, "decode": None},
    (0x04, 0x0c): {"name": "电量状态", "vlen": 3, "decode": _dec_battery_str},
    (0x04, 0x0d): {"name": "固件版本", "vlen": 3, "decode": decode_firmware},
    (0x05, 0x36): {"name": "降噪循环列表", "vlen": 4, "decode": decode_anc_list},
    (0x0b, 0x3e): {"name": "自定义 EQ(10段)", "vlen": 10,
                   "decode": _dec_custom_eq_str},
}


def module_name(group, enum):
    m = MODULES.get((group, enum))
    return m["name"] if m else "未知模块 %02x %02x" % (group, enum)


def module_vlen(group, enum):
    """返回模块 value 段字节数（用于 init TLV 切分）。未知模块默认 1。"""
    m = MODULES.get((group, enum))
    if m:
        return m["vlen"]
    return 1


def decode_module(group, enum, values):
    """把 (group, enum, values) 解码为可读字符串。"""
    m = MODULES.get((group, enum))
    if m and m["decode"]:
        return m["decode"](values)
    if (group, enum) == (0x03, 0x01) and len(values) >= 2:
        pos, act = values[0], values[1]
        return "%s → %s" % (TOUCH_POS.get(pos, "pos%02x" % pos),
                            TOUCH_ACTION.get(act, "act%02x" % act))
    return _hex(values)


# ---------------------------------------------------------------------------
# CERAMICS-MK2 静态能力清单（来自 docs/protocol.md 已验证结论）
# ---------------------------------------------------------------------------
SUPPORTED = [
    "降噪/通透/风噪模式 (02 09)", "降噪等级 轻/中/深 (02 2c)",
    "通透等级 (02 2d)", "游戏模式 (02 0e)", "LDAC 高音质切换 (02 2b)",
    "自定义 EQ 10 段 (0b 3e)", "EQ 预设 (02 2a)", "AAC/SBC 编解码",
    "触控开关 (02 07)", "触控手势配置 (03 01)", "提示音音量 (02 2e)",
    "语音语言 (02 31)", "查找耳机 (02 2f)", "多设备 (02 32)",
    "降噪触控循环 (05 36)",
]
UNSUPPORTED = [
    "入耳检测 (本机无硬件, init 恒报 01 占位)",
    "空间音频 / 头部追踪 / 空间音频模式",
    "耳道自适应", "轻点开关", "听力保护", "独立麦克风模式",
    "aptX / aptX HD / aptX Adaptive / LHDC / LC3 编解码",
]


# ---------------------------------------------------------------------------
# 校验和
# ---------------------------------------------------------------------------
def checksum_ff(seq, mod_hi, mod_lo, values=b""):
    return (CMD_START + seq + mod_hi + mod_lo + sum(values)) & 0xFF


def checksum_dd(seq, type_byte, data=b""):
    return (RESP_START + seq + type_byte + sum(data)) & 0xFF


def verify_response_checksum(frame):
    """校验一个完整的 dd..aa 帧。frame[-2] 为 checksum，覆盖 dd..最后一个 data 字节。"""
    if len(frame) < 5 or frame[0] != RESP_START or frame[-1] != FRAME_END:
        return False
    calc = sum(frame[0:-2]) & 0xFF
    return calc == frame[-2]


# ---------------------------------------------------------------------------
# 只读查询帧构造 + 发送白名单
# ---------------------------------------------------------------------------
# 允许发送的模块 ID（均为非破坏性查询）。任何不在此集合的模块禁止发送。
ALLOWED_QUERIES = {
    (0x1e, 0xfa),   # 能力查询（触发 TLV 上报）
    (0x02, 0xfa),   # 子命令查询（val=3e 查自定义 EQ）
    (0x01, 0xfe),   # 写命令前导（App 在写命令前先发；本只读工具默认不发）
}


class UnsafeCommandError(Exception):
    """尝试发送不在只读白名单内的模块时抛出。"""


# 能力查询 1e fa 携带的模块枚举清单（抓包 #7 实测，App 用它触发完整 TLV 上报）。
# 仅告知设备“上报哪些模块”，不修改任何设置，属只读查询。
CAPABILITY_QUERY_MODULES = bytes([
    0x01, 0x07, 0x08, 0x09, 0x0c, 0x0d, 0x0e, 0x12,
    0x2a, 0x2b, 0x2c, 0x2d, 0x2e, 0x2f, 0x31, 0x32, 0x33,
    0x36, 0x37, 0x38, 0x39, 0x3a, 0x3b, 0x3c, 0x3d, 0x3f,
    0x45, 0x46, 0x49,
])


def build_query(seq, mod_hi, mod_lo, values=b""):
    """构造只读查询帧。仅允许 ALLOWED_QUERIES 中的模块，否则抛异常。

    - (0x01, 0xfe) 使用 dd 格式: dd seq 01 fe cs aa
    - 其余使用 ff 格式: ff seq mod_hi mod_lo values cs aa
    """
    if (mod_hi, mod_lo) not in ALLOWED_QUERIES:
        raise UnsafeCommandError(
            "拒绝发送非只读模块 %02x %02x（只读白名单外）" % (mod_hi, mod_lo))
    values = bytes(values)
    if (mod_hi, mod_lo) == (0x01, 0xfe):
        cs = checksum_dd(seq, mod_hi, bytes([mod_lo]))
        return bytes([RESP_START, seq, mod_hi, mod_lo, cs, FRAME_END])
    cs = checksum_ff(seq, mod_hi, mod_lo, values)
    return bytes([CMD_START, seq, mod_hi, mod_lo, *values, cs, FRAME_END])


def build_capability_query(seq):
    return build_query(seq, 0x1e, 0xfa, CAPABILITY_QUERY_MODULES)


def build_custom_eq_query(seq):
    return build_query(seq, 0x02, 0xfa, bytes([0x3e]))


def build_prepare(seq):
    """构造 `01 fe` 的 H2C 帧，仅供协议实验使用。

    设备→手机方向的同形帧是 ACK。历史 App 抓包未见该 H2C 帧，且写入工具
    不需要、也不会在写命令前发送它；不得把本函数用于常规写入流程。
    """
    return build_query(seq, 0x01, 0xfe)


# ---------------------------------------------------------------------------
# 帧拆分（流式）
# ---------------------------------------------------------------------------
class FrameParser:
    """从字节流中按 dd..aa + 校验和切分响应帧。

    data 中可能含 0xAA，因此采用“定位 0xDD 起始 + 逐个候选 0xAA 结束 + 校验和验证”
    的方式，只接受校验和匹配的帧，天然跳过 data 内的伪 0xAA。
    """

    def __init__(self):
        self._buf = bytearray()

    def feed(self, chunk):
        """喂入新字节，返回本次可解析出的完整帧列表（bytes）。"""
        self._buf.extend(chunk)
        frames = []
        while True:
            frame = self._extract_one()
            if frame is None:
                break
            frames.append(frame)
        return frames

    def _extract_one(self):
        buf = self._buf
        # 定位起始 0xDD，丢弃前面的噪声
        start = buf.find(RESP_START)
        if start < 0:
            # 无起始标记，保留最后 0 字节
            buf.clear()
            return None
        if start > 0:
            del buf[:start]
        # 最短帧: dd seq type cs aa = 5 字节
        if len(buf) < 5:
            return None
        # 逐个候选结束 0xAA 位置 j（j-1 为 checksum）
        for j in range(4, len(buf)):
            if buf[j] != FRAME_END:
                continue
            candidate = bytes(buf[0:j + 1])
            if verify_response_checksum(candidate):
                del buf[:j + 1]
                return candidate
        # 未找到有效帧：可能帧还没收全，继续等待
        # 防止缓冲无限增长：若已有另一个 0xDD 起始，且当前起始很久未闭合，则丢弃当前起始
        nxt = buf.find(RESP_START, 1)
        if nxt > 0 and len(buf) > 512:
            del buf[:nxt]
            return self._extract_one()
        return None


# ---------------------------------------------------------------------------
# 响应帧解码
# ---------------------------------------------------------------------------
def decode_init(type_byte, data):
    """解码能力上报（type 0x15 / 0x03）: 触控默认配置 + 模块 TLV 列表。"""
    idx = 0
    result = {"touch": [], "modules": []}

    # type 0x03 旧格式带长度前缀
    if type_byte == 0x03 and len(data) >= 1:
        idx = 1

    # 触控块: 标记 0x01 + 10 个 (pos, action) 对，位置顺序固定
    if _looks_like_touch_block(data, idx):
        idx += 1  # 跳过标记
        for _ in range(len(TOUCH_POS_ORDER)):
            if idx + 1 >= len(data):
                break
            pos, act = data[idx], data[idx + 1]
            result["touch"].append((pos, act))
            idx += 2

    # 模块 TLV 列表: <group> <enum> <value(vlen)>
    while idx + 1 < len(data):
        group, enum = data[idx], data[idx + 1]
        vlen = module_vlen(group, enum)
        value = data[idx + 2: idx + 2 + vlen]
        if len(value) < vlen:
            break  # 数据不足，停止（防止误解析）
        result["modules"].append((group, enum, bytes(value)))
        idx += 2 + vlen
    return result


def _looks_like_touch_block(data, idx):
    """判断 data[idx] 起是否为 init 触控块（标记 0x01 + 固定顺序 10 对）。"""
    if idx >= len(data) or data[idx] != 0x01:
        return False
    need = 1 + len(TOUCH_POS_ORDER) * 2
    if idx + need > len(data):
        return False
    for i, expect_pos in enumerate(TOUCH_POS_ORDER):
        if data[idx + 1 + i * 2] != expect_pos:
            return False
    return True


def decode_frame(frame):
    """解码单个完整 dd..aa 帧，返回结构化 dict。"""
    seq = frame[1]
    type_byte = frame[2]
    data = frame[3:-2]  # type 与 checksum 之间
    out = {
        "seq": seq,
        "type": type_byte,
        "raw": " ".join("%02x" % b for b in frame),
        "checksum_ok": verify_response_checksum(frame),
    }

    if type_byte == 0x01:
        out["kind"] = "ACK"
        out["desc"] = "确认 (%s)" % _hex(data)
    elif type_byte in (0x15, 0x03):
        out["kind"] = "INIT"
        out["init"] = decode_init(type_byte, data)
    elif type_byte in (0x02, 0x04, 0x05, 0x0b) and len(data) >= 1:
        enum = data[0]
        values = bytes(data[1:])
        out["kind"] = "MODULE"
        out["group"] = type_byte
        out["enum"] = enum
        out["values"] = values
        out["name"] = module_name(type_byte, enum)
        out["decoded"] = decode_module(type_byte, enum, values)
    else:
        out["kind"] = "UNKNOWN"
        out["desc"] = _hex(data)
    return out
