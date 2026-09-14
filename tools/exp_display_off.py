"""息屏实验：验证「进程级电源请求」能否挡住 Modern Standby 的连接待机。

    python tools/exp_display_off.py            # 两阶段：对照组 25s + 修复组 60s
    python tools/exp_display_off.py --only fixed --seconds 60

背景
    本机只有 S0 现代待机（powercfg /a：无 S3、无休眠）。系统日志
    Kernel-Power 506/507 记录到，AiLock 一广播关屏，系统 2 秒内就进入
    连接待机并把进程整个挂起（9/4 一次挂了 47 分钟）。

原理
    脚本起一个 1 秒心跳线程，然后广播关屏，静默等待。如果系统进入连接
    待机，整个进程被冻结，心跳时间线会出现一段明显断档；反过来，全程
    没有断档就说明电源请求生效了。

    运行期间请**不要碰键鼠**（碰到会把屏幕点亮，实验就废了）。
    万一屏幕长时间不亮，动一下鼠标即可唤醒。

注意
    对照阶段有概率真的把机器冻住（这正是要复现的现象）。脚本结束后会
    主动广播开屏；若对照阶段被冻结，恢复后脚本会继续跑修复阶段。
"""
import argparse
import ctypes
import json
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.power import (ES_CONTINUOUS, ES_SYSTEM_REQUIRED,  # noqa: E402
                        PowerRequest, _apply)

WM_SYSCOMMAND = 0x0112
SC_MONITORPOWER = 0xF170
HWND_BROADCAST = 0xFFFF
SMTO_ABORTIFHUNG = 0x0002

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(wintypes.DWORD)]
_user32.SendMessageTimeoutW.restype = wintypes.LPARAM


def set_monitor(state: int) -> bool:
    """state: 2=关屏, -1=开屏。"""
    res = wintypes.DWORD()
    ok = _user32.SendMessageTimeoutW(
        wintypes.HWND(HWND_BROADCAST), wintypes.UINT(WM_SYSCOMMAND),
        wintypes.WPARAM(SC_MONITORPOWER), wintypes.LPARAM(state),
        wintypes.UINT(SMTO_ABORTIFHUNG), wintypes.UINT(2000),
        ctypes.byref(res))
    return bool(ok)


class Heartbeat(threading.Thread):
    """1 秒心跳，记录时间线，用来检测进程是否被冻结。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.stamps: list[float] = []
        self._stop = threading.Event()

    def run(self):
        while not self._stop.wait(1.0):
            self.stamps.append(time.time())

    def stop(self):
        self._stop.set()
        self.join(timeout=2)

    @staticmethod
    def max_gap(stamps):
        if len(stamps) < 2:
            return 0.0
        return max(b - a for a, b in zip(stamps, stamps[1:]))


def run_phase(name: str, seconds: int, use_request: bool,
              timeout_never: bool = False) -> dict:
    print(f"\n=== {name}：息屏 {seconds} 秒，"
          f"进程级电源请求={'开' if use_request else '关'}"
          f"{'，睡眠超时=从不' if timeout_never else ''} ===", flush=True)
    hb = Heartbeat()
    hb.start()
    req = None
    if use_request:
        req = PowerRequest("AiLock 息屏实验")
        if req.open():
            req.system()
            req.execution()
        else:
            req = None
    stop = threading.Event()

    def keep():
        while not stop.wait(5):
            _apply(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)

    kt = threading.Thread(target=keep, daemon=True)
    kt.start()

    old_timeout = None
    if timeout_never:
        # 读取当前 AC 睡眠超时并临时改为 0（从不），实验后恢复。
        # 注意：powercfg /change 的单位是分钟，/setacvalueindex 才是秒；
        # 用后者 + /setactive 保存并应用，避免四舍五入丢精度。
        from core.power import read_sleep_timeout_seconds
        old_timeout = read_sleep_timeout_seconds()
        if old_timeout:
            subprocess.run(
                ["powercfg", "/setacvalueindex", "SCHEME_CURRENT",
                 "SUB_SLEEP", "STANDBYIDLE", "0"], capture_output=True,
                timeout=15)
            subprocess.run(["powercfg", "/setactive", "SCHEME_CURRENT"],
                           capture_output=True, timeout=15)
        print(f"  睡眠超时(AC) {old_timeout}s -> 0（实验后恢复）", flush=True)

    try:
        time.sleep(2)                  # 让电源请求/策略先生效
        mark = len(hb.stamps)
        t0 = time.time()
        set_monitor(2)
        print(f"  {datetime.now():%H:%M:%S} 已关屏，{seconds} 秒内请勿操作",
              flush=True)
        time.sleep(seconds)
        elapsed = time.time() - t0
        set_monitor(-1)                # 主动开屏，不等用户动鼠标
    finally:
        if old_timeout:
            subprocess.run(
                ["powercfg", "/setacvalueindex", "SCHEME_CURRENT",
                 "SUB_SLEEP", "STANDBYIDLE", str(old_timeout)],
                capture_output=True, timeout=15)
            subprocess.run(["powercfg", "/setactive", "SCHEME_CURRENT"],
                           capture_output=True, timeout=15)
            print(f"  睡眠超时(AC) 已恢复为 {old_timeout}s", flush=True)
    phase_stamps = hb.stamps[mark:]
    stop.set()
    hb.stop()
    if req:
        req.close()

    gap = Heartbeat.max_gap(phase_stamps)
    print(f"  {datetime.now():%H:%M:%S} 结束："
          f"墙钟 {elapsed:.1f}s，心跳 {len(phase_stamps)} 次，"
          f"最大断档 {gap:.1f}s", flush=True)
    return {"phase": name, "use_request": use_request, "planned": seconds,
            "wall": round(elapsed, 1), "beats": len(phase_stamps),
            "max_gap": round(gap, 1),
            "frozen": gap > 10,
            "timeout_never": timeout_never,
            "start": datetime.fromtimestamp(t0).strftime("%H:%M:%S")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=("control", "fixed", "fixed2"),
                    help="只跑某一阶段")
    ap.add_argument("--seconds", type=int, default=0,
                    help="覆盖时长（默认：对照 25s / 修复 60s）")
    args = ap.parse_args()

    results = []
    try:
        if args.only in (None, "control"):
            results.append(run_phase("对照组（旧行为）",
                                     args.seconds or 25, False))
        if args.only in (None, "fixed"):
            results.append(run_phase("修复组（进程级请求）",
                                     args.seconds or 60, True))
        if args.only in (None, "fixed2"):
            results.append(run_phase("修复组2（请求+睡眠超时从不）",
                                     args.seconds or 60, True,
                                     timeout_never=True))
    finally:
        set_monitor(-1)                # 无论如何别把屏幕留在关闭状态

    print("\n=== 结论 ===")
    for r in results:
        verdict = "被冻结（进了连接待机）" if r["frozen"] else "全程存活"
        print(f"  {r['phase']}：{verdict}，最大断档 {r['max_gap']}s")
    print("\n提示：用下面这条命令核对系统日志（需要管理员权限）：")
    print('  Get-WinEvent -FilterHashtable @{LogName="System";'
          'ProviderName="Microsoft-Windows-Kernel-Power";'
          'Id=@(506,507);StartTime=(Get-Date).AddMinutes(-10)}')
    out = Path(__file__).resolve().parent / "_verify_out" / "display_off.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n明细已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
