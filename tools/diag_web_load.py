"""诊断 WebView2 三种页面加载方式，定位锁屏/设置白屏根因。

用法（在 lockscreen/ 目录下）：
    ..\\.venv\\Scripts\\python.exe tools\\diag_web_load.py [settings|lock] [all|a|b|c]

对比三种方式：
    A. url=data:text/html,...        —— 旧实现（走 CoreWebView2.Source 顶层导航）
    B. html=<HTML 字符串>            —— 走 CoreWebView2.NavigateToString
    C. url=http://127.0.0.1:<port>/  —— 本地 HTTP server

每种方式创建一个 320x220 的小窗口，2.5 秒后 evaluate_js 读取 DOM 与
window.pywebview 桥接状态，打印对比表后自动关闭所有窗口并退出。

两条工程性约束（踩过的坑）：
  1. 第一个 create_window 出来的窗口是 pywebview 的「主窗口」，它一旦初始化
     失败（例如 A 方案的 data: URI 超过 .NET Uri 65519 上限），evaluate_js
     会永久阻塞。所以：先建一个 html= 的 anchor 窗口占位；A 方案永远放最后；
     每个窗口单独开线程探测；再加一个全局看门狗到点 os._exit()。
  2. pywebview 的 JS 桥接是异步的，evaluate_js 直接调 api 会拿到 Promise，
     必须先存进全局变量再回读。
"""

import functools
import http.server
import json
import os
import socketserver
import sys
import threading
import time
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webview

import ui.webui as W

WEB_DIR = W._resource_root()
DATA_URI_PREFIX = "data:text/html;charset=utf-8,"
# .NET Framework 的 System.Uri 最大长度为 65519 字符
DOTNET_URI_LIMIT = 65519
WATCHDOG_SECONDS = 20.0

# 读取 DOM 与桥接状态的探针脚本
JS_PROBE = r"""
(function () {
  function probe(id) {
    var e = document.getElementById(id);
    if (!e) return 'MISSING';
    var cs = getComputedStyle(e);
    return 'len=' + (e.innerHTML ? e.innerHTML.length : 0) +
           ',display=' + cs.display +
           ',visible=' + (e.offsetWidth > 0 && e.offsetHeight > 0);
  }
  var out = {
    readyState: document.readyState,
    title: document.title,
    url: String(location.href).slice(0, 60),
    bodyLen: document.body ? document.body.innerHTML.length : -1,
    hasPywebview: !!window.pywebview,
    apiKeys: (window.pywebview && window.pywebview.api)
        ? Object.keys(window.pywebview.api).length : -1,
    hasGetConfig: !!(window.pywebview && window.pywebview.api &&
                     window.pywebview.api.getConfig),
    view: (typeof window.__VIEW__ === 'undefined') ? null : window.__VIEW__,
    lock: probe('lock'),
    settings: probe('settings')
  };
  return JSON.stringify(out);
})()
"""

# 异步调用桥接，把结果存到全局变量后再回读
JS_CALL_API = (
    "window.pywebview.api.getConfig().then(function (v) {"
    "  window.__cfgProbe = JSON.stringify(v);"
    "}).catch(function (e) { window.__cfgProbe = 'ERROR:' + e; });"
)

FIELDS = ("readyState", "url", "title", "bodyLen", "view", "hasPywebview",
          "apiKeys", "hasGetConfig", "getConfig", "lock", "settings")


def build_data_url(view: str) -> str:
    """A 方案：data: URI（旧实现，仅用于回归对比）。"""
    return W._to_data_url(view)


def build_html(view: str) -> str:
    """B 方案：内联后的 HTML 字符串（NavigateToString）。"""
    return W.build_html(view)


def start_http_server() -> str:
    """C 方案：本地 HTTP server，返回 base URL（serve ui/web 目录）。"""
    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

    handler = functools.partial(QuietHandler, directory=str(WEB_DIR))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % httpd.server_address[1]


class DiagApi:
    """最小桥接对象，仅用于验证 JS→Python 通道。"""

    def getConfig(self):
        return {"diag": True, "view": "probe"}

    def ping(self, text):
        return "pong:" + str(text)


def emit(line: str = "") -> None:
    print(line, flush=True)


def probe_window(name: str, win, results: dict) -> None:
    """在独立线程里探测单个窗口，任何异常都记下来而不是抛出。"""
    data = {}
    try:
        raw = win.evaluate_js(JS_PROBE)
        data = json.loads(raw) if isinstance(raw, str) else {"raw": repr(raw)}
    except Exception as exc:  # noqa: BLE001 - 诊断脚本要看到全部异常
        data = {"probeError": repr(exc)}
    try:
        win.evaluate_js(JS_CALL_API)
        time.sleep(0.8)
        data["getConfig"] = win.evaluate_js("window.__cfgProbe || null")
    except Exception as exc:  # noqa: BLE001
        data.setdefault("getConfig", "ERR:%r" % exc)
    results[name] = data


def report(order, results: dict, view: str) -> None:
    emit()
    for name in order:
        d = results.get(name)
        emit("-" * 72)
        emit("[%s]" % name)
        if not d:
            emit("   <无结果：窗口未响应，evaluate_js 被阻塞>")
            continue
        for k in FIELDS:
            if k in d:
                emit("   %-13s %s" % (k, d[k]))
        if "probeError" in d:
            emit("   %-13s %s" % ("probeError", d["probeError"]))
        body_len = d.get("bodyLen", -1)
        rendered = (isinstance(body_len, int) and body_len > 1000
                    and bool(d.get("hasPywebview")))
        cur = d.get("view")
        view_ok = (cur == view) if cur is not None else True
        emit("   => 结论：%s" % ("内容已渲染 ✔" if (rendered and view_ok) else "空白 ✘"))
    emit("-" * 72)


def main() -> int:
    view = sys.argv[1] if len(sys.argv) > 1 else "settings"
    if view not in ("settings", "lock"):
        emit("用法: python tools/diag_web_load.py [settings|lock] [all|a|b|c]")
        return 2
    mode = (sys.argv[2] if len(sys.argv) > 2 else "all").lower()
    if mode not in ("all", "a", "b", "c"):
        emit("mode 只能是 all / a / b / c")
        return 2

    html = build_html(view)
    data_url = build_data_url(view)
    emit("=" * 72)
    emit("AiLock WebView2 加载方式诊断 · view=%s · mode=%s" % (view, mode))
    emit("资源目录：%s" % WEB_DIR)
    emit("HTML 长度：%d 字符" % len(html))
    emit("data: URI 长度：%d 字符（.NET Uri 上限 %d → %s）"
         % (len(data_url), DOTNET_URI_LIMIT,
            "超限" if len(data_url) > DOTNET_URI_LIMIT else "未超限"))
    emit("=" * 72)

    api = DiagApi()
    wins = []
    order = []
    results = {}

    # anchor 必须是第一个 create_window：pywebview 把首个窗口当主窗口，
    # 主窗口初始化失败会拖垮 webview.start() 里所有窗口。
    anchor = webview.create_window(
        "AiLock-diag-anchor",
        html="<body style='background:transparent'></body>",
        frameless=True, transparent=True, width=2, height=2)

    if mode in ("all", "b"):
        wins.append(("B-html", webview.create_window(
            "B-html", html=html, width=320, height=220, js_api=api)))
        order.append("B-html")
    if mode in ("all", "c"):
        base = start_http_server()
        wins.append(("C-http", webview.create_window(
            "C-http", url="%s/index.html?view=%s" % (base, view),
            width=320, height=220, js_api=api)))
        order.append("C-http")
    if mode in ("all", "a"):
        # A 放最后：它失败时不会挡住其它窗口的探测
        wins.append(("A-data-uri", webview.create_window(
            "A-data-uri", url=data_url, width=320, height=220, js_api=api)))
        order.append("A-data-uri")

    def runner():
        time.sleep(2.5)                      # 等 CoreWebView2 初始化 + 导航
        threads = []
        for name, win in wins:
            t = threading.Thread(target=probe_window, args=(name, win, results),
                                 daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=6)
        report(order, results, view)
        for _, win in wins:
            try:
                win.destroy()
            except Exception:
                pass
        try:
            anchor.destroy()
        except Exception:
            pass

    def watchdog():
        time.sleep(WATCHDOG_SECONDS)
        emit()
        emit("[watchdog] %.0f 秒超时，强制结束（部分窗口可能未响应）"
             % WATCHDOG_SECONDS)
        report(order, results, view)
        sys.stdout.flush()
        os._exit(0)

    threading.Thread(target=watchdog, daemon=True).start()
    webview.start(func=runner, debug=False)

    # 正常路径：start() 返回后补一份报告再硬退出（避免残留线程挂住终端）
    time.sleep(0.3)
    report(order, results, view)
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
