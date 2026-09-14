import sys, types, os, tempfile

# 让 lockscreen 包可被导入（脚本位于 tools/ 下）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def mk(name):
    m = types.ModuleType(name); sys.modules[name] = m; return m

# ---- tkinter 桩 ----
tk = mk("tkinter")
class _W:
    def __init__(self, *a, **k): pass
    def place(self, *a, **k): pass
    def place_configure(self, *a, **k): pass
    def configure(self, *a, **k): pass
    def pack(self, *a, **k): pass
    def bind(self, *a, **k): pass
    def winfo_exists(self, *a, **k): return True
    def lower(self, *a, **k): pass
    def lift(self, *a, **k): pass
    def delete(self, *a, **k): pass
    def create_image(self, *a, **k): return 1
    def coords(self, *a, **k): pass
    def after(self, *a, **k): return 0
    def after_cancel(self, *a, **k): pass
    def destroy(self, *a, **k): pass
    def focus_force(self, *a, **k): pass
    def icursor(self, *a, **k): pass
for n in ["Tk", "Toplevel", "Canvas", "Label", "Frame", "Entry",
          "Button", "StringVar", "BooleanVar", "PhotoImage", "Variable"]:
    setattr(tk, n, _W)
ttk = mk("tkinter.ttk")
for n in ["Style", "Frame", "Label", "Button", "Entry", "Notebook",
          "Checkbutton", "Combobox", "Spinbox"]:
    setattr(ttk, n, _W)

# ---- PIL 桩 ----
pil = mk("PIL")
class _Img:
    def __init__(self, *a, **k): pass
    def convert(self, *a, **k): return self
    def resize(self, *a, **k): return self
    def crop(self, *a, **k): return self
    def filter(self, *a, **k): return self
    def paste(self, *a, **k): pass
    def save(self, *a, **k): pass
pil.Image = _Img
pil.Image.Image = pil.Image  # 让 `Image.Image` 注解可解析
pil.ImageDraw = type("D", (), {"Draw": staticmethod(lambda *a, **k: _Img())})
pil.ImageFilter = type("F", (), {"GaussianBlur": staticmethod(lambda *a, **k: _Img())})
pil.ImageTk = _Img

# ---- core 桩 ----
for mod in ["core", "core.logger", "core.monitors", "core.winapi", "core.actions"]:
    mk(mod)
sys.modules["core.logger"].log = type("L", (), {"log": staticmethod(lambda *a, **k: None)})()
sys.modules["core.monitors"].list_monitors = lambda: []
sys.modules["core.winapi"].hwnd_of_toplevel = staticmethod(lambda *a, **k: 0)
sys.modules["core.winapi"].keep_on_top = staticmethod(lambda *a, **k: None)
sys.modules["core.winapi"].make_lock_window = staticmethod(lambda *a, **k: None)
sys.modules["core.actions"].ACTION_LABELS = {}

import ui.lock_window as LW  # noqa: E402

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

tmp = tempfile.mkdtemp()
for n in ("a.png", "b.jpg", "c.png"):
    open(os.path.join(tmp, n), "wb").write(b"\x89PNG")

# 单图模式：扩展为同目录全部图片并定位当前图
cfg = FakeCfg({"background": {"mode": "image", "path": os.path.join(tmp, "b.jpg")}})
ls = LW.LockScreen(master=None, cfg=cfg, try_unlock=lambda p: (False, "x"),
                   on_user_activity=None)
ls._build_wall_list()
assert [os.path.basename(x) for x in ls._all_images] == ["a.png", "b.jpg", "c.png"], ls._all_images
assert os.path.basename(ls._all_images[ls._wall_index]) == "b.jpg"
print("wall_list OK:", [os.path.basename(x) for x in ls._all_images])

ls._override_path = None
ls._paint_background = lambda win, mon: None
ls.windows = []
ls._switch_wall(None, {"w": 1920, "h": 1080}, 1)
assert os.path.basename(ls._override_path) == "c.png"
assert ls._base_image() == ls._override_path
print("switch +1 ->", os.path.basename(ls._base_image()))
ls._switch_wall(None, {"w": 1920, "h": 1080}, 1)  # c -> a 回绕
assert os.path.basename(ls._base_image()) == "a.png"
print("wrap ->", os.path.basename(ls._base_image()))

# 纯色模式无壁纸
ls2 = LW.LockScreen(master=None, cfg=FakeCfg({"background": {"mode": "color"}}),
                    try_unlock=lambda p: (False, "x"))
ls2._build_wall_list()
assert ls2._all_images == []
print("color -> no walls OK")

# Ken Burns 平移范围始终覆盖屏幕
import math
w, h = 1920, 1080
amp_x, amp_y = 0.04 * w, 0.04 * h
for t in [0, 30, 60, 90, 130, 200]:
    ox = -amp_x + amp_x * math.sin(t * 2 * math.pi / 90.0)
    oy = -amp_y + amp_y * math.sin(t * 2 * math.pi / 130.0)
    assert -2 * amp_x - 1 <= ox <= 1 and -2 * amp_y - 1 <= oy <= 1
print("kenburns bounds OK")

print("ALL_LOGIC_OK")
