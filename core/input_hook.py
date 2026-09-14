"""低层键盘钩子：屏蔽能把人从锁屏带走的组合键。

实现要点（踩过的坑都写在注释里）：
1. WH_KEYBOARD_LL 是全局钩子，回调由系统在安装钩子的线程上下文里调用，
   所以安装钩子的线程**必须**跑消息循环，否则 Windows 会静默摘掉钩子。
2. 回调对象必须被长期持有（存成实例属性），一旦被 GC，回调时直接崩进程。
3. 只吞掉特定组合，普通按键一律 CallNextHookEx 放行，否则密码框没法打字。
"""

import ctypes
import threading
from ctypes import wintypes

from .winapi import kernel32, user32
from .logger import log

WH_KEYBOARD_LL = 13
HC_ACTION = 0

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_TAB = 0x09
VK_ESCAPE = 0x1B
VK_F4 = 0x73
VK_SPACE = 0x20
VK_SNAPSHOT = 0x2C
VK_MENU = 0x12      # Alt
VK_CONTROL = 0x11
VK_DELETE = 0x2E
VK_L = 0x4C

PM_REMOVE = 0x0001
WM_QUIT = 0x0012


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


HOOKPROC = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
)

user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD
]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.CallNextHookEx.argtypes = [
    wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
]
user32.CallNextHookEx.restype = ctypes.c_longlong
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short


class KeyboardBlocker(threading.Thread):
    """屏蔽 Win 键、Alt+Tab、Alt+Esc、Ctrl+Esc、Alt+F4、Alt+Space、PrintScreen。

    拦不住的（Windows 安全序列，用户态无权截获，属于设计限制而非缺陷）：
        Ctrl+Alt+Del      —— 由 Winlogon 在安全桌面处理
        Ctrl+Shift+Esc    —— 直达任务管理器
    """

    def __init__(self):
        super().__init__(daemon=True, name="KeyboardBlocker")
        self._hhook = None
        self._thread_id = None
        self._stop_event = threading.Event()
        self._ready = threading.Event()
        self._proc_ref = None          # 必须持有，防止回调被 GC
        self.blocked_count = 0

    # ---------- 判定 ----------
    @staticmethod
    def _held(vk: int) -> bool:
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)

    def _should_block(self, vk: int) -> bool:
        if vk in (VK_LWIN, VK_RWIN):
            return True
        alt = self._held(VK_MENU)
        ctrl = self._held(VK_CONTROL)

        if vk == VK_TAB and alt:
            return True                      # Alt+Tab 切换窗口
        if vk == VK_ESCAPE and (alt or ctrl):
            return True                      # Alt+Esc / Ctrl+Esc 开始菜单
        if vk == VK_F4 and alt:
            return True                      # Alt+F4 关闭窗口
        if vk == VK_SPACE and alt:
            return True                      # Alt+Space 系统菜单
        if vk == VK_SNAPSHOT:
            return True                      # PrintScreen 截图
        if vk == VK_DELETE and ctrl and alt:
            return True                      # 尽力而为，通常拦不住
        return False

    # ---------- 回调 ----------
    def _hook_proc(self, nCode, wParam, lParam):
        if nCode == HC_ACTION and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
            try:
                kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if self._should_block(int(kb.vkCode)):
                    self.blocked_count += 1
                    return 1                 # 返回非 0 = 吞掉，不再向下传递
            except Exception:
                pass
        return user32.CallNextHookEx(self._hhook, nCode, wParam, lParam)

    # ---------- 线程主体 ----------
    def run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        self._proc_ref = HOOKPROC(self._hook_proc)

        # hMod 必须传 NULL：WH_KEYBOARD_LL 是全局钩子，不注入 DLL，
        # 回调由系统在安装线程里直接调用。传 GetModuleHandle(None) 会得到
        # ERROR_MOD_NOT_FOUND(126)，这是踩过的坑。
        self._hhook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._proc_ref, wintypes.HINSTANCE(0), 0,
        )
        if not self._hhook:
            err = ctypes.get_last_error()
            log.log("hook.error",
                    f"SetWindowsHookEx 失败, GetLastError={err}")
            self._ready.set()
            return

        log.log("hook.start", f"低层键盘钩子已安装, tid={self._thread_id}")
        self._ready.set()

        msg = wintypes.MSG()
        while not self._stop_event.is_set():
            # PeekMessage 而非 GetMessage：GetMessage 会阻塞，无法及时响应停止信号
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_QUIT:
                    self._stop_event.set()
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            kernel32.Sleep(5)

        if self._hhook:
            user32.UnhookWindowsHookEx(self._hhook)
            self._hhook = None
        self._proc_ref = None
        log.log("hook.stop", f"钩子已卸载, 共拦截 {self.blocked_count} 次")

    # ---------- 控制 ----------
    def start(self):
        super().start()
        self._ready.wait(timeout=5)
        return self

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self.is_alive():
            self.join(timeout=3)

    @property
    def installed(self) -> bool:
        return bool(self._hhook)
