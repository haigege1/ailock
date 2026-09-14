"""电源管制：锁屏期间禁止休眠。

两套机制叠加使用，原因见下：

1. `SetThreadExecutionState`（本文件原有）—— **线程级**，调用线程退出即失效，
   所以必须由一个常驻线程周期性重新声明（心跳）。

      ES_SYSTEM_REQUIRED  阻止系统进入睡眠（AI 任务照跑）
      ES_DISPLAY_REQUIRED 额外阻止显示器熄灭（屏幕保持常亮）

2. `PowerCreateRequest` / `PowerSetRequest`（本文件新增）—— **进程级**计数请求。
   本机是 Modern Standby（S0，无 S3），实测只靠第 1 套挡不住息屏后的
   「连接待机」：系统日志 Kernel-Power 506/507 显示，AiLock 一广播关屏，
   系统 2 秒内就进入连接待机并把进程整个挂起（9/4 一次挂了 47 分钟）。
   进程级请求里 `PowerRequestExecutionRequired` 还能阻止 DAM（Desktop
   Activity Moderator）把桌面进程挂起——这正是「主机没睡但进程被冻」的解药。
   备注（MSDN）：仅在 **直流供电** 时，系统/执行请求会在睡眠超时到期
   5 分钟后被强制终止；交流供电下不会。

两套同时失效的概率远小于单独一套，代价只是多一个内核句柄。
"""

import ctypes
import re
import threading
import time
from ctypes import wintypes

from .idle import idle_seconds
from .winapi import kernel32, user32
from .logger import log

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

_HEARTBEAT_SECONDS = 20

# ---------------------------------------------------------------- 进程级电源请求
POWER_REQUEST_CONTEXT_VERSION = 0
POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x00000001

PowerRequestDisplayRequired = 0
PowerRequestSystemRequired = 1
PowerRequestAwayModeRequired = 2          # 仅 S3 传统待机，本程序不用
PowerRequestExecutionRequired = 3


class _DETAILED(ctypes.Structure):
    _fields_ = [("LocalizedReasonModule", wintypes.HMODULE),
                ("LocalizedReasonId", wintypes.ULONG),
                ("ReasonStringCount", wintypes.ULONG),
                ("ReasonStrings", ctypes.POINTER(ctypes.c_wchar_p))]


class _REASON(ctypes.Union):
    _fields_ = [("Detailed", _DETAILED),
                ("SimpleReasonString", ctypes.c_wchar_p)]


class REASON_CONTEXT(ctypes.Structure):
    _fields_ = [("Version", wintypes.ULONG),
                ("Flags", wintypes.DWORD),
                ("Reason", _REASON)]


class PowerRequest:
    """进程级电源请求句柄封装。

    用法：open() 后按需 system() / execution() / display(True|False)，
    退出前 close()。任何一步失败都只记日志、不影响调用方继续跑 —— 电源
    管制属于「尽力而为」，绝不能因为它把锁屏拖崩。
    """

    _bound = False

    def __init__(self, reason: str = "AiLock 正在守护 AI 任务"):
        self.reason_text = reason
        self._handle = None
        self._display_on = False
        self._reason = None        # 必须持有字符串引用，别让它被回收

    # ------------------------------------------------------------ 绑定
    @classmethod
    def _bind(cls) -> None:
        """只绑定一次；WinDLL 的函数对象是共享的，重复设置 argtypes 无害但没必要。"""
        if cls._bound:
            return
        kernel32.PowerCreateRequest.argtypes = [ctypes.POINTER(REASON_CONTEXT)]
        kernel32.PowerCreateRequest.restype = wintypes.HANDLE
        kernel32.PowerSetRequest.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.PowerSetRequest.restype = wintypes.BOOL
        kernel32.PowerClearRequest.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.PowerClearRequest.restype = wintypes.BOOL
        cls._bound = True

    # ------------------------------------------------------------ 生命周期
    def open(self) -> bool:
        """创建请求对象。失败返回 False（调用方应降级继续）。"""
        self._bind()
        try:
            self._reason = ctypes.c_wchar_p(self.reason_text)
            ctx = REASON_CONTEXT()
            ctx.Version = POWER_REQUEST_CONTEXT_VERSION
            ctx.Flags = POWER_REQUEST_CONTEXT_SIMPLE_STRING
            ctx.Reason.SimpleReasonString = self._reason
            h = kernel32.PowerCreateRequest(ctypes.byref(ctx))
        except Exception as exc:
            log.log("power.warn", f"PowerCreateRequest 异常: {exc!r}")
            return False
        # 无效句柄是 INVALID_HANDLE_VALUE(-1) 或 NULL
        if not h or h == wintypes.HANDLE(-1).value:
            log.log("power.warn", f"PowerCreateRequest 失败: {ctypes.get_last_error()}")
            self._handle = None
            return False
        self._handle = int(h)
        return True

    def close(self) -> None:
        if self._handle is None:
            return
        for kind in (PowerRequestDisplayRequired,
                     PowerRequestExecutionRequired,
                     PowerRequestSystemRequired):
            try:
                kernel32.PowerClearRequest(wintypes.HANDLE(self._handle),
                                           wintypes.DWORD(kind))
            except Exception:
                pass
        try:
            kernel32.CloseHandle(wintypes.HANDLE(self._handle))
        except Exception:
            pass
        self._handle = None
        self._display_on = False

    # ------------------------------------------------------------ 请求开关
    def _set(self, kind: int) -> bool:
        if self._handle is None:
            return False
        try:
            ok = kernel32.PowerSetRequest(wintypes.HANDLE(self._handle),
                                          wintypes.DWORD(kind))
        except Exception as exc:
            log.log("power.warn", f"PowerSetRequest({kind}) 异常: {exc!r}")
            return False
        if not ok:
            log.log("power.warn",
                    f"PowerSetRequest({kind}) 失败: {ctypes.get_last_error()}")
        return bool(ok)

    def _clear(self, kind: int) -> bool:
        if self._handle is None:
            return False
        try:
            ok = kernel32.PowerClearRequest(wintypes.HANDLE(self._handle),
                                            wintypes.DWORD(kind))
        except Exception:
            return False
        return bool(ok)

    def system(self) -> bool:
        """阻止系统进入睡眠（对应 ES_SYSTEM_REQUIRED）。"""
        return self._set(PowerRequestSystemRequired)

    def execution(self) -> bool:
        """阻止本进程被 Modern Standby 的 DAM 挂起。"""
        return self._set(PowerRequestExecutionRequired)

    def display(self, on: bool) -> bool:
        """开关「显示器保持点亮」请求；状态没变就不重复调用。"""
        if on == self._display_on:
            return True
        ok = self._set(PowerRequestDisplayRequired) if on \
            else self._clear(PowerRequestDisplayRequired)
        self._display_on = bool(on)
        return ok


def _apply(flags: int) -> int:
    return kernel32.SetThreadExecutionState(ctypes.c_uint(flags))


def release() -> None:
    """交还电源管理权：只保留 ES_CONTINUOUS 即表示清除之前的请求。"""
    try:
        _apply(ES_CONTINUOUS)
    except Exception:
        pass


class PowerGuard(threading.Thread):
    """在锁屏期间持续阻止休眠。

    keep_display_on=False：完全不干预显示器，息屏跟随系统电源计划。
    keep_display_on=True 且 off_after_minutes>0：锁定期间保持常亮，键鼠
    无操作达到时长后主动息屏（广播 SC_MONITORPOWER）；期间任何键鼠活动
    都会恢复常亮并重新计时。ES_SYSTEM_REQUIRED 始终保留，主机绝不休眠。
    """

    def __init__(self, keep_display_on: bool = True,
                 off_after_minutes: float = 0,
                 blackout_mode: bool | None = None,
                 on_blackout=None):
        super().__init__(daemon=True, name="PowerGuard")
        self.keep_display_on = bool(keep_display_on)
        self.off_after_seconds = max(0.0, float(off_after_minutes or 0) * 60.0)
        # blackout_mode=None 表示自动判断：Modern Standby 机器不能真关屏，
        # 改为「显示器保持点亮 + 锁屏窗口涂黑」；传统 S3 机器照旧广播关屏。
        if blackout_mode is None:
            blackout_mode = bool(self.off_after_seconds > 0
                                 and is_modern_standby())
        self.blackout_mode = bool(blackout_mode) and self.off_after_seconds > 0
        self.on_blackout = on_blackout        # callable(bool)，工作线程回调
        self._stop_event = threading.Event()
        self._req: PowerRequest | None = None
        self._last_blackout = False

    def _flags(self) -> int:
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        if self.keep_display_on and self.off_after_seconds <= 0:
            flags |= ES_DISPLAY_REQUIRED
        if self.blackout_mode:
            # 黑屏模式下显示器必须始终保持点亮：一旦让 Windows 自己把屏
            # 关掉（显示超时），就会立刻触发连接待机，前功尽弃。
            flags |= ES_DISPLAY_REQUIRED
        return flags

    def _compute(self, idle: float, display_off: bool) -> tuple:
        """纯逻辑，方便单测。

        输入键鼠空闲秒数与当前息屏状态，返回
        (flags, 新的息屏状态, 是否需要广播关屏)。

        黑屏模式：display_off 参数复用为「当前涂黑状态」，第二个返回值是
        新的涂黑状态，第三个恒为 False —— 永远不广播关屏。
        """
        if self.blackout_mode:
            flags = self._flags()
            blacked = idle >= self.off_after_seconds
            return flags, blacked, False
        if not self.keep_display_on or self.off_after_seconds <= 0:
            return self._flags(), display_off, False
        if idle < self.off_after_seconds:
            flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            # 用户回来了：恢复常亮，无需广播
            return flags, False, False
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        return flags, True, not display_off   # 刚跨过阈值才广播一次

    def start(self):
        """返回 self，方便写成 `guard = PowerGuard(...).start()`。

        threading.Thread.start() 返回 None，不重写的话这行会拿到 None。
        """
        super().start()
        return self

    # ---------------------------------------------------------- 进程级请求
    def _open_request(self) -> None:
        """起进程级电源请求：系统不睡 + 本进程不被 DAM 挂起。"""
        self._req = PowerRequest()
        if not self._req.open():
            self._req = None
            log.log("power.warn", "进程级电源请求不可用，仅依赖 SetThreadExecutionState")
            return
        ok_sys = self._req.system()
        ok_exe = self._req.execution()
        log.log("power.request",
                f"已建立进程级电源请求 system={ok_sys} execution={ok_exe}")

    def _close_request(self) -> None:
        if self._req is not None:
            self._req.close()
            self._req = None
            log.log("power.request", "进程级电源请求已释放")

    def run(self) -> None:
        log.log("power.start", f"keep_display_on={self.keep_display_on} "
                               f"off_after={self.off_after_seconds:.0f}s "
                               f"blackout={self.blackout_mode}")
        self._open_request()
        failed = False
        display_off = False
        # 息屏模式下用更短的轮询间隔，让「到点息屏」和「活动恢复常亮」更跟手
        wait = 5.0 if self.off_after_seconds > 0 else _HEARTBEAT_SECONDS
        while not self._stop_event.is_set():
            try:
                idle = idle_seconds()
                flags, display_off, need_off = self._compute(idle, display_off)
                if need_off:
                    log.log("power.display",
                            f"锁屏无输入超过 {self.off_after_seconds:.0f} 秒，"
                            f"自动息屏（主机不休眠）")
                    if not MonitorOff.turn_off():
                        log.log("power.warn", "关屏广播超时，可能有窗口无响应")
                elif self.blackout_mode and display_off != self._last_blackout:
                    # 黑屏模式：状态翻转时通知 UI（不广播关屏）
                    if display_off:
                        log.log("power.display",
                                f"锁屏无输入超过 {self.off_after_seconds:.0f} "
                                f"秒，进入黑屏（Modern Standby：不真关屏，"
                                f"主机保持唤醒）")
                    else:
                        log.log("power.display", "检测到键鼠活动，黑屏恢复")
                    self._last_blackout = bool(display_off)
                    if self.on_blackout:
                        try:
                            self.on_blackout(bool(display_off))
                        except Exception as exc:  # pragma: no cover
                            log.log("power.warn", f"on_blackout 回调异常: {exc!r}")
                elif display_off and idle < self.off_after_seconds \
                        and self.keep_display_on and self.off_after_seconds > 0 \
                        and not self.blackout_mode:
                    log.log("power.display", "检测到键鼠活动，恢复屏幕常亮")
                # 进程级的「显示器常亮」请求跟随同一套判断
                if self._req is not None:
                    self._req.display(bool(flags & ES_DISPLAY_REQUIRED))
                prev = _apply(flags)
                if not prev and not failed:
                    log.log("power.warn", "SetThreadExecutionState 返回 0")
                    failed = True
            except Exception as exc:  # pragma: no cover
                log.log("power.error", repr(exc))
            self._stop_event.wait(wait)
        self._close_request()
        release()
        log.log("power.stop", "电源管制已释放")

    def stop(self) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=3)
        if self._req is not None:
            self._close_request()
        release()


class MonitorOff:
    """「允许息屏」模式：仅让显示器熄灭，系统依然保持唤醒。

    Windows 没有公开的「只关显示器」API，惯用手法是广播
    WM_SYSCOMMAND / SC_MONITORPOWER，效果等同于系统空闲超时关屏。
    """

    HWND_BROADCAST = 0xFFFF
    WM_SYSCOMMAND = 0x0112
    SC_MONITORPOWER = 0xF170
    SMTO_ABORTIFHUNG = 0x0002
    TIMEOUT_MS = 2000

    _bound = False

    @classmethod
    def turn_off(cls) -> bool:
        """广播关屏。返回 False 表示广播超时（有窗口不响应）。

        必须用 SendMessageTimeout 而不是 SendMessage：后者向 HWND_BROADCAST
        发送时会**同步等待每一个顶层窗口**返回，只要有一个窗口无响应，调用
        线程就永久卡死 —— PowerGuard 线程一旦卡死，电源请求再也刷新不了，
        息屏后必然掉进连接待机。改用带超时的版本，顶多广播失败，不会陪葬。
        """
        if not cls._bind():
            return False
        result = wintypes.DWORD()
        try:
            ok = user32.SendMessageTimeoutW(
                wintypes.HWND(cls.HWND_BROADCAST),
                wintypes.UINT(cls.WM_SYSCOMMAND),
                wintypes.WPARAM(cls.SC_MONITORPOWER),
                wintypes.LPARAM(2),
                wintypes.UINT(cls.SMTO_ABORTIFHUNG),
                wintypes.UINT(cls.TIMEOUT_MS),
                ctypes.byref(result),
            )
        except Exception as exc:
            log.log("power.warn", f"关屏广播异常: {exc!r}")
            return False
        return bool(ok)

    @classmethod
    def _bind(cls) -> bool:
        if cls._bound:
            return True
        try:
            user32.SendMessageTimeoutW.argtypes = [
                wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                wintypes.UINT, wintypes.UINT, ctypes.POINTER(wintypes.DWORD)]
            user32.SendMessageTimeoutW.restype = wintypes.LPARAM
        except Exception as exc:
            log.log("power.warn", f"绑定 SendMessageTimeoutW 失败: {exc!r}")
            return False
        cls._bound = True
        return True


# ==========================================================
# 启动自检：电源环境
#
# 背景：本机类设备只支持 Modern Standby（S0，无 S3），睡眠超时一到
# 系统就进 S0 待机，网络随之挂起。AiLock 活着时无所谓，但它一旦退出
# 或失效，主机就会在「睡眠超时」后悄悄断网。自检负责把这个环境事实
# 写进日志，超时过短时提醒用户。
# ==========================================================

def _run_powercfg(args: list) -> str:
    """运行 powercfg 并返回输出。失败返回空串，绝不让自检拖垮主流程。"""
    import subprocess
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            ["powercfg", *args], capture_output=True,
            creationflags=flags, timeout=15)
    except Exception:
        return ""
    # 中文 Windows 输出是 ANSI（GBK）；mbcs 即当前代码页
    return proc.stdout.decode("mbcs", errors="replace")


def read_sleep_timeout_seconds():
    """当前电源计划的 AC 睡眠超时（秒）。0=从不，读不到返回 None。

    powercfg 输出里 最小/最大/增量/AC/DC 都是 0x 值，倒数第二个是
    AC，最后一个是 DC —— 与系统语言无关，不解析文字标签。
    """
    out = _run_powercfg(["/q", "SCHEME_CURRENT", "SUB_SLEEP", "STANDBYIDLE"])
    vals = re.findall(r"0x([0-9a-fA-F]+)", out)
    if len(vals) < 2:
        return None
    try:
        return int(vals[-2], 16)          # vals[-1] 是 DC，这里只关心 AC
    except ValueError:
        return None


def available_sleep_states() -> str:
    """powercfg /a 的「可用睡眠状态」段落文本（截掉不可用部分）。"""
    out = _run_powercfg(["/a"])
    for marker in ("are not available", "上没有以下睡眠状态"):
        i = out.find(marker)
        if i >= 0:
            return out[:i]
    return out


_MODERN_STANDBY = None


def is_modern_standby() -> bool:
    """本机是否只有 S0 现代待机（无传统 S3）。结果缓存，只查一次。

    Modern Standby 机器上「广播关屏」这个动作本身就是进入连接待机的
    扳机（2026-09-05 双轮实验实锤：即使睡眠超时=从不 + 持有
    SystemRequired/ExecutionRequired，关屏后 2 秒内必进待机并冻结进程），
    所以息屏必须改用「窗口涂黑」而不是真关屏。
    """
    global _MODERN_STANDBY
    if _MODERN_STANDBY is None:
        states = available_sleep_states()
        _MODERN_STANDBY = bool(states) and "S3" not in states
    return _MODERN_STANDBY


def evaluate_selfcheck(timeout_ac, states_text: str) -> list:
    """纯逻辑：根据睡眠超时与可用睡眠状态生成警告列表。可单测。"""
    if timeout_ac is None:
        return []                          # 读不到就别吓唬人
    if timeout_ac <= 0:
        return []                          # 从不睡眠，没有风险
    if not states_text:
        kind = "睡眠/待机"
    elif "S3" in states_text:
        kind = "睡眠"
    else:
        kind = "S0 待机（网络会挂起）"
    if timeout_ac >= 3600:
        desc = f"{timeout_ac // 3600} 小时"
    elif timeout_ac >= 60:
        desc = f"{timeout_ac // 60} 分钟"
    else:
        desc = f"{timeout_ac} 秒"
    return [
        f"系统电源计划设置了 {desc} 后自动进入{kind}。AiLock 运行期间"
        f"会阻止它；但 AiLock 一旦退出，超过该时长主机就会休眠并断开网络。",
    ]


def check_environment() -> list:
    """启动自检：把电源环境写入日志，返回警告列表（供 UI 提示）。"""
    timeout_ac = read_sleep_timeout_seconds()
    states = available_sleep_states()
    modern = bool(states) and "S3" not in states
    timeout_desc = "未知" if timeout_ac is None else f"{timeout_ac}s"
    log.log("power.selfcheck",
            f"睡眠超时(AC)={timeout_desc}  S3={'有' if 'S3' in states else '无'}"
            f"  ModernStandby={'是' if modern else '否'}")
    warns = evaluate_selfcheck(timeout_ac, states)
    for w in warns:
        log.log("power.warn", w)
    return warns
