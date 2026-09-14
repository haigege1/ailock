"""空闲时间与进程存活检测。"""

import ctypes
from ctypes import wintypes

from .winapi import kernel32, user32


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds() -> float:
    """返回用户最后一次键鼠操作至今的秒数。"""
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    elapsed_ms = kernel32.GetTickCount() - info.dwTime
    return max(0.0, elapsed_ms / 1000.0)


class IdleWatcher:
    """后台轮询空闲时长，超过阈值触发回调。"""

    def __init__(self, threshold_seconds: float, on_idle, poll_seconds: float = 5.0):
        import threading
        self.threshold = float(threshold_seconds)
        self.on_idle = on_idle
        self.poll = float(poll_seconds)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="IdleWatcher")

    def _run(self) -> None:
        while not self._stop.wait(self.poll):
            if self.threshold <= 0:
                continue
            try:
                if idle_seconds() >= self.threshold:
                    self.on_idle()
            except Exception:
                pass

    def start(self):
        if self.threshold > 0:
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
