"""
QQ 音乐爬虫 — 主入口
用法:
  python crawl_singer.py 周杰伦                    # 按歌手搜索并下载
  python crawl_singer.py "周杰伦 晴天"              # 按关键词搜索
  python crawl_singer.py 周杰伦 --no-download       # 只入库不下载
  python crawl_singer.py 周杰伦 --max-pages 5       # 最多搜5页
  python crawl_singer.py --fix-albums               # 修复：同专辑兜底 + 去重
  python crawl_singer.py --stats                    # 查看统计
  python crawl_singer.py --extract-durations        # 提取已下载MP3的时长
  python crawl_singer.py 周杰伦 --resume            # 断点续传：只下载未完成的
  python crawl_singer.py "  " --list --max-pages 1     #搜索songmid
  python crawl_singer.py --songmid 003aAPj81VWrbL      #指定下载歌曲
  python crawl_singer.py --songmids "004TXEXY2G2c7C,001dgMlv4CIpk9,000uqgxP2vAHfm"  #批量下载
  echo 004TXEXY2G2c7C > songmids.txt
  echo 001dgMlv4CIpk9 >> songmids.txt
  python crawl_singer.py --songmids-file songmids.txt   # 2. 保存到文件再批量下载（适合大量 songmid）
"""

import sys
import time
import json
import random
from pathlib import Path

# Windows 终端默认 GBK，改用 UTF-8 避免中文/特殊字符报错
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 将项目目录加入 Python Path
PROJECT_DIR = Path(__file__).parent
sys.path.insert(0, str(PROJECT_DIR))

import qqmusic_api as api
import database as db
from downloader import download_batch, extract_all_durations

# 进度文件路径
PROGRESS_FILE = PROJECT_DIR / ".crawl_progress.json"


def print_banner():
    print("=" * 60)
    print("  QQ 音乐爬虫  —  y.qq.com")
    print("  数据来源: 搜索API (元数据) + VkeyAPI (播放链接)")
    print("=" * 60)


def _load_progress():
    """加载上次中断的进度"""
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"downloaded": [], "failed_vkey": [], "failed_dl": []}


def _save_progress(progress):
    """保存进度到文件"""
    PROGRESS_FILE.write_text(
        json.dumps(progress, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def crawl_artist(keyword, max_pages=20, do_download=True, resume=False):
    """主流程：搜索 -> Vkey获取播放链接 -> 入库 -> 下载"""

    # 1. 搜索获取歌曲列表（含元数据）
    print(f"\n[1/3] 搜索关键词: {keyword}")
    search_results = api.search_all_pages(keyword, max_pages=max_pages)
    if not search_results:
        print("  未搜到任何歌曲，退出")
        return

    total = len(search_results)
    free_count = sum(1 for r in search_results if r["pay_down"] == 0)
    vip_count = total - free_count
    print(f"  共 {total} 首（免费 {free_count} 首, VIP {vip_count} 首）")

    # 加载进度
    progress = _load_progress() if resume else {"downloaded": [], "failed_vkey": [], "failed_dl": []}
    already_downloaded = set(progress.get("downloaded", []))

    # 2. 入库 + 下载封面
    print(f"\n[2/3] 入库 & 下载封面...")
    for i, s in enumerate(search_results):
        if db.song_exists(s["songmid"]):
            continue

        # 下载封面
        if s.get("albummid"):
            s["cover_path"] = api.download_cover(s["albummid"], "covers")
            # 封面间加小延迟
            time.sleep(random.uniform(0.3, 0.8))

        db.insert_song(s)

        if (i + 1) % 50 == 0:
            print(f"  已入库 {i + 1}/{total}", flush=True)

    print(f"  入库完成: {total} 首")

    # 同专辑兜底
    fd, fc = db.album_fallback()
    if fd or fc:
        print(f"  同专辑兜底: +{fd} 日期, +{fc} 封面")

    # 3. Vkey 获取播放链接 + 下载
    if do_download:
        print(f"\n[3/3] 获取播放链接 & 下载...")

        # 过滤掉已下载的
        if resume and already_downloaded:
            to_download = [
                s for s in search_results
                if s.get("songmid") and s["songmid"] not in already_downloaded
            ]
            print(f"  断点续传: 跳过已下载 {len(already_downloaded)} 首, 剩余 {len(to_download)} 首")
        else:
            to_download = [s for s in search_results if s.get("songmid")]

        if not to_download:
            print("  没有需要下载的歌曲")
            _save_progress(progress)
            return

        # 小批量处理：每批 2~3 首，批次间加随机延迟
        batch_size = random.choice([2, 3, 2, 3, 1])  # 偶尔只下1首，模拟人类行为
        total_downloaded = len(already_downloaded)
        total_failed_vkey = len(progress.get("failed_vkey", []))
        total_failed_dl = len(progress.get("failed_dl", []))

        for i in range(0, len(to_download), batch_size):
            batch = to_download[i : i + batch_size]

            # 获取这批的 vkey（使用 media_mid 构造文件名）
            song_list = [(s["songmid"], s.get("media_mid", s["songmid"])) for s in batch]
            url_map = api.get_play_urls_batch(song_list)
            hit = len(url_map)
            total_failed_vkey += (len(batch) - hit)

            # 立即下载这批
            download_list = []
            for s in batch:
                url = url_map.get(s["songmid"])
                if url:
                    download_list.append((s["songmid"], s["title"], s["artist"], url))

            if download_list:
                results = download_batch(download_list, headers_fn=api._headers)
                for mid, info in results.items():
                    if info.get("file_path"):
                        db.update_song_field(mid, "file_path", info["file_path"])
                        total_downloaded += 1
                        progress.setdefault("downloaded", []).append(mid)
                    else:
                        total_failed_dl += 1
                        progress.setdefault("failed_dl", []).append(mid)

            # 保存进度
            _save_progress(progress)

            # 进度显示
            progress_count = min(i + batch_size, len(to_download))
            print(f"  [{progress_count}/{len(to_download)}] "
                  f"已下载: {total_downloaded} | "
                  f"版权限制: {total_failed_vkey} | "
                  f"下载失败: {total_failed_dl}", flush=True)

            # 反爬：批次间随机延迟 3~8 秒
            if i + batch_size < len(to_download):
                delay = random.uniform(3.0, 8.0)
                time.sleep(delay)

        print(f"\n  完成: 下载 {total_downloaded} 首, "
              f"版权限制 {total_failed_vkey} 首, "
              f"失败 {total_failed_dl} 首")

        # 清理进度文件
        if PROGRESS_FILE.exists():
            PROGRESS_FILE.unlink()
    else:
        print(f"\n[3/3] 跳过下载 (--no-download)")

    # 输出统计
    stats = db.get_stats()
    print(f"\n数据库: {stats['total']} 首, 已下载 {stats['downloaded']} 首, "
          f"有日期 {stats['with_date']} 首")


def cmd_stats():
    """打印统计信息"""
    stats = db.get_stats()
    print(f"\n数据库统计 ({db.DB_PATH}):")
    print(f"  总歌曲:    {stats['total']}")
    print(f"  已下载:    {stats['downloaded']}")
    print(f"  有日期:    {stats['with_date']}")
    print(f"  有封面:    {stats['with_cover']}")
    print(f"  歌手数:    {stats['artist_count']}")
    print(f"  专辑数:    {stats['album_count']}")

    # 检查是否有中断的进度
    progress = _load_progress()
    if progress.get("downloaded"):
        print(f"\n  [!] 检测到未完成的下载进度 ({len(progress['downloaded'])} 首已下载)")
        print(f"       使用 --resume 断点续传")


def cmd_fix_albums():
    """修复：同专辑兜底 + 去重"""
    print("同专辑兜底...")
    fd, fc = db.album_fallback()
    print(f"  +{fd} 日期, +{fc} 封面")

    print("去重...")
    deleted = db.deduplicate()
    print(f"  删除 {deleted} 首重复歌曲")


def cmd_download_one(songmid):
    """下载单首歌曲（通过 songmid）"""
    return _download_single(songmid)


def _download_single(songmid):
    """下载单首歌曲的核心逻辑，返回 True/False"""
    existing = db.get_song(songmid)
    if existing and existing.get("file_path") and Path(existing["file_path"]).exists():
        print(f"  已存在: {existing['title']} — {existing['artist']}")
        return True

    info = api.get_song_detail(songmid)
    if not info:
        print(f"  [!] 无法获取歌曲信息: {songmid}")
        return False

    if info.get("albummid"):
        info["cover_path"] = api.download_cover(info["albummid"], "covers")
    db.insert_song(info)

    url, err = api.get_play_url(songmid, info.get("media_mid"))
    if not url:
        print(f"  [!] {info['title']} — {err}")
        return False

    results = download_batch(
        [(songmid, info["title"], info["artist"], url)],
        headers_fn=api._headers,
    )
    for mid, dl_info in results.items():
        if dl_info.get("file_path"):
            db.update_song_field(mid, "file_path", dl_info["file_path"])
            print(f"  OK: {info['title']} — {info['artist']}")
            return True
        else:
            print(f"  FAIL: {info['title']}")
            return False
    return False


def cmd_download_many(songmids):
    """批量下载多个 songmid"""
    print(f"\n批量下载 {len(songmids)} 首歌曲")
    print("=" * 50)

    success = 0
    fail = 0
    for i, mid in enumerate(songmids, 1):
        print(f"\n[{i}/{len(songmids)}] songmid={mid}")
        if _download_single(mid):
            success += 1
        else:
            fail += 1
        if i < len(songmids):
            delay = random.uniform(2.0, 5.0)
            time.sleep(delay)

    print(f"\n{'=' * 50}")
    print(f"完成: 成功 {success}, 失败 {fail}")
    stats = db.get_stats()
    print(f"数据库: {stats['total']} 首, 已下载 {stats['downloaded']} 首")


def cmd_list(keyword, max_pages=5):
    """快速搜索，只列出 songmid 不下载"""
    print(f"搜索: {keyword}")
    results = api.search_all_pages(keyword, max_pages=max_pages)
    free = sum(1 for r in results if r["pay_down"] == 0)
    vip = len(results) - free

    print(f"\n{'序号':<5} {'songmid':<18} {'免费/VIP':<8} {'歌名':<30} {'歌手':<15} {'专辑'}")
    print("-" * 100)
    for i, r in enumerate(results, 1):
        tag = "免费" if r["pay_down"] == 0 else "VIP"
        print(f"{i:<5} {r['songmid']:<18} {tag:<8} {r['title'][:28]:<30} {r['artist'][:13]:<15} {r['album'][:20]}")

    print(f"\n共 {len(results)} 首（免费 {free}, VIP {vip}）")
    print(f"\n# 下载单首:")
    print(f"  python crawl_singer.py --songmid <songmid>")
    print(f"\n# 批量下载（逗号分隔）:")
    songmid_csv = ",".join(r["songmid"] for r in results)
    print(f"  python crawl_singer.py --songmids \"{songmid_csv[:200]}{'...' if len(songmid_csv) > 200 else ''}\"")
    print(f"\n# 或保存到文件再批量下载:")
    print(f"  python crawl_singer.py --songmids-file songmids.txt")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="QQ音乐爬虫 — 搜索并下载歌曲",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python crawl_singer.py 周杰伦
  python crawl_singer.py 周杰伦 --no-download
  python crawl_singer.py "周杰伦 晴天"
  python crawl_singer.py 周杰伦 --resume
  python crawl_singer.py --stats
  python crawl_singer.py --fix-albums
        """,
    )
    parser.add_argument("keyword", nargs="?", help="搜索关键词（歌手名/歌名）")
    parser.add_argument(
        "--max-pages", type=int, default=20, help="最大搜索页数（默认20）"
    )
    parser.add_argument(
        "--no-download", action="store_true", help="只入库不下载 MP3"
    )
    parser.add_argument("--stats", action="store_true", help="查看数据库统计")
    parser.add_argument(
        "--fix-albums", action="store_true", help="同专辑兜底 + 去重"
    )
    parser.add_argument(
        "--extract-durations", action="store_true", help="为已下载歌曲提取时长"
    )
    parser.add_argument(
        "--cookie", default="cookies.txt", help="Cookie 文件路径"
    )
    parser.add_argument(
        "--resume", action="store_true", help="断点续传：跳过已下载的歌曲"
    )
    parser.add_argument(
        "--songmid", help="下载单首歌曲（通过 songmid）"
    )
    parser.add_argument(
        "--songmids", help="批量下载（逗号分隔的 songmid 列表，如: mid1,mid2,mid3）"
    )
    parser.add_argument(
        "--songmids-file", help="批量下载（文件，每行一个 songmid）"
    )
    parser.add_argument(
        "--list", action="store_true", help="仅搜索并列出 songmid，不下载"
    )

    args = parser.parse_args()

    # 初始化
    db.init_db()

    # 加载 Cookie
    if not api.load_cookies(args.cookie):
        print(f"[!] 未找到 Cookie 文件: {args.cookie}")
        print("    请先在 y.qq.com 登录，然后 F12 -> Application -> Cookies")
        print("    复制全部 Cookie 粘贴到 cookies.txt")
    else:
        print(f"[OK] Cookie 已加载 (uin={api._uin})")

    print_banner()

    # 执行命令
    if args.stats:
        cmd_stats()
    elif args.fix_albums:
        cmd_fix_albums()
    elif args.extract_durations:
        print("提取已下载歌曲时长...")
        count = extract_all_durations(db)
        print(f"  完成: {count} 首")
    elif args.keyword and args.list:
        cmd_list(args.keyword, max_pages=args.max_pages)
    elif args.keyword:
        crawl_artist(args.keyword, max_pages=args.max_pages,
                     do_download=not args.no_download,
                     resume=args.resume)
    elif args.songmid:
        cmd_download_one(args.songmid)
    elif args.songmids:
        songmids = [m.strip() for m in args.songmids.split(",") if m.strip()]
        cmd_download_many(songmids)
    elif args.songmids_file:
        path = Path(args.songmids_file)
        if not path.exists():
            print(f"[!] 文件不存在: {path}")
        else:
            # 尝试多种编码读取
            raw = None
            for enc in ["utf-8", "utf-16", "gbk", "utf-8-sig"]:
                try:
                    raw = path.read_text(encoding=enc)
                    break
                except (UnicodeDecodeError, UnicodeError):
                    continue
            if raw is None:
                print(f"[!] 无法读取文件，请保存为 UTF-8 编码")
                return

            songmids = []
            for line in raw.splitlines():
                # 去掉注释和首尾空白
                clean = line.split("#")[0].strip()
                if not clean:
                    continue
                # 只提取 14 位 songmid（大小写字母+数字）
                import re
                found = re.findall(r'[A-Za-z0-9]{14}', clean)
                songmids.extend(found)
            songmids = list(dict.fromkeys(songmids))  # 去重保序
            print(f"从文件读取 {len(songmids)} 个 songmid")
            cmd_download_many(songmids)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
