# -*- coding: utf-8 -*-
"""SettingsWindow 冒烟测试。

验证点：
  1. 窗口能创建并显示（4 个导航页）
  2. 页面切换正常（标题联动）
  3. _apply 配置写入 → on_saved 回调触发 → JSON 落盘
  4. close() → closed 信号发射且幂等
全部使用临时目录，不污染真实 config/vault。
"""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 硬超时兜底：防止任何死锁挂住 CI ----
def _watchdog():
    import time
    time.sleep(30)
    print("SMOKE TIMEOUT", flush=True)
    os._exit(3)

threading.daemon = True
threading.Thread(target=_watchdog, daemon=True).start()

from pathlib import Path  # noqa: E402

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.config import Config  # noqa: E402
from core.security import Vault  # noqa: E402

app = QApplication(sys.argv)

tmp = Path(tempfile.mkdtemp(prefix="ailock_smoke_"))
cfg = Config(tmp / "config.json")
vault = Vault(tmp / "vault.bin")

events = {"saved": 0, "closed": 0, "lock_now": 0}

# ---- 创建窗口 ----
from ui.qt.settings_window import SettingsWindow  # noqa: E402

win = SettingsWindow(
    cfg, vault,
    on_saved=lambda c: events.__setitem__("saved", events["saved"] + 1),
    on_lock_now=lambda: events.__setitem__("lock_now", events["lock_now"] + 1),
)
win.closed.connect(lambda: events.__setitem__("closed", events["closed"] + 1))
win.show()

checks = []


def check(name, ok):
    checks.append((name, bool(ok)))
    print(f"  [{'OK' if ok else 'FAIL'}] {name}", flush=True)


def step0():
    check("窗口可见", win.isVisible())
    check("导航页数量 = 4", len(win._navBtns) == 4)
    check("初始页为常规", win.pageTitle.text() == "常规")
    win._switch_page(1)
    QTimer.singleShot(300, step1)


def step1():
    check("切到安全页", win.pageTitle.text() == "安全")
    check("恢复码标签已初始化", win.recTip is not None)
    win._switch_page(2)
    QTimer.singleShot(300, step2)


def step2():
    check("切到壁纸页", win.pageTitle.text() == "壁纸")
    check("壁纸分段控件存在", win.bgSeg is not None)
    win._switch_page(3)
    QTimer.singleShot(300, step3)


def step3():
    check("切到 AI 联动页", win.pageTitle.text() == "AI 联动")
    # 模拟一个配置写入：验证 _apply → cfg.set → on_saved 链路
    before = events["saved"]
    win._apply("message.text", "冒烟测试写入")
    QTimer.singleShot(400, step4)


def step4():
    check("on_saved 回调已触发", events["saved"] >= 1)
    check("配置内存值生效", cfg.get("message.text") == "冒烟测试写入")
    raw = (tmp / "config.json").read_text(encoding="utf-8")
    check("配置已落盘 JSON", "冒烟测试写入" in raw)
    win.close()
    win.close()  # 第二次 close 验证幂等
    QTimer.singleShot(300, finish)


def finish():
    check("closed 信号恰好发射一次", events["closed"] == 1)
    fails = [n for n, ok in checks if not ok]
    if fails:
        print(f"=> Qt 设置面板 FAIL: {fails}", flush=True)
        os._exit(1)
    print(f"=> Qt 设置面板 OK（{len(checks)} 项检查全部通过）", flush=True)
    os._exit(0)


QTimer.singleShot(600, step0)
app.exec()
