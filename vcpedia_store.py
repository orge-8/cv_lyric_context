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
    fetched_at   REAL NOT NULL DEFAULT 0
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
                [*values[2:], safe],
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
