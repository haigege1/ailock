"""AiLock Qt 版 · 应用主控

职责与旧 tkinter 版 App 一致：锁定/解锁、AI 任务状态联动、托盘、看门狗、
完成动作倒计时。差别是用 Qt 的信号槽替代 `root.after` 做线程切换 ——
后端监控线程只 emit 信号，所有 UI 操作都在主线程执行。

托盘与全局热键沿用 `ui/tray.py`（纯 win32 实现，与界面框架无关）。
"""

import os
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal

from core import actions as A
from core import autostart
from core.balloon_queue import BalloonQueue
from core.config import Config, LockRequest, RuntimeState
from core.idle import IdleWatcher
from core.input_hook import KeyboardBlocker
from core.logger import log
from core.net_watch import NetWatchdog
from core.power import PowerGuard, check_environment
from core.security import Throttle, Vault
from core.statusdir import MultiStatusMonitor, pick_primary
from core.watchdog import Watchdog
from ui.qt import theme as T
from ui.qt.lock_window import LockScreen
from ui.tray import TrayApp

APP_TITLE = "AiLock"
ICON_NAME = "ailock.ico"

# 锁屏期间攒下来的托盘提醒上限与解锁后摘要里最多列几条
BALLOON_QUEUE_MAX = 20
BALLOON_SUMMARY_MAX = 5
# 状态快照合并窗口：恢复瞬间会积压上千条，一秒内最多渲染这么多次
SNAPSHOT_THROTTLE_MS = 300


class QtApp(QObject):
    """应用主控。必须在 QApplication 实例化之后创建（内部会用 T.font）。"""

    # 跨线程信号：后端线程 emit，主线程槽执行
    statusEvent = Signal(str, object)
    statusSnapshot = Signal(object)
    pendingChanged = Signal(object)
    uiCommand = Signal(str)   # 托盘线程 -> 主线程的命令分发（lock/settings/quit）
    wallpapersFetched = Signal(int)   # 在线壁纸下载线程 -> 主线程
    netEvent = Signal(str)    # 网络看门狗线程 -> 主线程（down/still/recover）
    blackoutChanged = Signal(bool)    # PowerGuard 线程 -> 主线程（黑屏开关）

    def __init__(self, cfg: Config, start_locked: bool = False,
                 icon_path: str = ""):
        super().__init__()
        self.cfg = cfg
        self.vault = Vault()
        self.state = RuntimeState()
        self.throttle = Throttle(
            self.state,
            int(cfg.get("security.max_attempts", 5) or 5),
            int(cfg.get("security.cooldown_seconds", 30) or 30),
        )

        self.locked = False
        self.lockscreen: LockScreen | None = None
        self.hook = None
        self.power = None
        self.idle_watcher = None
        self.status_monitor = None
        self.tray: TrayApp | None = None
        self.watchdog = Watchdog()
        self.net_watch = None
        self.lock_request = LockRequest()
        self.runner = A.ActionRunner(on_change=self._on_pending_change)
        self._last_status = None
        self._settings = None
        self._stopping = False
        self._icon_path = icon_path
        # 锁屏期间攒下的托盘提醒 + 快照合并
        self._balloons = BalloonQueue(BALLOON_QUEUE_MAX, BALLOON_SUMMARY_MAX)
        self._snap_pending = None
        self._last_beat = 0.0

        self.statusEvent.connect(self._handle_status_event)
        self.statusSnapshot.connect(self._apply_status)
        self.pendingChanged.connect(self._apply_pending)
        self.uiCommand.connect(self._dispatch_command)
        self.netEvent.connect(self._net_event_ui)
        self.blackoutChanged.connect(self._on_blackout)

        self._start_tray()
        self._restart_status_monitor()
        self._start_idle_watcher()
        self.watchdog.start_child()
        self._start_net_watch()
        self._power_selfcheck()

        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.timeout.connect(self._beat)
        self._heartbeat_timer.start(2000)

        self._snapshot_timer = QTimer(self)
        self._snapshot_timer.setSingleShot(True)
        self._snapshot_timer.timeout.connect(self._flush_snapshot)
        self._snapshot_timer.setInterval(SNAPSHOT_THROTTLE_MS)

        self._ipc_timer = QTimer(self)
        self._ipc_timer.timeout.connect(self._poll_ipc)
        self._ipc_timer.start(500)

        self._action_timer = QTimer(self)
        self._action_timer.timeout.connect(self._action_tick)
        self._action_timer.start(500)

        if not self.vault.is_set:
            log.log("app.firstrun", "尚未设置密码，将自动打开设置")
            QTimer.singleShot(400, self.open_settings)
        elif start_locked:
            QTimer.singleShot(500, self.lock)
        else:
            QTimer.singleShot(700, self._greet)

    # ========================================================== 锁定 / 解锁
    def lock(self) -> None:
        if self.locked or self._stopping:
            return
        if not self.vault.is_set:
            self._balloon("请先设置密码", "还没有设置锁屏密码，已为你打开设置。")
            self.open_settings()
            return

        self.locked = True
        log.log("lock.start", "进入锁定")

        if self.idle_watcher:
            self.idle_watcher.stop()
            self.idle_watcher = None

        self.hook = KeyboardBlocker().start()
        self.power = PowerGuard(
            keep_display_on=bool(self.cfg.get("display.keep_on", True)),
            off_after_minutes=float(
                self.cfg.get("display.off_after_minutes", 0) or 0),
            on_blackout=self.blackoutChanged.emit,
        ).start()

        self._maybe_fetch_online()

        # 必须在 show 之前把当前会话快照传给 LockScreen：锁屏一显示就要看到
        # 会话卡，否则会先空一下再由 2s 轮询补上。会话卡是绝对定位覆盖层，
        # 增删不会挤动中央的时钟/密码卡（旧 taskCard 那套布局抖动已不存在）。
        self.lockscreen = LockScreen(None, self.cfg, self.try_unlock,
                                     initial_status=self._last_status)
        self.lockscreen.show(self.cfg.get("lock.min_lock_minutes", 0) or 0)

        self._update_tray()

    def unlock(self, reason: str = "") -> None:
        if not self.locked:
            return
        self.locked = False
        self.runner.cancel("解锁时一并取消待执行动作")
        log.log("lock.stop", reason or "手动解锁")

        if self.lockscreen:
            self.lockscreen.destroy()
            self.lockscreen = None
        if self.hook:
            self.hook.stop()
            self.hook = None
        if self.power:
            self.power.stop()
            self.power = None

        self._start_idle_watcher()
        self._update_tray()
        # 锁屏期间攒下的提醒，等全屏窗口彻底消失后再补弹（弹早了会被
        # 系统的「全屏时隐藏通知」策略继续压着，最后还是一起喷出来）
        QTimer.singleShot(1200, self._flush_balloons)

    def try_unlock(self, password: str):
        """由锁屏界面调用。返回 (是否成功, 提示文本)。"""
        if self.lockscreen:
            min_seconds = float(self.cfg.get("lock.min_lock_minutes", 0) or 0) * 60
            if min_seconds > 0:
                left = min_seconds - self.lockscreen.lock_elapsed()
                if left > 0:
                    return False, f"任务进行中，最早可在 {T.mmss(left)} 后解锁"

        if self.vault.has_recovery() and self.vault.verify_recovery(password):
            log.log("unlock.recovery", "使用恢复码解锁")
            self.throttle.record_success()
            QTimer.singleShot(0, lambda: self.unlock("恢复码解锁"))
            return True, ""

        if self.throttle.in_cooldown():
            left = int(self.throttle.remaining_seconds()) + 1
            return False, f"错误次数过多，请等待 {left} 秒"

        if self.vault.verify(password):
            self.throttle.record_success()
            log.log("unlock.ok", "密码正确")
            QTimer.singleShot(0, lambda: self.unlock("密码解锁"))
            return True, ""

        self.throttle.record_failure()
        log.log("unlock.fail", "密码错误")
        if self.throttle.in_cooldown():
            return False, (f"密码错误次数过多，已进入 "
                           f"{self.throttle.cooldown_seconds} 秒冷却")
        return False, f"密码错误，还可尝试 {self.throttle.attempts_left()} 次"

    # ========================================================== 在线壁纸
    def _maybe_fetch_online(self) -> None:
        """在线模式：锁屏时后台检查 Bing 壁纸，增量下载，完成后刷新列表。"""
        if str(self.cfg.get("background.mode", "")) != "online":
            return
        days = int(self.cfg.get("background.online_days", 15) or 15)
        keep = int(self.cfg.get("background.online_keep", 30) or 30)
        from core.wallpaper_fetch import fetch_async

        def _done(n: int):
            # 工作线程回调，经信号切回主线程
            self.wallpapersFetched.emit(int(n))

        fetch_async(days=days, keep=keep, on_done=_done)

    def _on_wallpapers_fetched(self, new_count: int) -> None:
        if new_count and self.lockscreen:
            self.lockscreen.refresh_files()

    # ========================================================== 状态联动
    def _restart_status_monitor(self) -> None:
        if self.status_monitor:
            self.status_monitor.stop()
            self.status_monitor = None
        if not self.cfg.get("status.enabled", True):
            return
        from core.config import default_status_path
        path = self.cfg.get("status.path") or default_status_path()
        self.status_monitor = MultiStatusMonitor(
            path=path,
            poll_seconds=float(self.cfg.get("status.poll_seconds", 2) or 2),
            stale_minutes=float(self.cfg.get("status.stale_minutes", 0) or 0),
            on_event=self._on_status_event,
            on_snapshot=self._on_status_snapshot,
        )
        self.status_monitor.start()

    def _on_status_event(self, kind, data):
        self.statusEvent.emit(kind, data)

    def _on_status_snapshot(self, data):
        self.statusSnapshot.emit(data)

    def _apply_status(self, sessions):
        """多会话快照：合并后推给锁屏。托盘只能显示一行，取「主会话」。

        合并的原因：系统掉进连接待机（Modern Standby）时进程会被整个挂起，
        恢复瞬间监控线程积压的上千条快照信号一次性投递，每条都触发全窗口
        重排 —— 表现就是解锁后 UI 假死、通知齐喷。这里只记最新值，真正的
        渲染交给节流定时器：一秒内最多刷 300ms 一次。
        """
        self._snap_pending = sessions
        if not self._snapshot_timer.isActive():
            self._snapshot_timer.start()

    def _flush_snapshot(self) -> None:
        sessions = self._snap_pending
        self._snap_pending = None
        self._last_status = sessions or []
        primary = pick_primary(sessions)
        task = str(primary.get("task") or "").strip() if primary else ""
        if task and self.tray:
            self.tray.update_tooltip(f"{APP_TITLE} · {task}")
        if self.lockscreen and self.locked:
            self.lockscreen.update_status(sessions)

    def _handle_status_event(self, kind, data) -> None:
        if kind == "done":
            self._trigger_action(self.cfg.get("status.on_done_action", "none"),
                                 data, self.cfg.get("status.on_done_countdown", 90))
        elif kind == "failed":
            self._trigger_action(self.cfg.get("status.on_failed_action", "none"),
                                 data, self.cfg.get("status.on_done_countdown", 90))
        elif kind == "stale":
            self._trigger_action(self.cfg.get("status.on_stale_action", "none"),
                                 data, self.cfg.get("status.on_done_countdown", 90))

    def _trigger_action(self, action, data, countdown) -> None:
        if action == A.ACTION_NONE:
            return
        reason = str(data.get("task") or "任务完成")

        if A.need_countdown(action):
            countdown = max(0.0, float(countdown or 0))
            if countdown <= 0:
                self._execute_action(action, reason)
                return
            pa = self.runner.schedule(action, countdown, reason)
            log.log("action.pending", f"{action} / {countdown:.0f}s / {reason}")
            if self.lockscreen and self.locked:
                self.lockscreen.show_pending_action(pa)
            self._balloon(f"任务完成 · {A.human_action(action)}",
                          f"{reason}\n{countdown:.0f} 秒后执行，输入密码可取消。")
        else:
            self._execute_action(action, reason)

    def _on_pending_change(self, pending) -> None:
        self.pendingChanged.emit(pending)

    def _apply_pending(self, pending) -> None:
        if self.lockscreen and self.locked:
            self.lockscreen.show_pending_action(pending)

    def _action_tick(self) -> None:
        self.runner.tick(self._fire_action)

    def _fire_action(self, pending) -> None:
        self._execute_action(pending.action, pending.reason)

    def _execute_action(self, action: str, reason: str) -> None:
        log.log("action.execute", f"{action} | {reason}")
        if action == A.ACTION_UNLOCK:
            self.unlock("任务完成，自动解锁")
            return
        if action == A.ACTION_NOTIFY:
            A.beep("ok")
            if self.lockscreen and self.locked:
                self.lockscreen.set_message(f"{reason} · 已完成",
                                            color="#34D399")
            else:
                self._balloon("任务完成", reason)
            return
        ok, message = A.execute(action, reason)
        log.log("action.result", f"{action} -> ok={ok} {message}")
        if not ok and self.lockscreen and self.locked:
            self.lockscreen.set_message(message)

    # ========================================================== 周期任务
    def _beat(self) -> None:
        now = time.time()
        if self._last_beat and now - self._last_beat > 15:
            # 心跳间隔 2 秒，断档超过 15 秒只可能是系统把进程挂起了
            # （连接待机 / 休眠）。留一条日志，事后一眼就能判断有没有再犯。
            log.log("power.freeze",
                    f"心跳断档 {now - self._last_beat:.0f} 秒，进程很可能被挂起"
                    f"（locked={self.locked}）")
        self._last_beat = now
        self.watchdog.beat(os.getpid(), self.locked)
        self.watchdog.check_child()

    def _poll_ipc(self) -> None:
        """第二个进程通过文件请求本实例锁定。"""
        if self.lock_request.consume() and not self.locked:
            log.log("ipc.lock", "收到外部锁定请求")
            self.lock()

    def _start_idle_watcher(self) -> None:
        minutes = float(self.cfg.get("lock.auto_lock_idle_minutes", 0) or 0)
        if minutes <= 0:
            return
        self.idle_watcher = IdleWatcher(
            minutes * 60, lambda: self.uiCommand.emit("lock")).start()

    # ========================================================== 电源自检 / 网络看门狗
    def _power_selfcheck(self) -> None:
        """启动时把电源环境写日志；睡眠超时过短给一个托盘提醒。"""
        try:
            warns = check_environment()
        except Exception as exc:
            log.log("power.error", f"自检失败: {exc!r}")
            return
        if warns:
            self._balloon("AiLock 电源提示", "\n".join(warns[:2]))

    def _start_net_watch(self) -> None:
        if not self.cfg.get("net.watch", True):
            return
        self.net_watch = NetWatchdog(
            interval=float(self.cfg.get("net.interval_seconds", 30) or 30),
            fail_threshold=int(self.cfg.get("net.fail_threshold", 3) or 3),
            host=str(self.cfg.get("net.host", "") or ""),
            on_event=self._on_net_event,
        ).start()

    def _on_net_event(self, event: str) -> None:
        """看门狗线程回调 —— 只负责 emit，UI 动作回主线程做。"""
        self.netEvent.emit(event)

    def _net_event_ui(self, event: str) -> None:
        if event == "down":
            self._balloon("网络异常",
                          "连续多次探测失败，主机可能已断网，请检查网络。")
        elif event == "still":
            self._balloon("网络仍不可用", "断网已持续超过 10 分钟。")
        elif event == "recover":
            self._balloon("网络已恢复", "网络探测恢复正常。")

    def _on_blackout(self, on: bool) -> None:
        """黑屏模式开关（主线程）：Modern Standby 机器用涂黑代替关屏。"""
        if self.lockscreen:
            self.lockscreen.set_blackout(bool(on))

    # ========================================================== 托盘
    def _start_tray(self) -> None:
        self.tray = TrayApp(
            icon_path=self._icon_path,
            tooltip=APP_TITLE,
            menu_items=[
                ("立即锁定", lambda: self.uiCommand.emit("lock")),
                ("设置...", lambda: self.uiCommand.emit("settings")),
                ("打开日志", self._open_log),
                ("打开配置目录", self._open_config_dir),
                ("-", None),
                ("退出", lambda: self.uiCommand.emit("quit")),
            ],
            hotkey=self.cfg.get("hotkey", "ctrl+alt+l"),
            on_hotkey=lambda: self.uiCommand.emit("lock"),
        )
        self.tray.start()
        self.tray.wait_ready(5)
        if self.tray._hotkey_error:
            self._balloon("热键未生效", self.tray._hotkey_error)

    def _greet(self) -> None:
        self._balloon("AiLock 已启动",
                      "已在托盘常驻 · Ctrl+Alt+L 锁屏 · 右键托盘图标可打开设置")

    def _dispatch_command(self, cmd: str) -> None:
        """托盘线程经 uiCommand 信号切回主线程后，在此执行实际动作。

        托盘菜单/热键回调跑在 TrayApp 的 Win32 消息循环线程里，那里没有
        Qt 事件循环，QTimer.singleShot 投递的事件永远不会被处理（症状就是
        「点了设置/锁定毫无反应」）。所以回调只负责 emit 本信号，
        由 Qt 的队列连接把执行安全地切回主线程。
        """
        if cmd == "lock":
            self.lock()
        elif cmd == "settings":
            self.open_settings()
        elif cmd == "quit":
            self.quit()

    def _update_tray(self) -> None:
        if self.tray:
            self.tray.update_tooltip(
                f"{APP_TITLE} · 已锁定" if self.locked else APP_TITLE)

    def _balloon(self, title: str, message: str, to_lockscreen: bool = False) -> None:
        """托盘气泡。锁屏期间自动改道，避免「攒一堆再一起弹」。

        - to_lockscreen=True：这是用户刚点的操作反馈，直接写在锁屏上，
          别让用户点了没反应；不入队，解锁后也不会再吵一次。
        - 锁屏期间：全屏窗口盖着，气泡看不见，还会被系统的「全屏时隐藏
          通知」策略排队，等解锁时一次性放出来 —— 正是「通知一起弹出」
          的来源。改为入队，解锁后合并成一条摘要。
        """
        if self.tray is None:
            return
        if self.locked:
            if to_lockscreen and self.lockscreen:
                self.lockscreen.set_message(f"{title} · {message}")
                return
            self._balloons.add(title, message)
            return
        self.tray.balloon(title, message)

    def _flush_balloons(self) -> None:
        """解锁后补弹：单条原样，多条合并成一条摘要。"""
        if self.locked or not len(self._balloons) or self.tray is None:
            return
        self.tray.balloon(*self._balloons.take())

    def _open_log(self) -> None:
        p = log.path
        if p.exists():
            os.startfile(str(p))

    def _open_config_dir(self) -> None:
        from core.config import app_dir
        os.startfile(str(app_dir()))

    # ========================================================== 设置
    def open_settings(self) -> None:
        log.log("settings.open", f"locked={self.locked} has={self._settings is not None}")
        if self.locked:
            self._balloon("锁定中", "请先解锁再打开设置。", to_lockscreen=True)
            return
        if self._settings is not None:
            try:
                self._settings.show()
                self._settings.raise_()
                self._settings.activateWindow()
            except Exception:
                pass
            return
        try:
            from ui.qt.settings_window import SettingsWindow
            win = SettingsWindow(
                self.cfg, self.vault,
                on_saved=self._on_settings_saved,
                on_lock_now=self.lock,
            )
            win.closed.connect(self._on_settings_closed)
            self._settings = win
            win.show()
        except Exception:
            import traceback
            log.log("settings.error", traceback.format_exc(limit=6))
            self._settings = None

    def _on_settings_closed(self) -> None:
        self._settings = None

    def _on_settings_saved(self, cfg) -> None:
        self.throttle = Throttle(
            self.state,
            int(cfg.get("security.max_attempts", 5) or 5),
            int(cfg.get("security.cooldown_seconds", 30) or 30),
        )
        if self.tray:
            self.tray.reregister_hotkey(cfg.get("hotkey", "ctrl+alt+l"))
        self._restart_status_monitor()
        if not self.locked:
            if self.idle_watcher:
                self.idle_watcher.stop()
                self.idle_watcher = None
            self._start_idle_watcher()
        log.log("settings.saved", "配置已更新")

    # ========================================================== 退出
    def quit(self) -> None:
        if self.locked:
            self._balloon("锁定中无法退出", "请先解锁。忘记密码可使用恢复码。",
                          to_lockscreen=True)
            return
        self.shutdown()

    def shutdown(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        log.log("app.quit", "正常退出")
        self.runner.cancel("程序退出")
        self.unlock("程序退出")
        for t in (self._heartbeat_timer, self._ipc_timer, self._action_timer):
            try:
                t.stop()
            except Exception:
                pass
        if self.status_monitor:
            self.status_monitor.stop()
        if self.idle_watcher:
            self.idle_watcher.stop()
        if self.net_watch:
            self.net_watch.stop()
        self.watchdog.stop()
        if self.tray:
            self.tray.stop()
        from PySide6.QtWidgets import QApplication
        QApplication.instance().quit()
