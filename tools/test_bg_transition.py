"""壁纸切换动效冒烟：5 种模式各切一次，验证过渡启动→完成→落底层全链路。

用法：
    .venv/Scripts/python.exe -u tools/test_bg_transition.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from core.config import Config
from ui.qt.lock_window import LockScreen

app = QApplication(sys.argv)

fails = []


def check(name, cond, detail=""):
    tag = "ok" if cond else "FAIL"
    print("  [%s] %s %s" % (tag, name, detail))
    if not cond:
        fails.append(name)


def make_wallpapers(folder: str, n: int = 3):
    """用 PIL 生成 n 张可区分的纯色渐变图。"""
    from PIL import Image
    paths = []
    for i in range(n):
        base = (60 + i * 70, 90, 200 - i * 50)
        im = Image.new("RGB", (800, 600))
        px = im.load()
        for x in range(800):
            for y in range(0, 600, 20):
                px[x, y] = tuple(min(255, v + x // 8) for v in base)
        p = os.path.join(folder, "wp_%d.png" % i)
        im.save(p)
        paths.append(p)
    return paths


MODES = ["none", "fade", "slide", "zoom", "wipe"]

with tempfile.TemporaryDirectory() as tmp:
    wp_dir = os.path.join(tmp, "wps")
    os.makedirs(wp_dir)
    paths = make_wallpapers(wp_dir)

    cfg = Config(path=os.path.join(tmp, "cfg.json"))
    cfg.set("background.mode", "folder", autosave=False)
    cfg.set("background.path", wp_dir, autosave=False)
    cfg.set("background.slideshow_seconds", 0, autosave=False)  # 关自动轮播
    cfg.set("background.dim", 0.2, autosave=False)

    screen = LockScreen(None, cfg, lambda pwd: (False, ""))
    check("windows created", len(screen.windows) >= 1)
    w = screen.windows[0]

    results = {}   # mode -> dict(done=bool, under_ok=bool)
    state = {"mode_i": 0, "awaiting": False}

    def pump_mode():
        """切换到下一个动效模式并触发一次壁纸切换。"""
        i = state["mode_i"]
        if i >= len(MODES):
            report()
            return
        mode = MODES[i]
        state["mode_i"] = i + 1
        state["awaiting"] = True
        print("mode %d/5: %s" % (i + 1, mode))
        cfg.set("background.transition", mode, autosave=False)
        screen._on_switch(1)

    def watch_mode():
        """每次动效结束后核对状态，再进入下一种。"""
        if not state["awaiting"]:
            return
        state["awaiting"] = False
        mode = MODES[state["mode_i"] - 1]
        done = (w._trans is None and not w.bgOver.isVisible()
                and not w.wipe.isVisible())
        under_ok = w._under_pm is not None and not w._under_pm.isNull()
        results[mode] = {"done": done, "under_ok": under_ok}
        QTimer.singleShot(200, pump_mode)

    # 轮询盯梢：壁纸在后台线程解码，到货时机不定，每 150ms 看一眼
    def poll():
        if state["awaiting"] and w._trans is None and state["mode_i"] > 0:
            watch_mode()
        QTimer.singleShot(150, poll)

    def report():
        for mode in MODES:
            r = results.get(mode, {})
            check("%s: 过渡收尾干净" % mode, bool(r.get("done")))
            check("%s: 新壁纸落底层" % mode, bool(r.get("under_ok")))
        screen.destroy()
        app.quit()

    screen.show(0)
    QTimer.singleShot(600, pump_mode)   # 等首张壁纸铺好（首次不做过渡）
    QTimer.singleShot(200, poll)
    # 硬超时兜底
    QTimer.singleShot(30000, report)

app.exec()

print()
if fails:
    print("FAILED: %d 项未通过 -> %s" % (len(fails), fails))
    sys.exit(1)
print("bg transition tests: ALL OK")
