import os
import sys
import math
from PySide6.QtWidgets import QApplication, QWidget, QSpinBox
from PySide6.QtCore import Qt, Slot, QRect

import UIFunc
import Recorder
import argparse
from Event import ScriptEvent
from loguru import logger

from Plugin.Manager import PluginManager
from Util.RunScriptClass import RunScriptCMDClass, StopFlag


def to_abs_path(*args):
    return os.path.join(os.path.dirname(os.path.realpath(sys.argv[0])),
                        *args)


def resize_layout(ui, ratio_w, ratio_h):
    ui.resize(ui.width() * ratio_w, ui.height() * ratio_h)

    for q_widget in ui.findChildren(QWidget):
        q_widget.setGeometry(QRect(q_widget.x() * ratio_w,
                                   q_widget.y() * ratio_h,
                                   q_widget.width() * ratio_w,
                                   q_widget.height() * ratio_h))
        q_widget.setStyleSheet('font-size: ' + str(
                                math.ceil(9 * min(ratio_h, ratio_w))) + 'px')
        if isinstance(q_widget, QSpinBox):
            q_widget.setStyleSheet('padding-left: 7px')


def ensure_deepin_session_bus():
    """deepin Wayland 且 root 运行时, 恢复登录用户的 D-Bus session bus 地址。

    回放鼠标/键盘走 uinput(需 root), 而录制走 XEventMonitor、键盘 WlSimulateKey
    走用户 session bus, 两者权限不同。root 进程默认丢 DBUS_SESSION_BUS_ADDRESS,
    会导致 D-Bus 调用失败(表现为探测 09 里 sudo 后 XEventMonitor 崩溃)。
    此处自动探测 /run/user/<uid>/bus 并恢复。
    """
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return
    if not hasattr(os, "geteuid"):  # Windows 无 os.geteuid
        return
    if os.geteuid() != 0:
        return
    if os.environ.get("XDG_SESSION_TYPE", "").lower() != "wayland":
        return
    import glob
    for bus_path in sorted(glob.glob("/run/user/*/bus")):
        uid = bus_path.split("/")[2]
        if uid == "0":
            continue
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + bus_path
        logger.debug("恢复 D-Bus session bus: %s", bus_path)
        break


def main():
    ensure_deepin_session_bus()

    app = QApplication(sys.argv)
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    ui = UIFunc.UIFunc(app)

    ui.setFixedSize(ui.width(), ui.height())
    ui.show()
    sys.exit(app.exec())


@logger.catch
def single_run(script_path, run_times):
    ensure_deepin_session_bus()
    flag = StopFlag(False)
    thread = RunScriptCMDClass(script_path, run_times, flag)

    stop_name = 'f9'

    @Slot(ScriptEvent)
    def on_keyboard_event(event):
        key_name = event.action[1].lower()
        if key_name == stop_name:
            logger.debug('break exit!')
            flag.value = True
            thread.resume()
        return True

    Recorder.setuphook(commandline=True)
    Recorder.set_callback(on_keyboard_event)

    PluginManager.reload()
    eventloop = QApplication()

    thread.finished.connect(eventloop.exit)
    thread.start()

    sys.exit(eventloop.exec_())


if __name__ == '__main__':
    logger.debug(sys.argv)
    if len(sys.argv) > 1:
        parser = argparse.ArgumentParser()
        parser.add_argument('scripts',
                            help='Path for the scripts',
                            type=str,
                            nargs='+'
                            )
        parser.add_argument('-rt', '--runtimes',
                            help='Run times for the script',
                            type=int,
                            default=1
                            )
        args = vars(parser.parse_args())
        logger.debug(args)
        single_run(args['scripts'],
                   run_times=args['runtimes']
                   )
    else:
        main()
