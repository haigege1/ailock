"""多会话 UI 测试：右侧会话卡堆叠（offscreen，不弹真实窗口）。

    python tools/test_session_stack_ui.py

覆盖点：
1. 一会话一卡：N 个会话 → N 张卡，纵向排列互不重叠
2. 超过 MAX_CARDS 的会话截断
3. 排序：在跑的优先，其次按 updated 倒序（最新在前）
4. 收尾超过 TTL（10 分钟）的会话不再显示；在跑的会话不受 TTL 限制
5. 底部状态栏：单会话显示任务名，多会话显示「X 等 N 个会话」
6. 卡片不与壁纸切换箭头、居中密码卡重叠
7. normalize_sessions / pick_primary 的边界（None / dict / list / 脏数据）
8. LockScreen.update_status(list) 同步到所有显示器窗口
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)

from core.config import Config  # noqa: E402
from core.statusdir import (FINISHED_TTL_SECONDS, normalize_sessions,
                            pick_primary)
from ui.qt.lock_window import (STATE_COLORS, LockScreen, LockWindow,
                               SessionStack)
from ui.qt.lock_window import SessionCard  # noqa: E402

failures = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def session(sid, task, state="running", progress=None, lines=None, age=0):
    return {"sid": sid, "task": task, "state": state, "progress": progress,
            "lines": lines or [], "updated": time.time() - age}


def shown_titles(win):
    return [c._title_raw for c in win.stack._cards if not c.isHidden()]


cfg = Config()
cfg.set("background.mode", "color", autosave=False)
cfg.set("background.color", "#0B1220", autosave=False)

w = LockWindow(QApplication.primaryScreen(), cfg, None)
w.resize(1440, 900)
w.show()
app.processEvents()

# ============================================================ 1. 一会话一卡
w.apply_status([
    session("zc-aaa", "重构订单模块", progress=0.2, lines=["读取 12 个文件"]),
    session("zc-bbb", "写单元测试", progress=0.8,
            lines=["test_order.py", "通过 7 / 9"]),
    session("run-123", "训练 LoRA", progress=45,
            lines=["epoch 3/10", "loss 1.24"]),
])
app.processEvents()
cards = [c for c in w.stack._cards if not c.isHidden()]
check("1. 三个会话 → 三张卡", len(cards) == 3, f"cards={len(cards)}")
check("2. 卡片纵向不重叠",
      all(cards[i + 1].y() >= cards[i].y() + cards[i].height()
          for i in range(len(cards) - 1)),
      f"ys={[c.y() for c in cards]}")
check("3. 进度归一化（45 → 0.45）",
      abs(cards[0].progress._value - 45 / 100) < 1e-6
      or abs(cards[1].progress._value - 45 / 100) < 1e-6,
      f"vals={[round(c.progress._value, 3) for c in cards]}")

# ============================================================ 2. 超出截断
six = [session(f"s{i}", f"任务{i}") for i in range(6)]
w.apply_status(six)
app.processEvents()
check("4. 超过 MAX_CARDS 截断到 4 张",
      len(shown_titles(w)) == SessionStack.MAX_CARDS,
      f"shown={len(shown_titles(w))}")

# ============================================================ 3. 排序规则
w.apply_status([
    session("a", "A 已完成", state="done", age=5),
    session("b", "B 运行中", state="running", age=50),
    session("c", "C 运行中", state="running", age=1),
])
app.processEvents()
check("5. 在跑的优先 + 组内按 updated 倒序",
      shown_titles(w) == ["C 运行中", "B 运行中", "A 已完成"],
      f"order={shown_titles(w)}")

# ============================================================ 4. TTL 过期
w.apply_status([
    session("old", "很久前完成", state="done", age=FINISHED_TTL_SECONDS + 100),
    session("stuck", "卡住的在跑任务", state="running",
            age=FINISHED_TTL_SECONDS * 12),
    session("fresh", "刚完成", state="done", age=5),
])
app.processEvents()
titles = shown_titles(w)
check("6. 过期收尾会话被过滤", "很久前完成" not in titles, f"titles={titles}")
check("7. 在跑会话不受 TTL 限制", "卡住的在跑任务" in titles, f"titles={titles}")
check("8. 未过期收尾会话保留", "刚完成" in titles, f"titles={titles}")

# ============================================================ 5. 状态栏
w.apply_status([session("only", "唯一任务")])
app.processEvents()
check("9. 单会话 → 状态栏显示任务名", w.sbTask.text() == "唯一任务",
      w.sbTask.text())
check("10. 单会话 → 状态栏圆点为 running 色",
      w.sbDot._color.name().lower() == STATE_COLORS["running"].lower(),
      w.sbDot._color.name())

w.apply_status([session(f"m{i}", f"任务{i}") for i in range(3)])
app.processEvents()
check("11. 多会话 → 状态栏显示会话总数", "等 3 个会话" in w.sbTask.text(),
      w.sbTask.text())

w.apply_status([])
app.processEvents()
check("12. 空快照 → 状态栏回落空闲", w.sbTask.text() == "空闲", w.sbTask.text())

# ============================================================ 6. 不遮挡
w.apply_status([session(f"p{i}", f"任务{i}", progress=0.5,
                        lines=["第一行", "第二行"]) for i in range(4)])
app.processEvents()
cards = [c for c in w.stack._cards if not c.isHidden()]
right_edges = [c.x() + c.width() for c in cards]
check("13. 卡片不压住壁纸切换箭头",
      max(right_edges) <= w.arrowR.x(),
      f"card_right={max(right_edges)} arrow_x={w.arrowR.x()}")
check("14. 卡片不压住居中密码卡",
      min(c.x() for c in cards) >= w.card.x() + w.card.width(),
      f"card_x={min(c.x() for c in cards)} pw_right={w.card.x() + w.card.width()}")
check("15. 卡片完整落在窗口内",
      all(c.y() >= 0 and c.y() + c.height() <= w.height() for c in cards),
      f"ys={[(c.y(), c.height()) for c in cards]} h={w.height()}")

# ============================================================ 7. 聚合辅助
check("16. normalize_sessions(None) → []", normalize_sessions(None) == [])
check("17. normalize_sessions(dict) → [dict]",
      normalize_sessions({"sid": "x"}) == [{"sid": "x"}])
check("18. normalize_sessions 过滤脏数据",
      normalize_sessions([{"sid": "ok"}, None, "", 3]) == [{"sid": "ok"}])
check("19. pick_primary 优先最新在跑的",
      pick_primary([session("d", "已完成", state="done", age=1),
                    session("r", "在跑的", state="running", age=30)]).get("sid")
      == "r")
check("20. pick_primary 都不在跑时取最新收尾的",
      pick_primary([session("old", "旧", state="done", age=90),
                    session("new", "新", state="failed", age=2)]).get("sid")
      == "new")
check("21. pick_primary(空) → None", pick_primary([]) is None
      and pick_primary(None) is None)

# ============================================================ 8. 多屏同步
attempts = []
ls = LockScreen(None, cfg, lambda p: (False, "no"))
ls.show(0)
ls.update_status([session("w1", "会话一"), session("w2", "会话二",
                                                 state="done")])
app.processEvents()
check("22. 每个显示器窗口都同步了会话列表",
      len(ls.windows) > 0
      and all(len(shown_titles(win)) == 2 for win in ls.windows),
      f"windows={len(ls.windows)} titles={[shown_titles(x) for x in ls.windows]}")
ls.destroy()

# 窄屏收缩：卡片宽度自适应，不溢出窗口左边界
w.resize(1024, 768)
w.apply_status([session("n1", "窄屏任务一", progress=0.5, lines=["x"])])
app.processEvents()
cards = [c for c in w.stack._cards if not c.isHidden()]
check("23. 窄屏下卡片收窄且不越界",
      cards and cards[0].width() <= SessionCard.WIDTH and cards[0].x() >= 0,
      f"w={cards[0].width() if cards else -1} x={cards[0].x() if cards else -1}")

print()
if failures:
    print(f"结果：{len(failures)} 项失败 -> {failures}")
    sys.exit(1)
print("结果：全部通过 ✓")
