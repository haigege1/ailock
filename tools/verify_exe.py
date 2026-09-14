"""截取 exe 模式的锁屏画面用于回归对比。"""
import subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXE = HERE.parent / "dist" / "AiLock.exe"
OUT = HERE / "_verify_out"
OUT.mkdir(exist_ok=True)

print("[verify-exe] 启动", EXE.name)
proc = subprocess.Popen([str(EXE), "--selftest", "7"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace")
time.sleep(3)
try:
    from PIL import ImageGrab
    shot = ImageGrab.grab(all_screens=True)
    p = OUT / "lockscreen_exe.png"
    shot.save(p)
    print(f"[verify-exe] 截图 {shot.size[0]}x{shot.size[1]} -> {p}")
except Exception as e:
    print(f"[verify-exe] 截图失败: {e!r}")
out, _ = proc.communicate(timeout=20)
print("-" * 46)
print(out)
print("-" * 46)
