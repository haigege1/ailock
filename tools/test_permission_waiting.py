"""waiting（等待授权）状态全链路测试：tools\\test_permission_waiting.py

覆盖：
  1. StatusFile 写 waiting 不被回落成 idle
  2. 钩子脚本收到 PermissionRequest 事件 -> status.json state=waiting
  3. waiting 会话不被过期清理（cleanup_finished）
  4. pick_primary 把 waiting 会话当活跃会话
  5. waiting 会话抑制其他会话的 done 触发（_other_running）
  6. 钩子确认进程不把 waiting 覆盖成 done

用法:
    ..\\.venv\\Scripts\\python.exe tools\\test_permission_waiting.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.statusdir import (STATUS_DIR_NAME, MultiStatusMonitor,
                            SessionStatusFile, cleanup_finished,
                            pick_primary)
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
    td = tempfile.mkdtemp(prefix="ailock-waiting-")
    main_path = os.path.join(td, "status.json")
    sdir = os.path.join(td, STATUS_DIR_NAME)
    os.makedirs(sdir)

    hook = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))),
        "integrations", "zcode", "ailock_zcode_hook.py")

    print("== 1. StatusFile waiting 持久化 ==")
    sf = StatusFile(main_path)
    sf.write("waiting", task="测试任务")
    data = sf.read()
    check("state=waiting 不回落 idle", data and data.get("state") == "waiting",
          data)

    print("== 2. 钩子 PermissionRequest -> waiting ==")
    appdata = os.path.join(td, "appdata")
    env = dict(os.environ, APPDATA=appdata)
    ev = {"hook_event_name": "PermissionRequest",
          "tool_name": "Bash", "tool_input": {"command": "pytest -q"},
          "session_id": "test-sid"}
    r = subprocess.run([sys.executable, hook], input=json.dumps(ev).encode(),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=30, env=env)
    check("钩子退出码 0", r.returncode == 0,
          r.stderr.decode("utf-8", "replace")[:300])
    sfile = os.path.join(appdata, "AiLock", "status.d", "test-sid.json")
    raw = json.loads(open(sfile, encoding="utf-8").read())
    check("state=waiting", raw.get("state") == "waiting", raw)
    check("动作行带编号与描述",
          any("等待授权" in l and "pytest" in l for l in raw.get("lines") or []),
          raw.get("lines"))

    print("== 3. waiting 不被过期清理 ==")
    old = time.time() - 7200
    sfa = SessionStatusFile("w-sess", os.path.join(sdir, "w-sess.json"))
    sfa.write("waiting", task="等人授权")
    os.utime(sfa.path, (old, old))
    # 直接种时间戳字段（updated 在文件内，比 mtime 可靠）
    raw = json.loads(open(sfa.path, encoding="utf-8").read())
    raw["updated"] = old
    json.dump(raw, open(sfa.path, "w", encoding="utf-8"),
              ensure_ascii=False)
    cleanup_finished(sdir)
    check("waiting 文件保留", os.path.exists(sfa.path))
    sfb = SessionStatusFile("d-sess", os.path.join(sdir, "d-sess.json"))
    sfb.write("done", task="已收尾")
    raw = json.loads(open(sfb.path, encoding="utf-8").read())
    raw["updated"] = old
    json.dump(raw, open(sfb.path, "w", encoding="utf-8"),
              ensure_ascii=False)
    cleanup_finished(sdir)
    check("done 文件被清理", not os.path.exists(sfb.path))

    print("== 4. pick_primary 偏好 waiting ==")
    card = pick_primary([
        {"sid": "a", "state": "running", "updated": time.time()},
        {"sid": "b", "state": "waiting", "updated": time.time() - 5},
        {"sid": "c", "state": "done", "updated": time.time()},
    ])
    check("waiting 排最前", card and card.get("sid") == "b", card)

    print("== 5. waiting 抑制其他会话 done ==")
    events = []
    mon = MultiStatusMonitor(main_path, poll_seconds=0.3, stale_minutes=0,
                             on_event=lambda k, d: events.append(k))
    mon.start()
    time.sleep(0.8)   # 基线
    sw = SessionStatusFile("w2", os.path.join(sdir, "w2.json"))
    sd = SessionStatusFile("d2", os.path.join(sdir, "d2.json"))
    sw.write("waiting", task="授权中")
    time.sleep(0.6)
    sd.write("done", task="先收尾")
    time.sleep(0.8)
    check("d2 done 被抑制（w2 在等授权）", events == [], events)
    mon.stop()

    print("== 6. 确认进程不覆盖 waiting ==")
    env2 = dict(os.environ, APPDATA=appdata,
                AILOCK_DONE_DELAY="0.5")
    # 先让 Stop 写 running + 生成确认基准，再立刻插一个 PermissionRequest
    p1 = subprocess.Popen(
        [sys.executable, hook], stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env2)
    p1.communicate(json.dumps({
        "hook_event_name": "Stop", "stop_hook_active": False,
        "session_id": "test-sid2"}).encode())
    ev = {"hook_event_name": "PermissionRequest",
          "tool_name": "Write", "tool_input": {"file_path": "x.py"},
          "session_id": "test-sid2"}
    subprocess.run([sys.executable, hook], input=json.dumps(ev).encode(),
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   timeout=30, env=env2)
    time.sleep(1.5)   # 等 --confirm-done 到点
    sfile2 = os.path.join(appdata, "AiLock", "status.d", "test-sid2.json")
    raw = json.loads(open(sfile2, encoding="utf-8").read())
    check("waiting 未被翻 done", raw.get("state") == "waiting", raw)

    print()
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
