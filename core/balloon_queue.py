"""锁屏期间的托盘提醒暂存队列（纯逻辑，无 UI / 无 Win32 依赖）。

为什么需要它：
    锁屏时 AiLock 盖着全屏窗口，托盘气泡根本看不见；Windows 还会因为
    「全屏应用时隐藏通知」把气泡压在队列里，等全屏窗口消失时一次性放出来
    —— 用户看到的就是「通知半天不出，然后一起全部弹出来」。
    更糟的是系统掉进连接待机时进程被挂起，积压量能到上千条。

做法：锁屏期间不弹，先入队（去重 + 上限保护）；解锁后 take() 一次取走，
单条原样弹，多条合并成一条摘要，只列最近几条。
"""


class BalloonQueue:
    """上限保护的去重队列，take() 时合并。"""

    def __init__(self, max_items: int = 20, summary_max: int = 5):
        self.max_items = max(1, int(max_items))
        self.summary_max = max(1, int(summary_max))
        self._items: list[tuple[str, str]] = []

    def add(self, title: str, message: str) -> None:
        item = (str(title), str(message))
        if item in self._items:          # 看门狗/网络探测会重复报同一件事
            return
        self._items.append(item)
        while len(self._items) > self.max_items:
            del self._items[0]           # 丢最旧的，保留最近发生的

    def take(self):
        """取走并清空。返回 (title, message)，空队列返回 None。"""
        if not self._items:
            return None
        items, self._items = self._items, []
        if len(items) == 1:
            return items[0]
        shown = items[-self.summary_max:]
        lines = [f"· {t}：{_first_line(m)}" for t, m in shown]
        if len(items) > len(shown):
            lines.append(f"…另有 {len(items) - len(shown)} 条")
        return (f"{len(items)} 条未读提醒", "\n".join(lines))

    def clear(self) -> None:
        self._items = []

    def __len__(self) -> int:
        return len(self._items)


def _first_line(text: str) -> str:
    for line in str(text).splitlines():
        if line.strip():
            return line.strip()
    return ""
