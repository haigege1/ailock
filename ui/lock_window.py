"""锁屏界面。

每个显示器一个无边框全屏窗口：
  - 主屏：时钟 + 状态面板 + 口令输入 + 待执行动作倒计时
  - 副屏：时钟 + 状态面板 + 提示语（不提供输入，避免多屏同时输入打架）

线程安全约定：所有 tkinter 操作只能在主线程进行。
状态监控线程通过 master.after(0, ...) 把数据丢回主线程。
"""

import math
import os
import random
import time
from pathlib import Path

import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageDraw, ImageFilter, ImageTk

from core.logger import log
from core.monitors import list_monitors
from core.winapi import hwnd_of_toplevel, keep_on_top, make_lock_window
from core import actions as A

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp")

# 配色（锁屏是覆盖全屏的暗色界面，与系统主题无关）
C_ACCENT = "#38BDF8"
C_ACCENT_DARK = "#0EA5E9"
C_CARD = "#0F172A"
C_CARD_BORDER = "#243044"
C_TEXT = "#E5E7EB"
C_TEXT_DIM = "#94A3B8"
C_TEXT_FAINT = "#64748B"
C_DANGER = "#F87171"
C_WARN = "#FBBF24"
C_OK = "#34D399"
C_TRACK = "#1E293B"

CLOCK_FONT = ("Segoe UI Light", 76)
CLOCK_FONT_SMALL = ("Segoe UI Light", 48)
DATE_FONT = ("Segoe UI", 15)
TITLE_FONT = ("Segoe UI Semibold", 13)
BODY_FONT = ("Segoe UI", 12)
SMALL_FONT = ("Segoe UI", 10)
BTN_FONT = ("Segoe UI Semibold", 11)

WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def _mmss(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class LockScreen:
    """管理所有显示器的锁屏窗口。"""

    def __init__(self, master, cfg, try_unlock, on_user_activity=None):
        """
        master        tk.Tk 根窗口（应处于 withdrawn 状态）
        cfg           Config 实例
        try_unlock    callable(str) -> (ok: bool, message: str)
        """
        self.master = master
        self.cfg = cfg
        self.try_unlock = try_unlock
        self.on_user_activity = on_user_activity

        self.windows: list = []
        self._photos = []          # 持有 PhotoImage 引用，防止被 GC 后图片消失
        self._slideshow_index = 0
        self._slideshow_at = 0.0
        self._folder_files: list = []
        self._lock_started = time.time()
        self._min_lock_seconds = 0.0
        self._pending_action = None
        self._status_data = None
        self._after_ids = []

        self._primary_widgets = {}

        # 壁纸手动切换（仅本次锁屏会话生效，不写回配置）
        self._override_path = None
        self._all_images = []
        self._wall_index = 0
        self._entrance_done = True
        self._toast_aid = None

    # ==========================================================
    # 生命周期
    # ==========================================================
    def show(self, min_lock_minutes: float = 0) -> None:
        self._lock_started = time.time()
        self._min_lock_seconds = max(0.0, float(min_lock_minutes or 0)) * 60.0
        self._override_path = None
        self._slideshow_at = time.time()
        self._build_wall_list()
        self._build()
        self._schedule_ticks()
        self._focus_password()
        self._hint_wall()

    def lock_elapsed(self) -> float:
        return time.time() - self._lock_started

    def destroy(self) -> None:
        for aid in self._after_ids:
            try:
                self.master.after_cancel(aid)
            except Exception:
                pass
        self._after_ids.clear()
        for win in self.windows:
            try:
                win.destroy()
            except Exception:
                pass
        self.windows.clear()
        self._photos.clear()
        self._primary_widgets.clear()

    def _after(self, ms, fn):
        aid = self.master.after(ms, fn)
        self._after_ids.append(aid)
        return aid

    def _schedule_ticks(self) -> None:
        self._tick_clock()
        self._tick_topmost()
        self._tick_min_lock()
        self._tick_ken()

    # ==========================================================
    # 建窗口
    # ==========================================================
    def _build(self) -> None:
        monitors = list_monitors() or []
        log.log("lock.build", f"显示器数量={len(monitors)}")

        for idx, mon in enumerate(monitors):
            is_primary = idx == 0
            win = self._create_window(mon)
            if win is None:
                continue
            self._paint_background(win, mon)
            if is_primary:
                self._build_primary_content(win, mon)
            else:
                self._build_secondary_content(win, mon)

    def _create_window(self, mon):
        try:
            win = tk.Toplevel(self.master)
        except Exception as exc:
            log.log("lock.error", f"创建窗口失败: {exc!r}")
            return None

        win.overrideredirect(True)
        win.configure(bg="black", cursor="arrow")
        win.geometry(f"{mon['w']}x{mon['h']}+{mon['x']}+{mon['y']}")
        win.deiconify()
        win.update_idletasks()
        win.update()

        try:
            hwnd = hwnd_of_toplevel(win)
            make_lock_window(hwnd, mon["x"], mon["y"], mon["w"], mon["h"])
            win._ailock_hwnd = hwnd
        except Exception as exc:
            log.log("lock.error", f"设置置顶失败: {exc!r}")

        # 屏蔽窗口内的一切右键/系统菜单/关闭行为
        win.bind("<Button-3>", lambda e: "break")
        win.bind("<Escape>", lambda e: "break")
        win.protocol("WM_DELETE_WINDOW", lambda: None)

        self.windows.append(win)
        return win

    # ==========================================================
    # 背景
    # ==========================================================
    def _base_image(self):
        override = getattr(self, "_override_path", None)
        if override:
            return override
        mode = self.cfg.get("background.mode", "color")
        if mode == "image":
            p = self.cfg.get("background.path", "")
            return p if p and os.path.isfile(p) else None
        if mode == "folder":
            files = self._folder_images(self.cfg.get("background.path", ""))
            if not files:
                return None
            interval = max(5, int(self.cfg.get("background.slideshow_seconds", 30) or 30))
            if time.time() - self._slideshow_at > interval:
                self._slideshow_at = time.time()
                self._slideshow_index = (self._slideshow_index + 1) % len(files)
            return files[self._slideshow_index % len(files)]
        return None

    def _build_wall_list(self) -> None:
        """构建当前可手动切换的壁纸列表（仅本次锁屏会话生效）。"""
        self._wall_index = 0
        mode = self.cfg.get("background.mode", "color")
        if mode == "folder":
            self._all_images = self._folder_images(
                self.cfg.get("background.path", ""))
        elif mode == "image":
            p = self.cfg.get("background.path", "")
            if p and os.path.isfile(p):
                d = os.path.dirname(p)
                self._all_images = self._folder_images(d) if d else [p]
                if p in self._all_images:
                    self._wall_index = self._all_images.index(p)
        else:
            self._all_images = []
        if self._all_images:
            self._wall_index = max(
                0, min(self._wall_index, len(self._all_images) - 1))
        else:
            self._wall_index = 0

    @staticmethod
    def _folder_images(folder):
        if not folder or not os.path.isdir(folder):
            return []
        try:
            return sorted(
                str(p) for p in Path(folder).iterdir()
                if p.suffix.lower() in IMAGE_EXTS and p.is_file()
            )
        except Exception:
            return []

    def _compose(self, w: int, h: int) -> Image.Image:
        base_color = self.cfg.get("background.color", "#0B1220") or "#0B1220"
        src = self._base_image()
        img = None

        if src:
            try:
                img = Image.open(src).convert("RGB")
                img = self._fit(img, w, h, self.cfg.get("background.fit", "cover"),
                                base_color)
            except Exception as exc:
                log.log("lock.warn", f"载入背景图失败 {src}: {exc!r}")
                img = None

        if img is None:
            img = self._gradient(w, h, base_color)

        blur = int(self.cfg.get("background.blur", 0) or 0)
        if blur > 0:
            img = img.filter(ImageFilter.GaussianBlur(radius=blur))

        dim = float(self.cfg.get("background.dim", 0.45) or 0)
        if dim > 0:
            overlay = Image.new("RGBA", img.size, (0, 0, 0, int(255 * dim)))
            img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

        return img

    @staticmethod
    def _fit(img: Image.Image, w: int, h: int, mode: str, color: str):
        iw, ih = img.size
        if mode == "stretch":
            return img.resize((w, h), Image.Resampling.LANCZOS)
        if mode == "center":
            canvas = Image.new("RGB", (w, h), color)
            canvas.paste(img, ((w - iw) // 2, (h - ih) // 2))
            return canvas
        if mode == "tile":
            canvas = Image.new("RGB", (w, h), color)
            for y in range(0, h, max(1, ih)):
                for x in range(0, w, max(1, iw)):
                    canvas.paste(img, (x, y))
            return canvas
        if mode == "contain":
            scale = min(w / iw, h / ih)
            nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
            canvas = Image.new("RGB", (w, h), color)
            canvas.paste(img.resize((nw, nh), Image.Resampling.LANCZOS),
                         ((w - nw) // 2, (h - nh) // 2))
            return canvas
        # cover（默认）：铺满后裁掉溢出部分
        scale = max(w / iw, h / ih)
        nw, nh = max(1, int(iw * scale + 0.5)), max(1, int(ih * scale + 0.5))
        img = img.resize((nw, nh), Image.Resampling.LANCZOS)
        return img.crop(((nw - w) // 2, (nh - h) // 2,
                         (nw - w) // 2 + w, (nh - h) // 2 + h))

    @staticmethod
    def _gradient(w: int, h: int, color: str) -> Image.Image:
        """没有图片时画一张有质感的暗色渐变，避免纯色过于死板。"""
        try:
            base = tuple(int(color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        except Exception:
            base = (11, 18, 32)
        img = Image.new("RGB", (w, h))
        d = ImageDraw.Draw(img)
        for y in range(h):
            t = y / max(1, h - 1)
            d.line([(0, y), (w, y)], fill=tuple(
                max(0, min(255, int(c * (0.75 + 0.55 * t)))) for c in base))
        return img

    def _paint_background(self, win, mon) -> None:
        w, h = max(1, mon["w"]), max(1, mon["h"])
        # Ken Burns：仅当有真实背景图时做缓慢平移（纯色渐变不做）
        base = self._base_image()
        ken = bool(self.cfg.get("background.kenburns", True)) and base is not None
        if ken:
            tw, th = int(w * 1.08) + 1, int(h * 1.08) + 1
        else:
            tw, th = w, h
        img = self._compose(tw, th)
        photo = ImageTk.PhotoImage(img)

        canvas = getattr(win, "_ailock_bg", None)
        if canvas is None:
            # 背景在 _build 中先于内容创建，天然位于最底层；无需再 lower
            canvas = tk.Canvas(win, width=w, height=h,
                               highlightthickness=0, bd=0)
            canvas.place(x=0, y=0, width=w, height=h)
            item = canvas.create_image(0, 0, image=photo, anchor="nw")
            win._ailock_bg = canvas
            win._ailock_bg_item = item
        else:
            old = getattr(win, "_ailock_bg_item", None)
            if old is not None:
                try:
                    canvas.delete(old)
                except Exception:
                    pass
            item = canvas.create_image(0, 0, image=photo, anchor="nw")
            win._ailock_bg_item = item
        win._ailock_bg_photo = photo         # 持有引用，防止被 GC
        win._ailock_ken = (tw, th) if ken else None

    def _refresh_backgrounds(self) -> None:
        """轮播间隔到了就换一张。"""
        if self.cfg.get("background.mode") != "folder":
            return
        interval = max(5, int(self.cfg.get("background.slideshow_seconds", 30) or 30))
        if time.time() - self._slideshow_at <= interval:
            return
        for win in self.windows:
            try:
                mon = win._ailock_mon
            except AttributeError:
                continue
            self._paint_background(win, mon)

    # ==========================================================
    # 主屏内容
    # ==========================================================
    def _build_primary_content(self, win, mon):
        win._ailock_mon = mon
        w, h = mon["w"], mon["h"]

        # ---- 时钟 ----
        clock_top = max(40, int(h * 0.16))
        anim = self.cfg.get("ui.animations", True)
        enter = 28 if anim else 0
        clock = tk.Label(win, font=CLOCK_FONT, fg=C_TEXT, bg=C_CARD)
        clock.place(relx=0.5, y=clock_top - enter, anchor="n")
        date = tk.Label(win, font=DATE_FONT, fg=C_TEXT_DIM, bg=C_CARD)
        date.place(relx=0.5, y=clock_top - enter + 100, anchor="n")

        # 时钟标签直接贴在渐变背景上会显脏，给它一块与卡片同色的衬底
        pad = tk.Frame(win, bg=C_CARD)
        pad.place(relx=0.5, y=clock_top, anchor="n")
        pad.lower(clock)

        # ---- 卡片 ----（全程绝对坐标，入场动 y、摇晃动 x，互不冲突）
        outer = tk.Frame(win, bg=C_CARD_BORDER)
        outer.place(x=w // 2, y=int(h * 0.56) + (40 if anim else 0),
                    anchor="center")
        card = tk.Frame(outer, bg=C_CARD)
        card.pack(padx=1, pady=1)
        body = tk.Frame(card, bg=C_CARD)
        body.pack(padx=32, pady=24, fill="both")

        self._primary_widgets.update(clock=clock, date=date, card=card,
                                     body=body, outer=outer, win=win)

        # 标题行
        title_row = tk.Frame(body, bg=C_CARD)
        title_row.pack(fill="x")
        tk.Label(title_row, text="已锁定", font=TITLE_FONT, fg=C_TEXT,
                 bg=C_CARD).pack(side="left")
        self._primary_widgets["lock_timer"] = tk.Label(
            title_row, font=SMALL_FONT, fg=C_TEXT_FAINT, bg=C_CARD)
        self._primary_widgets["lock_timer"].pack(side="right")
        self._sep(body)

        # 任务状态面板
        self._build_status_panel(body)

        # 待执行动作横幅（默认隐藏）
        self._build_action_banner(body)

        # 口令输入区
        self._build_password_area(body)

        # 提示语
        msg = (self.cfg.get("message.text", "") or "").strip()
        if msg:
            tk.Label(win, text=msg, font=BODY_FONT, fg=C_TEXT_DIM, bg=C_CARD,
                     wraplength=min(900, w - 120), justify="center").place(
                relx=0.5, rely=0.93, anchor="center")

        # 壁纸切换：屏幕两侧箭头（仅有多张壁纸时显示）
        if len(self._all_images) > 1:
            for dxn, d in (("\u2039", -1), ("\u203a", 1)):
                a = tk.Label(win, text=dxn, font=("Segoe UI Light", 40),
                             fg="#64748B", bg=C_CARD, cursor="hand2",
                             padx=12, pady=6)
                a.place(relx=0.0 if d < 0 else 1.0, rely=0.5,
                        anchor="w" if d < 0 else "e", x=24 if d < 0 else -24)
                a.bind("<Button-1>",
                       lambda e, dd=d: self._switch_wall(win, mon, dd))

        # 壁纸切换提示气泡
        toast = tk.Label(win, text="", font=SMALL_FONT, fg="#E5E7EB",
                         bg="#0B1220", relief="flat", padx=14, pady=6)
        toast.place(relx=0.5, rely=0.84, anchor="center")
        self._primary_widgets["toast"] = toast

        # 键盘 ← / → 切换壁纸
        win.bind("<Left>", lambda e: self._switch_wall(win, mon, -1))
        win.bind("<Right>", lambda e: self._switch_wall(win, mon, 1))

        if anim:
            self._play_entrance(win, mon)

        win.bind("<Button-1>", lambda e: self._focus_password())
        win.bind("<Key>", lambda e: self._focus_password())

    def _sep(self, parent):
        tk.Frame(parent, bg=C_CARD_BORDER, height=1).pack(fill="x", pady=10)

    def _build_status_panel(self, body):
        w = {}
        holder = tk.Frame(body, bg=C_CARD)
        w["status_holder"] = holder

        head = tk.Frame(holder, bg=C_CARD)
        head.pack(fill="x")
        w["status_task"] = tk.Label(head, text="", font=("Segoe UI Semibold", 12),
                                    fg=C_TEXT, bg=C_CARD, anchor="w")
        w["status_task"].pack(side="left")
        w["status_badge"] = tk.Label(head, text="", font=SMALL_FONT,
                                     fg=C_TEXT, bg=C_CARD)
        w["status_badge"].pack(side="right")

        w["status_lines"] = tk.Label(holder, text="", font=BODY_FONT,
                                     fg=C_TEXT_DIM, bg=C_CARD,
                                     justify="left", anchor="w")
        w["status_lines"].pack(fill="x", pady=(4, 0))

        track = tk.Frame(holder, bg=C_TRACK, height=6)
        w["status_track"] = track
        fill = tk.Frame(track, bg=C_ACCENT)
        w["status_fill"] = fill
        w["status_pct"] = tk.Label(holder, text="", font=SMALL_FONT,
                                   fg=C_TEXT_FAINT, bg=C_CARD, anchor="w")

        self._primary_widgets.update(w)
        # 初始不显示，等第一次状态数据到达再 pack
        return holder

    def _build_action_banner(self, body):
        holder = tk.Frame(body, bg=C_WARN)
        inner = tk.Frame(holder, bg="#1C1508")
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        pad = tk.Frame(inner, bg="#1C1508")
        pad.pack(fill="x", padx=14, pady=10)

        title = tk.Label(pad, text="", font=("Segoe UI Semibold", 13),
                         fg=C_WARN, bg="#1C1508", anchor="w")
        title.pack(fill="x")
        count = tk.Label(pad, text="", font=("Segoe UI Light", 26),
                         fg=C_TEXT, bg="#1C1508", anchor="w")
        count.pack(fill="x")
        hint = tk.Label(pad, text="输入密码可取消并解锁", font=SMALL_FONT,
                        fg=C_TEXT_DIM, bg="#1C1508", anchor="w")
        hint.pack(fill="x")

        self._primary_widgets.update(
            action_holder=holder, action_title=title, action_count=count)

    def _build_password_area(self, body):
        w = {}
        holder = tk.Frame(body, bg=C_CARD)

        tk.Label(holder, text="请输入锁屏密码", font=SMALL_FONT,
                 fg=C_TEXT_FAINT, bg=C_CARD, anchor="w").pack(fill="x")

        row = tk.Frame(holder, bg=C_CARD)
        row.pack(fill="x", pady=(6, 0))

        entry = tk.Entry(
            row, font=("Segoe UI", 14), show="●",
            bg="#111C30", fg=C_TEXT, insertbackground=C_TEXT,
            relief="flat", bd=0, highlightthickness=1,
            highlightbackground=C_CARD_BORDER, highlightcolor=C_ACCENT,
            width=22,
        )
        entry.pack(side="left", ipady=8, padx=(0, 8))
        entry.bind("<Return>", lambda e: self._submit())
        entry.bind("<FocusOut>", lambda e: self._focus_password())

        btn = tk.Button(
            row, text="解锁", font=BTN_FONT, command=self._submit,
            bg=C_ACCENT, fg="#04263A", activebackground=C_ACCENT_DARK,
            activeforeground="#04263A", relief="flat", bd=0,
            padx=20, pady=8, cursor="hand2",
        )
        btn.pack(side="left")
        btn.bind("<Return>", lambda e: self._submit())

        msg = tk.Label(holder, text="", font=SMALL_FONT, fg=C_DANGER,
                       bg=C_CARD, anchor="w", wraplength=380, justify="left")
        msg.pack(fill="x", pady=(8, 0))

        holder.pack(fill="x", pady=(4, 0))
        w.update(pw_holder=holder, pw_entry=entry, pw_btn=btn, pw_msg=msg)
        self._primary_widgets.update(w)

    # ==========================================================
    # 副屏内容
    # ==========================================================
    def _build_secondary_content(self, win, mon):
        win._ailock_mon = mon
        w, h = mon["w"], mon["h"]
        top = max(40, int(h * 0.28))

        clock = tk.Label(win, font=CLOCK_FONT_SMALL, fg=C_TEXT, bg=C_CARD)
        clock.place(relx=0.5, y=top, anchor="n")
        date = tk.Label(win, font=DATE_FONT, fg=C_TEXT_DIM, bg=C_CARD)
        date.place(relx=0.5, y=top + 70, anchor="n")
        badge = tk.Label(win, text="已锁定", font=BODY_FONT, fg=C_TEXT_DIM,
                         bg=C_CARD)
        badge.place(relx=0.5, y=top + 110, anchor="n")

        msg = (self.cfg.get("message.text", "") or "").strip()
        if msg:
            tk.Label(win, text=msg, font=BODY_FONT, fg=C_TEXT_FAINT, bg=C_CARD,
                     wraplength=min(760, w - 120), justify="center").place(
                relx=0.5, y=top + 160, anchor="n")

        if not hasattr(self, "_secondary_clocks"):
            self._secondary_clocks = []
        self._secondary_clocks.append((clock, date))

    # ==========================================================
    # 周期任务
    # ==========================================================
    def _tick_clock(self):
        try:
            now = time.localtime()
            hhmm = time.strftime("%H:%M", now)
            date_text = (f"{now.tm_year}年{now.tm_mon}月{now.tm_mday}日  "
                         f"{WEEKDAYS[now.tm_wday]}")
            pw = self._primary_widgets
            if pw.get("clock"):
                pw["clock"].configure(text=hhmm)
                pw["date"].configure(text=date_text)
            for clock, date in getattr(self, "_secondary_clocks", []):
                clock.configure(text=hhmm)
                date.configure(text=date_text)
            self._refresh_backgrounds()
        except Exception:
            pass
        self._after(1000, self._tick_clock)

    def _tick_topmost(self):
        """周期性重申置顶，与其它抢 z-order 的窗口对抗。"""
        for win in self.windows:
            hwnd = getattr(win, "_ailock_hwnd", 0)
            if hwnd:
                try:
                    keep_on_top(hwnd)
                except Exception:
                    pass
        self._after(1000, self._tick_topmost)

    def _tick_min_lock(self):
        pw = self._primary_widgets
        timer = pw.get("lock_timer")
        if timer:
            elapsed = time.time() - self._lock_started
            if self._min_lock_seconds > 0:
                left = self._min_lock_seconds - elapsed
                if left > 0:
                    timer.configure(text=f"最早可在 {_mmss(left)} 后解锁")
                else:
                    timer.configure(text=f"已锁定 {_mmss(elapsed)}")
            else:
                timer.configure(text=f"已锁定 {_mmss(elapsed)}")
        self._after(500, self._tick_min_lock)

    def _tick_ken(self) -> None:
        """Ken Burns：每 120ms 仅平移画布坐标，不重渲染像素，CPU 几乎为零。"""
        if self.cfg.get("background.kenburns", True):
            for win in self.windows:
                ken = getattr(win, "_ailock_ken", None)
                if not ken:
                    continue
                canvas = getattr(win, "_ailock_bg", None)
                item = getattr(win, "_ailock_bg_item", None)
                mon = getattr(win, "_ailock_mon", None)
                if not (canvas and item and mon):
                    continue
                w, h = max(1, mon["w"]), max(1, mon["h"])
                t = time.time() - self._lock_started
                amp_x = 0.04 * w
                amp_y = 0.04 * h
                # 双周期正弦，避免明显重复
                ox = -amp_x + amp_x * math.sin(t * 2 * math.pi / 90.0)
                oy = -amp_y + amp_y * math.sin(t * 2 * math.pi / 130.0)
                canvas.coords(item, ox, oy)
        self._after(120, self._tick_ken)

    def _focus_password(self):
        entry = self._primary_widgets.get("pw_entry")
        if entry and entry.winfo_exists():
            try:
                entry.focus_force()
                entry.icursor("end")
            except Exception:
                pass
            return "break"
        return None

    # ==========================================================
    # 壁纸切换 + 提示气泡
    # ==========================================================
    def _switch_wall(self, win, mon, d: int) -> None:
        imgs = self._all_images
        if not imgs:
            self._show_toast(win, "当前为纯色背景，无可切换壁纸")
            return
        self._wall_index = (self._wall_index + d) % len(imgs)
        self._override_path = imgs[self._wall_index]
        self._slideshow_at = time.time()     # 手动切换后冻结自动轮播
        for w2 in self.windows:
            m2 = getattr(w2, "_ailock_mon", None)
            if m2:
                self._paint_background(w2, m2)
        name = os.path.splitext(os.path.basename(self._override_path))[0]
        self._show_toast(win, f"壁纸 {self._wall_index + 1} / {len(imgs)} · {name}")

    def _show_toast(self, win, text: str) -> None:
        toast = self._primary_widgets.get("toast")
        if not toast:
            return
        toast.configure(text=text)
        try:
            toast.lift()
        except Exception:
            pass
        aid = getattr(self, "_toast_aid", None)
        if aid:
            try:
                self.master.after_cancel(aid)
            except Exception:
                pass
        self._toast_aid = self._after(2200, lambda: toast.configure(text=""))

    def _hint_wall(self) -> None:
        if len(self._all_images) > 1:
            win = self._primary_widgets.get("win")
            if win:
                self._show_toast(win, "按 ← / → 或点击两侧箭头切换壁纸")

    # ==========================================================
    # 入场滑入 + 错误摇晃
    # ==========================================================
    def _play_entrance(self, win, mon) -> None:
        pw = self._primary_widgets
        clock = pw.get("clock")
        date = pw.get("date")
        outer = pw.get("outer")
        if not (clock and outer):
            return
        h = mon["h"]
        clock_top = max(40, int(h * 0.16))
        tgt_clock = clock_top
        tgt_outer = int(h * 0.56)
        n = 14
        dur = 340

        def step(i):
            p = i / n
            e = 1 - (1 - p) ** 3            # ease-out cubic
            cy = int((clock_top - 28) + 28 * e)
            clock.place_configure(y=cy)
            if date:
                date.place_configure(y=cy + 100)
            oy = int((tgt_outer + 40) - 40 * e)
            outer.place_configure(y=oy)
            if i < n:
                self._after(int(dur / n), lambda: step(i + 1))
            else:
                self._entrance_done = True

        self._entrance_done = False
        step(0)

    def _shake_card(self) -> None:
        pw = self._primary_widgets
        outer = pw.get("outer")
        win = pw.get("win")
        if not (outer and win):
            return
        if not self.cfg.get("ui.animations", True):
            return
        mon = getattr(win, "_ailock_mon", None)
        if not mon:
            return
        w, h = mon["w"], mon["h"]
        cx = w // 2
        amps = [12, -10, 7, -5, 3, -2, 0]

        def step(i):
            if i >= len(amps):
                outer.place_configure(x=cx)
                return
            outer.place_configure(x=cx + amps[i])
            self._after(40, lambda: step(i + 1))

        step(0)

    # ==========================================================
    # 状态更新（由主线程调用，线程安全）
    # ==========================================================
    def update_status(self, data: dict) -> None:
        self._status_data = data
        pw = self._primary_widgets
        holder = pw.get("status_holder")
        if holder is None:
            return

        if not self.cfg.get("status.enabled", True) or not data:
            holder.pack_forget()
            return

        state = data.get("state")
        lines = data.get("lines") or []
        task = (data.get("task") or "").strip()

        badge_map = {
            "waiting": ("等待授权", C_WARN),
            "running": ("运行中", C_ACCENT),
            "done": ("已完成", C_OK),
            "failed": ("失败", C_DANGER),
            "idle": ("空闲", C_TEXT_FAINT),
        }
        text, color = badge_map.get(state, ("", C_TEXT_FAINT))

        if not task and not lines and not text:
            holder.pack_forget()
            return

        holder.pack(fill="x")
        pw["status_task"].configure(text=task or "未命名任务")
        pw["status_badge"].configure(text=text, fg=color)

        pw["status_lines"].configure(text="\n".join(lines[:6]))
        if lines:
            pw["status_lines"].pack(fill="x", pady=(4, 0))
        else:
            pw["status_lines"].pack_forget()

        prog = data.get("progress")
        if prog is None:
            pw["status_track"].pack_forget()
            pw["status_pct"].pack_forget()
        else:
            pw["status_track"].pack(fill="x", pady=(8, 0))
            pw["status_fill"].place(relx=0, rely=0, relwidth=max(0.0, min(1.0, prog)),
                                    relheight=1.0)
            pw["status_pct"].pack(fill="x")
            pw["status_pct"].configure(text=f"{int(prog * 100)}%")

    # ==========================================================
    # 待执行动作倒计时
    # ==========================================================
    def show_pending_action(self, pending) -> None:
        self._pending_action = pending
        pw = self._primary_widgets
        holder = pw.get("action_holder")
        if holder is None:
            return
        if pending is None or pending.cancelled:
            holder.pack_forget()
            return
        holder.pack(fill="x", pady=(0, 10))
        pw["action_title"].configure(
            text=f"任务已完成 · 即将{A.human_action(pending.action)}")
        self._tick_pending_action()

    def _tick_pending_action(self) -> None:
        pa = self._pending_action
        pw = self._primary_widgets
        if pa is None or pa.cancelled:
            pw.get("action_holder").pack_forget()
            return
        left = pa.remaining()
        pw["action_count"].configure(text=_mmss(left))
        if left > 0:
            self._after(250, self._tick_pending_action)

    # ==========================================================
    # 解锁交互
    # ==========================================================
    def _submit(self) -> None:
        entry = self._primary_widgets.get("pw_entry")
        if entry is None or not entry.winfo_exists():
            return
        password = entry.get()
        if not password:
            return
        if self.on_user_activity:
            self.on_user_activity()

        ok, message = self.try_unlock(password)

        if ok:
            entry.delete(0, "end")
            return

        entry.delete(0, "end")
        msg = self._primary_widgets["pw_msg"]
        msg.configure(text=message or "密码错误")
        self._shake_card()
        self._after(4000, lambda: msg.configure(text=""))
        self._focus_password()

    def set_message(self, text: str, color: str = C_DANGER) -> None:
        lbl = self._primary_widgets.get("pw_msg")
        if lbl and lbl.winfo_exists():
            lbl.configure(text=text, fg=color)
