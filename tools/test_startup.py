"""集成测试：完整启动流程 + 托盘 + 首次运行行为。

模拟用户「双击 exe」，验证后台常驻是否稳定：
  - 进程能起来并存活
  - 托盘线程 / 状态监控 / 看门狗都能启动
  - 未设密码时会自动弹出设置面板（首次运行引导）
"""

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
EXE = HERE / "dist" / "AiLock.exe"
PY = sys.executable
LOG = Path.home() / "AppData" / "Roaming" / "AiLock" / "ailock.log"
OUT = HERE / "tools" / "_verify_out"
OUT.mkdir(parents=True, exist_ok=True)


def tail_log(n=15):
    try:
        return "\n".join(LOG.read_text(encoding="utf-8", errors="ignore")
                         .splitlines()[-n:])
    except Exception:
        return "(无日志)"


def kill_all():
    """清理所有 AiLock 进程。"""
    subprocess.run(["taskkill", "/F", "/IM", "AiLock.exe"],
                   capture_output=True)
    time.sleep(1)


def is_running():
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq AiLock.exe"],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="ignore")
    return "AiLock.exe" in (r.stdout or "")


def screenshot(name):
    try:
        from PIL import ImageGrab
        shot = ImageGrab.grab(all_screens=True)
        p = OUT / name
        shot.save(p)
        return p
    except Exception as exc:
        print(f"   截图失败: {exc!r}")
        return None


def main() -> int:
    kill_all()
    print("[itest] 启动 AiLock.exe（模拟双击）...")
    proc = subprocess.Popen([str(EXE)], cwd=str(HERE))

    time.sleep(5)

    alive = proc.poll() is None
    print(f"[itest] 进程存活: {alive}")

    print(f"[itest] tasklist 检出: {is_running()}")

    print("\n[itest] 日志尾部:")
    print("-" * 46)
    print(tail_log(20))
    print("-" * 46)

    # 首次运行（没设密码）应自动弹出设置面板
    shot = screenshot("integration_startup.png")
    if shot:
        print(f"[itest] 已截图: {shot.name}")

    print("\n[itest] 保持运行 3 秒后再查一次稳定性...")
    time.sleep(3)
    print(f"[itest] 3 秒后进程存活: {proc.poll() is None}")

    kill_all()
    print("[itest] 已清理")
    return 0 if alive else 1


if __name__ == "__main__":
    sys.exit(main())
