"""多会话状态目录：status.d/<sid>.json 每会话一个状态文件。

协议（与 status.json 单文件一致，外加 sid 字段）—— 任何语言都能写：

    {
      "sid":      "zc-a1b2c3",           // 会话 id（= 文件名去 .json）
      "task":     "重构订单模块",          // 任务名，可选
      "state":    "running",             // running | done | failed | idle
      "progress": 0.24,                  // 0~1，可选
      "lines":    ["你: ...", "AI: ..."],
      "updated":  1756789012.3,
      "watch_pid": 12345                 // 可选：进程消失即视为结束
    }

聚合规则（MultiStatusMonitor）：
  - 每个会话文件独立做状态跃迁 / watch_pid / stale 判定；
  - 动作触发以「最后一个收尾的会话」为准：会话 X 跃迁到 done/failed/stale
    时，若存在 updated 比 X 更新的 running 会话，则抑制本次触发；
  - 只有一个会话时行为与单文件 StatusMonitor 完全一致。
"""

import time
from pathlib import Path

from .config import default_status_path
from .logger import log
from .statusfile import (STATE_DONE, STATE_FAILED, STATE_IDLE,
                         STATE_RUNNING, STATE_WAITING, StatusFile)
from .winapi import is_process_running

STATUS_DIR_NAME = "status.d"

# 会话文件过期清理：收尾（done/failed/idle）后保留时长
FINISHED_TTL_SECONDS = 600


def status_dir() -> Path:
    return Path(default_status_path()).parent / STATUS_DIR_NAME


def sanitize_sid(sid: str) -> str:
    """会话 id 只保留安全字符，防路径穿越；空值回落 default。

    '../../evil' 过滤后只剩 'evil'，不含分隔符与点号，无法构造穿越。
    """
    allowed = set("abcdefghijklmnopqrstuvwxyz"
                  "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    out = "".join(c for c in str(sid or "") if c in allowed)[:64]
    return out or "default"


class SessionStatusFile(StatusFile):
    """带 sid 的会话状态文件（sid 随同一次原子写落盘）。"""

    def __init__(self, sid: str, path: str | None = None):
        sid = sanitize_sid(sid)
        super().__init__(path or str(status_dir() / f"{sid}.json"),
                         extra={"sid": sid})
        self.sid = sid

    def write(self, state: str, task: str = "", lines=None,
              progress=None, watch_pid: int = 0, merge: bool = False) -> dict:
        data = super().write(state, task=task, lines=lines,
                             progress=progress, watch_pid=watch_pid,
                             merge=merge)
        data["sid"] = self.sid
        return data

    def read(self) -> dict | None:
        data = super().read()
        if data is not None:
            data["sid"] = self.sid
        return data


def cleanup_finished(status_dir_path: Path | None = None,
                     ttl: float = FINISHED_TTL_SECONDS) -> None:
    """删除收尾超过 ttl 的会话文件，防目录无限膨胀。"""
    base = Path(status_dir_path) if status_dir_path else status_dir()
    try:
        files = list(base.glob("*.json"))
    except OSError:
        return
    now = time.time()
    for f in files:
        sf = StatusFile(str(f))
        data = sf.read()
        if data is None:
            continue
        if data.get("state") in (STATE_DONE, STATE_FAILED, STATE_IDLE):
            if now - float(data.get("updated") or 0) > ttl:
                try:
                    f.unlink()
                    log.log("status.cleanup", f"清理过期会话文件 {f.name}")
                except OSError:
                    pass


def normalize_sessions(data) -> list:
    """把监控推上来的数据统一成 list[dict]。

    兼容两种来源：
      - 多会话快照：list[dict]（MultiStatusMonitor.on_snapshot）
      - 旧单会话快照 / selftest 手工推送：单个 dict
    """
    if not data:
        return []
    if isinstance(data, dict):
        data = [data]
    out = []
    for d in data:
        if isinstance(d, dict) and d:
            out.append(d)
    return out


def pick_primary(sessions) -> dict | None:
    """从会话列表里挑「主会话」：优先最新在跑的，否则最新收尾的。

    旧界面（tk / web）只画一张卡，用它保持「显示最新会话」的语义；
    Qt 版用它填底部状态栏。
    """
    items = normalize_sessions(sessions)
    if not items:
        return None
    active = [d for d in items
              if str(d.get("state") or "idle").lower()
              in (STATE_RUNNING, STATE_WAITING)]
    pool = active or items

    def _updated(d: dict) -> float:
        try:
            return float(d.get("updated") or 0)
        except (TypeError, ValueError):
            return 0.0

    # waiting（等人授权）需要人动手，同权重下排最前
    def _key(d: dict) -> tuple:
        state = str(d.get("state") or "idle").lower()
        return (1 if state == STATE_WAITING else 0, _updated(d))

    return max(pool, key=_key)


class _SessionTracker:
    """单个会话的跃迁 / watch_pid / stale 状态机（线程内使用，非线程安全）。"""

    def __init__(self, sid: str):
        self.sid = sid
        self.last_state: str | None = None
        self.stale_reported = False
        self.last_pid = 0
        self.primed = False

    def check(self, data: dict, stale_minutes: float) -> tuple[str | None, dict]:
        """返回 (事件 kind | None, data)。事件: done/failed/restarted/stale。"""
        state = data["state"]
        updated = data["updated"]
        kind = None

        pid = int(data.get("watch_pid") or 0)
        if pid and state == STATE_RUNNING and not is_process_running(pid):
            data = dict(data)
            data["state"] = STATE_DONE
            data["lines"] = list(data.get("lines") or []) + [f"进程 {pid} 已结束"]
            state = STATE_DONE
        if pid:
            self.last_pid = pid

        if not self.primed:
            self.primed = True
            self.last_state = state
            self.stale_reported = state != STATE_RUNNING
            return None, data

        if state != self.last_state:
            previous = self.last_state
            self.last_state = state
            if state == STATE_DONE:
                kind = "done"
            elif state == STATE_FAILED:
                kind = "failed"
            elif previous in (STATE_DONE, STATE_FAILED) and state == STATE_RUNNING:
                kind = "restarted"

        if stale_minutes > 0 and state == STATE_RUNNING:
            age_min = (time.time() - updated) / 60.0
            if age_min >= stale_minutes and not self.stale_reported:
                self.stale_reported = True
                kind = kind or "stale"
            elif age_min < stale_minutes:
                self.stale_reported = False

        return kind, data


class MultiStatusMonitor:
    """扫描 status.d 目录 + 主 status.json，聚合出全局事件。

    与 StatusMonitor 相同的对外接口（start/stop/on_event/on_snapshot），
    上层（app.py）无需感知单/多会话差别。
    """

    def __init__(self, path: str, poll_seconds: float, stale_minutes: float,
                 on_event, on_snapshot=None):
        import threading
        self.main_path = Path(path)
        self.dir_path = self.main_path.parent / STATUS_DIR_NAME
        self.poll = max(0.5, float(poll_seconds or 2))
        self.stale_minutes = float(stale_minutes or 0)
        self.on_event = on_event
        self.on_snapshot = on_snapshot
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._trackers: dict[str, _SessionTracker] = {}
        self._last_cleanup = 0.0

    # ---- 兼容 StatusMonitor 的属性 ----
    @property
    def sf(self):
        return StatusFile(str(self.main_path))

    def start(self) -> None:
        import threading
        self._stop.clear()          # 允许 stop 后重新 start
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="MultiStatusMonitor")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _sessions(self) -> list[StatusFile]:
        """主文件（视作 default 会话）+ 目录下所有会话文件。"""
        out = [StatusFile(str(self.main_path))]
        try:
            for f in sorted(self.dir_path.glob("*.json")):
                out.append(StatusFile(str(f)))
        except OSError:
            pass
        return out

    def _other_running(self, sid: str) -> bool:
        """是否还有其他会话处于 running（不看时间先后，有就算）。"""
        for sf in self._sessions():
            data = sf.read()
            if data is None:
                continue
            other_sid = str(data.get("sid") or sf.path.stem)
            if other_sid == sid:
                continue
            if data.get("state") == STATE_RUNNING:
                return True
        return False

    def _emit_checked(self, kind: str, data: dict) -> bool:
        """触发全局事件；返回 False 表示被其他 running 会话抑制。

        抑制规则只针对收尾类事件（done/failed/stale）——「以最后一个
        会话为准」。restarted 表示用户回来继续干活，任何会话都要立即
        反映，不抑制。
        """
        sid = str(data.get("sid") or "default")
        if kind in ("done", "failed", "stale") and self._other_running(sid):
            log.log("status.suppress",
                    f"会话 {sid} {kind} 被抑制：存在仍在运行的其他会话")
            # 抑制时重置失联标记：等最后会话收尾后还能补发 stale
            tr = self._trackers.get(sid)
            if tr is not None and kind == "stale":
                tr.stale_reported = False
            return False
        log.log(f"status.{kind}", f"{sid} | {data.get('task') or ''}")
        try:
            self.on_event(kind, data)
        except Exception as exc:
            log.log("status.error", f"回调异常: {exc!r}")
        return True

    def _run(self) -> None:
        log.log("status.start", f"监控 {self.main_path} + {self.dir_path}")
        while not self._stop.wait(self.poll):
            snapshots = []
            events = []
            for sf in self._sessions():
                data = sf.read()
                if data is None:
                    continue
                sid = str(data.get("sid") or sf.path.stem)
                tr = self._trackers.get(sid)
                if tr is None:
                    tr = self._trackers[sid] = _SessionTracker(sid)
                kind, data = tr.check(data, self.stale_minutes)
                snapshots.append(data)
                if kind:
                    events.append((kind, data, sid))

            if self.on_snapshot:
                try:
                    self.on_snapshot(snapshots)
                except Exception:
                    pass

            for kind, data, _sid in events:
                self._emit_checked(kind, data)

            # 每 10 分钟清理一次过期会话文件
            if time.time() - self._last_cleanup > 600:
                self._last_cleanup = time.time()
                try:
                    cleanup_finished(self.dir_path)
                except Exception:
                    pass
