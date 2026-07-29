# -*- coding: utf-8 -*-
"""RoseLink rs_common_v2 写命令层（构造写帧；不连接、不发送）。

与 rs_protocol.py（只读解析层）配套。本模块负责构造所有 MK2 **已确认支持**
的写操作线格式帧；它**不**经过 rs_protocol 的只读白名单（ALLOWED_QUERIES）——
白名单护栏保护的是只读 reader，发送写命令是 writer 的显式职责。

线格式（手机 → 设备）:
    ff <seq> <mod_hi> <mod_lo> <val...> <cs> aa
    cs = (0xFF + seq + mod_hi + mod_lo + sum(val)) & 0xFF

所有命令字节均已从 rs_common_v2_sender.dart 逐条反汇编验证（field_f/2=mod_lo，
field_13 逻辑值=value），并与 docs/protocol.md 的 HCI 抓包交叉核对一致。
写入不需要 `01 fe`（dd 格式）前导；build_write_sequence() 只构造实际的 `ff`
命令帧。
"""

from __future__ import annotations

import rs_protocol as proto

# 复用只读层的常量与工具
CMD_START = proto.CMD_START
FRAME_END = proto.FRAME_END
checksum_ff = proto.checksum_ff
EQ_BANDS = proto.EQ_BANDS
TOUCH_POS = proto.TOUCH_POS
TOUCH_ACTION = proto.TOUCH_ACTION
decode_custom_eq = proto.decode_custom_eq
decode_module = proto.decode_module
module_name = proto.module_name


class WriteError(Exception):
    """写参数非法（越界、未知枚举等）。"""


# ---------------------------------------------------------------------------
# 写帧构造
# ---------------------------------------------------------------------------
def build_write_frame(seq, mod_hi, mod_lo, values=b""):
    """构造 ff 格式写帧。

    注意：本函数**不**经过 rs_protocol 的只读白名单（ALLOWED_QUERIES）——
    写模块（如 02 09 ANC 模式）本就不在白名单内。白名单护栏保护的是只读
    reader；writer 显式承担发送写命令的职责。
    """
    values = bytes(values)
    cs = checksum_ff(seq, mod_hi, mod_lo, values)
    return bytes([CMD_START, seq, mod_hi, mod_lo, *values, cs, FRAME_END])


def build_write_sequence(next_seq, op_frames):
    """构造一次写操作的完整线序列：每个命令段各对应一帧 ff。

    写入不发送 `01 fe`（dd 格式）前导。命令帧按构造顺序使用递增序号；自定义 EQ
    曲线本身产生一帧，进入自定义模式需另行执行 `eq custom`。

    Args:
        next_seq: 无参 callable，每次调用返回下一个序号并自增（见 Connection._next_seq）。
        op_frames: list of (mod_hi, mod_lo, values_bytes) —— WRITE_OPS[id]["build"] 的返回值。

    Returns:
        list of (label, frame_bytes)，每项都是实际发送的命令帧。
    """
    out = []
    for mod_hi, mod_lo, values in op_frames:
        seq = next_seq()
        out.append(("%02x %02x" % (mod_hi, mod_lo),
                    build_write_frame(seq, mod_hi, mod_lo, values)))
    return out


# ---------------------------------------------------------------------------
# 自定义 EQ 增益编码（decode_custom_eq 的逆）
# ---------------------------------------------------------------------------
def encode_eq_gains(gains):
    """10 段 dB 增益 → signed-magnitude 字节。

    频段顺序: 20Hz, 100Hz, 300Hz, 500Hz, 1kHz, 2kHz, 3kHz, 5kHz, 8kHz, 15kHz。

    编码: bit7 为符号位。正值=原值，负值=0x80|abs。
    每段有效范围 [-6, +6]（与 App 滑块一致），且必须恰好 10 段。
    """
    gains = list(gains)
    if len(gains) != 10:
        raise WriteError("自定义 EQ 需要恰好 10 段增益，得到 %d 段" % len(gains))
    out = bytearray()
    for i, g in enumerate(gains):
        if isinstance(g, bool) or not isinstance(g, int):
            raise WriteError("第 %d 段增益非整数: %r" % (i + 1, g))
        if g < -6 or g > 6:
            raise WriteError("第 %d 段增益 %d 超出范围 [-6, +6]" % (i + 1, g))
        out.append((0x80 | (-g)) if g < 0 else (g & 0x7F))
    return bytes(out)


# ---------------------------------------------------------------------------
# 各写操作的“值段”构造器（校验 + 返回 [(mod_hi, mod_lo, values), ...]）
# ---------------------------------------------------------------------------
def _eq_preset(preset):
    table = {"hifi": 0x00, "pop": 0x01, "rock": 0x02, "custom": 0x04}
    if preset not in table:
        raise WriteError("未知 EQ 预设 %r（可选 hifi/pop/rock/custom）" % preset)
    return [(0x02, 0x2a, bytes([table[preset]]))]


def _custom_eq(gains):
    eq_bytes = encode_eq_gains(gains)
    # 仅写入 0b 3e 数据；进入自定义模式由独立的 `eq custom` 完成。
    return [(0x0b, 0x3e, eq_bytes)]


def _ldac(state):
    table = {"on": 0x01, "off": 0x00}
    if state not in table:
        raise WriteError("LDAC 状态可选 on/off")
    return [(0x02, 0x2b, bytes([table[state]]))]


def _anc_mode(mode):
    table = {"anc": 0x01, "normal": 0x02, "transparency": 0x03, "wind": 0x04}
    if mode not in table:
        raise WriteError("未知 ANC 模式 %r（可选 anc/normal/transparency/wind）" % mode)
    return [(0x02, 0x09, bytes([table[mode]]))]


def _level(mod_hi, mod_lo, n, lo, hi, label):
    if isinstance(n, bool) or not isinstance(n, int) or n < lo or n > hi:
        raise WriteError("%s 需为 %d~%d 整数，得到 %r" % (label, lo, hi, n))
    return [(mod_hi, mod_lo, bytes([n]))]


def _anc_level(n):
    # CERAMICS-MK2 App 实际提供三档，线值为 1/3/5：轻/中/深。
    return _discrete_level(0x02, 0x2c, n, (1, 3, 5), "降噪等级")


def _trans_level(n):
    # CERAMICS-MK2 App 实际提供三档，线值为 1/3/5：舒适/人声/标准。
    return _discrete_level(0x02, 0x2d, n, (1, 3, 5), "通透等级")


def _discrete_level(mod_hi, mod_lo, n, allowed, label):
    if isinstance(n, bool) or not isinstance(n, int) or n not in allowed:
        choices = "/".join(str(v) for v in allowed)
        raise WriteError("%s 需为 %s 之一，得到 %r" % (label, choices, n))
    return [(mod_hi, mod_lo, bytes([n]))]


def _anc_cycle(anc, wind, normal, transparency):
    # 字节序 [降噪, 风噪, 普通, 通透]（抓包#8 联立 + 实机校准）
    bs = [anc, wind, normal, transparency]
    names = ["降噪", "风噪", "普通", "通透"]
    for i, b in enumerate(bs):
        if b not in (0, 1):
            raise WriteError("ANC 循环「%s」开关需为 0/1，得到 %r" % (names[i], b))
    return [(0x05, 0x36, bytes(bs))]


def _on_off(mod_hi, mod_lo, on_val, off_val, state, label):
    if state == "on":
        v = on_val
    elif state == "off":
        v = off_val
    else:
        raise WriteError("%s 状态可选 on/off" % label)
    return [(mod_hi, mod_lo, bytes([v]))]


def _game_mode(state):
    return _on_off(0x02, 0x0e, 0x01, 0x00, state, "游戏模式")


def _touch(state):
    # 注意：触控 ON=00, OFF=01（与多数开关相反）
    return _on_off(0x02, 0x07, 0x00, 0x01, state, "触控开关")


def _language(lang):
    table = {"cn": 0x00, "en": 0x01}
    if lang not in table:
        raise WriteError("语音语言可选 cn/en")
    return [(0x02, 0x31, bytes([table[lang]]))]


def _prompt_tone(n):
    # 0=关，1~5=开+音量；参数原样下发
    return _level(0x02, 0x2e, n, 0, 5, "提示音音量")


def _find(target):
    # setFindDevice arg 原样(1/2/3)；setFindOff 字面量 8→线格式 04
    table = {"left": 0x01, "right": 0x02, "both": 0x03, "off": 0x04}
    if target not in table:
        raise WriteError("查找耳机可选 left/right/both/off")
    return [(0x02, 0x2f, bytes([table[target]]))]


def _multi_device(state):
    return _on_off(0x02, 0x32, 0x01, 0x00, state, "多设备")


def _gesture(pos, action):
    if pos not in TOUCH_POS:
        raise WriteError("未知手势位置 0x%02x（见 rs_protocol.TOUCH_POS）" % pos)
    if action not in TOUCH_ACTION:
        raise WriteError("未知手势动作 0x%02x（见 rs_protocol.TOUCH_ACTION）" % action)
    return [(0x03, 0x01, bytes([pos, action]))]


# ---------------------------------------------------------------------------
# 写操作注册表
#   build(**kwargs) -> [(mod_hi, mod_lo, values), ...]
#   reboot:         True 表示会触发耳机重启（默认需交互确认，--force 跳过）
#   hard_precond:   "out_of_case" 表示硬前置（读电量帧拦截，--force 跳过）
#   precond_hint:   软提示（打印但不拦截）
# ---------------------------------------------------------------------------
WRITE_OPS = {
    "eq": {
        "desc": "EQ 预设切换（HIFI/POP/ROCK/CUSTOM）",
        "build": lambda **kw: _eq_preset(kw["preset"]),
    },
    "custom-eq": {
        "desc": "自定义 EQ 曲线写入（仅写 10 段增益数据；请先执行 eq custom）",
        "build": lambda **kw: _custom_eq(kw["gains"]),
    },
    "ldac": {
        "desc": "LDAC 无损音频协议开关（⚠ 重启；⚠ 须两只耳机出仓）",
        "reboot": True,
        "hard_precond": "out_of_case",
        "build": lambda **kw: _ldac(kw["state"]),
    },
    "anc-mode": {
        "desc": "ANC 模式（anc/normal/transparency/wind）",
        "build": lambda **kw: _anc_mode(kw["mode"]),
    },
    "anc-level": {
        "desc": "降噪等级（可选 1/3/5；轻/中/深）",
        "precond_hint": "降噪等级仅在 ANC 处于「降噪」模式时生效",
        "build": lambda **kw: _anc_level(kw["level"]),
    },
    "trans-level": {
        "desc": "通透等级（可选 1/3/5；舒适/人声/标准）",
        "precond_hint": "通透等级仅在 ANC 处于「通透」模式时生效",
        "build": lambda **kw: _trans_level(kw["level"]),
    },
    "anc-cycle": {
        "desc": "降噪触控循环列表（顺序: 降噪 风噪 普通 通透，各 0/1）",
        "build": lambda **kw: _anc_cycle(kw["anc"], kw["wind"], kw["normal"], kw["transparency"]),
    },
    "game-mode": {
        "desc": "游戏模式开关",
        "build": lambda **kw: _game_mode(kw["state"]),
    },
    "touch": {
        "desc": "触控开关（注意 ON=00 / OFF=01）",
        "build": lambda **kw: _touch(kw["state"]),
    },
    "gesture": {
        "desc": "触控手势配置（位置 + 动作，均以 hex 给出，如 01 03）",
        "build": lambda **kw: _gesture(kw["pos"], kw["action"]),
    },
    "language": {
        "desc": "语音语言（cn/en）",
        "build": lambda **kw: _language(kw["lang"]),
    },
    "prompt-tone": {
        "desc": "提示音音量（0=关，1~5）",
        "build": lambda **kw: _prompt_tone(kw["level"]),
    },
    "find": {
        "desc": "查找耳机（left/right/both/off=停止）",
        "build": lambda **kw: _find(kw["target"]),
    },
    "multi-device": {
        "desc": "多设备连接开关（⚠ 重启；⚠ 须两只耳机出仓）",
        "reboot": True,
        "hard_precond": "out_of_case",   # App 实测：开/关多设备亦须两耳出仓，与 LDAC(02 2b) 同款前置
        "build": lambda **kw: _multi_device(kw["state"]),
    },
}
