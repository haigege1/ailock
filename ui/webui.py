"""AiLock · Web UI 层（WebView2 / pywebview）

用 Web 技术重写界面，嵌入 WebView2 原生窗口，做到与设计稿 100% 还原；
后端（配置 / 密码 / 电源 / 看门狗 / 状态监控）完全复用，不经 tkinter 渲染。

线程模型：
  - 主线程：tkinter 隐藏窗口（仅作调度器，root.after / root.mainloop）
  - 后台线程：webview.start() 泵消息，承载所有 WebView2 窗口
  - JS→Python：pywebview.api（在 webview 线程执行）
  - Python→JS：window.evaluate_js（可从任意线程安全调用）
"""

import base64
import json
import os
import sys
import threading
import time
import heapq
import traceback
from pathlib import Path

import webview

from core.config import default_status_path
from core.logger import log


def _resource_root() -> Path:
    """定位 ui/web/ 资源根目录。

    - 源码运行：<repo>/lockscreen/ui/webui.py → <repo>/lockscreen/ui/web
    - PyInstaller 冻结：__file__ 在 sys._MEIPASS 下，路径同样有效
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "ui" / "web"
    return Path(__file__).resolve().parent / "web"


def build_html(view: str) -> str:
    """把 index.html + styles.css + app.js 拼成单文件 HTML 字符串。

    交给 pywebview 时必须用 ``html=`` 参数（底层走
    ``CoreWebView2.NavigateToString``），不要用 ``url=data:text/html,...``：
    中文经百分号编码后 data: URI 会超过 .NET ``System.Uri`` 的 65519 字符
    上限，`Uri(url)` 直接抛「URI 字符串太长」，导致该窗口（若为主窗口则是
    整个 webview）的 CoreWebView2 初始化失败，页面全白且
    ``window.pywebview`` 也不存在。详见 ``_to_data_url`` 的注释。

    为什么内联成单文件而不是 file://：
    1. PyInstaller 冻结后 webui.py 在 PYZ 归档，__file__ 不可靠
    2. WebView2 对 file:// 有沙箱限制（ERR_FILE_NOT_FOUND 即使路径正确也可能复现）
    3. CSS/JS 已内联，不依赖 base URL 加载子资源
    """
    root = _resource_root()
    html_path = root / "index.html"
    css_path = root / "styles.css"
    js_path = root / "app.js"
    if not html_path.exists():
        # 兜底：源码路径直读
        html_path = Path(__file__).resolve().parent / "web" / "index.html"
        css_path = Path(__file__).resolve().parent / "web" / "styles.css"
        js_path = Path(__file__).resolve().parent / "web" / "app.js"
    try:
        html = html_path.read_text(encoding="utf-8")
        css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
        js = js_path.read_text(encoding="utf-8") if js_path.exists() else ""
    except Exception as e:
        log("html read failed: %r" % e)
        raise
    # 内联 CSS（替换失败说明模板改过，退化为追加，保证样式不丢）
    css_tag = "<style>\n" + css + "\n</style>\n</head>"
    if '<link rel="stylesheet" href="styles.css" />' in html:
        html = html.replace('<link rel="stylesheet" href="styles.css" />',
                            "<style>\n" + css + "\n</style>", 1)
    elif css and "</head>" in html:
        html = html.replace("</head>", css_tag, 1)
    # 内联 JS
    if '<script src="app.js"></script>' in html:
        html = html.replace('<script src="app.js"></script>',
                            "<script>\n" + js + "\n</script>", 1)
    elif js and "</body>" in html:
        html = html.replace("</body>", "<script>\n" + js + "\n</script>\n</body>", 1)
    # 注入视图标志 + app 模式（隐藏预览条）
    marker = (
        "<script>"
        "window.__VIEW__=%s;window.__APP_MODE__=true;"
        "</script>"
    ) % json.dumps(view)
    html = html.replace("</head>", marker + "</head>", 1)
    return html


def _to_data_url(view: str) -> str:
    """历史实现：把整页内联后编码成 data:text/html URI。

    已废弃 —— 中文百分号编码后长度会远超 .NET System.Uri 上限（65519），
    `Uri(url)` 直接抛「URI 字符串太长」，导致该窗口（若为主窗口则整个
    webview）初始化失败、页面全白。现改用 html= 走 NavigateToString。
    保留此函数仅供 tools/diag_web_load.py 做回归对比（A 方案）。
    """
    html = build_html(view)
    from urllib.parse import quote
    encoded = quote(html, safe="")
    return "data:text/html;charset=utf-8," + encoded


HTML_DIR = _resource_root()
HTML_FILE = HTML_DIR / "index.html"


class AppRoot:
    """替代 tkinter.Tk 的轻量调度器（Web 模式下使用）。

    - 所有 after() 回调在线程安全的后台 worker 上执行；
      后端回调只接触文件与 webview（evaluate_js / destroy 均线程安全），
      因此不再依赖主线程的 Tcl 事件循环。
    - 主线程交给 webview.start() 阻塞，本调度器不阻塞主线程。
    """

    def __init__(self):
        self._timers = []
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._seq = 0
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    # tkinter 兼容的无操作方法
    def withdraw(self):
        pass

    def deiconify(self):
        pass

    def title(self, t=None):
        pass

    def iconbitmap(self, p=None):
        pass

    def quit(self):
        self._stop.set()

    def after(self, ms, cb):
        with self._cv:
            self._seq += 1
            heapq.heappush(self._timers,
                           (time.time() + (ms or 0) / 1000.0, self._seq, cb))
            self._cv.notify_all()
        return self._seq

    def _loop(self):
        while not self._stop.is_set():
            with self._cv:
                if not self._timers:
                    self._cv.wait(0.2)
                    continue
                due, seq, cb = self._timers[0]
                now = time.time()
                if due <= now:
                    heapq.heappop(self._timers)
                else:
                    self._cv.wait(min(0.2, due - now))
                    continue
            try:
                cb()
            except Exception:
                traceback.print_exc()

    def mainloop(self):
        # Web 模式下不调用；保留以兼容 tk 回退路径
        self._stop.wait()

    def destroy(self):
        self._stop.set()
        with self._cv:
            self._cv.notify_all()

# 壁纸转 dataURL 的 MIME
_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "bmp": "image/bmp", "gif": "image/gif", "webp": "image/webp",
}


def _img_dataurl(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower()
    mime = _MIME.get(ext, "image/png")
    with open(path, "rb") as f:
        b = f.read()
    return "data:%s;base64,%s" % (mime, base64.b64encode(b).decode("ascii"))


def _list_images(folder: str, limit: int = 24):
    out = []
    try:
        for name in sorted(os.listdir(folder)):
            if name.lower().rsplit(".", 1)[-1] in _MIME:
                out.append(os.path.join(folder, name))
                if len(out) >= limit:
                    break
    except Exception:
        pass
    return out


class WebUI:
    """单例引擎，管理 webview 生命周期与窗口。"""

    _instance = None

    def __init__(self):
        self.root = None
        self.cfg = None
        self.vault = None
        self.app = None
        self._started = False
        self._anchor = None
        self._ready = threading.Event()
        self._lock_windows = []
        self._lock_started = 0.0
        self._lock_min = 0.0
        self._settings_win = None
        self._anim = True
        self._wall_index = None  # 当前展示的壁纸索引（跨切换持久）
        self._api = None

    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ---------------- 生命周期 ----------------
    def attach(self, root, cfg, vault, app):
        self.root = root
        self.cfg = cfg
        self.vault = vault
        self.app = app
        self._anim = bool(cfg.get("ui.animations", True))
        # 桥接对象（JS → Python）。必须在创建窗口前就绪。
        self._api = AppApi(self)

    def _ensure_anchor(self):
        """保证 anchor 是 pywebview 的「主窗口」（windows[0]）。

        pywebview 的 start() 只阻塞式创建 windows[0]，其余窗口由 _create_children
        在 windows[0].events.shown 就绪后再建；若主窗口初始化失败（历史上就是
        data: URI 过长导致 Uri 构造抛异常），其余窗口全部不会创建，整个应用表现为
        白屏。因此 anchor 必须在任何业务窗口之前入列。
        """
        if self._anchor is not None:
            return
        try:
            self._anchor = webview.create_window(
                "AiLock-anchor",
                html="<body style='background:transparent'></body>",
                frameless=True, transparent=True, width=2, height=2,
            )
        except Exception as exc:
            log.log("webui.anchor", repr(exc))

    def start(self):
        """必须在主线程调用（pywebview 要求）。会阻塞直到所有窗口关闭。"""
        if self._started:
            return
        self._started = True
        self._ensure_anchor()
        try:
            webview.start(func=lambda: self._ready.set(), debug=False)
        except Exception as exc:
            log.log("webui.run", repr(exc))

    def stop(self):
        for w in list(self._lock_windows):
            self._safe_destroy(w)
        self._lock_windows = []
        self._safe_destroy(self._settings_win)
        self._settings_win = None
        self._safe_destroy(self._anchor)
        self._anchor = None

    def _safe_destroy(self, win):
        if not win:
            return
        try:
            win.destroy()
        except Exception:
            pass

    # ---------------- 锁屏 ----------------
    def create_lock(self, try_unlock, min_lock_minutes=0):
        self._ensure_anchor()   # anchor 必须先入列，见 _ensure_anchor 注释
        self._lock_started = time.time()
        self._wall_index = None  # 新会话从配置索引重新初始化
        self._lock_min = float(min_lock_minutes or 0) * 60.0
        # 走 html= （CoreWebView2.NavigateToString）。
        # 不能用 url=data:text/html,... ：中文百分号编码后会超过 .NET Uri 长度上限，
        # Uri 构造抛异常 → 窗口初始化失败 → 白屏。详见 _to_data_url 的注释。
        page = build_html("lock")
        # 每个显示器铺满一个全屏窗口；screen 必须是 webview.screens 的 Screen 对象
        # （整型索引会让 winforms 后端取 .x/.width 时 AttributeError，退化为单窗口）。
        # 在 start() 之前（如 selftest）gui 尚未就绪，webview.screens 可能取不到，
        # 此时退化为单窗口（铺主屏）。
        screens = []
        try:
            s = webview.screens
            if s:
                screens = list(s)
        except Exception:
            screens = []
        if not screens:
            screens = [None]
        wins = []
        for i, sc in enumerate(screens):
            try:
                w = webview.create_window(
                    "AiLock-lock-%d" % i, html=page, fullscreen=True,
                    frameless=True, on_top=True, screen=sc,
                    js_api=self._api,
                )
                wins.append(w)
            except Exception as exc:
                log.log("webui.lock", "monitor %d: %r" % (i, exc))
        if not wins:
            try:
                wins.append(webview.create_window(
                    "AiLock-lock", html=page, fullscreen=True, frameless=True, on_top=True,
                    js_api=self._api))
            except Exception as exc:
                log.log("webui.lock", repr(exc))
        self._lock_windows = wins
        return wins

    def destroy_lock(self):
        for w in list(self._lock_windows):
            self._safe_destroy(w)
        self._lock_windows = []

    def lock_elapsed(self):
        return max(0.0, time.time() - self._lock_started)

    def push_lock(self, js):
        for w in self._lock_windows:
            try:
                w.evaluate_js(js)
            except Exception:
                pass

    # ---------------- 设置 ----------------
    def create_settings(self, on_close):
        self._ensure_anchor()   # anchor 必须先入列，见 _ensure_anchor 注释
        page = build_html("settings")
        try:
            w = webview.create_window(
                "AiLock-settings", html=page, width=720, height=560,
                min_size=(640, 460), js_api=self._api,
            )
            if on_close:
                w.events.closed += lambda: on_close()
            self._settings_win = w
            return w
        except Exception as exc:
            log.log("webui.settings", repr(exc))
            return None

    def destroy_settings(self):
        self._safe_destroy(self._settings_win)
        self._settings_win = None

    # ---------------- 壁纸 ----------------
    def build_wallpapers(self):
        """返回 (list[{name,url}], index, ken)。"""
        mode = self.cfg.get("background.mode", "color")
        ken = bool(self.cfg.get("background.kenburns", True))
        index = 0
        if mode == "color":
            color = self.cfg.get("background.color", "#0B1220")
            return [{"name": "纯色", "url": "linear-gradient(%s,%s)" % (color, color)}], 0, False
        if mode == "image":
            path = self.cfg.get("background.path", "")
            folder = os.path.dirname(path) if path else ""
            files = _list_images(folder) if folder else []
            if path and os.path.isfile(path):
                if path not in files:
                    files = [path] + files
                files = files[:24]
                index = files.index(path) if path in files else 0
            items = [{"name": os.path.basename(f), "url": _img_dataurl(f)} for f in files]
            return items, index, ken
        if mode == "folder":
            folder = self.cfg.get("background.path", "")
            files = _list_images(folder) if folder else []
            items = [{"name": os.path.basename(f), "url": _img_dataurl(f)} for f in files]
            return items, 0, ken
        return [], 0, ken

    def switch_wall_index(self, direction):
        items, index, ken = self.build_wallpapers()
        if not items:
            return 0, items, ken
        if self._wall_index is None:
            self._wall_index = index
        if len(items) == 1:
            return 0, items, ken
        self._wall_index = (self._wall_index + int(direction or 1) + len(items)) % len(items)
        return self._wall_index, items, ken


# ============================================================
# 暴露给 JS 的桥接 API
# ============================================================
class AppApi:
    def __init__(self, engine: WebUI):
        self.e = engine

    # ---------- 配置 ----------
    def getConfig(self):
        try:
            return json.loads(json.dumps(self.e.cfg.data))
        except Exception:
            return {}

    def setConfig(self, key, value):
        try:
            self.e.cfg.set(key, value)
        except Exception as exc:
            log.log("webui.setConfig", "%s=%r %r" % (key, value, exc))
            return False
        # 联动后端
        app = self.e.app
        if app and key.startswith("status."):
            try:
                app._restart_status_monitor()
            except Exception:
                pass
        if key == "ui.animations":
            self.e._anim = bool(value)
            self.e.push_lock("window.setAnim(%s)" % ("true" if value else "false"))
        return True

    def setAnim(self, on):
        self.e._anim = bool(on)

    # ---------- 解锁 ----------
    def unlock(self, password):
        app = self.e.app
        if not app:
            return {"ok": False, "msg": "后端未就绪"}
        ok, msg = app.try_unlock(password or "")
        return {"ok": bool(ok), "msg": msg or ""}

    # ---------- 改密码 ----------
    def changePassword(self, old_pw, new_pw):
        vault = self.e.vault
        if vault.is_set:
            if not vault.verify(old_pw or ""):
                return {"ok": False, "msg": "旧密码不正确"}
        try:
            vault.set_password(new_pw or "")
        except Exception as exc:
            return {"ok": False, "msg": "保存失败：%s" % exc}
        return {"ok": True, "msg": "已保存"}

    # ---------- 壁纸 ----------
    def getWallpapers(self):
        items, index, ken = self.e.build_wallpapers()
        if self.e._wall_index is None:
            self.e._wall_index = index
        return {"list": items, "index": self.e._wall_index, "ken": ken}

    def switchWall(self, direction):
        index, items, ken = self.e.switch_wall_index(int(direction or 1))
        return {"index": index, "ken": ken, "name": items[index]["name"] if items else ""}

    # ---------- 杂项 ----------
    def isPasswordSet(self):
        return bool(self.e.vault and self.e.vault.is_set)

    def lockNow(self):
        app = self.e.app
        if app:
            try:
                app.root.after(0, app.lock)
            except Exception:
                pass
        return True

    def closeSettings(self):
        app = self.e.app
        if app:
            try:
                app.root.after(0, app._close_settings_for_web)
            except Exception:
                pass
        return True


# ============================================================
# 对接 App 的锁屏 / 设置对象（接口与旧 tkinter 版一致）
# ============================================================
class WebLock:
    """替代旧 LockScreen。"""

    def __init__(self, root, cfg, try_unlock):
        self.root = root
        self.cfg = cfg
        self.try_unlock = try_unlock
        self.engine = WebUI.get()
        self.windows = []
        self._ailock_hwnd = 0

    def show(self, min_lock_minutes=0):
        self.windows = self.engine.create_lock(self.try_unlock, min_lock_minutes)
        # 推送一次当前状态（若有）
        if self.engine.app and getattr(self.engine.app, "_last_status", None):
            self.update_status(self.engine.app._last_status)

    def update_status(self, data):
        if not data:
            return
        try:
            d = dict(data)
        except Exception:
            d = {}
        # 合并最短锁定倒计时
        if self.engine._lock_min > 0:
            left = self.engine._lock_min - self.engine.lock_elapsed()
            if left > 0:
                d["min_lock_left"] = left
        js = "window.updateStatus(%s)" % json.dumps(d, ensure_ascii=False)
        self.engine.push_lock(js)

    def show_pending_action(self, pa):
        if not pa:
            self.engine.push_lock("window.showPending(null)")
            return
        obj = {
            "reason": getattr(pa, "reason", ""),
            "remaining": int(getattr(pa, "remaining", lambda: 0)()),
        }
        self.engine.push_lock("window.showPending(%s)" % json.dumps(obj, ensure_ascii=False))

    def set_message(self, text, color=None):
        t = (text or "").replace("`", "")
        c = color or "rgba(255,255,255,.82)"
        js = "window.setMessage(%s,%s)" % (json.dumps(t, ensure_ascii=False), json.dumps(c, ensure_ascii=False))
        self.engine.push_lock(js)

    def destroy(self):
        self.engine.destroy_lock()
        self.windows = []

    def lock_elapsed(self):
        return self.engine.lock_elapsed()


class _WinProxy:
    """模拟 tkinter Toplevel 的最小接口，供 App 接线。"""

    def __init__(self, on_close):
        self._on_close = on_close
        self._closed = False

    def protocol(self, event, cb):
        # WebView2 窗口关闭由引擎的 events.closed 接管，这里仅保存
        self._on_close = cb

    def deiconify(self):
        pass

    def lift(self):
        pass

    def focus_force(self):
        pass

    def destroy(self):
        if self._closed:
            return
        self._closed = True
        if self._on_close:
            try:
                self._on_close()
            except Exception:
                pass


class WebSettings:
    """替代旧 SettingsPanel。"""

    def __init__(self, root, cfg, vault, on_saved=None, on_lock_now=None, on_close=None):
        self.root = root
        self.cfg = cfg
        self.vault = vault
        self.on_saved = on_saved
        self.on_lock_now = on_lock_now
        self.engine = WebUI.get()
        self._on_close = on_close or (lambda: self.engine.destroy_settings())
        self.win = _WinProxy(self._on_close)
        self.engine.create_settings(on_close=self._on_close)
