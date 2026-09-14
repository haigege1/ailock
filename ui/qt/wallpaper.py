"""AiLock Qt 版 · 壁纸服务

- 按配置枚举壁纸（纯色 / 单图同目录 / 文件夹轮播）
- 在后台线程做缩放 + 高斯模糊，主线程只负责贴图，避免 2K 大图卡 UI
- 结果按 (路径, 目标尺寸, 模糊半径) 缓存，切换壁纸不重复解码
"""

import os
import random
import threading

from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap

from core.logger import log

_IMG_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")


def list_images(folder: str, limit: int = 40) -> list:
    if not folder or not os.path.isdir(folder):
        return []
    try:
        out = []
        for name in sorted(os.listdir(folder)):
            if name.lower().endswith(_IMG_EXT):
                out.append(os.path.join(folder, name))
                if len(out) >= limit:
                    break
        return out
    except Exception:
        return []


def collect_wallpapers(cfg) -> tuple:
    """返回 (文件列表, 初始索引)。纯色模式返回 ([], 0)。"""
    mode = cfg.get("background.mode", "color")
    if mode == "color":
        return [], 0
    path = cfg.get("background.path", "") or ""
    if mode == "image":
        folder = os.path.dirname(path) if path else ""
        files = list_images(folder)
        idx = 0
        if path and os.path.isfile(path):
            if path not in files:
                files = [path] + files
            idx = files.index(path) if path in files else 0
        return files, idx
    if mode == "folder":
        files = list_images(path)
        # 轮播起点随机：固定从第一张（最旧）开始，观感上「永远是那几张」
        return files, random.randrange(len(files)) if files else 0
    if mode == "online":
        # Bing 在线壁纸缓存目录（多市场增量下载，池子持续变大）；
        # 首次使用时可能还没下载完，返回空列表，等下载线程完成后由
        # LockScreen.refresh_files() 重新收集。起点同样随机。
        from core.config import app_dir
        files = list_images(str(app_dir() / "wallpapers"), limit=200)
        return files, random.randrange(len(files)) if files else 0
    return [], 0


def _render(path: str, size: QSize, blur: int, dim: float) -> QPixmap:
    """解码 → cover 裁剪 → 模糊 → 暗化，返回 QPixmap。"""
    from PIL import Image, ImageFilter

    tw, th = max(1, size.width()), max(1, size.height())
    with Image.open(path) as im:
        im = im.convert("RGB")
        iw, ih = im.size
        # cover 裁剪（略微放大，给 Ken Burns 平移留余量）
        scale = max(tw / iw, th / ih) * 1.06
        nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
        im = im.resize((nw, nh), Image.LANCZOS)
        left, top = (nw - tw) // 2, (nh - th) // 2
        im = im.crop((left, top, left + tw, top + th))
        if blur and blur > 0:
            im = im.filter(ImageFilter.GaussianBlur(radius=float(blur)))

        data = im.tobytes("raw", "RGB")
        qimg = QImage(data, im.width, im.height, im.width * 3,
                      QImage.Format_RGB888)

    pm = QPixmap.fromImage(qimg)
    if dim and dim > 0:
        out = QPixmap(pm.size())
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.drawPixmap(0, 0, pm)
        p.setBrush(QColor(0, 0, 0, int(255 * float(dim))))
        p.setPen(Qt.NoPen)
        p.drawRect(pm.rect())
        p.end()
        return out
    return pm


class WallpaperService(QObject):
    """后台加载壁纸，完成后用信号把 QPixmap 送回主线程。"""

    loaded = Signal(int, QPixmap, str)   # index, pixmap, name

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cache = {}
        self._lock = threading.Lock()
        self._seq = 0
        self._files = []
        self._index = 0

    def set_files(self, files: list, index: int = 0):
        self._files = list(files)
        self._index = index if 0 <= index < len(self._files) else 0

    @property
    def count(self) -> int:
        return len(self._files)

    @property
    def index(self) -> int:
        return self._index

    def current_name(self) -> str:
        if not self._files:
            return "纯色"
        return os.path.basename(self._files[self._index])

    def step(self, direction: int) -> int:
        if len(self._files) <= 1:
            return self._index
        self._index = (self._index + int(direction or 1)) % len(self._files)
        return self._index

    def request(self, index: int, size: QSize, blur: int, dim: float):
        """请求某张壁纸；命中缓存则同步发射，否则后台线程加载。"""
        if not self._files or not (0 <= index < len(self._files)):
            return
        path = self._files[index]
        key = (path, size.width(), size.height(), blur, round(dim, 2))
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None:
            self.loaded.emit(index, hit, os.path.basename(path))
            return

        self._seq += 1
        seq = self._seq

        def work():
            try:
                pm = _render(path, size, blur, dim)
            except Exception as exc:
                log.log("wall.error", "%s: %r" % (path, exc))
                return
            with self._lock:
                self._cache[key] = pm
                # 控制缓存规模，壁纸轮播不至于吃满内存
                if len(self._cache) > 12:
                    try:
                        self._cache.pop(next(iter(self._cache)))
                    except Exception:
                        pass
            if seq == self._seq:
                self.loaded.emit(index, pm, os.path.basename(path))

        threading.Thread(target=work, daemon=True).start()

    def clear_cache(self):
        with self._lock:
            self._cache.clear()
