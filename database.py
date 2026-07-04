"""
SQLite 数据库操作
- 建表、增删改查
- 去重
- 同专辑兜底填充
"""

import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent / "music.db"


def get_conn():
    """获取数据库连接"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """初始化数据库表"""
    conn = get_conn()
    c = conn.cursor()

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS songs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            songmid    TEXT UNIQUE NOT NULL,
            title      TEXT DEFAULT '',
            artist     TEXT DEFAULT '',
            album      TEXT DEFAULT '',
            albummid   TEXT DEFAULT '',
            media_mid  TEXT DEFAULT '',
            duration   INTEGER DEFAULT 0,
            release_date TEXT DEFAULT '',
            play_url   TEXT DEFAULT '',
            cover_path TEXT DEFAULT '',
            file_path  TEXT DEFAULT '',
            file_size  INTEGER DEFAULT 0,
            play_count INTEGER DEFAULT 0,
            fav_count  INTEGER DEFAULT 0,
            download_time TEXT DEFAULT '',
            status     TEXT DEFAULT 'pending'
        )
        """
    )

    # 兼容旧表：如果缺少 media_mid 列则添加
    try:
        c.execute("ALTER TABLE songs ADD COLUMN media_mid TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # 列已存在

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS comments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            songmid      TEXT NOT NULL,
            nickname     TEXT DEFAULT '',
            content      TEXT DEFAULT '',
            like_count   INTEGER DEFAULT 0,
            comment_time TEXT DEFAULT ''
        )
        """
    )

    # 索引
    c.execute("CREATE INDEX IF NOT EXISTS idx_songs_songmid ON songs(songmid)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_songs_artist ON songs(artist)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_songs_album ON songs(album)")
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_comments_songmid ON comments(songmid)"
    )

    conn.commit()
    conn.close()


def song_exists(songmid):
    """检查歌曲是否已入库"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM songs WHERE songmid=?", (songmid,))
    count = c.fetchone()[0]
    conn.close()
    return count > 0


def insert_song(info):
    """插入或更新歌曲记录"""
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """
        INSERT OR REPLACE INTO songs
            (songmid, title, artist, album, albummid, media_mid, duration,
             release_date, play_url, cover_path, file_path, file_size,
             download_time, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            info.get("songmid", ""),
            info.get("title", ""),
            info.get("artist", ""),
            info.get("album", ""),
            info.get("albummid", ""),
            info.get("media_mid", ""),
            info.get("duration", 0),
            info.get("release_date", ""),
            info.get("play_url", ""),
            info.get("cover_path", ""),
            info.get("file_path", ""),
            info.get("file_size", 0),
            info.get("download_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            info.get("status", "done"),
        ),
    )
    conn.commit()
    conn.close()


def update_song_field(songmid, field, value):
    """更新歌曲单个字段"""
    conn = get_conn()
    c = conn.cursor()
    c.execute(f"UPDATE songs SET {field}=? WHERE songmid=?", (value, songmid))
    conn.commit()
    conn.close()


def get_song(songmid):
    """获取单首歌曲信息"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM songs WHERE songmid=?", (songmid,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_songs(artist=None):
    """获取所有歌曲，可按歌手筛选"""
    conn = get_conn()
    c = conn.cursor()
    if artist:
        c.execute(
            "SELECT * FROM songs WHERE artist=? ORDER BY release_date DESC",
            (artist,),
        )
    else:
        c.execute("SELECT * FROM songs ORDER BY artist, release_date DESC")
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_song_count():
    """歌曲总数"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM songs")
    count = c.fetchone()[0]
    conn.close()
    return count


def album_fallback():
    """
    同专辑兜底：用同专辑其他歌曲的 release_date / cover_path 填充空白记录
    返回填充数量
    """
    conn = get_conn()
    c = conn.cursor()

    filled_date = 0
    filled_cover = 0

    # 构建 album → date 映射
    c.execute(
        "SELECT DISTINCT album, release_date FROM songs "
        "WHERE release_date!='' AND album!=''"
    )
    album_dates = {row[0]: row[1] for row in c.fetchall()}

    # 构建 album → cover_path 映射
    c.execute(
        "SELECT DISTINCT album, cover_path FROM songs "
        "WHERE cover_path!='' AND album!=''"
    )
    album_covers = {row[0]: row[1] for row in c.fetchall()}

    # 填充空白日期
    for album, date in album_dates.items():
        c.execute(
            "UPDATE songs SET release_date=? WHERE album=? AND release_date=''",
            (date, album),
        )
        filled_date += c.rowcount

    # 填充空白封面
    for album, cover in album_covers.items():
        c.execute(
            "UPDATE songs SET cover_path=? WHERE album=? AND cover_path=''",
            (cover, album),
        )
        filled_cover += c.rowcount

    conn.commit()
    conn.close()
    return filled_date, filled_cover


def deduplicate():
    """
    同名歌曲去重：保留数据最全的那条（有日期 > 有专辑 > 有封面）
    返回删除数量
    """
    conn = get_conn()
    c = conn.cursor()

    # 找所有同名歌曲组
    c.execute(
        """
        SELECT title, GROUP_CONCAT(id) as ids, COUNT(*) as cnt
        FROM songs
        GROUP BY title
        HAVING cnt > 1
        """
    )
    dup_groups = c.fetchall()

    deleted = 0
    for title, ids_str, _ in dup_groups:
        ids = [int(x) for x in ids_str.split(",")]
        # 排序：数据最全的排最后
        c.execute(
            """
            SELECT id FROM songs WHERE id IN ({})
            ORDER BY
                CASE WHEN release_date!='' THEN 1 ELSE 0 END,
                CASE WHEN album!=''     THEN 1 ELSE 0 END,
                CASE WHEN cover_path!='' THEN 1 ELSE 0 END,
                CASE WHEN file_path!=''  THEN 1 ELSE 0 END
            """.format(
                ",".join(["?"] * len(ids))
            ),
            ids,
        )
        ordered = [r[0] for r in c.fetchall()]
        keep = ordered[-1]  # 保留最好的
        remove = [i for i in ordered[:-1]]

        if remove:
            c.execute(
                "DELETE FROM songs WHERE id IN ({})".format(
                    ",".join(["?"] * len(remove))
                ),
                remove,
            )
            deleted += c.rowcount

    conn.commit()
    conn.close()
    return deleted


def get_stats():
    """获取统计信息"""
    conn = get_conn()
    c = conn.cursor()

    stats = {}
    c.execute("SELECT COUNT(*) FROM songs")
    stats["total"] = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM songs WHERE file_path!=''")
    stats["downloaded"] = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM songs WHERE release_date!=''")
    stats["with_date"] = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM songs WHERE cover_path!=''")
    stats["with_cover"] = c.fetchone()[0]

    c.execute("SELECT COUNT(DISTINCT artist) FROM songs")
    stats["artist_count"] = c.fetchone()[0]

    c.execute("SELECT COUNT(DISTINCT album) FROM songs WHERE album!=''")
    stats["album_count"] = c.fetchone()[0]

    conn.close()
    return stats


if __name__ == "__main__":
    init_db()
    stats = get_stats()
    print("数据库统计:")
    for k, v in stats.items():
        print(f"  {k}: {v}")
