"""
QQ音乐 API 封装
- 搜索 API → 获取歌曲列表 + 元数据（time_public、albummid等）
- Vkey API → 获取播放链接（vkey.GetVkeyServer）
- 封面 CDN 下载
"""

import re
import json
import time
import uuid
import random
import requests
from pathlib import Path
from collections import deque

# ============================================================
# 常量
# ============================================================

MUSICU_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
COVER_CDN = "https://y.gtimg.cn/music/photo_new/T002R{size}M000{albummid}.jpg"
COVER_SIZES = ["500x500", "300x300"]

# User-Agent 池 —— 随机轮换，降低指纹一致性
UA_POOL = [
    # Chrome 120-126, Windows 10/11
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    # Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    # Chrome macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]

# Referer 池
REFERER_POOL = [
    "https://y.qq.com/",
    "https://y.qq.com/n/ryqq/player",
    "https://y.qq.com/n/ryqq/search",
    "https://i.y.qq.com/",
]

# ============================================================
# 反爬：请求节奏控制
# ============================================================

class RateLimiter:
    """请求速率控制器 —— 基于滑动窗口"""

    def __init__(self, max_requests=15, window_seconds=60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._timestamps = deque()

    def wait_if_needed(self):
        """如果近期请求过密，等待直到安全"""
        now = time.time()
        # 清理过期时间戳
        while self._timestamps and self._timestamps[0] < now - self.window_seconds:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.max_requests:
            sleep_for = self._timestamps[0] + self.window_seconds - now + random.uniform(0.5, 2.0)
            if sleep_for > 0:
                time.sleep(sleep_for)
                # 递归清理
                return self.wait_if_needed()
        self._timestamps.append(now)

# 全局限速器
_rate_limiter = RateLimiter(max_requests=15, window_seconds=60)


def jitter(base, spread=0.5):
    """在 base ± spread% 范围内随机抖动"""
    return base * (1 + random.uniform(-spread, spread))


def random_ua():
    """随机 User-Agent"""
    return random.choice(UA_POOL)


def random_referer():
    """随机 Referer"""
    return random.choice(REFERER_POOL)


# ============================================================
# Cookie 管理
# ============================================================

_raw_cookie = ""
_uin = "0"
_g_tk = 0
_guid = ""


def load_cookies(cookie_path="cookies.txt"):
    """从文件读取 Cookie，同时解析 uin 并计算 g_tk"""
    global _raw_cookie, _uin, _g_tk, _guid

    path = Path(cookie_path)
    if not path.exists():
        return False

    _raw_cookie = path.read_text(encoding="utf-8").strip()
    if not _raw_cookie:
        return False

    # 解析 uin
    m = re.search(r"uin=([^;]+)", _raw_cookie)
    if m:
        _uin = m.group(1).replace("o", "").lstrip("0") or "0"

    # 计算 g_tk（基于 qqmusic_key 的 DJB hash）
    m = re.search(r"qqmusic_key=([^;]+)", _raw_cookie)
    if m:
        skey = m.group(1)
        h = 5381
        for ch in skey:
            h += (h << 5) + ord(ch)
        _g_tk = h & 0x7FFFFFFF

    # 生成 guid（32 位随机字符串）
    _guid = uuid.uuid4().hex

    return True


def _headers(extra=None):
    """构建请求头 —— 每次调用随机化 UA 和 Referer"""
    h = {
        "User-Agent": random_ua(),
        "Referer": random_referer(),
        "Origin": "https://y.qq.com",
    }
    if _raw_cookie:
        h["Cookie"] = _raw_cookie
    if extra:
        h.update(extra)
    return h


# ============================================================
# 反爬核心：带重试 & 退避的 POST
# ============================================================

def _api_post(body, timeout=10, retries=3):
    """
    请求 u.y.qq.com，自带：
    - 速率控制（全局滑动窗口）
    - 指数退避重试
    - 随机抖动延迟
    """
    last_err = None

    for attempt in range(retries):
        try:
            _rate_limiter.wait_if_needed()

            r = requests.post(
                MUSICU_URL,
                json=body,
                headers=_headers(),
                timeout=timeout,
            )
            r.raise_for_status()

            # 检查空响应
            if not r.text or not r.text.strip():
                raise ValueError("空响应，可能被反爬拦截")

            return r.json()

        except ValueError:
            last_err = "空响应"
            wait = (2 ** attempt) * random.uniform(5, 10)
            print(f"  [反爬] 收到空响应，疑似被封锁，等待 {wait:.0f}s...")
            time.sleep(wait)
            continue

        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            last_err = f"HTTP {status}"

            if status == 403:
                wait = (2 ** attempt) * random.uniform(3, 6)
                print(f"  [反爬] 收到 403，疑似被封锁，等待 {wait:.0f}s...")
                time.sleep(wait)
                continue
            elif status == 429:
                wait = (2 ** attempt) * random.uniform(5, 10)
                print(f"  [反爬] 触发频率限制 (429)，等待 {wait:.0f}s...")
                time.sleep(wait)
                continue
            elif status >= 500:
                wait = (2 ** attempt) * random.uniform(1, 3)
                time.sleep(wait)
                continue
            else:
                break

        except requests.RequestException as e:
            last_err = str(e)
            wait = (2 ** attempt) * random.uniform(1, 3)
            print(f"  [网络] 请求失败 ({e})，{wait:.1f}s 后重试...")
            time.sleep(wait)
            continue

    print(f"  [请求] 最终失败: {last_err}")
    return None


# ============================================================
# 搜索 API — 获取歌曲列表 + 元数据
# ============================================================

def search(keyword, page=1, num=50):
    """
    搜索歌曲，返回详细信息列表
    每条: {songmid, title, artist, album, albummid, release_date,
           media_mid, duration, pay_down, pay_status}
    """
    body = {
        "comm": {"ct": "19", "cv": "1859", "uin": "0"},
        "req": {
            "method": "DoSearchForQQMusicDesktop",
            "module": "music.search.SearchCgiService",
            "param": {
                "grp": 1,
                "num_per_page": num,
                "page_num": page,
                "query": keyword,
                "search_type": 0,
            },
        },
    }

    data = _api_post(body, timeout=10)
    if data is None:
        return []

    code = data.get("code", -1)
    if code == 104009:
        print("  [搜索] Cookie 已过期 (104009)，请重新登录并更新 cookies.txt")
        return []
    if code == 10000 or code == 10400:
        print(f"  [搜索] 可能被反爬拦截 (code={code})，建议等待后重试")
        return []
    if code != 0:
        print(f"  [搜索] API 返回错误码: {code}")
        return []

    results = []
    try:
        songs = (
            data.get("req", {})
            .get("data", {})
            .get("body", {})
            .get("song", {})
            .get("list", [])
        )
        for s in songs:
            singer_list = s.get("singer", [])
            artist = "、".join([si.get("name", "") for si in singer_list if si.get("name")])

            album = s.get("album", {})
            pay = s.get("pay", {})
            file_info = s.get("file", {})

            results.append({
                "songmid": s.get("mid", ""),
                "title": s.get("title", "") or s.get("name", ""),
                "artist": artist,
                "album": album.get("name", "") or s.get("albumname", ""),
                "albummid": album.get("mid", ""),
                "release_date": s.get("time_public", ""),
                "media_mid": file_info.get("media_mid", ""),
                "duration": s.get("interval", 0),
                "pay_down": pay.get("pay_down", 1),
                "pay_status": pay.get("pay_status", 0),
            })
    except Exception as e:
        print(f"  [搜索] 解析结果失败: {e}")

    return results


def search_all_pages(keyword, max_pages=20):
    """翻页搜索，去重后返回全量歌曲列表"""
    seen = set()
    all_results = []

    for page in range(1, max_pages + 1):
        print(f"  搜索第 {page} 页...", end=" ", flush=True)
        results = search(keyword, page=page)

        if not results:
            print("无结果，翻页结束")
            break

        new_count = 0
        for r in results:
            mid = r["songmid"]
            if mid and mid not in seen:
                seen.add(mid)
                all_results.append(r)
                new_count += 1

        print(f"{len(results)} 首 (新增 {new_count})")

        # 反爬：页面间随机延迟 2~5 秒
        delay = random.uniform(2.0, 5.0)
        time.sleep(delay)

    return all_results


# ============================================================
# Vkey API — 获取播放链接
# ============================================================

def get_play_url(songmid, media_mid=None):
    """
    获取歌曲的 M4A 播放链接
    返回: (play_url, filename) 或 (None, error_reason)
    """
    file_mid = media_mid if media_mid else songmid
    filename = f"C400{file_mid}.m4a"

    body = {
        "req_0": {
            "module": "vkey.GetVkeyServer",
            "method": "CgiGetVkey",
            "param": {
                "filename": [filename],
                "guid": _guid,
                "songmid": [songmid],
                "songtype": [0],
                "uin": _uin,
                "loginflag": 1 if _uin != "0" else 0,
                "platform": "20",
            },
        },
        "comm": {
            "uin": int(_uin) if _uin.isdigit() else 0,
            "format": "json",
            "ct": 19,
            "cv": 0,
        },
    }

    data = _api_post(body, timeout=10)
    if data is None:
        return None, "请求失败"

    req_data = data.get("req_0", {})

    if req_data.get("code") != 0:
        return None, f"Vkey API 错误: {req_data.get('code')}"

    vkey_data = req_data.get("data", {})
    midurlinfo = vkey_data.get("midurlinfo", [])

    if not midurlinfo:
        return None, "无播放信息"

    info = midurlinfo[0]
    purl = info.get("purl", "")
    if not purl:
        result_code = info.get("result", "?")
        err_map = {
            "104003": "版权限制/付费歌曲",
            "101404": "该音质不可用",
        }
        return None, err_map.get(str(result_code), f"error={result_code}")

    # 拼接完整播放链接
    sip = vkey_data.get("sip", ["http://aqqmusic.tc.qq.com/"])
    play_url = sip[0] + purl

    return play_url, filename


def get_play_urls_batch(song_list):
    """
    批量获取播放链接（单次请求最多约 20 首）
    song_list: [(songmid, media_mid), ...] 或 [songmid, ...]
    返回: {songmid: play_url}
    """
    if not song_list:
        return {}

    # 兼容旧格式：如果第一个元素是字符串，则都视为 songmid
    if isinstance(song_list[0], str):
        songmids = song_list
        media_mids = song_list  # 回退到 songmid
    else:
        songmids = [item[0] for item in song_list]
        media_mids = [item[1] if item[1] else item[0] for item in song_list]

    filenames = [f"C400{m}.m4a" for m in media_mids]
    # 限制单批最多 15 首，降低被封概率
    max_batch = 15
    if len(songmids) > max_batch:
        # 分批请求
        all_results = {}
        for i in range(0, len(songmids), max_batch):
            chunk = list(zip(
                songmids[i:i + max_batch],
                media_mids[i:i + max_batch]
            ))
            all_results.update(_do_vkey_batch(chunk))
            if i + max_batch < len(songmids):
                time.sleep(random.uniform(0.5, 1.5))
        return all_results

    return _do_vkey_batch(list(zip(songmids, media_mids)))


def _do_vkey_batch(pairs):
    """执行单批 Vkey 请求"""
    songmids = [p[0] for p in pairs]
    media_mids = [p[1] for p in pairs]
    filenames = [f"C400{m}.m4a" for m in media_mids]

    body = {
        "req_0": {
            "module": "vkey.GetVkeyServer",
            "method": "CgiGetVkey",
            "param": {
                "filename": filenames,
                "guid": _guid,
                "songmid": songmids,
                "songtype": [0] * len(songmids),
                "uin": _uin,
                "loginflag": 1 if _uin != "0" else 0,
                "platform": "20",
            },
        },
        "comm": {
            "uin": int(_uin) if _uin.isdigit() else 0,
            "format": "json",
            "ct": 19,
            "cv": 0,
        },
    }

    data = _api_post(body, timeout=15)
    if data is None:
        return {}

    req_data = data.get("req_0", {})
    if req_data.get("code") != 0:
        print(f"  [Vkey批量] API 错误: {req_data.get('code')}")
        return {}

    vkey_data = req_data.get("data", {})
    sip_list = vkey_data.get("sip", ["http://aqqmusic.tc.qq.com/"])
    sip = sip_list[0]
    midurlinfo = vkey_data.get("midurlinfo", [])

    results = {}
    for info in midurlinfo:
        mid = info.get("songmid", "")
        purl = info.get("purl", "")
        if mid and purl:
            results[mid] = sip + purl

    return results


# ============================================================
# 歌曲详情 API — 通过 songmid 获取元数据
# ============================================================

def get_song_detail(songmid):
    """
    通过 songmid 获取歌曲完整元数据
    返回: dict 或 None
    """
    body = {
        "comm": {"ct": 24, "cv": 0},
        "req_0": {
            "module": "music.pf_song_detail_svr",
            "method": "get_song_detail",
            "param": {"song_mid": songmid},
        },
    }

    data = _api_post(body, timeout=10)
    if data is None:
        return None

    req_data = data.get("req_0", {})
    if req_data.get("code") != 0:
        return None

    track = req_data.get("data", {}).get("track_info", {})
    if not track:
        return None

    singer_list = track.get("singer", [])
    artist = "、".join([s.get("name", "") for s in singer_list if s.get("name")])

    album = track.get("album", {})
    file_info = track.get("file", {})

    return {
        "songmid": track.get("mid", songmid),
        "title": track.get("title") or track.get("name", ""),
        "artist": artist,
        "album": album.get("name") or album.get("title", ""),
        "albummid": album.get("mid", ""),
        "release_date": track.get("time_public") or album.get("time_public", ""),
        "media_mid": file_info.get("media_mid", ""),
        "duration": track.get("interval", 0),
        "pay_down": 0,
        "pay_status": 0,
    }


# ============================================================
# 封面下载
# ============================================================

def download_cover(albummid, save_dir="covers"):
    """下载专辑封面，返回本地路径，失败返回空字符串"""
    if not albummid:
        return ""

    save_path = Path(save_dir)
    save_path.mkdir(parents=True, exist_ok=True)

    for size in COVER_SIZES:
        url = COVER_CDN.format(size=size, albummid=albummid)
        try:
            r = requests.get(url, stream=True, timeout=10,
                           headers={"User-Agent": random_ua(),
                                    "Referer": "https://y.qq.com/"})
            if r.status_code == 200 and len(r.content) > 2000:
                filepath = save_path / f"{albummid}.jpg"
                filepath.write_bytes(r.content)
                return str(filepath)
        except requests.RequestException:
            continue

    return ""


# ============================================================
# 快速测试
# ============================================================

if __name__ == "__main__":
    print("=== 测试搜索 ===")
    load_cookies()
    print(f"  uin={_uin}, g_tk={_g_tk}")

    results = search("周杰伦 晴天", page=1, num=3)
    for r in results:
        print(f"  {r['songmid']} | {r['title']} | {r['artist']}")

    if results:
        mid = results[0]["songmid"]
        print(f"\n=== 测试 Vkey: {mid} ===")
        url, info = get_play_url(mid)
        if url:
            print(f"  PLAY: {url[:100]}...")
        else:
            print(f"  FAIL: {info}")
