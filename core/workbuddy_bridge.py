"""AiLock × WorkBuddy 联动桥接 —— 设置页按钮与 CLI 安装器共用的实现。

WorkBuddy 桌面版的 hooks 配置在 ~/.workbuddy/settings.json 的 hooks 字段，
格式与 Claude Code 同构（事件 → matcher 组 → hooks 数组）；Windows 下钩子
经 Git Bash 执行，命令必须是 bash 兼容写法（路径用引号包住即可）。

职责：
  - install()：复制钩子 → 备份并合并 settings.json（只追加自家事件，
    已有的 tokentracker / 其他 hooks 原样保留）→ 打开状态检测 → 冒烟测试
  - uninstall()：与 install 严格互逆，只摘自家条目
  - installed_state()：供界面显示当前接入状态

解释器查找、备份、原子写等通用逻辑直接复用 core.zcode_bridge。
"""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from core import zcode_bridge as _common
from core.zcode_bridge import BridgeError   # noqa: F401  (re-export)

MARKER = "ailock_workbuddy_hook.py"
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit",
               "PostToolUse", "PostToolUseFailure",
               "PermissionRequest", "Stop")
HOOK_TIMEOUT_S = 10
SMOKE_TIMEOUT = 30


# ---------------------------------------------------------------- 路径

def workbuddy_config_path() -> Path:
    return Path.home() / ".workbuddy" / "settings.json"


def hook_installed_path() -> Path:
    return _common.appdata_dir() / "hooks" / MARKER


def hook_source_path() -> Path:
    """钩子脚本源文件：冻结模式从 _MEIPASS 解包目录取，源码模式取仓库文件。"""
    exe_dir = Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", exe_dir))
        candidates = [
            base / "integrations" / "workbuddy" / MARKER,
            exe_dir / "integrations" / "workbuddy" / MARKER,
        ]
    else:
        candidates = [
            Path(__file__).resolve().parents[1] / "integrations" / "workbuddy" / MARKER,
        ]
    for p in candidates:
        if p.exists():
            return p
    raise BridgeError("找不到钩子脚本 %s（安装包可能不完整）" % MARKER)


def _bash_command(python_exe: str, hook_path: Path) -> str:
    """WorkBuddy 在 Windows 用 Git Bash 执行钩子命令，须 bash 兼容。

    路径一律转正斜杠 + 双引号包裹（Git Bash 与 Windows Python 都认）。
    """
    return '"{}" "{}"'.format(
        str(python_exe).replace("\\", "/"),
        str(hook_path).replace("\\", "/"))


# ---------------------------------------------------------------- 配置操作

def is_our_hook(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    return MARKER in str(entry.get("command") or "")


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
    """往 config 的 hooks 字段追加 AiLock 的事件。返回变更摘要行。"""
    hooks = config.setdefault("hooks", {})
    changes = []
    for ev in HOOK_EVENTS:
        lst = hooks.setdefault(ev, [])
        if not isinstance(lst, list):
            lst = hooks[ev] = []
        if event_has_our_hook(lst):
            changes.append("= %s：已存在，跳过" % ev)
            continue
        lst.append({
            "hooks": [{
                "type": "command",
                "command": _bash_command(python_exe, hook_path),
                "timeout": HOOK_TIMEOUT_S,
            }],
        })
        changes.append("+ %s：已注册" % ev)
    return changes


# ---------------------------------------------------------------- 冒烟测试

def _smoke_test(hook_path: Path, python_exe: str) -> str:
    """在临时 APPDATA 里模拟提问 + 授权 + Stop，验证能写出合法状态文件。"""
    with tempfile.TemporaryDirectory(prefix="ailock-wb-hook-test-") as td:
        env = dict(_common.os.environ)
        env["APPDATA"] = td
        env["AILOCK_DONE_DELAY"] = "0"
        fake_events = [
            {"hook_event_name": "UserPromptSubmit",
             "prompt": "冒烟测试：AiLock × WorkBuddy 联动"},
            {"hook_event_name": "PermissionRequest",
             "tool_name": "Bash", "tool_input": {"command": "echo smoke"}},
            {"hook_event_name": "Stop",
             "last_assistant_message": "联动冒烟完成",
             "stop_hook_active": False},
        ]
        for ev in fake_events:
            r = subprocess.run(
                [python_exe, str(hook_path)],
                input=json.dumps(ev).encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=SMOKE_TIMEOUT, env=env,
                creationflags=_common._no_window())
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
        "configured": False,
        "hook_file": False,
        "status_enabled": None,
        "found": workbuddy_config_path().exists(),
        "python": None,
    }
    try:
        st["python"] = _common.find_python()
    except Exception:
        pass
    if st["found"]:
        try:
            config = json.loads(
                workbuddy_config_path().read_text(encoding="utf-8"))
            hooks = config.get("hooks") or {}
            st["configured"] = any(
                event_has_our_hook(hooks.get(ev)) for ev in HOOK_EVENTS)
        except Exception:
            pass
    st["hook_file"] = hook_installed_path().exists()
    try:
        cfg_path = _common.appdata_dir() / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8")) \
            if cfg_path.exists() else {}
        st["status_enabled"] = bool((cfg.get("status") or {}).get("enabled"))
    except Exception:
        st["status_enabled"] = None
    return st


def install(dry: bool = False) -> str:
    """执行安装，返回人类可读报告；失败抛 BridgeError。"""
    src = hook_source_path()
    cfg_path = workbuddy_config_path()
    if not cfg_path.exists():
        raise BridgeError(
            "没找到 WorkBuddy 配置 %s\n请确认已安装 WorkBuddy 并至少启动过一次。"
            % cfg_path)
    python_exe = _common.find_python()
    hook_dst = hook_installed_path()
    report = []

    # 1. 复制钩子脚本
    report.append("[1/4] 钩子脚本 → %s" % hook_dst)
    if not dry:
        hook_dst.parent.mkdir(parents=True, exist_ok=True)
        _common.shutil.copy2(src, hook_dst)
    report.append("  已复制（解释器：%s）" % python_exe)

    # 2. 合并 WorkBuddy 配置
    report.append("[2/4] WorkBuddy 配置 %s" % cfg_path)
    config = json.loads(cfg_path.read_text(encoding="utf-8"))
    if not dry:
        report.append("  已备份 → %s" % _common._backup(cfg_path).name)
    report.extend("  " + line for line in
                  _merge_hooks(config, hook_dst, python_exe))
    if not dry:
        _common._write_json_atomic(cfg_path, config)
    report.append("  已写入（已有 hooks / 其他设置原样保留）")

    # 3. 打开 AiLock 状态检测
    report.append("[3/4] AiLock 状态检测")
    report.extend("  " + line for line in
                  _common._enable_ailock_status(
                      _common.appdata_dir() / "config.json", dry))

    # 4. 冒烟测试
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
    report.append("  1. WorkBuddy 新建一个会话（hook 在会话启动时加载）")
    report.append("  2. AiLock 正在运行就重启一次（不热加载配置）")
    return "\n".join(report)


def uninstall(dry: bool = False) -> str:
    """执行卸载，返回人类可读报告。"""
    report = ["== AiLock × WorkBuddy 卸载 =="]
    removed = 0
    cfg_path = workbuddy_config_path()
    if cfg_path.exists():
        config = json.loads(cfg_path.read_text(encoding="utf-8"))
        hooks = config.get("hooks") or {}
        for ev in list(hooks):
            groups = hooks[ev]
            if not isinstance(groups, list):
                continue
            kept = []
            for group in groups:
                entries = (group.get("hooks") or []) \
                    if isinstance(group, dict) else []
                if any(is_our_hook(h) for h in entries):
                    removed += 1
                else:
                    kept.append(group)
            if kept:
                hooks[ev] = kept
            else:
                del hooks[ev]           # 事件只剩自家 hook，整个删掉
        if removed and not dry:
            report.append("[1/2] 已备份 → %s" % _common._backup(cfg_path).name)
            _common._write_json_atomic(cfg_path, config)
        report.append("[1/2] %s%d 个 hook 事件%s"
                      % ("将摘除 " if dry else "已摘除 ", removed,
                         "（dry-run）" if dry else ""))
    else:
        report.append("[1/2] 没找到 WorkBuddy 配置，跳过")

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
        report.append("卸载完成。WorkBuddy 新会话生效。")
    return "\n".join(report)
