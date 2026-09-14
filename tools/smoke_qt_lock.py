"""Qt 锁屏冒烟：铺满所有屏幕 N 秒后自动退出，自带硬超时。

用法:
    .venv\\Scripts\\python.exe tools\\smoke_qt_lock.py [seconds]
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0


def _hard_exit():
    time.sleep(SECONDS + 12)
    print("[HARD TIMEOUT] force exit")
    sys.stdout.flush()
    os._exit(3)


threading.Thread(target=_hard_exit, daemon=True).start()

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication

from core.config import Config
from ui.qt.lock_window import LockScreen

QApplication.setHighDpiScaleFactorRoundingPolicy(
    Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
app = QApplication(sys.argv)

cfg = Config()
cfg.set("message.text", "AI 正在跑，请勿关机", autosave=False)
cfg.set("background.mode", "color", autosave=False)
cfg.set("background.color", "#0B1220", autosave=False)

attempts = []


def try_unlock(pwd):
    attempts.append(pwd)
    return False, "冒烟测试：不接受解锁（收到 %d 字符）" % len(pwd)


screen = LockScreen(None, cfg, try_unlock)
screen.show(0)
print("LockScreen windows:", len(screen.windows))
for i, w in enumerate(screen.windows):
    print("  win%d: %dx%d visible=%s onTop=%s" % (
        i, w.width(), w.height(), w.isVisible(),
        bool(w.windowFlags() & Qt.WindowStaysOnTopHint)))
    print("        clock=%r date=%r msg=%r" % (
        w.clock.text(), w.date.text(), w.msg.text()))

# 推两条会话状态，验证右侧会话卡堆叠渲染（多会话一会话一卡）
SAMPLES = [
    {
        "sid": "zc-a1b2",
        "task": "Qwen2.5-Coder 14B 微调",
        "state": "running",
        "progress": 0.37,
        "lines": ["epoch 12/32", "loss 1.842 → 1.367", "预计剩余 18 分钟"],
        "updated": time.time(),
    },
    {
        "sid": "zc-9f3c",
        "task": "重构订单模块",
        "state": "running",
        "progress": 0.72,
        "lines": ["改完 Service 层", "单测 12 / 15 通过"],
        "updated": time.time(),
    },
]
QTimer.singleShot(500, lambda: screen.update_status(SAMPLES))


def report():
    w = screen.windows[0] if screen.windows else None
    ok = False
    if w:
        cards = [c for c in w.stack._cards if not c.isHidden()]
        print("after status push:")
        print("  stack visible =", not w.stack.isHidden())
        print("  cards         =", len(cards))
        for c in cards:
            print("    - %s | %s | x=%d y=%d" % (
                c._title_raw, c.state.text(), c.x(), c.y()))
        print("  sbTask   =", repr(w.sbTask.text()))
        ok = (not w.stack.isHidden()) and len(cards) == len(SAMPLES)
    print("unlock attempts during smoke:", attempts)
    print("=> %s" % ("Qt 锁屏 OK" if ok else "FAIL"))
    sys.stdout.flush()


def finish():
    # 先检查再销毁：destroy() 会清空 windows 列表
    report()
    screen.destroy()
    app.quit()


QTimer.singleShot(int(SECONDS * 1000), finish)
app.exec()
sys.stdout.flush()
os._exit(0)
