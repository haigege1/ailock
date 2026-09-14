"""把任意命令包一层：开始前上报 running，结束后按退出码上报 done / failed。

这是「跟 AI 工具联动」最省事的入口 —— 不用改一行训练脚本，
把原来的命令前面加 `AiLock.exe --run` 就行。

用法（exe 形态，推荐）：
    AiLock.exe --run -- python train.py --epochs 50
    AiLock.exe --run -t "数据清洗" -- bash ./pipeline.sh

用法（源码形态）：
    python ailock_run.py -t "模型微调" -- python train.py

多会话：每个 --run 进程写自己的 status.d/run-<pid>.json，并发互不覆盖；
"任务完成"动作由 AiLock 监控端聚合——最后一个收尾的任务才触发。

建议用 `--` 分隔，避免命令自身的参数被本工具吃掉。
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import default_status_path  # noqa: E402
from core.statusfile import (STATE_DONE, STATE_FAILED,  # noqa: E402
                             STATE_RUNNING, StatusFile)


def session_status_file() -> StatusFile:
    """--run 独占一个会话文件：run-<pid>.json，并发任务互不覆盖。"""
    sid = "run-%d" % os.getpid()
    base = Path(default_status_path()).parent / "status.d"
    return StatusFile(str(base / (sid + ".json")), extra={"sid": sid})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="ailock_run",
        description="包裹一条命令，并向 AiLock 锁屏上报其运行状态",
    )
    ap.add_argument("-t", "--task", default="", help="任务名")
    ap.add_argument("-f", "--file", default=None,
                    help="状态文件路径（默认 status.d/run-<pid>.json）")
    ap.add_argument("-l", "--line", action="append", default=[], help="附加文本行")
    ap.add_argument("--no-pid", action="store_true", help="不注入进程号监控")
    ap.add_argument("--no-console", action="store_true",
                    help="不弹出控制台窗口（hooks / CI 场景用）")
    ap.add_argument("command", nargs=argparse.REMAINDER,
                    help="要执行的命令，建议前置 --")
    args = ap.parse_args(argv)

    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        ap.error("缺少要执行的命令")

    task = args.task or " ".join(args.command)
    sf = session_status_file() if not args.file else StatusFile(args.file)

    print(f"[ailock-run] 开始: {task}", flush=True)
    sf.write(STATE_RUNNING, task=task,
             lines=args.line or ["启动中..."], progress=0.0)

    start = time.time()
    # AiLock.exe 是 GUI 程序（无控制台），默认让被包装命令自己开一个控制台窗口，
    # 这样人还在机器旁时能直接看到训练日志；在 hooks / CI 里用 --no-console 关掉。
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if args.no_console else 0
    try:
        proc = subprocess.Popen(args.command, creationflags=flags)
    except FileNotFoundError:
        print(f"[ailock-run] 命令不存在: {args.command[0]}", file=sys.stderr)
        sf.write(STATE_FAILED, task=task,
                 lines=[f"命令不存在: {args.command[0]}"])
        return 127
    except OSError as exc:
        print(f"[ailock-run] 启动失败: {exc}", file=sys.stderr)
        sf.write(STATE_FAILED, task=task, lines=[f"启动失败: {exc}"])
        return 127

    # 拿到 pid 后再补一次 running：锁屏端据此判断「进程没了 = 任务结束」，
    # 这样即使命令自己崩了（没机会上报）也能被识别为完成。
    if not args.no_pid and proc.pid:
        sf.write(STATE_RUNNING, task=task, lines=args.line or ["运行中"],
                 watch_pid=proc.pid, merge=True)

    try:
        code = proc.wait()
    except KeyboardInterrupt:
        try:
            proc.terminate()
        except OSError:
            pass
        sf.write(STATE_FAILED, task=task, lines=["已被用户中断 (Ctrl+C)"])
        return 130

    elapsed = time.time() - start
    mins, secs = divmod(int(elapsed), 60)
    hours, mins = divmod(mins, 60)
    dur = f"{hours}h{mins}m{secs}s" if hours else f"{mins}m{secs}s"

    if code == 0:
        sf.write(STATE_DONE, task=task,
                 lines=[f"完成，用时 {dur}", *(args.line or [])], progress=1.0)
        print(f"[ailock-run] 完成，退出码 {code}，用时 {dur}", flush=True)
    else:
        sf.write(STATE_FAILED, task=task,
                 lines=[f"失败，退出码 {code}，用时 {dur}", *(args.line or [])],
                 progress=1.0)
        print(f"[ailock-run] 失败，退出码 {code}，用时 {dur}", flush=True)

    return code


if __name__ == "__main__":
    sys.exit(main())
