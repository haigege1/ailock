"""会话卡「撤不下来」回归测试：tools\\test_status_expiry.py

背景（2026-09-24 实机问题）：
  锁屏右侧的会话卡一直挂着不消失。根因是钩子 Stop 时写 running，靠另起
  的后台确认进程翻 done，而宿主（WorkBuddy）用 Job Object 收进程树，钩子
  一退出就把这个子进程连坐杀掉 —— status.d 里所有文件永远停在 running，
  日志里 status.done 一条都没有。读取侧又没有 running 的过期策略，卡片
  就永远钉在屏幕上了。

覆盖：
  1. settle()：Stop 卡住的会话按确认窗口在读取侧补成 done
  2. is_expired()：waiting / watch_pid 永不过期；running、收尾各有 TTL
  3. cleanup_finished()：过期 running 文件、孤儿 .tmp 一起扫
  4. MultiStatusMonitor 端到端：僵尸会话不再抑制正常会话的 done
  5. 钩子写的会话文件没有 sid 字段时，stale 抑制标记仍能正确重置（补发）

用法:
    ..\\.venv\\Scripts\\python.exe tools\\test_status_expiry.py
    （或 managed python + PYTHONPATH 指向带 PySide6 的 site-packages；
      本文件不依赖 Qt）
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import statusdir as SD  # noqa: E402
from core.statusdir import (DONE_CONFIRM_SECONDS, FINISHED_TTL_SECONDS,
                            RUNNING_IDLE_TTL_SECONDS, MultiStatusMonitor,
                            cleanup_finished, is_expired, live_sessions,
                            settle)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def write_raw(path, data):
    """按钩子的写法落盘（原子替换，且不带 sid 字段）。"""
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, str(path))


def stop_card(sid, age, **extra):
    """模拟钩子 Stop 落下的会话文件内容。"""
    ts = time.time() - age
    data = {"task": "任务" + sid, "state": "running",
            "lines": ["你: 干活", "AI: 干完了", "✓ 本轮结束，等待确认…"],
            "progress": 0.5, "updated": ts, "turn_end_at": ts,
            "source": "workbuddy"}
    data.update(extra)
    return data


def main():
    print("== 1. settle：读取侧补记 done ==")
    now = time.time()
    d = settle(stop_card("A", DONE_CONFIRM_SECONDS + 5), now)
    check("Stop 过了确认窗口 -> done", d["state"] == "done", d)
    check("done 卡片进度补满", d["progress"] == 1, d)
    check("收尾行替换为「✓ 已完成」",
          d["lines"][-1] == "✓ 已完成" and not any(
              str(x).startswith("✓ 本轮") for x in d["lines"]), d["lines"])
    check("updated 保持 Stop 时刻（10 分钟窗口从本轮结束算起）",
          abs(float(d["updated"]) - float(d["turn_end_at"])) < 1e-6, d)

    raw = stop_card("B", 1)          # 确认窗口内
    check("确认窗口内不翻 done", settle(raw, now)["state"] == "running")
    raw2 = {"task": "x", "state": "running", "lines": [], "updated": now - 9999}
    check("没有 turn_end_at（老版本钩子）不动它",
          settle(raw2, now) == raw2)
    done = {"task": "x", "state": "done", "lines": [], "updated": now - 9999}
    check("已是 done 不重复处理", settle(done, now) is done)
    # Stop 之后又来了新事件：确认窗口从最后一次事件起算
    raw3 = dict(stop_card("C", 60))
    raw3["updated"] = now - 2
    check("Stop 后又有新事件 -> 再等窗口", settle(raw3, now)["state"] == "running")
    check("settle 不改原对象", raw is not None and raw["state"] == "running")

    print("== 2. is_expired ==")
    check("waiting 永不过期",
          not is_expired({"state": "waiting", "updated": now - 86400}, now))
    check("带 watch_pid 的 running 不过期",
          not is_expired({"state": "running", "updated": now - 86400,
                          "watch_pid": 1234}, now))
    check("新鲜 running 不过期",
          not is_expired({"state": "running", "updated": now - 5}, now))
    check("running 停更超 RUNNING_IDLE_TTL 过期",
          is_expired({"state": "running",
                      "updated": now - RUNNING_IDLE_TTL_SECONDS - 5}, now))
    check("running 未到收尾 TTL 不受影响（不算收尾过期）",
          not is_expired({"state": "running",
                          "updated": now - FINISHED_TTL_SECONDS - 5}, now))
    check("新鲜 done 不过期",
          not is_expired({"state": "done", "updated": now - 5}, now))
    check("done 超 FINISHED_TTL 过期",
          is_expired({"state": "done",
                      "updated": now - FINISHED_TTL_SECONDS - 5}, now))
    check("空数据视为过期", is_expired(None, now) and is_expired({}, now))

    print("== 2b. live_sessions（UI 的唯一入口）==")
    live = live_sessions([
        {"task": "在跑的", "state": "running", "updated": now - 5},
        {"task": "僵尸", "state": "running", "updated": now - 99999},
        {"task": "刚完成", "state": "done", "updated": now - 5},
        stop_card("Z", DONE_CONFIRM_SECONDS + 5),
        {"task": "等授权", "state": "waiting", "updated": now - 99999},
    ], now)
    check("剔除僵尸、保留在跑/新收尾/等授权",
          [d["task"] for d in live] == ["在跑的", "刚完成", "任务Z", "等授权"],
          [d["task"] for d in live])
    check("Stop 卡住的在这里收敛成 done",
          [d for d in live if d["task"] == "任务Z"][0]["state"] == "done")
    check("空输入不炸", live_sessions(None, now) == [])

    print("== 3. cleanup_finished ==")
    td = tempfile.mkdtemp(prefix="ailock-expiry-")
    sdir = os.path.join(td, SD.STATUS_DIR_NAME)
    os.makedirs(sdir)

    p_zombie = os.path.join(sdir, "zombie.json")
    write_raw(p_zombie, {"task": "僵尸", "state": "running",
                         "lines": [], "updated": now - 7200})
    p_alive = os.path.join(sdir, "alive.json")
    write_raw(p_alive, {"task": "在跑", "state": "running", "lines": [],
                        "updated": now - 5})
    p_stopped = os.path.join(sdir, "stopped.json")
    write_raw(p_stopped, stop_card("S", 7200))       # Stop 卡住的
    p_wait = os.path.join(sdir, "wait.json")
    write_raw(p_wait, {"task": "等授权", "state": "waiting", "lines": [],
                       "updated": now - 7200})
    p_tmp_old = os.path.join(sdir, "alive.json.abc123.tmp")
    write_raw(p_tmp_old, {"task": "半截"})
    os.utime(p_tmp_old, (now - 7200, now - 7200))
    p_tmp_new = os.path.join(sdir, "tmpfresh.tmp")
    write_raw(p_tmp_new, {"task": "刚写"})

    cleanup_finished(sdir, ttl=FINISHED_TTL_SECONDS, now=now)
    check("停更超时的 running 被清理", not os.path.exists(p_zombie))
    check("Stop 卡住且超期的被清理", not os.path.exists(p_stopped))
    check("在跑的会话文件保留", os.path.exists(p_alive))
    check("等人授权的会话文件保留", os.path.exists(p_wait))
    check("孤儿 .tmp 被清理", not os.path.exists(p_tmp_old))
    check("新鲜 .tmp 保留", os.path.exists(p_tmp_new))

    print("== 4. 端到端：僵尸会话不再吞掉 done ==")
    old_window = SD.DONE_CONFIRM_SECONDS
    SD.DONE_CONFIRM_SECONDS = 0.5          # 缩短确认窗口，测试跑得快
    # 单独开一个干净目录：别的用例会留下在跑的会话，会正常地抑制本次动作
    td3 = tempfile.mkdtemp(prefix="ailock-e2e-")
    sdir3 = os.path.join(td3, SD.STATUS_DIR_NAME)
    os.makedirs(sdir3)
    try:
        main_path = os.path.join(td3, "status.json")
        p_x = os.path.join(sdir3, "sess-x.json")
        p_y = os.path.join(sdir3, "sess-y.json")
        events, snaps = [], []
        mon = MultiStatusMonitor(
            main_path, poll_seconds=0.2, stale_minutes=0,
            on_event=lambda k, d: events.append(
                (k, str(d.get("task") or d.get("sid") or ""))),
            on_snapshot=lambda s: snaps.append([dict(x) for x in s]))
        mon.start()
        # 4.1 一轮进行中（UserPromptSubmit）：只该有 running
        write_raw(p_x, {"task": "任务X", "state": "running",
                        "lines": ["你: 干活"], "progress": 0.3,
                        "updated": time.time()})
        time.sleep(0.5)
        check("进行中不误报 done", events == [], events)

        # 4.2 Stop：钩子写 running + turn_end_at；同时目录里躺着一个 2 小时
        #     没动静的僵尸会话（老版本钩子写的，没有 turn_end_at）
        write_raw(p_y, {"task": "僵尸会话", "state": "running",
                        "lines": ["✓ 本轮结束，等待确认…"],
                        "updated": time.time() - 7200})
        write_raw(p_x, stop_card("X", 0))
        time.sleep(0.3)
        check("确认窗口内先不翻 done",
              not any(k == "done" for k, _ in events), events)

        # 4.3 窗口过完：没有新事件 -> 读取侧补记 done
        time.sleep(0.7)
        kinds = [k for k, _ in events]
        check("Stop 卡住的会话补发 done", "done" in kinds, events)
        last = snaps[-1]
        x = [s for s in last if s.get("task") == "任务X"]
        check("快照里该会话已是 done",
              bool(x) and x[0]["state"] == "done", last)
        check("停更超时的僵尸会话不再进快照",
              all(s.get("task") != "僵尸会话" for s in last), last)
        check("僵尸会话不算「仍在运行」（否则会吞掉动作）",
              mon._other_running("另一个会话") is False)
        mon.stop()
    finally:
        SD.DONE_CONFIRM_SECONDS = old_window
    check("确认窗口默认 12 秒（与钩子 AILOCK_DONE_DELAY 一致）",
          DONE_CONFIRM_SECONDS == 12.0, DONE_CONFIRM_SECONDS)

    print("== 5. 无 sid 字段的钩子文件：stale 抑制标记仍能重置 ==")
    td2 = tempfile.mkdtemp(prefix="ailock-stale-")
    sdir2 = os.path.join(td2, SD.STATUS_DIR_NAME)
    os.makedirs(sdir2)
    main2 = os.path.join(td2, "status.json")
    p_a = os.path.join(sdir2, "sess-a.json")
    p_b = os.path.join(sdir2, "sess-b.json")
    write_raw(p_a, {"task": "会话A", "state": "running", "lines": [],
                    "updated": time.time() - 60})
    write_raw(p_b, {"task": "会话B", "state": "running", "lines": [],
                    "updated": time.time()})
    events2 = []
    mon2 = MultiStatusMonitor(main2, poll_seconds=0.2, stale_minutes=0.01,
                              on_event=lambda k, d: events2.append(
                                  (k, str(d.get("sid") or d.get("task")))),
                              on_snapshot=None)
    mon2.start()
    time.sleep(1.5)
    events2.clear()
    write_raw(p_b, {"task": "会话B", "state": "done", "lines": [],
                    "updated": time.time()})
    time.sleep(1.5)
    check("B 收尾后 A 的 stale 补发（无 sid 字段也要成立）",
          any(k == "stale" and v.startswith("会话A") for k, v in events2),
          events2)
    mon2.stop()

    print(f"\n结果: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
