"""真实 WebView2 冒烟：创建锁屏窗口，2 秒后自动关闭，验证窗口创建链路不崩。"""
import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config
from ui.webui import WebUI

cfg = Config()
eng = WebUI.get()
eng.attach(None, cfg, None, None)

try:
    wins = eng.create_lock(lambda p: (False, "x"), 0)
    print("SMOKE created windows:", len(wins), flush=True)
except Exception as exc:
    print("SMOKE create_lock ERR:", repr(exc), flush=True)
    raise SystemExit(1)


def closer():
    time.sleep(2)
    try:
        eng.destroy_lock()
        eng.stop()
        print("SMOKE closed", flush=True)
    except Exception as exc:
        print("SMOKE close ERR:", repr(exc), flush=True)


threading.Thread(target=closer, daemon=True).start()

try:
    WebUI.get().start()
    print("SMOKE start returned", flush=True)
except Exception as exc:
    print("SMOKE start ERR:", repr(exc), flush=True)
print("SMOKE_DONE", flush=True)
