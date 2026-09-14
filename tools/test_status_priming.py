"""验证：监控启动时已有状态文件 -> 不触发动作；新写入的状态 -> 正常触发。"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.statusfile import StatusFile, StatusMonitor

tmp = Path(tempfile.gettempdir()) / "ailock_priming_test.json"
tmp.unlink(missing_ok=True)

events = []
mon = StatusMonitor(str(tmp), poll_seconds=0.5, stale_minutes=0,
                    on_event=lambda kind, data: events.append((kind, data)),
                    on_snapshot=None)

# 场景 1：启动前就存在一个 failed 状态（模拟残留）
tmp.write_text(json.dumps({"task": "残留任务", "state": "failed",
                           "updated": time.time()}), encoding="utf-8")
mon.start()
time.sleep(2)
spurious = [e for e in events]
print(f"[场景1] 启动时有残留 failed 状态，2 秒内触发的事件: {spurious}")
assert not spurious, "失败：残留状态触发了误报动作！"
print("[场景1] 通过 ✅ 无误报")

# 场景 2：之后真正写入 done -> 应触发一次
sf = StatusFile(str(tmp))
sf.write("done", task="新任务", lines=["完成"])
time.sleep(2)
kinds = [k for k, _ in events]
print(f"[场景2] 写入 done 后触发的事件: {kinds}")
assert kinds.count("done") == 1, f"失败：应恰好触发一次 done，实际 {kinds}"
print("[场景2] 通过 ✅ 正确触发一次 done")

mon.stop()
tmp.unlink(missing_ok=True)
print("\n全部通过 ✅")
