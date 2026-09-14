"""AiLock Qt 版 · 自定义控件

设置面板用：胶囊开关 / 分段按钮 / 滑块行 / 卡片行 / 分组卡片
锁屏用：圆形头像 / 细进度条 / 状态点

控件全部自绘，不依赖平台样式，保证在不同 DPI 下观感一致。
"""

from PySide6.QtCore import (QEasingCurve, QPoint, QPropertyAnimation,
                            QRect, QRectF, Qt, QTimer, Signal)
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractButton, QFrame, QHBoxLayout, QLabel,
                               QPushButton, QSizePolicy, QSlider,
                               QVBoxLayout, QWidget)

from . import theme as T


# ============================================================ 胶囊开关
class Switch(QAbstractButton):
    """宽 46 / 高 26 的 iOS 风格开关，带位移动画。"""

    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(46, 26)
        self._pos = 1.0 if checked else 0.0
        # 注意：动画目标属性名不能叫 "pos" —— QWidget 自己有一个 pos 是 QPoint，
        # PySide6 在某些路径上会优先解析到 C++ 的 QPoint 属性，动画一启动开关
        # 会被当成几何值插值，整控件"飘"到屏幕一角。改用不撞名的 "knob"。
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.InOutQuad)
        self.toggled.connect(self._on_toggle)

    # pyqtProperty 等价：用 Qt Property 包装 _pos
    def getKnob(self):
        return self._pos

    def setKnob(self, v):
        self._pos = float(v)
        self.update()

    knob = __import__("PySide6.QtCore", fromlist=["Property"]).Property(
        float, getKnob, setKnob)

    def _on_toggle(self, on: bool):
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def sizeHint(self):
        return self.minimumSizeHint()

    def minimumSizeHint(self):
        from PySide6.QtCore import QSize
        return QSize(46, 26)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        track = QColor(T.C_ACCENT) if self.isChecked() else QColor(T.C_TRACK)
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(self.rect()), 13, 13)

        d = 20
        margin = 3
        x = margin + (self.width() - d - margin * 2) * self._pos
        knob = QColor("#FFFFFF")
        p.setBrush(knob)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(x, margin, d, d))
        p.end()


# ============================================================ 分段按钮
class SegButton(QWidget):
    """纯色 / 单图 / 文件夹 这类互斥选择。"""

    changed = Signal(str)

    def __init__(self, options, value: str = "", parent=None):
        super().__init__(parent)
        self._value = value
        self._btns = {}
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.setFixedHeight(32)
        for label, val in options:
            # QAbstractButton 是抽象类不能直接实例化，用扁平 QPushButton 代替
            b = QPushButton(self)
            b.setFlat(True)
            b.setText(label)
            b.setCursor(Qt.PointingHandCursor)
            b.setCheckable(True)
            b.setFont(T.font(12))
            b.setMinimumWidth(64)
            b.clicked.connect(lambda _c=False, v=val: self._pick(v))
            self._btns[val] = b
            lay.addWidget(b)
        self._sync()

    def _pick(self, val: str):
        if val != self._value:
            self._value = val
            self._sync()
            self.changed.emit(val)
        else:
            # 重复点击已选项：恢复勾选态，避免视觉上被取消选中
            self._sync()

    def _sync(self):
        for val, b in self._btns.items():
            b.setChecked(val == self._value)

    def value(self) -> str:
        return self._value

    def setValue(self, v: str):
        self._value = v
        self._sync()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # 外框容器
        p.setPen(QPen(QColor(T.C_BORDER), 1))
        p.setBrush(QColor(T.C_BG))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -1, -1), 8, 8)
        # 选中项高亮胶囊
        b = self._btns.get(self._value)
        if b and b.isVisible():
            r = b.geometry().adjusted(2, 2, -2, -2)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(T.C_ACCENT))
            p.drawRoundedRect(QRectF(r), 6, 6)
            b.setStyleSheet("color:#FFFFFF;background:transparent;")
        for val, btn in self._btns.items():
            if val != self._value:
                btn.setStyleSheet("color:%s;background:transparent;" % T.C_DIM)
        p.end()


# ============================================================ 设置行 / 卡片
class CardRow(QFrame):
    """一行：标题 + 描述 + 右侧控件。"""

    def __init__(self, title: str, desc: str = "", control: QWidget = None,
                 vertical: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("cardRow")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(16)

        text = QWidget(self)
        tl = QVBoxLayout(text)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(3)
        t = QLabel(title)
        t.setObjectName("rowTitle")
        t.setFont(T.font(13, QFont.Medium))
        tl.addWidget(t)
        if desc:
            d = QLabel(desc)
            d.setObjectName("rowDesc")
            d.setFont(T.font(11))
            d.setWordWrap(True)
            tl.addWidget(d)
        lay.addWidget(text, 1)

        if vertical:
            outer = QWidget(self)
            ol = QVBoxLayout(outer)
            ol.setContentsMargins(0, 0, 0, 0)
            ol.addWidget(control)
            lay.addWidget(outer, 2)
        elif control is not None:
            lay.addWidget(control, 0, Qt.AlignRight | Qt.AlignVCenter)
        elif not desc:
            lay.addStretch(1)


class CardBlock(QFrame):
    """圆角白底分组容器。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("cardBlock")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(0)
        self._rows = []

    def addRow(self, row: CardRow):
        self._rows.append(row)
        self._lay.addWidget(row)
        if len(self._rows) > 1:
            line = QFrame(self)
            line.setFrameShape(QFrame.HLine)
            line.setStyleSheet("background:%s;border:none;" % T.C_BORDER)
            line.setFixedHeight(1)
            self._lay.insertWidget(self._lay.count() - 1, line)
        return row


# ============================================================ 滑块行
class SliderRow(QWidget):
    """滑块 + 实时数值，值域 0~1 或自定义。"""

    valueChanged = Signal(float)

    def __init__(self, minimum=0, maximum=100, value=0, fmt: str = "%d%%",
                 scale: float = 100.0, parent=None):
        super().__init__(parent)
        self._fmt = fmt
        self._scale = scale
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(minimum, maximum)
        self.slider.setValue(value)
        self.slider.setFixedWidth(180)
        self.slider.setCursor(Qt.PointingHandCursor)
        self.slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                background: {T.C_TRACK}; height: 4px; border-radius: 2px;
            }}
            QSlider::sub-page:horizontal {{
                background: {T.C_ACCENT}; height: 4px; border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                background: #FFFFFF; border: 1px solid {T.C_BORDER};
                width: 16px; margin: -6px 0; border-radius: 8px;
            }}
            QSlider::handle:horizontal:hover {{ border-color: {T.C_ACCENT}; }}
        """)
        self.label = QLabel(self._fmt % (value / self._scale * 100
                                         if self._scale == 100 else value))
        self.label.setFont(T.font(12))
        self.label.setStyleSheet("color:%s;" % T.C_DIM)
        self.label.setFixedWidth(52)
        self.label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(self.slider)
        lay.addWidget(self.label)
        lay.addStretch(1)
        self.slider.valueChanged.connect(self._on_change)

    def _on_change(self, v):
        if self._scale == 100.0:
            self.label.setText(self._fmt % v)
        else:
            self.label.setText(self._fmt % (v / self._scale))
        self.valueChanged.emit(v)

    def value(self):
        return self.slider.value()

    def setValue(self, v):
        self.slider.setValue(v)


# ============================================================ 锁屏用控件
class AvatarCircle(QWidget):
    """锁屏卡片上的圆形头像（字母）。"""

    def __init__(self, letter: str = "A", size: int = 52, parent=None):
        super().__init__(parent)
        self._letter = letter[:1].upper()
        self.setFixedSize(size, size)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        grad_color = QColor(T.LOCK_ACCENT)
        p.setPen(Qt.NoPen)
        p.setBrush(grad_color)
        p.drawEllipse(r)
        p.setPen(QColor("#FFFFFF"))
        p.setFont(T.font(self.height() // 2 - 4, QFont.DemiBold))
        p.drawText(r, Qt.AlignCenter, self._letter)
        p.end()


class ThinProgress(QWidget):
    """细进度条，高度 3px。"""

    def __init__(self, parent=None, height: int = 3,
                 color: str = T.LOCK_ACCENT):
        super().__init__(parent)
        self._value = 0.0
        self._color = QColor(color)
        self.setFixedHeight(height)

    def setValue(self, v: float):
        self._value = max(0.0, min(1.0, float(v or 0)))
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 40))
        p.drawRoundedRect(QRectF(self.rect()), self.height() / 2,
                          self.height() / 2)
        if self._value > 0:
            w = self.width() * self._value
            p.setBrush(self._color)
            p.drawRoundedRect(QRectF(0, 0, w, self.height()),
                              self.height() / 2, self.height() / 2)
        p.end()


class StatusDot(QWidget):
    """状态小圆点，带呼吸感（running 时脉動）。"""

    def __init__(self, color: str = T.LOCK_OK, size: int = 8, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self._on = 1.0
        self.setFixedSize(size, size)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._pulse)
        self._pulsing = False

    def setColor(self, color: str):
        self._color = QColor(color)
        self.update()

    def setPulsing(self, on: bool):
        self._pulsing = on
        if on:
            self._timer.start(90)
        else:
            self._timer.stop()
            self._on = 1.0
            self.update()

    def _pulse(self):
        import time
        self._on = 0.45 + 0.55 * abs(__import__("math").sin(time.time() * 3))
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QColor(self._color)
        c.setAlphaF(c.alphaF() * self._on)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawEllipse(QRectF(self.rect()))
        p.end()


# ============================================================ 跑马灯提示语
class MarqueeLabel(QWidget):
    """单行提示语：宽度够时居中静止；放不下时无缝循环滚动（跑马灯）。

    全屏锁屏宽度充裕，提示语默认单行加宽展示；只有用户自定义超长文案时
    才触发滚动，替代换行（锁屏上多行提示语观感差）。
    """

    SPEED = 60          # 滚动速度，像素/秒
    GAP = 64            # 循环时两份文字之间的间隔
    REFRESH_MS = 33     # ~30fps

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        # 关键：LOCK_INK_2 是 CSS rgba(...) 字符串，QColor 不认，
        # 直接 QColor(...) 会得到无效色 → 画成黑色。必须走 parse_color。
        self._color = T.parse_color(T.LOCK_INK_2, QColor("#F2F6FC"))
        self._font = T.font(14)
        self._text_w = 0.0
        self._offset = 0.0
        self._scrolling = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------ 公共接口
    def setInk(self, text: str, color: str | None = None,
               font: QFont | None = None):
        """设置文字 / 颜色 / 字体（任一可省）。颜色同样走 parse_color。"""
        self._text = text or ""
        if color:
            c = T.parse_color(color)
            if c is not None:
                self._color = c
        if font:
            self._font = font
        self._reflow()
        # sizeHint 随文字宽度变化，必须通知父布局重排，
        # 否则控件宽度停留在旧值，会误判「放不下」而滚动
        self.updateGeometry()
        self.update()

    def text(self) -> str:
        return self._text

    # ------------------------------------------------------ 内部
    def sizeHint(self):
        from PySide6.QtCore import QSize
        return QSize(1, 26)

    def minimumSizeHint(self):
        from PySide6.QtCore import QSize
        return QSize(1, 26)

    def _reflow(self):
        from PySide6.QtGui import QFontMetrics
        self._offset = 0.0
        self._text_w = (QFontMetrics(self._font).horizontalAdvance(self._text)
                        if self._text else 0.0)
        # 宽度为 0 时（尚未布局）先按不滚动处理，resizeEvent 会再触发
        self._scrolling = (self.width() > 0
                           and self._text_w > self.width())
        if self._scrolling and self.isVisible():
            self._timer.start(self.REFRESH_MS)
        else:
            self._timer.stop()

    def _tick(self):
        period = self._text_w + self.GAP
        self._offset += self.SPEED * self.REFRESH_MS / 1000.0
        if self._offset >= period:
            self._offset -= period
        self.update()

    # ------------------------------------------------------ 事件
    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._reflow()

    def showEvent(self, e):
        super().showEvent(e)
        self._reflow()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()

    def paintEvent(self, _e):
        if not self._text:
            return
        p = QPainter(self)
        p.setFont(self._font)
        p.setPen(self._color)
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(self._font)
        y = (self.height() + fm.ascent() - fm.descent()) / 2.0
        if not self._scrolling:
            p.drawText(QRectF(0, 0, self.width(), self.height()),
                       int(Qt.AlignCenter), self._text)
        else:
            # 画两份文字实现无缝循环
            x = -self._offset
            p.drawText(QPointF(x, y), self._text)
            p.drawText(QPointF(x + self._text_w + self.GAP, y), self._text)
        p.end()
