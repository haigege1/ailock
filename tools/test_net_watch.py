"""启动自检（电源环境）+ 网络看门狗的测试（不启动真实线程）。

覆盖：
1. evaluate_selfcheck 纯逻辑：Modern Standby / 传统 S3 / 无超时 / 读不到
2. NetWatchdog.feed 状态机：阈值判定、断网去重、10 分钟再提醒、恢复
3. default_gateway / ping 真实探测（只验证类型与不通主机的确定性结果）
4. net.* 配置默认字段存在且可读写

运行：python tools/test_net_watch.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


def t1_selfcheck_logic():
    print("[1] evaluate_selfcheck 纯逻辑")
    from core.power import evaluate_selfcheck

    check("读不到超时不告警", evaluate_selfcheck(None, "") == [])
    check("从不睡眠不告警", evaluate_selfcheck(0, "") == [])

    warns = evaluate_selfcheck(2700, "待机 (S0 低电量待机)")   # 本机：45 分钟 + Modern Standby
    check("45 分钟 S0 产生一条警告", len(warns) == 1)
    check("警告含 S0 挂起网络", "S0 待机" in warns[0] and "网络" in warns[0])
    check("警告含分钟数", "45 分钟" in warns[0])

    warns = evaluate_selfcheck(1800, "待机 (S3)")              # 有 S3：传统睡眠
    check("S3 机器措辞不同", len(warns) == 1 and "S0" not in warns[1 - 1])

    warns = evaluate_selfcheck(90, "")                         # 状态未知：通用措辞
    check("状态未知用通用措辞", "睡眠/待机" in warns[0])

    warns = evaluate_selfcheck(2 * 3600 + 5 * 60, "S3")        # 小时换算
    check("2 小时换算", "2 小时" in warns[0])


def t2_feed():
    print("[2] NetWatchdog.feed 状态机")
    from core.net_watch import RENOTIFY_SECONDS, NetWatchdog

    w = NetWatchdog(fail_threshold=3)
    check("在线不产生事件", w.feed(True, now=0) == [])

    check("失败 1 次不判定", w.feed(False, now=30) == [])
    check("失败 2 次不判定", w.feed(False, now=60) == [])
    ev = w.feed(False, now=90)
    check("连续 3 次判定断网", ev == ["down"], str(ev))
    check("断网中不重复上报", w.feed(False, now=120) == [])

    ev = w.feed(False, now=90 + RENOTIFY_SECONDS)
    check("超 10 分钟再提醒", ev == ["still"], str(ev))

    check("恢复上报 recover", w.feed(True, now=90 + RENOTIFY_SECONDS + 30) == ["recover"])
    check("恢复后失败重新计数", w.feed(False, now=960) == [])

    w2 = NetWatchdog(fail_threshold=1)
    check("threshold=1 一次就判定", w2.feed(False, now=0) == ["down"])


def t3_real_probe():
    print("[3] 真实探测（网关 / ping）")
    from core.net_watch import default_gateway, ping

    gw = default_gateway()
    check("default_gateway 返回 None 或合法 IP",
          gw is None or (gw.count(".") == 3 and all(p.isdigit() for p in gw.split("."))),
          repr(gw))
    print(f"    （当前网关: {gw}）")

    # TEST-NET 保留网段，永远不通 —— 结果是确定性的 False
    check("保留网段 ping 必失败", ping("203.0.113.1") is False)

    # 在线时公共 DNS 应通；离线则跳过判定（探测本身不允许抛异常）
    online = ping("223.5.5.5")
    print(f"    （公共 DNS 223.5.5.5: {'通' if online else '不通（可能离线）'}）")
    check("ping 保留地址时也捕获空串", ping("") is False)


def t4_config():
    print("[4] net.* 配置默认字段")
    import tempfile

    from core.config import Config
    with __import__("tempfile").TemporaryDirectory() as tmp:
        cfg = Config(path=os.path.join(tmp, "config.json"))
        check("默认 net.watch=True", cfg.get("net.watch") is True)
        check("默认 interval_seconds=30", cfg.get("net.interval_seconds") == 30)
        check("默认 fail_threshold=3", cfg.get("net.fail_threshold") == 3)
        check("默认 host 为空", cfg.get("net.host") == "")
        cfg.set("net.interval_seconds", 15)
        cfg2 = Config(path=os.path.join(tmp, "config.json"))
        check("interval_seconds 落盘可回读", cfg2.get("net.interval_seconds") == 15)


if __name__ == "__main__":
    t1_selfcheck_logic()
    t2_feed()
    t3_real_probe()
    t4_config()
    print(f"\n结果：{OK} 通过，{FAIL} 失败")
    sys.exit(1 if FAIL else 0)
