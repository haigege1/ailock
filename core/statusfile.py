"""AI 任务状态联动：状态文件协议与轮询监控。

协议（status.json）—— 任何语言、任何 AI 工具都能写：

    {
      "task":     "模型微调 v3",        // 任务名，可选
      "state":    "running",           // running | done | failed | idle | waiting
      "progress": 0.24,                // 0~1，可选；不填则不显示进度条
      "lines":    ["epoch 12/50", "预计剩余 23 分钟"],   // 自定义文本行
      "updated":  1756789012.3,        // 时间戳，不填则用文件 mtime
      "watch_pid": 12345               // 可选：该进程消失即视为任务结束
    }

写入必须是原子的（临时文件 + os.replace），否则轮询端会读到半截 JSON。
"""

import json
import os
import tempfile
import threading
import time
from pathlib import Path

from .config import default_status_path
from .logger import log
from .winapi import is_process_running

STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_IDLE = "idle"
STATE_WAITING = "waiting"    # AI 在等人授权（PermissionRequest）

VALID_STATES = (STATE_RUNNING, STATE_DONE, STATE_FAILED, STATE_IDLE,
                STATE_WAITING)


class StatusFile:
    def __init__(self, path: str | None = None, extra: dict | None = None):
        self.path = Path(path) if path else Path(default_status_path())
        # 子类可注入固定字段（如 sid），随同一次原子写落盘
        self.extra: dict = extra or {}

    def _dump(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def write(self, state: str, task: str = "", lines=None,
              progress=None, watch_pid: int = 0, merge: bool = False) -> dict:
        """原子写入状态文件。merge=True 时保留原有未指定的字段。"""
        data = {}
        if merge:
            data = self.read() or {}
        data.update({
            "task": task if task is not None else data.get("task", ""),
            "state": state if state in VALID_STATES else STATE_IDLE,
            "lines": list(lines) if lines is not None else data.get("lines", []),
            "progress": progress,
            "updated": time.time(),
        })
        if watch_pid:
            data["watch_pid"] = int(watch_pid)
        elif not merge:
            data.pop("watch_pid", None)
        if self.extra:
            data.update(self.extra)

        self._dump(data)
        return data

    def read(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception:
            return None
        if not isinstance(data, dict):
            return None

        try:
            ts = float(data.get("updated") or 0)
        except (TypeError, ValueError):
            ts = 0
        if ts <= 0:
            try:
                ts = self.path.stat().st_mtime
            except OSError:
                ts = 0
        data["updated"] = ts

        try:
            p = data.get("progress")
            data["progress"] = None if p is None else max(0.0, min(1.0, float(p)))
        except (TypeError, ValueError):
            data["progress"] = None

        lines = data.get("lines")
        data["lines"] = [str(x) for x in lines][:8] if isinstance(lines, list) else []

        if str(data.get("state")) not in VALID_STATES:
            data["state"] = STATE_IDLE
        return data

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class StatusMonitor(threading.Thread):
    """轮询状态文件，把「状态跃迁」而不是「状态值」抛给上层。

    上层只关心三件事：任务完成了、任务失败了、任务失联了。
    持续 running 不应反复触发动作。
    """

    def __init__(self, path: str, poll_seconds: float, stale_minutes: float,
                 on_event, on_snapshot=None):
        super().__init__(daemon=True, name="StatusMonitor")
        self.sf = StatusFile(path)
        self.poll = max(0.5, float(poll_seconds or 2))
        self.stale_minutes = float(stale_minutes or 0)
        self.on_event = on_event
        self.on_snapshot = on_snapshot
        self._stop = threading.Event()
        self._last_state = None
        self._last_updated = 0.0
        self._last_watch_pid = 0
        self._stale_reported = False
        self._primed = False

    def _emit(self, kind: str, data: dict):
        try:
            self.on_event(kind, data)
        except Exception as exc:
            log.log("status.error", f"回调异常: {exc!r}")

    def run(self) -> None:
        log.log("status.start", f"监控 {self.sf.path}")
        while not self._stop.wait(self.poll):
            data = self.sf.read()
            if data is None:
                continue

            if self.on_snapshot:
                try:
                    self.on_snapshot(data)
                except Exception:
                    pass

            state = data["state"]
            updated = data["updated"]

            # 监控的进程没了 -> 视为结束（无法区分成功失败，按 done 处理）
            pid = int(data.get("watch_pid") or 0)
            if pid and state == STATE_RUNNING and not is_process_running(pid):
                log.log("status.pid_gone", f"被监控进程 {pid} 已退出")
                data = dict(data)
                data["state"] = STATE_DONE
                data["lines"] = list(data.get("lines") or []) + [f"进程 {pid} 已结束"]
                state = STATE_DONE
            if pid:
                self._last_watch_pid = pid

            # 首次读到的既有状态只作基线，不触发动作
            # （否则残留的旧状态文件会让每次启动都误报一次完成/失败）
            if not self._primed:
                self._primed = True
                self._last_state = state
                self._last_updated = updated
                self._stale_reported = state != STATE_RUNNING
                continue

            # 状态跃迁
            if state != self._last_state:
                previous = self._last_state
                self._last_state = state
                if state == STATE_DONE:
                    log.log("status.done", str(data.get("task") or ""))
                    self._emit("done", data)
                elif state == STATE_FAILED:
                    log.log("status.failed", str(data.get("task") or ""))
                    self._emit("failed", data)
                elif previous in (STATE_DONE, STATE_FAILED) and state == STATE_RUNNING:
                    self._emit("restarted", data)

            # 失联判定
            if self.stale_minutes > 0 and state == STATE_RUNNING:
                age_min = (time.time() - updated) / 60.0
                if age_min >= self.stale_minutes and not self._stale_reported:
                    self._stale_reported = True
                    log.log("status.stale", f"状态 {age_min:.1f} 分钟未更新")
                    self._emit("stale", data)
                elif age_min < self.stale_minutes:
                    self._stale_reported = False

    def stop(self) -> None:
        self._stop.set()
