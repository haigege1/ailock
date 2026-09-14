#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AiLock × WorkBuddy 一键卸载器。

从 ~/.workbuddy/settings.json 摘除 AiLock 的 hook 事件（先备份），
再删除 %APPDATA%/AiLock/hooks/ailock_workbuddy_hook.py。
其余 hooks 与设置原样保留。
"""

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from core import workbuddy_bridge   # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="AiLock × WorkBuddy 卸载")
    ap.add_argument("--dry-run", action="store_true",
                    help="只显示将要做的操作，不写任何文件")
    args = ap.parse_args()

    print(workbuddy_bridge.uninstall(dry=args.dry_run))


if __name__ == "__main__":
    main()
