"""应用配置与运行时状态。

配置明文段落存在 config.json；口令相关的一律走 security.py 的 DPAPI 保险箱，
绝不明文落盘。
"""

import json
import os
import threading
from pathlib import Path

APP_NAME = "AiLock"

DEFAULT_CONFIG = {
    "version": 1,
    # 背景
    "background": {
        "mode": "color",            # color | image | folder | online
        "path": "",                 # 图片文件或文件夹
        "fit": "cover",             # cover | contain | stretch | center | tile
        "blur": 0,                  # 高斯模糊半径(px)
        "dim": 0.45,                # 暗化遮罩 0~1
        "color": "#0B1220",         # mode=color 时的底色
        "slideshow_seconds": 30,    # 文件夹/在线轮播间隔
        # 切换动效：none | fade | slide | zoom | wipe
        "transition": "fade",
        "kenburns": True,           # 壁纸缓慢平移动效（仅图片/文件夹模式）
        "online_days": 8,           # 在线模式拉取最近 N 天 Bing 壁纸
    },
    # 界面动效
    "ui": {
        "animations": True,         # 入场滑入、密码错误摇晃等
    },
    # 显示与电源
    "display": {
        "keep_on": True,            # True=屏幕常亮；False=允许息屏但主机不睡
        "off_after_minutes": 0,     # 锁定后键鼠无操作 N 分钟自动息屏，0=关闭
    },
    # 锁定行为
    "lock": {
        "min_lock_minutes": 0,          # 最短锁定时长，防手贱立刻解开
        "auto_lock_idle_minutes": 0,    # 空闲自动上锁，0=关闭
        "lock_on_start": False,         # 开机自启后立即锁定
        "show_clock": True,
    },
    "message": {
        "text": "",                     # 锁屏提示语
        "color": "",                    # 提示语颜色（#hex），空=跟随主题
    },
    # AI 任务状态联动
    "status": {
        "enabled": True,
        "path": "",                     # 默认 %APPDATA%/AiLock/status.json
        "poll_seconds": 2,
        # 任务完成时的动作：none | shutdown | hibernate | sleep | unlock | notify
        "on_done_action": "none",
        "on_done_countdown": 90,        # 动作执行前的可取消倒计时(秒)
        "on_failed_action": "none",
        "stale_minutes": 0,             # 状态多久没更新视为失联，0=不判断
        "on_stale_action": "none",
    },
    # 安全
    "security": {
        "max_attempts": 5,
        "cooldown_seconds": 30,
    },
    # 网络
    "net": {
        "watch": True,             # 网络看门狗：断网时托盘提醒
        "interval_seconds": 30,    # 探测间隔
        "fail_threshold": 3,       # 连续失败 N 次判定断网
        "host": "",                # 指定探测目标；空=自动（网关+公共DNS）
    },
    # 杂项
    "hotkey": "ctrl+alt+l",
    "autostart": False,
}


def app_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    p = Path(base) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def default_status_path() -> str:
    return str(app_dir() / "status.json")


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else app_dir() / "config.json"
        self._lock = threading.RLock()
        self._data = json.loads(json.dumps(DEFAULT_CONFIG))
        self.load()

    # ---------- 读写 ----------
    def load(self) -> None:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = _deep_merge(DEFAULT_CONFIG, raw)
            except FileNotFoundError:
                pass
            except Exception:
                # 配置损坏时备份一份，避免直接丢失用户设置
                try:
                    self.path.replace(self.path.with_suffix(".corrupt.json"))
                except Exception:
                    pass

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)

    # ---------- 点号路径访问 ----------
    def get(self, dotted: str, default=None):
        node = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value, autosave: bool = True) -> None:
        parts = dotted.split(".")
        with self._lock:
            node = self._data
            for part in parts[:-1]:
                node = node.setdefault(part, {})
                if not isinstance(node, dict):
                    node = {}
            node[parts[-1]] = value
            if autosave:
                self.save()

    @property
    def data(self) -> dict:
        return self._data


# ---------- 运行时状态（失败次数 / 冷却） ----------

class RuntimeState:
    """记录失败尝试次数与冷却截止时间，明文但与配置分文件存放。"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else app_dir() / "state.json"
        self._lock = threading.RLock()
        self.failed_attempts = 0
        self.cooldown_until = 0.0
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.failed_attempts = int(raw.get("failed_attempts", 0) or 0)
            self.cooldown_until = float(raw.get("cooldown_until", 0) or 0)
        except Exception:
            pass

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps({
                "failed_attempts": self.failed_attempts,
                "cooldown_until": self.cooldown_until,
            }), encoding="utf-8")
        except Exception:
            pass

    def reset(self) -> None:
        with self._lock:
            self.failed_attempts = 0
            self.cooldown_until = 0.0
            self._save()


# ---------- 心跳（供看门狗判断主进程是否还活着） ----------

class Heartbeat:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else app_dir() / "heartbeat.json"
        self._lock = threading.RLock()

    def beat(self, pid: int, locked: bool) -> None:
        with self._lock:
            try:
                self.path.write_text(json.dumps({
                    "pid": int(pid),
                    "ts": __import__("time").time(),
                    "locked": bool(locked),
                    "alive": True,
                }), encoding="utf-8")
            except Exception:
                pass

    def stop(self) -> None:
        """干净退出时写入停止标记，看门狗见此标记即自行退出，不会误拉起。"""
        with self._lock:
            try:
                self.path.write_text(json.dumps({
                    "pid": 0, "ts": 0, "locked": False, "alive": False,
                }), encoding="utf-8")
            except Exception:
                pass

    def read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}


# ---------- 单实例锁 + 进程间「请求锁定」 ----------

class LockRequest:
    """第二个进程通过写一个时间戳文件，请求已在运行的实例执行锁定。"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else app_dir() / "lock_request"
        self._last_seen = 0.0

    def request(self) -> None:
        import time
        try:
            self.path.write_text(str(time.time()), encoding="utf-8")
        except Exception:
            pass

    def consume(self) -> bool:
        """返回 True 表示有新请求待处理。"""
        try:
            ts = float(self.path.read_text(encoding="utf-8").strip() or 0)
        except Exception:
            return False
        if ts > self._last_seen:
            self._last_seen = ts
            return True
        return False
