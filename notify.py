"""AiLock 状态上报命令行工具 —— 给 AI 工具 / 脚本 / CI 调用。

    notify.py --state running --task "模型微调 v3" --progress 24 -l "epoch 12/50"
    notify.py --state done
    notify.py --state failed -l "OOM, 显存不足"
    notify.py --state running --sid my-session        # 多会话：写 status.d/<sid>.json
    notify.py --show
    notify.py --clear

选项：
    -s/--state     running | done | failed | idle
    -t/--task      任务名
    -l/--line      自定义文本行，可重复
    -p/--progress  进度，0~100 或 0~1（>1 视为百分比）
    --pid          监控该进程，进程消失即视为任务结束
    --sid          会话 id：写 status.d/<sid>.json（多会话互不覆盖）；
                   不带则写主 status.json（与旧版行为一致）
    -f/--file      指定状态文件路径（优先于 --sid）
    -m/--merge     只更新指定字段，保留其它字段

退出码：0 成功，2 参数错误。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import default_status_path  # noqa: E402
from core.statusfile import StatusFile, VALID_STATES  # noqa: E402


def parse_progress(text):
    if text is None:
        return None
    s = str(text).strip().rstrip("%")
    try:
        v = float(s)
    except ValueError:
        return None
    if v > 1.0:
        v = v / 100.0
    return max(0.0, min(1.0, v))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="notify.py",
        description="向 AiLock 锁屏上报任务状态",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("-s", "--state", choices=VALID_STATES, help="任务状态")
    ap.add_argument("-t", "--task", default="", help="任务名")
    ap.add_argument("-l", "--line", action="append", default=[],
                    help="自定义文本行，可重复多次")
    ap.add_argument("-p", "--progress", help="进度（24 或 0.24 或 24%%）")
    ap.add_argument("--pid", type=int, default=0, help="监控该进程，退出即视为完成")
    ap.add_argument("--sid", default="", help="会话 id，写 status.d/<sid>.json")
    ap.add_argument("-f", "--file", default=None, help="状态文件路径（优先于 --sid）")
    ap.add_argument("-m", "--merge", action="store_true", help="增量更新其它字段")
    ap.add_argument("--show", action="store_true", help="打印当前状态后退出")
    ap.add_argument("--clear", action="store_true", help="删除状态文件")

    args = ap.parse_args(argv)

    if args.file:
        sf = StatusFile(args.file)
    elif args.sid:
        from core.statusdir import SessionStatusFile
        sf = SessionStatusFile(args.sid)
    else:
        sf = StatusFile(default_status_path())

    if args.show:
        data = sf.read()
        if data is None:
            print("(无状态文件)")
            return 0
        import json
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if args.clear:
        sf.clear()
        print("已清除状态")
        return 0

    if args.merge and not (args.state or args.task or args.line
                           or args.progress or args.pid):
        ap.error("--merge 至少需要一个待更新字段")
    if not args.merge and not args.state:
        ap.error("需要 --state（或使用 --merge / --show / --clear）")

    data = sf.write(
        state=args.state or "",
        task=args.task or None,
        lines=args.line or None,
        progress=parse_progress(args.progress),
        watch_pid=args.pid,
        merge=args.merge or not args.state,
    )
    print(f"已上报: state={data['state']} "
          f"progress={data['progress']} -> {sf.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
