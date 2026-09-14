"""在线壁纸模式端到端冒烟（offscreen，不弹真屏）。

流程：mode=online 构造 LockScreen → show 后自动请求壁纸 → 后台 fetch
→ refresh_files → 断言：
1. 壁纸列表非空（缓存目录收集成功）
2. bgLabel 真正渲染出位图（完整走完 服务→信号→set_background 链路）
3. 轮播定时器已启动，手动切换索引前进

运行：python tools/smoke_online_wallpaper.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

OK = 0
FAIL = 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def main() -> int:
    app = QApplication.instance() or QApplication([])

    from core.config import Config
    from core.wallpaper_fetch import fetch_async
    from ui.qt.lock_window import LockScreen

    cfg = Config(path=os.path.join(tempfile.mkdtemp(), "config.json"))
    cfg.set("background.mode", "online")
    cfg.set("background.slideshow_seconds", 30)

    ls = LockScreen(None, cfg, lambda p: (False, ""))
    ls.show(0)
    app.processEvents()

    check("锁屏窗口已创建", len(ls.windows) >= 1)
    check("在线模式收集到壁纸缓存", ls._svc.count >= 1,
          f"count={ls._svc.count}")

    # 等待壁纸渲染完成（缓存命中也走后台线程，需轮询事件循环）
    deadline = time.time() + 30
    pm = None
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
        pm = ls.windows[0].bgLabel.pixmap()
        if pm is not None and not pm.isNull():
            break
    check("壁纸位图已渲染到 bgLabel", pm is not None and not pm.isNull())

    # 后台 fetch + refresh_files 链路
    done = {}
    fetch_async(days=2, on_done=lambda n: done.update(n=n))
    deadline = time.time() + 60
    while time.time() < deadline and "n" not in done:
        app.processEvents()
        time.sleep(0.05)
    check("fetch_async 回调到达", "n" in done, f"done={done}")
    ls.refresh_files()
    app.processEvents()
    check("refresh_files 后列表仍非空", ls._svc.count >= 1)

    # 轮播定时器与手动切换
    check("自动轮播定时器已启动", ls._slide is not None and ls._slide.isActive())
    before = ls._svc.index
    ls._on_switch(1)
    app.processEvents()
    check("手动切换索引前进", ls._svc.index != before or ls._svc.count == 1,
          f"{before} -> {ls._svc.index}")

    ls.destroy()
    app.processEvents()
    print(f"\n结果：{OK} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
