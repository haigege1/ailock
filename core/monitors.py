"""多显示器枚举。"""

from .logger import log

try:
    import win32api
    _HAS_PYWIN32 = True
except ImportError:  # pragma: no cover
    _HAS_PYWIN32 = False


def list_monitors() -> list:
    """返回按「主屏优先、其次左到右」排序的显示器列表。

    每项: {"handle": int, "rect": (l, t, r, b), "primary": bool,
           "x": int, "y": int, "w": int, "h": int}
    """
    if not _HAS_PYWIN32:
        return _fallback_virtual_screen()

    monitors = []
    try:
        for h_mon, _hdc, _rect in win32api.EnumDisplayMonitors():
            info = win32api.GetMonitorInfo(h_mon)
            l, t, r, b = info["Monitor"]
            monitors.append({
                "handle": int(h_mon),
                "rect": (int(l), int(t), int(r), int(b)),
                "primary": bool(int(info.get("Flags", 0)) & 1),
                "x": int(l), "y": int(t),
                "w": int(r - l), "h": int(b - t),
            })
    except Exception as exc:
        log.log("monitor.error", repr(exc))
        return _fallback_virtual_screen()

    monitors.sort(key=lambda m: (not m["primary"], m["x"], m["y"]))
    return monitors


def _fallback_virtual_screen() -> list:
    from .winapi import user32, SM_CXSCREEN, SM_CYSCREEN
    w = user32.GetSystemMetrics(SM_CXSCREEN)
    h = user32.GetSystemMetrics(SM_CYSCREEN)
    return [{
        "handle": 0, "rect": (0, 0, w, h), "primary": True,
        "x": 0, "y": 0, "w": w, "h": h,
    }]


WM_DISPLAYCHANGE = 0x007E


def on_display_change(handler) -> None:
    """显示器插拔 / 分辨率变化时回调。需要在带消息循环的线程里调用。"""
    if not _HAS_PYWIN32:
        return
    try:
        import win32gui
        win32gui.__dict__.setdefault("_ailock_display_handlers", []).append(handler)
    except Exception:
        pass
