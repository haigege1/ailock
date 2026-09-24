"""离屏渲染真机会话卡：tools\\check_status_cards.py

拿会话状态文件离屏渲染锁屏右侧会话卡，输出「修复前 / 修复后」两张 PNG
方便肉眼对比。

  修复前：等价于旧逻辑（running 永不过期、Stop 卡住的不会被补成 done）
  修复后：settle() + is_expired()（当前代码路径）

两种数据来源：
  python tools\\check_status_cards.py          # 读真机 %APPDATA%\\AiLock\\status.d
  python tools\\check_status_cards.py --demo   # 用内置样例（复现 2026-09-24 那 4 张卡）

只读状态文件，不写、不锁屏、不弹窗（QT_QPA_PLATFORM=offscreen）。

用法:
    ..\\.venv\\Scripts\\python.exe tools\\check_status_cards.py [--demo]
"""
import glob
import json
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# offscreen 平台默认找不到系统字体，中文会渲染成方框；指个字体目录
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(
    os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from PySide6.QtGui import QFontDatabase, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core import statusdir as SD  # noqa: E402
from core.config import Config, default_status_path  # noqa: E402
from core.statusfile import StatusFile  # noqa: E402
from ui.qt import theme as T  # noqa: E402
from ui.qt.lock_window import LockWindow  # noqa: E402


def _ensure_cjk_font():
    """离屏环境里显式加载中文字体，否则整屏都是方框。"""
    for name in ("msyh.ttc", "msyhbd.ttc", "Deng.ttf"):
        p = os.path.join(os.environ["QT_QPA_FONTDIR"], name)
        if os.path.exists(p):
            QFontDatabase.addApplicationFont(p)
    T._cached_family = None      # 让 theme 按新注册的字体重算
    fam = T.base_family()
    print(f"字体族: {fam}")

OUT = os.path.join(HERE, "tools", "_verify_out")
W, H = 1600, 900

# 2026-09-24 用户截图里钉在屏幕上的 4 张卡（内容取自当时的会话文件）
DEMO_STUCK = [
    ("ec013656", "发送", [
        "你: 发送",
        "AI: ✅ 已成功提交到钉钉「日志」日报 | 项目 | 结果 | |---|---| | 状态 | success:…",
        "✓ 本轮结束，等待确认…"]),
    ("32b60de4", "WorkBuddy 任务", [
        "AI: 報告已生成：`outputs/报错分析报告-2026-09-23.md`（统计周期 9/22 10:00 ~ 9/23…",
        "✓ 本轮结束，等待确认…"]),
    ("574f3620", "你是一名「每日工作日报超时自动发送」兜底任务。每天 18:55 自动运行一次（…", [
        "你: 你是一名「每日工作日报超时自动发送」兜底任务。每天 18:55 自动运行一次（…",
        "AI: **兜底任务已执行：日报超时自动提交成功。** ## 判定过程 读取 `send_state.json` → `dat…"]),
    ("c8b10a63", "你是一名「每日工作日报助手」。每天 17:55 自动运行一次，负责起草当日 git 工作日报", [
        "你: 你是一名「每日工作日报助手」。每天 17:55 自动运行一次，负责起草当日 g…",
        "AI: 今日日报草稿已生成（**尚未提交**），DING 强提醒已推到手机（openDingId `6935FA13C5DC4…"]),
]


def read_sessions(base_dir):
    """读目录下所有会话文件（只读），额外的 status.json 也算一个会话。"""
    out = []
    for p in sorted(glob.glob(os.path.join(base_dir, "status.d", "*.json"))):
        data = StatusFile(p).read()
        if data:
            out.append(data)
    return out


def demo_sessions():
    """构造样例：4 个钉住的旧会话（几小时~几天没更新）+ 1 个正在跑的会话。"""
    base = tempfile.mkdtemp(prefix="ailock-cards-")
    os.makedirs(os.path.join(base, "status.d"), exist_ok=True)
    now = time.time()
    for i, (sid, task, lines) in enumerate(DEMO_STUCK):
        with open(os.path.join(base, "status.d", sid + ".json"), "w",
                  encoding="utf-8") as f:
            json.dump({"task": task, "state": "running", "lines": lines,
                       "progress": 0.6, "updated": now - (9000 + i * 7200),
                       "step": 8, "source": "workbuddy"}, f, ensure_ascii=False)
    with open(os.path.join(base, "status.d", "live.json"), "w",
              encoding="utf-8") as f:
        json.dump({"task": "会话卡修复验证", "state": "running",
                   "lines": ["你: 修复锁屏右侧一直不消失的会话卡",
                             "AI: 正在改 statusdir / 钩子 / UI …"],
                   "progress": 0.5, "updated": now, "step": 5,
                   "source": "workbuddy"}, f, ensure_ascii=False)
    return base, read_sessions(base)


def newest_wallpaper():
    base = os.path.join(os.path.dirname(default_status_path()), "wallpapers")
    files = [p for p in glob.glob(os.path.join(base, "*"))
             if p.lower().endswith((".jpg", ".jpeg", ".png"))]
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def render(sessions, path, label):
    cfg = Config()
    cfg.set("background.mode", "color", autosave=False)
    cfg.set("background.color", "#0B1220", autosave=False)
    cfg.set("background.kenburns", False, autosave=False)
    cfg.set("ui.animations", False, autosave=False)
    cfg.set("message.text", "AI大人正在工作，请勿关机~~~", autosave=False)

    app = QApplication.instance() or QApplication(sys.argv)
    win = LockWindow(QApplication.primaryScreen(), cfg, None)
    win.resize(W, H)
    win.show()
    app.processEvents()

    pm = newest_wallpaper()
    if pm:
        win.set_background(QPixmap(pm), ken=False)
    app.processEvents()

    win.apply_status(sessions)
    app.processEvents()

    cards = [c for c in win.stack._cards if not c.isHidden()]
    print(f"\n== {label} ==")
    print(f"   会话文件 {len(sessions)} 个 -> 显示卡片 {len(cards)} 张")
    for c in cards:
        print(f"     · {c._title_raw}  [{c.state.text()}]")
    if not os.path.isdir(OUT):
        os.makedirs(OUT, exist_ok=True)
    ok = win.grab().save(path)
    print(f"   截图 {'OK -> ' + path if ok else '保存失败'}")
    win.close()
    return len(cards)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    _ensure_cjk_font()
    demo = "--demo" in sys.argv
    if demo:
        _base, sessions = demo_sessions()
        print("数据来源: 内置样例（复现 2026-09-24 截图里那 4 张卡）")
    else:
        base = os.path.dirname(default_status_path())
        sessions = read_sessions(base)
        print(f"数据来源: 真机 {base}\\status.d")
    now = time.time()
    print(f"会话文件: {len(sessions)} 个")
    for s in sessions:
        ago = (now - float(s.get("updated") or 0)) / 60.0
        print("  - %-40s state=%-8s %7.0f 分钟前  turn_end_at=%s" % (
            str(s.get("task") or s.get("sid"))[:40], s.get("state"), ago,
            "有" if s.get("turn_end_at") else "无"))

    # --- 修复前：等价旧逻辑（running 永不撤卡、不补 done） ---
    old_run, old_done = SD.RUNNING_IDLE_TTL_SECONDS, SD.DONE_CONFIRM_SECONDS
    SD.RUNNING_IDLE_TTL_SECONDS = 10 ** 9
    SD.DONE_CONFIRM_SECONDS = 10 ** 9
    n_before = render(sessions, os.path.join(OUT, "status_cards_before.png"),
                      "修复前（旧逻辑）")
    SD.RUNNING_IDLE_TTL_SECONDS, SD.DONE_CONFIRM_SECONDS = old_run, old_done

    # --- 修复后：当前代码路径 ---
    n_after = render(sessions, os.path.join(OUT, "status_cards_after.png"),
                     "修复后（settle + is_expired）")

    print(f"\n结论：卡片 {n_before} 张 -> {n_after} 张")
    return 0


if __name__ == "__main__":
    sys.exit(main())
