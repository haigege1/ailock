"""看门狗：主进程被杀掉后自动重新拉起并重新锁定。

设计选择：
    用「心跳文件 + 停止标记」而不是单纯查 PID。
    - 主进程每 2 秒刷新心跳文件的 mtime，并写入当前是否处于锁定状态
    - 看门狗发现心跳超过 stale_seconds 未更新，且最后一次记录是「已锁定」，
      才判定为「被杀」，重新拉起主进程并直接锁定
    - 主进程正常退出时会写入 alive=false 的停止标记，看门狗见此标记自行退出，
      不会把正常关闭误判成崩溃
    这样既规避了 PID 复用问题，也避免了「用户主动退出后又被拉起来」的恼人行为。
"""

import json
import os
import sys
import time

from .config import Heartbeat, app_dir
from .logger import log
from .winapi import relaunch_self

DEFAULT_STALE_SECONDS = 8.0


def watchdog_child_main(heartbeat_path: str, stale_seconds: float) -> int:
    """看门狗子进程主体。以 `--watchdog-child` 参数启动。"""
    hb = Heartbeat(__import__("pathlib").Path(heartbeat_path))
    log.log("watchdog.start", f"监控心跳 {heartbeat_path}")

    while True:
        time.sleep(2)
        try:
            data = hb.read()
        except Exception:
            continue

        # 主进程干净退出留下的停止标记
        if data and data.get("alive") is False:
            log.log("watchdog.stop", "收到正常退出标记，看门狗退出")
            try:
                os.unlink(heartbeat_path)
            except OSError:
                pass
            return 0

        if not data:
            continue

        ts = float(data.get("ts", 0) or 0)
        was_locked = bool(data.get("locked"))
        if ts <= 0:
            continue

        if (time.time() - ts) > stale_seconds:
            if was_locked:
                log.log("watchdog.relaunch", "检测到主进程失联且处于锁定状态，重新拉起")
                relaunch_self(["--lock", "--from-watchdog"])
            else:
                log.log("watchdog.stop", "主进程失联，但未处于锁定状态，不拉起")
            return 0


class Watchdog:
    """主进程侧：负责拉起看门狗子进程，并在它被杀掉时补一个。"""

    def __init__(self, stale_seconds: float = DEFAULT_STALE_SECONDS):
        self.hb = Heartbeat()
        self.stale = float(stale_seconds)
        self._proc = None
        self._last_ts = 0.0

    # ---------- 心跳 ----------
    def beat(self, pid: int, locked: bool) -> None:
        self.hb.beat(pid, locked)
        self._last_ts = time.time()

    def stop(self) -> None:
        self.hb.stop()
        self._kill_child()

    # ---------- 子进程 ----------
    def start_child(self) -> None:
        import subprocess
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--watchdog-child", str(self.hb.path),
                   str(self.stale)]
        else:
            cmd = [sys.executable, os.path.abspath(sys.argv[0]),
                   "--watchdog-child", str(self.hb.path), str(self.stale)]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(
                cmd, creationflags=flags,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True,
            )
            log.log("watchdog.spawn", f"已启动看门狗子进程 pid={self._proc.pid}")
        except Exception as exc:
            log.log("watchdog.error", f"启动看门狗失败: {exc!r}")

    def check_child(self) -> None:
        """看门狗被杀时补拉一个。"""
        if self._proc is None:
            return
        if self._proc.poll() is not None:
            log.log("watchdog.warn", "看门狗子进程已退出，重新启动")
            self.start_child()

    def _kill_child(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None
