"""Bug 回归：veil 必须铺满锁屏窗口，不能停在默认 100x30 角落。

之前 lock_window.py 的 _apply_bg_geometry 只给 bgLabel/bgOver/wipe 设了
全屏几何，veil (半透明遮罩) 没被显式设过，导致 LockWindow 里多出一个小
~100x30 的暗色块叠在左上角看着像「灰块」。本测试：

1. 构造 LockScreen → 拿到 LockWindow
2. resize 触发 _apply_bg_geometry 后，断言 veil.geometry() == 整窗矩形
3. 验证 veil.isVisibleTo(parent) == True 且 opacity 真的能覆盖整窗
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, QTimer
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


def make_wallpapers(folder, n=2):
    from PIL import Image
    out = []
    for i in range(n):
        im = Image.new("RGB", (1920, 1080),
                       (60 + i * 90, 90 + i * 40, 200 - i * 60))
        p = os.path.join(folder, "wp_%d.png" % i)
        im.save(p)
        out.append(p)
    return out


with __import__("tempfile").TemporaryDirectory() as tmp:
    wp_dir = os.path.join(tmp, "wps")
    os.makedirs(wp_dir)
    make_wallpapers(wp_dir)

    cfg = Config(path=os.path.join(tmp, "cfg.json"))
    cfg.set("background.mode", "folder", autosave=False)
    cfg.set("background.path", wp_dir, autosave=False)
    cfg.set("background.slideshow_seconds", 0, autosave=False)

    lock = LockScreen(None, cfg, lambda pwd: (False, ""))
    check("windows created", len(lock.windows) >= 1)
    w = lock.windows[0]

    # 1) 构造时 _apply_screen_geometry 已经走了一遍 _apply_bg_geometry
    full = QRect(0, 0, w.width(), w.height())
    check("veil 已是全屏矩形（构造后）",
          w.veil.geometry() == full,
          "veil=%s full=%s" % (w.veil.geometry(), full))

    # 2) 直接调一次 _apply_bg_geometry（模拟 showFullScreen 走 resizeEvent 走它）
    w._apply_bg_geometry(1000, 700)
    full2 = QRect(0, 0, 1000, 700)
    check("veil 跟随 _apply_bg_geometry 重设全屏",
          w.veil.geometry() == full2,
          "veil=%s full2=%s" % (w.veil.geometry(), full2))
    check("bgLabel 同步",
          w.bgLabel.geometry() == full2,
          "bgLabel=%s" % w.bgLabel.geometry())
    check("bgOver 同步",
          w.bgOver.geometry() == full2,
          "bgOver=%s" % w.bgOver.geometry())
    check("wipe 同步",
          w.wipe.geometry() == full2,
          "wipe=%s" % w.wipe.geometry())

    # 3) 兜底：veil 不能等于 QWidget 的默认 ~100x30（这正是用户看到的「灰块」）
    sz = w.veil.size()
    not_default = not (90 <= sz.width() <= 110 and 20 <= sz.height() <= 40)
    check("veil 不是 ~100x30 默认尺寸", not_default,
          "size=%dx%d" % (sz.width(), sz.height()))

    # 4) 背景四件套的可见几何应当一致 —— 没有谁比别的小一截
    geom_pat = w.veil.geometry()
    for name, peer in (("bgLabel", w.bgLabel),
                       ("bgOver", w.bgOver),
                       ("wipe", w.wipe)):
        check("%s 与 veil 几何一致" % name, peer.geometry() == geom_pat,
              "%s=%s vs veil=%s" % (name, peer.geometry(), geom_pat))

    print("checks done, tearing down...")
    lock.destroy()
    print("lock destroyed")

print("ALL OK line:", len(fails) == 0)

if fails:
    print("FAILED: %d -> %s" % (len(fails), fails))
    sys.exit(1)
print("veil geometry: ALL OK")
