#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AiLock × ZCode 状态桥接钩子（Python 版，仅标准库，Python 3.8+）。

ZCode 在每个事件发生时向本进程 stdin 写入「一行 JSON」。本脚本：

1. 把 ZCode 实时对话气泡（你 / AI 回复 / 工具调用）按会话原子写进
   AiLock 的 status.d/<sid>.json（多会话各写各的，互不覆盖；
   sid 取自 stdin 的 session_id，过滤后作文件名）
2. Stop 触发后经过一个「完成确认窗口」再写 done：
   - ZCode 的 Stop 在每一轮对话结束都会触发，不是整个会话结束
   - 直接写 done 会让多轮对话每轮都触发一次 on_done_action
   - 所以 Stop 时先保持 running，另起一个后台确认进程，等
     DONE_DELAY_SECONDS 秒后再检查：期间没有新事件才写 done
   - 确认进程只核对本会话文件，其他会话在跑不影响本会话判定
   - 设环境变量 AILOCK_DONE_DELAY=0 可关掉窗口（Stop 立即写 done）
3. 跳过 subagent 触发的事件（session_id / transcript_path 含 "subagent"）
4. 从 stdin 里的 transcript_path（临时目录）解析 AI 回复与用户提问

为什么不直接调 AiLock.exe --notify：
  AiLock.exe 是几十 MB 的打包程序，冷启动 1~3 秒，而 PostToolUse 每次
  工具调用都会触发，会明显拖慢 ZCode。直接按协议写文件只要几毫秒。

诊断信息一律写 stderr（ZCode 会记进日志），stdout 保持为空。
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
TRANSCRIPT_TAIL = 200 * 1024   # transcript 只读尾部 200KB

# 完成确认窗口（秒）。0 = Stop 立即写 done。
DONE_DELAY_SECONDS = float(os.environ.get("AILOCK_DONE_DELAY", "12") or "0")

HOOK_VERSION = "py-1.1"


def log(msg):
    try:
        sys.stderr.write("[ailock-hook] %s\n" % msg)
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
    """读完全部 stdin（ZCode 写完会关流）；超时则用已收到的部分。"""
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


def tail_text(path, max_bytes=TRANSCRIPT_TAIL):
    """读文件尾部（跳过可能残缺的首行），返回文本。"""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
        data = f.read()
    text = data.decode("utf-8", errors="replace")
    if size > max_bytes:
        nl = text.find("\n")
        if nl >= 0:
            text = text[nl + 1:]
    return text


def parse_lines(text):
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def aggregate_ai_text(transcript_path):
    """从 transcript 的 model_streaming 事件聚合最近一次 AI 文本回复。

    期望格式：每行 JSON，type=model_streaming，
    payload.kind=text_start|text_delta|text_end，payload.assistantMessageId。
    """
    try:
        objs = parse_lines(tail_text(transcript_path))
    except Exception as err:
        log("aggregate_ai_text 失败: %s" % err)
        return None

    buckets = {}   # assistantMessageId -> text（按插入序）
    order = []
    current_mid = None
    for obj in reversed(objs):
        if obj.get("type") != "model_streaming":
            continue
        p = obj.get("payload") or {}
        kind = p.get("kind")
        if kind == "text_end":
            current_mid = None
            continue
        mid = p.get("assistantMessageId") or current_mid
        if not mid:
            continue
        if kind == "text_start":
            current_mid = mid
            break   # 反向遍历遇到开头即止，最近一次回复收集完毕
        if kind == "text_delta":
            if mid not in buckets:
                buckets[mid] = ""
                order.append(mid)
            buckets[mid] = (p.get("delta") or "") + buckets[mid]
            current_mid = mid
    for mid in order:
        if buckets[mid]:
            return buckets[mid]
    return None


def aggregate_user_input(transcript_path):
    """找最近一次主对话的用户提问（turn_started.payload.input）。"""
    try:
        objs = parse_lines(tail_text(transcript_path))
    except Exception as err:
        log("aggregate_user_input 失败: %s" % err)
        return None

    last = None
    for obj in objs:
        if obj.get("type") != "turn_started":
            continue
        p = obj.get("payload") or {}
        if p.get("querySource") and p.get("querySource") != "main_turn":
            continue
        if p.get("inputSource") == "subagent":
            continue
        if p.get("input"):
            last = " ".join(str(p["input"]).split())
    return last


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
    if tool == "TodoWrite":
        return "更新任务清单"
    if tool == "WebFetch":
        return "抓取网页"
    if tool == "WebSearch":
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
        # （读-写之间可能插入了 PermissionRequest，这里再核一次防竞态）
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
    """--confirm-done 模式：确认窗口内本会话无新事件才翻 done。

    只核对本会话文件的时间戳——其他会话是否在跑与本会话无关，
    全局动作抑制由 AiLock 监控端（MultiStatusMonitor）负责。
    """
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
    transcript_path = input_data.get("transcript_path") or input_data.get("transcriptPath") or ""
    session_id = str(input_data.get("session_id") or input_data.get("sessionId") or "")

    # --- subagent 事件：只刷本会话时间戳，不污染对话内容 ---
    is_sub = ("subagent" in session_id
              or "subagent" in transcript_path.lower())
    if is_sub:
        st = status_file(session_id)
        prev = read_json(st) or {}
        data = {**prev, "updated": time.time(), "source": "zcode"}
        if not data.get("task"):
            data["task"] = "ZCode 任务"
        if not data.get("state"):
            data["state"] = "running"
        write_json_atomic(st, data)
        log("skip-subagent %s (refresh updated only)" % event)
        return

    # --- 主对话：从 transcript 解析 AI 回复与用户提问 ---
    ai_reply = None
    user_from_transcript = None
    if transcript_path and os.path.exists(transcript_path):
        ai_reply = aggregate_ai_text(transcript_path)
        user_from_transcript = aggregate_user_input(transcript_path)

    st = status_file(session_id)
    prev = read_json(st) or {}
    step = int(prev.get("step") or 0)
    data = {
        "task": prev.get("task") or "ZCode 任务",
        "state": prev.get("state") or "idle",
        "lines": prev.get("lines") if isinstance(prev.get("lines"), list) else [],
        "progress": None,
        "updated": time.time(),
    }

    if event == "SessionStart":
        step = 0
        data["task"] = "ZCode 已连接"
        data["state"] = "idle"
        data["lines"] = ["等待任务"]

    elif event == "UserPromptSubmit":
        prompt = " ".join(str(input_data.get("prompt") or "").split())
        step = 0
        data["task"] = clip(prompt or user_from_transcript or "ZCode 任务", 40)
        data["state"] = "running"
        data["lines"] = ["你: " + clip(prompt or user_from_transcript or "（无内容）", 60)]

    elif event in ("PostToolUse", "PostToolUseFailure"):
        step += 1
        data["state"] = "running"
        tool_line = "%d. %s" % (step, describe_tool(tool, tool_input))
        data["lines"] = build_lines(data["lines"], ai_reply, tool_line, None)
        if user_from_transcript:
            data["task"] = clip(user_from_transcript, 40)

    elif event == "PermissionRequest":
        # 授权请求对应「即将发生的第 step+1 步」，不消费步数：
        # 批准后 PostToolUse 以同一编号落一行，拒绝则编号自然空缺
        data["state"] = "waiting"
        tool_line = "%d. 等待授权：%s" % (step + 1,
                                          describe_tool(tool, tool_input))
        data["lines"] = build_lines(data["lines"], ai_reply, tool_line, None)
        if user_from_transcript:
            data["task"] = clip(user_from_transcript, 40)

    elif event == "Stop":
        if input_data.get("stop_hook_active") is True:
            # 其他 hook 引发的二次回调，跳过防递归
            log("Stop 由 hook 自身触发，跳过")
            return
        if user_from_transcript:
            data["task"] = clip(user_from_transcript, 40)

        if DONE_DELAY_SECONDS > 0:
            # 先保持 running 写基准时间戳，再交给后台确认进程
            data["state"] = "running"
            data["step"] = step
            data["progress"] = (min(0.9, step / (step + 6)) if step > 0 else None)
            data["source"] = "zcode"
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
    data["source"] = "zcode"
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
    # 后台确认模式：python ailock_zcode_hook.py --confirm-done <base_ts> --sid <sid>
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
