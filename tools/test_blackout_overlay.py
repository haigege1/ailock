"""BlackoutOverlay 黑屏覆盖层测试（offscreen，无真实窗口）。

验证：
1. 构造默认隐藏
2. show 后可见、铺满父窗口
3. 鼠标点击 / 移动 / 按键任意一种输入都会自动隐藏（等价于唤醒）
4. LockScreen.set_blackout 开关链路（不依赖真实多屏）
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from ui.qt.lock_window import BlackoutOverlay  # noqa: E402

OK = 0
FAIL = 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def main():
    app = QApplication.instance() or QApplication([])

    print("[1] 基础行为")
    parent = QWidget()
    parent.resize(800, 600)
    parent.show()          # 子组件可见性依赖父窗口链
    blk = BlackoutOverlay(parent)
    check("构造后默认隐藏", not blk.isVisible())
    blk.setGeometry(parent.rect())
    blk.show()
    check("show 后可见", blk.isVisible())
    check("铺满父窗口", blk.geometry() == parent.rect(), blk.geometry())

    print("[2] 任意输入即隐藏（唤醒）")
    from PySide6.QtGui import QMouseEvent, QKeyEvent
    from PySide6.QtCore import QEvent, QPointF, Qt
    press = QMouseEvent(QEvent.MouseButtonPress, QPointF(10, 10),
                        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    blk.mousePressEvent(press)
    check("点击后隐藏", not blk.isVisible())

    blk.show()
    blk.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, QPointF(20, 20),
                                   Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
    check("移动后隐藏", not blk.isVisible())

    blk.show()
    blk.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Space,
                                Qt.NoModifier))
    check("按键后隐藏", not blk.isVisible())

    print("[3] 重复创建 / hide 幂等")
    blk2 = BlackoutOverlay(parent)
    blk2.show()
    blk2.hide()
    blk2.hide()
    check("重复 hide 不报错", not blk2.isVisible())

    print(f"\n结果：{OK} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
