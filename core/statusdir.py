"""多会话状态目录：status.d/<sid>.json 每会话一个状态文件。

协议（与 status.json 单文件一致，外加 sid 字段）—— 任何语言都能写：

    {
      "sid":      "zc-a1b2c3",           // 会话 id（= 文件名去 .json）
      "task":     "重构订单模块",          // 任务名，可选
      "state":    "running",             // running | done | failed | idle
      "progress": 0.24,                  // 0~1，可选
      "lines":    ["你: ...", "AI: ..."],
      "updated":  1756789012.3,
      "turn_end_at": 1756789012.3,       // 可选：本轮 Stop 的时间戳，
                                         //   见下方「收尾确认窗口」
      "watch_pid": 12345                 // 可选：进程消失即视为结束
    }

聚合规则（MultiStatusMonitor）：
  - 每个会话文件独立做状态跃迁 / watch_pid / stale 判定；
  - 动作触发以「最后一个收尾的会话」为准：会话 X 跃迁到 done/failed/stale
    时，若存在 updated 比 X 更新的 running 会话，则抑制本次触发；
  - 只有一个会话时行为与单文件 StatusMonitor 完全一致。

收尾确认窗口（读取侧兜底）：
  Stop 事件每轮对话结束都会触发，所以钩子不能直接写 done（多轮对话会每轮
  误触发一次动作）。原设计是钩子另起一个后台进程等 DONE_CONFIRM_SECONDS 秒
  确认无新事件再翻 done —— 但宿主（WorkBuddy / ZCode）用 Job Object 收进程树，
  钩子一退出这个子进程就被连坐杀掉，文件便永远停在 running：锁屏右侧的会话卡
  从此再也撤不下来（2026-09-24 实机确认，日志里 status.done 一条都没有）。
  因此改为读取侧按同一规则兜底：Stop 时钩子额外写 turn_end_at，监控端读到
  「turn_end_at 存在 且 距 updated 已超过确认窗口」即视作 done。钩子里的
  后台进程保留为快路径（宿主不杀子进程时仍由它落盘），两条路殊途同归。

过期策略（is_expired，UI 与会话文件清理共用）：
  - waiting：不等人不动，永不过期；
  - 带 watch_pid：有进程在看守，永不过期；
  - running：超过 RUNNING_IDLE_TTL_SECONDS 无任何更新 → 视为已死，撤卡+清理；
  - done / failed / idle：收尾超过 FINISHED_TTL_SECONDS → 撤卡+清理。
"""

import os
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

# running 但长时间无任何更新 → 视为已死（撤卡 + 清理）。与 status.stale_minutes
# 的默认 15 分钟失联判定配合：失联通知 15 分钟，再等 15 分钟仍无动静才撤卡。
RUNNING_IDLE_TTL_SECONDS = 1800

# Stop 后的「收尾确认窗口」：窗口内有新事件说明还在多轮对话，不算结束。
# 与钩子里的 AILOCK_DONE_DELAY 保持一致（钩子不写环境变量时默认 12）。
DONE_CONFIRM_SECONDS = float(os.environ.get("AILOCK_DONE_DELAY", "12") or "0")

# 原子写的临时文件被进程中途杀死时会残留（<状态文件>.xxxx.tmp / tmpXXXX.tmp），
# 超过这个时长即视为孤儿，随清理一起扫掉。
TMP_ORPHAN_TTL_SECONDS = 3600


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


def settle(data: dict, now: float | None = None) -> dict:
    """读取侧兜底：Stop 已过确认窗口的会话补记为 done。

    钩子 Stop 时写的是 `running` + `turn_end_at`（外加一个后台确认进程）。
    后台进程被宿主连坐杀掉时文件会永远停在 running —— 这里按同一规则在读取
    侧确认：`turn_end_at` 之后确认窗口内没有任何新事件（`updated` 没再前进），
    就说明这一轮真的结束了。

    返回新 dict（不改原对象）；`updated` 保持 Stop 的时刻，这样「已完成」卡片
    的 10 分钟展示窗口是从本轮结束算起的。
    """
    if not isinstance(data, dict):
        return data
    if str(data.get("state") or "").lower() != STATE_RUNNING:
        return data
    try:
        end_at = float(data.get("turn_end_at") or 0)
    except (TypeError, ValueError):
        end_at = 0.0
    if end_at <= 0:
        return data
    now = time.time() if now is None else now
    try:
        updated = float(data.get("updated") or 0)
    except (TypeError, ValueError):
        updated = 0.0
    if now - max(updated, end_at) < DONE_CONFIRM_SECONDS:
        return data                      # 确认窗口内，可能还有新事件，再等等
    out = dict(data)
    out["state"] = STATE_DONE
    out["progress"] = 1
    # 与钩子 write_done_now 的收尾行保持一致
    out["lines"] = [ln for ln in (data.get("lines") or [])
                    if not str(ln).startswith("✓")] + ["✓ 已完成"]
    return out


def is_expired(data: dict, now: float | None = None) -> bool:
    """该会话是否已不该继续展示（/不该继续留在磁盘上）。

    传入的数据应先用 settle() 处理过，否则 Stop 后卡住的会话会被误判成
    还活着。
    """
    if not isinstance(data, dict):
        return True
    state = str(data.get("state") or STATE_IDLE).lower()
    if state == STATE_WAITING:
        return False                     # 在等人授权，撤掉就没人知道要动手了
    if int(data.get("watch_pid") or 0):
        return False                     # 有被监控进程，说明确实还在跑
    now = time.time() if now is None else now
    try:
        updated = float(data.get("updated") or 0)
    except (TypeError, ValueError):
        updated = 0.0
    ttl = (RUNNING_IDLE_TTL_SECONDS if state == STATE_RUNNING
           else FINISHED_TTL_SECONDS)
    return (now - updated) > ttl


def cleanup_finished(status_dir_path: Path | None = None,
                     ttl: float = FINISHED_TTL_SECONDS,
                     now: float | None = None) -> None:
    """删除过期会话文件，防目录无限膨胀。

    过期 = 收尾超 ttl / running 长时间无更新（is_expired）；顺手扫掉原子写
    残留的孤儿 .tmp（进程被半路杀掉留下的，见 TMP_ORPHAN_TTL_SECONDS）。
    """
    base = Path(status_dir_path) if status_dir_path else status_dir()
    now = time.time() if now is None else now
    try:
        files = list(base.glob("*.json"))
    except OSError:
        return
    for f in files:
        sf = StatusFile(str(f))
        data = sf.read()
        if data is None:
            continue
        state = str(data.get("state") or STATE_IDLE).lower()
        if state in (STATE_DONE, STATE_FAILED, STATE_IDLE):
            expired = (now - float(data.get("updated") or 0)) > ttl
        else:
            # running / waiting：Stop 卡住的先按确认窗口补成 done 再看是否过期
            expired = is_expired(settle(data, now), now)
        if expired:
            try:
                f.unlink()
                log.log("status.cleanup", f"清理过期会话文件 {f.name}")
            except OSError:
                pass
    # 孤儿临时文件
    try:
        tmps = [p for p in base.glob("*.tmp")
                if now - p.stat().st_mtime > TMP_ORPHAN_TTL_SECONDS]
    except OSError:
        return
    for p in tmps:
        try:
            p.unlink()
            log.log("status.cleanup", f"清理残留临时文件 {p.name}")
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


def live_sessions(sessions, now: float | None = None) -> list:
    """快照里「还值得展示」的会话：先 settle 再按 is_expired 过滤。

    UI（会话卡 + 状态栏 + 托盘）统一走这个，别再各自算一遍 —— 之前状态栏
    按未过滤的原始列表数数，会出现「撤到只剩 1 张卡，状态栏还说等 5 个会话」。
    """
    now = time.time() if now is None else now
    out = []
    for d in normalize_sessions(sessions):
        s = settle(d, now)
        if not is_expired(s, now):
            out.append(s)
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
        self._suppress_log: dict[str, float] = {}   # sid -> 上次记抑制日志的时刻

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

    def _other_running(self, sid: str, now: float | None = None) -> bool:
        """是否还有其他会话处于 running（不看时间先后，有就算）。

        跳过已经「读出来就是死」的会话：Stop 卡住的按确认窗口折算成 done，
        长时间没动静的按 is_expired 排除 —— 否则一堆僵尸会话会把真正收尾
        的那个会话的动作永久抑制掉（2026-09-24 实机就是这状态）。
        """
        now = time.time() if now is None else now
        for sf in self._sessions():
            data = sf.read()
            if data is None:
                continue
            other_sid = str(data.get("sid") or sf.path.stem)
            if other_sid == sid:
                continue
            data = settle(data, now)
            if (data.get("state") == STATE_RUNNING
                    and not is_expired(data, now)):
                return True
        return False

    def _emit_checked(self, kind: str, data: dict, sid: str = "") -> bool:
        """触发全局事件；返回 False 表示被其他 running 会话抑制。

        抑制规则只针对收尾类事件（done/failed/stale）——「以最后一个
        会话为准」。restarted 表示用户回来继续干活，任何会话都要立即
        反映，不抑制。

        sid 由调用方传入（_run 里按「文件里的 sid，缺省用文件名」算好的）。
        这里别再自己从 data 里猜：钩子写的会话文件没有 sid 字段，猜出来
        一律是 "default"，会导致 tracker 查不到、stale 的抑制标记重置失效。
        """
        sid = sid or str(data.get("sid") or "default")
        if kind in ("done", "failed", "stale") and self._other_running(sid):
            # 抑制时重置失联标记：等最后会话收尾后还能补发 stale
            tr = self._trackers.get(sid)
            if tr is not None and kind == "stale":
                tr.stale_reported = False
            # 僵尸会话会让这条日志每次轮询都刷一遍（早前一次启动刷了 19 行），
            # 按会话节流，别把 ailock.log 淹了
            if self._should_log_suppress(sid):
                log.log("status.suppress",
                        f"会话 {sid} {kind} 被抑制：存在仍在运行的其他会话")
            return False
        log.log(f"status.{kind}", f"{sid} | {data.get('task') or ''}")
        try:
            self.on_event(kind, data)
        except Exception as exc:
            log.log("status.error", f"回调异常: {exc!r}")
        return True

    def _should_log_suppress(self, sid: str, interval: float = 60.0) -> bool:
        now = time.time()
        if now - self._suppress_log.get(sid, 0.0) < interval:
            return False
        self._suppress_log[sid] = now
        return True

    def _run(self) -> None:
        log.log("status.start", f"监控 {self.main_path} + {self.dir_path}")
        while not self._stop.wait(self.poll):
            snapshots = []
            events = []
            now = time.time()
            for sf in self._sessions():
                data = sf.read()
                if data is None:
                    continue
                sid = str(data.get("sid") or sf.path.stem)
                tr = self._trackers.get(sid)
                if tr is None:
                    tr = self._trackers[sid] = _SessionTracker(sid)
                # 先做收尾兜底（Stop 卡住的补成 done），再判跃迁
                data = settle(data, now)
                kind, data = tr.check(data, self.stale_minutes)
                # 过期的会话不再进快照 → 右侧卡片/托盘跟着撤下来；
                # 但仍要走一遍跃迁判定，免得它的收尾事件被漏掉
                if not is_expired(data, now):
                    snapshots.append(data)
                if kind:
                    events.append((kind, data, sid))

            if self.on_snapshot:
                try:
                    self.on_snapshot(snapshots)
                except Exception:
                    pass

            for kind, data, sid in events:
                self._emit_checked(kind, data, sid)

            # 每 10 分钟清理一次过期会话文件
            if time.time() - self._last_cleanup > 600:
                self._last_cleanup = time.time()
                try:
                    cleanup_finished(self.dir_path)
                except Exception:
                    pass
