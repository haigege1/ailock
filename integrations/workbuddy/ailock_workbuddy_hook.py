#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AiLock × WorkBuddy 状态桥接钩子（Python 版，仅标准库，Python 3.8+）。

WorkBuddy 桌面版的 hooks 与 Claude Code 同构：事件发生时向本进程 stdin
写入「一行 JSON」，Windows 下经 Git Bash 执行（命令须 bash 兼容）。本脚本：

1. 把实时对话气泡（你的提问 / AI 回复 / 工具调用）按会话原子写进
   AiLock 的 status.d/<sid>.json（多会话各写各的，互不覆盖；
   sid 取自 stdin 的 session_id，过滤后作文件名）
2. Stop 触发后经过一个「完成确认窗口」再写 done：
   - Stop 在每一轮对话结束都会触发，不是整个会话结束
   - 直接写 done 会让多轮对话每轮都触发一次 on_done_action
   - 所以 Stop 时先保持 running，另起一个后台确认进程，等
     DONE_DELAY_SECONDS 秒后再检查：期间没有新事件才写 done
   - 设环境变量 AILOCK_DONE_DELAY=0 可关掉窗口（Stop 立即写 done）
3. 跳过 subagent 触发的事件（session_id / transcript_path 含 "subagent"）

与 ZCode 版的差异：不解析 transcript（WorkBuddy 事件 payload 直接带
prompt / tool_input / last_assistant_message，够用且更稳）。

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

MAX_LINES = 4          # 锁屏一次显示几行
STDIN_TIMEOUT = 5.0    # 等 stdin 的最长秒数

# 完成确认窗口（秒）。0 = Stop 立即写 done。
DONE_DELAY_SECONDS = float(os.environ.get("AILOCK_DONE_DELAY", "12") or "0")

HOOK_VERSION = "py-wb-1.0"


def log(msg):
    try:
        sys.stderr.write("[ailock-wb-hook] %s\n" % msg)
    except Exception:
        pass


def status_file(sid=""):
    """会话状态文件路径：sid 非空走 status.d/<sid>.json，空走主文件。"""
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    if sid:
        sid = "".join(c for c in sid
                      if c in "abcdefghijklmnopqrstuvwxyz"
                         "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")[:64]
        if sid:
            return Path(base) / "AiLock" / "status.d" / (sid + ".json")
    return Path(base) / "AiLock" / "status.json"


def read_json(path, fallback=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else fallback
    except Exception:
        return fallback


def write_json_atomic(path, data):
    """同目录临时文件 + os.replace，保证写入原子性。"""
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


def read_stdin(timeout=STDIN_TIMEOUT):
    """读完全部 stdin（写完会关流）；超时则用已收到的部分。"""
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


def clip(s, n):
    s = " ".join(str(s or "").split())
    return (s[: n - 1] + "…") if len(s) > n else s


def describe_tool(tool, tool_input):
    tool_input = tool_input or {}
    raw = tool_input.get("file_path") or tool_input.get("path") \
        or tool_input.get("notebook_path") or ""
    name = os.path.basename(str(raw)) if raw else ""
    if tool in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return ("编辑 " + name) if name else "写入文件"
    if tool == "Bash":
        cmd = " ".join(str(tool_input.get("command") or "").split())
        return ("执行 " + cmd[:44]) if cmd else "执行命令"
    if tool == "Read":
        return ("读取 " + name) if name else "读取文件"
    if tool == "Grep":
        return "搜索代码"
    if tool == "Glob":
        return "查找文件"
    if tool in ("TaskCreate", "TaskUpdate", "TodoWrite"):
        return "更新任务清单"
    if tool in ("WebFetch",):
        return "抓取网页"
    if tool in ("WebSearch",):
        return "联网搜索"
    if tool in ("Agent", "Task"):
        return "调度子代理"
    return "调用 " + (tool or "工具")


def build_lines(prev_lines, ai_reply, tool_line, status_tail):
    """组织锁屏气泡：你 → AI → 工具 → 状态，最多 MAX_LINES 行。"""
    user_line = None
    for l in (prev_lines or []):
        if isinstance(l, str) and l.startswith("你:"):
            user_line = l
            break
    ordered = []
    if user_line:
        ordered.append(user_line)
    if ai_reply:
        ordered.append("AI: " + clip(ai_reply, 60))
    if tool_line:
        ordered.append(tool_line)
    if status_tail:
        ordered.append(status_tail)
    return ordered[-MAX_LINES:]


def spawn_done_watcher(base_ts, sid=""):
    """后台确认进程：等 DONE_DELAY_SECONDS 秒，无新事件才写 done。"""
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
             "--confirm-done", repr(float(base_ts)), "--sid", sid],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **kwargs)
        return True
    except Exception as err:
        log("spawn 确认进程失败: %s（降级：立即写 done）" % err)
        return False


def write_done_now(sid=""):
    st = status_file(sid)
    cur = read_json(st) or {}
    if cur.get("state") in ("failed", "waiting"):
        # failed 是明确结论；waiting 是在等人授权——都不能被 done 覆盖
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


def confirm_done(base_ts, sid=""):
    """--confirm-done 模式：确认窗口内本会话无新事件才翻 done。"""
    time.sleep(max(0.0, DONE_DELAY_SECONDS))
    st = status_file(sid)
    cur = read_json(st) or {}
    updated = float(cur.get("updated") or 0)
    if abs(updated - base_ts) > 1.0:
        log("确认窗口内又有新事件（%.3f -> %.3f），放弃写 done"
            % (base_ts, updated))
        return
    if cur.get("state") in ("done", "failed"):
        return
    write_done_now(sid)
    log("确认窗口内无新输入 -> done")


def handle_event(input_data):
    event = input_data.get("hook_event_name") or input_data.get("hookEventName") or ""
    tool = input_data.get("tool_name") or input_data.get("toolName") or ""
    tool_input = input_data.get("tool_input") or input_data.get("toolInput") or {}
    session_id = str(input_data.get("session_id") or input_data.get("sessionId") or "")
    transcript_path = str(input_data.get("transcript_path") or "")

    # --- subagent 事件：只刷本会话时间戳，不污染对话内容 ---
    if ("subagent" in session_id.lower()
            or "subagent" in transcript_path.lower()):
        st = status_file(session_id)
        prev = read_json(st) or {}
        data = {**prev, "updated": time.time(), "source": "workbuddy"}
        if not data.get("task"):
            data["task"] = "WorkBuddy 任务"
        if not data.get("state"):
            data["state"] = "running"
        write_json_atomic(st, data)
        log("skip-subagent %s (refresh updated only)" % event)
        return

    st = status_file(session_id)
    prev = read_json(st) or {}
    step = int(prev.get("step") or 0)
    data = {
        "task": prev.get("task") or "WorkBuddy 任务",
        "state": prev.get("state") or "idle",
        "lines": prev.get("lines") if isinstance(prev.get("lines"), list) else [],
        "progress": None,
        "updated": time.time(),
    }

    if event == "SessionStart":
        step = 0
        data["task"] = "WorkBuddy 已连接"
        data["state"] = "idle"
        data["lines"] = ["等待任务"]

    elif event == "UserPromptSubmit":
        prompt = " ".join(str(input_data.get("prompt") or "").split())
        step = 0
        data["task"] = clip(prompt or "WorkBuddy 任务", 40)
        data["state"] = "running"
        data["lines"] = ["你: " + clip(prompt or "（无内容）", 60)]

    elif event in ("PostToolUse", "PostToolUseFailure"):
        step += 1
        data["state"] = "running"
        prefix = "" if event == "PostToolUse" else "失败："
        tool_line = "%d. %s%s" % (step, prefix,
                                  describe_tool(tool, tool_input))
        data["lines"] = build_lines(data["lines"], None, tool_line, None)

    elif event == "PermissionRequest":
        # 授权请求对应「即将发生的第 step+1 步」，不消费步数
        data["state"] = "waiting"
        tool_line = "%d. 等待授权：%s" % (step + 1,
                                          describe_tool(tool, tool_input))
        data["lines"] = build_lines(data["lines"], None, tool_line, None)

    elif event == "Stop":
        if input_data.get("stop_hook_active") is True:
            # 其他 hook 引发的二次回调，跳过防递归
            log("Stop 由 hook 自身触发，跳过")
            return
        ai_reply = input_data.get("last_assistant_message") \
            or input_data.get("lastAssistantMessage") or ""

        if DONE_DELAY_SECONDS > 0:
            # 先保持 running 写基准时间戳，再交给后台确认进程
            data["state"] = "running"
            data["step"] = step
            data["progress"] = (min(0.9, step / (step + 6)) if step > 0 else None)
            data["source"] = "workbuddy"
            data["lines"] = build_lines(data["lines"], ai_reply, None,
                                        "✓ 本轮结束，等待确认…")
            write_json_atomic(st, data)
            if spawn_done_watcher(data["updated"], session_id):
                log("Stop -> running，%gs 后确认是否真结束" % DONE_DELAY_SECONDS)
                return
            # spawn 失败已降级写 done
            return

        data["state"] = "done"
        data["lines"] = build_lines(data["lines"], ai_reply, None, "✓ 已完成")

    else:
        log("未处理事件: %s" % event)
        return

    data["step"] = step
    if data["state"] == "done":
        data["progress"] = 1
    else:
        data["progress"] = (min(0.9, step / (step + 6)) if step > 0 else None)
    data["source"] = "workbuddy"
    write_json_atomic(st, data)
    log("%s -> %s | lines=%s" % (event, data["task"], json.dumps(
        data["lines"], ensure_ascii=False)))


def main():
    raw = read_stdin()
    if not raw:
        log("stdin 为空")
        return
    try:
        input_data = json.loads(raw)
    except Exception:
        log("stdin 不是合法 JSON")
        return
    handle_event(input_data)


if __name__ == "__main__":
    # 后台确认模式：python ailock_workbuddy_hook.py --confirm-done <ts> --sid <sid>
    if len(sys.argv) >= 3 and sys.argv[1] == "--confirm-done":
        sid = ""
        if "--sid" in sys.argv:
            i = sys.argv.index("--sid")
            if i + 1 < len(sys.argv):
                sid = sys.argv[i + 1]
        try:
            confirm_done(float(sys.argv[2]), sid)
        except Exception as err:
            log("confirm_done 异常: %r" % err)
        sys.exit(0)

    try:
        main()
    except Exception as err:
        log("异常: %r" % err)
    sys.exit(0)
