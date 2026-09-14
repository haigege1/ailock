#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AiLock × Codex 一键安装器（命令行入口）。

核心逻辑在 core/codex_bridge.py，与设置页「一键安装」按钮共用同一套实现。

做的事（全部可逆，见 uninstall.py）：
  1. 把中转脚本复制到 %APPDATA%/AiLock/hooks/ailock_codex_relay.py
  2. 把 ~/.codex/config.toml 顶部的 notify 行改指向中转脚本
     - 原 notify 命令（如 codex-computer-use.exe）先存进 sidecar
       codex_relay.json，中转脚本每轮会原样转发，插件功能不受影响
     - 改写前自动备份为 config.toml.bak-<时间戳>
  3. 打开 AiLock 的「状态检测」开关（status.enabled=true）
  4. 用临时 APPDATA 跑一次冒烟测试

用法：
  python integrations/codex/install.py            # 安装
  python integrations/codex/install.py --dry-run  # 只看会做什么
  python integrations/codex/uninstall.py          # 卸载（还原原 notify）

装完需要：重启 Codex（notify 在启动时加载）；
AiLock 正在运行的话需重启一次。
"""

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from core import codex_bridge   # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="AiLock × Codex 一键安装")
    ap.add_argument("--dry-run", action="store_true",
                    help="只显示将要做的操作，不写任何文件")
    args = ap.parse_args()

    print("== AiLock × Codex 安装器 ==")
    try:
        report = codex_bridge.install(dry=args.dry_run)
    except codex_bridge.BridgeError as err:
        print(err)
        sys.exit(1)
    print(report)


if __name__ == "__main__":
    main()
