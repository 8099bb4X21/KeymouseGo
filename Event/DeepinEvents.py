# -*- encoding:utf-8 -*-
"""
deepin(Wayland) 回放后端。

通过 uinput(内核级虚拟设备) 实现鼠标移动/点击与键盘按下/抬起，
彻底替换 pyautogui(pyautogui 依赖 X11, 在本机 Wayland 会话 import 即崩)。

仅在 Linux + Wayland + deepin 环境下加载(由 Event/__init__.py 选择)。
回放必须 root 运行(uinput 需写 /dev/uinput 权限)。

坐标模型: 脚本中鼠标坐标为"绝对像素 int"(录制端存的就是 XEventMonitor 给的绝对坐标,
配合 flag_multiplemonitor=True 跳过 UIFunc 的 % 格式化, 本文件无需任何换算)。

uinput 常量与结构见 test/findings.md(uname: aarch64, C long=8)。
"""

import fcntl
import os
import re
import struct
import subprocess
import time

from Event.Event import Event
from loguru import logger

# 仅作占位导出(Event/__init__.py 要求 _Event.SW/_Event.SH 存在), 不参与坐标换算
SW, SH = 1920, 1080

# ---- uinput 常量 ----
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502

EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02

REL_X = 0x00
REL_Y = 0x01
REL_WHEEL = 0x08

BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112

SYN_REPORT = 0
BUS_USB = 0x03

# XEventMonitor 光标坐标读取
XEM_SERVICE = "com.deepin.api.XEventMonitor"
XEM_PATH = "/com/deepin/api/XEventMonitor"
XEM_INTERFACE = "com.deepin.api.XEventMonitor"

# 相对移动闭环校准参数
MOVE_MAX_ITER = 30      # 最大校准迭代次数
MOVE_STEP_MAX = 100     # 单次注入最大步长(px), 避免加速放大导致跳过头
MOVE_TOLERANCE = 3      # 判定到位的容差(px)
MOVE_ITER_SLEEP = 0.01  # 每次迭代间 sleep(等待合成器处理)

# 鼠标 action_type -> (事件类型, 编码, 按下值)
_BUTTON_ACTIONS = {
    'mouse left down': (BTN_LEFT, 1),
    'mouse left up': (BTN_LEFT, 0),
    'mouse right down': (BTN_RIGHT, 1),
    'mouse right up': (BTN_RIGHT, 0),
    'mouse middle down': (BTN_MIDDLE, 1),
    'mouse middle up': (BTN_MIDDLE, 0),
}

# 滚轮
_WHEEL_DELTA = 1


class _UInputDevice(object):
    """uinput 虚拟键鼠设备(进程级单例)。设备创建后需 warm-up 丢弃首次事件。"""

    def __init__(self):
        self.fd = None

    def open(self):
        if self.fd is not None:
            return True
        try:
            self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            logger.error("打开 /dev/uinput 失败: %s (需 root 运行)", e)
            return False
        try:
            for code in (EV_REL, EV_KEY):
                fcntl.ioctl(self.fd, UI_SET_EVBIT, code)
            for code in (REL_X, REL_Y, REL_WHEEL):
                fcntl.ioctl(self.fd, UI_SET_RELBIT, code)
            for code in (BTN_LEFT, BTN_RIGHT, BTN_MIDDLE):
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)
            self._write_user_dev()
            fcntl.ioctl(self.fd, UI_DEV_CREATE, 0)
        except OSError as e:
            logger.error("初始化 uinput 设备失败: %s", e)
            self._close()
            return False
        logger.info("uinput 虚拟键鼠设备已创建")
        self._warm_up()
        return True

    def _write_user_dev(self):
        name = b"KeymouseGo-deepin"
        buf = bytearray(1116)
        buf[0:len(name)] = name
        struct.pack_into("H", buf, 80, BUS_USB)
        os.write(self.fd, bytes(buf))

    def _warm_up(self):
        # 设备识别期(udev->libinput->kwin)会丢弃首批事件, 等待并注入一次丢弃移动
        time.sleep(1.0)
        self.emit(EV_REL, REL_X, 0)
        self.emit(EV_REL, REL_Y, 0)
        self.emit(EV_SYN, SYN_REPORT, 0)
        time.sleep(0.5)

    def emit(self, etype, code, value):
        if self.fd is None:
            return
        try:
            data = struct.pack("llHHi", 0, 0, etype, code, value)
            os.write(self.fd, data)
        except OSError as e:
            logger.error("uinput 注入失败: %s", e)

    def _close(self):
        if self.fd is not None:
            try:
                fcntl.ioctl(self.fd, UI_DEV_DESTROY, 0)
            except OSError:
                pass
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


_uinput = _UInputDevice()


def _get_cursor_pos():
    """通过 gdbus 读 XEventMonitor 的 CursorX/CursorY, 返回 (x, y) 或 None。"""
    pos = []
    for prop in ("CursorX", "CursorY"):
        cmd = ["gdbus", "call", "--session",
               "--dest", XEM_SERVICE, "--object-path", XEM_PATH,
               "--method", "org.freedesktop.DBus.Properties.Get",
               XEM_INTERFACE, prop]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            return None
        out = proc.stdout.decode("utf-8", errors="replace").strip()
        m = re.search(r"-?\d+", out)
        if m is None:
            return None
        pos.append(int(m.group(0)))
    if len(pos) == 2:
        return pos[0], pos[1]
    return None


def _move_to(x, y):
    """移动光标到绝对坐标: 读当前位置 -> 循环 EV_REL 相对移动 -> 闭环校准。"""
    if not _uinput.open():
        return
    for _ in range(MOVE_MAX_ITER):
        cur = _get_cursor_pos()
        if cur is None:
            return
        dx = x - cur[0]
        dy = y - cur[1]
        if abs(dx) < MOVE_TOLERANCE and abs(dy) < MOVE_TOLERANCE:
            return
        step_x = max(-MOVE_STEP_MAX, min(MOVE_STEP_MAX, dx))
        step_y = max(-MOVE_STEP_MAX, min(MOVE_STEP_MAX, dy))
        _uinput.emit(EV_REL, REL_X, step_x)
        _uinput.emit(EV_REL, REL_Y, step_y)
        _uinput.emit(EV_SYN, SYN_REPORT, 0)
        time.sleep(MOVE_ITER_SLEEP)


def _click(button_code, value):
    if not _uinput.open():
        return
    _uinput.emit(EV_KEY, button_code, value)
    _uinput.emit(EV_SYN, SYN_REPORT, 0)


def _wheel(direction):
    if not _uinput.open():
        return
    _uinput.emit(EV_REL, REL_WHEEL, direction * _WHEEL_DELTA)
    _uinput.emit(EV_SYN, SYN_REPORT, 0)


def _key(key_code, down):
    if not _uinput.open():
        return
    _uinput.emit(EV_KEY, key_code, 1 if down else 0)
    _uinput.emit(EV_SYN, SYN_REPORT, 0)


class DeepinEvent(Event):
    """deepin 后端事件执行。EM=鼠标, EK=键盘, EX=文本。"""

    def execute(self, thd=None):
        self.sleep(thd)

        if self.event_type == "EM":
            x, y = self.action
            if self.action == [-1, -1]:
                pass
            else:
                _move_to(int(x), int(y))

            if self.action_type in _BUTTON_ACTIONS:
                code, value = _BUTTON_ACTIONS[self.action_type]
                _click(code, value)
            elif self.action_type == "mouse wheel up":
                _wheel(1)
            elif self.action_type == "mouse wheel down":
                _wheel(-1)
            elif self.action_type == "mouse move":
                pass
            else:
                logger.warning("Unknown mouse event: %s", self.action_type)

        elif self.event_type == "EK":
            key_code, key_name, extended = self.action
            if self.action_type == "key down":
                _key(key_code, True)
            elif self.action_type == "key up":
                _key(key_code, False)
            else:
                logger.warning("Unknown keyboard event: %s", self.action_type)

        elif self.event_type == "EX":
            if self.action_type == "input":
                _type_text(self.action)
            else:
                logger.warning("Unknown extra event: %s", self.action_type)

        else:
            logger.warning("Unknown event_type: %s", self.event_type)


def _type_text(text):
    """逐字符注入 ASCII 文本(数字/字母可直转; 其余跳过)。中文文本需剪贴板方案另补。"""
    for ch in text:
        key_code = _char_to_code(ch)
        if key_code is None:
            continue
        _key(key_code, True)
        _key(key_code, False)


def _char_to_code(ch):
    """ASCII 字符 -> evdev keycode。仅覆盖可单键直取的字符, 其余返回 None。"""
    o = ord(ch)
    if 97 <= o <= 122:  # a-z
        return 30 + (o - 97)
    if 48 <= o <= 57:   # 0-9
        return {48: 11, 49: 2, 50: 3, 51: 4, 52: 5,
                53: 6, 54: 7, 55: 8, 56: 9, 57: 10}[o]
    single = {
        " ": 57, "\n": 28, "\t": 15, "-": 12, "=": 13, ".": 52,
        ",": 51, "/": 53, ";": 39, "'": 40, "`": 41, "\\": 43,
    }
    return single.get(ch)