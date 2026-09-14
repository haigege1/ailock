"""集成测试：exe 的 --run 包装模式。

验证「不需要装 Python 也能跟 AI 工具联动」这条路径：
  - `--` 之后的参数原样传给被包装命令（不被主解析器吃掉）
  - 运行期状态文件是 running 且带 watch_pid
  - 命令成功 → done，退出码 0 透传
  - 命令失败 → failed，退出码原样透传
  - 常驻实例能收到跃迁并触发配置里的动作
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
EXE = HERE / "dist" / "AiLock.exe"
PY = Path(sys.executable)
APPDATA_DIR = Path.home() / "AppData" / "Roaming" / "AiLock"
LOG = APPDATA_DIR / "ailock.log"
CONFIG = APPDATA_DIR / "config.json"
STATUS = APPDATA_DIR / "status.json"

PASS, FAIL = 0, 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [通过] {label}")
    else:
        FAIL += 1
        print(f"  [失败] {label}" + (f"\n         {detail}" if detail else ""))
    return ok


def kill_all():
    subprocess.run(["taskkill", "/F", "/IM", "AiLock.exe"], capture_output=True)
    time.sleep(1.2)


def log_lines():
    try:
        return LOG.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []


def fired(lines, event, needle=""):
    n = 0
    for ln in lines:
        head, _sep, detail = ln.partition("  |  ")
        if head.split("  ")[-1].strip() != event:
            continue
        if not needle or needle in detail:
            n += 1
    return n


def read_status():
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_config(patch):
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    cfg.setdefault("status", {}).update(patch)
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                      encoding="utf-8")


def run_wrapped(*args):
    return subprocess.run([str(EXE), "--run", *args],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def main() -> int:
    if not EXE.exists():
        print(f"[run] 找不到 {EXE}")
        return 1

    bak = None
    if CONFIG.exists():
        bak = CONFIG.with_suffix(".json.runbak")
        shutil.copy2(CONFIG, bak)

    kill_all()
    try:
        STATUS.unlink()
    except FileNotFoundError:
        pass
    write_config({"enabled": True, "poll_seconds": 1,
                  "on_done_action": "notify",
                  "on_failed_action": "notify"})

    print("=" * 52)
    print("exe --run 包装模式")
    print("=" * 52)

    proc = subprocess.Popen([str(EXE)], cwd=str(HERE))
    mark = len(log_lines())          # 必须在启动前打点，否则会漏掉 status.start
    time.sleep(4)
    check("常驻实例已启动并监控状态",
          fired(log_lines()[mark:], "status.start") >= 1)

    # ---- 场景 A：成功 ----
    # 被包装命令会新开控制台，父进程拿不到它的 stdout，
    # 所以让它把收到的 argv 写进文件来验证「参数没被吃掉」。
    argv_probe = Path(os.environ.get("TEMP", ".")) / "ailock_argv_probe.txt"
    try:
        argv_probe.unlink()
    except FileNotFoundError:
        pass
    script = ("import sys,time,pathlib; time.sleep(3); "
              f"pathlib.Path(r'{argv_probe}').write_text(repr(sys.argv[1:])); "
              "sys.exit(0)")
    p = subprocess.Popen(
        [str(EXE), "--run", "-t", "exe包装·成功演练", "--",
         str(PY), "-c", script, "--epochs", "50"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace")

    time.sleep(2.0)
    st = read_status()
    check("运行期状态为 running",
          bool(st) and st.get("state") == "running", f"实际: {st}")
    check("运行期注入了 watch_pid",
          bool(st) and int(st.get("watch_pid") or 0) > 0, f"实际: {st}")

    out, err = p.communicate(timeout=60)
    check("成功场景退出码为 0", p.returncode == 0,
          f"code={p.returncode} out={out!r} err={err!r}")
    try:
        probe = argv_probe.read_text(encoding="utf-8")
    except OSError:
        probe = ""
    check("被包装命令收到了自己的 --epochs 参数",
          "['--epochs', '50']" in probe, f"probe={probe!r}")

    time.sleep(3.5)
    st = read_status()
    check("命令结束后状态为 done", bool(st) and st.get("state") == "done",
          f"实际: {st}")

    lines = log_lines()[mark:]
    check("常驻实例捕获到 done 并触发 notify",
          fired(lines, "action.execute", "notify") >= 1,
          "\n".join(ln for ln in lines if "action." in ln) or "(无)")

    # ---- 场景 B：失败 ----
    mark2 = len(log_lines())
    r = run_wrapped("-t", "exe包装·失败演练", "--",
                    str(PY), "-c", "import sys; sys.exit(7)")
    check("失败场景退出码原样透传（7）", r.returncode == 7,
          f"code={r.returncode} out={r.stdout!r} err={r.stderr!r}")

    time.sleep(3.5)
    st = read_status()
    check("失败后状态为 failed", bool(st) and st.get("state") == "failed",
          f"实际: {st}")
    lines2 = log_lines()[mark2:]
    check("常驻实例捕获到 failed",
          fired(lines2, "status.failed") >= 1,
          "\n".join(lines2[-6:]) or "(无)")

    # ---- 场景 C：命令不存在 ----
    r = run_wrapped("--", "definitely_not_a_cmd_xyz")
    check("命令不存在退出码为 127", r.returncode == 127,
          f"code={r.returncode}")

    kill_all()
    if bak and bak.exists():
        shutil.copy2(bak, CONFIG)
        try:
            bak.unlink()
        except OSError:
            pass

    print(f"\n{'=' * 52}\n通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 52}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
