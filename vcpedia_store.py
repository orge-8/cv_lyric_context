"""歌曲库持久化：SQLite 存储与检索。

每次操作开新连接，避免后台同步线程与主线程共享连接（sqlite3 默认
check_same_thread=True，跨线程复用会抛 ProgrammingError）。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    safe_name    TEXT NOT NULL UNIQUE,
    uploader     TEXT NOT NULL DEFAULT '',
    singers      TEXT NOT NULL DEFAULT '',
    lyricist     TEXT NOT NULL DEFAULT '',
    composer     TEXT NOT NULL DEFAULT '',
    arranger     TEXT NOT NULL DEFAULT '',
    mixer        TEXT NOT NULL DEFAULT '',
    tuner        TEXT NOT NULL DEFAULT '',
    mastering    TEXT NOT NULL DEFAULT '',
    pv           TEXT NOT NULL DEFAULT '',
    illustrator  TEXT NOT NULL DEFAULT '',
    year         INTEGER,
    introduction TEXT NOT NULL DEFAULT '',
    lyrics       TEXT NOT NULL DEFAULT '',
    categories   TEXT NOT NULL DEFAULT '',
    emotion      TEXT NOT NULL DEFAULT '',
    emotion_annotated_at REAL NOT NULL DEFAULT 0,
    fetched_at   REAL NOT NULL DEFAULT 0,
    lyrics_checked_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_songs_name ON songs(name);
CREATE TABLE IF NOT EXISTS sync_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_CREDIT_FIELDS = (
    "uploader", "singers", "lyricist", "composer", "arranger",
    "mixer", "tuner", "mastering", "pv", "illustrator",
)

# 情绪标签白名单（固定 7 个，见 annotate_emotions.py 的标注 prompt）
EMOTION_TAGS = ("甜美", "温柔", "积极", "帅气", "搞怪", "伤感", "愤怒")


def safe_song_name(name: str) -> str:
    """歌名归一化键：只留字母数字与空格/连字符/下划线（中文 isalnum 为真）。"""
    return "".join(
        ch for ch in (name or "") if ch.isalnum() or ch in (" ", "-", "_")
    ).strip()


class SongStore:
    """歌曲库读写。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """给老库补列。

        CREATE TABLE IF NOT EXISTS 对已存在的表不做任何事，老库（v2.3.4 之前
        建的）没有 lyrics_checked_at，靠这里 ALTER 补上。
        v2.5.0 起补 emotion / emotion_annotated_at（情绪标签推荐）。
        """
        columns = {row[1] for row in conn.execute("PRAGMA table_info(songs)")}
        if "lyrics_checked_at" not in columns:
            conn.execute("ALTER TABLE songs ADD COLUMN lyrics_checked_at REAL NOT NULL DEFAULT 0")
        if "emotion" not in columns:
            conn.execute("ALTER TABLE songs ADD COLUMN emotion TEXT NOT NULL DEFAULT ''")
        if "emotion_annotated_at" not in columns:
            conn.execute("ALTER TABLE songs ADD COLUMN emotion_annotated_at REAL NOT NULL DEFAULT 0")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # ── 写入 ────────────────────────────────────────────────────

    def upsert(self, record: Dict[str, Any]) -> bool:
        """写入/更新一首歌，返回 True 表示新增（False 表示已存在并更新）。"""
        name = str(record.get("name") or "").strip()
        if not name:
            return False
        safe = safe_song_name(name)
        if not safe:
            return False
        year = record.get("year")
        year = int(year) if isinstance(year, int) or (isinstance(year, str) and year.isdigit()) else None
        values = [
            name, safe,
            *[str(record.get(f) or "") for f in _CREDIT_FIELDS],
            year,
            str(record.get("introduction") or ""),
            str(record.get("lyrics") or ""),
            str(record.get("categories") or ""),
            time.time(),
        ]
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM songs WHERE safe_name = ?", (safe,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO songs (name, safe_name, " + ", ".join(_CREDIT_FIELDS)
                    + ", year, introduction, lyrics, categories, fetched_at) "
                    "VALUES (?, ?, " + ", ".join("?" * len(_CREDIT_FIELDS))
                    + ", ?, ?, ?, ?, ?)", values,
                )
                return True
            conn.execute(
                "UPDATE songs SET name = ?, " + ", ".join(f"{f} = ?" for f in _CREDIT_FIELDS)
                + ", year = ?, introduction = ?, lyrics = ?, categories = ?, fetched_at = ? "
                "WHERE safe_name = ?",
                [name, *values[2:], safe],
            )
            return False

    def bulk_exists(self, safe_names: Iterable[str]) -> set[str]:
        """批量判断哪些归一化歌名已在库中。"""
        keys = [k for k in safe_names if k]
        if not keys:
            return set()
        found: set[str] = set()
        with self._connect() as conn:
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT safe_name FROM songs WHERE safe_name IN ({placeholders})", chunk
                ).fetchall()
                found.update(str(r["safe_name"]) for r in rows)
        return found

    # ── 查询 ────────────────────────────────────────────────────

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0])

    def count_empty_lyrics(self, skip_checked_within: float = 0) -> int:
        """歌词为空的条目数（解析失败，或词条本来就没有歌词章节）。

        解析器修好后，用它可以估出「历史解析失败」的存量有多少。
        传 skip_checked_within 则只数「近期还没确认过」的待补条目。
        """
        sql, params = self._empty_lyrics_clause(skip_checked_within)
        with self._connect() as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM songs WHERE {sql}", params).fetchone()[0])

    def empty_lyric_names(self, limit: int = 50, skip_checked_within: float = 0) -> List[str]:
        """取出待补的歌词为空条目名（按 id 升序），供批量重抓回填。

        limit<=0 视为不限制（慎用，可能上千首）。
        skip_checked_within>0 时跳过「多少秒内已确认无歌词」的条目：批量补歌词
        会给它们打上 lyrics_checked_at，否则它们永远排在队首、每批都被重抓。
        """
        where, params = self._empty_lyrics_clause(skip_checked_within)
        sql = f"SELECT name FROM songs WHERE {where} ORDER BY id"
        if limit and limit > 0:
            sql += " LIMIT ?"
            params = [*params, limit]
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [str(r["name"]) for r in rows]

    @staticmethod
    def _empty_lyrics_clause(skip_checked_within: float) -> tuple[str, list]:
        sql = "lyrics IS NULL OR TRIM(lyrics) = ''"
        if skip_checked_within > 0:
            return f"({sql}) AND lyrics_checked_at < ?", [time.time() - skip_checked_within]
        return f"({sql})", []

    def mark_lyrics_checked(self, names: Iterable[str], checked_at: Optional[float] = None) -> int:
        """给「已确认无歌词」的条目打时间戳，返回更新行数。

        只用于抓取成功、但确认没有歌词的情况；网络失败的不打，下次仍会重试。
        """
        stamp = time.time() if checked_at is None else checked_at
        keys = [(stamp, safe_song_name(str(n))) for n in names if str(n or "").strip()]
        keys = [(stamp, safe) for stamp, safe in keys if safe]
        if not keys:
            return 0
        with self._connect() as conn:
            cur = conn.executemany(
                "UPDATE songs SET lyrics_checked_at = ? WHERE safe_name = ?", keys
            )
            return int(cur.rowcount)

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        """按歌名精确查（先精确匹配，再归一化匹配）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM songs WHERE name = ? OR safe_name = ? ORDER BY id LIMIT 1",
                (name, safe_song_name(name)),
            ).fetchone()
        return dict(row) if row else None

    def search(self, keyword: str, limit: int = 10) -> List[Dict[str, Any]]:
        """按歌名/歌手/UP主模糊搜索。"""
        kw = (keyword or "").strip()
        if not kw:
            return []
        like = f"%{kw}%"
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM songs WHERE name LIKE ? OR singers LIKE ? OR uploader LIKE ? "
                "ORDER BY (name = ?) DESC, LENGTH(name) ASC, id LIMIT ?",
                (like, like, like, kw, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def search_lyrics(self, snippet: str, limit: int = 5) -> List[Dict[str, Any]]:
        """按歌词片段反查歌曲。"""
        kw = (snippet or "").strip()
        if not kw:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM songs WHERE lyrics LIKE ? ORDER BY LENGTH(name) LIMIT ?",
                (f"%{kw}%", limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── 情绪标签（v2.5.0 氛围选歌）──────────────────────────────

    EMOTION_SEPARATOR = "|"

    @staticmethod
    def parse_emotion(raw: str) -> List[str]:
        """把 emotion 列的管道分隔字符串拆成标签列表。"""
        return [t.strip() for t in str(raw or "").split("|") if t.strip()]

    @staticmethod
    def join_emotion(tags: Iterable[str]) -> str:
        """把标签列表合并为管道分隔字符串（去重、保序）。"""
        seen: List[str] = []
        for t in tags:
            t = str(t or "").strip()
            if t and t not in seen:
                seen.append(t)
        return SongStore.EMOTION_SEPARATOR.join(seen)

    def search_by_emotion(self, tags: Iterable[str], limit: int = 200) -> List[Dict[str, Any]]:
        """按情绪标签查歌：任一标签命中即算匹配，命中标签数多者在前。

        limit<=0 视为不限制。
        """
        wanted = [t for t in (str(x or "").strip() for x in tags) if t]
        if not wanted:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM songs WHERE emotion != '' AND lyrics != ''"
            ).fetchall()
        tagset = set(wanted)
        scored: List[tuple[int, Dict[str, Any]]] = []
        for r in rows:
            song = dict(r)
            hit = tagset & set(self.parse_emotion(song.get("emotion")))
            if hit:
                scored.append((len(hit), song))
        scored.sort(key=lambda x: x[0], reverse=True)
        result = [song for _, song in scored]
        if limit and int(limit) > 0:
            result = result[: int(limit)]
        return result

    def all_annotated_songs(self, limit: int = 200) -> List[Dict[str, Any]]:
        """已标注歌曲全量（无标签过滤，供无匹配回退时随机选歌）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM songs WHERE lyrics != '' ORDER BY id LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_emotion(self, safe_name: str, tags: Iterable[str]) -> bool:
        """写入一首歌的情绪标签与标注时间戳。tags 为空视为清空（重标）。"""
        safe = safe_song_name(safe_name)
        if not safe:
            return False
        joined = self.join_emotion(tags)
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE songs SET emotion = ?, emotion_annotated_at = ? WHERE safe_name = ?",
                (joined, time.time() if joined else 0.0, safe),
            )
            return int(cur.rowcount) > 0

    def pending_emotions(
        self, limit: int = 50, newest_first: bool = False
    ) -> List[Dict[str, Any]]:
        """待标注队列：emotion 为空且歌词非空（没歌词没法定整体情绪）。

        失败的歌不写 emotion，下轮仍会进队列，天然断点续跑。
        newest_first=True 时按 id 倒序（新歌优先）——同步后自动标注用它，
        保证刚爬到的歌先被标上；离线脚本保持默认的 id 正序，慢慢排空存量。
        """
        order = "id DESC" if newest_first else "id ASC"
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, safe_name, lyrics, singers FROM songs "
                "WHERE (emotion IS NULL OR emotion = '') AND TRIM(lyrics) != '' "
                "ORDER BY " + order + " LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def emotion_stats(self) -> Dict[str, int]:
        """情绪标签覆盖统计：总数 / 已标注 / 各标签命中数。"""
        stats = {"total": 0, "annotated": 0}
        with self._connect() as conn:
            stats["total"] = int(
                conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
            )
            stats["annotated"] = int(
                conn.execute(
                    "SELECT COUNT(*) FROM songs WHERE emotion != ''"
                ).fetchone()[0]
            )
            for row in conn.execute("SELECT emotion FROM songs WHERE emotion != ''"):
                for t in self.parse_emotion(row["emotion"]):
                    stats[t] = stats.get(t, 0) + 1
        return stats

    # ── 同步元信息 ──────────────────────────────────────────────

    def meta_get(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM sync_meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def meta_set(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sync_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def all_titles(self) -> List[str]:
        with self._connect() as conn:
            return [str(r["name"]) for r in conn.execute("SELECT name FROM songs")]
