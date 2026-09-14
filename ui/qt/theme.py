"""AiLock Qt 版 · 设计系统（颜色 / 字体 / 通用样式片段）

所有界面共用这一套 token，改这里就能整体换肤。
视觉上对齐原 Web 版：深色锁屏 + 浅色 Fluent 风格设置面板。
"""

import re

from PySide6.QtGui import QColor, QFont, QFontDatabase

# ---------------------------------------------------------------- 字体
_FONT_FAMILY = "Microsoft YaHei UI"
_FONT_FALLBACK = "Segoe UI"
_cached_family = None


def base_family() -> str:
    """返回可用的中文字体族。

    注意：QFontDatabase 必须在 QGuiApplication 创建之后才能安全调用，
    在模块顶层直接查询会导致进程硬崩溃（无 traceback，退出码 127）。
    所以这里做成懒加载 + 缓存。
    """
    global _cached_family
    if _cached_family is not None:
        return _cached_family
    try:
        families = QFontDatabase.families()
    except Exception:
        families = []
    if _FONT_FAMILY in families:
        _cached_family = _FONT_FAMILY
    elif _FONT_FALLBACK in families:
        _cached_family = _FONT_FALLBACK
    else:
        try:
            _cached_family = QFont().defaultFamily()
        except Exception:
            _cached_family = "System"
    return _cached_family


def font(size: int = 13, weight: QFont.Weight = QFont.Normal,
         family: str | None = None) -> QFont:
    f = QFont(family or base_family(), size)
    f.setWeight(weight)
    return f


# ---------------------------------------------------------------- 锁屏（深色）
LOCK_BG = "#0B1220"          # 默认纯色底（无壁纸时）
LOCK_INK = "#F2F6FC"         # 主文字
LOCK_INK_2 = "rgba(242,246,252,.82)"
LOCK_INK_3 = "rgba(242,246,252,.55)"
LOCK_CARD = "rgba(18,26,42,.55)"
LOCK_CARD_BORDER = "rgba(255,255,255,.14)"
LOCK_FIELD = "rgba(255,255,255,.08)"
LOCK_FIELD_BORDER = "rgba(255,255,255,.16)"
LOCK_ACCENT = "#4C8DFF"
LOCK_ACCENT_HOVER = "#6BA1FF"
LOCK_DANGER = "#FF6B6B"
LOCK_OK = "#34D399"
LOCK_BAR = "rgba(10,16,28,.55)"

# ---------------------------------------------------------------- 设置（浅色）
C_BG = "#F6F7F9"
C_PANEL = "#FFFFFF"
C_TEXT = "#1B2430"
C_DIM = "#5A6878"
C_FAINT = "#9AA7B6"
C_BORDER = "#E4E8EE"
C_ACCENT = "#2F6FED"
C_ACCENT_BG = "#E8F1FF"
C_ACCENT_DIM = "#D6E4FF"
C_OK = "#12A67A"
C_DANGER = "#D9534F"
C_TRACK = "#D5DAE1"
C_TRACK_ON = "#2F6FED"

RADIUS_CARD = 14
RADIUS_CTL = 8
RADIUS_PILL = 999


# ---------------------------------------------------------------- QSS 片段
def q(s: str) -> str:
    """去掉 QSS 里的注释空行，便于拼接。"""
    return "\n".join(line.rstrip() for line in s.strip().splitlines())


LOCK_QSS = q(f"""
QWidget#LockRoot {{ background: transparent; }}
QLabel#clock {{
    color: {LOCK_INK}; background: transparent;
}}
QLabel#date, QLabel#msg {{ color: {LOCK_INK_2}; background: transparent; }}
QFrame#card {{
    background: {LOCK_CARD};
    border: 1px solid {LOCK_CARD_BORDER};
    border-radius: 20px;
}}
QLineEdit#pw {{
    background: {LOCK_FIELD};
    border: 1px solid {LOCK_FIELD_BORDER};
    border-radius: 10px;
    padding: 0 14px;
    color: {LOCK_INK};
    selection-background-color: {LOCK_ACCENT};
}}
QLineEdit#pw:focus {{ border: 1px solid {LOCK_ACCENT}; }}
QPushButton#unlock {{
    background: {LOCK_ACCENT};
    color: #FFFFFF;
    border: none;
    border-radius: 10px;
    font-weight: 600;
}}
QPushButton#unlock:hover {{ background: {LOCK_ACCENT_HOVER}; }}
QPushButton#unlock:pressed {{ background: #3B7AE6; }}
QLabel#err {{ color: {LOCK_DANGER}; background: transparent; }}
QFrame#statusBar {{
    background: {LOCK_BAR};
    border-radius: 12px;
}}
QLabel#statusText {{ color: {LOCK_INK_2}; background: transparent; }}
QPushButton#arrow {{
    background: rgba(255,255,255,.06);
    border: none;
    border-radius: 22px;
    color: {LOCK_INK_2};
}}
QPushButton#arrow:hover {{ background: rgba(255,255,255,.16); }}
""")

SETTINGS_QSS = q(f"""
QWidget#SettingsRoot {{ background: {C_BG}; }}
QFrame#nav {{
    background: {C_PANEL};
    border-right: 1px solid {C_BORDER};
}}
QFrame#cardBlock {{
    background: {C_PANEL};
    border: 1px solid {C_BORDER};
    border-radius: {RADIUS_CARD}px;
}}
QLabel#pageTitle {{ color: {C_TEXT}; background: transparent; }}
QLabel#pageSub {{ color: {C_FAINT}; background: transparent; }}
QLabel#rowTitle {{ color: {C_TEXT}; background: transparent; }}
QLabel#rowDesc {{ color: {C_DIM}; background: transparent; }}
QPushButton#navItem {{
    background: transparent; border: none; text-align: left;
    color: {C_DIM}; padding: 0 14px; border-radius: {RADIUS_CTL}px;
}}
QPushButton#navItem:hover {{ background: {C_BG}; color: {C_TEXT}; }}
QPushButton#navItem[active="true"] {{ background: {C_ACCENT_BG}; color: {C_ACCENT}; }}
QPushButton#btn {{
    background: {C_PANEL}; border: 1px solid {C_BORDER};
    border-radius: {RADIUS_CTL}px; color: {C_TEXT}; padding: 6px 16px;
}}
QPushButton#btn:hover {{ background: {C_BG}; }}
QPushButton#btnPrimary {{
    background: {C_ACCENT}; border: none; border-radius: {RADIUS_CTL}px;
    color: #FFFFFF; padding: 6px 18px;
}}
QPushButton#btnPrimary:hover {{ background: #4079F0; }}
QPushButton#btnPrimary:disabled {{ background: #A9BEDF; }}
QLineEdit#field {{
    background: {C_PANEL}; border: 1px solid {C_BORDER};
    border-radius: {RADIUS_CTL}px; padding: 6px 10px; color: {C_TEXT};
}}
QLineEdit#field:focus {{ border: 1px solid {C_ACCENT}; }}
QComboBox#field {{
    background: {C_PANEL}; border: 1px solid {C_BORDER};
    border-radius: {RADIUS_CTL}px; padding: 6px 10px; color: {C_TEXT};
    min-width: 130px;
}}
QComboBox#field:focus {{ border: 1px solid {C_ACCENT}; }}
QComboBox#field::drop-down {{ border: none; width: 22px; }}
QComboBox#field QAbstractItemView {{
    background: {C_PANEL}; border: 1px solid {C_BORDER};
    selection-background-color: {C_ACCENT_BG};
    selection-color: {C_TEXT}; outline: none;
}}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: #D3D9E0; border-radius: 5px; min-height: 36px;
}}
QScrollBar::handle:vertical:hover {{ background: #BCC4CE; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QLabel#feedback {{ color: {C_DIM}; background: transparent; }}
QLabel#feedback[state="ok"] {{ color: {C_OK}; }}
QLabel#feedback[state="err"] {{ color: {C_DANGER}; }}
""")


# ---------------------------------------------------------------- 小工具
# QColor 原生只认 #hex / 命名色，不认 CSS 的 rgb()/rgba() 写法；
# 解析失败会得到「无效色」，QPainter 拿去画就是黑色（提示语变黑正是此坑）。
_RGBA_RE = re.compile(
    r"^rgba?\(\s*([^,]+?)\s*,\s*([^,]+?)\s*,\s*([^,]+?)\s*"
    r"(?:,\s*([0-9.]+)\s*)?\)$")


def parse_color(s, fallback=None):
    """把颜色字符串解析成 QColor，支持 #hex / 命名色 / CSS rgb()/rgba()。

    解析失败返回 fallback（可能是 None）。
    """
    s = str(s or "").strip()
    if not s:
        return fallback
    c = QColor(s)
    if c.isValid():
        return c
    m = _RGBA_RE.match(s.lower())
    if m:
        try:
            rgb = []
            for part in m.group(1, 2, 3):
                v = float(part[:-1]) * 2.55 if part.endswith("%") \
                    else float(part)
                rgb.append(max(0, min(255, int(round(v)))))
            alpha = 255
            if m.group(4) is not None:
                av = float(m.group(4))
                if av <= 1.0:
                    alpha = int(round(max(0.0, min(1.0, av)) * 255))
                else:
                    alpha = max(0, min(255, int(round(av))))
            out = QColor(rgb[0], rgb[1], rgb[2])
            out.setAlpha(alpha)
            return out
        except Exception:
            pass
    return fallback


def with_alpha(hex_color: str, alpha: int) -> QColor:
    """#RRGGBB + 0~255 alpha -> QColor"""
    c = QColor(hex_color)
    c.setAlpha(alpha)
    return c


def mmss(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    return "%02d:%02d" % (s // 60, s % 60)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))
