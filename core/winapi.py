"""共享的 Win32 ctypes 封装。"""

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

try:  # Win8.1+
    shcore = ctypes.WinDLL("shcore")
except OSError:  # pragma: no cover
    shcore = None

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_FRAMECHANGED = 0x0020

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOPMOST = 0x00000008
WS_EX_LAYERED = 0x00080000

MONITORINFOF_PRIMARY = 0x00000001

SM_CXSCREEN = 0
SM_CYSCREEN = 1

# LONG_PTR 不在 wintypes 里，按指针宽度自己定义，保证 32/64 位都对
LONG_PTR = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long

# GetWindowLongPtr 在 32/64 位下的正确绑定
try:
    _GetWindowLongPtr = user32.GetWindowLongPtrW
    _SetWindowLongPtr = user32.SetWindowLongPtrW
except AttributeError:  # pragma: no cover
    _GetWindowLongPtr = user32.GetWindowLongW
    _SetWindowLongPtr = user32.SetWindowLongW

_GetWindowLongPtr.argtypes = [wintypes.HWND, ctypes.c_int]
_GetWindowLongPtr.restype = LONG_PTR
_SetWindowLongPtr.argtypes = [wintypes.HWND, ctypes.c_int, LONG_PTR]
_SetWindowLongPtr.restype = LONG_PTR

user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.SetWindowPos.restype = wintypes.BOOL


def set_dpi_awareness() -> None:
    """必须在创建任何窗口之前调用。

    使用 System DPI Aware（而不是 Per-Monitor），这样 tkinter 的坐标体系与
    EnumDisplayMonitors 返回的虚拟屏幕坐标一致，多屏缩放下窗口不会错位或漏边。
    """
    if shcore is not None:
        try:
            # PROCESS_SYSTEM_DPI_AWARE = 1
            shcore.SetProcessDpiAwareness(1)
            return
        except Exception:
            pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def get_ex_style(hwnd: int) -> int:
    return int(_GetWindowLongPtr(hwnd, GWL_EXSTYLE))


def set_ex_style(hwnd: int, style: int) -> None:
    _SetWindowLongPtr(hwnd, GWL_EXSTYLE, LONG_PTR(style))


def make_lock_window(hwnd: int, x: int, y: int, w: int, h: int) -> None:
    """把一个顶层窗口改成「全屏置顶 + 从 Alt-Tab 隐藏」的锁屏窗口。

    WS_EX_TOOLWINDOW 让它不出现在任务栏和 Alt-Tab 列表里，
    去掉 WS_EX_APPWINDOW 进一步确保不会以「应用窗口」身份出现。
    """
    style = get_ex_style(hwnd)
    style = (style | WS_EX_TOOLWINDOW | WS_EX_TOPMOST) & ~WS_EX_APPWINDOW
    set_ex_style(hwnd, style)
    user32.SetWindowPos(
        hwnd, wintypes.HWND(HWND_TOPMOST),
        int(x), int(y), int(w), int(h),
        SWP_NOACTIVATE | SWP_SHOWWINDOW,
    )


def keep_on_top(hwnd: int) -> None:
    """周期性重新声明置顶，用于与其它抢 z-order 的程序对抗。"""
    user32.SetWindowPos(
        hwnd, wintypes.HWND(HWND_TOPMOST), 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
    )


def hwnd_of_toplevel(tk_widget) -> int:
    """从 tkinter Toplevel / Tk 取得真正的顶层 HWND。"""
    try:
        return int(str(tk_widget.frame()), 16)
    except Exception:  # pragma: no cover
        parent = user32.GetParent(int(tk_widget.winfo_id()))
        return int(parent) or int(tk_widget.winfo_id())


def get_foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow() or 0)


def is_process_running(pid: int) -> bool:
    """不依赖 psutil 的进程存活检测。"""
    if pid <= 0:
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def relaunch_self(extra_args: list) -> None:
    """以与当前进程相同的方式重新启动自己。"""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable] + list(extra_args)
    else:
        cmd = [sys.executable, os.path.abspath(sys.argv[0])] + list(extra_args)
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(cmd, creationflags=creationflags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True)
