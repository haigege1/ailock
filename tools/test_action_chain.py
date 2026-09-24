"""集成测试：AI 任务完成 → 触发动作 的完整链路。

这是用户明确要求的场景（「任务跑完后自动关机」），必须端到端验证：

  阶段 1  notify（非破坏性动作）
          running → done 的**状态跃迁**应触发一次 action.execute

  阶段 2  shutdown（破坏性动作）
          绝不能立刻执行，必须进入**可取消倒计时**；
          倒计时未走完时进程里不能出现 action.execute | shutdown

  阶段 3  failed
          失败走 on_failed_action，与 done 分流

  阶段 4  幂等性
          同一个 done 状态重复写入不应反复触发；
          但 done → running → done 的「重启任务」应能再次触发

隔离说明：状态文件写在临时目录（配置里的 status.path + notify 的 -f），
          不碰真机 %APPDATA%/AiLock/status.json，也避开 status.d 里正在
          跑的会话 —— 否则「存在仍在运行的其他会话」会把 done 动作合法地
          抑制掉，测试就假红了（2026-09-24 遇到）。

安全约束：阶段 2 的倒计时设为 15 秒，测试在触发后 4 秒内就 kill 掉进程，
          永远不会真的关机。
"""

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
EXE = HERE / "dist" / "AiLock.exe"
APPDATA_DIR = Path.home() / "AppData" / "Roaming" / "AiLock"
LOG = APPDATA_DIR / "ailock.log"
CONFIG = APPDATA_DIR / "config.json"
# 临时状态目录：本测试的 status.json 落在这里，真机状态不受影响
TMP_DIR = Path(tempfile.mkdtemp(prefix="ailock-chain-"))
STATUS = TMP_DIR / "status.json"

PASS, FAIL = 0, 0


def check(label: str, ok: bool, detail: str = "") -> bool:
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


def log_lines() -> list:
    try:
        return LOG.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []


def new_log_since(mark: int) -> list:
    return log_lines()[mark:]


def log_mark() -> int:
    return len(log_lines())


def count(lines, needle: str) -> int:
    return sum(1 for ln in lines if needle in ln)


def fired(lines, event: str, needle: str = "") -> int:
    """统计某事件出现次数。日志行格式：`时间  事件  |  详情`。

    用结构解析而不是子串匹配，避免被时间戳里的空格干扰。
    """
    n = 0
    for ln in lines:
        head, _sep, detail = ln.partition("  |  ")
        # 没有详情的行（如 status.failed）head 就是整行
        if head.split("  ")[-1].strip() != event:
            continue
        if not needle or needle in detail:
            n += 1
    return n


def action_lines(lines) -> str:
    out = [ln for ln in lines if "action." in ln or "status." in ln]
    return "\n".join(out[-8:]) or "(无 action / status 日志)"


def write_config(patch: dict):
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    cfg.setdefault("status", {}).update(patch)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                      encoding="utf-8")


def backup():
    bak = {}
    for p in (CONFIG, STATUS):
        if p.exists():
            b = p.with_suffix(p.suffix + ".chainbak")
            shutil.copy2(p, b)
            bak[p] = b
    return bak


def restore(bak):
    for p, b in bak.items():
        if b.exists():
            shutil.copy2(b, p)
            try:
                b.unlink()
            except OSError:
                pass


def notify(*args) -> subprocess.CompletedProcess:
    """通过 exe 自带的上报入口写状态文件（走真实用户路径）。"""
    return subprocess.run([str(EXE), "--notify", *args, "-f", str(STATUS)],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def start_app() -> subprocess.Popen:
    return subprocess.Popen([str(EXE)], cwd=str(HERE))


def wait_for(needle: str, mark: int, timeout: float = 12.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if count(new_log_since(mark), needle):
            return True
        time.sleep(0.3)
    return False


def run_phase(title: str, cfg_patch: dict, script):
    print(f"\n{'=' * 52}\n{title}\n{'=' * 52}")
    kill_all()
    # 监控路径指向临时目录：status.d 里现有会话不再干扰本测试
    write_config({**cfg_patch, "path": str(STATUS)})
    try:
        STATUS.unlink()
    except FileNotFoundError:
        pass
    proc = start_app()
    mark = log_mark()
    try:
        if not check("状态监控线程已启动", wait_for("status.start", mark)):
            print("  (尾部日志)\n" + "\n".join(new_log_since(mark)[-10:]))
            return
        script(proc, mark)
    finally:
        kill_all()


def main() -> int:
    if not EXE.exists():
        print(f"[chain] 找不到 {EXE}，请先构建")
        return 1

    bak = backup()
    try:
        # ---------------------------------------------------------
        def phase1(proc, mark):
            # 阶段 1：非破坏性动作 notify
            notify("-s", "running", "-t", "集成测试·模型微调",
                   "-p", "42", "-l", "epoch 12/50", "-l", "预计剩余 18 分钟")
            time.sleep(2.5)
            check("running 状态未触发任何动作",
                  count(new_log_since(mark), "action.execute") == 0)

            notify("-s", "done", "-m", "-p", "100", "-l", "完成，用时 0m02s")
            time.sleep(4)

            lines = new_log_since(mark)
            check("捕获到 status.done 状态跃迁",
                  fired(lines, "status.done") >= 1)
            check("触发了一次 notify 动作",
                  fired(lines, "action.execute", "notify") == 1,
                  action_lines(lines))
            check("任务名透传到动作原因",
                  fired(lines, "action.execute", "模型微调") >= 1)
            check("未误触发破坏性动作",
                  fired(lines, "action.execute", "shutdown") == 0)
            check("进程依旧存活", proc.poll() is None)

        run_phase("阶段 1 · 任务完成 → 仅提示（非破坏性）",
                  {"enabled": True, "poll_seconds": 1,
                   "on_done_action": "notify"}, phase1)

        # ---------------------------------------------------------
        def phase2(proc, mark):
            # 阶段 2：破坏性动作必须走可取消倒计时
            notify("-s", "running", "-t", "集成测试·关机演练")
            time.sleep(2.5)
            notify("-s", "done")
            time.sleep(4)

            lines = new_log_since(mark)
            check("shutdown 被排入倒计时而非立即执行",
                  fired(lines, "action.schedule", "shutdown 倒计时 15s") >= 1,
                  action_lines(lines))
            check("倒计时期间未真正执行关机",
                  fired(lines, "action.execute", "shutdown") == 0)
            check("已记录待执行动作",
                  fired(lines, "action.pending", "shutdown") >= 1)
            check("4 秒后进程仍在（说明没关机）", proc.poll() is None)

        run_phase("阶段 2 · 任务完成 → 关机（必须可取消倒计时）",
                  {"enabled": True, "poll_seconds": 1,
                   "on_done_action": "shutdown",
                   "on_done_countdown": 15}, phase2)

        # ---------------------------------------------------------
        def phase3(proc, mark):
            # 阶段 3：失败分流
            notify("-s", "running", "-t", "集成测试·失败演练")
            time.sleep(2.5)
            notify("-s", "failed", "-l", "OOM: 显存不足")
            time.sleep(4)

            lines = new_log_since(mark)
            check("捕获到 status.failed", fired(lines, "status.failed") >= 1)
            check("走了 on_failed_action（notify）",
                  fired(lines, "action.execute", "notify") == 1,
                  action_lines(lines))
            check("失败时未触发 done 分支的关机",
                  fired(lines, "action.execute", "shutdown") == 0)

        run_phase("阶段 3 · 任务失败 → 走独立动作分支",
                  {"enabled": True, "poll_seconds": 1,
                   "on_done_action": "shutdown", "on_done_countdown": 15,
                   "on_failed_action": "notify"}, phase3)

        # ---------------------------------------------------------
        def phase4(proc, mark):
            # 阶段 4：幂等性 —— 重复写 done 不该反复触发
            notify("-s", "running", "-t", "集成测试·幂等性")
            time.sleep(2.5)
            notify("-s", "done")
            time.sleep(3)
            n1 = count(new_log_since(mark), "action.execute")

            # 同一个 done 状态再刷三次
            for _ in range(3):
                notify("-s", "done", "-m", "-p", "100")
                time.sleep(1.5)
            n2 = count(new_log_since(mark), "action.execute")
            check("重复写入同一 done 状态不重复触发",
                  n1 == 1 and n2 == 1, f"首次={n1} 重复后={n2}")

            # 任务重启：done -> running -> done，应能再次触发
            notify("-s", "running", "-t", "集成测试·第二轮任务")
            time.sleep(2.5)
            notify("-s", "done")
            time.sleep(3)
            n3 = count(new_log_since(mark), "action.execute")
            check("任务重启（done→running→done）能再次触发",
                  n3 == 2, f"期望 2 次，实际 {n3}")

        run_phase("阶段 4 · 触发幂等性（不重复触发 / 重启可再触发）",
                  {"enabled": True, "poll_seconds": 1,
                   "on_done_action": "notify",
                   "on_failed_action": "none"}, phase4)

    finally:
        kill_all()
        restore(bak)
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    print(f"\n{'=' * 52}\n通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 52}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
