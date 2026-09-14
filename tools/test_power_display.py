"""定时息屏 + 在线壁纸相关逻辑测试（不启动真实线程，不开窗口）。

覆盖：
1. PowerGuard._compute 纯逻辑：常亮 / 到点息屏 / 恢复常亮 / 不广播重复关屏
2. 默认配置新增字段存在且可读写
3. Bing 文件名生成的稳定性

运行：python tools/test_power_display.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config
from core.power import (ES_CONTINUOUS, ES_DISPLAY_REQUIRED, ES_SYSTEM_REQUIRED,
                        PowerGuard)

OK = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def t1_compute():
    print("[1] PowerGuard._compute 逻辑（传统 S3 广播关屏路径）")
    g = PowerGuard(keep_display_on=True, off_after_minutes=1,
                   blackout_mode=False)

    # 未到阈值：常亮
    flags, off, need = g._compute(10.0, False)
    check("未到阈值保持常亮", bool(flags & ES_DISPLAY_REQUIRED))
    check("未到阈值不广播", not need and not off)

    # 跨过阈值：撤掉 DISPLAY_REQUIRED + 广播一次关屏
    flags, off, need = g._compute(61.0, False)
    check("到点撤掉常亮位", not (flags & ES_DISPLAY_REQUIRED))
    check("到点保留防休眠位", bool(flags & ES_SYSTEM_REQUIRED))
    check("跨阈值广播一次关屏", need and off)

    # 已经息屏且仍无输入：不重复广播
    flags, off, need = g._compute(120.0, True)
    check("持续息屏不重复广播", not need and off)

    # 用户回来了：恢复常亮，不广播
    flags, off, need = g._compute(3.0, True)
    check("键鼠活动恢复常亮", bool(flags & ES_DISPLAY_REQUIRED))
    check("恢复时不广播", not need and not off)


def t1b_blackout():
    print("[1b] PowerGuard._compute 黑屏模式（Modern Standby）")
    g = PowerGuard(keep_display_on=True, off_after_minutes=1,
                   blackout_mode=True)
    check("blackout_mode 生效", g.blackout_mode)

    # 未到阈值：不黑屏
    flags, blacked, need = g._compute(10.0, False)
    check("未到阈值不涂黑", not blacked)
    check("未到阈值不广播", not need)

    # 跨过阈值：涂黑，但永远不广播关屏
    flags, blacked, need = g._compute(61.0, False)
    check("到点涂黑", blacked)
    check("黑屏模式绝不广播关屏", not need)
    check("黑屏期间显示器保持点亮位",
          bool(flags & ES_DISPLAY_REQUIRED),
          "DISPLAY_REQUIRED 被撤掉会让 Windows 自己关屏→触发待机")
    check("黑屏期间保留防休眠位", bool(flags & ES_SYSTEM_REQUIRED))

    # 持续无输入：状态不变
    flags, blacked, need = g._compute(120.0, True)
    check("持续无输入保持涂黑", blacked and not need)

    # 用户回来了：解除黑屏
    flags, blacked, need = g._compute(3.0, True)
    check("键鼠活动解除黑屏", not blacked)
    check("解除时仍不广播", not need)

    # off_after=0 时黑屏模式强制关闭
    g0 = PowerGuard(keep_display_on=True, off_after_minutes=0,
                    blackout_mode=True)
    check("off_after=0 强制关闭黑屏", not g0.blackout_mode)

    # on_blackout 回调只应在状态翻转时触发
    calls = []
    g2 = PowerGuard(keep_display_on=True, off_after_minutes=1,
                    blackout_mode=True, on_blackout=lambda v: calls.append(v))
    g2._last_blackout = False
    for idle, cur in ((61.0, False), (70.0, True), (80.0, True), (5.0, True)):
        flags, cur, need = g2._compute(idle, cur)
        if cur != g2._last_blackout:
            g2._last_blackout = bool(cur)
            g2.on_blackout(bool(cur))
    check("回调只翻转两次（开/关）", calls == [True, False], calls)


def t2_keep_on_off():
    print("[2] keep_display_on=False 不干预显示器")
    g = PowerGuard(keep_display_on=False, off_after_minutes=5,
                   blackout_mode=False)
    flags, off, need = g._compute(999.0, False)
    check("不设置 DISPLAY_REQUIRED", not (flags & ES_DISPLAY_REQUIRED))
    check("不广播关屏", not need and not off)
    check("仍然阻止休眠", bool(flags & ES_SYSTEM_REQUIRED))


def t3_off_after_zero():
    print("[3] off_after=0 时维持原有常亮行为")
    g = PowerGuard(keep_display_on=True, off_after_minutes=0,
                   blackout_mode=False)
    for idle in (0.0, 3600.0):
        flags, off, need = g._compute(idle, False)
        check(f"idle={idle:.0f}s 始终常亮", bool(flags & ES_DISPLAY_REQUIRED)
              and not need and not off)


def t4_config():
    print("[4] 配置字段")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(path=os.path.join(tmp, "config.json"))
        check("默认 off_after_minutes=0",
              cfg.get("display.off_after_minutes", None) == 0)
        check("默认 online_days=8",
              cfg.get("background.online_days", None) == 8)
        cfg.set("display.off_after_minutes", 5)
        cfg.set("background.mode", "online")
        cfg2 = Config(path=os.path.join(tmp, "config.json"))
        check("off_after_minutes 落盘可回读",
              cfg2.get("display.off_after_minutes") == 5)
        check("background.mode=online 可回读",
              cfg2.get("background.mode") == "online")


def t5_filename():
    print("[5] Bing 文件名生成")
    from core.wallpaper_fetch import _filename
    name = _filename("20260901",
                     "/th?id=OHR.ZhangjiajieAutumn_EN-US1234567890")
    check("文件名稳定格式", name == "bing_20260901_ZhangjiajieAutumn_EN-US1234567890.jpg",
          name)
    check("非法字符被清洗",
          _filename("20260901", "/th?id=OHR.a/b\\c") == "bing_20260901_abc.jpg",
          _filename("20260901", "/th?id=OHR.a/b\\c"))
    check("空 urlbase 兜底", _filename("", "").startswith("bing_00000000_"))


if __name__ == "__main__":
    t1_compute()
    t1b_blackout()
    t2_keep_on_off()
    t3_off_after_zero()
    t4_config()
    t5_filename()
    print(f"\n结果：{OK} 通过，{FAIL} 失败")
    sys.exit(1 if FAIL else 0)
