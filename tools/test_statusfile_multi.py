"""多会话状态目录聚合测试：tools\\test_statusfile_multi.py

覆盖：
  1. sanitize_sid 防路径穿越
  2. SessionStatusFile 写入带 sid、原子性
  3. 最后收尾的会话才触发动作（A done + B running -> 抑制；B done -> 触发）
  4. 重启（restarted）事件不抑制
  5. stale 被抑制后，其他会话收尾时补发
  6. 收尾会话文件过期清理

用法:
    ..\\.venv\\Scripts\\python.exe tools\\test_statusfile_multi.py
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.statusdir import (STATUS_DIR_NAME, MultiStatusMonitor,
                            SessionStatusFile, cleanup_finished,
                            sanitize_sid)
from core.statusfile import StatusFile

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


def main():
    td = tempfile.mkdtemp(prefix="ailock-multi-")
    main_path = os.path.join(td, "status.json")
    sdir = os.path.join(td, STATUS_DIR_NAME)
    os.makedirs(sdir)

    print("== 1. sanitize_sid ==")
    check("路径穿越被过滤", sanitize_sid("../../evil") == "evil")
    check("空值回落 default", sanitize_sid("") == "default")
    check("特殊字符剔除", sanitize_sid("a<b>c|d*.json") == "abcdjson")
    check("长度截断", len(sanitize_sid("x" * 200)) == 64)

    print("== 2. SessionStatusFile 写入 ==")
    sfa = SessionStatusFile("sess-A", os.path.join(sdir, "sess-A.json"))
    sfa.write("running", task="任务A", progress=0.5, watch_pid=99999)
    raw = json.loads(open(sfa.path, encoding="utf-8").read())
    check("sid 随原子写落盘", raw.get("sid") == "sess-A", raw)
    check("watch_pid 保留", raw.get("watch_pid") == 99999)
    sfa.write("running", task="任务A", merge=True)
    raw = json.loads(open(sfa.path, encoding="utf-8").read())
    check("merge 保留 pid 与 sid", raw.get("watch_pid") == 99999
          and raw.get("sid") == "sess-A", raw)

    print("== 3. 最后收尾触发 ==")
    sfb = SessionStatusFile("sess-B", os.path.join(sdir, "sess-B.json"))
    sfb.write("running", task="任务B")
    events, snaps = [], []
    mon = MultiStatusMonitor(main_path, poll_seconds=0.3, stale_minutes=0,
                             on_event=lambda k, d: events.append((k, dict(d))),
                             on_snapshot=lambda s: snaps.append(list(s)))
    mon.start()
    time.sleep(0.8)
    sfa.write("done", task="任务A")
    time.sleep(0.8)
    check("A done 被抑制（B 还在跑）", events == [], events)
    sfb.write("done", task="任务B")
    time.sleep(0.8)
    kinds = [(k, d.get("sid")) for k, d in events]
    check("B done 触发（最后收尾）", kinds == [("done", "sess-B")], kinds)
    check("快照包含 2 个会话", len(snaps[-1]) == 2, snaps[-1])
    check("快照 sid 齐全",
          {s["sid"] for s in snaps[-1]} == {"sess-A", "sess-B"})

    print("== 4. restarted 不抑制 ==")
    events.clear()
    sfb.write("running", task="任务B v2")
    time.sleep(0.8)
    check("restarted 立即触发（不抑制）",
          any(k == "restarted" for k, _ in events), events)
    mon.stop()

    print("== 5. stale 抑制后补发 ==")
    events.clear()
    sfa.write("running", task="任务A again")
    mon2 = MultiStatusMonitor(main_path, poll_seconds=0.3, stale_minutes=0.01,
                              on_event=lambda k, d: events.append((k, dict(d))),
                              on_snapshot=None)
    mon2.start()
    time.sleep(0.8)
    events.clear()
    sfb.write("running", task="任务B v3")   # B 活跃
    time.sleep(2.5)                          # A 停更超阈值
    check("A stale 被 running 的 B 抑制", events == [], events)
    sfb.write("done", task="任务B final")
    time.sleep(1.5)
    kinds2 = [(k, d.get("sid")) for k, d in events]
    check("B 收尾后 A 的 stale 补发", ("stale", "sess-A") in kinds2, kinds2)
    mon2.stop()

    print("== 6. 过期清理 ==")
    old = SessionStatusFile("sess-old", os.path.join(sdir, "sess-old.json"))
    old.write("done", task="old")
    data = json.loads(open(old.path, encoding="utf-8").read())
    data["updated"] = time.time() - 7200
    tmp = str(old.path) + ".tmp"
    open(tmp, "w", encoding="utf-8").write(json.dumps(data))
    os.replace(tmp, old.path)
    cleanup_finished(sdir, ttl=600)
    check("过期收尾文件被清理", not os.path.exists(old.path))
    check("running 会话保留", os.path.exists(sfa.path))

    print(f"\n结果: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
