from platform import system
import os


def is_deepin_wayland():
    """判断是否为 deepin(统信UOS) 的 Wayland 会话。满足时才启用 DeepinEvents 后端。"""
    if system() != 'Linux':
        return False
    session_type = os.environ.get('XDG_SESSION_TYPE', '')
    desktop = os.environ.get('XDG_CURRENT_DESKTOP', '')
    return session_type.lower() == 'wayland' and 'deepin' in desktop.lower()


if system() == 'Windows':
    import Event.WindowsEvents as _Event
    event_cls = _Event.WindowsEvent
    flag_multiplemonitor = _Event.numofmonitors > 1
elif is_deepin_wayland():
    import Event.DeepinEvents as _Event
    event_cls = _Event.DeepinEvent
    # 存绝对像素坐标, 让 UIFunc 跳过 % 格式化
    flag_multiplemonitor = True
elif system() in ['Linux', 'Darwin']:
    import Event.UniversalEvents as _Event
    event_cls = _Event.UniversalEvent
    flag_multiplemonitor = False
else:
    raise OSError("Unsupported platform '{}'".format(system()))

ScriptEvent = event_cls
ScreenWidth = _Event.SW
ScreenHeight = _Event.SH