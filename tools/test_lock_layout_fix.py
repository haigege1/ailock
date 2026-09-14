"""锁屏布局回归：会话卡改为右侧覆盖层后，中央布局不再被挤压。

用 offscreen 平台跑，不弹真实全屏窗口：
    python tools/test_lock_layout_fix.py

历史问题：旧版把任务卡放在中央 QVBoxLayout 里，卡片显隐会让整列上下
位移 ~110px，与入场动画撞车 → 「刚锁屏时元素重叠、提示语被遮」。
现在会话卡是绝对定位覆盖层（SessionStack），本测试锁死这个行为。

覆盖点：
1. 无状态时会话堆叠隐藏
2. 有会话时按会话数出卡，卡片落在窗口右半边
3. 关键回归：会话卡 0 → 4 张，密码卡 / 时钟的几何完全不变
4. 清空状态后堆叠隐藏
5. msg 提示语高度与跑马灯行为不回归
6. LockScreen(initial_status=...) 在 show 之前完成状态应用
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)

from core.config import Config  # noqa: E402
from ui.qt.lock_window import LockScreen, LockWindow  # noqa: E402

failures = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def session(sid, task, state="running", progress=None, lines=None, age=0):
    return {"sid": sid, "task": task, "state": state, "progress": progress,
            "lines": lines or [], "updated": time.time() - age}


def visible_cards(win):
    """offscreen 下 isVisible() 不可靠，用 isHidden() 判断显隐意图。"""
    return [c for c in win.stack._cards if not c.isHidden()]


cfg = Config()
cfg.set("message.text", "AI 大人正在工作，请勿关机或合盖", autosave=False)
cfg.set("background.mode", "color", autosave=False)
cfg.set("background.color", "#0B1220", autosave=False)

screen = QApplication.primaryScreen()

# ---- 单窗口行为 ----
w = LockWindow(screen, cfg, None)
w.resize(1440, 900)     # offscreen 默认屏幕偏小，贴近真实分辨率再测排版
w.show()
app.processEvents()

check("1. 初始会话堆叠隐藏", w.stack.isHidden())
check("1b. 初始无可见卡片", len(visible_cards(w)) == 0,
      f"cards={len(visible_cards(w))}")

one = session("s1", "definitely_not_a_cmd_xyz", state="failed",
              lines=["命令不存在: definitely_not_a_cmd_xyz"])
w.apply_status(one)
app.processEvents()
check("2. 单会话 → 堆叠可见且 1 张卡",
      (not w.stack.isHidden()) and len(visible_cards(w)) == 1,
      f"cards={len(visible_cards(w))}")
card = visible_cards(w)[0]
check("3. 卡片标题已设置", card._title_raw == "definitely_not_a_cmd_xyz",
      card._title_raw)
check("4. 卡片落在窗口右半边", card.geometry().x() > w.width() // 2,
      f"x={card.geometry().x()} win_w={w.width()}")

# ---- 关键回归：覆盖层不参与中央布局 ----
before = (w.card.geometry(), w.clock.geometry())
four = [session(f"s{i}", f"任务{i}", progress=0.3, lines=["line-a", "line-b"])
        for i in range(4)]
w.apply_status(four)
app.processEvents()
after = (w.card.geometry(), w.clock.geometry())
check("5. 会话卡 0→4 张，密码卡几何零位移", before[0] == after[0],
      f"{before[0]} -> {after[0]}")
check("5b. 会话卡 0→4 张，时钟几何零位移", before[1] == after[1],
      f"{before[1]} -> {after[1]}")
check("5c. 4 张卡全部渲染", len(visible_cards(w)) == 4,
      f"cards={len(visible_cards(w))}")

w.apply_status([])
app.processEvents()
check("6. 清空状态 → 堆叠隐藏", w.stack.isHidden())

# ---- 提示语 / 跑马灯不回归 ----
check("7. msg 固定单行高度", w.msg.height() == 26, f"h={w.msg.height()}")
w.set_message("AI 大人正在工作，请勿关机或合盖")
w.layout().activate()       # setInk 触发的重排是惰性的，同步测试需手动激活
check("8. msg 文字已设置", "请勿关机" in w.msg.text())
check("8b. msg 占满整行宽度", w.msg.width() > 200, f"msg_w={w.msg.width()}")
check("9a. 正常文案不滚动", not w.msg._scrolling)
w.resize(200, 800)          # 把窗口压窄，逼文字超宽
w.set_message("这是一条特别特别特别长的自定义提示语文案" * 3)
check("9b. 超长文案触发滚动", w.msg._scrolling,
      f"text_w={w.msg._text_w:.0f} win_w={w.width()}")

# ---- 管理器：initial_status 预应用 ----
attempts = []


def try_unlock(pwd):
    attempts.append(pwd)
    return False, "no"


ls = LockScreen(None, cfg, try_unlock, initial_status=one)
check("10. LockScreen 接受 initial_status",
      bool(ls._last_status)
      and ls._last_status[0].get("task") == "definitely_not_a_cmd_xyz")
check("11. show 之前会话卡已渲染",
      all(len(visible_cards(win)) == 1 for win in ls.windows),
      f"windows={len(ls.windows)}")
ls.show(0)
check("12. show 后会话卡保持可见",
      all(not win.stack.isHidden() for win in ls.windows))
ls.destroy()

# 无 initial_status 的老路径不回归
ls2 = LockScreen(None, cfg, try_unlock)
check("13. 无 initial_status 时堆叠保持隐藏",
      all(win.stack.isHidden() for win in ls2.windows))
ls2.destroy()

print()
if failures:
    print(f"结果：{len(failures)} 项失败 -> {failures}")
    sys.exit(1)
print("结果：全部通过 ✓")
