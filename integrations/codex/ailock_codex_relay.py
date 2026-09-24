#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AiLock × Codex 状态中转脚本（Python 版，仅标准库，Python 3.8+）。

Codex 的 notify 是根级单值（config.toml 顶部 notify = [...]），只能填一个
命令，而且本机已被 codex-computer-use.exe 占用。本脚本是「一变二」的中转：

  Codex 每轮结束 ──► python ailock_codex_relay.py '<json>'
                        ├─ 1. 原样转发给原 notify 命令（computer-use 不受影响）
                        └─ 2. 把本轮结果写进 AiLock 的 status.d/codex.json

原 notify 命令存在 sidecar（%APPDATA%/AiLock/hooks/codex_relay.json）里，
由 core/codex_bridge.py 在安装时写入；sidecar 不存在则只写状态不转发。

与 ZCode/WorkBuddy 钩子一样：Stop 类事件（agent-turn-complete）在每轮
结束都触发，不是整个任务结束 —— 所以先保持 running，另起后台确认进程，
等 DONE_DELAY_SECONDS 秒无新事件才翻 done（AILOCK_DONE_DELAY=0 关闭窗口）。

诊断信息一律写 stderr，stdout 保持为空。
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

STDIN_TIMEOUT = 5.0    # 兼容 stdin 输入的读取超时（notify 走 argv，一般用不到）

# 完成确认窗口（秒）。0 = 立即写 done。
DONE_DELAY_SECONDS = float(os.environ.get("AILOCK_DONE_DELAY", "12") or "0")

HOOK_VERSION = "py-cx-1.0"
SID = "codex"          # 固定会话名：一台机器同时只有一个 Codex 主会话


def log(msg):
    try:
        sys.stderr.write("[ailock-codex-relay] %s\n" % msg)
    except Exception:
        pass


def status_file():
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "AiLock" / "status.d" / (SID + ".json")


def sidecar_path():
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "AiLock" / "hooks" / "codex_relay.json"


def read_json(path, fallback=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else fallback
    except Exception:
        return fallback


def write_json_atomic(path, data):
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                   dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        os.replace(tmp, str(path))
    except Exception as err:
        log("写 %s 失败: %s" % (path.name, err))


def clip(s, n):
    s = " ".join(str(s or "").split())
    return (s[: n - 1] + "…") if len(s) > n else s


def forward_original(raw):
    """把原始 JSON 转发给原 notify 命令（fire-and-forget，不阻塞）。"""
    sidecar = read_json(sidecar_path()) or {}
    original = sidecar.get("original")
    if not (isinstance(original, list) and original):
        return
    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(
            [str(a) for a in original] + [raw],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **kwargs)
        log("已转发原 notify：%s" % original[0])
    except Exception as err:
        log("转发原 notify 失败（不影响状态写入）: %s" % err)


def spawn_done_watcher(base_ts):
    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__),
             "--confirm-done", repr(float(base_ts))],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **kwargs)
        return True
    except Exception as err:
        log("spawn 确认进程失败: %s（降级：立即写 done）" % err)
        return False


def write_done_now():
    st = status_file()
    cur = read_json(st) or {}
    if cur.get("state") in ("failed", "waiting"):
        log("当前 %s，不覆盖为 done" % cur.get("state"))
        return
    write_json_atomic(st, {
        **cur,
        "state": "done",
        "progress": 1,
        "updated": time.time(),
        "hook_version": HOOK_VERSION,
        "lines": [l for l in (cur.get("lines") or [])
                  if not str(l).startswith("✓")] + ["✓ 已完成"],
    })


def confirm_done(base_ts):
    time.sleep(max(0.0, DONE_DELAY_SECONDS))
    cur = read_json(status_file()) or {}
    updated = float(cur.get("updated") or 0)
    if abs(updated - base_ts) > 1.0:
        log("确认窗口内又有新事件（%.3f -> %.3f），放弃写 done"
            % (base_ts, updated))
        return
    if cur.get("state") in ("done", "failed"):
        return
    write_done_now()
    log("确认窗口内无新输入 -> done")


def handle_event(payload):
    """Codex notify 的 payload 是 JSON 对象（作为单个 argv 传入）。

    已知类型：
      agent-turn-complete —— 每轮结束，字段 input-messages /
                             last-assistant-message / turn-id
    """
    etype = str(payload.get("type") or "")
    st = status_file()
    prev = read_json(st) or {}
    step = int(prev.get("step") or 0)
    data = {
        "task": prev.get("task") or "Codex 任务",
        "state": prev.get("state") or "idle",
        "lines": prev.get("lines") if isinstance(prev.get("lines"), list) else [],
        "progress": None,
        "updated": time.time(),
    }

    if etype == "agent-turn-complete":
        # 任务名：取本轮输入的第一条消息；第一次见到就固定下来
        inputs = payload.get("input-messages") or []
        first_input = ""
        if isinstance(inputs, list) and inputs:
            first_input = " ".join(str(inputs[0]).split())
        if first_input and (not prev.get("task")
                            or prev.get("task") in ("Codex 任务", "Codex 已连接")):
            data["task"] = clip(first_input, 40)
        ai_reply = payload.get("last-assistant-message") or ""
        step += 1

        if DONE_DELAY_SECONDS > 0:
            data["state"] = "running"
            data["step"] = step
            data["progress"] = min(0.9, step / (step + 6))
            data["source"] = "codex"
            # turn_end_at 兜底：宿主收进程树时后台确认进程会被连坐杀掉，
            # 光靠它文件会永远停在 running。AiLock 读取侧见到本字段且过了
            # 确认窗口就补记 done。
            data["turn_end_at"] = data["updated"]
            data["lines"] = ([l for l in data["lines"]
                              if isinstance(l, str) and l.startswith("你:")]
                             + ["AI: " + clip(ai_reply, 60)]
                             + ["✓ 第 %d 轮结束，等待确认…" % step])[-4:]
            write_json_atomic(st, data)
            if spawn_done_watcher(data["updated"]):
                log("turn-complete -> running，%gs 后确认" % DONE_DELAY_SECONDS)
                return
            # spawn 失败已降级写 done
            return

        data["state"] = "done"
        data["step"] = step
        data["progress"] = 1
        data["source"] = "codex"
        data["lines"] = ([l for l in data["lines"]
                          if isinstance(l, str) and l.startswith("你:")]
                         + ["AI: " + clip(ai_reply, 60)]
                         + ["✓ 已完成"])[-4:]
    else:
        log("未处理 notify 类型: %s" % etype)
        return

    write_json_atomic(st, data)
    log("%s -> %s" % (etype, data["task"]))


def read_stdin(timeout=STDIN_TIMEOUT):
    chunks = []

    def _reader():
        try:
            while True:
                piece = sys.stdin.buffer.read(65536)
                if not piece:
                    break
                chunks.append(piece)
        except Exception:
            pass

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    t.join(timeout)
    return b"".join(chunks).decode("utf-8", errors="replace").strip()


def main():
    # notify 模式：JSON 作为单个命令行参数传入
    if len(sys.argv) >= 2:
        raw = sys.argv[1]
        try:
            payload = json.loads(raw)
        except Exception:
            log("argv[1] 不是合法 JSON")
            return
        forward_original(raw)
        if isinstance(payload, dict):
            handle_event(payload)
        return

    # 兼容 stdin 模式（调试用）：echo '<json>' | python ailock_codex_relay.py
    raw = read_stdin()
    if not raw:
        log("没有输入（既无 argv 也无 stdin）")
        return
    forward_original(raw)
    try:
        payload = json.loads(raw)
    except Exception:
        log("stdin 不是合法 JSON")
        return
    if isinstance(payload, dict):
        handle_event(payload)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--confirm-done":
        try:
            confirm_done(float(sys.argv[2]))
        except Exception as err:
            log("confirm_done 异常: %r" % err)
        sys.exit(0)

    try:
        main()
    except Exception as err:
        log("异常: %r" % err)
    sys.exit(0)
