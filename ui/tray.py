"""系统托盘图标 + 全局热键。

托盘需要一个带消息循环的隐藏窗口来接收通知区消息，全局热键同样依赖消息循环，
所以两者合并放在同一个后台线程里，共用一个隐藏窗口。
"""

import threading
import time

import win32api
import win32con
import win32gui

from core.logger import log

WM_TRAY = win32con.WM_USER + 20
WM_REREG = win32con.WM_USER + 21   # 请求托盘线程重新注册热键
WM_HOTKEY = 0x0312
WM_NULL = 0x0000

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010

MENU_BASE = 1000
HOTKEY_ID = 1

MOD_MAP = {
    "ctrl": win32con.MOD_CONTROL,
    "control": win32con.MOD_CONTROL,
    "alt": win32con.MOD_ALT,
    "shift": win32con.MOD_SHIFT,
    "win": win32con.MOD_WIN,
    "super": win32con.MOD_WIN,
}


def parse_hotkey(spec: str):
    """'ctrl+alt+l' -> (modifiers, virtual_key)"""
    modifiers = 0
    vk = 0
    for part in (spec or "").lower().replace(" ", "").split("+"):
        if not part:
            continue
        if part in MOD_MAP:
            modifiers |= MOD_MAP[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit():
            vk = 0x6F + int(part[1:])       # VK_F1 = 0x70
        elif part == "space":
            vk = win32con.VK_SPACE
        elif part == "esc":
            vk = win32con.VK_ESCAPE
    return modifiers, vk


class TrayApp(threading.Thread):
    """menu_items: [(显示文本, 回调)]，文本为 '-' 表示分隔线。"""

    def __init__(self, icon_path: str, tooltip: str = "AiLock",
                 menu_items=None, hotkey: str = "", on_hotkey=None,
                 on_ready=None):
        super().__init__(daemon=True, name="TrayApp")
        self.icon_path = icon_path
        self.tooltip = tooltip
        self.menu_items = list(menu_items or [])
        self.hotkey_spec = hotkey
        self.on_hotkey = on_hotkey
        self.on_ready = on_ready

        self.hwnd = None
        self._hicon = None
        self._ready = threading.Event()
        self._hotkey_error = None

    # ---------- 线程主体 ----------
    def run(self) -> None:
        hinst = win32api.GetModuleHandle(None)
        wc = win32gui.WNDCLASS()
        wc.hInstance = hinst
        wc.lpszClassName = "AiLockTrayWindowClass"
        wc.lpfnWndProc = {
            win32con.WM_DESTROY: self._on_destroy,
            win32con.WM_COMMAND: self._on_command,
            WM_TRAY: self._on_tray,
            WM_HOTKEY: self._on_hotkey,
            WM_REREG: self._on_rereg,
        }
        try:
            win32gui.RegisterClass(wc)
        except win32gui.error:
            pass

        self.hwnd = win32gui.CreateWindow(
            wc.lpszClassName, "AiLock", 0, 0, 0, 0, 0, 0, 0, hinst, None)
        win32gui.UpdateWindow(self.hwnd)
        log.log("tray.start", f"隐藏窗口 hwnd={self.hwnd}")

        self._add_icon()
        self._register_hotkey()

        self._ready.set()
        if self.on_ready:
            try:
                self.on_ready(self)
            except Exception:
                pass

        win32gui.PumpMessages()

    def wait_ready(self, timeout: float = 5.0) -> bool:
        return self._ready.wait(timeout)

    # ---------- 图标 ----------
    def _add_icon(self) -> None:
        hinst = win32api.GetModuleHandle(None)
        try:
            self._hicon = win32gui.LoadImage(
                hinst, self.icon_path, win32con.IMAGE_ICON, 0, 0,
                win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE)
        except Exception as exc:
            log.log("tray.warn", f"载入图标失败: {exc!r}，改用系统默认图标")
            self._hicon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)

        nid = (self.hwnd, 0, NIF_ICON | NIF_MESSAGE | NIF_TIP,
               WM_TRAY, self._hicon, self.tooltip)
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, nid)
        except Exception as exc:
            log.log("tray.error", f"添加托盘图标失败: {exc!r}")

    def _remove_icon(self) -> None:
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, 0))
        except Exception:
            pass

    def balloon(self, title: str, message: str, timeout: int = 6) -> None:
        if not self.hwnd:
            return
        nid = (self.hwnd, 0, NIF_INFO, WM_TRAY, self._hicon, "",
               message, timeout * 1000, title, 0x00000001)
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, nid)
        except Exception:
            pass

    def update_tooltip(self, text: str) -> None:
        self.tooltip = text[:63]
        if not self.hwnd:
            return
        nid = (self.hwnd, 0, NIF_TIP, WM_TRAY, self._hicon, self.tooltip)
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, nid)
        except Exception:
            pass

    # ---------- 热键 ----------
    def _register_hotkey(self) -> None:
        if not self.hotkey_spec:
            return
        modifiers, vk = parse_hotkey(self.hotkey_spec)
        if not vk:
            self._hotkey_error = f"无法解析热键: {self.hotkey_spec}"
            log.log("tray.warn", self._hotkey_error)
            return
        try:
            win32gui.RegisterHotKey(self.hwnd, HOTKEY_ID, modifiers, vk)
            log.log("tray.hotkey", f"{self.hotkey_spec} -> mods={modifiers} vk={vk}")
        except Exception as exc:
            self._hotkey_error = f"注册热键失败（可能被其它程序占用）: {exc}"
            log.log("tray.warn", self._hotkey_error)

    def reregister_hotkey(self, spec: str) -> None:
        """设置里改了热键后重新注册。

        可从任意线程调用：RegisterHotKey/UnregisterHotKey 必须由
        窗口所属线程调用（否则报 1408 无效窗口），所以这里只投递
        一条消息，由托盘线程自己在 _on_rereg 里完成真正的注册。
        """
        self.hotkey_spec = spec
        if not self.hwnd:
            return
        try:
            win32gui.PostMessage(self.hwnd, WM_REREG, 0, 0)
        except Exception:
            pass  # 窗口已失效（正在退出），放弃

    def _on_rereg(self, hwnd, msg, wparam, lparam):
        try:
            win32gui.UnregisterHotKey(self.hwnd, HOTKEY_ID)
        except Exception:
            pass
        self._register_hotkey()
        return 0

    # ---------- 消息处理 ----------
    def _on_destroy(self, hwnd, msg, wparam, lparam):
        self._remove_icon()
        win32gui.PostQuitMessage(0)
        return 0

    def _on_command(self, hwnd, msg, wparam, lparam):
        cmd_id = win32api.LOWORD(wparam)
        index = cmd_id - MENU_BASE
        if 0 <= index < len(self.menu_items):
            _text, callback = self.menu_items[index]
            if callback:
                try:
                    callback()
                except Exception as exc:
                    log.log("tray.error", f"菜单回调异常: {exc!r}")
        return 0

    def _on_tray(self, hwnd, msg, wparam, lparam):
        if lparam in (win32con.WM_RBUTTONUP, win32con.WM_LBUTTONUP,
                      win32con.WM_CONTEXTMENU):
            self._show_menu()
        return 0

    def _on_hotkey(self, hwnd, msg, wparam, lparam):
        if wparam == HOTKEY_ID and self.on_hotkey:
            try:
                self.on_hotkey()
            except Exception as exc:
                log.log("tray.error", f"热键回调异常: {exc!r}")
        return 0

    def _show_menu(self) -> None:
        try:
            menu = win32gui.CreatePopupMenu()
            for i, (text, _cb) in enumerate(self.menu_items):
                if text == "-":
                    win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
                else:
                    win32gui.AppendMenu(menu, win32con.MF_STRING,
                                        MENU_BASE + i, text)
            pos = win32gui.GetCursorPos()
            win32gui.SetForegroundWindow(self.hwnd)
            win32gui.TrackPopupMenu(
                menu, win32con.TPM_LEFTALIGN | win32con.TPM_RIGHTBUTTON,
                pos[0], pos[1], 0, self.hwnd, None)
            # 这条空消息是为了让菜单点击后能正常收起（Windows 的经典坑）
            win32gui.PostMessage(self.hwnd, WM_NULL, 0, 0)
        except Exception as exc:
            log.log("tray.error", f"弹出菜单失败: {exc!r}")

    def stop(self) -> None:
        if self.hwnd:
            try:
                win32gui.PostMessage(self.hwnd, win32con.WM_DESTROY, 0, 0)
            except Exception:
                pass
        if self.is_alive():
            self.join(timeout=3)
