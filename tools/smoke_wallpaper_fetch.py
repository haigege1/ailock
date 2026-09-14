"""Bing 在线壁纸下载真跑冒烟（多市场版）。

真实请求多个 Bing 市场接口并下载最近 2 天壁纸，验证：
- 接口可达且返回合法元数据，meta.json 记录成功市场数（应 >= 3）
- 文件真实落盘且是 JPEG（FFD8 魔数）
- 二次调用增量跳过（下载 0 张）

运行：python tools/smoke_wallpaper_fetch.py
"""

import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import wallpaper_fetch as WF


def is_jpeg(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"\xff\xd8"
    except Exception:
        return False


def main() -> int:
    print("[1] fetch_online(days=2) 真跑下载 ...")
    n = WF.fetch_online(days=2)
    files = sorted(glob.glob(os.path.join(WF.wallpaper_dir(), "bing_*.jpg")))
    print(f"    新下载 {n} 张，缓存目录共 {len(files)} 张")
    if not files:
        print("    ✗ 没有任何壁纸落盘")
        return 1
    bad = [p for p in files if not is_jpeg(p)]
    if bad:
        print(f"    ✗ 非 JPEG 文件：{bad}")
        return 1
    print(f"    ✓ 全部为合法 JPEG（示例：{os.path.basename(files[-1])}）")

    print("[2] 二次调用应增量跳过 ...")
    n2 = WF.fetch_online(days=2)
    print(f"    二次下载 {n2} 张")
    if n2 != 0:
        print(f"    ⚠ 预期 0，实际 {n2}（若 Bing 恰好换图可忽略）")

    print("[3] 多市场验证（meta.json markets 应 >= 3）...")
    try:
        with open(os.path.join(WF.wallpaper_dir(), "meta.json"),
                  encoding="utf-8") as f:
            meta = json.load(f)
    except Exception as exc:
        print(f"    ✗ meta.json 读取失败：{exc!r}")
        return 1
    mk = int(meta.get("markets", 0) or 0)
    print(f"    成功市场数 {mk}/{len(WF._MARKETS)}")
    if mk < 3:
        print("    ✗ 成功市场数不足，多市场拉取可能失效")
        return 1
    print("    ✓ 多市场拉取生效")

    print("[4] 滚动清理 cleanup_old（本地临时目录，不走网络）...")
    import tempfile
    with tempfile.TemporaryDirectory() as tmp_dir:
        names = ["bing_20260901_a.jpg", "bing_20260902_b.jpg",
                 "bing_20260903_c.jpg", "bing_20260904_d.jpg"]
        for n in names:
            open(os.path.join(tmp_dir, n), "wb").close()
        open(os.path.join(tmp_dir, "meta.json"), "w").close()
        open(os.path.join(tmp_dir, "user_own.jpg"), "wb").close()
        removed = WF.cleanup_old(2, folder=tmp_dir)
        left = sorted(os.listdir(tmp_dir))
        print(f"    keep=2 删除 {removed} 张，剩余 {left}")
        if removed != 2 or left != ["bing_20260903_c.jpg",
                                    "bing_20260904_d.jpg",
                                    "meta.json", "user_own.jpg"]:
            print("    ✗ 清理结果不符合预期（应只删最旧 2 张，保留其它文件）")
            return 1
        print("    ✓ 只删最旧、保留窗口内文件与无关文件")

    print("[5] 按图去重 dedupe_existing（本地临时目录，不走网络）...")
    with tempfile.TemporaryDirectory() as tmp_dir:
        # 同一张图（slug 相同）跨市场差一天上线，应只留最早且优先 ZH-CN
        names = ["bing_20260903_X_ZH-CN111.jpg", "bing_20260904_X_EN-US222.jpg",
                 "bing_20260903_Y_EN-GB333.jpg", "bing_20260903_Y_ZH-CN444.jpg",
                 "bing_20260905_Z_ZH-CN555.jpg"]
        for n in names:
            open(os.path.join(tmp_dir, n), "wb").close()
        removed = WF.dedupe_existing(folder=tmp_dir)
        left = sorted(os.listdir(tmp_dir))
        print(f"    删除 {removed} 张重复，剩余 {left}")
        if removed != 2 or left != ["bing_20260903_X_ZH-CN111.jpg",
                                    "bing_20260903_Y_ZH-CN444.jpg",
                                    "bing_20260905_Z_ZH-CN555.jpg"]:
            print("    ✗ 去重结果不符合预期")
            return 1
        print("    ✓ 同图跨市场只留一张（最早日期、优先 ZH-CN）")

    ts = WF.last_refresh()
    print(f"[6] last_refresh = {ts:.0f}")
    if ts <= 0:
        print("    ✗ meta 未写入")
        return 1
    print("    ✓ meta 已写入")
    print("\n冒烟通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
