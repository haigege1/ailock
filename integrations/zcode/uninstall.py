#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AiLock × ZCode 卸载器 —— 与 install.py 严格互逆。

核心逻辑在 core/zcode_bridge.py，与设置页「卸载」按钮共用同一套实现。

  1. 从 ~/.zcode/cli/config.json 摘除 AiLock 的 hook（只删自家条目）
  2. 删除 %APPDATA%/AiLock/hooks/ailock_zcode_hook.py
  3. AiLock 的 status.enabled 保持不动（可能还有别的联动在用）

用法（在仓库根目录或本目录均可）：python uninstall.py [--dry-run]
"""

import argparse
import sys
from pathlib import Path

# 允许从任意目录运行：把仓库根加进 sys.path
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from core import zcode_bridge   # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="AiLock × ZCode 卸载")
    ap.add_argument("--dry-run", action="store_true",
                    help="只显示将要做的操作，不写任何文件")
    args = ap.parse_args()

    try:
        print(zcode_bridge.uninstall(dry=args.dry_run))
    except zcode_bridge.BridgeError as err:
        print(err)
        sys.exit(1)


if __name__ == "__main__":
    main()
