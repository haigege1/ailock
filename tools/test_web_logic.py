"""Web UI 桥接逻辑验证（不创建真实窗口）。"""
import os, sys, tempfile, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ui.webui as W
from core.config import Config
from core.security import Vault

tmp = tempfile.mkdtemp()
cfg = Config(path=os.path.join(tmp, "config.json"))
vault = Vault(path=os.path.join(tmp, "vault.dat"))

eng = W.WebUI.get()
eng.attach(None, cfg, vault, None)
api = W.AppApi(eng)

# 1) getConfig / setConfig
cfg.set("message.text", "测试提示语")
assert api.getConfig()["message"]["text"] == "测试提示语", "getConfig 应回显"
api.setConfig("lock.min_lock_minutes", 2)
assert cfg.get("lock.min_lock_minutes") == 2, "setConfig 应落盘"

# 2) changePassword（首次未设密码）
assert api.changePassword("", "abcd")[ "ok"] is True
assert vault.is_set, "设置后应 is_set"
assert api.changePassword("wrong", "efgh")["ok"] is False, "旧密码错应失败"
assert api.changePassword("abcd", "efgh")["ok"] is True, "旧密码对可改"
assert vault.verify("efgh"), "新密码应生效"

# 3) build_wallpapers：纯色模式
cfg.set("background.mode", "color")
cfg.set("background.color", "#102030")
items, idx, ken = eng.build_wallpapers()
assert items and items[0]["url"].startswith("linear-gradient"), "纯色应返回渐变"
assert idx == 0 and ken is False, "纯色模式不缓移"

# 4) build_wallpapers：图片模式（多图同目录，定位当前图）
from PIL import Image
d = os.path.join(tmp, "imgs")
os.makedirs(d, exist_ok=True)
paths = []
for n in ("a.png", "b.png", "c.png"):
    p = os.path.join(d, n)
    Image.new("RGB", (4, 4), (10, 20, 30)).save(p)
    paths.append(p)
cfg.set("background.mode", "image")
cfg.set("background.path", paths[1])  # b.png
items, idx, ken = eng.build_wallpapers()
assert len(items) == 3, "应列出同目录全部图片"
assert items[idx]["url"].startswith("data:image/png;base64,"), "应为 dataURL"
assert os.path.basename(json.loads('"%s"' % items[idx]["name"]) if False else items[idx]["name"]) == "b.png", "应定位到 b.png"

# 5) switch_wall_index 循环
i0, _, _ = eng.switch_wall_index(1)
i1, _, _ = eng.switch_wall_index(1)
assert i1 == (i0 + 1) % 3, "切换应前进一位并回绕"

# 6) 颜色模式切墙不应崩
cfg.set("background.mode", "color")
ci, citems, _ = eng.switch_wall_index(1)
print("DEBUG color ci=%r len=%d" % (ci, len(citems)))
assert ci == 0

print("WEB_LOGIC_OK")
