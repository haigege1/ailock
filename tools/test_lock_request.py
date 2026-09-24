"""LockRequest 回归测试：tools\\test_lock_request.py

背景（2026-09-24）：%APPDATA%\\AiLock\\lock_request 里躺着一个 9/14 的残留
时间戳，而 consume() 的 _last_seen 初始值是 0.0 —— 任何残留请求都比它大，
于是**每次启动 AiLock 都会立刻锁屏**。重启后真机上复现了一次。

覆盖：
  1. 启动前就存在的残留请求：不认，且顺手删掉
  2. 启动后写入的请求：认一次，之后不再认（文件已清除）
  3. 二次启动流程：request() -> consume() == True
  4. 文件缺失 / 内容非法：不崩、返回 False
  5. 并发复用：consume 连续调用只生效一次

用法:
    ..\\.venv\\Scripts\\python.exe tools\\test_lock_request.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import LockRequest  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def main():
    td = Path(tempfile.mkdtemp(prefix="ailock-lockreq-"))
    p = td / "lock_request"

    print("== 1. 残留请求不认（启动不锁屏） ==")
    p.write_text(str(time.time() - 86400 * 10), encoding="utf-8")   # 10 天前
    lr = LockRequest(p)
    check("启动时读到残留请求 -> False", lr.consume() is False)
    check("残留文件被清掉", not p.exists())

    print("== 2. 启动后写入的请求认一次 ==")
    p.write_text(str(time.time()), encoding="utf-8")
    check("新请求 -> True", lr.consume() is True)
    check("消费后文件清除", not p.exists())
    check("再调一次 -> False", lr.consume() is False)

    print("== 3. 二次启动流程 request() -> consume() ==")
    LockRequest(p).request()
    check("写入后能被运行中的实例认到", lr.consume() is True)

    print("== 4. 缺失 / 脏数据 ==")
    check("文件不存在 -> False", LockRequest(td / "nope").consume() is False)
    p.write_text("不是数字", encoding="utf-8")
    check("内容非法 -> False", LockRequest(p).consume() is False)
    p.write_text("", encoding="utf-8")
    check("空文件 -> False", LockRequest(p).consume() is False)

    print("== 5. 旧实例不受后续请求影响（_last_seen 单调） ==")
    q = td / "lr2"
    q.write_text(str(time.time() - 3600), encoding="utf-8")
    lr2 = LockRequest(q)
    check("一小时前的请求 -> False", lr2.consume() is False)
    lr2.request()
    check("刚写的请求 -> True", lr2.consume() is True)

    print(f"\n结果: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
