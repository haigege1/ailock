#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AiLock × WorkBuddy 一键安装器（命令行入口）。

核心逻辑在 core/workbuddy_bridge.py，与设置页「一键安装」按钮共用
同一套实现，行为完全一致。

做的事（全部可逆，见 uninstall.py）：
  1. 把钩子脚本复制到 %APPDATA%/AiLock/hooks/ailock_workbuddy_hook.py
  2. 合并写入 WorkBuddy 用户级配置 ~/.workbuddy/settings.json
     - 只追加 AiLock 的 6 个 hook 事件，不动你已有的其他 hooks / 设置
     - 写入前自动备份为 settings.json.bak-<时间戳>
  3. 打开 AiLock 的「状态检测」开关（status.enabled=true，其余设置不动）
  4. 用临时 APPDATA 跑一次冒烟测试，验证钩子能写出合法状态文件

用法（在仓库根目录或本目录均可）：
  python integrations/workbuddy/install.py            # 安装
  python integrations/workbuddy/install.py --dry-run  # 只看会做什么
  python integrations/workbuddy/uninstall.py          # 卸载

装完需要：重启 WorkBuddy 会话（hook 配置在会话启动时加载），
AiLock 正在运行的话需重启一次（它不热加载配置）。
"""

import argparse
import sys
from pathlib import Path

# 允许从任意目录运行：把仓库根加进 sys.path
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from core import workbuddy_bridge   # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="AiLock × WorkBuddy 一键安装")
    ap.add_argument("--dry-run", action="store_true",
                    help="只显示将要做的操作，不写任何文件")
    args = ap.parse_args()

    print("== AiLock × WorkBuddy 安装器 ==")
    try:
        report = workbuddy_bridge.install(dry=args.dry_run)
    except workbuddy_bridge.BridgeError as err:
        print(err)
        sys.exit(1)
    print(report)


if __name__ == "__main__":
    main()
