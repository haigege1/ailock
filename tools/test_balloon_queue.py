"""托盘提醒暂存队列测试（纯逻辑，不需要 Qt / 窗口）。

    python tools/test_balloon_queue.py

覆盖点：
1. 空队列 take() → None
2. 单条原样返回
3. 多条合并成一条摘要，只列最近 N 条，并提示「另有 X 条」
4. 摘要里只取正文第一行（多行正文会把气泡撑爆）
5. 同内容去重（网络看门狗 / 看门狗会重复报同一件事）
6. 超过上限丢最旧的，保留最近发生的
7. take() 之后队列清空，可以接着攒
8. 上限 / 摘要条数可配置，且至少为 1
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.balloon_queue import BalloonQueue  # noqa: E402

failures = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


# ---------------------------------------------------------------- 1. 空 / 单条
q = BalloonQueue()
check("1. 空队列 take() → None", q.take() is None)
q.add("任务完成", "重构订单模块")
check("2. 入队后长度为 1", len(q) == 1, f"len={len(q)}")
check("3. 单条原样返回", q.take() == ("任务完成", "重构订单模块"))
check("4. take() 后清空", len(q) == 0 and q.take() is None)

# ---------------------------------------------------------------- 2. 多条合并
q = BalloonQueue()
for i in range(3):
    q.add(f"标题{i}", f"正文{i}")
title, body = q.take()
check("5. 多条 → 标题带总数", title == "3 条未读提醒", f"title={title}")
check("6. 多条 → 正文逐条列出",
      body.splitlines() == ["· 标题0：正文0", "· 标题1：正文1", "· 标题2：正文2"],
      repr(body))

# ---------------------------------------------------------------- 3. 超出摘要上限
q = BalloonQueue(summary_max=2)
for i in range(7):
    q.add(f"会话{i}", f"任务{i}")
title, body = q.take()
check("7. 超出上限只列最近 2 条", len(body.splitlines()) == 3, repr(body))
check("8. 末行提示剩余条数", body.splitlines()[-1] == "…另有 5 条",
      body.splitlines()[-1])
check("9. 保留的是最近的（会话5/会话6）",
      "会话5" in body and "会话6" in body and "会话0" not in body, repr(body))

# ---------------------------------------------------------------- 4. 多行正文
q = BalloonQueue()
q.add("网络异常", "连续多次探测失败，\n主机可能已断网。\n请检查网络。")
q.add("第二条", "正文")
title, body = q.take()
check("10. 摘要只取正文第一行",
      body.splitlines()[0] == "· 网络异常：连续多次探测失败，", repr(body))

# ---------------------------------------------------------------- 5. 去重
q = BalloonQueue()
q.add("重复", "同一件事")
q.add("重复", "同一件事")
q.add("重复", "同一件事")
check("11. 完全相同的条目只留一条", len(q) == 1, f"len={len(q)}")
q.add("重复", "同一件事，但正文不同")
check("12. 正文不同则不算重复", len(q) == 2, f"len={len(q)}")

# ---------------------------------------------------------------- 6. 上限保护
q = BalloonQueue(max_items=3)
for i in range(50):
    q.add(f"t{i}", f"m{i}")
check("13. 队列长度不超过上限", len(q) == 3, f"len={len(q)}")
title, body = q.take()
check("14. 溢出时保留最近 3 条",
      "t49" in body and "t47" in body and "t46" not in body, repr(body))

# ---------------------------------------------------------------- 7. 连续攒两轮
q = BalloonQueue()
q.add("第一轮", "a")
q.take()
q.add("第二轮", "b")
check("15. take() 后可继续攒", q.take() == ("第二轮", "b"))

# ---------------------------------------------------------------- 8. 参数下限
q = BalloonQueue(max_items=0, summary_max=0)
for i in range(5):
    q.add(f"t{i}", f"m{i}")
check("16. max_items / summary_max 至少为 1（不崩、不空摘要）",
      len(q) == 1 and len(q.take()[1].splitlines()) == 1, f"len={len(q)}")

# ---------------------------------------------------------------- 9. clear
q = BalloonQueue()
q.add("a", "b")
q.clear()
check("17. clear() 清空", len(q) == 0 and q.take() is None)

print()
if failures:
    print(f"结果：{len(failures)} 项失败 -> {failures}")
    sys.exit(1)
print("结果：全部通过 ✓")
