# RoseLink 蓝牙协议逆向与Linux实现 — CERAMICS-MK2

对 RoseLink App 的通信协议逆向工程，近乎完整还原了 CERAMICS-MK2 耳机的蓝牙 SPP-RFCOMM 协议。

所有结论均通过 ARM64 反汇编（`rs_common_v2_sender.dart` / `rs_common_v2_receiver.dart`）与 HCI 抓包交叉验证。

> 本项目（包括协议逆向分析、文档、代码）**完全由 AI 生成**，由人类提供方向、验证结论并做出技术决策。

## 文件结构

| 文件 | 内容 |
|---|---|
| `rs_protocol.py` | 协议常量、帧拆分（流式 `FrameParser`）、校验和、模块解码器（电量/ANC/EQ/固件/触控） |
| `roselink_reader.py` | 只读 CLI — 连接耳机、查询全部能力与状态、持续监听、JSON 输出 |
| `rs_writer.py` | 写命令帧构造器 — 全部已确认写操作的线格式帧，带参数校验与越界拦截 |
| `roselink_writer.py` | 写操作 CLI — 通过子命令修改设备设置，带 ACK 确认、出仓前置、重启确认 |
| `roselink_gui.py` | flet 桌面 GUI — 可视化连接与全部设置（EQ 曲线编辑器、降噪/手势/查找耳机等） |
| `selftest.py` | 离线自测（92 项），无需蓝牙设备 |

## 环境要求

- Linux（BlueZ 蓝牙栈）
- Python 3.7+
- [PyBluez](https://pypi.org/project/PyBluez/) 0.30
- flet（仅 GUI 需要）

```bash
pip install PyBluez==0.30 flet
```

`--scan` 依赖 `bluetoothctl`，不可用时降级但 `--mac` 直连仍可用。

## 只读读取（`roselink_reader.py`）

仅发送 `1e fa` 和 `02 fa 3e` 两种查询指令，**不修改设备任何设置**。

```bash
# 扫描
python3 roselink_reader.py --scan

# 连接并读取设备全部状态（电量/ANC/EQ/触控/固件等）
python3 roselink_reader.py --mac AA:BB:CC:DD:EE:FF

# 持续监听状态变更
python3 roselink_reader.py --mac AA:BB:CC:DD:EE:FF --watch

# JSON 格式化输出
python3 roselink_reader.py --mac AA:BB:CC:DD:EE:FF --json

# 调试：打印收发原始 hex
python3 roselink_reader.py --mac AA:BB:CC:DD:EE:FF --raw
```

## 写操作（`roselink_writer.py`）

子命令方式修改设备设置。**建议先 `--dry-run` 确认。**

```bash
# 列出所有支持的操作
python3 roselink_writer.py --list

# 演练（不发送）
python3 roselink_writer.py --mac AA:BB:CC:DD:EE:FF eq pop --dry-run

# EQ 预设
python3 roselink_writer.py --mac AA:BB:CC:DD:EE:FF eq hifi
python3 roselink_writer.py --mac AA:BB:CC:DD:EE:FF eq custom

# 自定义 EQ（10 段增益 -6 ~ +6 dB，顺序: 20Hz/100Hz/300Hz/500Hz/1kHz/2kHz/3kHz/5kHz/8kHz/15kHz）
python3 roselink_writer.py --mac AA:BB:CC:DD:EE:FF custom-eq 6 5 4 3 2 1 0 -1 -2 -3

# ANC 模式 / 等级 / 触控循环
python3 roselink_writer.py --mac AA:BB:CC:DD:EE:FF anc-mode transparency
python3 roselink_writer.py --mac AA:BB:CC:DD:EE:FF anc-level 1
python3 roselink_writer.py --mac AA:BB:CC:DD:EE:FF anc-cycle 1 0 0 1

# 通透模式等级
python3 roselink_writer.py --mac AA:BB:CC:DD:EE:FF trans-level 1

# 显示手势位置和动作代码 / 自定义手势
python3 roselink_writer.py --list-touch
python3 roselink_writer.py --mac AA:BB:CC:DD:EE:FF gesture 11 04

# LDAC（会重启 + 须出仓）
python3 roselink_writer.py --mac AA:BB:CC:DD:EE:FF ldac on

# 多设备连接（会重启 + 须出仓）
python3 roselink_writer.py --mac AA:BB:CC:DD:EE:FF multi-device on
```

### 子命令一览

| 子命令 | 功能 | 特殊标记 |
|---|---|---|
| `eq` | EQ 预设（hifi/pop/rock/custom） | — |
| `custom-eq` | 自定义 EQ 10 段增益（-6 ~ +6 dB） | — |
| `anc-mode` | ANC 模式（anc/normal/transparency/wind） | — |
| `anc-level` | 降噪等级（1/3/5） | — |
| `trans-level` | 通透等级（1/3/5） | — |
| `anc-cycle` | 降噪触控循环（降噪 风噪 普通 通透，各 0/1） | — |
| `game-mode` | 游戏模式 on/off | — |
| `touch` | 触控开关 on/off（注意 ON=00） | — |
| `gesture` | 触控手势配置 | — |
| `language` | 语音语言 cn/en | — |
| `prompt-tone` | 提示音音量 0~5 | — |
| `find` | 查找耳机 left/right/both/off | — |
| `ldac` | LDAC 开关 | ⚠重启 ⚠须出仓 |
| `multi-device` | 多设备连接开关 | ⚠重启 ⚠须出仓 |

### 安全机制

- **出仓硬前置**：`ldac`/`multi-device` 写前先主动查询电量，确认两只耳机均已出仓（bit7=0），否则拒绝发送
- **重启确认**：触发重启的操作需交互式确认（`y/N`）
- **越界拦截**：所有参数在构造阶段校验，非法值在发送前即报错
- **跳过安全拦截**：`--force` 可跳过出仓检查和重启确认

写命令发送后等待 C2H ACK 帧，ACK 数量匹配发送帧数才判定为成功。

## 图形界面（`roselink_gui.py`）

基于 flet 的桌面控制台，把只读状态、写操作和 EQ 编辑集成到一个界面，功能与 CLI 完全对应，适合日常使用。

```bash
cd reader
source .venv/bin/activate
python3 roselink_gui.py
```

窗口布局为「设备信息 + 电量卡片 + 四张功能卡片」：

| 区域 | 功能 |
|---|---|
| 顶部设备栏 | 已配对设备下拉选择、连接/断开切换按钮；未连接时每 10 秒自动扫描设备 |
| 电量卡片 | 左耳/右耳/充电仓电量进度条，低电红色预警，在仓充电与仓电量缓存标记 |
| 降噪控制 | ANC 模式、降噪等级（轻/中/深）、通透等级（舒适/人声/标准）、降噪触控循环 |
| 音频设置 | EQ 预设切换、自定义 EQ 10 段曲线编辑器、LDAC 开关、游戏模式 |
| 交互设置 | 触控开关、左右耳 8 个手势下拉配置（全部应用/重置）、语音语言、提示音音量、查找耳机左右耳独立按钮 |
| 连接设置 | 多设备连接开关 |

自定义 EQ 支持鼠标拖拽手柄实时绘制曲线，10 段增益可保存为命名预设，预设文件存放在 `~/.config/roselink/eq_presets.json`。

安全机制与 CLI 一致：LDAC 与多设备开关写前检查双耳出仓（电量 bit7），重启类操作需弹窗二次确认；写操作实时等待设备 ACK，失败会在底部状态栏提示。

GUI 直接复用 `roselink_reader` 的连接、拆帧与解码逻辑（后台 watch 线程 + 主线程事件循环），不依赖 CLI 进程，也不需要解析 CLI 的 JSON 输出。

## 离线自测

```bash
python3 selftest.py
```

92 项离线测试覆盖：校验和、帧解析（含噪声/data 内 0xAA/拼接）、编解码器往返、写操作参数校验、边界/类型异常输入，以及 fake socket 下的 JSON、连接生命周期（含 EALREADY 自动重试）、GUI 写入、EQ/查找状态、CLI 异常边界和扫描线程生命周期回归。**无需蓝牙设备。**

## 免责声明

本软件按"原样"提供，**无任何明示或默示的担保**。作者不对因使用本软件造成的任何直接或间接损失承担责任。

文中提及的所有商标（包括但不限于 RoseLink、CERAMICS）均为其各自所有者的财产。**本项目与上述所有者无任何关联、背书或附属关系。** 写操作可能导致设备设置异常、功能失效或违反保修条款，使用前请确保您理解所执行操作的含义。
