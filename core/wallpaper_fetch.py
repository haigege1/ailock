"""在线壁纸下载器 · Bing 每日图源（多市场并行）

数据源：https://<host>/HPImageArchive.aspx（微软公开接口，无需 key，国内直
连）。Bing 单市场每天只出 1 张新图，池子最多 15 天就到顶，观感上「永远是那
几张」；同一张原图在不同市场（zh-CN / en-US / ja-JP ...）通常不同，因此并行
拉取多个市场，每天可新增 4~6 张，池子持续滚动更新。

设计要点：
- 按图去重：同一张图各市场差一天上线（图名 slug 相同、日期/编号不同），
  下载前按 slug 合并，磁盘上 dedupe_existing 兜底清理
- 滚动窗口：只下载/保留最新 keep 张（默认 30，配置 background.online_keep），
  新图进来、滑出窗口的旧图自动删除，磁盘占用有硬上限
- 单个市场失败只跳过该市场，全部失败才抛异常（与旧版容错语义一致）
- 优先拉 UHD（3840x2160，2K 屏缩放后依然锐利），404 再退回 1920x1080
- 全程静默容错：断网 / 超时只写日志，绝不影响锁屏使用本地缓存
- meta.json 记录最近一次成功刷新时间，供诊断与展示
"""

import json
import os
import re
import threading
import time
import urllib.request

from .config import app_dir
from .logger import log

_API_CANDIDATES = (
    "https://cn.bing.com/HPImageArchive.aspx?format=js&idx={idx}&n={n}&mkt={mkt}",
    "https://www.bing.com/HPImageArchive.aspx?format=js&idx={idx}&n={n}&mkt={mkt}",
)
# 多市场并行：单市场每天仅 1 张新图；不同市场当天图大多不同。
# zh-CN 排第一，保证国内最想看到的「今日中国区壁纸」优先下载。
_MARKETS = ("zh-CN", "en-US", "en-GB", "ja-JP", "de-DE", "fr-FR")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_HEADERS = {"User-Agent": _UA}
_META_NAME = "meta.json"
_MIN_JPEG_BYTES = 20_000   # 错误页/空响应兜底，正常壁纸远大于这个值
_TIMEOUT = 10              # 秒，API 与图片共用
_MARKET_SUFFIX_RE = re.compile(r"_[A-Z]{2}-[A-Z]{2}\d+$")   # _ZH-CN0517707643
_DEFAULT_KEEP = 30         # 滚动窗口默认保留张数（UHD 约 3.5MB/张 ≈ 105MB）


def wallpaper_dir() -> str:
    p = app_dir() / "wallpapers"
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def last_refresh() -> float:
    """最近一次成功拉取的 Unix 时间戳，失败返回 0。"""
    try:
        with open(os.path.join(wallpaper_dir(), _META_NAME),
                  encoding="utf-8") as f:
            return float(json.load(f).get("last_check", 0) or 0)
    except Exception:
        return 0.0


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return resp.read()


def _fetch_meta(days: int, mkt: str, idx: int = 0) -> list:
    """请求指定市场、指定回溯偏移的壁纸元数据，主源失败自动换备用源。

    idx=0 从今天往前数；实测接口单次 n 有上限，深历史靠 idx 翻页拿。
    """
    last_err = None
    for tpl in _API_CANDIDATES:
        try:
            raw = _http_get(tpl.format(n=int(days), mkt=mkt, idx=int(idx)))
            data = json.loads(raw.decode("utf-8", "replace"))
            images = data.get("images") or []
            if images:
                return images
        except Exception as exc:
            last_err = exc
    if last_err is not None:
        raise last_err
    return []


def _filename(startdate: str, urlbase: str) -> str:
    """由日期 + OHR 图片 ID 生成稳定的本地文件名。"""
    idx = (urlbase or "").find("OHR.")
    raw = urlbase[idx + 4:] if idx >= 0 else ""
    name = re.sub(r"[^\w-]", "", raw) or "img"
    return "bing_%s_%s.jpg" % (startdate or "00000000", name)


def _download_image(urlbase: str) -> bytes | None:
    """优先 UHD，失败退回 1920x1080。都失败返回 None。"""
    for suffix in ("_UHD.jpg", "_1920x1080.jpg"):
        for host in ("https://cn.bing.com", "https://www.bing.com"):
            try:
                data = _http_get(host + urlbase + suffix)
                if len(data) >= _MIN_JPEG_BYTES:
                    return data
            except Exception:
                continue
    return None


def _base_slug(fname: str) -> str:
    """从文件名提取图片唯一标识：去掉 bing_ 日期前缀与市场后缀。

    bing_20260903_Westerheversand_ZH-CN0517707643.jpg -> Westerheversand
    同一张图在不同市场上线日期差一天、OHR 数字也不同，但图名 slug 一致，
    这是「按图去重」的依据。
    """
    stem = fname[5:-4] if fname.lower().endswith(".jpg") else fname
    if stem.startswith("bing_"):
        stem = stem[5:]
    stem = stem.split("_", 1)[1] if "_" in stem else stem   # 去掉日期段
    return _MARKET_SUFFIX_RE.sub("", stem)


def fetch_online(days: int = 8, keep: int = _DEFAULT_KEEP) -> int:
    """同步拉取最近 N 天壁纸（多市场），滚动保留最新 keep 张。

    返回本次新下载的张数。单个市场失败只跳过；所有市场都失败才抛异常。
    按图片 slug 去重（同一张图各市场差一天上线，文件名不同但图相同）；
    按日期倒序只下载最新 keep 张；下载后清理磁盘上的重复图与滑出窗口
    的旧图，空间占用有硬上限。
    """
    days = max(1, min(int(days or 8), 15))   # Bing 接口最多回溯约 15 天
    keep = max(1, min(int(keep or _DEFAULT_KEEP), 200))
    ok_markets = 0
    last_err = None
    merged: dict = {}   # slug -> (fname, urlbase)，按图去重；zh-CN 先到先得

    for mkt in _MARKETS:
        got = False
        # 每市场翻两页：idx=0（今天~8 天前）+ idx=8（9~15 天前），
        # 把 15 天历史拿全；days<=8 时第二页无意义，跳过
        for idx in ((0, 8) if days > 8 else (0,)):
            n_page = min(days, 8)
            try:
                images = _fetch_meta(n_page, mkt, idx)
            except Exception as exc:
                last_err = exc
                log.log("wall.fetch.market_fail",
                        "%s idx=%d: %r" % (mkt, idx, exc))
                continue
            if not images:
                continue
            got = True
            for item in images:
                urlbase = str(item.get("urlbase") or "")
                fname = _filename(str(item.get("startdate") or ""), urlbase)
                merged.setdefault(_base_slug(fname), (fname, urlbase))
        if got:
            ok_markets += 1

    if not ok_markets:
        if last_err is not None:
            raise last_err
        return 0

    # 文件名以日期开头，倒序即「最新优先」；只处理窗口内的 keep 张
    items = sorted(merged.values(), reverse=True)[:keep]
    target = wallpaper_dir()
    downloaded = 0
    for fname, urlbase in items:
        path = os.path.join(target, fname)
        if os.path.isfile(path) and os.path.getsize(path) > _MIN_JPEG_BYTES:
            continue
        data = _download_image(urlbase)
        if data is None:
            log.log("wall.fetch.skip", fname)
            continue
        try:
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            downloaded += 1
            log.log("wall.fetch.ok", fname)
        except Exception as exc:
            log.log("wall.fetch.error", "%s: %r" % (fname, exc))

    deduped = dedupe_existing(folder=target)
    removed = cleanup_old(keep, folder=target)

    try:
        with open(os.path.join(target, _META_NAME), "w", encoding="utf-8") as f:
            json.dump({"last_check": time.time(), "days": days,
                       "markets": ok_markets, "keep": keep,
                       "downloaded": downloaded, "deduped": deduped,
                       "removed": removed},
                      f, ensure_ascii=False)
    except Exception:
        pass
    log.log("wall.fetch.done",
            f"markets={ok_markets} new={downloaded} "
            f"deduped={deduped} removed={removed}")
    return downloaded


def dedupe_existing(folder: str | None = None) -> int:
    """按图清理磁盘重复：同一 slug 只留一张，返回删除数。

    保留策略：日期最早者优先，同日优先保留 ZH-CN（国内市场命名）。
    只处理 bing_*.jpg，不碰 meta.json 和用户自放的其它图片。
    """
    folder = folder or wallpaper_dir()
    try:
        files = [n for n in os.listdir(folder)
                 if n.startswith("bing_") and n.lower().endswith(".jpg")]
    except Exception as exc:
        log.log("wall.dedupe.error", repr(exc))
        return 0

    def sort_key(n: str):
        date = n[5:13] if len(n) >= 13 else "99999999"
        return (date, 0 if "_ZH-CN" in n else 1, n)

    kept: set = set()
    removed = 0
    for name in sorted(files, key=sort_key):
        slug = _base_slug(name)
        if slug in kept:
            try:
                os.remove(os.path.join(folder, name))
                removed += 1
                log.log("wall.dedupe", name)
            except Exception as exc:
                log.log("wall.dedupe.error", "%s: %r" % (name, exc))
        else:
            kept.add(slug)
    if removed:
        log.log("wall.dedupe.done", f"removed={removed}")
    return removed


def cleanup_old(keep: int, folder: str | None = None) -> int:
    """滚动清理：只保留最新 keep 张 bing_*.jpg，删除更旧的，返回删除数。

    文件名以 startdate 开头，按名称排序即按时间排序。只删 bing_*.jpg，
    不碰 meta.json 和用户自放的其它图片。
    """
    keep = max(1, int(keep or _DEFAULT_KEEP))
    folder = folder or wallpaper_dir()
    try:
        files = [n for n in os.listdir(folder)
                 if n.startswith("bing_") and n.lower().endswith(".jpg")]
    except Exception as exc:
        log.log("wall.cleanup.error", repr(exc))
        return 0
    files.sort()
    removed = 0
    for name in files[:-keep] if len(files) > keep else []:
        try:
            os.remove(os.path.join(folder, name))
            removed += 1
            log.log("wall.cleanup", name)
        except Exception as exc:
            log.log("wall.cleanup.error", "%s: %r" % (name, exc))
    if removed:
        log.log("wall.cleanup.done", f"removed={removed} keep={keep}")
    return removed


def fetch_async(days: int = 8, keep: int = _DEFAULT_KEEP,
                on_done=None) -> threading.Thread:
    """后台线程拉取；on_done(new_count) 在工作线程回调，调用方自行切线程。"""

    def work():
        try:
            n = fetch_online(days=days, keep=keep)
        except Exception as exc:
            log.log("wall.fetch.fail", repr(exc))
            n = 0
        if on_done:
            try:
                on_done(n)
            except Exception as exc:
                log.log("wall.fetch.cb_error", repr(exc))

    t = threading.Thread(target=work, daemon=True, name="wallpaper-fetch")
    t.start()
    return t
