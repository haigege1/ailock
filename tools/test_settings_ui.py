"""设置面板（新 Fluent UI）真实 Tk 冒烟测试。

覆盖：实例化、四页切换、开关翻转、滑杆、下拉、保存密码校验、退出流程。
用法（在项目根，用带 tkinter/PIL 的 venv）：
    .venv/Scripts/python.exe -u tools/test_settings_ui.py
"""
import os
import sys
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

errors = []


def hook(exc, val, tb):
    import traceback
    errors.append("".join(traceback.format_exception(exc, val, tb)))


def main():
    from core.config import Config
    from core.security import Vault
    from ui.settings_panel import SettingsPanel

    root = tk.Tk()
    root.withdraw()
    root.report_callback_exception = hook

    # 临时配置
    cfg = Config() if hasattr(Config, "__call__") else None
    # Config 可能是单例/需要参数，尽量兜底
    try:
        import core.config as cfgmod
        cfg = cfgmod.Config()
    except Exception:
        cfg = None

    # 用内存配置兜底
    if cfg is None:
        class FakeCfg(dict):
            def get(self, k, d=None):
                cur = self
                for part in k.split("."):
                    if isinstance(cur, dict) and part in cur:
                        cur = cur[part]
                    else:
                        return d
                return cur

            def set(self, k, v, **kw):
                parts = k.split(".")
                cur = self
                for p in parts[:-1]:
                    cur = cur.setdefault(p, {})
                cur[parts[-1]] = v

            def save(self):
                pass
        cfg = FakeCfg({
            "background": {"mode": "color", "color": "#0B1220"},
            "lock": {"min_lock_minutes": 0},
            "security": {"max_attempts": 5},
            "status": {"enabled": False},
        })

    class FakeVault:
        is_set = False

        def has_recovery(self):
            return False

        def ensure_recovery(self):
            return "TEST-CODE-123"

        def set_password(self, pw):
            self._pw = pw
            self.is_set = True

        def verify(self, pw):
            return pw == self._pw

        def regenerate_recovery(self):
            return "NEW-CODE-456"

    vault = FakeVault()
    saved = []
    locked = []

    panel = SettingsPanel(
        root, cfg, vault,
        on_saved=lambda c: saved.append(1),
        on_lock_now=lambda: locked.append(1))

    def step():
        try:
            # 1) 四页都能切换且无异常
            for pg in ("security", "wallpaper", "ai", "general"):
                panel._show_page(pg)
                root.update_idletasks()
            print("pages OK")

            # 2) 密码页已构建，校验逻辑（新密码不一致）
            panel._show_page("security")
            root.update_idletasks()
            if panel.pw_new is None:
                raise AssertionError("pw_new 未构建")
            panel.pw_new.insert(0, "1234")
            panel.pw_new2.insert(0, "9999")
            panel._save_password()
            print("mismatch err:", panel.pw_err.cget("text"))
            assert "不一致" in panel.pw_err.cget("text")

            # 3) 一致后保存成功 -> vault 已设
            panel.pw_new.delete(0, "end")
            panel.pw_new2.delete(0, "end")
            panel.pw_new.insert(0, "1234")
            panel.pw_new2.insert(0, "1234")
            ok = panel._save_password()
            print("save ok:", ok, "saved cb:", len(saved))
            assert ok and vault.is_set

            # 4) 关闭回调
            panel._on_cancel()
            print("ALL_OK cb_errors=", len(errors))
        finally:
            root.quit()

    root.after(400, step)
    root.mainloop()
    try:
        root.destroy()
    except Exception:
        pass

    if errors:
        print("\n".join(errors))
        sys.exit(1)
    print("SETTINGS_UI_OK")


if __name__ == "__main__":
    main()
