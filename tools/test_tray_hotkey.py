# -*- coding: utf-8 -*-
"""验证 reregister_hotkey 跨线程安全（1408 修复）。

背景：RegisterHotKey 必须由窗口所属线程调用。设置面板保存时，
_on_settings_saved 在主线程调 tray.reregister_hotkey() —— 修复前
直接跨线程调用报 1408（无效窗口），修复后经 PostMessage 转到托盘线程。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 收集日志到内存，不写盘
from core.logger import log as _log  # noqa: E402

captured = []
_log.log = lambda tag, msg="": captured.append((tag, msg))

from ui.tray import TrayApp  # noqa: E402

tray = TrayApp("", "AiLock-hotkey-test",
               menu_items=[("测试", lambda: None)],
               hotkey="ctrl+alt+l", on_hotkey=lambda: None)
tray.start()
assert tray.wait_ready(5), "tray 未就绪"
time.sleep(0.3)

# 从主线程（= 错误线程）调用，修复前这里直接 1408
tray.reregister_hotkey("ctrl+alt+l")
time.sleep(0.5)
tray.reregister_hotkey("ctrl+alt+f9")   # 冷门组合，避开被其他软件占用
time.sleep(0.5)

tray.stop()
time.sleep(0.3)

warns = [m for t, m in captured if t == "tray.warn"]
regs = [m for t, m in captured if t == "tray.hotkey"]

print("注册记录:", regs)
if warns:
    print("警告:", warns)

ok = True
if any("1408" in w for w in warns):
    print("=> FAIL: 仍然出现 1408 跨线程错误")
    ok = False
if not any("ctrl+alt+f9" in r for r in regs):
    print("=> FAIL: 新热键未注册成功")
    ok = False
if len(regs) < 3:
    print("=> FAIL: 注册次数不足（应为 3 次：初始 + 两次重注册）")
    ok = False

print("=> 热键跨线程重注册 " + ("OK" if ok else "FAIL"))
sys.exit(0 if ok else 1)
