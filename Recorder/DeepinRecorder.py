# -*- encoding:utf-8 -*-
"""
deepin(Wayland) 录制后端。

鼠标录制: dbus-monitor 子进程监听 com.deepin.api.XEventMonitor 信号(CursorMove/ButtonPress/ButtonRelease)
键盘录制: 读 /dev/input/eventX 的 EV_KEY 事件(内核层, 全局可用, 覆盖 Wayland 原生窗口)

替换 UniversalRecorder(pynput 只作用 XWayland, Wayland 原生窗口录不到)。

坐标: 存"绝对像素 int"(XEventMonitor 直接给的屏幕坐标), 配合 Event/__init__.py 中
flag_multiplemonitor=True, 使 UIFunc 跳过 % 格式化。
"""

import os
import re
import struct
import subprocess
import sys
import threading
import time

import Recorder.globals as globalv
from loguru import logger

record_signals = globalv.RecordSignal()

# pynput 只覆盖 XWayland 窗口（KeymouseGo 自身、浏览器等多跑 Xcb），与 XEventMonitor
#（覆盖 Wayland 原生）互补。X 环境不可用时降级为单通道，不致命。
try:
    from pynput import mouse as _pynput_mouse
    _HAS_PYNPUT = True
    _PYNPUT_ERR = ""
except Exception as e:
    _pynput_mouse = None
    _HAS_PYNPUT = False
    _PYNPUT_ERR = str(e)

XEM_INTERFACE = "com.deepin.api.XEventMonitor"


def _clean_env():
    """调系统工具(dbus-monitor/gdbus)用的净化环境。

    PyInstaller 包运行时把 bundle 目录塞进 LD_LIBRARY_PATH，spawn 出来的系统
    二进制会被拐去加载包内低版本 .so 而崩溃（实测：系统 dbus-monitor 加载包内
    libdbus-1.so.3 缺 LIBDBUS_PRIVATE_1.12.20 符号，rc=1 秒退）。此处剥离 bundle
    目录，让系统工具走系统库。
    """
    env = dict(os.environ)
    bundle_dir = getattr(sys, "_MEIPASS", None)
    ld_path = env.get("LD_LIBRARY_PATH", "")
    if ld_path:
        parts = [p for p in ld_path.split(":") if p and p != bundle_dir]
        if parts:
            env["LD_LIBRARY_PATH"] = ":".join(parts)
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env

EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)
EV_KEY = 0x01

# X11 button 编号 -> KeymouseGo action_type
BUTTON_TO_NAME = {1: "left", 2: "middle", 3: "right"}

# evdev keycode -> keyname(命名与 KeymouseGo 现有约定一致: 小写字母/数字/f1-f12 等)
KEYCODE_TO_NAME = {
    1: "escape", 2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6",
    8: "7", 9: "8", 10: "9", 11: "0", 12: "minus", 13: "equal",
    14: "backspace", 15: "tab",
    16: "q", 17: "w", 18: "e", 19: "r", 20: "t", 21: "y", 22: "u",
    23: "i", 24: "o", 25: "p", 26: "[", 27: "]", 28: "enter",
    29: "ctrl",
    30: "a", 31: "s", 32: "d", 33: "f", 34: "g", 35: "h", 36: "j",
    37: "k", 38: "l", 39: ";", 40: "'", 41: "`", 42: "shift",
    43: "backslash",
    44: "z", 45: "x", 46: "c", 47: "v", 48: "b", 49: "n", 50: "m",
    51: ",", 52: ".", 53: "/", 54: "shiftright", 56: "alt",
    57: "space", 58: "capslock",
    59: "f1", 60: "f2", 61: "f3", 62: "f4", 63: "f5", 64: "f6",
    65: "f7", 66: "f8", 67: "f9", 68: "f10", 69: "numlock",
    70: "scrolllock",
    97: "ctrlright", 100: "altright", 125: "win", 126: "winright",
}


def _get_delay(action_type):
    delay = globalv.current_ts() - globalv.latest_time
    mouse_move_interval_ms = globalv.mouse_interval_ms or 999999
    if action_type == "mouse move" and delay < mouse_move_interval_ms:
        return -1
    if globalv.latest_time < 0:
        delay = 0
    globalv.latest_time = globalv.current_ts()
    return delay


# 双通道去重：同一物理动作可能同时触发 XEventMonitor 与 pynput，
# 60ms 内同类型、坐标差<=2px 的视为重复，直接丢弃后者
_last_mouse = {"action_type": None, "x": None, "y": None, "ts": 0}
_DEDUP_MS = 60
_DEDUP_PX = 2
_mouse_lock = threading.Lock()


def _emit_mouse(action_type, x, y):
    now = globalv.current_ts()
    with _mouse_lock:
        last = _last_mouse
        if (last["action_type"] == action_type and last["x"] is not None
                and abs(x - last["x"]) <= _DEDUP_PX
                and abs(y - last["y"]) <= _DEDUP_PX
                and (now - last["ts"]) < _DEDUP_MS):
            return
        last["action_type"] = action_type
        last["x"] = x
        last["y"] = y
        last["ts"] = now
    delay = _get_delay(action_type)
    if delay < 0:
        return
    record_signals.cursor_pos_change.emit((x, y))
    event = globalv.ScriptEvent({
        "delay": delay,
        "event_type": "EM",
        "action_type": action_type,
        "action": (x, y),
    })
    record_signals.event_signal.emit(event)


def _emit_key(key_code, down):
    key_name = KEYCODE_TO_NAME.get(key_code, "key%d" % key_code)
    action_type = "key down" if down else "key up"
    delay = _get_delay(action_type)
    if delay < 0:
        return
    event = globalv.ScriptEvent({
        "delay": delay,
        "event_type": "EK",
        "action_type": action_type,
        "action": (key_code, key_name, 0),
    })
    record_signals.event_signal.emit(event)


# ==================== 鼠标监听(XEventMonitor) ====================

_MEMBER_RE = re.compile(r"member=([A-Za-z]+)")


def _mouse_monitor_thread():
    rule = "interface='{}'".format(XEM_INTERFACE)
    try:
        proc = subprocess.Popen(
            ["dbus-monitor", "--session", rule],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=_clean_env())
    except (OSError, ValueError) as e:
        logger.error("启动 dbus-monitor 监听 XEventMonitor 失败: {}", e)
        return

    # 启动后检查进程是否存活：连不上 session bus 会立刻退出，以前是静默死
    time.sleep(0.5)
    if proc.poll() is not None:
        try:
            err = proc.stderr.read().decode("utf-8", errors="replace").strip()
        except Exception:
            err = ""
        logger.error("dbus-monitor 启动后即退出 rc={} err={}", proc.returncode, err)
        return
    logger.info("dbus-monitor 进程存活，开始监听 XEventMonitor 信号")

    header = None
    args = []
    try:
        for raw in proc.stdout:
            text = raw.decode("utf-8", errors="replace").rstrip(chr(13) + chr(10))
            if "member=" in text and XEM_INTERFACE in text:
                header = text
                args = []
            elif text.strip() == "":
                if header is not None:
                    _handle_mouse_block(header, args)
                header = None
                args = []
            elif header is not None:
                args.append(text.strip())
    except Exception as e:
        logger.error("XEventMonitor 监听线程异常: {}", e)


def _handle_mouse_block(header, args):
    m = _MEMBER_RE.search(header)
    if not m:
        return
    member = m.group(1)
    logger.debug("收到 XEventMonitor 信号 {} 参数 {}", member, args)
    ints = []
    for line in args:
        if line.startswith("int32 "):
            try:
                ints.append(int(line.split()[1]))
            except (IndexError, ValueError):
                pass

    if member == "CursorMove" and len(ints) >= 2:
        _emit_mouse("mouse move", ints[0], ints[1])
    elif member in ("ButtonPress", "ButtonRelease") and len(ints) >= 3:
        button = ints[0]
        x, y = ints[1], ints[2]
        is_press = (member == "ButtonPress")
        if button in BUTTON_TO_NAME:
            name = BUTTON_TO_NAME[button]
            action_type = "mouse %s %s" % (name, "down" if is_press else "up")
            _emit_mouse(action_type, x, y)
        elif button == 4:
            _emit_mouse("mouse wheel up", x, y)
        elif button == 5:
            _emit_mouse("mouse wheel down", x, y)


# ==================== 键盘监听(/dev/input) ====================

_EVENT_RE = re.compile(r"event[0-9]+")


def _find_keyboard_event():
    current = {}
    try:
        with open("/proc/bus/input/devices", "r") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    name = current.get("name", "")
                    handlers = current.get("handlers", "")
                    if ("keyboard" in name.lower() or "kbd" in name.lower()) \
                            and "event" in handlers:
                        m = _EVENT_RE.search(handlers)
                        if m:
                            return m.group(0)
                    current = {}
                elif line.startswith("N: Name="):
                    current["name"] = line.split("=", 1)[1].strip().strip('"')
                elif line.startswith("H: Handlers="):
                    current["handlers"] = line.split("=", 1)[1].strip()
    except OSError as e:
        logger.error("读 /proc/bus/input/devices 失败: {}", e)
    return None


def _keyboard_monitor_thread(event_dev):
    path = "/dev/input/%s" % event_dev
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as e:
        logger.error("打开键盘设备 {} 失败: {}", path, e)
        return
    try:
        while True:
            data = os.read(fd, EVENT_SIZE)
            if not data or len(data) != EVENT_SIZE:
                continue
            tsec, tusec, etype, code, value = struct.unpack(EVENT_FORMAT, data)
            if etype != EV_KEY:
                continue
            if value == 1:
                _emit_key(code, True)
            elif value == 0:
                _emit_key(code, False)
    except OSError as e:
        logger.error("键盘监听线程异常: {}", e)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _register_fullscreen():
    """向 XEventMonitor 注册全屏监听。必须先注册 daemon 才会广播信号；
    只被动监听(dbus-monitor)而从不注册，是部分窗口收不到信号的根因。"""
    cmd = ["gdbus", "call", "--session",
           "--dest", "com.deepin.api.XEventMonitor",
           "--object-path", "/com/deepin/api/XEventMonitor",
           "--method", "com.deepin.api.XEventMonitor.RegisterFullScreen"]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=10,
                              env=_clean_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.error("RegisterFullScreen 调用失败: {}", e)
        return None
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        logger.error("RegisterFullScreen 返回 rc={} err={}", proc.returncode, err)
        return None
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    logger.info("RegisterFullScreen 成功, id={}", out)
    return out


# ==================== 鼠标监听(pynput/XWayland) ====================

_PYNPUT_BUTTON = {}
if _HAS_PYNPUT:
    _PYNPUT_BUTTON = {
        _pynput_mouse.Button.left: "left",
        _pynput_mouse.Button.right: "right",
        _pynput_mouse.Button.middle: "middle",
    }


def _pynput_on_move(x, y):
    try:
        _emit_mouse("mouse move", int(x), int(y))
    except Exception as e:
        logger.error("pynput move 回调异常: {}", e)


def _pynput_on_click(x, y, button, pressed):
    name = _PYNPUT_BUTTON.get(button)
    if name is None:
        return
    try:
        _emit_mouse("mouse {} {}".format(name, "down" if pressed else "up"),
                    int(x), int(y))
    except Exception as e:
        logger.error("pynput click 回调异常: {}", e)


def _pynput_on_scroll(x, y, dx, dy):
    try:
        _emit_mouse("mouse wheel {}".format("down" if dy < 0 else "up"),
                    int(x), int(y))
    except Exception as e:
        logger.error("pynput scroll 回调异常: {}", e)


def _start_pynput_mouse():
    """启动 pynput 鼠标监听（覆盖 XWayland 窗口）。X 不可用则降级，不抛异常。"""
    if not _HAS_PYNPUT:
        logger.warning("pynput 不可用({})，仅 XEventMonitor 单通道录制", _PYNPUT_ERR)
        return
    try:
        listener = _pynput_mouse.Listener(
            on_move=_pynput_on_move,
            on_click=_pynput_on_click,
            on_scroll=_pynput_on_scroll)
        listener.start()
        logger.info("pynput 鼠标监听已启动（覆盖 XWayland 窗口）")
    except Exception as e:
        logger.error("pynput 鼠标监听启动失败: {}，仅 XEventMonitor 单通道录制", e)


def setuphook(commandline=False):
    _register_fullscreen()
    mouse_thread = threading.Thread(target=_mouse_monitor_thread, daemon=True)
    mouse_thread.start()
    logger.info("XEventMonitor 鼠标监听已启动")
    _start_pynput_mouse()

    event_dev = _find_keyboard_event()
    if event_dev:
        kb_thread = threading.Thread(
            target=_keyboard_monitor_thread, args=(event_dev,), daemon=True)
        kb_thread.start()
        logger.info("键盘监听已启动: /dev/input/{}", event_dev)
    else:
        logger.warning("未识别到键盘设备, 键盘录制不可用")