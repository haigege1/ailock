"""AiLock × ZCode 联动桥接 —— 设置页按钮与 CLI 安装器共用的实现。

职责：
  - 定位钩子脚本（源码模式 / PyInstaller 冻结模式均可）
  - 找一个能跑钩子的系统 Python（冻结模式下 sys.executable 是 AiLock.exe，
    不是解释器；还须绕开 WindowsApps 的商店假 python.exe）
  - install()：复制钩子 → 备份并合并 ~/.zcode/cli/config.json（只追加自家
    事件，plugins / MCP / 其他 hooks 原样保留）→ 打开状态检测 → 冒烟测试
  - uninstall()：与 install 严格互逆，只摘自家条目
  - installed_state()：供界面显示当前接入状态

所有写操作先备份；dry=True 时只生成报告不改任何文件。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MARKER = "ailock_zcode_hook.py"       # 识别自家 hook 的标记
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit",
               "PostToolUse", "PostToolUseFailure",
               "PermissionRequest", "Stop")
HOOK_TIMEOUT_MS = 8000
SMOKE_TIMEOUT = 30          # 冒烟测试单个事件的最长秒数


class BridgeError(Exception):
    """给界面展示的友好错误。"""


# ---------------------------------------------------------------- 路径

def appdata_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "AiLock"


def hook_installed_path() -> Path:
    return appdata_dir() / "hooks" / MARKER


def zcode_config_path() -> Path:
    return Path.home() / ".zcode" / "cli" / "config.json"


def hook_source_path() -> Path:
    """钩子脚本源文件：冻结模式从 _MEIPASS 解包目录取，源码模式取仓库文件。"""
    exe_dir = Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", exe_dir))
        candidates = [
            base / "integrations" / "zcode" / MARKER,
            exe_dir / "integrations" / "zcode" / MARKER,   # 兜底：随 exe 分发
        ]
    else:
        candidates = [
            Path(__file__).resolve().parents[1] / "integrations" / "zcode" / MARKER,
        ]
    for p in candidates:
        if p.exists():
            return p
    raise BridgeError("找不到钩子脚本 %s（安装包可能不完整）" % MARKER)


# ---------------------------------------------------------------- 解释器

def _is_real_python(path: str) -> bool:
    """跑一次 --version 确认真的是解释器（排除 WindowsApps 商店占位符）。"""
    try:
        r = subprocess.run([path, "--version"], capture_output=True,
                           text=True, timeout=15, creationflags=_no_window())
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode == 0 and out.strip().startswith("Python 3")
    except Exception:
        return False


def _no_window() -> int:
    """Windows 下隐藏子进程控制台窗口。"""
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def find_python() -> str:
    """返回能执行钩子的 Python 解释器路径；找不到抛 BridgeError。

    源码模式优先找系统 Python（venv 解释器可能随目录消失，写进
    ZCode 配置的路径要经得起时间考验）；找不到再退回当前解释器。
    """
    if not getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve()
        in_venv = "venv" in base.parts or ".venv" in base.parts
        if not in_venv:
            return sys.executable
        # 当前是 venv：先找系统级 Python
        for name in ("python", "python3", "py"):
            p = shutil.which(name)
            if not p:
                continue
            pl = p.lower()
            if "windowsapps" in pl or ("venv" in Path(p).resolve().parts):
                continue
            if _is_real_python(p):
                return p
        return sys.executable       # 实在找不到，退回 venv（有总比没有强）
    for name in ("python", "python3", "py"):
        p = shutil.which(name)
        if not p:
            continue
        if "windowsapps" in p.lower():
            continue                     # 微软商店占位符，不是真解释器
        if _is_real_python(p):
            return p
    raise BridgeError(
        "没找到可用的 Python 3（7 以上版本）。\n"
        "钩子脚本需要一个系统 Python 来执行，请安装 Python 后重试，\n"
        "或改用命令行：python integrations/zcode/install.py")


# ---------------------------------------------------------------- 配置操作

def is_our_hook(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    args = " ".join(str(a) for a in entry.get("args") or [])
    cmd = str(entry.get("command") or "")
    return MARKER in args or MARKER in cmd


def event_has_our_hook(event_cfg) -> bool:
    if not isinstance(event_cfg, list):
        return False
    for group in event_cfg:
        hooks = group.get("hooks") or [] if isinstance(group, dict) else []
        for h in hooks:
            if is_our_hook(h):
                return True
    return False


def _merge_hooks(config: dict, hook_path: Path, python_exe: str) -> list:
    """往 config 追加 AiLock 的 hook 事件。返回变更摘要行。"""
    hooks = config.setdefault("hooks", {})
    events = hooks.setdefault("events", {})
    changes = []
    for ev in HOOK_EVENTS:
        lst = events.setdefault(ev, [])
        if not isinstance(lst, list):
            lst = events[ev] = []
        if event_has_our_hook(lst):
            changes.append("= %s：已存在，跳过" % ev)
            continue
        lst.append({
            "hooks": [{
                "type": "process",
                "command": python_exe,
                "args": [str(hook_path)],
                "enabled": True,
                "timeoutMs": HOOK_TIMEOUT_MS,
            }],
        })
        changes.append("+ %s：已注册" % ev)
    if not hooks.get("enabled"):
        hooks["enabled"] = True
        changes.append("+ hooks.enabled：false → true")
    return changes


def _enable_ailock_status(cfg_path: Path, dry: bool) -> list:
    """打开 AiLock 状态检测开关（只动 status.enabled）。"""
    changes = []
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8")) \
            if cfg_path.exists() else {}
    except Exception:
        cfg = {}
    status = cfg.setdefault("status", {})
    if status.get("enabled"):
        changes.append("= AiLock 状态检测本来就是开的，不动")
        return changes
    status["enabled"] = True
    changes.append("+ AiLock status.enabled：false → true")
    if not dry:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, cfg_path)
    return changes


def _write_json_atomic(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def _backup(path: Path) -> Path:
    backup = path.with_name("%s.bak-%s" % (path.name,
                                           time.strftime("%Y%m%d-%H%M%S")))
    shutil.copy2(path, backup)
    return backup


# ---------------------------------------------------------------- 冒烟测试

def _smoke_test(hook_path: Path, python_exe: str) -> str:
    """在临时 APPDATA 里模拟提问 + Stop，验证钩子能写出合法状态文件。"""
    with tempfile.TemporaryDirectory(prefix="ailock-hook-test-") as td:
        env = dict(os.environ)
        env["APPDATA"] = td
        env["AILOCK_DONE_DELAY"] = "0"     # Stop 立即写 done，测试快
        fake_events = [
            {"hook_event_name": "UserPromptSubmit",
             "prompt": "冒烟测试：AiLock × ZCode 联动"},
            {"hook_event_name": "PermissionRequest",
             "tool_name": "Bash", "tool_input": {"command": "echo smoke"}},
            {"hook_event_name": "Stop", "stop_hook_active": False},
        ]
        for ev in fake_events:
            r = subprocess.run(
                [python_exe, str(hook_path)],
                input=json.dumps(ev).encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=SMOKE_TIMEOUT, env=env, creationflags=_no_window())
            if r.returncode != 0:
                raise BridgeError(
                    "钩子退出码 %d\nstderr: %s"
                    % (r.returncode,
                       r.stderr.decode("utf-8", "replace").strip()[:400]))
        st = Path(td) / "AiLock" / "status.json"
        if not st.exists():
            raise BridgeError("状态文件没写出来")
        data = json.loads(st.read_text(encoding="utf-8"))
        if data.get("state") != "done" or "冒烟测试" not in str(data.get("task")):
            raise BridgeError("状态内容异常：%s" % data)
        return "state=%s, task=%s" % (data.get("state"), data.get("task"))


# ---------------------------------------------------------------- 对外 API

def installed_state() -> dict:
    """当前接入状态，供界面展示。不抛异常。"""
    st = {
        "configured": False,     # Zcode 配置里有自家 hook
        "hook_file": False,      # 钩子脚本已在 AiLock 目录就位
        "status_enabled": None,  # AiLock 状态检测开关
        "zcode_found": zcode_config_path().exists(),
        "python": None,
    }
    try:
        st["python"] = find_python()
    except Exception:
        pass
    if st["zcode_found"]:
        try:
            config = json.loads(
                zcode_config_path().read_text(encoding="utf-8"))
            events = (config.get("hooks") or {}).get("events") or {}
            st["configured"] = any(
                event_has_our_hook(events.get(ev)) for ev in HOOK_EVENTS)
        except Exception:
            pass
    st["hook_file"] = hook_installed_path().exists()
    try:
        cfg_path = appdata_dir() / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8")) \
            if cfg_path.exists() else {}
        st["status_enabled"] = bool(
            (cfg.get("status") or {}).get("enabled"))
    except Exception:
        st["status_enabled"] = None
    return st


def install(dry: bool = False) -> str:
    """执行安装，返回人类可读报告；失败抛 BridgeError。"""
    src = hook_source_path()
    zcfg_path = zcode_config_path()
    if not zcfg_path.exists():
        raise BridgeError(
            "没找到 ZCode 配置 %s\n请确认已安装 ZCode 并至少启动过一次。"
            % zcfg_path)
    python_exe = find_python()
    hook_dst = hook_installed_path()
    report = []

    # 1. 复制钩子脚本
    report.append("[1/4] 钩子脚本 → %s" % hook_dst)
    if not dry:
        hook_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, hook_dst)
    report.append("  已复制（解释器：%s）" % python_exe)

    # 2. 合并 ZCode 配置
    report.append("[2/4] ZCode 配置 %s" % zcfg_path)
    config = json.loads(zcfg_path.read_text(encoding="utf-8"))
    if not dry:
        report.append("  已备份 → %s" % _backup(zcfg_path).name)
    report.extend("  " + line for line in
                  _merge_hooks(config, hook_dst, python_exe))
    if not dry:
        _write_json_atomic(zcfg_path, config)
    report.append("  已写入（plugins / MCP / 其他 hooks 原样保留）")

    # 3. 打开 AiLock 状态检测
    report.append("[3/4] AiLock 状态检测")
    report.extend("  " + line for line in
                  _enable_ailock_status(appdata_dir() / "config.json", dry))

    # 4. 冒烟测试（dry-run 还没复制，直接测源脚本）
    report.append("[4/4] 冒烟测试（临时目录，不碰真实状态文件）")
    try:
        report.append("  通过：%s" % _smoke_test(hook_dst if not dry else src,
                                                 python_exe))
    except BridgeError as err:
        if not dry:
            report.append("  失败：%s" % err)
            raise BridgeError("\n".join(report)
                              + "\n\n冒烟测试失败，可用 uninstall.py 回退")
        report.append("  （dry-run 跳过冒烟）" if dry else "  失败：%s" % err)

    report.append("")
    report.append("后续两步（必须）：")
    report.append("  1. ZCode 新建一个会话（hook 在会话启动时加载）")
    if getattr(sys, "frozen", False):
        report.append("  2. AiLock 重启一次（不热加载配置）")
    else:
        report.append("  2. AiLock 正在运行就重启一次（不热加载配置）")
    return "\n".join(report)


def uninstall(dry: bool = False) -> str:
    """执行卸载，返回人类可读报告。"""
    report = ["== AiLock × ZCode 卸载 =="]
    removed = 0
    zcfg_path = zcode_config_path()
    if zcfg_path.exists():
        config = json.loads(zcfg_path.read_text(encoding="utf-8"))
        events = (config.get("hooks") or {}).get("events") or {}
        for ev in list(events):
            groups = events[ev]
            if not isinstance(groups, list):
                continue
            kept = []
            for group in groups:
                hooks = (group.get("hooks") or []) \
                    if isinstance(group, dict) else []
                if any(is_our_hook(h) for h in hooks):
                    removed += 1
                else:
                    kept.append(group)
            if kept:
                events[ev] = kept
            else:
                del events[ev]           # 事件只剩自家 hook，整个删掉
        if removed and not dry:
            report.append("[1/2] 已备份 → %s"
                          % _backup(zcfg_path).name)
            _write_json_atomic(zcfg_path, config)
        report.append("[1/2] %s%d 个 hook 事件%s"
                      % ("将摘除 " if dry else "已摘除 ", removed,
                         "（dry-run）" if dry else ""))
    else:
        report.append("[1/2] 没找到 ZCode 配置，跳过")

    hook_dst = hook_installed_path()
    if hook_dst.exists():
        if not dry:
            hook_dst.unlink()
        report.append("[2/2] %s%s%s"
                      % ("将删除 " if dry else "已删除 ", hook_dst,
                         "（dry-run）" if dry else ""))
    else:
        report.append("[2/2] 钩子脚本不存在，跳过")

    if removed == 0 and not hook_dst.exists():
        report.append("本来就没装。")
    elif not dry:
        report.append("")
        report.append("卸载完成。ZCode 新会话生效（旧会话的 hook 快照仍在）。")
    return "\n".join(report)
