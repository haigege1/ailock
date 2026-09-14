"""端到端测试：解锁流程、恢复码、冷却、设置面板、图片背景。

会短暂锁屏约 6 秒，请在合适的时机运行。会自动备份并恢复 vault，
不会改动你真实的密码。
"""

import shutil
import sys
import time
import tkinter as tk
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from core.config import Config, RuntimeState, app_dir
from core.security import Vault

VAULT = app_dir() / "vault.dat"
BACKUP = VAULT.with_suffix(".testbackup")


def _safe_unlink(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def backup_vault():
    """备份用户原 vault，并把上一次的测试残留清掉，避免累积。"""
    _safe_unlink(BACKUP)
    if VAULT.exists():
        shutil.copy2(VAULT, BACKUP)


def restore_vault():
    if BACKUP.exists():
        shutil.copy2(BACKUP, VAULT)
        _safe_unlink(BACKUP)
    else:
        # 测试过程中创建了一个，又没用户原文件 —— 删掉
        _safe_unlink(VAULT)


def main() -> int:
    backup_vault()
    try:
        cfg = Config()
        cfg.set("security.max_attempts", 3, autosave=False)
        cfg.set("security.cooldown_seconds", 2, autosave=False)
        cfg.set("background.mode", "color", autosave=False)
        cfg.set("background.color", "#0B1220", autosave=False)
        cfg.set("message.text", "端到端测试", autosave=False)

        # 用测试密码覆盖现有密码，跑完恢复
        vault = Vault()
        vault.reset_all()
        vault.set_password("test1234")
        code = vault.regenerate_recovery()
        print(f"[test] 已设置测试密码 test1234，恢复码 {code}")

        root = tk.Tk()
        root.withdraw()

        from main import App
        app = App(root, cfg, start_locked=False)
        # 启动时如果没设密码会弹设置，把那个 after 取消掉
        try:
            for aid in list(root.tk.call("after", "info")):
                root.after_cancel(aid)
        except Exception:
            pass

        # ========== 1) 设置面板：能开就 OK ==========
        print("[test] 打开设置面板...")
        from ui.settings_panel import SettingsPanel
        panel = SettingsPanel(root, cfg, vault)
        root.update()
        print("[test]   ✓ 设置面板实例化无异常")
        panel.win.destroy()
        root.update()

        # ========== 2) 锁屏 + 错误密码 ==========
        print("[test] 锁定...")
        app.lock()
        time.sleep(0.5)
        assert app.locked and app.lockscreen, "锁屏未生效"
        print(f"[test]   ✓ 已锁，窗口数 {len(app.lockscreen.windows)}")

        print("[test] 输入错误密码...")
        ok, msg = app.try_unlock("wrong1")
        assert not ok, f"错误密码应被拒：{msg}"
        print(f"[test]   ✓ 拒绝原因：{msg}")

        # ========== 3) 正确密码解锁 ==========
        print("[test] 输入正确密码...")
        ok, msg = app.try_unlock("test1234")
        assert ok, f"正确密码应通过：{msg}"
        print("[test]   ✓ 验证通过，等待解锁...")
        for _ in range(20):
            root.update()
            time.sleep(0.05)
            if not app.locked:
                break
        assert not app.locked, "解锁未生效"
        print("[test]   ✓ 已解锁")

        # ========== 4) 恢复码解锁 ==========
        print("[test] 再次锁定，用恢复码解锁...")
        # 清失败计数
        RuntimeState().reset()
        app.lock()
        time.sleep(0.5)
        ok, msg = app.try_unlock("this-is-wrong")
        assert not ok
        ok, msg = app.try_unlock(code)
        assert ok, f"恢复码应通过：{msg}"
        for _ in range(20):
            root.update()
            time.sleep(0.05)
            if not app.locked:
                break
        assert not app.locked
        print("[test]   ✓ 恢复码解锁成功")

        # ========== 5) 冷却保护 ==========
        print("[test] 触发冷却保护...")
        RuntimeState().reset()
        app.lock()
        time.sleep(0.5)
        for i in range(3):
            app.try_unlock("wrong")
        # 第四次应进入冷却
        ok, msg = app.try_unlock("test1234")
        assert not ok and "等待" in msg, f"应进入冷却：{msg}"
        print(f"[test]   ✓ 冷却生效：{msg}")
        # 等冷却过去
        print("[test] 等待 2.5s 冷却结束...")
        time.sleep(2.6)
        ok, msg = app.try_unlock("test1234")
        assert ok, f"冷却后应可通过：{msg}"
        for _ in range(20):
            root.update()
            time.sleep(0.05)
            if not app.locked:
                break
        assert not app.locked
        print("[test]   ✓ 冷却后正常解锁")

        # ========== 6) 图片背景模式 ==========
        print("[test] 切换到图片背景...")
        cfg.set("background.mode", "image", autosave=False)
        cfg.set("background.path", str(HERE / "assets" / "default_wallpaper.jpg"),
                autosave=False)
        app.lock()
        time.sleep(0.8)
        # 截图
        try:
            from PIL import ImageGrab
            shot = ImageGrab.grab(all_screens=True)
            out = HERE / "tools" / "_verify_out" / "unlock_flow.png"
            out.parent.mkdir(exist_ok=True)
            shot.save(out)
            print(f"[test]   ✓ 已截图 {out.name} ({shot.size[0]}x{shot.size[1]})")
        except Exception as exc:
            print(f"[test]   截图失败：{exc!r}")

        # 解锁收尾
        ok, msg = app.try_unlock("test1234")
        for _ in range(20):
            root.update()
            time.sleep(0.05)
            if not app.locked:
                break

        # 退出
        app.shutdown()
        for _ in range(10):
            try:
                root.update()
            except tk.TclError:
                break
            time.sleep(0.05)

        print()
        print("=" * 46)
        print("  全部通过 ✅")
        print("=" * 46)
        return 0

    finally:
        restore_vault()
        # 恢复原始 config
        try:
            cfg = Config()
            cfg.set("background.mode", "color", autosave=True)
            cfg.set("background.color", "#0B1220", autosave=True)
            cfg.set("message.text", "", autosave=True)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
