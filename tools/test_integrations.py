#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AiLock × AI 工具联动（ZCode / WorkBuddy / Codex）桥接自动化测试。

不碰真实配置：把 USERPROFILE / APPDATA 指到临时目录，伪造三家的
配置文件，然后对每个 bridge 走完整流程：

  1. dry-run          —— 断言不写任何文件
  2. install          —— 断言钩子就位、自家条目写入、**原有条目原样保留**、
                         冒烟测试（真实子进程跑钩子）通过
  3. installed_state  —— 断言 configured=True
  4. uninstall        —— 断言自家条目摘干净、Codex notify 还原为原命令、
                         其余内容不受影响
  5. 再卸载一次       —— 幂等，不报错

用法：
  python tools/test_integrations.py            # 跑全部
  python tools/test_integrations.py workbuddy  # 只跑指定 bridge
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import zcode_bridge, workbuddy_bridge, codex_bridge   # noqa: E402

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok  %s" % msg)
    else:
        FAIL += 1
        print("  FAIL %s" % msg)


# ---------------------------------------------------------------- 造配置

ZCODE_CFG = {
    "plugins": {"foo": {"enabled": True}},
    "mcp": {"server": {"cmd": "bar"}},
    "hooks": {"enabled": False,
              "events": {"Stop": [{"hooks": [{"type": "process",
                                              "command": "other.exe"}]}]}},
}

WORKBUDDY_CFG = {
    "sandbox": {"mode": "default"},
    "hooks": {"SessionEnd": [{"hooks": [
        {"type": "command",
         "command": "/usr/bin/env node \"C:\\\\x\\\\notify.cjs\""}]}]},
}

CODEX_TOML = (
    'model = "gpt-5.6-terra"\n'
    'disable_response_storage = true\n'
    '\n'
    'notify = [ "C:\\\\Tools\\\\codex-computer-use.exe", "turn-ended" ]\n'
    '\n'
    '[model_providers.custom]\n'
    'name = "custom"\n'
    '\n'
    '[projects.\'c:\\\\work\']\n'
    'trust_level = "trusted"\n'
)

CODEX_ORIGINAL = ["C:\\Tools\\codex-computer-use.exe", "turn-ended"]


def make_fake_home(home: Path):
    (home / ".zcode" / "cli").mkdir(parents=True, exist_ok=True)
    (home / ".zcode" / "cli" / "config.json").write_text(
        json.dumps(ZCODE_CFG, ensure_ascii=False, indent=2), encoding="utf-8")
    (home / ".workbuddy").mkdir(parents=True, exist_ok=True)
    (home / ".workbuddy" / "settings.json").write_text(
        json.dumps(WORKBUDDY_CFG, ensure_ascii=False, indent=2),
        encoding="utf-8")
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    (home / ".codex" / "config.toml").write_text(CODEX_TOML, encoding="utf-8")


# ---------------------------------------------------------------- 断言工具

def zcode_events(config):
    return (config.get("hooks") or {}).get("events") or {}


def wb_hooks(config):
    return config.get("hooks") or {}


def codex_notify(text):
    m = codex_bridge._NOTIFY_RE.search(text)
    return None if m is None else codex_bridge._parse_notify_array(m.group(1))


# ---------------------------------------------------------------- 各 bridge

def test_one(name, bridge):
    print("== %s ==" % name)
    home = Path(tempfile.mkdtemp(prefix="ailock-itg-"))
    old_up = os.environ.get("USERPROFILE")
    old_app = os.environ.get("APPDATA")
    os.environ["USERPROFILE"] = str(home)
    os.environ["APPDATA"] = str(home / "AppData" / "Roaming")
    try:
        make_fake_home(home)
        appdata = home / "AppData" / "Roaming"
        hooks_dir = appdata / "AiLock" / "hooks"

        # --- dry-run：不写任何文件 ---
        rep = bridge.install(dry=True)
        check("冒烟" in rep, "%s: dry-run 出报告" % name)
        check(not hooks_dir.exists(), "%s: dry-run 未写钩子文件" % name)

        # --- install ---
        rep = bridge.install(dry=False)
        check(bridge.hook_installed_path().exists(),
              "%s: 钩子脚本已复制" % name)
        check("通过" in rep, "%s: 冒烟测试通过（真实子进程）" % name)

        # 原有配置保留 + 自家条目写入
        if name == "zcode":
            cfg = json.loads((home / ".zcode/cli/config.json")
                             .read_text(encoding="utf-8"))
            check(cfg.get("plugins", {}).get("foo", {}).get("enabled") is True,
                  "zcode: plugins 原样保留")
            check(cfg.get("mcp", {}).get("server", {}).get("cmd") == "bar",
                  "zcode: mcp 原样保留")
            evs = zcode_events(cfg)
            others = [g for g in evs.get("Stop", [])
                      if "other.exe" in json.dumps(g)]
            check(len(others) == 1, "zcode: 已有 Stop hook 原样保留")
            check(all(evs.get(e) for e in zcode_bridge.HOOK_EVENTS),
                  "zcode: 全部事件已注册")
        elif name == "workbuddy":
            cfg = json.loads((home / ".workbuddy/settings.json")
                             .read_text(encoding="utf-8"))
            check(cfg.get("sandbox", {}).get("mode") == "default",
                  "workbuddy: 其他设置原样保留")
            hk = wb_hooks(cfg)
            se = hk.get("SessionEnd", [])
            check(len(se) == 1 and "notify.cjs" in json.dumps(se),
                  "workbuddy: 已有 SessionEnd hook 原样保留")
            check(all(hk.get(e) for e in workbuddy_bridge.HOOK_EVENTS),
                  "workbuddy: 全部事件已注册")
            cmd = hk["Stop"][0]["hooks"][0]["command"]
            check("\\" not in cmd and cmd.startswith('"'),
                  "workbuddy: 命令为 bash 兼容格式（正斜杠+引号）")
        else:
            text = (home / ".codex/config.toml").read_text(encoding="utf-8")
            check(codex_bridge.MARKER in text, "codex: notify 指向中转脚本")
            check('name = "custom"' in text and "turn-ended" not in text,
                  "codex: TOML 其余内容未动、原命令已从 notify 摘除")
            sidecar = json.loads((hooks_dir / codex_bridge.SIDECAR_NAME)
                                 .read_text(encoding="utf-8"))
            check(sidecar.get("original") == CODEX_ORIGINAL,
                  "codex: 原 notify 命令已存 sidecar")
            # TOML 仍可被标准库解析（结构没破坏）
            import tomllib
            data = tomllib.loads(text)
            check(isinstance(data.get("notify"), list)
                  and any(codex_bridge.MARKER in str(x) for x in data["notify"]),
                  "codex: 改写后 TOML 合法且 notify 是数组")

        st = bridge.installed_state()
        check(st["configured"] and st["hook_file"],
              "%s: installed_state=已接入" % name)
        check(st["status_enabled"] is True, "%s: AiLock 状态检测已打开" % name)

        # --- install 幂等（重复安装不炸、不重复）---
        rep2 = bridge.install(dry=False)
        check("跳过" in rep2 or "已存在" in rep2 or "已指向" in rep2,
              "%s: 重复安装幂等" % name)
        if name == "workbuddy":
            cfg = json.loads((home / ".workbuddy/settings.json")
                             .read_text(encoding="utf-8"))
            check(len(wb_hooks(cfg)["Stop"]) == 1, "workbuddy: 不重复注册")

        # --- uninstall ---
        rep3 = bridge.uninstall(dry=False)
        if name == "zcode":
            cfg = json.loads((home / ".zcode/cli/config.json")
                             .read_text(encoding="utf-8"))
            evs = zcode_events(cfg)
            ours = [e for e, lst in evs.items()
                    if zcode_bridge.event_has_our_hook(lst)]
            check(not ours, "zcode: 自家事件已摘除")
            check(cfg.get("plugins", {}).get("foo", {}).get("enabled") is True,
                  "zcode: 卸载后 plugins 仍在")
            check("other.exe" in json.dumps(evs.get("Stop", [])),
                  "zcode: 卸载后原有 Stop hook 仍在")
        elif name == "workbuddy":
            cfg = json.loads((home / ".workbuddy/settings.json")
                             .read_text(encoding="utf-8"))
            hk = wb_hooks(cfg)
            ours = [e for e, lst in hk.items()
                    if workbuddy_bridge.event_has_our_hook(lst)]
            check(not ours, "workbuddy: 自家事件已摘除")
            check("notify.cjs" in json.dumps(hk.get("SessionEnd", [])),
                  "workbuddy: 卸载后 SessionEnd tokentracker 仍在")
        else:
            text = (home / ".codex/config.toml").read_text(encoding="utf-8")
            check(codex_bridge.MARKER not in text,
                  "codex: notify 不再指向中转脚本")
            got = codex_notify(text)
            check(got == CODEX_ORIGINAL, "codex: notify 还原为原命令 %s" % got)
            import tomllib
            check(tomllib.loads(text).get("notify") == CODEX_ORIGINAL,
                  "codex: 还原后 TOML 合法且值正确")
        check(not bridge.hook_installed_path().exists(),
              "%s: 钩子文件已删除" % name)

        st2 = bridge.installed_state()
        check(not st2["configured"], "%s: 卸载后状态=未接入" % name)

        # --- 卸载幂等 ---
        rep4 = bridge.uninstall(dry=False)
        check("本来就没装" in rep4 or "未指向" in rep4 or "没接" in rep4,
              "%s: 重复卸载幂等" % name)
    finally:
        if old_up is not None:
            os.environ["USERPROFILE"] = old_up
        if old_app is not None:
            os.environ["APPDATA"] = old_app
        shutil.rmtree(home, ignore_errors=True)


def main():
    targets = sys.argv[1:] or ["zcode", "workbuddy", "codex"]
    mods = {"zcode": zcode_bridge, "workbuddy": workbuddy_bridge,
            "codex": codex_bridge}
    for t in targets:
        test_one(t, mods[t])
    print("")
    print("结果：%d 通过，%d 失败" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
