# -*- coding: utf-8 -*-
"""验证托盘线程 -> 主线程命令分发的跨线程信号修复。

复现原 bug：托盘菜单回调（跑在 TrayApp 的 Win32 消息循环线程）里，
若用 QTimer.singleShot(0, slot)，slot 永远不执行（该线程没有 Qt 事件循环）。
修复：回调改为 emit uiCommand 信号，Qt 队列连接切回主线程。

本测试直接构造 QtApp，从「模拟托盘线程」里触发菜单回调，
断言主线程槽确实被调用。
"""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _watchdog():
    import time
    time.sleep(30)
    print("SMOKE TIMEOUT", flush=True)
    os._exit(3)


threading.Thread(target=_watchdog, daemon=True).start()

from pathlib import Path  # noqa: E402

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.config import Config  # noqa: E402
from core.logger import log as _log  # noqa: E402

# 日志收集到内存
_events = []
_log.log = lambda tag, msg="": _events.append((tag, msg))

app = QApplication(sys.argv)

TMP = Path(tempfile.mkdtemp(prefix="ailock_cmd_"))
cfg = Config(TMP / "config.json")

from ui.qt.app import QtApp  # noqa: E402

# 直接构造（内部 Vault 读真实配置目录，已设密码 → 不会触发 firstrun 自动开设置）
ctrl = QtApp(cfg, icon_path="")

called = {"lock": 0, "settings": 0, "quit": 0}


# 打桩：拦截 open_settings / lock / quit 观察是否被主线程调用
def fake_open_settings():
    called["settings"] += 1
    print("    [debug] fake_open_settings called, count=", called["settings"], flush=True)


def fake_lock():
    called["lock"] += 1
    print("    [debug] fake_lock called, count=", called["lock"], flush=True)


ctrl.open_settings = fake_open_settings
ctrl.lock = fake_lock

# 用托盘菜单回调的原始 lambda 逻辑：从「非主线程」emit uiCommand
def from_tray_thread(cmd):
    # 直接模拟托盘线程里 callback 的调用方式
    ctrl.uiCommand.emit(cmd)


def step0():
    # firstrun 已自动调度 open_settings（is_set=False），等它先执行完
    QTimer.singleShot(600, step_pre)


def step_pre():
    # 清零：忽略 firstrun 那次自动调用，从此刻开始计数
    called["settings"] = 0
    called["lock"] = 0
    # 从主线程 emit（对照：应同步/队列触发）
    ctrl.uiCommand.emit("settings")
    QTimer.singleShot(300, step1)


def step1():
    check("主线程 emit settings 生效", called["settings"] == 1)
    # 从后台线程 emit（关键：模拟托盘线程）
    t = threading.Thread(target=from_tray_thread, args=("lock",), daemon=True)
    t.start()
    QTimer.singleShot(600, step2)


def step2():
    check("后台线程 emit lock 生效（跨线程信号）", called["lock"] == 1)
    # 再测 settings 从后台线程
    t = threading.Thread(target=from_tray_thread, args=("settings",), daemon=True)
    t.start()
    QTimer.singleShot(600, step3)


def step3():
    check("后台线程 emit settings 生效", called["settings"] == 2)
    # 清理：shutdown 会启动一堆 stop，直接销毁
    ctrl.shutdown()
    QTimer.singleShot(300, finish)


def finish():
    fails = [n for n, ok in checks if not ok]
    if fails:
        print(f"=> 跨线程命令分发 FAIL: {fails}", flush=True)
        os._exit(1)
    print(f"=> 跨线程命令分发 OK（{len(checks)} 项检查全部通过）", flush=True)
    os._exit(0)


checks = []


def check(name, ok):
    checks.append((name, bool(ok)))
    print(f"  [{'OK' if ok else 'FAIL'}] {name}", flush=True)


QTimer.singleShot(200, step0)
app.exec()
