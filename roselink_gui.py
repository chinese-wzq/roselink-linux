#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RoseLink CERAMICS-MK2 耳机控制 GUI (flet).

使用方式:
    source .venv/bin/activate
    python3 roselink_gui.py

不修改 roselink_reader / roselink_writer / rs_protocol / rs_writer 等现有文件。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import flet as ft
from flet.canvas import Canvas, Line as CanvasLine, Circle as CanvasCircle, \
    Text as CanvasText, Path as CanvasPath

import rs_protocol as proto
import rs_writer as wproto
import roselink_reader as reader

# ── 常量 ──────────────────────────────────────────────────────────────────
EQ_PRESETS_PATH = os.path.expanduser("~/.config/roselink/eq_presets.json")
ACK_TIMEOUT = 1.5
WATCH_TIMEOUT = 0.5


# ── 面板级扫描（不依赖 roselink_reader.do_scan——那是打印函数） ────────────
def _scan_devices():
    """通过 bluetoothctl 列出已配对设备。

    Returns:
        [(mac, name, is_connected), ...]  成功
        None                               调用失败
    """
    try:
        out = subprocess.run(["bluetoothctl", "devices"],
                             capture_output=True, text=True, timeout=10)
        conn = subprocess.run(["bluetoothctl", "devices", "Connected"],
                              capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.SubprocessError):
        return None

    def parse(text):
        result = []
        for line in text.splitlines():
            parts = line.split(" ", 2)
            if len(parts) >= 3 and parts[0] == "Device":
                result.append((parts[1], parts[2]))
        return result

    connected = {a for a, _ in parse(conn.stdout)}
    return [(addr, name, addr in connected) for addr, name in parse(out.stdout)]


# ── EQ 预设管理 ───────────────────────────────────────────────────────────
def _load_eq_presets():
    try:
        with open(EQ_PRESETS_PATH) as f:
            data = json.load(f)
        return data.get("presets", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_eq_presets(presets):
    os.makedirs(os.path.dirname(EQ_PRESETS_PATH), exist_ok=True)
    with open(EQ_PRESETS_PATH, "w") as f:
        json.dump({"presets": presets}, f, ensure_ascii=False, indent=2)


_ANC_MODES = ["anc", "normal", "transparency", "wind"]
_ANC_MODE_LABELS = ["降噪", "普通", "通透", "风噪"]

_ANC_LEVELS = [1, 3, 5]
_ANC_LEVEL_LABELS = ["轻", "中", "深"]

_TRANS_LEVELS = [1, 3, 5]
_TRANS_LEVEL_LABELS = ["舒适", "人声", "标准"]

_EQ_PRESET_KEYS = ["hifi", "pop", "rock", "custom"]
_EQ_PRESET_LABELS = ["HiFi", "Pop", "Rock", "Custom"]

_LANGUAGE_OPTIONS = [("cn", "中文"), ("en", "English")]

# ── EQ 曲线 Canvas 几何参数 ────────────────────────────────────────────────
_EQ_CANVAS_W = 440   # 适配半宽卡片（列宽约 490，卡片内容区约 455）
_EQ_CANVAS_H = 230   # 顶部预留安全区：拖到 +6dB 时悬标不超出画布
_EQ_PAD_L = 44   # Y 轴标签宽度
_EQ_PAD_R = 10
_EQ_PAD_T = 46   # 顶部安全区：手柄 r=9 + 悬标间距 20 + 文字高约 16 = 45
_EQ_PAD_B = 30   # X 轴标签高度
_EQ_PLOT_W = _EQ_CANVAS_W - _EQ_PAD_L - _EQ_PAD_R   # 386
_EQ_PLOT_H = _EQ_CANVAS_H - _EQ_PAD_T - _EQ_PAD_B   # 154
_EQ_COL_W = _EQ_PLOT_W // 10                         # 每列宽度

# 频段颜色（统一品牌粉；选中态加深为深玫红）
_EQ_COLORS = ["#F06292"] * 10
_EQ_COLORS_SEL = ["#AD1457"] * 10

# ── 主题配色（浅色简洁 + 淡粉品牌色） ──────────────────────────────────────
_BG = "#FAF7F9"            # 页面背景（极淡暖粉灰）
_CARD_BG = "#FFFFFF"       # 卡片背景
_BRAND = "#F06292"         # 品牌淡粉（主色）
_BRAND_DARK = "#AD1457"    # 品牌深玫红（选中/强调态）
_BRAND_TINT = "#FCEFF3"    # 极浅粉（分区底色）
_CONNECTING_BG = "#E0E0E0" # 连接中按钮灰底
_CONNECTING_FG = "#9E9E9E" # 连接中按钮灰字
_CARD_RADIUS = 12          # 卡片统一圆角
# 分区标题色（统一粉色系，靠明度梯度区分层次，避免色相冲突）
# 越重要的功能区用越深的玫红，次要区用越浅的粉。
_ACCENT = {
    "anc": "#AD1457",      # 降噪：深玫红（主功能，最强权重）
    "audio": "#E91E63",    # 音频：标准粉
    "interact": "#F06292", # 交互：品牌淡粉
    "conn": "#F48FB1",     # 连接：浅粉
}


def _alpha(hex_color: str, alpha: int) -> str:
    """给 #RRGGBB 颜色叠加透明度，返回 Flutter 期望的 #AARRGGBB 格式。

    ⚠️ Flutter/Flet 的 8 位 hex 是 **alpha 在前**（#AARRGGBB），
    不是 #RRGGBBAA。直接 `color + "14"` 会把 alpha 拼到末尾被当成
    蓝色通道，导致粉色错位解析成绿色。必须 alpha 放前面。
    """
    return f"#{alpha:02X}{hex_color[1:7].upper()}"


class RoseLinkApp:
    """主应用类。"""

    # ═══════════════════════════════════════════════════════════════════════
    # 初始化与 UI 构建
    # ═══════════════════════════════════════════════════════════════════════

    def __init__(self, page: ft.Page):
        self.page = page
        page.title = "RoseLink 耳机控制台"
        page.window.width = 1000
        page.window.height = 740
        page.window.min_width = 720
        page.window.min_height = 500
        # 浅色主题 + 淡粉品牌色
        page.theme_mode = ft.ThemeMode.LIGHT
        page.bgcolor = _BG
        page.theme = ft.Theme(
            color_scheme=ft.ColorScheme(
                primary=_BRAND, on_primary="white",
                secondary="#FFB6C1", on_secondary=_BRAND_DARK,
                surface=_CARD_BG, on_surface="#3D2B33",
                surface_container_low=_BRAND_TINT,
                outline="#E8C5D2",
            ),
        )
        page.scroll = ft.ScrollMode.AUTO

        # 线程同步
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        # Connection.io_lock 负责真实 socket 的串行读写；保留本地锁作为
        # fake connection/离线测试的兼容回退。
        self._sock_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._connect_cancel: threading.Event | None = None
        self._auto_scan_stop = threading.Event()
        self._auto_scan_thread: threading.Thread | None = None

        # 状态
        self.conn: reader.Connection | None = None
        self.state = reader.DeviceState()
        self._eq_presets = _load_eq_presets()
        self._devices: list[tuple[str, str, bool]] = []

        # 查找耳机状态跟踪
        self._find_left_active = False
        self._find_right_active = False

        # event loop 引用（watch 线程通过此桥接到主线程）
        self._loop = page.session.connection.loop
        # pubsub 仅用于非 watch 线程消息（扫描结果等）
        page.pubsub.subscribe(self._on_message)
        page.on_window_event = self._on_window_event

        # 控件引用（在 _build_* 中赋值）
        self._ref_dev_dropdown: ft.Dropdown | None = None
        self._ref_refresh_btn: ft.TextButton | None = None
        self._ref_connect_btn: ft.Button | None = None  # 连接/断开 合并切换按钮
        self._ref_dev_info: ft.Row | None = None
        self._ref_battery_row: ft.Row | None = None
        self._ref_bottom_status: ft.Text | None = None
        self._ref_anc_mode_sb: ft.SegmentedButton | None = None
        self._ref_anc_level_sb: ft.SegmentedButton | None = None
        self._ref_trans_level_sb: ft.SegmentedButton | None = None
        self._ref_anc_cycle_apply: ft.Button | None = None
        self._ref_eq_radio: ft.RadioGroup | None = None
        self._ref_custom_eq_section: ft.Column | None = None
        self._ref_eq_preset_dropdown: ft.Dropdown | None = None
        self._ref_apply_custom_eq: ft.Button | None = None
        self._ref_ldac_switch: ft.Switch | None = None
        self._ref_game_switch: ft.Switch | None = None
        self._ref_gesture_section: ft.Column | None = None
        self._ref_gesture_toggle: ft.TextButton | None = None
        self._ref_gesture_apply_all: ft.Button | None = None
        self._ref_gesture_reset: ft.Button | None = None
        self._ref_prompt_slider: ft.Slider | None = None
        self._ref_touch_switch: ft.Switch | None = None
        self._ref_multi_switch: ft.Switch | None = None
        self._ref_lang_sb: ft.SegmentedButton | None = None
        self._ref_find_left_btn: ft.GestureDetector | None = None
        self._ref_find_right_btn: ft.GestureDetector | None = None
        self._ref_prompt_slider: ft.Slider | None = None
        self._ref_anc_chks: list[ft.Checkbox] = []
        self._gesture_dropdowns: dict[int, ft.Dropdown] = {}

        # AD 级操作控件列表（连接/断开时统一 disabled/enabled）
        self._op_controls: list[ft.Control] = []

        # 自定义 EQ 曲线编辑器（数据 + Canvas + 选中态）
        self._eq_gains: list[int] = [0] * 10
        self._eq_selected: int | None = None
        self._eq_canvas: Canvas | None = None
        self._eq_curve_section: ft.Container | None = None
        self._eq_readout_texts: list[ft.Text] = []

        # 手势配置控件（用于禁用）——与 gesture section 同步
        self._gesture_controls: list[ft.Control] = []

        # 构建界面
        self._build()
        self._set_controls_enabled(False)
        self._start_auto_scan()
        page.update()

    # ── 整体布局 ──────────────────────────────────────────────────────────
    def _build(self):
        self.page.appbar = self._build_appbar()
        self.page.add(
            self._build_device_info(),
            self._build_battery_card(),
            ft.Divider(height=1),
            self._build_operations(),
        )
        self.page.bottom_appbar = self._build_bottom_bar()

    # ── AppBar ────────────────────────────────────────────────────────────
    def _build_appbar(self) -> ft.AppBar:
        self._ref_dev_dropdown = ft.Dropdown(
            hint_text="选择 MAC",
            width=280,
            options=[],
        )
        self._ref_refresh_btn = ft.TextButton(
            "刷新状态",
            icon=ft.Icons.REFRESH,
            icon_color=_BRAND,
            style=ft.ButtonStyle(
                color=_BRAND_DARK,
                padding=ft.Padding(left=10, right=10, top=4, bottom=4),
            ),
            on_click=lambda _: self._on_refresh(),
        )
        # 连接/断开合并为一个切换按钮，样式随状态切换
        self._ref_connect_btn = ft.Button(
            "连接", icon=ft.Icons.LINK,
            style=ft.ButtonStyle(bgcolor=_BRAND, color="white"),
            on_click=lambda _: self._on_conn_toggle(),
        )
        return ft.AppBar(
            title=ft.Row([
                ft.Icon(ft.Icons.HEADPHONES, color=_BRAND, size=28),
                ft.Text("RoseLink 耳机控制台", weight=ft.FontWeight.BOLD),
            ], spacing=8),
            bgcolor=_CARD_BG,
            actions=[
                self._ref_dev_dropdown,
                # AppBar actions 之间默认无间距，用 margin 补间隔
                ft.Container(self._ref_connect_btn, margin=ft.Margin(left=10)),
            ],
        )

    # ── Device Info ───────────────────────────────────────────────────────
    def _build_device_info(self) -> ft.Row:
        """设备信息行：名称/固件/MAC，带品牌色图标前缀。"""
        def item(icon, text):
            return ft.Row([
                ft.Icon(icon, size=16, color=_BRAND),
                ft.Text(text, color="#6B7280"),
            ], spacing=6)

        self._ref_dev_info = ft.Row([
            item(ft.Icons.LABEL, "名称: —"),
            item(ft.Icons.MEMORY, "固件: —"),
            item(ft.Icons.BLUETOOTH, "MAC: —"),
        ], spacing=24)
        return self._ref_dev_info

    # ── Battery Card ──────────────────────────────────────────────────────
    def _build_battery_card(self) -> ft.Card:
        """三耳电量卡片：图标 + 百分比 + 进度条，低电变红预警。"""
        self._batt_ui = {}  # key -> {"icon", "pct", "bar"}

        def ear_block(key, label, icon):
            ico = ft.Icon(icon, size=28, color=_BRAND)
            pct = ft.Text("—", size=20, weight=ft.FontWeight.BOLD, color="#3D2B33")
            bar = ft.ProgressBar(value=None, width=90, color=_BRAND,
                                 bgcolor="#F5E0E8", border_radius=4)
            self._batt_ui[key] = {"icon": ico, "pct": pct, "bar": bar}
            return ft.Column(
                [ico, pct, bar,
                 ft.Text(label, size=11, color=_BRAND_DARK)],
                horizontal_alignment=ft.Alignment.CENTER,
                spacing=6,
            )

        row = ft.Row([
            ear_block("l", "左耳", ft.Icons.EARBUDS),
            ft.VerticalDivider(thickness=1, color="#F0DCE5"),
            ear_block("r", "右耳", ft.Icons.EARBUDS),
            ft.VerticalDivider(thickness=1, color="#F0DCE5"),
            ear_block("case", "充电仓", ft.Icons.BATTERY_CHARGING_FULL),
        ], alignment=ft.MainAxisAlignment.CENTER, spacing=28)
        self._ref_battery_row = row
        return ft.Card(
            content=ft.Container(content=row, padding=16),
            elevation=2,
        )

    def _reset_battery_display(self):
        """电量卡片重置为占位符。"""
        for ui in self._batt_ui.values():
            ui["pct"].value = "—"
            ui["pct"].color = "#3D2B33"
            ui["bar"].value = None
            ui["icon"].color = _BRAND

    def _update_battery_display(self):
        """根据 state.battery 更新电量卡片（百分比 + 进度条 + 低电预警）。"""
        bat = self.state.battery
        if not bat or "left_pct" not in bat:
            return

        def apply(key, pct_key, charge_key=None, cached=False):
            ui = self._batt_ui.get(key)
            if not ui:
                return
            pct = bat.get(pct_key)
            if pct is None:
                return
            charging = bool(bat.get(charge_key)) if charge_key else False
            low = pct < 20
            color = ft.Colors.RED_400 if low else _BRAND
            ui["icon"].color = color
            ui["pct"].color = ft.Colors.RED_400 if low else "#3D2B33"
            suffix = " ⚡" if charging else (" (缓存)" if cached else "")
            ui["pct"].value = f"{pct}%{suffix}"
            ui["bar"].value = pct / 100
            ui["bar"].color = color

        apply("l", "left_pct", "left_charging")
        apply("r", "right_pct", "right_charging")
        apply("case", "case_pct", cached=bool(bat.get("case_cached")))

    # ── 分区卡片 ────────────────────────────────────────────────────────
    @staticmethod
    def _seg_style():
        """SegmentedButton 统一圆角样式。"""
        return ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8))

    def _section_card(self, title, icon, accent, body_controls, col_span=None):
        """统一的分区卡片：彩色标题条 + 内容区，圆角卡片。

        body_controls: 控件列表（不含标题），放入卡片内容区。
        col_span: ResponsiveRow 的 col 字典，如 {"xs": 12, "lg": 6}；None 时不参与响应式布局。
        """
        header = ft.Container(
            content=ft.Row([
                ft.Icon(icon, color=accent, size=20),
                ft.Text(title, size=16, weight=ft.FontWeight.BOLD, color=accent),
            ], spacing=8),
            padding=ft.Padding(left=16, top=12, right=16, bottom=10),
            bgcolor=_alpha(accent, 0x1A),  # accent 10% 半透明（alpha 必须在前）
            border_radius=ft.border_radius.BorderRadius.only(
                top_left=_CARD_RADIUS, top_right=_CARD_RADIUS),
        )
        body = ft.Container(
            content=ft.Column(body_controls, spacing=12),
            padding=16,
            border_radius=ft.border_radius.BorderRadius.only(
                bottom_left=_CARD_RADIUS, bottom_right=_CARD_RADIUS),
        )
        return ft.Card(
            content=ft.Column([header, body], spacing=0),
            elevation=2,
            col=col_span,
        )

    # ── 操作区（卡片网格） ──────────────────────────────────────────────
    def _build_operations(self) -> ft.ResponsiveRow:
        """四张卡片按两列瀑布流排布：各列内卡片紧密堆叠，自动上移填补空白。

        不再按「行」对齐（行高取最高卡片会把矮卡片下方留白），
        而是左右两列各自独立堆叠，窄窗口时（xs）自动变为单列。
        """
        card_anc = self._section_card(
            "降噪控制", ft.Icons.GRAPHIC_EQ, _ACCENT["anc"],
            self._build_anc_group())
        card_audio = self._section_card(
            "音频设置", ft.Icons.EQUALIZER, _ACCENT["audio"],
            self._build_audio_group())
        card_interact = self._section_card(
            "交互设置", ft.Icons.TOUCH_APP, _ACCENT["interact"],
            self._build_interaction_group())
        card_conn = self._section_card(
            "连接设置", ft.Icons.DEVICES_OTHER, _ACCENT["conn"],
            self._build_connection_group())
        col_left = ft.Column(
            [card_anc, card_interact], spacing=16,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            col={"xs": 12, "lg": 6})
        col_right = ft.Column(
            [card_audio, card_conn], spacing=16,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            col={"xs": 12, "lg": 6})
        return ft.ResponsiveRow(
            [col_left, col_right],
            spacing=16, run_spacing=16, expand=True,
            scroll=ft.ScrollMode.AUTO)

    # ── 降噪控制组 ────────────────────────────────────────────────────────
    def _build_anc_group(self):
        self._ref_anc_mode_sb = ft.SegmentedButton(
            allow_empty_selection=True,
            segments=[ft.Segment(value=v, label=ft.Text(l))
                      for v, l in zip(_ANC_MODES, _ANC_MODE_LABELS)],
            style=self._seg_style(),
            on_change=lambda e: self._on_anc_mode_change(e),
        )
        self._op_controls.append(self._ref_anc_mode_sb)

        self._ref_anc_level_sb = ft.SegmentedButton(
            allow_empty_selection=True,
            segments=[ft.Segment(value=str(v), label=ft.Text(l))
                      for v, l in zip(_ANC_LEVELS, _ANC_LEVEL_LABELS)],
            style=self._seg_style(),
            on_change=lambda e: self._on_anc_level_change(e),
        )
        self._op_controls.append(self._ref_anc_level_sb)

        self._ref_trans_level_sb = ft.SegmentedButton(
            allow_empty_selection=True,
            segments=[ft.Segment(value=str(v), label=ft.Text(l))
                      for v, l in zip(_TRANS_LEVELS, _TRANS_LEVEL_LABELS)],
            style=self._seg_style(),
            on_change=lambda e: self._on_trans_level_change(e),
        )
        self._op_controls.append(self._ref_trans_level_sb)

        anc_chk = ft.Checkbox(label="降噪")
        wind_chk = ft.Checkbox(label="风噪")
        normal_chk = ft.Checkbox(label="普通")
        trans_chk = ft.Checkbox(label="通透")
        self._ref_anc_chks = [anc_chk, wind_chk, normal_chk, trans_chk]
        self._ref_anc_cycle_apply = ft.Button(
            "应用", on_click=lambda _: self._on_anc_cycle_apply(
                anc_chk.value, wind_chk.value, normal_chk.value, trans_chk.value),
        )
        for c in self._ref_anc_chks:
            self._op_controls.append(c)
        self._op_controls.append(self._ref_anc_cycle_apply)

        return [
            ft.Text("降噪模式:"),
            self._ref_anc_mode_sb,
            ft.Text("降噪等级:"),
            self._ref_anc_level_sb,
            ft.Text("通透等级:"),
            self._ref_trans_level_sb,
            ft.Text("降噪循环:"),
            ft.Row([*self._ref_anc_chks, self._ref_anc_cycle_apply]),
        ]

    # ── 音频设置组 ────────────────────────────────────────────────────────
    def _build_audio_group(self):
        radio = ft.RadioGroup(
            content=ft.Column([
                ft.Radio(value=v, label=l)
                for v, l in zip(_EQ_PRESET_KEYS, _EQ_PRESET_LABELS)
            ]),
            on_change=self._on_eq_preset_change,
        )
        self._ref_eq_radio = radio
        self._op_controls.append(radio)

        self._ref_custom_eq_section = self._build_custom_eq_section()

        self._ref_ldac_switch = ft.Switch(
            label="LDAC",
            on_change=lambda e: self._on_apply_ldac(e.control.value),
        )
        self._op_controls.append(self._ref_ldac_switch)

        self._ref_game_switch = ft.Switch(
            label="游戏模式",
            on_change=lambda e: self._on_apply_game(e.control.value),
        )
        self._op_controls.append(self._ref_game_switch)

        return [
            radio,
            self._ref_custom_eq_section,
            self._ref_ldac_switch,
            self._ref_game_switch,
        ]

    # ── 自定义 EQ ─────────────────────────────────────────────────────────
    def _build_custom_eq_section(self):
        # ── Canvas 曲线图 ────────────────────────────────────────────────
        canvas = Canvas(
            [],
            width=_EQ_CANVAS_W,
            height=_EQ_CANVAS_H,
        )
        self._eq_canvas = canvas

        # ── 10 条全高透明拖拽列（GestureDetector，叠在 Canvas 上） ──────
        cols = []
        for i in range(10):
            detector = ft.GestureDetector(
                on_pan_start=lambda e, idx=i: self._on_band_pan_start(idx, e),
                on_pan_update=lambda e, idx=i: self._on_band_pan_update(idx, e),
                on_pan_end=lambda e, idx=i: self._on_band_pan_end(idx, e),
                content=ft.Container(
                    width=_EQ_COL_W,
                    height=_EQ_CANVAS_H,
                    bgcolor=None,  # 完全透明
                ),
            )
            cols.append(detector)

        curve_stack = ft.Stack(
            [canvas, *[ft.Container(c, left=_EQ_PAD_L + i * _EQ_COL_W, top=0)
                       for i, c in enumerate(cols)]],
            width=_EQ_CANVAS_W,
            height=_EQ_CANVAS_H,
        )

        # ── 底部 dB 读数行 ───────────────────────────────────────────────
        readouts = []
        for i in range(10):
            t = ft.Text(
                " 0", width=40, text_align=ft.TextAlign.CENTER,
                size=13, weight=ft.FontWeight.NORMAL,
                color=_EQ_COLORS[i],
            )
            readouts.append(t)
        self._eq_readout_texts = readouts
        readout_row = ft.Row(
            readouts,
            spacing=0,
            alignment=ft.MainAxisAlignment.START,
        )
        # 给读数行增加左侧缩进以对齐曲线图
        readout_wrapper = ft.Container(
            content=readout_row,
            padding=ft.Padding(left=_EQ_PAD_L - 14, top=2, right=0, bottom=4),
        )

        # ── 预设管理栏 ───────────────────────────────────────────────────
        self._ref_eq_preset_dropdown = ft.Dropdown(width=200)
        self._refresh_eq_preset_dropdown()
        btn_save = ft.Button("保存", on_click=lambda _: self._on_save_eq_preset())
        btn_save_as = ft.Button("另存为…", on_click=lambda _: self._on_save_as_eq_preset())
        btn_load = ft.Button("载入", on_click=lambda _: self._on_load_eq_preset())
        btn_delete = ft.Button("删除", on_click=lambda _: self._on_delete_eq_preset())
        self._ref_apply_custom_eq = ft.Button(
            "应用EQ", on_click=lambda _: self._on_apply_custom_eq(),
        )
        self._op_controls.append(self._ref_eq_preset_dropdown)
        self._op_controls.append(btn_save)
        self._op_controls.append(btn_save_as)
        self._op_controls.append(btn_load)
        self._op_controls.append(btn_delete)
        self._op_controls.append(self._ref_apply_custom_eq)
        for c in cols:
            self._op_controls.append(c)
        for t in readouts:
            self._op_controls.append(t)
        self._op_controls.append(canvas)

        section = ft.Column([
            curve_stack,
            readout_wrapper,
            ft.Row([
                self._ref_eq_preset_dropdown,
                btn_save, btn_save_as, btn_load, btn_delete, self._ref_apply_custom_eq,
            ], wrap=True, spacing=8),
        ], visible=False)
        self._ref_custom_eq_section = section
        self._eq_curve_section = section
        return section

    # ── 交互设置组 ────────────────────────────────────────────────────────
    def _build_interaction_group(self):
        self._ref_touch_switch = ft.Switch(
            label="触控",
            on_change=lambda e: self._on_apply_touch(e.control.value),
        )
        self._op_controls.append(self._ref_touch_switch)

        self._ref_gesture_toggle = ft.TextButton(
            "▸ 手势设置", on_click=self._on_toggle_gesture,
        )
        self._ref_gesture_section = self._build_gesture_section()
        self._op_controls.append(self._ref_gesture_toggle)

        self._ref_lang_sb = ft.SegmentedButton(
            allow_empty_selection=True,
            segments=[ft.Segment(value=v, label=ft.Text(l))
                      for v, l in _LANGUAGE_OPTIONS],
            style=self._seg_style(),
            on_change=lambda e: self._on_language_change(e),
        )
        self._op_controls.append(self._ref_lang_sb)

        self._ref_prompt_slider = ft.Slider(
            min=0, max=5, divisions=5, label="{value}",
            on_change_end=lambda e: self._on_apply_prompt_tone(e.control.value),
        )
        self._op_controls.append(self._ref_prompt_slider)

        # 查找耳机：左右耳独立卡片按钮，基于设备上报状态自动重置
        self._find_btn_ui = {}  # target -> {"box": Container, "icon": Icon}
        self._ref_find_left_btn = self._make_find_btn("left", "左耳")
        self._ref_find_right_btn = self._make_find_btn("right", "右耳")
        self._update_find_button_colors()
        self._op_controls.append(self._ref_find_left_btn)
        self._op_controls.append(self._ref_find_right_btn)

        return [
            self._ref_touch_switch,
            self._ref_gesture_toggle,
            self._ref_gesture_section,
            ft.Text("语音:"),
            self._ref_lang_sb,
            ft.Row([ft.Text("提示音音量:"), self._ref_prompt_slider]),
            ft.Text("查找耳机:"),
            ft.Row([self._ref_find_left_btn, self._ref_find_right_btn],
                   alignment=ft.MainAxisAlignment.CENTER, spacing=24),
        ]

    def _make_find_btn(self, target, label):
        """查找耳机卡片式按钮：图标 + 文字，激活态粉色高亮。"""
        icon = ft.Icon(ft.Icons.EARBUDS, size=30, color="#B0A0A8")  # 暖灰，与粉系协调
        box = ft.Container(
            content=ft.Column(
                [icon, ft.Text(label, size=12)],
                horizontal_alignment=ft.Alignment.CENTER,
                spacing=4,
            ),
            padding=12, width=90,
            border_radius=_CARD_RADIUS,
            bgcolor=_BRAND_TINT,
            tooltip=f"{label}查找",
        )
        self._find_btn_ui[target] = {"box": box, "icon": icon}
        return ft.GestureDetector(
            on_tap=lambda _: self._on_find_toggle(target),
            content=box,
        )

    def _update_find_button_colors(self):
        """根据左右耳查找状态更新按钮样式（激活 = 粉色填充 + 描边）。"""
        for target, active in (("left", self._find_left_active),
                               ("right", self._find_right_active)):
            ui = self._find_btn_ui.get(target)
            if not ui:
                continue
            ui["box"].bgcolor = _alpha(_BRAND, 0x1A) if active else _BRAND_TINT
            ui["box"].border = (
                ft.border.Border.all(2, _BRAND) if active else None)
            ui["icon"].color = _BRAND if active else "#B0A0A8"  # 暖灰

    def _on_find_toggle(self, target):
        """查找耳机按钮逻辑，对齐官方 App normal_ear_find.dart。

        由于固件查找功能存在已知问题，不做「双耳播→只停一侧」的智能优化，
        严格按 App 原始逻辑（绝对状态设置，02 2f 命令）：

          L 钮: leftActive→off(04) / !leftActive+rightActive→both(03) / else→left(01)
          R 钮: rightActive→off(04) / !rightActive+leftActive→both(03) / else→right(02)

        带乐观 UI 更新避免「点了没反应」的感知。
        """
        if target == "left":
            if self._find_left_active:
                self._find_left_active = False
                self._find_right_active = False
                self._execute_write("find", {"target": "off"})
            elif self._find_right_active:
                self._find_left_active = True
                self._find_right_active = True
                self._execute_write("find", {"target": "both"})
            else:
                self._find_left_active = True
                self._find_right_active = False
                self._execute_write("find", {"target": "left"})
        else:  # right
            if self._find_right_active:
                self._find_left_active = False
                self._find_right_active = False
                self._execute_write("find", {"target": "off"})
            elif self._find_left_active:
                self._find_left_active = True
                self._find_right_active = True
                self._execute_write("find", {"target": "both"})
            else:
                self._find_left_active = False
                self._find_right_active = True
                self._execute_write("find", {"target": "right"})
        self._update_find_button_colors()
        self.page.update()

    # ── 手势设置（默认折叠） ──────────────────────────────────────────────
    # 注意：曾使用 ft.Tabs + 嵌套 TabBar/TabBarView，在 visible=False（初始
    # 尺寸为 0）时会让 Flutter 引擎构造出非法变换矩阵，刷出
    #   [ERROR:flutter/flow/layers/transform_layer.cc(15)]
    #   TransformLayer is constructed with an invalid matrix.
    # 改为左右耳并排显示，彻底避开 TabBarView 的渲染路径。
    def _build_gesture_section(self):
        # 每耳固定宽度，左右真正并排（不用 wrap，避免窄窗口折行变成竖排）。
        # 200px 使手势区能在半宽交互卡片内并排显示。
        ear_width = 200
        def ear_col(title, positions):
            return ft.Container(
                width=ear_width,
                content=ft.Column([
                    ft.Text(title, weight=ft.FontWeight.BOLD),
                    self._build_gesture_tab(positions),
                ]),
            )
        self._ref_gesture_apply_all = ft.Button(
            "全部应用", on_click=lambda _: self._on_gesture_apply_all(),
        )
        self._ref_gesture_reset = ft.Button(
            "重置", on_click=lambda _: self._on_gesture_reset(),
        )
        self._op_controls.append(self._ref_gesture_apply_all)
        self._op_controls.append(self._ref_gesture_reset)
        col = ft.Column([
            ft.Row(
                controls=[
                    ear_col("左耳", [p for p in proto.TOUCH_POS if p <= 0x04]),
                    ear_col("右耳", [p for p in proto.TOUCH_POS if p >= 0x11]),
                ],
                alignment=ft.MainAxisAlignment.START,
                spacing=30,
            ),
            ft.Row([self._ref_gesture_apply_all, self._ref_gesture_reset]),
        ], visible=False)
        self._gesture_tabs = col
        return col

    def _build_gesture_tab(self, positions):
        rows = []
        for pos in positions:
            # TOUCH_POS 形如 "左耳-单击"；列标题已标耳别，这里只取动作名。
            full = proto.TOUCH_POS.get(pos, f"pos{pos:02x}")
            label = full.split("-", 1)[1] if "-" in full else full
            dd = ft.Dropdown(
                options=[ft.dropdown.Option(k, v) for k, v in
                         sorted(proto.TOUCH_ACTION.items())],
                width=140,
            )
            self._gesture_dropdowns[pos] = dd
            row = ft.Row(
                [ft.Text(label, width=50), dd],
                alignment=ft.MainAxisAlignment.START,
            )
            rows.append(row)
            self._op_controls.append(dd)
        return ft.Column(rows)

    # ── 连接设置组 ────────────────────────────────────────────────────────
    def _build_connection_group(self):
        self._ref_multi_switch = ft.Switch(
            label="多设备连接",
            on_change=lambda e: self._on_apply_multi_device(e.control.value),
        )
        self._op_controls.append(self._ref_multi_switch)

        return [self._ref_multi_switch]

    # ── BottomBar ─────────────────────────────────────────────────────────
    def _build_bottom_bar(self):
        self._ref_status_icon = ft.Icon(
            ft.Icons.LINK_OFF, color=ft.Colors.RED, size=18)
        self._ref_bottom_status = ft.Text("未连接", color=ft.Colors.RED)
        return ft.BottomAppBar(
            # 默认高度 80，减半为 40
            height=40,
            # 默认上下 padding 各 12，会挤压内容；改成 2 让按钮有足够空间
            padding=ft.Padding.symmetric(vertical=2, horizontal=16),
            content=ft.Row([
                ft.Row([self._ref_status_icon, self._ref_bottom_status],
                       spacing=8),
                self._ref_refresh_btn,  # 更新状态按钮放在状态栏右侧
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        )

    def _set_bottom_status(self, text, color=None, icon=None):
        """更新底部状态栏文字、颜色和图标。"""
        self._ref_bottom_status.value = text
        if color:
            self._ref_bottom_status.color = color
        if icon:
            self._ref_status_icon.icon = icon
            self._ref_status_icon.color = color or self._ref_status_icon.color
        self.page.update()

    # ═══════════════════════════════════════════════════════════════════════
    # 控件状态（连接/断开时统一切换）
    # ═══════════════════════════════════════════════════════════════════════

    def _reset_ui_to_idle(self):
        info = self._ref_dev_info
        if info:
            info.controls[0].controls[1].value = "名称: —"
            info.controls[1].controls[1].value = "固件: —"
            info.controls[2].controls[1].value = "MAC: —"
        self._reset_battery_display()
        if self._ref_anc_mode_sb:
            self._ref_anc_mode_sb.selected = []
        if self._ref_anc_level_sb:
            self._ref_anc_level_sb.selected = []
        if self._ref_trans_level_sb:
            self._ref_trans_level_sb.selected = []
        if self._ref_anc_chks:
            for chk in self._ref_anc_chks:
                chk.value = False
        if self._ref_eq_radio:
            self._ref_eq_radio.value = "hifi"
        if self._ref_custom_eq_section:
            self._ref_custom_eq_section.visible = False
        if self._eq_gains:
            self._eq_gains[:] = [0] * 10
        if self._eq_readout_texts:
            for t in self._eq_readout_texts:
                t.value = "+0"
        self._eq_selected = None
        if self._ref_ldac_switch:
            self._ref_ldac_switch.value = False
        if self._ref_game_switch:
            self._ref_game_switch.value = False
        if self._ref_touch_switch:
            self._ref_touch_switch.value = True
        if self._ref_lang_sb:
            self._ref_lang_sb.selected = ["cn"]
        if self._ref_multi_switch:
            self._ref_multi_switch.value = False
        if self._ref_prompt_slider:
            self._ref_prompt_slider.value = 0
        # 查找耳机状态重置
        self._find_left_active = False
        self._find_right_active = False
        self._update_find_button_colors()
        if self._gesture_dropdowns:
            for dd in self._gesture_dropdowns.values():
                dd.value = None

    def _set_controls_enabled(self, enabled: bool):
        for c in self._op_controls:
            c.disabled = not enabled

    # ═══════════════════════════════════════════════════════════════════════
    # pubsub 消息分发器
    # ═══════════════════════════════════════════════════════════════════════

    def _post(self, msg):
        """从任何线程安全地将消息投递到主线程 event loop 处理。"""
        self._loop.call_soon_threadsafe(self._handle_message, msg)

    def _on_message(self, msg):
        """pubsub 回调（主线程消息专用，如 scan_result）。"""
        # pubsub 可能通过多线程 executor 派发导致乱序，
        # watch 线程消息走 _post，不会到这里。
        self._handle_message(msg)

    def _handle_message(self, msg):
        t = msg.get("type")
        if t == "frame":
            source = msg.get("connection")
            if source is None or source is self.conn:
                self.state.consume(msg["decoded"])
                self._update_status(self.state)
        elif t == "connected":
            self._on_connected(msg["mac"], msg.get("connection"))
        elif t == "disconnected":
            self._on_disconnected(msg["reason"], msg.get("connection"))
        elif t == "connect_error":
            self._on_connect_error(msg["error"], msg.get("attempt"))
        elif t == "scan_result":
            self._on_scan_result(msg["devices"])
        elif t == "write_result":
            self._on_write_result(msg["success"], msg["msg"])
        elif t == "write_done":
            btn = self._find_trigger_btn(msg["btn_op"])
            if btn:
                btn.disabled = False
            self._set_controls_enabled(self.conn is not None)
        self.page.update()

    # ── 状态更新 ──────────────────────────────────────────────────────────
    def _update_status(self, state: reader.DeviceState):
        self.state = state

        info = self._ref_dev_info
        if info:
            name = state.name or "—"
            fw = state.firmware or "—"
            mac = state.mac or "—"
            info.controls[0].controls[1].value = f"名称: {name}"
            info.controls[1].controls[1].value = f"固件: {fw}"
            info.controls[2].controls[1].value = f"MAC: {mac}"

        self._update_battery_display()

        self._sync_controls_from_state()

    _ANC_MODE_TO_OP = {1: "anc", 2: "normal", 3: "transparency", 4: "wind"}
    _ANC_LEVEL_TO_OP = {1: "1", 3: "3", 5: "5"}
    _TRANS_LEVEL_TO_OP = {1: "1", 3: "3", 5: "5"}
    _EQ_VALUE_TO_OP = {0: "hifi", 1: "pop", 2: "rock", 4: "custom"}
    _LDAC_TO_OP = {0: False, 1: True}
    _GAME_TO_OP = {0: False, 1: True}
    _TOUCH_TO_OP = {0: True, 1: False}
    _LANG_TO_OP = {0: "cn", 1: "en"}
    _MULTI_TO_OP = {0: False, 1: True}

    def _update_find_state(self, values):
        """根据设备上报的 02 2f 值更新查找耳机按钮状态。
        
        01=左耳查找中, 02=右耳查找中, 03=双耳, 04=全部停止,
        05=左耳停止(右不变), 06=右耳停止(左不变)。
        """
        if not values:
            return
        v = values[0]
        if v == 0x01:
            self._find_left_active = True
            self._find_right_active = False
        elif v == 0x02:
            self._find_left_active = False
            self._find_right_active = True
        elif v == 0x03:
            self._find_left_active = True
            self._find_right_active = True
        elif v in (0x00, 0x04):
            self._find_left_active = False
            self._find_right_active = False
        elif v == 0x05:
            self._find_left_active = False
            # 右耳不变
        elif v == 0x06:
            self._find_right_active = False
            # 左耳不变
        self._update_find_button_colors()

    def _sync_controls_from_state(self):
        st = self.state
        mods = st.modules

        def value_of(key):
            """取模块的首个值字节；values 为空（异常帧）时返回 None，
            避免主线程消息处理因下标越界而中断。"""
            m = mods.get(key)
            if m and m["values"]:
                return m["values"][0]
            return None

        v = value_of((0x02, 0x09))
        if v is not None and self._ref_anc_mode_sb:
            self._ref_anc_mode_sb.selected = [self._ANC_MODE_TO_OP.get(v, "anc")]

        v = value_of((0x02, 0x2c))
        if v is not None and self._ref_anc_level_sb:
            self._ref_anc_level_sb.selected = [self._ANC_LEVEL_TO_OP.get(v, "1")]

        v = value_of((0x02, 0x2d))
        if v is not None and self._ref_trans_level_sb:
            self._ref_trans_level_sb.selected = [self._TRANS_LEVEL_TO_OP.get(v, "1")]

        m = mods.get((0x05, 0x36))
        if m and self._ref_anc_chks:
            vals = m["values"]
            for i in range(min(4, len(vals))):
                self._ref_anc_chks[i].value = bool(vals[i])

        v = value_of((0x02, 0x2a))
        if v is not None and self._ref_eq_radio:
            preset = self._EQ_VALUE_TO_OP.get(v)
            if preset:
                self._ref_eq_radio.value = preset
                custom_visible = (preset == "custom")
                if self._ref_custom_eq_section:
                    self._ref_custom_eq_section.visible = custom_visible

        m = mods.get((0x0b, 0x3e))
        if m and self._eq_gains:
            gains = proto.decode_custom_eq(m["values"])
            for i, g in enumerate(gains):
                if i < len(self._eq_gains):
                    self._eq_gains[i] = g
            self._redraw_eq_curve()

        v = value_of((0x02, 0x2b))
        if v is not None and self._ref_ldac_switch:
            self._ref_ldac_switch.value = self._LDAC_TO_OP.get(v, False)

        v = value_of((0x02, 0x0e))
        if v is not None and self._ref_game_switch:
            self._ref_game_switch.value = self._GAME_TO_OP.get(v, False)

        v = value_of((0x02, 0x07))
        if v is not None and self._ref_touch_switch:
            self._ref_touch_switch.value = self._TOUCH_TO_OP.get(v, True)

        v = value_of((0x02, 0x31))
        if v is not None and self._ref_lang_sb:
            lang = self._LANG_TO_OP.get(v, "cn")
            self._ref_lang_sb.selected = [lang]

        v = value_of((0x02, 0x32))
        if v is not None and self._ref_multi_switch:
            self._ref_multi_switch.value = self._MULTI_TO_OP.get(v, False)

        v = value_of((0x02, 0x2e))
        if v is not None and self._ref_prompt_slider:
            self._ref_prompt_slider.value = float(v)

        m = mods.get((0x02, 0x2f))
        if m:
            self._update_find_state(m["values"])

        # 触控手势 dropdowns
        if st.touch and self._gesture_dropdowns:
            for pos, action in st.touch:
                dd = self._gesture_dropdowns.get(pos)
                if dd:
                    dd.value = str(action)

    def _on_connected(self, mac, connection=None):
        if (connection is not None and self.conn is not None and
                connection is not self.conn):
            return
        self._set_bottom_status(f"已连接 {mac}", ft.Colors.GREEN,
                                ft.Icons.CHECK_CIRCLE)
        self._ref_dev_dropdown.disabled = True
        self._ref_refresh_btn.disabled = False
        self._update_conn_btn(True)
        self._stop_auto_scan()
        self._set_controls_enabled(True)
        self._update_status(self.state)

    def _on_disconnected(self, reason, connection=None):
        # 旧 watch 线程可能在新连接建立后才投递结束消息，不能让它清理
        # 新连接的状态。
        if (connection is not None and self.conn is not None and
                connection is not self.conn):
            return
        self._watch_stop.set()
        self._cleanup_conn(connection)
        self._set_bottom_status(f"已断开 ({reason})", ft.Colors.RED,
                                ft.Icons.LINK_OFF)
        self._ref_dev_dropdown.disabled = False
        self._ref_refresh_btn.disabled = False
        self._update_conn_btn(False)
        self._set_controls_enabled(False)
        self._reset_ui_to_idle()
        self._start_auto_scan()

    def _on_connect_error(self, error, attempt=None):
        if (attempt is not None and self._connect_cancel is not None and
                attempt is not self._connect_cancel):
            return
        self._cleanup_conn()
        self._set_bottom_status("未连接", ft.Colors.RED, ft.Icons.LINK_OFF)
        self._show_snack(f"连接失败: {error}")
        self._update_conn_btn(False)
        self._ref_dev_dropdown.disabled = False
        self._ref_refresh_btn.disabled = False

    def _on_scan_result(self, devices):
        if devices is None:
            self._show_snack("扫描失败: 无法调用 bluetoothctl")
            return
        self._devices = devices
        options = []
        seen = set()
        for mac, name, connected in devices:
            label = f"{name} ({mac}){' [已连接]' if connected else ''}"
            options.append(ft.dropdown.Option(mac, label))
            seen.add(mac)
        self._ref_refresh_btn.disabled = False
        self._ref_dev_dropdown.options = options
        self._show_snack(f"发现 {len(devices)} 个已配对设备")

    def _on_write_result(self, success, msg):
        color = ft.Colors.GREEN if success else ft.Colors.RED
        icon = ft.Icons.CHECK_CIRCLE if success else ft.Icons.ERROR
        self._set_bottom_status(msg, color, icon)
        if not success:
            self._show_snack(msg)

    # ═══════════════════════════════════════════════════════════════════════
    # UI 事件：设备管理
    # ═══════════════════════════════════════════════════════════════════════

    # ═══════════════════════════════════════════════════════════════════════
    # 自动扫描
    # ═══════════════════════════════════════════════════════════════════════

    def _start_auto_scan(self):
        """启动后台自动扫描（每 10 秒），仅在未连接时运行。

        每个扫描线程持有自己的停止事件：重启扫描时不会清除旧线程的停止
        信号，避免旧线程（可能仍卡在 bluetoothctl 子进程中）被“复活”后
        与新线程同时扫描；`_stop_auto_scan` 只是通知旧线程退出，不做 join，
        因此不会阻塞主线程。
        """
        if self._auto_scan_thread and self._auto_scan_thread.is_alive():
            # 已有扫描线程在运行（例如重复的断连通知），不重复启动。
            return
        stop = threading.Event()
        self._auto_scan_stop = stop

        def loop():
            while not stop.is_set():
                devices = _scan_devices()
                self._post({"type": "scan_result", "devices": devices})
                # 每 10 秒扫一次，但每 1 秒检查一次 stop flag
                for _ in range(10):
                    if stop.is_set():
                        return
                    time.sleep(1)
        self._auto_scan_thread = threading.Thread(target=loop, daemon=True)
        self._auto_scan_thread.start()

    def _stop_auto_scan(self):
        stop = getattr(self, "_auto_scan_stop", None)
        if stop is not None:
            stop.set()
        self._auto_scan_thread = None

    def _on_refresh(self):
        """手动刷新：重新查询设备状态。"""
        if not self._check_connected():
            self._show_snack("未连接，请先连接设备")
            return
        self._set_bottom_status("正在刷新设备状态…", _BRAND_DARK,
                                ft.Icons.REFRESH)
        self.page.update()
        conn = self.conn

        def task():
            try:
                if self.conn is not conn:
                    raise RuntimeError("连接已断开")
                conn.send_queries()
                self._post({"type": "write_result", "success": True,
                            "msg": "状态已刷新"})
            except Exception as ex:
                self._post({"type": "write_result", "success": False,
                            "msg": f"刷新失败: {ex}"})
        threading.Thread(target=task, daemon=True).start()

    def _update_conn_btn(self, connected: bool):
        """连接/断开切换按钮：按连接状态切换文字、图标与样式。"""
        btn = self._ref_connect_btn
        if connected:
            btn.content = "断开连接"
            btn.icon = ft.Icons.LINK_OFF
            btn.style = ft.ButtonStyle(color=_BRAND)
        else:
            btn.content = "连接"
            btn.icon = ft.Icons.LINK
            btn.style = ft.ButtonStyle(bgcolor=_BRAND, color="white")
        btn.disabled = False
        self.page.update()

    def _on_conn_toggle(self):
        """合并按钮点击：已连接则断开，否则连接。"""
        if self.conn is not None:
            self._on_disconnect()
        else:
            self._on_connect()

    def _on_connect(self):
        mac = self._ref_dev_dropdown.value
        if not mac or not mac.strip():
            self._show_snack("请选择一个 MAC 地址")
            return
        mac = mac.strip()
        self._connect_cancel = threading.Event()
        # 连接中状态：文字「连接中…」、按钮整体变灰并禁用，避免重复点击
        self._ref_connect_btn.content = "连接中…"
        self._ref_connect_btn.icon = ft.Icons.HOURGLASS_TOP
        self._ref_connect_btn.style = ft.ButtonStyle(
            bgcolor=_CONNECTING_BG, color=_CONNECTING_FG)
        self._ref_connect_btn.disabled = True
        self._set_bottom_status("正在连接…", _BRAND_DARK,
                                ft.Icons.HOURGLASS_TOP)
        self.page.update()

        threading.Thread(target=self._do_connect,
                         args=(mac, self._connect_cancel), daemon=True).start()

    def _on_disconnect(self):
        self._disconnect()

    # ═══════════════════════════════════════════════════════════════════════
    # UI 事件：降噪控制
    # ═══════════════════════════════════════════════════════════════════════

    def _on_anc_mode_change(self, e):
        data = e.data if isinstance(e.data, list) else json.loads(e.data)
        if not data:
            return
        mode = data[0]
        self._execute_write("anc-mode", {"mode": mode})

    def _on_anc_level_change(self, e):
        data = e.data if isinstance(e.data, list) else json.loads(e.data)
        if not data:
            return
        level = int(data[0])
        self._execute_write("anc-level", {"level": level})

    def _on_trans_level_change(self, e):
        data = e.data if isinstance(e.data, list) else json.loads(e.data)
        if not data:
            return
        level = int(data[0])
        self._execute_write("trans-level", {"level": level})

    def _on_anc_cycle_apply(self, anc, wind, normal, trans):
        self._execute_write("anc-cycle", {
            "anc": 1 if anc else 0,
            "wind": 1 if wind else 0,
            "normal": 1 if normal else 0,
            "transparency": 1 if trans else 0,
        })

    # ═══════════════════════════════════════════════════════════════════════
    # UI 事件：音频设置
    # ═══════════════════════════════════════════════════════════════════════

    def _on_eq_preset_change(self, e):
        preset = e.control.value
        custom_visible = (preset == "custom")
        self._ref_custom_eq_section.visible = custom_visible
        if custom_visible:
            self._redraw_eq_curve()
        self._execute_write("eq", {"preset": preset})

    # ── EQ 曲线坐标工具 ─────────────────────────────────────────────────
    @staticmethod
    def _band_x(i: int) -> float:
        """第 i 个频段在 Canvas 上的 X 坐标（中心）。"""
        return _EQ_PAD_L + (i + 0.5) * _EQ_COL_W

    @staticmethod
    def _gain_y(g: float) -> float:
        """增益值 g (-6~+6) 在 Canvas 上的 Y 坐标（顶部=+6）。"""
        return _EQ_PAD_T + (6 - g) * (_EQ_PLOT_H / 12)

    @staticmethod
    def _gain_from_canvas_y(y: float) -> int:
        """把拖动坐标映射到实际曲线绘图区的 -6~+6 dB。"""
        plot_y = max(_EQ_PAD_T, min(_EQ_CANVAS_H - _EQ_PAD_B, y))
        raw = 6 - ((plot_y - _EQ_PAD_T) / _EQ_PLOT_H) * 12
        return max(-6, min(6, round(raw)))

    # ── Catmull-Rom → 三次贝塞尔 ───────────────────────────────────────
    @staticmethod
    def _catmull_rom_bezier(pts: list[tuple[float, float]]
                            ) -> list[tuple[tuple[float, float],
                                            tuple[float, float],
                                            tuple[float, float]]]:
        """将点序列 (Catmull-Rom 插值) 转为三次贝塞尔控制点序列。
        
        返回: list of (cp1, cp2, end) for each segment, 可直接喂给
              CanvasPath.cubicTo(cp1, cp2, end)。
        端点用线性外推处理。
        """
        n = len(pts)
        if n < 2:
            return []
        if n == 2:
            return [(pts[0], pts[1], pts[1])]
        segments = []
        for i in range(n - 1):
            p0 = pts[i - 1] if i > 0 else (2 * pts[0][0] - pts[1][0], 2 * pts[0][1] - pts[1][1])
            p1 = pts[i]
            p2 = pts[i + 1]
            p3 = pts[i + 2] if i + 2 < n else (2 * pts[-1][0] - pts[-2][0], 2 * pts[-1][1] - pts[-2][1])
            cp1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
            cp2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
            segments.append((cp1, cp2, p2))
        return segments

    # ── 构建 EQ 曲线 Canvas 图形 ───────────────────────────────────────
    def _eq_curve_shapes(self, gains: list[int],
                         selected: int | None = None) -> list:
        """纯函数：根据增益列表生成 Canvas 图形列表（网格 + 曲线 + 手柄）。"""
        shapes = []
        w, h = _EQ_CANVAS_W, _EQ_CANVAS_H

        # ── 水平网格线 (+6, +3, 0, -3, -6) ──────────────────────────────
        for db in (6, 3, -3, -6):
            y = self._gain_y(db)
            shapes.append(CanvasLine(
                _EQ_PAD_L, y, _EQ_CANVAS_W - _EQ_PAD_R, y,
                paint=ft.Paint("#9E9E9E4D", stroke_width=1),
            ))
        # 0dB 基准线加粗
        y0 = self._gain_y(0)
        shapes.append(CanvasLine(
            _EQ_PAD_L, y0, _EQ_CANVAS_W - _EQ_PAD_R, y0,
            paint=ft.Paint("#9E9E9E99", stroke_width=2),
        ))

        # ── 垂直频段分隔线（每个频段中心一条，手柄位于线上） ──────────
        for i in range(10):
            x = self._band_x(i)
            shapes.append(CanvasLine(
                x, _EQ_PAD_T, x, _EQ_CANVAS_H - _EQ_PAD_B,
                paint=ft.Paint("#9E9E9E1A", stroke_width=1),  # 10% alpha
            ))

        # ── Y 轴标签 ────────────────────────────────────────────────────
        for db in (6, 3, 0, -3, -6):
            y = self._gain_y(db)
            shapes.append(CanvasText(
                value=f"+{db}" if db >= 0 else str(db),
                x=0, y=y - 7,
                style=ft.TextStyle(size=11, color="#9E9E9E"),
            ))

        # ── 准备曲线点 ────────────────────────────────────────────────
        pts = [(self._band_x(i), self._gain_y(gains[i])) for i in range(10)]

        # ── 曲线下填充 ────────────────────────────────────────────────
        fill_paint = ft.Paint(
            _alpha("#F06292", 0x1A),  # 品牌粉半透明 10%（alpha 在前）
            style=ft.PaintingStyle.FILL,
        )
        if len(pts) >= 2:
            bottom_y = _EQ_CANVAS_H - _EQ_PAD_B
            path_cmds = [CanvasPath.MoveTo(pts[0][0], bottom_y)]
            path_cmds.append(CanvasPath.LineTo(pts[0][0], pts[0][1]))
            segs = self._catmull_rom_bezier(pts)
            for cp1, cp2, end in segs:
                path_cmds.append(CanvasPath.CubicTo(cp1[0], cp1[1], cp2[0], cp2[1], end[0], end[1]))
            path_cmds.append(CanvasPath.LineTo(pts[-1][0], bottom_y))
            path_cmds.append(CanvasPath.Close())
            shapes.append(CanvasPath(path_cmds, fill_paint))

        # ── 曲线本身 ────────────────────────────────────────────────────
        if len(pts) >= 2:
            line_paint = ft.Paint(
                "#F06292", stroke_width=2.5,  # 品牌粉
                style=ft.PaintingStyle.STROKE,
                stroke_cap=ft.StrokeCap.ROUND,
                stroke_join=ft.StrokeJoin.ROUND,
            )
            path_cmds = [CanvasPath.MoveTo(pts[0][0], pts[0][1])]
            segs = self._catmull_rom_bezier(pts)
            for cp1, cp2, end in segs:
                path_cmds.append(CanvasPath.CubicTo(cp1[0], cp1[1], cp2[0], cp2[1], end[0], end[1]))
            shapes.append(CanvasPath(path_cmds, line_paint))

        # ── 手柄圆点 ────────────────────────────────────────────────────
        for i, (x, y) in enumerate(pts):
            is_sel = (i == selected)
            r = 9 if is_sel else 6
            # 阴影（选中时外圈）
            if is_sel:
                shapes.append(CanvasCircle(
                    x, y, r + 4,
                    paint=ft.Paint(_EQ_COLORS_SEL[i] + "4C",  # 30% alpha
                                   style=ft.PaintingStyle.FILL),
                ))
            # 手柄主体
            shapes.append(CanvasCircle(
                x, y, r,
                paint=ft.Paint(color=_EQ_COLORS[i],
                               style=ft.PaintingStyle.FILL),
            ))
            # 选中时白色描边
            if is_sel:
                shapes.append(CanvasCircle(
                    x, y, r,
                    paint=ft.Paint(color="white", stroke_width=2,
                                   style=ft.PaintingStyle.STROKE),
                ))
                # 显示当前 dB 值悬标（_EQ_PAD_T 已预留安全区，贴顶也不出画布）
                shapes.append(CanvasText(
                    value=f"{gains[i]:+d}dB",
                    x=x - 14, y=y - r - 20,
                    style=ft.TextStyle(
                        size=12, weight=ft.FontWeight.BOLD,
                        color=_EQ_COLORS_SEL[i],
                    ),
                ))

        # ── X 轴标签 ────────────────────────────────────────────────────
        for i, band in enumerate(proto.EQ_BANDS):
            x = self._band_x(i)
            shapes.append(CanvasText(
                value=band,
                x=x - 12, y=_EQ_CANVAS_H - _EQ_PAD_B + 6,
                style=ft.TextStyle(size=10, color="#9E9E9E"),
            ))

        return shapes

    # ── 重绘 EQ 曲线（刷新 Canvas + 底部读数） ─────────────────────────
    def _redraw_eq_curve(self):
        if not self._eq_canvas:
            return
        self._eq_canvas.shapes = self._eq_curve_shapes(
            self._eq_gains, self._eq_selected,
        )
        # 更新底部读数字
        for i, g in enumerate(self._eq_gains):
            if i < len(self._eq_readout_texts):
                txt = f"{g:+d}" if g >= 0 else f"{g:d}"
                self._eq_readout_texts[i].value = txt
                self._eq_readout_texts[i].color = (
                    _EQ_COLORS_SEL[i] if i == self._eq_selected else _EQ_COLORS[i]
                )
                self._eq_readout_texts[i].weight = (
                    ft.FontWeight.BOLD if i == self._eq_selected else ft.FontWeight.NORMAL
                )
        self._eq_canvas.update()

    # ── 拖拽手柄事件 ───────────────────────────────────────────────────
    def _on_band_pan_start(self, idx: int, e):
        self._eq_selected = idx
        self._redraw_eq_curve()

    def _on_band_pan_update(self, idx: int, e):
        self._eq_selected = idx
        # local_position.y: 绘图区顶部=+6dB，底部=-6dB；画布上下安全区
        # 只用于悬标，不应参与增益范围换算。
        gain = self._gain_from_canvas_y(e.local_position.y)
        if self._eq_gains[idx] != gain:
            self._eq_gains[idx] = gain
            self._redraw_eq_curve()

    def _on_band_pan_end(self, idx: int, e):
        self._eq_selected = None
        self._redraw_eq_curve()

    def _on_apply_custom_eq(self):
        gains = list(self._eq_gains)
        self._do_apply_custom_eq(gains)

    def _do_apply_custom_eq(self, gains):
        if not self._check_connected():
            return
        if not self._begin_write("custom-eq", "正在发送自定义 EQ…"):
            return

        def task():
            try:
                # 先切到 Custom 模式；_do_write_inner 抛异常时立即停止，
                # 不再把后续曲线写入误当作成功流程的一部分。
                self._do_write_inner("eq", {"preset": "custom"})
                time.sleep(0.3)
                self._do_write_inner("custom-eq", {"gains": gains})
                self._post({
                    "type": "write_result", "success": True,
                    "msg": "自定义 EQ 已应用",
                })
            except Exception as ex:
                self._post({
                    "type": "write_result", "success": False,
                    "msg": f"自定义 EQ 写入失败: {ex}",
                })
            finally:
                self._write_lock.release()
                self._post({"type": "write_done", "btn_op": "custom-eq"})
        threading.Thread(target=task, daemon=True).start()

    # ── LDAC (重启 + 出仓) ────────────────────────────────────────────────
    def _on_apply_ldac(self, state):
        self._execute_write("ldac", {"state": "on" if state else "off"})

    # ── 游戏模式 ──────────────────────────────────────────────────────────
    def _on_apply_game(self, state):
        self._execute_write("game-mode", {"state": "on" if state else "off"})

    # ═══════════════════════════════════════════════════════════════════════
    # UI 事件：交互设置
    # ═══════════════════════════════════════════════════════════════════════

    def _on_apply_touch(self, state):
        self._execute_write("touch", {"state": "on" if state else "off"})

    def _on_toggle_gesture(self, e):
        visible = not self._ref_gesture_section.visible
        self._ref_gesture_section.visible = visible
        self._ref_gesture_toggle.content = ("▾" if visible else "▸") + " 手势设置"
        self.page.update()

    def _on_language_change(self, e):
        data = e.data if isinstance(e.data, list) else json.loads(e.data)
        if not data:
            return
        lang = data[0]
        self._execute_write("language", {"lang": lang})

    def _on_apply_prompt_tone(self, level):
        self._execute_write("prompt-tone", {"level": int(round(level))})

    def _on_gesture_apply_all(self):
        """将当前所有手势 dropdown 的值一次性写入设备。"""
        if not self._check_connected():
            return
        pending = []
        for pos, dd in self._gesture_dropdowns.items():
            if dd.value is not None:
                pending.append((pos, int(dd.value)))
        if not pending:
            self._show_snack("没有需要应用的手势设置")
            return
        if not self._begin_write("gesture", f"正在设置 {len(pending)} 个手势…"):
            return
        def task():
            try:
                for pos, action in pending:
                    self._do_write_inner("gesture", {"pos": pos, "action": action})
                    time.sleep(0.15)
                self._post({
                    "type": "write_result", "success": True,
                    "msg": f"已应用 {len(pending)} 个手势设置",
                })
            except Exception as ex:
                self._post({
                    "type": "write_result", "success": False,
                    "msg": f"手势设置失败: {ex}",
                })
            finally:
                self._write_lock.release()
                self._post({"type": "write_done", "btn_op": "gesture"})
        threading.Thread(target=task, daemon=True).start()

    def _on_gesture_reset(self):
        """将所有手势 dropdown 重置为「无作用」(0x00) 并写入。"""
        if not self._check_connected():
            return
        for dd in self._gesture_dropdowns.values():
            dd.value = "0"
        self._on_gesture_apply_all()

    # ═══════════════════════════════════════════════════════════════════════
    # UI 事件：连接设置
    # ═══════════════════════════════════════════════════════════════════════

    def _on_apply_multi_device(self, state):
        self._execute_write("multi-device", {"state": "on" if state else "off"})

    # ═══════════════════════════════════════════════════════════════════════
    # EQ 预设管理
    # ═══════════════════════════════════════════════════════════════════════

    def _refresh_eq_preset_dropdown(self):
        names = sorted(self._eq_presets.keys())
        self._ref_eq_preset_dropdown.options = [
            ft.dropdown.Option(n) for n in names
        ]
        self._ref_eq_preset_dropdown.value = names[0] if names else None
        self.page.update()

    def _on_save_eq_preset(self):
        """保存 EQ 预设：如果下拉框有选中则快速覆盖，否则弹出命名对话框。"""
        name = self._ref_eq_preset_dropdown.value
        if name:
            self._confirm_then(
                f"覆盖预设「{name}」？",
                lambda: self._do_save_eq_preset(name),
            )
        else:
            self._show_name_dialog("保存 EQ 预设", "预设名称:", self._do_save_eq_preset)

    def _on_save_as_eq_preset(self):
        """另存为：始终弹出命名对话框。"""
        self._show_name_dialog("另存为 EQ 预设", "预设名称:", self._do_save_eq_preset)

    def _do_save_eq_preset(self, name):
        if not name:
            return
        gains = list(self._eq_gains)
        self._eq_presets[name] = gains
        _save_eq_presets(self._eq_presets)
        self._refresh_eq_preset_dropdown()
        self._show_snack(f"预设「{name}」已保存")

    def _on_load_eq_preset(self):
        name = self._ref_eq_preset_dropdown.value
        if not name:
            self._show_snack("请先选择一个预设")
            return
        gains = self._eq_presets.get(name)
        if gains is None:
            self._show_snack(f"预设「{name}」不存在")
            return
        for i, g in enumerate(gains):
            if i < len(self._eq_gains):
                self._eq_gains[i] = g
        self._redraw_eq_curve()
        self._show_snack(f"已载入预设「{name}」")

    def _on_delete_eq_preset(self):
        name = self._ref_eq_preset_dropdown.value
        if not name:
            self._show_snack("请先选择一个预设")
            return
        self._confirm_then(
            f"确定删除预设「{name}」？",
            lambda: self._do_delete_eq_preset(name),
        )

    def _do_delete_eq_preset(self, name):
        if name in self._eq_presets:
            del self._eq_presets[name]
            _save_eq_presets(self._eq_presets)
            self._refresh_eq_preset_dropdown()
            self._show_snack(f"预设「{name}」已删除")

    # ═══════════════════════════════════════════════════════════════════════
    # 对话框工具
    # ═══════════════════════════════════════════════════════════════════════

    def _show_snack(self, msg: str):
        self.page.show_dialog(ft.SnackBar(content=ft.Text(msg), open=True))

    def _confirm_then(self, msg: str, on_confirm):
        dlg = ft.AlertDialog(
            title=ft.Text("确认操作"),
            content=ft.Text(msg),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self._close_dlg(dlg)),
                ft.Button("确认", on_click=lambda e: (
                    self._close_dlg(dlg), on_confirm(),
                )),
            ],
            open=True,
        )
        self.page.show_dialog(dlg)

    def _show_name_dialog(self, title: str, hint: str, on_ok):
        tf = ft.TextField(label=hint)
        dlg = ft.AlertDialog(
            title=ft.Text(title),
            content=tf,
            actions=[
                ft.TextButton("取消", on_click=lambda e: self._close_dlg(dlg)),
                ft.Button("确认", on_click=lambda e: (
                    self._close_dlg(dlg), on_ok(tf.value),
                )),
            ],
            open=True,
        )
        self.page.show_dialog(dlg)

    def _close_dlg(self, dlg):
        dlg.open = False
        self.page.update()

    def _check_connected(self) -> bool:
        if not self.conn:
            self._show_snack("请先连接设备")
            return False
        return True

    def _check_in_case(self) -> bool:
        bat = self.state.battery
        if not bat or "left_pct" not in bat:
            self._show_snack("电量信息尚未采集，请稍候")
            return True
        if bat.get("left_charging") or bat.get("right_charging"):
            self._show_snack("双耳需出仓才能执行此操作")
            return True
        return False

    # ═══════════════════════════════════════════════════════════════════════
    # 连接线程
    # ═══════════════════════════════════════════════════════════════════════

    def _connection_io_lock(self, conn):
        return getattr(conn, "io_lock", self._sock_lock)

    def _do_connect(self, mac: str, cancel_event=None):
        connect_timeout = 15.0
        cancel_event = cancel_event or threading.Event()
        conn = reader.Connection(mac)
        try:
            conn.connect(connect_timeout=connect_timeout)
        except Exception as ex:
            if not cancel_event.is_set():
                print(f"[BLE-DEBUG] _do_connect connect() 失败: {type(ex).__name__}: {ex}",
                      file=sys.stderr)
                self._post({"type": "connect_error", "error": str(ex),
                            "attempt": cancel_event})
            return

        # 用户可能在连接线程阻塞期间点击了断开；不要把已取消的连接
        # 安装回 UI，也不要让旧连接覆盖新连接。
        if (cancel_event.is_set() or
                (self._connect_cancel is not None and
                 self._connect_cancel is not cancel_event)):
            conn.close()
            return

        self.conn = conn
        self.state.mac = mac
        try:
            self.state.name = conn.bluetooth.lookup_name(mac, timeout=5)
        except Exception as ex:
            print(f"[BLE-DEBUG] lookup_name 失败: {ex}", file=sys.stderr)
        try:
            conn.send_queries()
        except Exception as ex:
            print(f"[BLE-DEBUG] _do_connect send_queries 失败: {type(ex).__name__}: {ex}",
                  file=sys.stderr)
            self._cleanup_conn(conn)
            if not cancel_event.is_set():
                self._post({"type": "connect_error",
                            "error": f"查询失败: {ex}",
                            "attempt": cancel_event})
            return
        if cancel_event.is_set() or self.conn is not conn:
            self._cleanup_conn(conn)
            return
        self._start_watch(conn)
        self._post({"type": "connected", "mac": mac, "connection": conn})

    def _start_watch(self, conn=None):
        conn = conn or self.conn
        if not conn:
            return
        # 每个连接拥有自己的停止事件，避免旧 watch 在线程退出前被新连接
        # clear() 后重新放活。
        self._watch_stop.set()
        watch_stop = threading.Event()
        self._watch_stop = watch_stop
        with self._connection_io_lock(conn):
            if not conn.sock:
                return
            conn.sock.settimeout(WATCH_TIMEOUT)
        t = threading.Thread(target=self._watch_loop,
                             args=(conn, watch_stop), daemon=True)
        self._watch_thread = t
        t.start()

    def _watch_loop(self, conn, stop_event):
        def on_frame(decoded):
            self._post({"type": "frame", "decoded": decoded,
                        "connection": conn})

        try:
            conn.watch_frames(on_frame, stop_event)
        except Exception as ex:
            print(f"[BLE-DEBUG] _watch_loop: {type(ex).__name__}: {ex}",
                  file=sys.stderr)
        if not stop_event.is_set() and self.conn is conn:
            self._post({"type": "disconnected", "reason": "连接断开",
                        "connection": conn})

    def _disconnect(self):
        if self._connect_cancel:
            self._connect_cancel.set()
        self._watch_stop.set()
        conn = self.conn
        self._cleanup_conn(conn)
        self._post({"type": "disconnected", "reason": "用户断开",
                    "connection": conn})

    def _cleanup_conn(self, connection=None):
        current = self.conn
        if connection is not None and current is not None and current is not connection:
            # 只关闭传入的旧连接，不碰当前新连接。
            try:
                connection.close()
            except Exception:
                pass
            return
        conn = current or connection
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        if current is None or connection is None or current is connection:
            self.conn = None
            self.state = reader.DeviceState()

    # ═══════════════════════════════════════════════════════════════════════
    # 写执行（UI 侧协调）
    # ═══════════════════════════════════════════════════════════════════════

    def _begin_write(self, op_id, status):
        if not self._write_lock.acquire(blocking=False):
            self._show_snack("已有写入操作进行中，请稍候")
            return False
        btn = self._find_trigger_btn(op_id)
        if btn:
            btn.disabled = True
        self._set_controls_enabled(False)
        self._set_bottom_status(status, _BRAND_DARK, ft.Icons.HOURGLASS_TOP)
        self.page.update()
        return True

    def _execute_write(self, op_id: str, kwargs: dict):
        if not self._check_connected():
            return
        op = wproto.WRITE_OPS.get(op_id)
        if op is None:
            self._show_snack(f"未知操作: {op_id}")
            return
        if op.get("hard_precond") == "out_of_case":
            if self._check_in_case():
                return
        if op.get("reboot"):
            self._confirm_then(
                f"{op['desc']}\n\n此操作会触发耳机重启",
                lambda: self._do_write(op_id, kwargs),
            )
            return
        self._do_write(op_id, kwargs)

    def _do_write(self, op_id: str, kwargs: dict):
        if not self._begin_write(op_id, f"正在发送 {op_id}…"):
            return

        def task():
            try:
                msg = self._do_write_inner(op_id, kwargs)
                self._post({
                    "type": "write_result", "success": True, "msg": msg,
                })
            except Exception as ex:
                self._post({
                    "type": "write_result", "success": False, "msg": str(ex),
                })
            finally:
                self._write_lock.release()
                self._post({"type": "write_done", "btn_op": op_id})

        threading.Thread(target=task, daemon=True).start()

    def _do_write_inner(self, op_id: str, kwargs: dict):
        op = wproto.WRITE_OPS[op_id]
        op_frames = op["build"](**kwargs)
        conn = self.conn
        if not conn or not conn.sock:
            raise RuntimeError("连接已断开")

        # 序号生成、发送和 ACK 接收必须在同一连接锁内完成；这样 GUI
        # 的第一条写命令会沿用连接查询之后的序号，而不是重置到 02。
        ack_seqs = []
        sent_count = 0
        connection_lost = False
        watch_stop = getattr(self, "_watch_stop", None)
        lock = self._connection_io_lock(conn)
        with lock:
            full = wproto.build_write_sequence(conn._next_seq, op_frames)
            if not full:
                raise RuntimeError("没有可发送的命令")
            try:
                for label, frame in full:
                    if self.conn is not conn or not conn.sock:
                        raise RuntimeError("连接已断开")
                    try:
                        conn.sock.send(bytes(frame))
                    except Exception as ex:
                        print(f"[BLE-DEBUG] 发送 {label} 失败: {type(ex).__name__}: {ex}",
                              file=sys.stderr)
                        raise RuntimeError(f"发送 {label} 失败: {ex}") from ex
                    sent_count += 1
                    time.sleep(0.15)

                # ACK 计时从拿到 socket 锁并完成发送后开始，避免 watch
                # 线程占锁时提前消耗本次操作的等待窗口。
                deadline = time.monotonic() + ACK_TIMEOUT
                parser = proto.FrameParser()
                while (time.monotonic() < deadline and
                       len(ack_seqs) < len(full) and
                       not (watch_stop and watch_stop.is_set())):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        conn.sock.settimeout(min(0.5, remaining))
                        chunk = conn.sock.recv(1024)
                    except Exception as ex:
                        if reader.Connection._is_timeout_error(ex):
                            continue
                        connection_lost = True
                        print(f"[BLE-DEBUG] ACK等待失败: {type(ex).__name__}: {ex}",
                              file=sys.stderr)
                        break
                    if not chunk:
                        connection_lost = True
                        break
                    for raw_frame in parser.feed(chunk):
                        decoded = proto.decode_frame(raw_frame)
                        if decoded.get("kind") == "ACK":
                            ack_seqs.append(decoded["seq"])
                        else:
                            # 写入后的 MODULE/INIT 状态帧不能被 ACK 等待
                            # 循环吞掉，交回与 watch 相同的状态处理链。
                            self._post({"type": "frame", "decoded": decoded,
                                        "connection": conn})
            finally:
                if self.conn is conn and conn.sock:
                    try:
                        conn.sock.settimeout(WATCH_TIMEOUT)
                    except Exception:
                        pass

        if len(ack_seqs) >= len(full):
            return "操作成功"
        if op_id == "multi-device" and sent_count == len(full):
            # 02 32 的合法行为是设备重启并断开，可能没有任何 ACK。
            return "命令已发出，设备正在重启，等待重连"
        detail = f"ACK 超时 ({len(ack_seqs)}/{len(full)})"
        if connection_lost:
            detail += "，连接已断开"
        raise RuntimeError(detail)

    def _find_trigger_btn(self, op_id: str):
        mapping = {
            "anc-mode": self._ref_anc_mode_sb,
            "anc-level": self._ref_anc_level_sb,
            "trans-level": self._ref_trans_level_sb,
            "anc-cycle": self._ref_anc_cycle_apply,
            "language": None,
            "find": None,
            "custom-eq": self._ref_apply_custom_eq,
            "gesture": None,
        }
        return mapping.get(op_id)

    # ═══════════════════════════════════════════════════════════════════════
    # 窗口关闭
    # ═══════════════════════════════════════════════════════════════════════

    def _on_window_event(self, e):
        if e.type == ft.WindowEventType.CLOSE:
            self._disconnect()
            self._stop_auto_scan()
            self.page.pubsub.unsubscribe_all()
            self.page.window.destroy()


# ═══════════════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════════════

def main():
    ft.run(main=RoseLinkApp, view=ft.AppView.FLET_APP)


if __name__ == "__main__":
    main()
