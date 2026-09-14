"""任务完成后的动作执行器（关机 / 休眠 / 睡眠 / 解锁 / 提示）。

安全设计 —— 关机是不可逆操作，所以任何破坏性动作都必须先走一段
**可取消的倒计时**：锁屏上显示大字「任务完成 · 88 秒后关机」，期间输入密码
即取消。只有倒计时自然走完才真正执行。这样即便 AI 工具误报 done，
也不会把机器直接关掉。
"""

import subprocess
import threading
import time

from .logger import log

ACTION_NONE = "none"
ACTION_SHUTDOWN = "shutdown"
ACTION_HIBERNATE = "hibernate"
ACTION_SLEEP = "sleep"
ACTION_UNLOCK = "unlock"
ACTION_NOTIFY = "notify"

ACTION_LABELS = {
    ACTION_NONE: "不执行任何动作",
    ACTION_SHUTDOWN: "自动关机",
    ACTION_HIBERNATE: "自动休眠",
    ACTION_SLEEP: "自动睡眠",
    ACTION_UNLOCK: "自动解锁",
    ACTION_NOTIFY: "仅提示（锁屏上显示完成横幅 + 提示音）",
}

DESTRUCTIVE = (ACTION_SHUTDOWN, ACTION_HIBERNATE, ACTION_SLEEP)


def human_action(action: str) -> str:
    return ACTION_LABELS.get(action, action)


def need_countdown(action: str) -> bool:
    return action in DESTRUCTIVE


def beep(kind: str = "ok") -> None:
    def _run():
        try:
            import winsound
            if kind == "ok":
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            else:
                winsound.MessageBeep(winsound.MB_ICONHAND)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def _run_hidden(cmd: list) -> tuple:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=30,
                           creationflags=flags)
        return p.returncode, (p.stderr or b"").decode("gbk", "ignore")
    except Exception as exc:
        return -1, repr(exc)


def execute(action: str, reason: str = "") -> tuple:
    """真正执行动作。返回 (成功与否, 说明文本)。

    注意：这里不记 action.execute 日志 —— 统一由上层 _execute_action 记，
    否则每执行一次会出现两条日志，排查问题时反而更难读。
    """
    if action == ACTION_NONE:
        return True, "未配置动作"
    if action == ACTION_UNLOCK:
        return True, "已解锁"
    if action == ACTION_NOTIFY:
        beep("ok")
        return True, "已提示"

    if action == ACTION_SHUTDOWN:
        # 不加 /f：让系统有机会提示未保存的工作，失败也比强杀用户进程好
        code, err = _run_hidden(["shutdown", "/s", "/t", "0",
                                 "/c", f"AiLock: {reason or '任务完成，自动关机'}"])
        return code == 0, err or "关机命令已发出"

    if action == ACTION_HIBERNATE:
        code, err = _run_hidden(["shutdown", "/h"])
        if code != 0:
            return False, (err or "休眠失败，可能未启用休眠功能"
                                  "（可用 powercfg /hibernate on 开启）")
        return True, "已进入休眠"

    if action == ACTION_SLEEP:
        code, err = _run_hidden(
            ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
        return code == 0, err or "已进入睡眠"

    return False, f"未知动作: {action}"


class PendingAction:
    """一个正在倒计时的待执行动作。"""

    def __init__(self, action: str, countdown: float, reason: str = ""):
        self.action = action
        self.reason = reason
        self.countdown = max(0.0, float(countdown))
        self.created = time.time()
        self.deadline = self.created + self.countdown
        self.cancelled = False

    @property
    def total(self) -> float:
        return self.countdown

    def remaining(self) -> float:
        if self.cancelled:
            return 0.0
        return max(0.0, self.deadline - time.time())

    def expired(self) -> bool:
        return (not self.cancelled) and time.time() >= self.deadline


class ActionRunner:
    """管理待执行动作的倒计时与取消。

    用法：
        runner.schedule("shutdown", 90, "模型微调 v3 已完成")
        ... 每帧 runner.tick(on_fire=lambda pa: ...) ...
        runner.cancel()          # 用户输密码取消
    """

    def __init__(self, on_change=None):
        self._lock = threading.RLock()
        self._pending: PendingAction | None = None
        self.on_change = on_change
        self.last_result = ""

    @property
    def pending(self) -> PendingAction | None:
        with self._lock:
            if self._pending and self._pending.cancelled:
                return None
            return self._pending

    def schedule(self, action: str, countdown: float, reason: str = "") -> PendingAction:
        with self._lock:
            if action == ACTION_NONE:
                self._pending = None
                return None
            pa = PendingAction(action, countdown, reason)
            self._pending = pa
            log.log("action.schedule",
                    f"{action} 倒计时 {countdown:.0f}s | {reason}")
            if self.on_change:
                self.on_change(pa)
            return pa

    def cancel(self, reason: str = "用户取消") -> bool:
        with self._lock:
            if self._pending and not self._pending.cancelled:
                self._pending.cancelled = True
                log.log("action.cancel", f"{self._pending.action} | {reason}")
                self._pending = None
                if self.on_change:
                    self.on_change(None)
                return True
            return False

    def tick(self, on_fire) -> None:
        """在主循环里周期性调用；倒计时归零时触发 on_fire(pending_action)。"""
        with self._lock:
            pa = self._pending
            if pa is None or pa.cancelled:
                return
            if not pa.expired():
                return
            self._pending = None
        try:
            on_fire(pa)
        except Exception as exc:
            log.log("action.error", repr(exc))
        if self.on_change:
            self.on_change(None)
