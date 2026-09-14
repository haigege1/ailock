# -*- coding: utf-8 -*-
"""Qt 锁屏锁定/解锁全闭环冒烟测试。

覆盖（全部走真实信号路径 LockWindow._submit → unlockRequested → try_unlock）：
  1. 锁定铺屏 + 窗口可见性
  2. 错误密码 → 错误提示可见、窗口不销毁、错误计数递减
  3. 连续错误进入冷却 → 冷却提示
  4. 正确密码 → 解锁成功、窗口销毁
  5. 最短锁定时长内提交 → 被拦截
  6. 恢复码解锁 → 成功
  7. pending 动作横幅显示/隐藏

try_unlock 回调按 ui/qt/app.py 的实现逐行复刻（同步版），
使用真实 Vault / RuntimeState / Throttle，临时目录隔离。
"""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _watchdog():
    import time
    time.sleep(45)
    print("SMOKE TIMEOUT", flush=True)
    os._exit(3)


threading.Thread(target=_watchdog, daemon=True).start()

from pathlib import Path  # noqa: E402

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.config import Config, RuntimeState  # noqa: E402
from core.security import Throttle, Vault  # noqa: E402
from core.logger import log as _log  # noqa: E402
from ui.qt.theme import mmss  # noqa: E402

# 日志收集到内存，不污染真实日志
_events = []
_log.log = lambda tag, msg="": _events.append((tag, msg))

app = QApplication(sys.argv)

TMP = Path(tempfile.mkdtemp(prefix="ailock_unlock_"))
cfg = Config(TMP / "config.json")
cfg.set("background.mode", "color", autosave=False)
cfg.set("background.color", "#0B1220", autosave=False)
cfg.set("ui.animations", True, autosave=False)

vault = Vault(TMP / "vault.bin")
vault.set_password("test1234")
RECOVERY = vault.ensure_recovery()   # 拿到恢复码（只显示一次）

from ui.qt.lock_window import LockScreen  # noqa: E402


class MiniApp:
    """复刻 ui/qt/app.py 的 try_unlock（同步版，去掉了 QTimer 延迟）。"""

    def __init__(self):
        self.state = RuntimeState(TMP / "state.json")
        self.throttle = Throttle(self.state, max_attempts=2, cooldown_seconds=5)
        self.lockscreen = None
        self.locked = False
        self.unlock_reason = ""

    def try_unlock(self, password: str):
        if self.lockscreen:
            min_seconds = float(cfg.get("lock.min_lock_minutes", 0) or 0) * 60
            if min_seconds > 0:
                left = min_seconds - self.lockscreen.lock_elapsed()
                if left > 0:
                    return False, f"任务进行中，最早可在 {mmss(left)} 后解锁"
        if vault.has_recovery() and vault.verify_recovery(password):
            self.throttle.record_success()
            _log.log("unlock.recovery", "使用恢复码解锁")
            self.unlock("恢复码解锁")
            return True, ""
        if self.throttle.in_cooldown():
            left = int(self.throttle.remaining_seconds()) + 1
            return False, f"错误次数过多，请等待 {left} 秒"
        if vault.verify(password):
            self.throttle.record_success()
            _log.log("unlock.ok", "密码正确")
            self.unlock("密码解锁")
            return True, ""
        self.throttle.record_failure()
        _log.log("unlock.fail", "密码错误")
        if self.throttle.in_cooldown():
            return False, (f"密码错误次数过多，已进入 "
                           f"{self.throttle.cooldown_seconds} 秒冷却")
        return False, f"密码错误，还可尝试 {self.throttle.attempts_left()} 次"

    def unlock(self, reason: str = ""):
        if not self.locked:
            return
        self.locked = False
        self.unlock_reason = reason
        if self.lockscreen:
            self.lockscreen.destroy()
            self.lockscreen = None


ctrl = MiniApp()
checks = []


def check(name, ok):
    checks.append((name, bool(ok)))
    print(f"  [{'OK' if ok else 'FAIL'}] {name}", flush=True)


def submit(pwd: str):
    """走真实信号路径：填入密码框 → _submit → unlockRequested。"""
    w = ctrl.lockscreen.windows[0]
    w.pw.setText(pwd)
    w._submit()


# ---------------- 阶段 1：错误密码 + 冷却 ----------------
def stage1():
    ctrl.locked = True
    ctrl.lockscreen = LockScreen(None, cfg, ctrl.try_unlock)
    ctrl.lockscreen.show(0)
    check("铺屏窗口数 >= 1", len(ctrl.lockscreen.windows) >= 1)
    check("窗口可见", all(w.isVisible() for w in ctrl.lockscreen.windows))
    QTimer.singleShot(700, s1_wrong)


def s1_wrong():
    submit("wrong-password")
    QTimer.singleShot(400, s1_check_wrong)


def s1_check_wrong():
    w = ctrl.lockscreen.windows[0]
    check("错误提示可见", w.err.isVisible())
    check("错误文本含剩余次数", "还可尝试" in w.err.text())
    check("窗口未销毁", len(ctrl.lockscreen.windows) >= 1)
    submit("wrong-again")
    QTimer.singleShot(400, s1_check_cooldown)


def s1_check_cooldown():
    w = ctrl.lockscreen.windows[0]
    check("进入冷却提示", "冷却" in w.err.text() or "错误次数过多" in w.err.text())
    # 冷却中提交正确密码也应被拒
    submit("test1234")
    QTimer.singleShot(400, s2_relock)


def s2_relock():
    w = ctrl.lockscreen.windows[0]
    check("冷却期内正确密码也被拒", w.err.isVisible() and "等待" in w.err.text())
    check("仍处锁定", ctrl.locked)
    ctrl.unlock("阶段结束")
    ctrl.throttle.record_success()   # 阶段解耦：清掉冷却，别影响后续阶段
    # ---------------- 阶段 2：最短锁定 ----------------
    cfg.set("lock.min_lock_minutes", 30, autosave=False)
    ctrl.locked = True
    ctrl.lockscreen = LockScreen(None, cfg, ctrl.try_unlock)
    ctrl.lockscreen.show(30)
    QTimer.singleShot(700, s2_try_early)


def s2_try_early():
    submit("test1234")
    QTimer.singleShot(400, s2_check_early)


def s2_check_early():
    w = ctrl.lockscreen.windows[0]
    check("最短锁定拦截提示", "最早可在" in w.err.text())
    check("窗口仍在（未解锁）", len(ctrl.lockscreen.windows) >= 1)
    ctrl.unlock("阶段结束")
    ctrl.throttle.record_success()   # 阶段解耦
    # ---------------- 阶段 3：正确密码 + 恢复码 + pending ----------------
    cfg.set("lock.min_lock_minutes", 0, autosave=False)
    ctrl.locked = True
    ctrl.lockscreen = LockScreen(None, cfg, ctrl.try_unlock)
    ctrl.lockscreen.show(0)

    class FakePA:
        reason = "任务完成 · 即将执行关机"

        def remaining(self):
            return 42

    ctrl.lockscreen.show_pending_action(FakePA())
    w = ctrl.lockscreen.windows[0]
    check("pending 横幅可见", w.pending.isVisible())
    check("倒计时文本 = 42 秒", "42" in w.paCount.text())
    QTimer.singleShot(700, s3_correct)


def s3_correct():
    submit("test1234")
    QTimer.singleShot(500, s3_check_ok)


def s3_check_ok():
    check("正确密码解锁成功", not ctrl.locked)
    check("解锁原因 = 密码解锁", ctrl.unlock_reason == "密码解锁")
    check("窗口已销毁", ctrl.lockscreen is None or len(ctrl.lockscreen.windows) == 0)
    # ---------------- 阶段 4：恢复码解锁 ----------------
    ctrl.locked = True
    ctrl.lockscreen = LockScreen(None, cfg, ctrl.try_unlock)
    ctrl.lockscreen.show(0)
    QTimer.singleShot(700, s4_recovery)


def s4_recovery():
    submit(RECOVERY)
    QTimer.singleShot(500, finish)


def finish():
    check("恢复码解锁成功", not ctrl.locked)
    check("解锁原因 = 恢复码解锁", ctrl.unlock_reason == "恢复码解锁")
    if ctrl.lockscreen:
        ctrl.lockscreen.destroy()
    tags = [t for t, _m in _events]
    check("日志记录了解锁事件", "unlock.ok" in tags or "unlock.fail" in tags)
    fails = [n for n, ok in checks if not ok]
    if fails:
        print(f"=> Qt 解锁闭环 FAIL: {fails}", flush=True)
        os._exit(1)
    print(f"=> Qt 解锁闭环 OK（{len(checks)} 项检查全部通过）", flush=True)
    os._exit(0)


QTimer.singleShot(400, stage1)
app.exec()
