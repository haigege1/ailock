"""验收脚本：铺满所有屏幕 -> 截图 -> 合成按键验证拦截 -> 自动收尾。

用法：
    python _verify.py [锁定秒数]
"""

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable
OUT = HERE / "_verify_out"
OUT.mkdir(exist_ok=True)

user32 = ctypes.WinDLL("user32", use_last_error=True)
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", INPUTUNION)]


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002


def send_key(vk: int, up: bool = False) -> None:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki.wVk = vk
    inp.u.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    time.sleep(0.06)


def tap(vk: int) -> None:
    send_key(vk, False)
    send_key(vk, True)


def combo(mods: list, vk: int) -> None:
    for m in mods:
        send_key(m, False)
    tap(vk)
    for m in reversed(mods):
        send_key(m, True)


VK_LWIN, VK_TAB, VK_ESCAPE, VK_F4, VK_MENU, VK_CONTROL = 0x5B, 0x09, 0x1B, 0x73, 0x12, 0x11
VK_A, VK_SNAPSHOT = 0x41, 0x2C


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 9.0

    print("[verify] 启动自检进程...")
    proc = subprocess.Popen(
        [PY, "-u", str(HERE / "main.py"), "--selftest", str(seconds)],
        cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )

    time.sleep(3.0)

    # 1) 截图
    try:
        from PIL import ImageGrab
        shot = ImageGrab.grab(all_screens=True)
        path = OUT / "lockscreen.png"
        shot.save(path)
        print(f"[verify] 已截图 {shot.size[0]}x{shot.size[1]} -> {path}")
    except Exception as exc:
        print(f"[verify] 截图失败: {exc!r}")

    # 2) 合成按键验证拦截
    print("[verify] 发送需要被拦截的组合键...")
    tap(VK_LWIN)                    # 应拦截 1
    combo([VK_MENU], VK_TAB)        # 应拦截 1
    combo([VK_CONTROL], VK_ESCAPE)  # 应拦截 1
    combo([VK_MENU], VK_F4)         # 应拦截 1
    tap(VK_SNAPSHOT)                # 应拦截 1
    tap(VK_A)                       # 普通按键，应放行
    print("[verify] 期望被拦截 5 次（普通按键 A 不计）")

    try:
        out, _ = proc.communicate(timeout=seconds + 15)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    print("-" * 46)
    print(out)
    print("-" * 46)
    return 0


if __name__ == "__main__":
    sys.exit(main())
