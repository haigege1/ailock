"""会话卡尺寸测试：放大后仍不越界、不压密码卡与壁纸箭头。

    python tools/test_session_card_size.py

背景：用户反馈「会话卡太紧凑」，本轮把卡片整体放大（宽 300→380、
内边距 14/11→18/14、标题 12→13、日志 10→11、日志 2→3 行、卡间距
10→14）。放大最怕的是反过来压住居中的密码卡或右侧壁纸切换箭头，
所以这里把常见分辨率整套跑一遍，锁死三件事：

  1. 宽屏能吃到理想宽度（放大真的生效）
  2. 从 1024 到 3440 宽，卡片永远在「密码卡右缘」与「箭头左缘」之间
  3. 4 张满内容卡（3 行日志）在常见高度下不越出窗口上下边界
  4. 日志行数上限与单行截断长度生效
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)

from core.config import Config  # noqa: E402
from ui.qt.lock_window import LockWindow, SessionCard, SessionStack  # noqa: E402

failures = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def session(sid, task, state="running", progress=0.5, lines=None, age=0):
    return {"sid": sid, "task": task, "state": state, "progress": progress,
            "lines": lines or [], "updated": time.time() - age}


def full_cards(n=4):
    """满内容：3 行日志 + 进度，最高的情形。"""
    return [session(f"s{i}", f"任务{i}", progress=0.42,
                    lines=[f"第1行日志-{i}", f"第2行日志-{i}", f"第3行日志-{i}"])
            for i in range(n)]


cfg = Config()
cfg.set("background.mode", "color", autosave=False)
cfg.set("background.color", "#0B1220", autosave=False)

w = LockWindow(QApplication.primaryScreen(), cfg, None)
w.show()

# ============================================================ 1. 放大生效
w.resize(2560, 1440)
w.apply_status([session("a", "单个任务", lines=["第一行日志", "第二行日志"])])
app.processEvents()
c = [x for x in w.stack._cards if not x.isHidden()][0]
check("1. 2560 宽下取到理想宽度 380", c.width() == SessionCard.WIDTH,
      f"w={c.width()}")
check("2. 内边距已放大（左右各 18）",
      c.layout().contentsMargins().left() == 18
      and c.layout().contentsMargins().top() == 14,
      f"margins={c.layout().contentsMargins()}")
check("3. 卡间距已放大到 14", SessionStack.GAP == 14, f"gap={SessionStack.GAP}")
# 旧版（内边距 11/14、字号 12/10、2 行日志）实测 81px，这里用同样的
# 2 行数据对比，确认放大不是只改了宽度常量
check("4. 2 行日志的卡高 > 旧版 81px", c.height() > 88, f"h={c.height()}")

# ============================================================ 2. 分辨率矩阵
SIZES = [(1024, 768), (1280, 800), (1366, 768), (1440, 900),
         (1600, 900), (1920, 1080), (2560, 1440), (3440, 1440)]
bad_pw, bad_arrow, bad_bounds = [], [], []
for width, height in SIZES:
    w.resize(width, height)
    w.apply_status(full_cards())
    app.processEvents()
    cards = [x for x in w.stack._cards if not x.isHidden()]
    pw_right = w.card.x() + w.card.width()
    for c in cards:
        if c.x() < pw_right:
            bad_pw.append(f"{width}x{height}: x={c.x()} < pw_right={pw_right}")
        if c.x() + c.width() > w.arrowR.x():
            bad_arrow.append(f"{width}x{height}: right={c.x() + c.width()} "
                             f"> arrow={w.arrowR.x()}")
        if c.y() < 0 or c.y() + c.height() > height:
            bad_bounds.append(f"{width}x{height}: y={c.y()} "
                              f"bottom={c.y() + c.height()}/{height}")

check("5. 全分辨率下不压密码卡", not bad_pw, "; ".join(bad_pw[:3]))
check("6. 全分辨率下不压壁纸箭头", not bad_arrow, "; ".join(bad_arrow[:3]))
check("7. 全分辨率下 4 张满内容卡不越界", not bad_bounds,
      "; ".join(bad_bounds[:3]))

# ============================================================ 3. 高度压力
tight = []
for width, height in ((1024, 600), (1280, 720), (1920, 1080)):
    w.resize(width, height)
    w.apply_status(full_cards())
    app.processEvents()
    cards = [x for x in w.stack._cards if not x.isHidden()]
    if any(c.y() < 0 or c.y() + c.height() > height for c in cards):
        tight.append(f"{width}x{height}")
        continue
    if any(cards[i + 1].y() < cards[i].y() + cards[i].height()
           for i in range(len(cards) - 1)):
        tight.append(f"{width}x{height}(重叠)")
check("8. 矮窗口下 4 张卡既不越界也不重叠", not tight, f"bad={tight}")

# ============================================================ 4. 日志渲染
w.resize(2560, 1440)
long_line = "长" * 200
w.apply_status([session("x", "日志截断",
                        lines=[long_line, "第二行", "第三行", "第四行"])])
app.processEvents()
c = [x for x in w.stack._cards if not x.isHidden()][0]
shown = c.lines.text().split("\n")
check("9. 日志最多 3 行", len(shown) == SessionCard.MAX_LINES,
      f"lines={len(shown)}")
check("10. 单行截断到 76 字符",
      all(len(s) <= SessionCard.LINE_CHARS for s in shown),
      f"lens={[len(s) for s in shown]}")

# ============================================================ 5. 标题省略
w.resize(2560, 1440)
w.apply_status([session("long", "超长任务名" * 20)])
app.processEvents()
c = [x for x in w.stack._cards if not x.isHidden()][0]
check("11. 超长标题按新宽度省略（不撑破卡片）",
      c.title.width() <= c.width() and "…" in c.title.text(),
      f"title_w={c.title.width()} card_w={c.width()}")
check("12. 标题原文保留在 tooltip", c.title.toolTip() == "超长任务名" * 20)

print()
if failures:
    print(f"结果：{len(failures)} 项失败 -> {failures}")
    sys.exit(1)
print("结果：全部通过 ✓")
