"""Bug 回归：Switch 点击后只能动 knob 位置，控件本身不能漂移。

widgets.py 的 Switch 之前用 QPropertyAnimation(self, b"pos", ...) 来动画化
开关里的滑块位置。但 QWidget 自带一个 pos 属性的类型是 QPoint，PySide6 在
某些路径下会把动画目标解析成 QWidget 的几何位置 —— 结果就是点击后整开关被
插值到一个莫名其妙的坐标（用户在设置页里看到的就是「漂移到右上角」）。

修复：knob 属性改名，改动 QPropertyAnimation 目标到 b"knob"，从根上避开。

本测试做三件事：
1. 构造 Switch(False)，挂在一个 fixed-position 的容器里
2. 记下 widget 几何 → click 触发 toggle → 等动画结束
3. 几何不能变；_pos 必须平滑动到 1.0
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QPoint, QRect, Qt, QTimer
from PySide6.QtWidgets import QApplication, QWidget

from ui.qt.widgets import Switch

app = QApplication(sys.argv)

fails = []


def check(name, cond, detail=""):
    tag = "ok" if cond else "FAIL"
    print("  [%s] %s %s" % (tag, name, detail))
    if not cond:
        fails.append(name)


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


# ---- 用一个固定布局把 Switch 摆在已知坐标，模拟设置页一行 ----
class Frame(QWidget):
    def __init__(self):
        super().__init__()
        self.setGeometry(0, 0, 600, 60)


parent = Frame()
parent.show()

sw = Switch(False, parent)
# 让布局把它摆在已知点：右侧，距上 17px
sw.move(540, 17)
sw.show()
wait(50)

orig_geom = QRect(sw.geometry())
orig_pos_attr = sw.pos()
orig__pos = sw._pos

print("orig geom=%s pos-attr=%s _pos=%.3f"
      % (orig_geom, orig_pos_attr, orig__pos))

# 触发一次 toggle 模拟用户点击
sw.toggle()

# 抓个动画中间帧，确认 _pos 在平滑插值（但 widget 几何仍没动）
wait(80)
mid_geom = QRect(sw.geometry())
mid_pos = sw.pos()
mid__pos = sw._pos
print("mid   geom=%s pos-attr=%s _pos=%.3f"
      % (mid_geom, mid_pos, mid__pos))

# 等动画走完
wait(250)

final_geom = QRect(sw.geometry())
final_pos = sw.pos()
final__pos = sw._pos
print("final geom=%s pos-attr=%s _pos=%.3f"
      % (final_geom, final_pos, final__pos))


# ----- 断言 -----
# 1) 整控件几何全程不变
check("orig→mid 几何一致", mid_geom == orig_geom,
      "%s vs %s" % (orig_geom, mid_geom))
check("orig→final 几何一致", final_geom == orig_geom,
      "%s vs %s" % (orig_geom, final_geom))

# 2) QWidget.pos 全程不变（这是关键回归点）
check("orig→mid QWidget.pos 一致", mid_pos == orig_pos_attr,
      "%s vs %s" % (orig_pos_attr, mid_pos))
check("orig→final QWidget.pos 一致", final_pos == orig_pos_attr,
      "%s vs %s" % (orig_pos_attr, final_pos))

# 3) _pos 从 0 动到接近 1，没被 QWidget.pos 错误截走
check("_pos 起始 0", abs(orig__pos) < 1e-6, "_pos=%.3f" % orig__pos)
check("_pos 终态接近 1", final__pos > 0.95, "_pos=%.3f" % final__pos)
check("动画中段 _pos 在 (0,1) 间", 0.0 < mid__pos < 1.0,
      "_pos=%.3f" % mid__pos)

# 4) 再 toggle 回来，_pos 必须回到 0（双向无残留）
sw.toggle()
wait(250)
check("再 toggle 回 _pos≈0", sw._pos < 0.05,
      "_pos=%.3f" % sw._pos)
check("再 toggle 终态几何仍未动", QRect(sw.geometry()) == orig_geom,
      "%s vs %s" % (orig_geom, sw.geometry()))

# 5) 属性确实改名为 knob，不要再撞 QWidget.pos
knob_attr = Switch.__dict__.get("knob")
check("Switch 类上有 knob 属性", knob_attr is not None)
check("knob 是 PySide6 Property",
      type(knob_attr).__name__ == "Property")
check("Switch 类不再定义同名 pos 属性",
      "pos" not in Switch.__dict__)

parent.close()

print()
if fails:
    print("FAILED: %d -> %s" % (len(fails), fails))
    sys.exit(1)
print("switch animation: ALL OK")
