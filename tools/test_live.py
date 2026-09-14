import sys, os, tempfile, tkinter as tk
from PIL import Image

# 把 lockscreen 加入路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ui.lock_window as LW

cb_errors = []

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
        self[k] = v

# 生成 3 张真实壁纸，放进临时文件夹
tmp = tempfile.mkdtemp()
for i, c in enumerate([(20, 60, 90), (40, 90, 60), (90, 50, 20)]):
    Image.new("RGB", (400, 300), c).save(os.path.join(tmp, f"w{i}.png"))

cfg = FakeCfg({
    "background": {"mode": "folder", "path": tmp, "kenburns": True,
                   "slideshow_seconds": 30, "dim": 0.45},
    "ui": {"animations": True},
    "lock": {"show_clock": True},
    "message": {"text": ""},
    "status": {"enabled": False},
})

root = tk.Tk()
root.withdraw()
root.report_callback_exception = lambda *a: cb_errors.append(a)

ls = LW.LockScreen(root, cfg, try_unlock=lambda p: (False, "密码错误"),
                   on_user_activity=None)
ls.show(min_lock_minutes=2)   # 触发最短锁定计时

def step():
    try:
        win = ls._primary_widgets.get("win")
        mon = getattr(win, "_ailock_mon", None)
        if win and mon:
            ls._switch_wall(win, mon, 1)   # 覆盖 + 重绘 + toast
        entry = ls._primary_widgets.get("pw_entry")
        if entry:
            entry.delete(0, "end")
            entry.insert(0, "wrong")
            ls._submit()                    # 触发错误摇晃
        print("step OK: walls=", len(ls._all_images),
              "override=", os.path.basename(ls._override_path or ""), flush=True)
    except Exception as e:
        print("STEP_EXC", repr(e), flush=True)
    root.after(400, root.quit)

root.after(500, step)
root.after(2500, root.quit)   # 硬超时兜底
try:
    root.mainloop()
except Exception as e:
    print("MAINLOOP_EXC", repr(e), flush=True)

ls.destroy()
print("cb_errors:", len(cb_errors))
for e in cb_errors[:3]:
    print("  ", e[0] if e else e)
print("LIVE_OK" if not cb_errors else "LIVE_HAD_ERRORS")
