"""设置面板 —— Windows 11 Fluent 风格（左导航 + 内容卡 + 即改即存）。

与设计稿对齐：常规 / 安全 / 壁纸 / AI 联动 四页左导航；
每项 = 标题 + 一句话说明 + 控件；开关与下拉即改即存，
仅密码页保留显式「保存密码」主按钮 + 「已保存 ✓」反馈。
"""

import math
import os
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox

from core import actions as A
from core import autostart
from core.config import app_dir, default_status_path
from core.logger import Logger
from core.security import Vault

# ---------- 设计变量（对齐 AiLock-UI设计方案.html） ----------
C_BG = "#F3F4F6"                 # 窗口底色（浅灰）
C_CARD = "#FFFFFF"
C_TEXT = "#1F2937"
C_DIM = "#4B5563"
C_FAINT = "#9CA3AF"
C_ACCENT = "#185FA5"             # 主色
C_ACCENT_BG = "#E6F1FB"          # 选中态浅蓝底
C_BORDER = "#E2E5EA"
C_DANGER = "#A32D2D"
C_DANGER_BG = "#FDEDED"
C_OK = "#0F6E56"
C_TRACK_OFF = "#C7CBD1"

RADII = 10                        # 卡片圆角
RADII_BTN = 6

try:
    _ALL_FONTS = set(tkfont.families())
    _UI = "Segoe UI" if "Segoe UI" in _ALL_FONTS else "Microsoft YaHei UI"
except Exception:
    _UI = "Microsoft YaHei UI"

F_TITLE = (_UI, 14, "bold")
F_BODY = (_UI, 13)
F_BODY_B = (_UI, 13, "bold")
F_CAP = (_UI, 12)
F_CODE = ("Consolas", 10)

# 动作选项：[(存储 key, 中文标签), ...]
ACTION_OPTS = list(A.ACTION_LABELS.items())
_MODE_OPTS = [("color", "纯色"), ("image", "单张图片"), ("folder", "文件夹轮播")]
_FIT_OPTS = [("cover", "铺满裁剪"), ("contain", "完整包含"),
             ("stretch", "拉伸填满"), ("center", "居中"),
             ("tile", "平铺重复")]


def _rrect_points(x0, y0, x1, y1, r, steps=10):
    pts = []

    def arc(cx, cy, a0, a1):
        for i in range(steps + 1):
            a = math.radians(a0 + (a1 - a0) * i / steps)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    arc(x0 + r, y0 + r, 180, 270)
    arc(x1 - r, y0 + r, 270, 360)
    arc(x1 - r, y1 - r, 0, 90)
    arc(x0 + r, y1 - r, 90, 180)
    return pts


def _rrect(cv, x0, y0, x1, y1, r, **kw):
    return cv.create_polygon(_rrect_points(x0, y0, x1, y1, r),
                             smooth=True, splinesteps=12, **kw)


# ----------------------------------------------------------------
# 开关（Fluent 胶囊，带动画）
# ----------------------------------------------------------------
class Switch(tk.Canvas):
    W, H = 44, 22

    def __init__(self, parent, variable, command=None, bg="#FFFFFF"):
        super().__init__(parent, width=self.W, height=self.H, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._var = variable
        self._cmd = command
        self._anim_id = None
        self._knob_x = 0.0
        self.bind("<Button-1>", self._on_click)
        self._redraw(instant=True)
        self._var.trace_add("write", lambda *a: self._on_var())

    def _state(self):
        return bool(self._var.get())

    def _knob_target(self):
        on = self._state()
        return (self.W - 9) if on else 7   # 9=轨道高-2-半径

    def _redraw(self, instant=False):
        if self._anim_id:
            try:
                self.after_cancel(self._anim_id)
            except Exception:
                pass
            self._anim_id = None
        if instant or not self._knob_x:
            self._knob_x = float(self._knob_target())

        def animate(step=0, total=8):
            t = 1 - (1 - (step + 1) / total) ** 3
            self._knob_x = float(self._from_x) + \
                (float(self._to_x) - float(self._from_x)) * t
            self._paint()
            if step < total - 1:
                self._anim_id = self.after(12, lambda: animate(step + 1, total))
            else:
                self._anim_id = None
                self._knob_x = float(self._to_x)

        self._from_x = self._knob_x
        self._to_x = self._knob_target()
        if abs(self._from_x - self._to_x) < 0.5:
            self._paint()
        else:
            animate()

    def _paint(self):
        self.delete("all")
        on = self._state()
        track = C_ACCENT if on else C_TRACK_OFF
        _rrect(self, 1, 1, self.W - 1, self.H - 1, self.H / 2 - 1,
               fill=track, outline=track)
        y = self.H / 2
        self.create_oval(self._knob_x - 7, y - 7,
                         self._knob_x + 7, y + 7,
                         fill="#FFFFFF", outline="#FFFFFF")

    def _on_var(self):
        # 程序化 set() 也刷新；不触发 command 防止循环
        self._redraw()

    def _on_click(self, _e):
        self._var.set(not self._state())
        self._redraw()
        if self._cmd:
            self._cmd(self._state())
        return "break"

    def set(self, value, notify=True):
        self._var.set(bool(value))
        if notify and self._cmd:
            self._cmd(self._state())


# ----------------------------------------------------------------
# 内容卡：白底圆角 + 内嵌 body
# ----------------------------------------------------------------
class Card(tk.Canvas):
    def __init__(self, parent, bg=C_BG):
        super().__init__(parent, bg=bg, highlightthickness=0, bd=0)
        self._body = None
        self._inner_item = None
        self._fit_id = None
        self.bind("<Configure>", self._on_resize)

    def body(self):
        if self._body is None:
            self._body = tk.Frame(self, bg=C_CARD)
            self._inner_item = self.create_window(
                0, 0, anchor="nw", window=self._body)
            self._body.bind("<Configure>", self._on_body_resize)
            # 先画一次白底（不依赖 resize 事件）
            self.after_idle(self._paint_bg)
        return self._body

    def _on_resize(self, e):
        w = max(2, e.width)
        self._paint_bg()
        if self._inner_item is not None:
            pad = 20
            self.coords(self._inner_item, 0, 0)
            self.itemconfigure(self._inner_item, width=max(2, w - 2 * pad),
                               anchor="nw")
            # 让 body 重新布局，随后 body 高度回调再更新卡高
        if self._body is not None:
            self._body.update_idletasks()
            self._sync_height()

    def _paint_bg(self):
        self.delete("bg")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 10 or h < 10:
            return
        _rrect(self, 0, 0, w - 1, h - 1, RADII,
               fill=C_CARD, outline=C_BORDER, width=1)

    def _on_body_resize(self, e):
        self._sync_height()

    def _sync_height(self):
        if self._body is None:
            return
        req = self._body.winfo_reqheight()
        pad = 20
        target = req + 2 * pad + 2
        if abs(self.winfo_height() - target) > 1:
            self.configure(height=target)
            self._paint_bg()


# ----------------------------------------------------------------
# 行组：标题 + 说明 + 右侧控件
# ----------------------------------------------------------------
class Rows:
    """在卡片 body 上按「左文字 / 右控件」排版若干设置行。"""

    def __init__(self, parent):
        self.parent = parent

    def add(self, title, desc=None, right_builder=None):
        """right_builder(frame) 负责把控件放进右侧 frame。
        返回 (row_frame, right_frame)。"""
        row = tk.Frame(self.parent, bg=C_CARD)
        row.pack(fill="x", pady=(0, 14))
        # 左：标题 + 说明
        left = tk.Frame(row, bg=C_CARD)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text=title, font=F_BODY_B, fg=C_TEXT, bg=C_CARD,
                 anchor="w").pack(fill="x")
        if desc:
            tk.Label(left, text=desc, font=F_CAP, fg=C_DIM, bg=C_CARD,
                     anchor="w", justify="left", wraplength=360).pack(
                fill="x", pady=(3, 0))
        # 右：控件区
        right = tk.Frame(row, bg=C_CARD)
        right.pack(side="right", padx=(16, 0))
        if right_builder:
            right_builder(right)
        return row, right

    def note(self, text, color=C_FAINT, font=None):
        tk.Label(self.parent, text=text, font=font or F_CAP, fg=color,
                 bg=C_CARD, anchor="w", justify="left",
                 wraplength=460).pack(fill="x", pady=(0, 14))


# ----------------------------------------------------------------
# 小组件
# ----------------------------------------------------------------
def _entry(parent, width=20, show=None):
    return tk.Entry(parent, font=F_BODY, fg=C_TEXT, show=show,
                    relief="flat", bd=0, bg="#FFFFFF",
                    highlightthickness=1, highlightbackground=C_BORDER,
                    highlightcolor=C_ACCENT, insertbackground=C_TEXT,
                    width=width)


def _spin(parent, variable, lo, hi, width=6):
    sb = tk.Spinbox(parent, textvariable=variable, from_=lo, to=hi,
                    font=F_BODY, fg=C_TEXT, relief="flat", bd=0,
                    bg="#FFFFFF", highlightthickness=1,
                    highlightbackground=C_BORDER, highlightcolor=C_ACCENT,
                    buttonbackground="#F3F4F6", width=width, justify="center")
    return sb


def _combo(parent, variable, opts, width=14):
    """opts: [(value, label)] 列表。显示 label，变量存 label。"""
    labels = [lb for _, lb in opts]
    cb = tk.OptionMenu.__self__ and None
    # 用 ttk.Combobox 保留下拉样式
    from tkinter import ttk
    box = ttk.Combobox(parent, textvariable=variable, values=labels,
                       state="readonly", width=width, font=F_BODY)
    return box


def _btn(parent, text, cmd, kind="secondary"):
    bg, fg, abg, afg = {
        "primary": (C_ACCENT, "#FFFFFF", "#0F4A82", "#FFFFFF"),
        "secondary": ("#F3F4F6", C_TEXT, "#E2E5EA", C_TEXT),
        "danger": (C_DANGER_BG, C_DANGER, "#F8D9D9", C_DANGER),
    }[kind]
    b = tk.Button(parent, text=text, command=cmd, font=F_BODY,
                  bg=bg, fg=fg, activebackground=abg, activeforeground=afg,
                  relief="flat", bd=0, cursor="hand2", padx=16, pady=6)
    return b


def _nav_item(parent, text, bg):
    tk.Label(parent, text=text, font=F_BODY, bg=bg, fg=C_TEXT,
             anchor="w").pack(fill="x")


# ----------------------------------------------------------------
# 主面板
# ----------------------------------------------------------------
class SettingsPanel:
    def __init__(self, master, cfg, vault: Vault, on_saved=None, on_lock_now=None,
                 on_cancel=None):
        self.master = master
        self.cfg = cfg
        self.vault = vault
        self.on_saved = on_saved
        self.on_lock_now = on_lock_now
        self.on_cancel = on_cancel
        self._built_pages = set()

        self.win = tk.Toplevel(master)
        self.win.title("AiLock 设置")
        self.win.geometry("820x600")
        self.win.minsize(680, 520)
        self.win.configure(bg=C_BG)
        try:
            self.win.iconbitmap(str(app_dir() / ".." / "ailock.ico"))
        except Exception:
            pass

        self._body = self._build_frame()
        self._show_page("general")

        # 注意：不要 transient() 到 withdraw 的主窗口上
        try:
            self.win.protocol("WM_DELETE_WINDOW", self._on_cancel)
        except Exception:
            pass

    # ---------------- 骨架 ----------------
    def _build_frame(self):
        root = tk.Frame(self.win, bg=C_BG)
        root.pack(fill="both", expand=True)

        # ===== 左侧导航 =====
        nav = tk.Frame(root, bg=C_BG, width=176)
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)

        tk.Label(nav, text="AiLock", font=F_TITLE, fg=C_TEXT, bg=C_BG,
                 anchor="w").pack(fill="x", padx=(20, 8), pady=(22, 2))
        tk.Label(nav, text="设置", font=F_CAP, fg=C_FAINT, bg=C_BG,
                 anchor="w").pack(fill="x", padx=(20, 8), pady=(0, 18))

        self._nav_btns = {}
        for key, label in (("general", "常规"), ("security", "安全"),
                           ("wallpaper", "壁纸"), ("ai", "AI 联动")):
            b = tk.Frame(nav, bg=C_BG, cursor="hand2")
            b.pack(fill="x", padx=8, pady=1)
            inner = tk.Frame(b, bg=C_BG, height=34)
            inner.pack(fill="x")
            inner.pack_propagate(False)
            txt = tk.Label(inner, text=label, font=F_BODY, bg=C_BG,
                           fg=C_DIM, anchor="w")
            txt.pack(fill="both", expand=True, padx=(16, 8))
            b._txt = txt
            for w in (b, inner, txt):
                w.bind("<Button-1>", lambda e, k=key: self._show_page(k))
                w.bind("<Enter>", lambda e, i=inner, k=key: (
                    i.configure(bg=C_ACCENT_BG if k not in
                                self._active_page else "#DCEBFA")
                    if False else i.configure(bg="#E9EDF2")))
                w.bind("<Leave>", lambda e, i=inner, k=key: i.configure(
                    bg=C_ACCENT_BG if getattr(self, "_active_page", None) == k
                    else C_BG))
            self._nav_btns[key] = (b, inner, txt)

        # 底部小字
        tk.Label(nav, text="v1.0 · 即改即存", font=F_CAP, fg=C_FAINT,
                 bg=C_BG, anchor="w").pack(side="bottom", fill="x",
                                           padx=20, pady=16)

        # ===== 右侧主区 =====
        right = tk.Frame(root, bg=C_BG)
        right.pack(side="left", fill="both", expand=True)

        # 顶栏：标题 + 动作按钮
        topbar = tk.Frame(right, bg=C_BG)
        topbar.pack(fill="x", padx=(4, 24), pady=(20, 12))
        self._page_title = tk.Label(topbar, text="", font=F_TITLE,
                                    fg=C_TEXT, bg=C_BG, anchor="w")
        self._page_title.pack(side="left", fill="x", expand=True)
        self._btn_lock = _btn(topbar, "立即锁定", self._lock_now, "primary")
        self._btn_lock.pack(side="right", padx=(8, 0))
        self._btn_log = _btn(topbar, "打开日志", self._open_log)
        self._btn_log.pack(side="right", padx=(8, 0))

        # 内容：可滚动
        scroll_bg = tk.Frame(right, bg=C_BG)
        scroll_bg.pack(fill="both", expand=True, padx=(8, 24), pady=(0, 20))
        cv = tk.Canvas(scroll_bg, bg=C_BG, highlightthickness=0, bd=0)
        sb = tk.Scrollbar(scroll_bg, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        self._canvas = cv
        holder = tk.Frame(cv, bg=C_BG)
        self._holder = holder
        self._holder_id = cv.create_window((0, 0), window=holder, anchor="nw")

        def _cfg_scroll(_e=None):
            cv.configure(scrollregion=cv.bbox("all"))
            if holder.winfo_reqwidth() > 0:
                cv.itemconfigure(self._holder_id, width=cv.winfo_width())
        holder.bind("<Configure>", _cfg_scroll)
        cv.bind("<Configure>", _cfg_scroll)
        cv.bind("<Enter>", lambda e: cv.bind_all("<MouseWheel>",
                                                 self._on_wheel))
        cv.bind("<Leave>", lambda e: cv.unbind_all("<MouseWheel>"))

        # 页面容器（懒构建）
        self._pages = {}
        for key, _ in (("general", "常规"), ("security", "安全"),
                       ("wallpaper", "壁纸"), ("ai", "AI 联动")):
            pg = tk.Frame(holder, bg=C_BG)
            self._pages[key] = pg

        return root

    def _on_wheel(self, e):
        try:
            self._canvas.yview_scroll(int(-e.delta / 120), "units")
        except Exception:
            pass
        return "break"

    def _show_page(self, key):
        self._active_page = key
        titles = {"general": "常规", "security": "安全",
                  "wallpaper": "壁纸", "ai": "AI 联动"}
        self._page_title.configure(text=titles[key])
        for k, pg in self._pages.items():
            pg.pack_forget() if k != key else pg.pack(fill="both", expand=True)
            _, inner, txt = self._nav_btns[k]
            if k == key:
                inner.configure(bg=C_ACCENT_BG)
                txt.configure(fg=C_ACCENT, font=F_BODY_B)
            else:
                inner.configure(bg=C_BG)
                txt.configure(fg=C_DIM, font=F_BODY)
        if key not in self._built_pages:
            self._built_pages.add(key)
            getattr(self, "_build_" + key)(self._pages[key])
        try:
            self._canvas.yview_moveto(0)
        except Exception:
            pass

    # ---------------- 通用控件绑定 ----------------
    def _immediate(self, key, value, kind="raw"):
        """kind: raw / int / dim(0-100 转 0-1) / str"""
        try:
            if kind == "int":
                value = int(float(str(value).strip() or 0))
            elif kind == "dim":
                value = max(0.0, min(1.0, float(value) / 100.0))
            elif kind == "str":
                value = str(value).strip()
            self.cfg.set(key, value, autosave=False)
            self.cfg.save()
            if key == "autostart":
                try:
                    autostart.sync(bool(value))
                except Exception:
                    pass
        except Exception:
            return
        if self.on_saved:
            try:
                self.on_saved(self.cfg)
            except Exception:
                pass

    def _add_switch(self, rows, key, title, desc=None, default=False):
        var = tk.BooleanVar(value=bool(self.cfg.get(key, default)))
        var.__set = False

        def on_change(val):
            if var.__set:
                return
            self._immediate(key, val)
        sw = Switch(rows.parent, var, command=on_change)
        sw.pack(side="right")
        rows.add(title, desc)
        # 保存变量便于外部读取（如 _lock_now 前统一 flush）
        setattr(self, "_var_" + key.replace(".", "_"), var)
        return var

    def _add_entry(self, rows, key, title, desc=None, default="", width=20,
                   show=None):
        var = tk.StringVar(value=str(self.cfg.get(key, default)))
        var.__saving = False

        def flush():
            if var.__saving:
                return
            var.__saving = True
            try:
                self._immediate(key, var.get(), "str")
            finally:
                var.__saving = False
        rows.add(title, desc, lambda f: (
            _entry(f, width=width, show=show).pack(side="right"),
            setattr(self, "_e_" + key.replace(".", "_"),
                    f.winfo_children()[-1]),
            f.winfo_children()[-1].bind("<FocusOut>", lambda e: flush())))
        setattr(self, "_var_" + key.replace(".", "_"), var)
        return var

    def _add_spin(self, rows, key, title, desc=None, lo=0, hi=1440,
                  default=0, unit=""):
        var = tk.StringVar(value=str(self.cfg.get(key, default)))

        def flush():
            try:
                v = int(float(str(var.get()).strip() or 0))
            except ValueError:
                return
            self._immediate(key, v, "int")
            if unit:
                var.set(str(v) + (" " + unit if False else ""))
                # 重新取纯数值：去掉单位
                var.set(str(v))
        rows.add(title, desc, lambda f: (
            _spin(f, var, lo, hi).pack(side="right"),
            f.winfo_children()[-1].bind("<FocusOut>", lambda e: flush()),
            f.winfo_children()[-1].bind("<Return>", lambda e: flush())))
        setattr(self, "_var_" + key.replace(".", "_"), var)
        return var

    def _add_combo(self, rows, key, title, desc=None, opts=None,
                   default=None, width=14):
        from tkinter import ttk
        opts = opts or []
        cur = self.cfg.get(key, default)
        labels = [lb for _, lb in opts]
        cur_label = next((lb for v, lb in opts if v == cur), None)
        if cur_label is None:
            cur_label = labels[0] if labels else ""
        var = tk.StringVar(value=cur_label)

        def flush():
            lb = var.get()
            value = next((v for v, l in opts if l == lb), None)
            if value is not None:
                self._immediate(key, value)
        box = ttk.Combobox(rows.parent, textvariable=var, values=labels,
                           state="readonly", width=width, font=F_BODY)
        box.pack(side="right")
        box.bind("<<ComboboxSelected>>", lambda e: flush())
        rows.add(title, desc)
        setattr(self, "_var_" + key.replace(".", "_"), var)
        return var

    def _add_scale(self, rows, key, title, desc=None, lo=0, hi=100,
                   default=0, kind="int"):
        """滑杆；kind=dim 时内部值 0-1 映射 UI 0-100。"""
        if kind == "dim":
            cur = float(self.cfg.get(key, 0.35))
            shown = int(round(cur * 100))
        else:
            shown = int(self.cfg.get(key, default))
        var = tk.IntVar(value=shown)
        scale = tk.Scale(rows.parent, from_=lo, to=hi, orient="horizontal",
                         variable=var, showvalue=False, length=170,
                         bg=C_CARD, fg=C_ACCENT, troughcolor="#E2E5EA",
                         highlightthickness=0, bd=0, activebackground=C_ACCENT,
                         sliderlength=18, width=10, cursor="hand2")
        val_lab = tk.Label(rows.parent, text=f"{shown}", font=F_BODY,
                           fg=C_DIM, bg=C_CARD, width=4)
        scale.pack(side="right")
        val_lab.pack(side="right", padx=(4, 0))

        def on_release(_e):
            v = int(var.get())
            val_lab.configure(text=str(v))
            self._immediate(key, v, kind)
        scale.bind("<ButtonRelease-1>", on_release)
        scale.bind("<B1-Motion>", lambda e: val_lab.configure(text=str(var.get())))
        rows.add(title, desc)
        setattr(self, "_var_" + key.replace(".", "_"), var)
        return var

    def _add_path(self, rows, key, title, desc, is_dir=False):
        var = tk.StringVar(value=str(self.cfg.get(key, "")))

        def pick():
            if is_dir:
                p = filedialog.askdirectory(title="选择文件夹")
            else:
                p = filedialog.askopenfilename(
                    title="选择图片", filetypes=[
                        ("图片", "*.jpg *.jpeg *.png *.bmp *.webp"),
                        ("所有文件", "*.*")])
            if p:
                var.set(p)
                self._immediate(key, p)
                # 顺带把背景来源切到 image/folder
                self._immediate("background.mode",
                                "folder" if is_dir else "image")
        e = _entry(rows.parent, width=26)
        e.insert(0, var.get())
        e.pack(side="right")
        b = _btn(rows.parent, "浏览…", pick)
        b.pack(side="right", padx=(0, 6))

        def flush():
            self._immediate(key, e.get(), "str")
        e.bind("<FocusOut>", lambda ev: flush())
        var.__entry = e
        rows.add(title, desc)
        setattr(self, "_var_" + key.replace(".", "_"), var)
        return var

    # ============ 页面：常规 ============
    def _build_general(self, page):
        c1 = Card(page)
        c1.pack(fill="x", pady=(0, 16))
        b1 = c1.body()
        tk.Label(b1, text="启动与常驻", font=F_TITLE, fg=C_TEXT, bg=C_CARD,
                 anchor="w").pack(fill="x", pady=(0, 16))
        r1 = Rows(b1)
        self._add_switch(r1, "autostart", "开机自启",
                         "随 Windows 启动，在后台常驻")
        self._add_switch(r1, "lock.lock_on_start", "启动后立即锁屏",
                         "程序启动即进入锁屏状态")
        self._add_switch(r1, "display.keep_on", "锁屏期间禁止休眠",
                         "AI 任务运行时主机不休眠、不熄屏")

        c2 = Card(page)
        c2.pack(fill="x", pady=(0, 16))
        b2 = c2.body()
        tk.Label(b2, text="锁定规则", font=F_TITLE, fg=C_TEXT, bg=C_CARD,
                 anchor="w").pack(fill="x", pady=(0, 16))
        r2 = Rows(b2)
        self._add_spin(r2, "lock.auto_lock_idle_minutes", "空闲自动锁定",
                       "空闲多少分钟后自动锁屏，0 = 关闭",
                       lo=0, hi=1440, unit="min")
        self._add_spin(r2, "lock.min_lock_minutes", "最短锁定时长",
                       "至少锁定多久才能解锁（防误触）",
                       lo=0, hi=1440, unit="min")
        self._add_entry(r2, "hotkey", "全局锁屏热键",
                        "如 ctrl+alt+l", default="ctrl+alt+l", width=16)

        c3 = Card(page)
        c3.pack(fill="x", pady=(0, 16))
        b3 = c3.body()
        tk.Label(b3, text="锁屏界面", font=F_TITLE, fg=C_TEXT, bg=C_CARD,
                 anchor="w").pack(fill="x", pady=(0, 16))
        r3 = Rows(b3)
        self._add_switch(r3, "lock.show_clock", "显示时钟",
                         "锁屏界面显示大时钟")
        self._add_switch(r3, "ui.animations", "界面动效",
                         "入场滑入、密码错误摇晃（笔记本省电可关）", default=True)
        self._add_entry(r3, "message.text", "锁屏提示语",
                        "显示在锁屏底部的自定义文字",
                        default="", width=24)

    # ============ 页面：安全 ============
    def _build_security(self, page):
        c1 = Card(page)
        c1.pack(fill="x", pady=(0, 16))
        b1 = c1.body()
        tk.Label(b1, text="锁屏密码", font=F_TITLE, fg=C_TEXT, bg=C_CARD,
                 anchor="w").pack(fill="x", pady=(0, 4))
        has = self.vault.is_set
        if not has:
            tk.Label(b1, text="尚未设置密码。", font=F_CAP, fg="#BA7517",
                     bg=C_CARD, anchor="w").pack(fill="x", pady=(0, 12))
        else:
            tk.Label(b1, text="已设置。修改需要验证当前密码。", font=F_CAP,
                     fg=C_OK, bg=C_CARD, anchor="w").pack(fill="x",
                                                          pady=(0, 12))
        rows = Rows(b1)
        self.pw_status_lab = None
        self.pw_old = self._add_pw_entry(rows, "old", "当前密码",
                                         has, 18)
        self.pw_new = self._add_pw_entry(rows, "new", "新密码", True, 18)
        self.pw_new2 = self._add_pw_entry(rows, "new2", "确认新密码", True, 18)

        self.pw_err = tk.Label(b1, text="", font=F_CAP, fg=C_DANGER,
                               bg=C_CARD, anchor="w")
        self.pw_err.pack(fill="x", pady=(2, 10))
        btnrow = tk.Frame(b1, bg=C_CARD)
        btnrow.pack(fill="x")
        self._btn_save = _btn(btnrow, "保存密码", self._save_password,
                              "primary")
        self._btn_save.pack(side="left")
        self._btn_save_hint = tk.Label(btnrow, text="", font=F_CAP,
                                       fg=C_OK, bg=C_CARD)
        self._btn_save_hint.pack(side="left", padx=10)

        c2 = Card(page)
        c2.pack(fill="x", pady=(0, 16))
        b2 = c2.body()
        tk.Label(b2, text="恢复码", font=F_TITLE, fg=C_TEXT, bg=C_CARD,
                 anchor="w").pack(fill="x", pady=(0, 16))
        r2 = Rows(b2)
        self._add_entry(r2, "recovery", "恢复码（只显示一次）",
                        "忘记密码时用它解锁。重新生成会使旧码失效。",
                        width=26, default="")
        b = _btn(b2, "生成 / 重新生成", self._gen_recovery)
        b.pack(anchor="w")

        c3 = Card(page)
        c3.pack(fill="x", pady=(0, 16))
        b3 = c3.body()
        tk.Label(b3, text="防暴力破解", font=F_TITLE, fg=C_TEXT, bg=C_CARD,
                 anchor="w").pack(fill="x", pady=(0, 16))
        r3 = Rows(b3)
        self._add_spin(r3, "security.max_attempts", "最大尝试次数",
                       "连续输错达到次数后锁定输入",
                       lo=3, hi=20, unit="次")
        self._add_spin(r3, "security.cooldown_seconds", "冷却时间（秒）",
                       "锁定后需等待多久",
                       lo=0, hi=3600, unit="s")
        self._add_switch(r3, "security.lock_ui_on_fail",
                         "失败后显示冷却倒计时",
                         "锁屏界面展示剩余等待时间", default=True)

    def _add_pw_entry(self, rows, kind, label, enable, width):
        var = tk.StringVar(value="")
        e = _entry(rows.parent, width=width, show="●")
        e.configure(textvariable=var)
        if not enable:
            e.configure(state="disabled")
        e.pack(side="right")
        rows.add(label, None)
        return e

    # ============ 页面：壁纸 ============
    def _build_wallpaper(self, page):
        c1 = Card(page)
        c1.pack(fill="x", pady=(0, 16))
        b1 = c1.body()
        tk.Label(b1, text="背景", font=F_TITLE, fg=C_TEXT, bg=C_CARD,
                 anchor="w").pack(fill="x", pady=(0, 16))
        r1 = Rows(b1)
        self._add_combo(r1, "background.mode", "来源",
                        "决定背景如何显示", opts=_MODE_OPTS,
                        default="color")
        self._add_path(r1, "background.path", "图片 / 文件夹",
                       "选择单张图片，或包含多张图的文件夹轮播",
                       is_dir=False)

        c2 = Card(page)
        c2.pack(fill="x", pady=(0, 16))
        b2 = c2.body()
        tk.Label(b2, text="效果", font=F_TITLE, fg=C_TEXT, bg=C_CARD,
                 anchor="w").pack(fill="x", pady=(0, 16))
        r2 = Rows(b2)
        self._add_combo(r2, "background.fit", "填充方式",
                        "图片如何适配屏幕", opts=_FIT_OPTS, default="cover")
        self._add_scale(r2, "background.dim", "暗化遮罩",
                        "压暗背景突出时钟", kind="dim")
        self._add_scale(r2, "background.blur", "高斯模糊",
                        "背景模糊程度，0 为不模糊", lo=0, hi=40)
        self._add_entry(r2, "background.color", "纯色背景",
                        "来源为「纯色」时的背景色，如 #0B1220",
                        default="#0B1220", width=12)

        c3 = Card(page)
        c3.pack(fill="x", pady=(0, 16))
        b3 = c3.body()
        tk.Label(b3, text="轮播与动效", font=F_TITLE, fg=C_TEXT, bg=C_CARD,
                 anchor="w").pack(fill="x", pady=(0, 16))
        r3 = Rows(b3)
        self._add_spin(r3, "background.slideshow_seconds", "轮播间隔（秒）",
                       "文件夹模式下多久切一张",
                       lo=5, hi=3600, unit="s")
        self._add_switch(r3, "background.kenburns", "壁纸缓移动效",
                         "背景缓慢平移，让壁纸「活」起来",
                         default=True)

    # ============ 页面：AI 联动 ============
    def _build_ai(self, page):
        c1 = Card(page)
        c1.pack(fill="x", pady=(0, 16))
        b1 = c1.body()
        tk.Label(b1, text="任务状态", font=F_TITLE, fg=C_TEXT, bg=C_CARD,
                 anchor="w").pack(fill="x", pady=(0, 16))
        r1 = Rows(b1)
        self._add_switch(r1, "status.enabled", "在锁屏显示任务进度",
                         "任务运行时锁屏底部显示状态条", default=True)
        self._add_path(r1, "status.path", "状态文件",
                       "AI 工具写入状态的 JSON 路径",
                       is_dir=False)
        self._add_spin(r1, "status.poll_seconds", "轮询间隔（秒）",
                       "每几秒读取一次状态文件",
                       lo=1, hi=60, unit="s")

        c2 = Card(page)
        c2.pack(fill="x", pady=(0, 16))
        b2 = c2.body()
        tk.Label(b2, text="任务完成时", font=F_TITLE, fg=C_TEXT, bg=C_CARD,
                 anchor="w").pack(fill="x", pady=(0, 16))
        r2 = Rows(b2)
        self._add_combo(r2, "status.on_done_action", "执行动作",
                        "任务跑完后自动执行", opts=ACTION_OPTS,
                        default="shutdown")
        self._add_spin(r2, "status.on_done_countdown", "倒计时（秒）",
                       "执行前等待，输入密码可取消",
                       lo=0, hi=3600, unit="s")

        c3 = Card(page)
        c3.pack(fill="x", pady=(0, 16))
        b3 = c3.body()
        tk.Label(b3, text="异常处理", font=F_TITLE, fg=C_TEXT, bg=C_CARD,
                 anchor="w").pack(fill="x", pady=(0, 16))
        r3 = Rows(b3)
        self._add_combo(r3, "status.on_failed_action", "任务失败",
                        "检测到失败状态时", opts=ACTION_OPTS,
                        default="none")
        self._add_spin(r3, "status.stale_minutes", "失联判定（分钟）",
                       "状态文件多久未更新视为失联，0=关闭",
                       lo=0, hi=1440, unit="min")
        self._add_combo(r3, "status.on_stale_action", "任务失联",
                        "AI 进程消失时", opts=ACTION_OPTS,
                        default="none")
        r3.note("对接示例：notify.py --state running -p 24 -l \"epoch 12/50\"；"
                "任务结束 notify.py --state done。详见 README。",
                font=F_CODE)

    # ============ 密码保存 ============
    def _save_password(self):
        new = self.pw_new.get() if self.pw_new else ""
        new2 = self.pw_new2.get() if self.pw_new2 else ""
        old = (self.pw_old.get() if self.pw_old
               and str(self.pw_old.cget("state")) != "disabled" else "")
        self.pw_err.configure(text="")
        if not new and not new2:
            self.pw_err.configure(text="请输入新密码。")
            return False
        if new != new2:
            self.pw_err.configure(text="两次输入的新密码不一致。")
            return False
        if len(new) < 4:
            self.pw_err.configure(text="密码至少需要 4 位。")
            return False
        if self.vault.is_set and not self.vault.verify(old):
            self.pw_err.configure(text="当前密码不正确。")
            return False

        try:
            self.vault.set_password(new)
        except Exception as exc:
            self.pw_err.configure(text=f"保存失败：{exc}")
            return False
        if not self.vault.has_recovery():
            code = self.vault.ensure_recovery()
            self.win.clipboard_clear()
            self.win.clipboard_append(code)
            messagebox.showinfo(
                "请保存恢复码",
                f"首次设置密码，已生成恢复码：\n\n    {code}\n\n"
                "已复制到剪贴板。忘记密码时可用它解锁，请立即抄写保存。")

        for e in (self.pw_old, self.pw_new, self.pw_new2):
            if e:
                try:
                    e.delete(0, "end")
                except Exception:
                    pass
        self._btn_save_hint.configure(text="已保存 ✓")
        self.win.after(2000, lambda: self._btn_save_hint.configure(text=""))
        if self.on_saved:
            try:
                self.on_saved(self.cfg)
            except Exception:
                pass
        return True

    def _gen_recovery(self):
        if not messagebox.askyesno(
                "生成恢复码",
                "将生成新的恢复码，旧的恢复码立即失效。\n\n"
                "生成后请立即抄写保存，关闭窗口后无法再次查看。\n\n继续？"):
            return
        code = self.vault.regenerate_recovery()
        self.win.clipboard_clear()
        self.win.clipboard_append(code)
        messagebox.showinfo(
            "恢复码已生成",
            f"你的恢复码是：\n\n    {code}\n\n"
            "已复制到剪贴板。请抄写到安全的地方，这是忘记密码时唯一的解锁方式。")
        # 同步到输入框
        v = getattr(self, "_var_recovery", None)
        if v is not None:
            v.set(code)

    def _flush_entries(self):
        """把所有未失焦的文本域写盘（立即锁定前兜底）。"""
        for name, var in list(vars(self).items()):
            if name.startswith("_var_") and isinstance(var, tk.StringVar):
                key = name[5:].replace("_", ".")
                if key in ("message.text", "hotkey", "background.color",
                           "background.path", "status.path"):
                    try:
                        self.cfg.set(key, var.get().strip(), autosave=False)
                    except Exception:
                        pass
        self.cfg.save()
        if self.on_saved:
            try:
                self.on_saved(self.cfg)
            except Exception:
                pass

    # ============ 动作 ============
    def _on_cancel(self):
        if self.on_cancel:
            self.on_cancel()
        else:
            self.win.destroy()

    def _lock_now(self):
        self._flush_entries()
        # 关闭窗口（走回调复位打开标记），再执行锁定
        if self.on_cancel:
            self.on_cancel()
        else:
            try:
                self.win.destroy()
            except Exception:
                pass
        if self.on_lock_now:
            self.win.after(50, self.on_lock_now)

    def _open_log(self):
        path = Logger().path
        if path.exists():
            os.startfile(str(path))
        else:
            messagebox.showinfo("日志", "暂无日志记录。")
