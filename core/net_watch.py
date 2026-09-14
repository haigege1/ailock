"""网络看门狗：盯住「主机还在线吗」。

动机：Modern Standby（S0，无 S3）设备上，电源心跳一旦失效，系统到达
睡眠超时后就会进 S0 待机，网络随之挂起——远程再也无法唤醒。看门狗
负责在断网的瞬间告诉用户，而不是等想连回去时才发现。

探测策略：
    host 配置了 → 只 ping 它
    否则 → 默认网关 + 公共 DNS 任一通就算在线（网关通=局域网活，
    DNS 通=互联网活，两者互为补充，避免单点误判）

实现要点：
    网关用 GetBestRoute 纯 ctypes 获取，不解析 ipconfig 输出；
    ping 只看返回码，不解析输出——两者都与系统语言无关。
"""

import ctypes
import socket
import struct
import subprocess
import threading
import time

from .logger import log

# 探测兜底目标：阿里 / 腾讯公共 DNS
FALLBACK_HOSTS = ("223.5.5.5", "119.29.29.29")

# 持续断网时，每隔多久再提醒一次（秒）
RENOTIFY_SECONDS = 600.0


def default_gateway():
    """默认路由的下一跳 = 网关。拿不到 / on-link(0.0.0.0) 返回 None。"""
    class MIB_IPFORWARDROW(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulong) for n in (
            "dwForwardDest", "dwForwardMask", "dwForwardPolicy",
            "dwForwardNextHop", "dwForwardIfIndex", "dwForwardType",
            "dwForwardProto", "dwForwardAge", "dwForwardNextHopAS",
            "dwForwardMetric1", "dwForwardMetric2", "dwForwardMetric3",
            "dwForwardMetric4", "dwForwardMetric5")]

    try:
        row = MIB_IPFORWARDROW()
        if ctypes.windll.iphlpapi.GetBestRoute(0, 0, ctypes.byref(row)) != 0:
            return None
        # DWORD 按小端读进来，字节序已在内存里转了一道；用 "<" 打包
        # 还原成网络字节序的原始 4 字节（用 "!" 会再翻一次，得到反序 IP）
        gw = socket.inet_ntoa(struct.pack("<I", row.dwForwardNextHop))
        return None if gw == "0.0.0.0" else gw
    except Exception:
        return None


def ping(host: str, timeout_ms: int = 2000) -> bool:
    """单次 ping。只看返回码（0=通），不解析任何本地语言输出。"""
    if not host:
        return False
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            ["ping", "-n", "1", "-w", str(int(timeout_ms)), host],
            capture_output=True, creationflags=flags,
            timeout=timeout_ms / 1000.0 + 5.0)
    except Exception:
        return False
    return proc.returncode == 0


class NetWatchdog(threading.Thread):
    """周期探测网络；状态翻转时通过 on_event(event) 上报。

    event 取值：
        "down"     连续 fail_threshold 次失败，判定断网（只报一次）
        "still"    持续断网中，每 RENOTIFY_SECONDS 再报一次
        "recover"  断网后恢复在线

    用法：NetWatchdog(on_event=cb).start() ... 线程退出用 stop()。
    """

    def __init__(self, interval: float = 30.0, fail_threshold: int = 3,
                 host: str = "", on_event=None):
        super().__init__(daemon=True, name="NetWatchdog")
        self.interval = max(5.0, float(interval))
        self.fail_threshold = max(1, int(fail_threshold))
        self.host = str(host or "").strip()
        self.on_event = on_event
        self._stop_event = threading.Event()
        self._fails = 0
        self._down = False
        self._last_notify = 0.0

    # ---------- 探测 ----------
    def probe(self) -> bool:
        """跑一轮探测。配置了 host 只 ping 它，否则网关+公共DNS任一通即可。"""
        hosts = ([self.host] if self.host
                 else [h for h in (default_gateway(), *FALLBACK_HOSTS) if h])
        return any(ping(h) for h in hosts)

    # ---------- 状态机（纯逻辑，可单测） ----------
    def feed(self, ok: bool, now: float = None) -> list:
        """喂入一次探测结果，返回需要上报的事件列表。"""
        if now is None:
            now = time.time()
        events = []
        if ok:
            if self._down:
                events.append("recover")
            self._fails = 0
            self._down = False
        else:
            self._fails += 1
            if not self._down and self._fails >= self.fail_threshold:
                self._down = True
                self._last_notify = now
                events.append("down")
            elif self._down and now - self._last_notify >= RENOTIFY_SECONDS:
                self._last_notify = now
                events.append("still")
        return events

    # ---------- 线程 ----------
    def start(self):
        """返回 self，方便写成 `watch = NetWatchdog(...).start()`。"""
        super().start()
        return self

    def run(self) -> None:
        log.log("net.start",
                f"interval={self.interval:.0f}s threshold={self.fail_threshold}"
                f" host={self.host or '自动(网关+公共DNS)'}")
        while not self._stop_event.is_set():
            try:
                ok = self.probe()
                for ev in self.feed(ok):
                    log.log("net.down" if ev != "recover" else "net.recover",
                            {"down": "判定断网", "still": "断网仍未恢复",
                             "recover": "网络已恢复"}.get(ev, ev))
                    if self.on_event:
                        try:
                            self.on_event(ev)
                        except Exception:
                            pass
            except Exception as exc:  # pragma: no cover
                log.log("net.error", repr(exc))
            self._stop_event.wait(self.interval)
        log.log("net.stop", "网络看门狗已停止")

    def stop(self) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=10.0)
