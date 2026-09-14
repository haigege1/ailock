"""简单的滚动日志记录器。"""

import threading
from datetime import datetime
from pathlib import Path

from .config import app_dir

MAX_BYTES = 256 * 1024


class Logger:
    def __init__(self, path: Path | None = None, name: str = "ailock.log"):
        self.path = Path(path) if path else app_dir() / name
        self._lock = threading.Lock()

    def log(self, event: str, detail: str = "") -> None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {event}"
        if detail:
            line += f"  |  {detail}"
        line += "\n"
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
                self._trim()
            except Exception:
                pass

    def _trim(self) -> None:
        """超过上限时保留后一半，避免日志无限增长。"""
        try:
            if self.path.stat().st_size <= MAX_BYTES:
                return
            text = self.path.read_text(encoding="utf-8", errors="ignore")
            keep = text[len(text) // 2:]
            self.path.write_text(keep, encoding="utf-8")
        except Exception:
            pass

    def tail(self, n: int = 200) -> str:
        try:
            lines = self.path.read_text(encoding="utf-8", errors="ignore").splitlines()
            return "\n".join(lines[-n:])
        except Exception:
            return ""


log = Logger()
