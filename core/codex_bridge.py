"""AiLock × Codex 联动桥接 —— 设置页按钮与 CLI 安装器共用的实现。

Codex（codex-cli 0.148+ / Codex 桌面版共用 ~/.codex/config.toml）的
notify 是根级单值：notify = ["命令", "参数"]，只能填一个，且本机已被
codex-computer-use.exe 占用。方案：

  1. 生成中转脚本 %APPDATA%/AiLock/hooks/ailock_codex_relay.py
     —— 收到 Codex 的 JSON 后先转发原命令，再写 AiLock 状态
  2. 把 config.toml 顶部的 notify 行改指向中转脚本（原命令存进
     sidecar codex_relay.json，卸载时原样还原）
  3. 只做「这一行」的文本手术，config.toml 其余内容（注释、缩进、
     表顺序）一律不动

所有写操作先备份；dry=True 时只生成报告不改任何文件。
"""

import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from core import zcode_bridge as _common
from core.zcode_bridge import BridgeError   # noqa: F401  (re-export)

MARKER = "ailock_codex_relay.py"
SIDECAR_NAME = "codex_relay.json"
SMOKE_TIMEOUT = 30

# notify = [ ... ] 单行数组（config.toml 顶部根级键，在第一个 [table] 之前）
_NOTIFY_RE = re.compile(r"^[ \t]*notify[ \t]*=[ \t]*(\[.*\])[ \t]*$",
                        re.MULTILINE)
# 提取数组里的字符串字面量（基础串 "..." 带转义 / 字面串 '...' 无转义）
_STR_RE = re.compile(r"""(['"])((?:\\.|(?!\1).)*)\1""", re.DOTALL)


# ---------------------------------------------------------------- 路径

def codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def hook_installed_path() -> Path:
    return _common.appdata_dir() / "hooks" / MARKER


def sidecar_installed_path() -> Path:
    return _common.appdata_dir() / "hooks" / SIDECAR_NAME


def hook_source_path() -> Path:
    exe_dir = Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", exe_dir))
        candidates = [
            base / "integrations" / "codex" / MARKER,
            exe_dir / "integrations" / "codex" / MARKER,
        ]
    else:
        candidates = [
            Path(__file__).resolve().parents[1] / "integrations" / "codex" / MARKER,
        ]
    for p in candidates:
        if p.exists():
            return p
    raise BridgeError("找不到中转脚本 %s（安装包可能不完整）" % MARKER)


# ---------------------------------------------------------------- TOML 手术

def _parse_notify_array(arr_text: str):
    """从 notify=[...] 的方括号内容里解析出字符串列表（TOML 转义已还原）。"""
    out = []
    for m in _STR_RE.finditer(arr_text):
        quote, body = m.group(1), m.group(2)
        if quote == '"':
            body = body.replace(r'\\"', '"').replace("\\\\", "\\")
        out.append(body)
    return out


def _toml_basic(s: str) -> str:
    """把路径编码为 TOML 基础字符串字面量（含首尾引号）。"""
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')


def _find_notify_span(text: str):
    """返回 (start, end, 数组文本)；没找到返回 (None, None, None)。"""
    m = _NOTIFY_RE.search(text)
    if not m:
        return None, None, None
    return m.start(1), m.end(1), m.group(1)


def _is_our_line(arr_text: str) -> bool:
    return MARKER in arr_text


# ---------------------------------------------------------------- 冒烟测试

def _smoke_test(relay_path: Path, python_exe: str) -> str:
    """临时 APPDATA 里模拟一轮 agent-turn-complete，验证能写出状态文件。"""
    with tempfile.TemporaryDirectory(prefix="ailock-cx-relay-test-") as td:
        env = dict(_common.os.environ)
        env["APPDATA"] = td
        env["AILOCK_DONE_DELAY"] = "0"
        # 预写 sidecar：original 置空 → 中转脚本跳过转发，只写状态
        hooks_dir = Path(td) / "AiLock" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / SIDECAR_NAME).write_text(
            json.dumps({"original": None, "smoke": True}, ensure_ascii=False),
            encoding="utf-8")
        payload = {
            "type": "agent-turn-complete",
            "turn-id": "smoke-1",
            "input-messages": ["冒烟测试：AiLock × Codex 联动"],
            "last-assistant-message": "联动冒烟完成",
        }
        r = subprocess.run(
            [python_exe, str(relay_path), json.dumps(payload, ensure_ascii=False)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=SMOKE_TIMEOUT, env=env,
            creationflags=_common._no_window())
        if r.returncode != 0:
            raise BridgeError(
                "中转脚本退出码 %d\nstderr: %s"
                % (r.returncode,
                   r.stderr.decode("utf-8", "replace").strip()[:400]))
        st = Path(td) / "AiLock" / "status.d" / "codex.json"
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
        "configured": False,     # config.toml 的 notify 已指向中转脚本
        "hook_file": False,      # 中转脚本已就位
        "sidecar": False,        # 原 notify 命令已备份（卸载还原的依据）
        "status_enabled": None,
        "found": codex_config_path().exists(),
        "python": None,
    }
    try:
        st["python"] = _common.find_python()
    except Exception:
        pass
    if st["found"]:
        try:
            text = codex_config_path().read_text(encoding="utf-8")
            _s, _e, arr = _find_notify_span(text)
            st["configured"] = bool(arr) and _is_our_line(arr)
        except Exception:
            pass
    st["hook_file"] = hook_installed_path().exists()
    st["sidecar"] = sidecar_installed_path().exists()
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
    cfg_path = codex_config_path()
    if not cfg_path.exists():
        raise BridgeError(
            "没找到 Codex 配置 %s\n请确认已安装 Codex 并至少启动过一次。"
            % cfg_path)
    python_exe = _common.find_python()
    relay_dst = hook_installed_path()
    text = cfg_path.read_text(encoding="utf-8")
    start, end, arr = _find_notify_span(text)
    report = []

    # 1. 复制中转脚本
    report.append("[1/4] 中转脚本 → %s" % relay_dst)
    if not dry:
        relay_dst.parent.mkdir(parents=True, exist_ok=True)
        _common.shutil.copy2(src, relay_dst)
    report.append("  已复制（解释器：%s）" % python_exe)

    # 2. 备份原 notify + 改写 config.toml
    report.append("[2/4] Codex 配置 %s" % cfg_path)
    if start is None:
        raise BridgeError(
            "config.toml 里没有根级 notify 行。\n"
            "请先在 config.toml 顶部（任何 [table] 之前）加一行：\n"
            '  notify = [ "你的命令" ]\n再重试安装。')
    original = _parse_notify_array(arr)
    if _is_our_line(arr):
        report.append("  notify 已指向 AiLock 中转脚本，跳过改写")
    else:
        if not dry:
            report.append("  已备份 → %s" % _common._backup(cfg_path).name)
        if not original:
            report.append("  ⚠ 原 notify 为空数组，无可转发命令")
        # 写 sidecar（记录原命令，卸载时还原用）
        if not dry:
            sidecar = sidecar_installed_path()
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            _common._write_json_atomic(sidecar, {
                "original": original,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "config": str(cfg_path),
            })
        new_arr = "[ %s, %s ]" % (_toml_basic(str(python_exe).replace("\\", "/")),
                                  _toml_basic(str(relay_dst).replace("\\", "/")))
        if not dry:
            new_text = text[:start] + new_arr + text[end:]
            tmp = cfg_path.with_suffix(".tmp")
            tmp.write_text(new_text, encoding="utf-8")
            _common.os.replace(tmp, cfg_path)
        report.append("  notify：原 %s 个命令 → 中转脚本（原命令已存 sidecar）"
                      % len(original))
        if original:
            report.append("    原命令：%s" % " ".join(original))

    # 3. 打开 AiLock 状态检测
    report.append("[3/4] AiLock 状态检测")
    report.extend("  " + line for line in
                  _common._enable_ailock_status(
                      _common.appdata_dir() / "config.json", dry))

    # 4. 冒烟测试
    report.append("[4/4] 冒烟测试（临时目录，不碰真实状态文件）")
    try:
        report.append("  通过：%s" % _smoke_test(relay_dst if not dry else src,
                                                 python_exe))
    except BridgeError as err:
        if not dry:
            report.append("  失败：%s" % err)
            raise BridgeError("\n".join(report)
                              + "\n\n冒烟测试失败，可用 uninstall.py 回退")
        report.append("  （dry-run 跳过冒烟）" if dry else "  失败：%s" % err)

    report.append("")
    report.append("后续两步（必须）：")
    report.append("  1. 重启 Codex（notify 配置在启动时加载）")
    report.append("  2. AiLock 正在运行就重启一次（不热加载配置）")
    return "\n".join(report)


def uninstall(dry: bool = False) -> str:
    """执行卸载：还原 notify 原命令、删除中转脚本与 sidecar。"""
    report = ["== AiLock × Codex 卸载 =="]
    cfg_path = codex_config_path()
    restored = False
    our_notify_present = False
    if cfg_path.exists():
        text = cfg_path.read_text(encoding="utf-8")
        start, end, arr = _find_notify_span(text)
        if start is not None and _is_our_line(arr):
            our_notify_present = True
            sidecar = read_sidecar()
            original = (sidecar or {}).get("original")
            if isinstance(original, list) and original:
                new_arr = "[%s]" % ", ".join(_toml_basic(s) for s in original)
            else:
                # sidecar 丢了：至少把 AiLock 摘掉，notify 置空数组
                new_arr = "[]"
                report.append("  ⚠ sidecar 丢失，无法还原原命令，notify 置空")
            if not dry:
                report.append("[1/3] 已备份 → %s" % _common._backup(cfg_path).name)
                new_text = text[:start] + new_arr + text[end:]
                tmp = cfg_path.with_suffix(".tmp")
                tmp.write_text(new_text, encoding="utf-8")
                _common.os.replace(tmp, cfg_path)
                restored = True
            report.append("[1/3] %snotify 已还原为原命令"
                          % ("将还原 " if dry else ""))
        else:
            report.append("[1/3] notify 未指向 AiLock，跳过")
    else:
        report.append("[1/3] 没找到 Codex 配置，跳过")

    relay_dst = hook_installed_path()
    if relay_dst.exists():
        if not dry:
            relay_dst.unlink()
        report.append("[2/3] %s%s%s"
                      % ("将删除 " if dry else "已删除 ", relay_dst,
                         "（dry-run）" if dry else ""))
    else:
        report.append("[2/3] 中转脚本不存在，跳过")

    sidecar_path = sidecar_installed_path()
    if sidecar_path.exists():
        if not dry:
            sidecar_path.unlink()
        report.append("[3/3] %s%s%s"
                      % ("将删除 " if dry else "已删除 ", sidecar_path,
                         "（dry-run）" if dry else ""))
    else:
        report.append("[3/3] sidecar 不存在，跳过")

    if not dry and restored:
        report.append("")
        report.append("卸载完成。重启 Codex 生效。")
    if not our_notify_present and not restored:
        report.append("")
        report.append("Codex 本来就没接 AiLock。")
    return "\n".join(report)


def read_sidecar():
    try:
        return json.loads(
            sidecar_installed_path().read_text(encoding="utf-8"))
    except Exception:
        return None
