"""提示语颜色：parse_color 单测 + MarqueeLabel 默认色 + LockScreen 配置链路。

覆盖历史 bug：QColor("rgba(...)") 不认 CSS 写法 → 无效色 → 画成黑色。
用法：
    .venv/Scripts/python.exe -u tools/test_msg_color.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from core.config import DEFAULT_CONFIG, Config
from ui.qt.theme import parse_color

app = QApplication(sys.argv)

fails = []


def check(name, cond, detail=""):
    tag = "ok" if cond else "FAIL"
    print("  [%s] %s %s" % (tag, name, detail))
    if not cond:
        fails.append(name)


print("== parse_color ==")
c = parse_color("#FF8040")
check("#hex", c.isValid() and (c.red(), c.green(), c.blue()) == (255, 128, 64))

c = parse_color("red")
check("named color", c.isValid() and c.red() == 255 and c.green() == 0)

c = parse_color("rgba(242,246,252,.82)")
check("rgba() rgb", c.isValid()
      and (c.red(), c.green(), c.blue()) == (242, 246, 252),
      f"got {(c.red(), c.green(), c.blue()) if c.isValid() else 'invalid'}")
check("rgba() alpha .82 -> ~209", c.isValid() and abs(c.alpha() - 209) <= 1,
      "alpha=%d" % c.alpha() if c.isValid() else "invalid")

c = parse_color("rgba(255,0,0,0.5)")
check("rgba() alpha 0.5 -> 128", c.isValid() and abs(c.alpha() - 128) <= 1)

c = parse_color("rgb(10, 20, 30)")
check("rgb()", c.isValid() and (c.red(), c.green(), c.blue()) == (10, 20, 30))

c = parse_color("rgba(100%, 0%, 0%, 1)")
check("rgba() percent", c.isValid() and c.red() == 255 and c.green() == 0)

fb = QColor("#123456")
check("empty -> fallback", parse_color("", fb) is fb)
check("garbage -> fallback", parse_color("not-a-color", fb) is fb)
check("None -> fallback", parse_color(None, fb) is fb)

print("== MarqueeLabel 默认色（bug 回归）==")
from ui.qt.widgets import MarqueeLabel
m = MarqueeLabel()
check("默认色有效（非黑）", m._color.isValid())
check("默认色是主题浅色", (m._color.red(), m._color.green(), m._color.blue())
      == (242, 246, 252),
      f"got {(m._color.red(), m._color.green(), m._color.blue())}")
m.setInk("hello", color="rgba(255,0,0,1)")
check("setInk rgba 颜色生效", m._color.isValid() and m._color.red() == 255)
m.setInk("hello", color="garbage")
check("setInk 非法颜色不污染", m._color.red() == 255)

print("== LockScreen 配置链路 ==")
import tempfile
from ui.qt.lock_window import LockScreen

with tempfile.TemporaryDirectory() as tmp:
    cfg = Config(path=os.path.join(tmp, "cfg.json"))
    cfg.set("message.text", "测试提示语", autosave=False)
    cfg.set("message.color", "#FFCC00", autosave=False)
    cfg.set("background.mode", "color", autosave=False)
    calls = []

    def try_unlock(pwd):
        calls.append(pwd)
        return False, ""

    screen = LockScreen(None, cfg, try_unlock)
    screen.show(0)
    check("windows created", len(screen.windows) >= 1)
    w = screen.windows[0]
    check("msg text", w.msg.text() == "测试提示语")
    check("msg color 走配置",
          (w.msg._color.red(), w.msg._color.green(), w.msg._color.blue())
          == (255, 204, 0),
          "got %s" % w.msg._color.name())
    screen.destroy()

    # 默认配置里应有 message.color / background.transition
    check("DEFAULT_CONFIG.message.color", "color" in DEFAULT_CONFIG["message"])
    check("DEFAULT_CONFIG.background.transition",
          DEFAULT_CONFIG["background"].get("transition") == "fade")

print()
if fails:
    print("FAILED: %d 项未通过 -> %s" % (len(fails), fails))
    sys.exit(1)
print("msg color tests: ALL OK")
