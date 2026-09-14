#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AiLock × Codex 一键卸载器。

从 ~/.codex/config.toml 把 notify 还原为安装前的原命令（sidecar 里存的），
再删除 %APPDATA%/AiLock/hooks/ 下的中转脚本和 codex_relay.json。
config.toml 其余内容一律不动。
"""

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from core import codex_bridge   # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="AiLock × Codex 卸载")
    ap.add_argument("--dry-run", action="store_true",
                    help="只显示将要做的操作，不写任何文件")
    args = ap.parse_args()

    print(codex_bridge.uninstall(dry=args.dry_run))


if __name__ == "__main__":
    main()
