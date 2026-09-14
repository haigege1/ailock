"""AiLock —— 为「AI 在跑、人不在」场景设计的 Windows 轻量锁屏。

命令行：
    无参数              后台常驻（托盘）
    --lock              立即锁定（已有实例在跑则转发给它）
    --settings          打开设置面板
    --notify ...        状态上报模式，等价于 notify.py
    --run ...           包装模式：跑一条命令并自动上报 running/done/failed
    --selftest          自检：铺满所有屏幕 6 秒后自动退出
    --watchdog-child    内部使用，看门狗子进程入口

--run 是要跟 AI 工具联动时最省事的入口：

    AiLock.exe --run -t "模型微调" -- python train.py --epochs 50
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# DPI 感知必须在创建任何窗口之前设置，否则多屏缩放下会漏边或错位
from core.winapi import set_dpi_awareness  # noqa: E402

set_dpi_awareness()

# 中文输出兜底：frozen 状态下 stdout/stderr 默认 cp936，打印中文会乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import os  # noqa: E402

# UI 层三选一（默认 qt）：
#   AILOCK_UI=qt    PySide6 原生界面 —— 不依赖 WebView2 / 任何浏览器运行时
#   AILOCK_UI=web   Web UI（pywebview + WebView2），还原度 100% 但依赖运行时版本
#   AILOCK_UI=tk    旧 tkinter 界面（回退 / 调试）
_UI_MODE = (os.environ.get("AILOCK_UI", "qt") or "qt").lower()
USE_QT = _UI_MODE == "qt"
USE_WEB = _UI_MODE == "web"

from core import actions as A  # noqa: E402
from core import autostart  # noqa: E402
from core.config import Config, LockRequest, RuntimeState  # noqa: E402
from core.idle import IdleWatcher  # noqa: E402
from core.input_hook import KeyboardBlocker  # noqa: E402
from core.logger import log  # noqa: E402
from core.net_watch import NetWatchdog  # noqa: E402
from core.power import PowerGuard, check_environment  # noqa: E402
from core.security import Throttle, Vault  # noqa: E402
from core.statusdir import MultiStatusMonitor, pick_primary  # noqa: E402
from core.watchdog import Watchdog, watchdog_child_main  # noqa: E402
from ui.tray import TrayApp  # noqa: E402

if USE_QT:
    # Qt 模式：锁屏 / 设置 / 主控都在 ui/qt 下，延迟到 main() 里导入，
    # 避免模块加载阶段就触发 GUI 相关初始化，也不拖入 tkinter。
    LockScreen = None
    SettingsPanel = None
    WebUI = None
    AppRoot = None
    from ui.qt import theme as _qt_theme

    def _mmss(seconds):          # noqa: F811 - Qt 版用自己的实现
        return _qt_theme.mmss(seconds)
elif USE_WEB:
    from ui.lock_window import _mmss  # noqa: E402
    from ui.webui import WebLock, WebSettings, WebUI, AppRoot  # noqa: E402
    LockScreen = WebLock          # 别名，供 selftest 等沿用
    SettingsPanel = WebSettings
else:
    import tkinter as tk  # noqa: E402
    from ui.lock_window import LockScreen, _mmss  # noqa: E402
    from ui.settings_panel import SettingsPanel  # noqa: E402
    WebUI = None
    AppRoot = None

APP_TITLE = "AiLock"
MUTEX_NAME = "Global\\AiLock_SingleInstance_2026"
ICON_NAME = "ailock.ico"

_MUTEX_HANDLE = None


def assets_dir() -> str:
    """返回 assets 目录的绝对路径。

    源码运行：脚本所在目录下的 assets/
    PyInstaller 冻结：解压到 _MEIPASS 临时目录的 assets/
    """
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "assets")


def icon_path() -> str:
    p = os.path.join(assets_dir(), ICON_NAME)
    return p if os.path.isfile(p) else ""


def try_acquire_single_instance() -> bool:
    """返回 True 表示本进程是第一个实例。"""
    global _MUTEX_HANDLE
    try:
        import win32api
        import win32event
        import winerror
        _MUTEX_HANDLE = win32event.CreateMutex(None, False, MUTEX_NAME)
        return win32api.GetLastError() != winerror.ERROR_ALREADY_EXISTS
    except Exception as exc:
        log.log("single.warn", f"无法创建互斥量: {exc!r}")
        return True


class App:
    def __init__(self, root: "tk.Tk", cfg: Config, start_locked: bool = False):
        self.root = root
        self.cfg = cfg
        self.vault = Vault()
        self.state = RuntimeState()
        self.throttle = Throttle(
            self.state,
            int(self.cfg.get("security.max_attempts", 5) or 5),
            int(self.cfg.get("security.cooldown_seconds", 30) or 30),
        )

        self.locked = False
        self.lockscreen: LockScreen | None = None
        self.hook: KeyboardBlocker | None = None
        self.power: PowerGuard | None = None
        self.net_watch: NetWatchdog | None = None
        self.idle_watcher: IdleWatcher | None = None
        self.status_monitor: MultiStatusMonitor | None = None
        self.tray: TrayApp | None = None
        self.watchdog = Watchdog()
        self.lock_request = LockRequest()
        self.runner = A.ActionRunner(on_change=self._on_pending_change)
        self._last_status = None
        self._settings_open = False
        self._panel = None
        self._stopping = False

        if USE_WEB:
            _engine = WebUI.get()
            _engine.attach(root, cfg, self.vault, self)

        root.withdraw()
        root.title(APP_TITLE)

        self._start_tray()
        self._restart_status_monitor()
        self._start_idle_watcher()
        self.watchdog.start_child()

        self._schedule_heartbeat()
        self._schedule_ipc()
        self._schedule_action_tick()

        if not self.vault.is_set:
            log.log("app.firstrun", "尚未设置密码，将自动打开设置")
            root.after(400, self.open_settings)
        elif start_locked:
            root.after(500, self.lock)

        # 启动反馈：托盘程序没有窗口，必须给用户一个「我已经在运行」的信号
        if not start_locked:
            root.after(600, lambda: self._balloon(
                "AiLock 已启动",
                "已在托盘常驻 · Ctrl+Alt+L 锁屏 · 右键托盘图标可打开设置"))

    # ==========================================================
    # 锁定 / 解锁
    # ==========================================================
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
            keep_display_on=bool(self.cfg.get("display.keep_on", True))).start()

        self.lockscreen = WebLock(self.root, self.cfg, self.try_unlock)
        self.lockscreen.show(self.cfg.get("lock.min_lock_minutes", 0) or 0)
        if self._last_status:
            self.lockscreen.update_status(self._last_status)

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

    def try_unlock(self, password: str):
        """由锁屏界面调用。返回 (是否成功, 提示文本)。"""
        # 1) 最短锁定时长
        if self.lockscreen:
            min_seconds = float(self.cfg.get("lock.min_lock_minutes", 0) or 0) * 60
            if min_seconds > 0:
                left = min_seconds - self.lockscreen.lock_elapsed()
                if left > 0:
                    return False, f"任务进行中，最早可在 {_mmss(left)} 后解锁"

        # 2) 恢复码（紧急通道，绕过冷却）
        if self.vault.has_recovery() and self.vault.verify_recovery(password):
            log.log("unlock.recovery", "使用恢复码解锁")
            self.throttle.record_success()
            self.root.after(0, lambda: self.unlock("恢复码解锁"))
            return True, ""

        # 3) 冷却期
        if self.throttle.in_cooldown():
            left = int(self.throttle.remaining_seconds()) + 1
            return False, f"错误次数过多，请等待 {left} 秒"

        # 4) 密码
        if self.vault.verify(password):
            self.throttle.record_success()
            log.log("unlock.ok", "密码正确")
            self.root.after(0, lambda: self.unlock("密码解锁"))
            return True, ""

        self.throttle.record_failure()
        log.log("unlock.fail", "密码错误")
        if self.throttle.in_cooldown():
            return False, (f"密码错误次数过多，已进入 "
                           f"{self.throttle.cooldown_seconds} 秒冷却")
        return False, f"密码错误，还可尝试 {self.throttle.attempts_left()} 次"

    # ==========================================================
    # AI 任务状态联动
    # ==========================================================
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
        # 来自监控线程，必须切回主线程再动 UI
        self.root.after(0, lambda: self._handle_status_event(kind, data))

    def _on_status_snapshot(self, data):
        self.root.after(0, lambda: self._apply_status(data))

    def _apply_status(self, sessions):
        """多会话快照落地：tk / web 旧界面只画一张卡，取主会话。

        主会话 = 最新在跑的会话；都不在跑则取最新收尾的。多会话的完整
        展示只在 Qt 版（右侧会话卡堆叠）提供。
        """
        primary = pick_primary(sessions)
        self._last_status = primary
        task = str(primary.get("task") or "").strip() if primary else ""
        if task:
            self.tray and self.tray.update_tooltip(f"{APP_TITLE} · {task}")
        if self.lockscreen and self.locked and primary:
            self.lockscreen.update_status(primary)

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
        if self.lockscreen and self.locked:
            self.root.after(0, lambda: self.lockscreen
                            and self.lockscreen.show_pending_action(pending))

    def _schedule_action_tick(self) -> None:
        self.runner.tick(self._fire_action)
        self.root.after(500, self._schedule_action_tick)

    def _fire_action(self, pending) -> None:
        self._execute_action(pending.action, pending.reason)

    def _execute_action(self, action: str, reason: str) -> None:
        # 所有动作（含解锁、仅提示）统一在这里记一笔。
        # 之前「仅提示」走了提前 return，日志里什么都看不到，出了问题没法排查。
        log.log("action.execute", f"{action} | {reason}")
        if action == A.ACTION_UNLOCK:
            self.unlock("任务完成，自动解锁")
            return
        if action == A.ACTION_NOTIFY:
            A.beep("ok")
            if self.lockscreen and self.locked:
                self.lockscreen.set_message(f"{reason} · 已完成", color="#34D399")
            else:
                # 没锁屏时至少要让用户知道，否则「仅提示」等于什么都没发生
                self._balloon("任务完成", reason)
            return
        ok, message = A.execute(action, reason)
        log.log("action.result", f"{action} -> ok={ok} {message}")
        if not ok and self.lockscreen and self.locked:
            self.lockscreen.set_message(message)

    # ==========================================================
    # 后台周期任务
    # ==========================================================
    def _schedule_heartbeat(self) -> None:
        self.watchdog.beat(os.getpid(), self.locked)
        self.watchdog.check_child()
        self.root.after(2000, self._schedule_heartbeat)

    def _schedule_ipc(self) -> None:
        """第二个进程通过文件请求本实例锁定。"""
        if self.lock_request.consume() and not self.locked:
            log.log("ipc.lock", "收到外部锁定请求")
            self.lock()
        self.root.after(500, self._schedule_ipc)

    def _start_idle_watcher(self) -> None:
        minutes = float(self.cfg.get("lock.auto_lock_idle_minutes", 0) or 0)
        if minutes <= 0:
            return
        self.idle_watcher = IdleWatcher(
            minutes * 60, lambda: self.root.after(0, self.lock)).start()

    # ==========================================================
    # 电源自检 / 网络看门狗
    # ==========================================================
    def _power_selfcheck(self) -> None:
        """启动时把电源环境写日志；睡眠超时过短给一个不弹窗的托盘提醒。"""
        try:
            warns = check_environment()
        except Exception as exc:
            log.log("power.error", f"自检失败: {exc!r}")
            return
        if warns:
            self.root.after(0, lambda: self._balloon(
                "AiLock 电源提示", "\n".join(warns[:2])))

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
        # 探测线程回调，切回主线程再动 UI
        self.root.after(0, lambda: self._net_event_ui(event))

    def _net_event_ui(self, event: str) -> None:
        if event == "down":
            self._balloon("网络异常",
                          "连续多次探测失败，主机可能已断网，请检查网络。")
        elif event == "still":
            self._balloon("网络仍不可用", "断网已持续超过 10 分钟。")
        elif event == "recover":
            self._balloon("网络已恢复", "网络探测恢复正常。")

    # ==========================================================
    # 托盘
    # ==========================================================
    def _start_tray(self) -> None:
        self.tray = TrayApp(
            icon_path=icon_path() or self._fallback_icon(),
            tooltip=APP_TITLE,
            menu_items=[
                ("立即锁定", lambda: self.root.after(0, self.lock)),
                ("设置...", lambda: self.root.after(0, self.open_settings)),
                ("打开日志", self._open_log),
                ("打开配置目录", self._open_config_dir),
                ("-", None),
                ("退出", lambda: self.root.after(0, self.quit)),
            ],
            hotkey=self.cfg.get("hotkey", "ctrl+alt+l"),
            on_hotkey=lambda: self.root.after(0, self.lock),
        )
        self.tray.start()
        self.tray.wait_ready(5)
        if self.tray._hotkey_error:
            self._balloon("热键未生效", self.tray._hotkey_error)

    def _fallback_icon(self) -> str:
        """没有图标文件时，用系统自带的应用图标顶上。"""
        try:
            import win32con
            import win32gui
            win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        except Exception:
            pass
        return ""

    def _update_tray(self) -> None:
        if self.tray:
            self.tray.update_tooltip(
                f"{APP_TITLE} · 已锁定" if self.locked else APP_TITLE)

    def _balloon(self, title: str, message: str) -> None:
        if self.tray:
            self.tray.balloon(title, message)

    def _open_log(self) -> None:
        p = log.path
        if p.exists():
            os.startfile(str(p))

    def _open_config_dir(self) -> None:
        from core.config import app_dir
        os.startfile(str(app_dir()))

    # ==========================================================
    # 设置
    # ==========================================================
    def open_settings(self) -> None:
        log.log("settings.open", f"locked={self.locked} already={self._settings_open}")
        if self.locked:
            self._balloon("锁定中", "请先解锁再打开设置。")
            return
        if self._settings_open:
            return
        self._settings_open = True
        try:
            # web 版回调叫 on_close，tk 版叫 on_cancel —— 按模式传对参数
            if USE_WEB and WebUI is not None:
                panel = WebSettings(
                    self.root, self.cfg, self.vault,
                    on_saved=self._on_settings_saved,
                    on_lock_now=self.lock,
                    on_close=lambda: self._close_settings(panel),
                )
            else:
                panel = SettingsPanel(
                    self.root, self.cfg, self.vault,
                    on_saved=self._on_settings_saved,
                    on_lock_now=self.lock,
                    on_cancel=lambda: self._close_settings(panel),
                )
        except Exception:
            import traceback
            log.log("settings.error", traceback.format_exc(limit=5))
            self._settings_open = False
            return
        self._panel = panel  # 持有引用，防止面板被 GC 回收
        panel.on_cancel = lambda: self._close_settings(panel)
        panel.win.protocol("WM_DELETE_WINDOW",
                           lambda: self._close_settings(panel))
        panel.win.deiconify()
        panel.win.lift()
        panel.win.focus_force()

    def _close_settings(self, panel) -> None:
        if not self._settings_open:
            return
        self._settings_open = False
        self._panel = None
        panel.win.destroy()

    def _close_settings_for_web(self) -> None:
        """供 Web 设置窗口的「关闭」按钮（JS 桥接）调用。"""
        if self._panel:
            self._close_settings(self._panel)

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

    # ==========================================================
    # 退出
    # ==========================================================
    def quit(self) -> None:
        if self.locked:
            self._balloon("锁定中无法退出", "请先解锁。忘记密码可使用恢复码。")
            return
        self.shutdown()

    def shutdown(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        log.log("app.quit", "正常退出")
        self.runner.cancel("程序退出")
        self.unlock("程序退出")
        if self.status_monitor:
            self.status_monitor.stop()
        if self.idle_watcher:
            self.idle_watcher.stop()
        if self.net_watch:
            self.net_watch.stop()
        self.watchdog.stop()
        if self.tray:
            self.tray.stop()
        if USE_WEB and WebUI is not None:
            try:
                WebUI.get().stop()
            except Exception:
                pass
        self.root.after(100, self.root.destroy)


# ==========================================================
# 自检
# ==========================================================
def run_selftest(root, cfg, seconds: float = 6.0) -> None:
    """铺满所有屏幕若干秒，验证多屏覆盖、钩子、电源管制是否正常。"""
    from core.monitors import list_monitors

    monitors = list_monitors()
    print(f"[selftest] 显示器 {len(monitors)} 个:")
    for m in monitors:
        print(f"  {m['w']}x{m['h']} @ ({m['x']},{m['y']}) primary={m['primary']}")

    vault = Vault()
    if not vault.is_set:
        # 关键：绝不能在这里偷偷给用户设密码，否则用户自己都不知道密码是啥。
        # 未设密码时 selftest 只能演示空锁屏（不会真的锁），通过其他方式验证。
        print("[selftest] 提示：尚未设置密码，selftest 仅展示界面布局。")
        cfg.set("background.mode", "color", autosave=False)
        cfg.set("background.color", "#0B1220", autosave=False)
        cfg.set("message.text", "自检模式 · 尚未设置密码", autosave=False)
    else:
        cfg.set("background.mode", "color", autosave=False)
        cfg.set("background.color", "#0B1220", autosave=False)
        cfg.set("message.text", "自检模式 —— 几秒后自动退出", autosave=False)
    cfg.set("status.enabled", True, autosave=False)

    hook = KeyboardBlocker().start()
    print(f"[selftest] 键盘钩子: {'已安装' if hook.installed else '安装失败'}")

    power = PowerGuard(keep_display_on=True).start()
    print("[selftest] 电源管制: 已启动（系统不会休眠）")

    if USE_WEB and WebUI is not None:
        from core.security import Vault as _Vault
        WebUI.get().attach(root, cfg, _Vault(), None)

    screen = LockScreen(root, cfg, lambda p: (False, "自检模式，不接受解锁"))
    screen.show(0)
    print(f"[selftest] 已创建 {len(screen.windows)} 个全屏窗口")

    # 给一个看起来像样的状态，让卡片内容完整
    sample_status = {
        "task": "Qwen2.5-Coder 14B 微调",
        "state": "running",
        "progress": 0.37,
        "lines": [
            "epoch 12/32",
            "loss 1.842 → 1.367",
            "预计剩余 18 分钟",
        ],
    }
    # 延迟推送：确保 webview 已启动、窗口已就绪后再 evaluate_js
    if USE_WEB and WebUI is not None:
        root.after(400, lambda: screen.update_status(sample_status))
    else:
        screen.update_status(sample_status)
    print("[selftest] 已安排示例任务状态推送")
    print("  截图提示：另开终端运行 .venv\\Scripts\\python.exe -m PIL.ImageGrab")

    for i, win in enumerate(screen.windows):
        w = getattr(win, "width", 0) or 0
        h = getattr(win, "height", 0) or 0
        print(f"  窗口{i}: {'Web' if USE_WEB else 'Tk'} 初始尺寸={w}x{h}")

    def finish():
        if finish.done:
            return
        if time.time() < finish.at:
            root.after(200, finish)
            return
        finish.done = True
        try:
            print(f"[selftest] 期间拦截了 {hook.blocked_count} 次按键")
        finally:
            screen.destroy()
            hook.stop()
            power.stop()
        print("[selftest] 完成，已释放所有资源")
        if USE_WEB and WebUI is not None:
            WebUI.get().stop()   # 关掉所有 webview 窗口 → webview.start() 返回
        else:
            root.quit()
            root.destroy()

    finish.done = False
    finish.at = time.time() + seconds
    root.after(200, finish)

    # 主线程阻塞：泵 webview 消息循环，直到 finish 关掉所有窗口
    if USE_WEB and WebUI is not None:
        WebUI.get().start()


# ==========================================================
# 入口
# ==========================================================
def raw_argv(argv) -> list:
    if argv is None:
        return list(sys.argv[1:])
    return list(argv)


def take_run_argv(argv: list):
    """若命令行里有 --run，把它之后的所有参数原样切出来。

    关键点：必须**手工切一刀**，不能让主解析器和被包装命令碰面。
    `AiLock.exe --run -- python train.py --epochs 50` 里的 `--epochs`
    是训练脚本自己的参数，交给 argparse 会直接报「无法识别的参数」。

    返回 None 表示不是 run 模式。
    """
    try:
        i = argv.index("--run")
    except ValueError:
        return None
    return argv[i + 1:]


def build_parser():
    import argparse
    ap = argparse.ArgumentParser(
        prog="AiLock",
        description="为「AI 在跑、人不在」场景设计的 Windows 轻量锁屏",
    )
    ap.add_argument("--lock", action="store_true", help="立即锁定")
    ap.add_argument("--settings", action="store_true", help="打开设置面板")
    ap.add_argument("--notify", action="store_true",
                    help="状态上报模式，等价于 notify.py")
    ap.add_argument("--run", action="store_true",
                    help="包装模式：AiLock --run -t 任务名 -- 你的命令")
    ap.add_argument("--selftest", type=float, nargs="?", const=6.0,
                    metavar="SECONDS", help="自检：铺满所有屏幕 N 秒后退出")
    ap.add_argument("--watchdog-child", nargs=2, metavar=("HEARTBEAT", "STALE"),
                    help=argparse.SUPPRESS)
    ap.add_argument("--from-watchdog", action="store_true", help=argparse.SUPPRESS)
    return ap


def main(argv=None) -> int:
    # 包装模式必须先于 argparse 处理，见 take_run_argv 的说明
    run_argv = take_run_argv(raw_argv(argv))
    if run_argv is not None:
        import ailock_run
        return ailock_run.main(run_argv)

    args, unknown = build_parser().parse_known_args(argv)

    # 看门狗子进程：独立、极简，不初始化任何 UI
    if args.watchdog_child:
        return watchdog_child_main(args.watchdog_child[0],
                                   float(args.watchdog_child[1]))

    # 状态上报：转发给 notify.py，方便只分发一个 exe
    if args.notify:
        import notify
        return notify.main(unknown)

    if not try_acquire_single_instance():
        if args.lock:
            LockRequest().request()
            print("已请求正在运行的 AiLock 实例锁定屏幕")
        else:
            print("AiLock 已在运行中（托盘图标可找到它）")
        return 0

    cfg = Config()
    autostart.sync(bool(cfg.get("autostart", False)))

    # Qt 是默认界面，走独立分支（有自己的事件循环，不用 tkinter root）
    if USE_QT:
        return run_qt(cfg, args)

    if USE_WEB:
        root = AppRoot()
    else:
        root = tk.Tk()
        try:
            if icon_path():
                root.iconbitmap(icon_path())
        except Exception:
            pass

    if args.selftest is not None:
        run_selftest(root, cfg, max(1.0, float(args.selftest)))
        return 0

    app = App(root, cfg, start_locked=bool(args.lock or args.from_watchdog))
    if args.settings:
        root.after(600, app.open_settings)

    try:
        if USE_WEB:
            WebUI.get().start()       # 主线程：阻塞直到所有窗口关闭
        else:
            root.mainloop()
    except KeyboardInterrupt:
        app.shutdown()
    return 0


# ==========================================================
# Qt 入口
# ==========================================================
def run_qt(cfg, args) -> int:
    """PySide6 界面入口：构造 QApplication，跑 Qt 事件循环。"""
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)   # 托盘常驻，关窗不退出
    qapp.setApplicationName(APP_TITLE)

    if args.selftest is not None:
        return run_qt_selftest(qapp, cfg, max(1.0, float(args.selftest)))

    from ui.qt.app import QtApp
    controller = QtApp(cfg,
                       start_locked=bool(args.lock or args.from_watchdog),
                       icon_path=icon_path())
    if args.settings:
        QTimer.singleShot(600, controller.open_settings)

    try:
        return qapp.exec()
    except KeyboardInterrupt:
        controller.shutdown()
    return 0


def run_qt_selftest(qapp, cfg, seconds: float) -> int:
    """铺满所有屏幕若干秒后自动退出，验证多屏覆盖、钩子、电源管制。"""
    from PySide6.QtCore import QTimer

    from core.monitors import list_monitors
    from ui.qt.lock_window import LockScreen as QtLockScreen

    monitors = list_monitors()
    print(f"[selftest] 显示器 {len(monitors)} 个:")
    for m in monitors:
        print(f"  {m['w']}x{m['h']} @ ({m['x']},{m['y']}) primary={m['primary']}")

    vault = Vault()
    cfg.set("background.mode", "color", autosave=False)
    cfg.set("background.color", "#0B1220", autosave=False)
    if not vault.is_set:
        cfg.set("message.text", "自检模式 · 尚未设置密码", autosave=False)
        print("[selftest] 提示：尚未设置密码，selftest 仅展示界面布局。")
    else:
        cfg.set("message.text", "自检模式 —— 几秒后自动退出", autosave=False)

    hook = KeyboardBlocker().start()
    print(f"[selftest] 键盘钩子: {'已安装' if hook.installed else '安装失败'}")
    power = PowerGuard(keep_display_on=True).start()
    print("[selftest] 电源管制: 已启动（系统不会休眠）")

    screen = QtLockScreen(None, cfg,
                          lambda p: (False, "自检模式，不接受解锁"))
    screen.show(0)
    print(f"[selftest] 已创建 {len(screen.windows)} 个全屏窗口")
    for i, win in enumerate(screen.windows):
        print(f"  窗口{i}: Qt 尺寸={win.width()}x{win.height()}")

    # 两条会话：验证右侧一会话一卡的堆叠效果
    now = time.time()
    samples = [
        {
            "sid": "zc-a1b2",
            "task": "Qwen2.5-Coder 14B 微调",
            "state": "running",
            "progress": 0.37,
            "lines": ["epoch 12/32", "loss 1.842 → 1.367", "预计剩余 18 分钟"],
            "updated": now,
        },
        {
            "sid": "zc-9f3c",
            "task": "重构订单模块",
            "state": "running",
            "progress": 0.72,
            "lines": ["改完 Service 层", "单测 12 / 15 通过"],
            "updated": now - 5,
        },
    ]
    QTimer.singleShot(500, lambda: screen.update_status(samples))

    def finish():
        try:
            print(f"[selftest] 期间拦截了 {hook.blocked_count} 次按键")
        finally:
            screen.destroy()
            hook.stop()
            power.stop()
        print("[selftest] 完成，已释放所有资源")
        qapp.quit()

    QTimer.singleShot(int(seconds * 1000), finish)
    return qapp.exec()


if __name__ == "__main__":
    sys.exit(main())
