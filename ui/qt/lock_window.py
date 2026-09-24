"""AiLock Qt 版 · 锁屏窗口

多屏全覆盖：每个显示器一个无边框置顶窗口，每屏都能输密码解锁。
接口沿用旧 tkinter 版，main.py 无需大改。

视觉：大时钟 + 毛玻璃卡片 + 右侧会话卡堆叠（进度/日志行）+ 底部状态栏，
支持入场滑入、密码错误摇晃、壁纸 Ken Burns 缓移、切换壁纸 toast。

多会话：status.d 下每个会话一个状态文件，快照以 list[dict] 推给
apply_status，右侧竖排一会话一卡（最多 MAX_CARDS 张）。会话卡是绝对
定位的覆盖层，不进中央布局，因此增删卡片不会晃动时钟/密码卡的位置。
"""

import time

from PySide6.QtCore import (QEasingCurve, QParallelAnimationGroup, QPoint,
                            Property, QPropertyAnimation, QRect, QRectF,
                            QSequentialAnimationGroup, Qt, QTimer,
                            Signal)
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QPainter, QPainterPath,
                           QPixmap)
from PySide6.QtWidgets import (QApplication, QFrame, QGraphicsOpacityEffect,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

from core.logger import log
from core.statusdir import live_sessions, normalize_sessions, pick_primary
from . import theme as T
from .wallpaper import WallpaperService, collect_wallpapers
from .widgets import AvatarCircle, MarqueeLabel, StatusDot, ThinProgress

STATE_COLORS = {
    "waiting": "#F5A623",
    "running": "#4C8DFF",
    "done": "#34D399",
    "failed": "#FF6B6B",
    "idle": "#8FA3BF",
}
STATE_LABELS = {"waiting": "等待授权", "running": "运行中",
                "done": "已完成", "failed": "失败", "idle": "空闲"}

# 壁纸切换动效
BG_TRANSITIONS = ("none", "fade", "slide", "zoom", "wipe")


class GlassCard(QFrame):
    """半透明圆角卡片，自绘背景（QSS 的 border-radius 在部分场景有毛边）。"""

    def __init__(self, radius: int = 20, parent=None):
        super().__init__(parent)
        self._radius = radius
        self._bg = QColor(18, 26, 42, 140)
        self._border = QColor(255, 255, 255, 36)
        self.setAttribute(Qt.WA_TranslucentBackground)

    def setBgAlpha(self, a: int):
        self._bg.setAlpha(a)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5),
                            self._radius, self._radius)
        p.setPen(Qt.NoPen)
        p.setBrush(self._bg)
        p.drawPath(path)
        p.setPen(QColor(self._border))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.end()


class WipeOverlay(QWidget):
    """擦除过渡层：新壁纸从左到右扫开覆盖旧壁纸。

    用 Property(frac) 驱动 QPropertyAnimation，paintEvent 按frac 裁剪绘制。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pm = None
        self._scaled = None
        self._frac = 0.0
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def getFrac(self) -> float:
        return self._frac

    def setFrac(self, v: float):
        self._frac = max(0.0, min(1.0, float(v)))
        self.update()

    frac = Property(float, getFrac, setFrac)

    def setPixmap(self, pm: QPixmap | None):
        self._pm = pm
        self._scaled = self._rescale()
        self.update()

    def _rescale(self) -> QPixmap | None:
        if self._pm is None or self._pm.isNull():
            return None
        if self.width() < 2 or self.height() < 2:
            return None
        # cover 铺满窗口，过渡中避免每帧重复缩放
        return self._pm.scaled(self.width(), self.height(),
                               Qt.KeepAspectRatioByExpanding,
                               Qt.SmoothTransformation)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._scaled = self._rescale()

    def paintEvent(self, _e):
        if self._pm is None or self._pm.isNull():
            return
        if self._scaled is None:
            self._scaled = self._rescale()
        pm = self._scaled
        if pm is None or pm.isNull():
            return
        p = QPainter(self)
        w, h = self.width(), self.height()
        x = max(0, (pm.width() - w) // 2)
        y = max(0, (pm.height() - h) // 2)
        cut = int(round(w * self._frac))
        if cut > 0:
            p.drawPixmap(QRect(0, 0, cut, h), pm,
                         QRect(x, y, min(cut, pm.width() - x), h))
        # 扫过前沿加一条高光，动效更有质感
        if 0 < cut < w:
            p.fillRect(cut - 2, 0, 3, h, QColor(255, 255, 255, 70))
        p.end()


class SessionCard(GlassCard):
    """单个 AI 会话卡：状态点 + 任务名 + 状态 + 进度 + 最近日志行。

    宽度会按窗口实际可用空间收缩（见 SessionStack._card_width），这里的
    WIDTH 只是「宽屏下的理想宽度」，不是写死值 —— 写死会在窄屏压住居中的
    密码卡。
    """

    WIDTH = 380          # 宽屏下的理想宽度
    MIN_WIDTH = 240      # 窄屏收缩下限
    # 标题可占宽度 = 卡片宽 - 边距(36) - 状态点(9) - 间距(8×2) - 状态标签(~46)
    TITLE_OVERHEAD = 110
    MAX_LINES = 3        # 最多展示的日志行数
    LINE_CHARS = 76      # 单行截断长度（按加宽后的卡片重新定）

    def __init__(self, parent=None):
        super().__init__(16, parent)
        self._title_raw = "会话"
        self.setFixedWidth(self.WIDTH)
        l = QVBoxLayout(self)
        l.setContentsMargins(18, 14, 18, 14)
        l.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.dot = StatusDot(T.LOCK_ACCENT, 9, self)
        self.title = QLabel("会话")
        self.title.setFont(T.font(13, QFont.Medium))
        self.title.setStyleSheet("color:%s;" % T.LOCK_INK)
        self.state = QLabel("")
        self.state.setFont(T.font(11))
        self.state.setStyleSheet("color:%s;" % T.LOCK_INK_3)
        head.addWidget(self.dot)
        head.addWidget(self.title, 1)
        head.addWidget(self.state)
        l.addLayout(head)

        self.progress = ThinProgress(self, 4)
        l.addWidget(self.progress)

        self.lines = QLabel("")
        self.lines.setFont(T.font(11))
        self.lines.setStyleSheet("color:%s;" % T.LOCK_INK_3)
        self.lines.setWordWrap(True)
        l.addWidget(self.lines)

    def set_width(self, w: int) -> None:
        """窄屏收窄卡片后重排标题（省略号按新宽度重算）。"""
        self.setFixedWidth(w)
        fm = QFontMetrics(self.title.font())
        room = max(60, w - self.TITLE_OVERHEAD)
        self.title.setText(fm.elidedText(self._title_raw, Qt.ElideRight, room))

    def apply(self, data: dict):
        self._title_raw = str(data.get("task") or "").strip() or str(
            data.get("sid") or "会话")
        state = str(data.get("state") or "idle").lower()
        color = STATE_COLORS.get(state, STATE_COLORS["idle"])

        self.set_width(self.width())
        self.title.setToolTip(self._title_raw)
        self.state.setText(STATE_LABELS.get(state, state))
        self.state.setStyleSheet("color:%s;" % color)
        self.dot.setColor(color)
        self.dot.setPulsing(state == "running")

        progress = data.get("progress")
        if progress is not None:
            try:
                pv = float(progress)
                if pv > 1.0:
                    pv = pv / 100.0
                self.progress.setVisible(True)
                self.progress.setValue(pv)
            except (TypeError, ValueError):
                self.progress.setVisible(False)
        else:
            self.progress.setVisible(False)

        lines = data.get("lines") or []
        if lines:
            self.lines.setText("\n".join(
                str(x)[:self.LINE_CHARS] for x in lines[:self.MAX_LINES]))
            self.lines.setVisible(True)
        else:
            self.lines.setVisible(False)


class SessionStack(QWidget):
    """右侧会话卡竖排容器。

    绝对定位的透明覆盖层：铺满整个窗口，内部的卡片按窗口坐标摆放，
    因此增删卡片不会触发中央布局重排（旧 taskCard 的抖动根因）。
    整层鼠标穿透，不影响密码输入与壁纸切换箭头。
    """

    MAX_CARDS = 4
    GAP = 14
    # 右边距 = 箭头宽 44 + 箭头边距 24 + 16 间隙，避免卡片压住切换箭头
    RIGHT_MARGIN = 84
    TOP_MARGIN = 24
    BOTTOM_MARGIN = 78       # 底部状态栏 46 + 32 留白

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._cards = [SessionCard(self) for _ in range(self.MAX_CARDS)]
        self._count = 0
        for c in self._cards:
            c.setVisible(False)
        self.setVisible(False)

    # ---------------------------------------------------------- 数据
    def apply(self, sessions) -> None:
        """按「在跑的优先、其次最新」排序后渲染最多 MAX_CARDS 张卡。

        过期判定统一走 core.statusdir（收尾超 TTL / running 长时间无更新），
        settle 兜底那些 Stop 卡在 running 的会话（后台确认进程被宿主杀掉的
        场景）—— 两者都在 live_sessions 里，UI 只管排版。
        """
        items = live_sessions(sessions)
        now = time.time()
        ranked = []
        for d in items:
            state = str(d.get("state") or "idle").lower()
            try:
                updated = float(d.get("updated") or 0)
            except (TypeError, ValueError):
                updated = 0.0
            # 等待授权排最前（需要人动手），其次在跑的，再按最新
            rank = 0 if state == "waiting" else 1 if state == "running" else 2
            ranked.append((rank, -updated, d))
        ranked.sort(key=lambda t: (t[0], t[1]))
        shown = [t[2] for t in ranked[:self.MAX_CARDS]]

        self._count = len(shown)
        self.setVisible(self._count > 0)
        for i, card in enumerate(self._cards):
            if i < self._count:
                card.apply(shown[i])
                card.setVisible(True)
            else:
                card.setVisible(False)
        self.reposition()

    # ---------------------------------------------------------- 几何
    def _card_width(self, w: int) -> int:
        """窄屏收窄卡片：右缘到「居中密码卡右缘 + 留白」这段可用宽度。

        密码卡固定 360 宽居中，右缘 = w/2 + 180。宽屏（≥1248px）这段
        足够放下 300 宽的卡片，再窄就按比例收缩，下限 MIN_WIDTH。
        """
        avail = (w - self.RIGHT_MARGIN) - (w // 2 + 180 + 24)
        return max(SessionCard.MIN_WIDTH, min(SessionCard.WIDTH, avail))

    def reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None or self._count <= 0:
            return
        w, h = parent.width(), parent.height()
        self.setGeometry(0, 0, w, h)          # 铺满窗口 → 卡片用窗口坐标
        visible = self._cards[:self._count]

        cw = self._card_width(w)
        for card in visible:
            card.set_width(cw)                # 先定宽，sizeHint 才准
        heights = [c.sizeHint().height() for c in visible]
        total = sum(heights) + self.GAP * (len(heights) - 1)
        x = max(12, w - self.RIGHT_MARGIN - cw)
        y = max(self.TOP_MARGIN,
                min((h - total) // 2, h - self.BOTTOM_MARGIN - total))
        for card, ch in zip(visible, heights):
            card.setGeometry(x, y, cw, ch)
            y += ch + self.GAP


class LockWindow(QWidget):
    """单个显示器的锁屏窗口。"""

    unlockRequested = Signal(str)
    switchWallpaper = Signal(int)

    def __init__(self, screen, cfg, services, is_primary=True, parent=None):
        super().__init__(parent)
        self._screen = screen
        self._cfg = cfg
        self._svc = services
        self._is_primary = is_primary
        self._started = None
        self._ken_anim = None
        self._shake = None
        self._under_pm = None    # 底层当前壁纸（None=纯色/未加载）
        self._trans = None       # 进行中的切换动效动画

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setWindowTitle("AiLock")
        self.setCursor(Qt.ArrowCursor)
        self._build()
        self._apply_screen_geometry()
        self.setStyleSheet(T.LOCK_QSS)

    # ---------------------------------------------------------- 构建
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- 背景层（放在最底，用绝对定位铺满）---
        # 双层结构：bgLabel 是当前壁纸（Ken Burns 作用于它），bgOver 是
        # 切换动效期间承载新壁纸的过渡层，动效结束后回合入底层。
        self.bgLabel = QLabel(self)
        self.bgLabel.setObjectName("bg")
        self.bgLabel.setScaledContents(False)

        self.bgOver = QLabel(self)
        self.bgOver.setScaledContents(True)
        self.bgOver.setVisible(False)

        # 暗化遮罩（纯色模式下也压一层，保证文字可读）
        self.veil = QLabel(self)
        self.veil.setStyleSheet("background:rgba(6,10,18,.28);")

        # 擦除动效层
        self.wipe = WipeOverlay(self)
        self.wipe.setVisible(False)

        # 堆叠顺序（从底到顶）：bgLabel < wipe/bgOver < veil < 其余内容。
        # 两者都在 bgLabel 之上、veil 之下即可（动效层同一时刻只显示一个）；
        # 不用 lower()——多控件时顺序靠创建顺序，不直观。
        self.bgOver.stackUnder(self.veil)
        self.wipe.stackUnder(self.bgOver)
        self.bgLabel.stackUnder(self.wipe)

        # --- 中央内容 ---
        center = QWidget(self)
        center.setAttribute(Qt.WA_TranslucentBackground)
        cl = QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addStretch(1)

        self.clock = QLabel("00:00", center)
        self.clock.setObjectName("clock")
        self.clock.setAlignment(Qt.AlignHCenter | Qt.AlignBottom)
        self.clock.setFont(T.font(64, QFont.ExtraLight))
        self.clock.setStyleSheet("color:%s;letter-spacing:2px;" % T.LOCK_INK)
        cl.addWidget(self.clock, 0, Qt.AlignHCenter)

        self.date = QLabel("", center)
        self.date.setObjectName("date")
        self.date.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.date.setFont(T.font(15))
        cl.addWidget(self.date, 0, Qt.AlignHCenter)

        # 提示语：单行加宽展示，超长时循环滚动（跑马灯），不换行。
        # 注意：不要加 Qt.AlignHCenter —— 那会让布局把控件收缩到 sizeHint 宽，
        # 而 sizeHint 失效在多层嵌套布局里传播不可靠，宽度会卡在默认 100px
        # 导致误判「放不下」而滚动。让它占满整行，文字居中由控件内部绘制处理。
        self.msg = MarqueeLabel(center)
        self.msg.setObjectName("msg")
        self.msg.setFont(T.font(14))
        self.msg.setFixedHeight(26)
        cl.addSpacing(6)
        cl.addWidget(self.msg)

        # 密码卡片
        self.card = GlassCard(20, center)
        self.card.setFixedWidth(360)
        cardl = QVBoxLayout(self.card)
        cardl.setContentsMargins(26, 26, 26, 24)
        cardl.setSpacing(12)

        av = QHBoxLayout()
        av.addStretch(1)
        av.addWidget(AvatarCircle("A", 52, self.card))
        av.addStretch(1)
        cardl.addLayout(av)

        self.cardTitle = QLabel("输入密码以解锁")
        self.cardTitle.setAlignment(Qt.AlignHCenter)
        self.cardTitle.setFont(T.font(15, QFont.Medium))
        self.cardTitle.setStyleSheet("color:%s;" % T.LOCK_INK)
        cardl.addWidget(self.cardTitle)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.pw = QLineEdit(self.card)
        self.pw.setObjectName("pw")
        self.pw.setEchoMode(QLineEdit.Password)
        self.pw.setPlaceholderText("密码")
        self.pw.setFixedHeight(42)
        self.pw.setFont(T.font(13))
        self.pw.returnPressed.connect(self._submit)
        self.unlockBtn = QPushButton("解锁", self.card)
        self.unlockBtn.setObjectName("unlock")
        self.unlockBtn.setFixedSize(76, 42)
        self.unlockBtn.setFont(T.font(13, QFont.Medium))
        self.unlockBtn.setCursor(Qt.PointingHandCursor)
        self.unlockBtn.clicked.connect(self._submit)
        row.addWidget(self.pw, 1)
        row.addWidget(self.unlockBtn)
        cardl.addLayout(row)

        self.err = QLabel("")
        self.err.setObjectName("err")
        self.err.setAlignment(Qt.AlignHCenter)
        self.err.setFont(T.font(12))
        self.err.setVisible(False)
        cardl.addWidget(self.err)

        cl.addSpacing(20)
        cl.addWidget(self.card, 0, Qt.AlignHCenter)
        cl.addStretch(1)

        # 底部状态栏
        self.statusBar = QFrame(self)
        self.statusBar.setObjectName("statusBar")
        self.statusBar.setFixedHeight(46)
        sb = QHBoxLayout(self.statusBar)
        sb.setContentsMargins(18, 0, 18, 0)
        sb.setSpacing(14)
        self.sbDot = StatusDot(T.LOCK_OK, 7, self.statusBar)
        self.sbTask = QLabel("空闲")
        self.sbTask.setObjectName("statusText")
        self.sbTask.setFont(T.font(12))
        sb.addWidget(self.sbDot)
        sb.addWidget(self.sbTask)
        sb.addWidget(_sep())
        d2 = StatusDot(T.LOCK_OK, 7, self.statusBar)
        lbl = QLabel("防休眠已开启")
        lbl.setObjectName("statusText")
        lbl.setFont(T.font(12))
        sb.addWidget(d2)
        sb.addWidget(lbl)
        self.sbSep = _sep()
        self.sbLockLbl = QLabel("")
        self.sbLockLbl.setObjectName("statusText")
        self.sbLockLbl.setFont(T.font(12))
        self.sbLockLbl.setVisible(False)
        sb.addWidget(self.sbSep)
        sb.addWidget(self.sbLockLbl)
        self.sbSep.setVisible(False)
        sb.addStretch(1)
        kb = QLabel("← / → 切换壁纸 · Ctrl+Alt+L 锁屏")
        kb.setObjectName("statusText")
        kb.setFont(T.font(11))
        kb.setStyleSheet("color:%s;" % T.LOCK_INK_3)
        sb.addWidget(kb)

        # 壁纸切换箭头
        self.arrowL = QPushButton("‹", self)
        self.arrowR = QPushButton("›", self)
        for a in (self.arrowL, self.arrowR):
            a.setObjectName("arrow")
            a.setFixedSize(44, 44)
            a.setFont(T.font(22))
            a.setCursor(Qt.PointingHandCursor)
        self.arrowL.clicked.connect(lambda: self.switchWallpaper.emit(-1))
        self.arrowR.clicked.connect(lambda: self.switchWallpaper.emit(1))

        # toast
        self.toast = QLabel(self)
        self.toast.setObjectName("toast")
        self.toast.setAlignment(Qt.AlignCenter)
        self.toast.setFont(T.font(12))
        self.toast.setFixedHeight(34)
        self.toast.setStyleSheet(
            "background:rgba(10,16,28,.72);color:%s;border-radius:17px;"
            % T.LOCK_INK_2)
        self.toast.setVisible(False)

        # 待执行动作横幅
        self.pending = GlassCard(18, self)
        self.pending.setFixedWidth(420)
        pl = QVBoxLayout(self.pending)
        pl.setContentsMargins(24, 18, 24, 18)
        pl.setSpacing(4)
        self.paTitle = QLabel("任务完成")
        self.paTitle.setAlignment(Qt.AlignHCenter)
        self.paTitle.setFont(T.font(16, QFont.DemiBold))
        self.paTitle.setStyleSheet("color:%s;" % T.LOCK_INK)
        self.paCount = QLabel("90 秒")
        self.paCount.setAlignment(Qt.AlignHCenter)
        self.paCount.setFont(T.font(30, QFont.Light))
        self.paCount.setStyleSheet("color:%s;" % T.LOCK_DANGER)
        self.paHint = QLabel("输入密码可取消，倒计时结束后执行")
        self.paHint.setAlignment(Qt.AlignHCenter)
        self.paHint.setFont(T.font(12))
        self.paHint.setStyleSheet("color:%s;" % T.LOCK_INK_3)
        pl.addWidget(self.paTitle)
        pl.addWidget(self.paCount)
        pl.addWidget(self.paHint)
        self.pending.setVisible(False)

        # 会话卡堆叠（右侧覆盖层，创建顺序在 veil 之后 → 绘制在其上方）
        self.stack = SessionStack(self)

        # 解锁成功
        self.unlocked = QLabel("✓", self)
        self.unlocked.setAlignment(Qt.AlignCenter)
        self.unlocked.setFont(T.font(56))
        self.unlocked.setStyleSheet("color:%s;background:transparent;"
                                    % T.LOCK_OK)
        self.unlocked.setVisible(False)

        self._center = center
        self._root_layout = root
        root.addWidget(center)
        root.addWidget(self.statusBar)

        # 时钟定时
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self._tick()

    # ---------------------------------------------------------- 几何
    def _apply_screen_geometry(self):
        g = self._screen.geometry()
        self.setGeometry(g)
        self._apply_bg_geometry(g.width(), g.height())
        self._place_overlays()

    def _apply_bg_geometry(self, w: int, h: int):
        # 背景相关四件套全部铺满窗口。veil 是无 setText/setPixmap 的纯色盖板，
        # 创建时停在默认坐标 (0,0) 默认尺寸 (~100x30)，不显式设全屏就会在左上
        # 留下一坨深色块（叠 28% 透明度 + 壁纸 ≈ 一块灰影）。bgOver / wipe 的可
        # 视区也要跟随窗口大小，否则过渡层还是按老尺寸贴图会出现错位。
        self.bgLabel.setGeometry(0, 0, w, h)
        self.bgOver.setGeometry(0, 0, w, h)
        self.wipe.setGeometry(0, 0, w, h)
        self.veil.setGeometry(0, 0, w, h)

    def _place_overlays(self):
        w, h = self.width(), self.height()
        self.statusBar.setGeometry(0, h - 46, w, 46)
        self.arrowL.move(24, h // 2 - 22)
        self.arrowR.move(w - 24 - 44, h // 2 - 22)
        self.toast.setGeometry((w - 260) // 2, h - 110, 260, 34)
        self.pending.setGeometry((w - 420) // 2, h // 2 - 150, 420, 128)
        self.unlocked.setGeometry(0, h // 2 - 60, w, 120)
        self.stack.reposition()
        # 状态栏上方留白，别让内容贴底
        self._root_layout.setContentsMargins(0, 0, 0, 56)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply_bg_geometry(self.width(), self.height())
        self._place_overlays()

    # ---------------------------------------------------------- 时钟
    def _tick(self):
        now = time.localtime()
        self.clock.setText(time.strftime("%H:%M", now))
        week = "一二三四五六日"[now.tm_wday]
        self.date.setText(time.strftime("%Y年%m月%d日", now) + " 星期" + week)

    # ---------------------------------------------------------- 交互
    def _submit(self):
        pwd = self.pw.text()
        if not pwd:
            return
        self.unlockBtn.setEnabled(False)
        self.pw.setEnabled(False)
        self.unlockRequested.emit(pwd)

    def reset_input(self, focus: bool = True):
        self.pw.setEnabled(True)
        self.unlockBtn.setEnabled(True)
        self.pw.clear()
        if focus:
            self.pw.setFocus()

    def show_error(self, text: str, shake: bool = True):
        self.err.setText(text)
        self.err.setVisible(True)
        self.pw.setEnabled(True)
        self.unlockBtn.setEnabled(True)
        self.pw.selectAll()
        self.pw.setFocus()
        if shake and self._cfg.get("ui.animations", True):
            self._shake_card()

    def _shake_card(self):
        """左右摇晃：用多段动画串联，避免 setKeyValues 的类型差异问题。"""
        if self._shake:
            self._shake.stop()
        base = self.card.pos()
        offsets = [-12, 10, -7, 5, 0]
        grp = QSequentialAnimationGroup(self)
        cur = base
        for off in offsets:
            nxt = base + QPoint(off, 0)
            seg = QPropertyAnimation(self.card, b"pos", grp)
            seg.setDuration(80)
            seg.setEasingCurve(QEasingCurve.InOutQuad)
            seg.setStartValue(cur)
            seg.setEndValue(nxt)
            grp.addAnimation(seg)
            cur = nxt
        self._shake = grp
        grp.start()
        # 红边提示
        self.card._border = QColor(255, 107, 107, 200)
        QTimer.singleShot(700, self._clear_error_border)

    def _clear_error_border(self):
        self.card._border = QColor(255, 255, 255, 36)
        self.card.update()

    def show_unlocked(self):
        self.unlocked.setVisible(True)
        self.card.setVisible(False)
        self.err.setVisible(False)

    # ---------------------------------------------------------- 背景
    def set_background(self, pixmap: QPixmap | None, ken: bool = True):
        if pixmap is None or pixmap.isNull():
            self._cancel_transition()
            color = self._cfg.get("background.color", T.LOCK_BG)
            self._set_under_color(color)
            return
        self._show_wallpaper(pixmap, ken)

    def _set_under_color(self, color: str):
        self._under_pm = None
        self.bgLabel.setStyleSheet("background:%s;" % color)
        self.bgLabel.setPixmap(QPixmap())

    def _apply_under(self, pm: QPixmap, ken: bool):
        """把新壁纸落到底层，并（按需）启动 Ken Burns。"""
        self.bgLabel.setStyleSheet("background:#000000;")
        self.bgLabel.setPixmap(pm)
        self._under_pm = pm
        self._start_ken(ken)

    def _show_wallpaper(self, pm: QPixmap, ken: bool):
        """按配置选择切换动效；首次铺图（底层为空）不做过渡。"""
        mode = str(self._cfg.get("background.transition", "fade")
                   or "fade").strip().lower()
        if mode not in BG_TRANSITIONS:
            mode = "fade"
        if not self._cfg.get("ui.animations", True) or self._under_pm is None:
            mode = "none"
        if mode == "none":
            self._apply_under(pm, ken)
            return

        self._cancel_transition()
        # 过渡期间先停掉 Ken Burns，避免几何动画打架；结束后再启动
        self._start_ken(False)
        w, h = self.width(), self.height()
        dur = 650
        easing = QEasingCurve.OutCubic
        finish = lambda: self._finish_transition(pm, ken)  # noqa: E731

        if mode == "fade":
            self._prep_over(pm)
            eff = QGraphicsOpacityEffect(self.bgOver)
            self.bgOver.setGraphicsEffect(eff)
            eff.setOpacity(0.0)
            a = QPropertyAnimation(eff, b"opacity", self)
            a.setDuration(dur)
            a.setStartValue(0.0)
            a.setEndValue(1.0)
            a.finished.connect(finish)
            a.start()
            self._trans = a
        elif mode == "slide":
            self._prep_over(pm)
            a = QPropertyAnimation(self.bgOver, b"pos", self)
            a.setDuration(dur)
            a.setEasingCurve(easing)
            a.setStartValue(QPoint(w, 0))
            a.setEndValue(QPoint(0, 0))
            a.finished.connect(finish)
            a.start()
            self._trans = a
        elif mode == "zoom":
            self._prep_over(pm)
            eff = QGraphicsOpacityEffect(self.bgOver)
            self.bgOver.setGraphicsEffect(eff)
            eff.setOpacity(0.0)
            fade = QPropertyAnimation(eff, b"opacity", self)
            fade.setDuration(dur)
            fade.setStartValue(0.0)
            fade.setEndValue(1.0)
            m = max(4, int(min(w, h) * 0.07))
            geo = QPropertyAnimation(self.bgOver, b"geometry", self)
            geo.setDuration(dur)
            geo.setEasingCurve(easing)
            geo.setStartValue(QRect(-m, -m, w + 2 * m, h + 2 * m))
            geo.setEndValue(QRect(0, 0, w, h))
            grp = QParallelAnimationGroup(self)
            grp.addAnimation(fade)
            grp.addAnimation(geo)
            grp.finished.connect(finish)
            grp.start()
            self._trans = grp
        elif mode == "wipe":
            self.wipe.setPixmap(pm)
            self.wipe.setFrac(0.0)
            self.wipe.setVisible(True)
            self.wipe.stackUnder(self.veil)
            a = QPropertyAnimation(self.wipe, b"frac", self)
            a.setDuration(dur + 100)
            a.setEasingCurve(easing)
            a.setStartValue(0.0)
            a.setEndValue(1.0)
            a.finished.connect(finish)
            a.start()
            self._trans = a

    def _prep_over(self, pm: QPixmap):
        """过渡层就位：铺满窗口、载入新壁纸、复位位置并置顶显示。"""
        self.bgOver.setGeometry(0, 0, self.width(), self.height())
        self.bgOver.move(0, 0)
        self.bgOver.setPixmap(pm)
        self.bgOver.setGraphicsEffect(None)
        self.bgOver.stackUnder(self.veil)
        self.bgOver.setVisible(True)

    def _finish_transition(self, pm: QPixmap, ken: bool):
        self._trans = None
        self._apply_under(pm, ken)
        self.bgOver.setVisible(False)
        self.bgOver.setPixmap(QPixmap())
        self.bgOver.setGraphicsEffect(None)
        self.wipe.setVisible(False)
        self.wipe.setPixmap(None)

    def _cancel_transition(self):
        if self._trans is not None:
            try:
                self._trans.stop()
            except Exception:
                pass
            self._trans = None
        self.bgOver.setVisible(False)
        self.wipe.setVisible(False)

    def _start_ken(self, ken: bool):
        if self._ken_anim:
            self._ken_anim.stop()
            self._ken_anim = None
        self.bgLabel.setGeometry(0, 0, self.width(), self.height())
        if not ken or not self._cfg.get("background.kenburns", True):
            return
        if not self._cfg.get("ui.animations", True):
            return
        g = self.rect()
        anim = QPropertyAnimation(self.bgLabel, b"geometry", self)
        anim.setDuration(28000)
        anim.setEasingCurve(QEasingCurve.InOutSine)
        m = 26
        anim.setStartValue(QRect(-m // 2, -m // 2,
                                 g.width() + m, g.height() + m))
        anim.setEndValue(QRect(-m, -m, g.width() + m * 2, g.height() + m * 2))
        anim.start()
        self._ken_anim = anim

    # ---------------------------------------------------------- 状态
    def set_message(self, text: str, color: str | None = None):
        self.msg.setInk(text or "", color=color)

    def apply_status(self, data):
        """渲染会话列表。

        data 既可以是多会话快照 list[dict]，也可以是旧的单会话 dict
        （selftest / 手工推送），由 normalize_sessions 统一。
        会话卡全在右侧覆盖层里，这里不碰中央布局，因此不会出现卡片
        显隐导致的布局抖动。

        先过滤出「还该展示」的会话（live_sessions），卡片和底部状态栏都用
        这一份 —— 否则会出现卡片撤光了、状态栏还在说「等 N 个会话」。
        """
        sessions = live_sessions(data)
        if not sessions:
            self.stack.apply([])
            self.sbTask.setText("空闲")
            self.sbDot.setColor(STATE_COLORS["idle"])
            return

        self.stack.apply(sessions)
        primary = pick_primary(sessions)
        state = str(primary.get("state") or "idle").lower()
        color = STATE_COLORS.get(state, STATE_COLORS["idle"])
        task = str(primary.get("task") or "").strip() or "任务"
        if len(sessions) > 1:
            self.sbTask.setText("%s 等 %d 个会话" % (task, len(sessions)))
        else:
            self.sbTask.setText(task)
        self.sbDot.setColor(color)

    def set_min_lock(self, seconds: float):
        if seconds and seconds > 0:
            self.sbLockLbl.setText("最短锁定 " + T.mmss(seconds))
            self.sbLockLbl.setVisible(True)
            self.sbSep.setVisible(True)
        else:
            self.sbLockLbl.setVisible(False)
            self.sbSep.setVisible(False)

    def show_pending(self, info: dict | None):
        if not info:
            self.pending.setVisible(False)
            return
        self.pending.setVisible(True)
        self.paTitle.setText(info.get("reason") or "任务完成")
        self.paCount.setText("%d 秒" % max(0, int(info.get("remaining", 0))))

    def flash_toast(self, text: str, ms: int = 1600):
        self.toast.setText(text)
        self.toast.setVisible(True)
        QTimer.singleShot(ms, lambda: self.toast.setVisible(False))

    # ---------------------------------------------------------- 动效
    def play_entrance(self):
        if not self._cfg.get("ui.animations", True):
            return
        eff = QGraphicsOpacityEffect(self.card)
        self.card.setGraphicsEffect(eff)
        fade = QPropertyAnimation(eff, b"opacity", self)
        fade.setDuration(420)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        base = self.card.pos()
        move = QPropertyAnimation(self.card, b"pos", self)
        move.setDuration(420)
        move.setEasingCurve(QEasingCurve.OutCubic)
        move.setStartValue(base + QPoint(0, 34))
        move.setEndValue(base)
        grp = QParallelAnimationGroup(self)
        grp.addAnimation(fade)
        grp.addAnimation(move)
        grp.start()
        self._entrance = grp

    def focus_input(self):
        self.pw.setFocus()
        self.activateWindow()
        self.raise_()


def _sep() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.VLine)
    f.setFixedHeight(16)
    f.setStyleSheet("background:rgba(255,255,255,.18);border:none;")
    return f


class BlackoutOverlay(QWidget):
    """黑屏覆盖层：铺满整个锁屏窗口的不透明纯黑层。

    Modern Standby 机器上不能用 SC_MONITORPOWER 真关屏（关屏即触发
    连接待机，进程被冻结，见 core/power.py 注释），改由这层遮挡实现
    「看起来黑了」。显示器本身保持点亮，系统全程清醒。

    交互：鼠标点击被本层吞掉（防止误触到下面的密码卡）；任何键鼠
    输入都会重置系统空闲计时，PowerGuard 轮询到后调用 set_blackout(False)
    恢复界面。
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.setAutoFillBackground(True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("background-color:#000000;")
        self.setCursor(Qt.BlankCursor)
        self.hide()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#000000"))

    def mousePressEvent(self, _e):
        self.hide()

    def mouseMoveEvent(self, _e):
        self.hide()

    def keyPressEvent(self, _e):
        self.hide()


# ============================================================ 管理器
class LockScreen:
    """锁屏管理器：为每个显示器创建一个 LockWindow。

    接口与旧 tkinter 版保持一致，main.py 可直接替换。
    root 参数仅为兼容旧签名，Qt 版不使用。
    """

    def __init__(self, root, cfg, try_unlock, initial_status=None):
        self.cfg = cfg
        self.try_unlock = try_unlock
        self.windows: list = []
        self._started = time.time()
        self._min_seconds = 0.0
        self._svc = WallpaperService()
        self._svc.loaded.connect(self._on_wallpaper)
        self._files, idx = collect_wallpapers(cfg)
        self._svc.set_files(self._files, idx)
        self._slide: QTimer | None = None
        self._setup_slide_timer()
        # 关键：在构造 windows 之前先把状态记下来，构造完之后立刻 apply。
        # 会话卡虽然是覆盖层（不参与布局），但锁屏一显示就该是对的內容，
        # 否则用户会先看到空屏再闪出卡片。
        self._last_status = normalize_sessions(initial_status)
        self._msg = ""
        self._msg_color = None

        blur = int(cfg.get("background.blur", 0) or 0)
        dim = float(cfg.get("background.dim", 0.45) or 0)
        self._blur, self._dim = blur, dim

        screens = QApplication.screens() or []
        if not screens:
            screens = [QApplication.primaryScreen()]
        for i, sc in enumerate(screens):
            try:
                w = LockWindow(sc, cfg, self._svc,
                               is_primary=(sc == QApplication.primaryScreen()))
                w.unlockRequested.connect(self._on_unlock)
                w.switchWallpaper.connect(self._on_switch)
                self.windows.append(w)
            except Exception as exc:
                log.log("qt.lock.create", "screen %d: %r" % (i, exc))

        # 在窗口 show 之前就把会话卡渲染出来，避免锁屏瞬间闪一下空状态
        if self._last_status:
            for w in self.windows:
                w.apply_status(self._last_status)

    # ---------------------------------------------------------- 生命周期
    def show(self, min_lock_minutes=0):
        self._min_seconds = float(min_lock_minutes or 0) * 60.0
        self._started = time.time()
        msg = self.cfg.get("message.text", "") or ""
        color = str(self.cfg.get("message.color", "") or "").strip() or None
        for w in self.windows:
            w.set_message(msg, color)
            w.showFullScreen()
            w.show()
            w.raise_()
        # 壁纸：先铺纯色/上次缓存，异步加载真实图片
        self._request_wallpaper()
        for w in self.windows:
            w.play_entrance()
            w.focus_input()
        self._tick_min_lock()

    def destroy(self):
        self._stop_slide_timer()
        for w in self.windows:
            try:
                w.close()
                w.deleteLater()
            except Exception:
                pass
        self.windows = []

    def lock_elapsed(self) -> float:
        return max(0.0, time.time() - self._started)

    # ---------------------------------------------------------- 状态推送
    def update_status(self, data):
        """推送会话快照：list[dict]（多会话）或单个 dict（兼容旧调用）。"""
        sessions = normalize_sessions(data)
        if not sessions:
            return
        self._last_status = sessions
        for w in self.windows:
            w.apply_status(sessions)
        self._tick_min_lock()

    def show_pending_action(self, pa):
        if not pa:
            for w in self.windows:
                w.show_pending(None)
            return
        info = {"reason": getattr(pa, "reason", ""),
                "remaining": int(pa.remaining())}
        for w in self.windows:
            w.show_pending(info)

    def set_message(self, text: str, color: str | None = None):
        self._msg = text or ""
        self._msg_color = color
        for w in self.windows:
            w.set_message(text, color)

    # ---------------------------------------------------------- 黑屏模式
    def set_blackout(self, on: bool):
        """Modern Standby 机器的「息屏」：每个屏铺一层纯黑覆盖层。"""
        for w in self.windows:
            if on:
                if not hasattr(w, "_blackout"):
                    w._blackout = BlackoutOverlay(w)
                w._blackout.setGeometry(w.rect())
                w._blackout.show()
                w._blackout.raise_()
            else:
                blk = getattr(w, "_blackout", None)
                if blk is not None:
                    blk.hide()

    # ---------------------------------------------------------- 解锁回调
    def _on_unlock(self, password: str):
        ok, msg = self.try_unlock(password or "")
        if ok:
            for w in self.windows:
                w.show_unlocked()
            return
        for w in self.windows:
            w.show_error(msg or "密码错误")

    def _on_switch(self, direction: int):
        if self._svc.count <= 1:
            for w in self.windows:
                w.flash_toast("当前壁纸模式不支持切换", 1200)
            return
        idx = self._svc.step(direction)
        self._request_wallpaper()
        self._setup_slide_timer()   # 手动切换后重新计时
        name = self._svc.current_name()
        for w in self.windows:
            w.flash_toast("壁纸 %d / %d · %s" % (idx + 1, self._svc.count,
                                                 name))

    # ---------------------------------------------------------- 壁纸
    def _setup_slide_timer(self):
        """文件夹/在线模式下按 slideshow_seconds 自动轮播。"""
        self._stop_slide_timer()
        mode = str(self.cfg.get("background.mode", "color"))
        if mode not in ("folder", "online"):
            return
        seconds = float(self.cfg.get("background.slideshow_seconds", 30) or 0)
        if seconds <= 0 or len(self._files) <= 1:
            return
        parent = self.windows[0] if self.windows else None
        t = QTimer(parent)
        t.timeout.connect(self._slide_next)
        t.start(int(max(5, seconds) * 1000))
        self._slide = t

    def _stop_slide_timer(self):
        if self._slide is not None:
            try:
                self._slide.stop()
            except Exception:
                pass
            self._slide = None

    def _slide_next(self):
        if self._svc.count <= 1:
            return
        self._svc.step(1)
        self._request_wallpaper()

    def refresh_files(self):
        """在线壁纸下载完成后由主控调用：重新收集文件列表并刷新显示。"""
        files, idx = collect_wallpapers(self.cfg)
        if files == self._files:
            return
        self._files = files
        self._svc.set_files(files, idx if 0 <= idx < len(files) else 0)
        self._setup_slide_timer()
        for w in self.windows:
            w.flash_toast("在线壁纸已更新 · 共 %d 张" % len(files), 1800)
        self._request_wallpaper()

    def _request_wallpaper(self):
        if not self._files:
            for w in self.windows:
                w.set_background(None)
            return
        idx = self._svc.index
        size = self.windows[0].size() if self.windows else None
        for w in self.windows:
            self._svc.request(idx, w.size(), self._blur, self._dim)
            if size is None:
                size = w.size()

    def _on_wallpaper(self, index: int, pm: QPixmap, _name: str):
        if index != self._svc.index:
            return
        ken = bool(self.cfg.get("background.kenburns", True))
        for w in self.windows:
            w.set_background(pm, ken)

    # ---------------------------------------------------------- 最短锁定
    def _tick_min_lock(self):
        left = self._min_seconds - self.lock_elapsed()
        for w in self.windows:
            w.set_min_lock(left)
        if left > 0:
            QTimer.singleShot(1000, self._tick_min_lock)
