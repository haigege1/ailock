"""观察 exe 启动后窗口创建与进程死亡时机。"""
import subprocess
import sys
import time
from pathlib import Path

import win32gui
import win32process

EXE = r"C:\Users\admin\WorkBuddy\2026-09-02-11-49-44\lockscreen\dist\AiLock.exe"
OUT = Path(r"C:\Users\admin\AppData\Local\Temp\ailock_obs.txt")
out = []


def rec(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    out.append(line)
    print(line, flush=True)


def windows_of(pids):
    found = []

    def enum(h, _):
        _, pid = win32process.GetWindowThreadProcessId(h)
        if pid in pids:
            t = win32gui.GetWindowText(h)
            found.append((pid, t, win32gui.IsWindowVisible(h)))

    try:
        win32gui.EnumWindows(enum, None)
    except Exception as exc:
        found.append(("enum-error", repr(exc), False))
    return found


proc = subprocess.Popen([EXE])
time.sleep(1.0)
p = subprocess.run(["tasklist"], capture_output=True)
pids = {int(ln.split()[1]) for ln in p.stdout.decode("gbk", "ignore").splitlines()
        if "AiLock.exe" in ln}
rec(f"启动后1s，AiLock pids={sorted(pids)}")

alive_since = time.time()
for i in range(15):
    time.sleep(2)
    p = subprocess.run(["tasklist"], capture_output=True)
    cur = {int(ln.split()[1]) for ln in p.stdout.decode("gbk", "ignore").splitlines()
           if "AiLock.exe" in ln}
    wins = windows_of(pids)
    rec(f"t+{3 + i * 2}s pids={len(cur)} windows={wins}")
    if not cur:
        rec(">>> 进程全部消失！")
        break

OUT.write_text("\n".join(out), encoding="utf-8")
