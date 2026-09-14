"""开机自启（写入当前用户的 Run 注册表项，不需要管理员权限）。"""

from .logger import log

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "AiLock"


def _open_key(write: bool = False):
    import winreg
    access = winreg.KEY_READ
    if write:
        access = winreg.KEY_SET_VALUE | winreg.KEY_READ
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)


def exe_command() -> str:
    """生成开机自启的命令行。"""
    import sys
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    import os
    return f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}"'


def is_enabled() -> bool:
    import winreg
    try:
        with _open_key() as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except Exception:
        return False


def enable() -> bool:
    import winreg
    try:
        with _open_key(write=True) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, exe_command())
        log.log("autostart.enable", exe_command())
        return True
    except Exception as exc:
        log.log("autostart.error", repr(exc))
        return False


def disable() -> bool:
    import winreg
    try:
        with _open_key(write=True) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        log.log("autostart.disable", "已移除开机自启")
        return True
    except FileNotFoundError:
        return True
    except Exception as exc:
        log.log("autostart.error", repr(exc))
        return False


def sync(enabled: bool) -> bool:
    return enable() if enabled else disable()
