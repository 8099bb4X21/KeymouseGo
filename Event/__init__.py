from platform import system
import os


def is_deepin_wayland():
    """判断是否为 deepin(统信UOS) 的 Wayland 会话。满足时才启用 DeepinEvents 后端。"""
    if system() != 'Linux':
        return False
    session_type = os.environ.get('XDG_SESSION_TYPE', '').lower()
    desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()
    if session_type == 'wayland' and 'deepin' in desktop:
        return True
    # plain sudo 会清空 XDG_* 环境变量导致误判回 pynput；UOS20 默认 Wayland，
    # 此时用 /etc/os-release 兜底（明确 x11 会话的除外）
    if session_type == 'x11':
        return False
    try:
        with open('/etc/os-release', 'r', encoding='utf-8') as f:
            content = f.read().lower()
            if 'uos' in content or 'deepin' in content:
                return True
    except OSError:
        pass
    return False


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