"""歌曲库持久化：SQLite 存储与检索。

结构见 vcpedia_schema.py：底层是规范化实体（song / artist / singer / lyrics /
tag / ...），对上暴露一个名为 ``songs`` 的**兼容视图**（列名与顺序复刻旧版 21
列单表），因此调用方无感。

每次操作开新连接，避免后台同步线程与主线程共享连接（sqlite3 默认
check_same_thread=True，跨线程复用会抛 ProgrammingError）。

写入一律走本模块的显式方法，不经过视图：视图只读 —— 若让 ``UPDATE songs``
反写关系表，重建 song_singer 会把单独存放的 engine 抹掉。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from vcpedia_schema import (
    CONTENT_ROLE_CODES,
    EMOTION_SEPARATOR,
    ROLE_DEFS,
    TAG_KIND_CATEGORY,
    TAG_KIND_EMOTION,
    ensure_schema,
    record_revision,
    safe_song_name,
    split_multi,
    write_annotation,
    write_credits,
    write_lyrics,
    write_singers,
    write_tags,
)

__all__ = ["SongStore", "EMOTION_TAGS", "EMOTION_SEPARATOR", "safe_song_name"]

# 情绪标签白名单（固定 7 个，见 annotate_emotions.py 的标注 prompt）
EMOTION_TAGS = ("甜美", "温柔", "积极", "帅气", "搞怪", "伤感", "愤怒")

#: 视图里主歌词的筛选条件（language='zh' 且非翻译），多处复用。
_MAIN_LYRICS_JOIN = (
    "JOIN lyrics l ON l.song_id = s.id AND l.language = 'zh' AND l.is_translated = 0"
)
_HAS_LYRICS = "TRIM(l.plain_text) != ''"
_NO_EMOTION = (
    "NOT EXISTS (SELECT 1 FROM song_tag st JOIN tag t ON t.id = st.tag_id "
    "WHERE st.song_id = s.id AND t.kind = 'emotion')"
)
_EMOTION_JOIN = (
    "JOIN song_tag st ON st.song_id = s.id "
    "JOIN tag t ON t.id = st.tag_id AND t.kind = 'emotion'"
)
_SINGERS_SUBQUERY = (
    "COALESCE((SELECT group_concat(si.name, '、' ORDER BY ss.position, ss.id) "
    "FROM song_singer ss JOIN singer si ON si.id = ss.singer_id "
    "WHERE ss.song_id = s.id AND si.deleted_at IS NULL), '')"
)
_EMOTION_SUBQUERY = (
    # song_tag 是无 id 列的联结表（主键 song_id+tag_id），排序只能用 t.id 兜底
    "COALESCE((SELECT group_concat(t.name, '|' ORDER BY st.position, t.id) "
    "FROM song_tag st JOIN tag t ON t.id = st.tag_id "
    "WHERE st.song_id = s.id AND t.kind = 'emotion'), '')"
)


def _escape_like(text: str) -> str:
    """转义 LIKE 通配符，配合 SQL 里的 ESCAPE '\\' 使用。

    搜索关键词直接来自聊天消息：不转义时 `%` 匹配任意串、`_` 匹配任意单字，
    「/歌词 搜索 %」会一次捞出整个曲库。
    """
    return (
        str(text or "")
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _coerce_year(value: Any) -> Optional[int]:
    """年份归一：接受 int 与纯数字字符串，其余为 None（与旧版行为一致）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


class SongStore:
    """歌曲库读写。

    首次打开旧库（``songs`` 还是实体表）时会自动迁移到规范化结构，旧表改名为
    ``songs_legacy_v1`` 保留；迁移统计放在 :attr:`migration_stats`。
    """

    EMOTION_SEPARATOR = EMOTION_SEPARATOR

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.migration_stats: Optional[dict] = None
        with self._connect() as conn:
            self.migration_stats = ensure_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _resolve_song_id(self, conn: sqlite3.Connection, name: str) -> Optional[int]:
        """按歌名或归一化歌名定位歌曲 id（与 get() 的匹配顺序一致）。"""
        row = conn.execute(
            "SELECT id FROM song WHERE (name = ? OR safe_name = ?) AND deleted_at IS NULL "
            "ORDER BY id LIMIT 1",
            (name, safe_song_name(name)),
        ).fetchone()
        return int(row["id"]) if row else None

    # ── 写入 ────────────────────────────────────────────────────

    def upsert(self, record: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> bool:
        """写入/更新一首歌，返回 True 表示新增（False 表示已存在并更新）。

        传入 conn 时复用调用方的连接（批量同步用，见 open_writer），仍逐首提交，
        与逐次开连接的语义一致；不传则自开连接（默认）。

        与旧版语义一致：9 个创作者字段与歌姬每次整体覆盖（record 里缺失即视为
        清空），分类整体覆盖；情绪标签与 lyrics_checked_at 不受影响。
        """
        name = str(record.get("name") or "").strip()
        if not name:
            return False
        safe = safe_song_name(name)
        if not safe:
            return False
        if conn is not None:
            result = self._write_song(conn, record, name, safe)
            conn.commit()
            return result
        with self._connect() as own:
            return self._write_song(own, record, name, safe)

    @staticmethod
    def _write_song(
        conn: sqlite3.Connection, record: Dict[str, Any], name: str, safe: str
    ) -> bool:
        """把一条记录落到规范化表，返回 True 表示新增。"""
        now = time.time()
        year = _coerce_year(record.get("year"))
        introduction = str(record.get("introduction") or "")
        lyrics = str(record.get("lyrics") or "")
        categories = split_multi(record.get("categories"))
        credits = {code: record.get(code) for code in CONTENT_ROLE_CODES}

        row = conn.execute(
            "SELECT id, name, introduction, year FROM song WHERE safe_name = ?", (safe,)
        ).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO song (name, safe_name, year, introduction, fetched_at, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, safe, year, introduction, now, now, now),
            )
            song_id = int(cur.lastrowid)
            created = True
        else:
            song_id = int(row["id"])
            created = False
            record_revision(conn, song_id, "name", row["name"], name, now)
            record_revision(conn, song_id, "year", row["year"], year, now)
            record_revision(conn, song_id, "introduction", row["introduction"], introduction, now)
            conn.execute(
                "UPDATE song SET name = ?, year = ?, introduction = ?, fetched_at = ?, "
                "updated_at = ? WHERE id = ?",
                (name, year, introduction, now, now, song_id),
            )

        write_credits(conn, song_id, credits, now)
        write_singers(conn, song_id, record.get("singers"), now)
        # 歌词改动的可追溯性由 lyrics.updated_at 承载，不进 song_revision：
        # 正文字段体积大，逐版存快照会让库迅速膨胀。
        write_lyrics(conn, song_id, lyrics, now, source="vcpedia")
        write_tags(conn, song_id, TAG_KIND_CATEGORY, categories, now)
        return created

    def open_writer(self) -> sqlite3.Connection:
        """给批量同步复用的写入连接（调用方负责 close）。

        同步几千首时避免逐首 connect/close；提交仍在 upsert 内逐首进行，
        /歌词 状态 里 count() 依旧能实时看到增长。
        """
        return self._connect()

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
                "UPDATE song SET lyrics_checked_at = ? WHERE safe_name = ?", keys
            )
            return int(cur.rowcount)

    # ── 查询 ────────────────────────────────────────────────────

    def count(self) -> int:
        with self._connect() as conn:
            return int(
                conn.execute("SELECT COUNT(*) FROM song WHERE deleted_at IS NULL").fetchone()[0]
            )

    def count_empty_lyrics(self, skip_checked_within: float = 0) -> int:
        """歌词为空的条目数（解析失败，或词条本来就没有歌词章节）。

        解析器修好后，用它可以估出「历史解析失败」的存量有多少。
        传 skip_checked_within 则只数「近期还没确认过」的待补条目。
        """
        where, params = self._empty_lyrics_where(skip_checked_within)
        with self._connect() as conn:
            return int(
                conn.execute(f"SELECT COUNT(*) FROM song s WHERE {where}", params).fetchone()[0]
            )

    def empty_lyric_names(self, limit: int = 50, skip_checked_within: float = 0) -> List[str]:
        """取出待补的歌词为空条目名（按 id 升序），供批量重抓回填。

        limit<=0 视为不限制（慎用，可能上千首）。
        skip_checked_within>0 时跳过「多少秒内已确认无歌词」的条目：批量补歌词
        会给它们打上 lyrics_checked_at，否则它们永远排在队首、每批都被重抓。
        """
        where, params = self._empty_lyrics_where(skip_checked_within)
        sql = f"SELECT s.name FROM song s WHERE {where} ORDER BY s.id"
        if limit and limit > 0:
            sql += " LIMIT ?"
            params = [*params, limit]
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [str(r["name"]) for r in rows]

    @staticmethod
    def _empty_lyrics_where(skip_checked_within: float) -> tuple[str, list]:
        """「没有主歌词」的 WHERE 片段（别名固定为 s = song）。"""
        sql = (
            "NOT EXISTS (SELECT 1 FROM lyrics l WHERE l.song_id = s.id "
            "AND l.language = 'zh' AND l.is_translated = 0 AND TRIM(l.plain_text) != '')"
        )
        sql = f"({sql}) AND s.deleted_at IS NULL"
        if skip_checked_within > 0:
            return f"{sql} AND s.lyrics_checked_at < ?", [time.time() - skip_checked_within]
        return sql, []

    def _empty_lyrics_clause(self, skip_checked_within: float) -> tuple[str, list]:
        """兼容旧签名：返回可直接用于 songs 视图的 WHERE 片段。"""
        sql = "lyrics IS NULL OR TRIM(lyrics) = ''"
        if skip_checked_within > 0:
            return f"({sql}) AND lyrics_checked_at < ?", [time.time() - skip_checked_within]
        return f"({sql})", []

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
        # 关键词来自用户输入，% 和 _ 在 LIKE 里是通配符：不转义时
        # 「/歌词 搜索 %」会匹配到全库任何一条记录。
        like = f"%{_escape_like(kw)}%"
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM songs WHERE name LIKE ? ESCAPE '\\' "
                "OR singers LIKE ? ESCAPE '\\' OR uploader LIKE ? ESCAPE '\\' "
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
                "SELECT * FROM songs WHERE lyrics LIKE ? ESCAPE '\\' "
                "ORDER BY LENGTH(name) LIMIT ?",
                (f"%{_escape_like(kw)}%", limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── 关系型查询（规范化之后才成立的能力）────────────────────

    def credits_of(self, name: str) -> Dict[str, List[str]]:
        """取某首歌的创作者分工，返回 {角色 code: [人名]}（只含非空角色）。"""
        with self._connect() as conn:
            song_id = self._resolve_song_id(conn, name)
            if song_id is None:
                return {}
            rows = conn.execute(
                "SELECT r.code AS code, a.name AS name FROM song_credit sc "
                "JOIN artist a ON a.id = sc.artist_id "
                "JOIN credit_role r ON r.id = sc.role_id "
                "WHERE sc.song_id = ? AND a.deleted_at IS NULL "
                "ORDER BY r.sort_order, sc.position, sc.id",
                (song_id,),
            ).fetchall()
        result: Dict[str, List[str]] = {}
        for row in rows:
            result.setdefault(str(row["code"]), []).append(str(row["name"]))
        return result

    def singers_of(self, name: str) -> List[Dict[str, Optional[str]]]:
        """取某首歌的演唱者，含各自引擎/声库（未知则为 None）。"""
        with self._connect() as conn:
            song_id = self._resolve_song_id(conn, name)
            if song_id is None:
                return []
            rows = conn.execute(
                "SELECT si.name AS name, ss.engine AS engine, ss.voicebank AS voicebank "
                "FROM song_singer ss JOIN singer si ON si.id = ss.singer_id "
                "WHERE ss.song_id = ? AND si.deleted_at IS NULL "
                "ORDER BY ss.position, ss.id",
                (song_id,),
            ).fetchall()
        return [
            {"name": str(r["name"]), "engine": r["engine"], "voicebank": r["voicebank"]}
            for r in rows
        ]

    def tags_of(self, name: str, kind: str = TAG_KIND_CATEGORY) -> List[str]:
        """取某首歌的某类标签（默认分类），保序。"""
        with self._connect() as conn:
            song_id = self._resolve_song_id(conn, name)
            if song_id is None:
                return []
            rows = conn.execute(
                "SELECT t.name FROM song_tag st JOIN tag t ON t.id = st.tag_id "
                "WHERE st.song_id = ? AND t.kind = ? ORDER BY st.position, t.id",
                (song_id, kind),
            ).fetchall()
        return [str(r["name"]) for r in rows]

    def songs_by_artist(
        self, artist: str, role_code: Optional[str] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """反查某位创作者参与的歌曲；role_code 可限定角色（如 'composer'）。"""
        target = (artist or "").strip()
        if not target:
            return []
        sql = (
            "SELECT s.* FROM songs s WHERE s.id IN ("
            "SELECT sc.song_id FROM song_credit sc JOIN artist a ON a.id = sc.artist_id "
            "JOIN credit_role r ON r.id = sc.role_id "
            "WHERE (a.name = ? OR a.name LIKE ?)"
        )
        params: List[Any] = [target, f"%{_escape_like(target)}%"]
        if role_code:
            sql += " AND r.code = ?"
            params.append(role_code)
        sql += ") ORDER BY s.id LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def songs_by_singer(self, singer: str, limit: int = 50) -> List[Dict[str, Any]]:
        """反查某位歌姬演唱的歌曲。"""
        target = (singer or "").strip()
        if not target:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT s.* FROM songs s WHERE s.id IN ("
                "SELECT ss.song_id FROM song_singer ss JOIN singer si ON si.id = ss.singer_id "
                "WHERE si.name = ? OR si.name LIKE ?) ORDER BY s.id LIMIT ?",
                (target, f"%{_escape_like(target)}%", max(1, int(limit))),
            ).fetchall()
        return [dict(r) for r in rows]

    def role_breakdown(self) -> Dict[str, int]:
        """各角色有分工记录的歌曲数，用于体检数据完整度。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT r.code AS code, COUNT(DISTINCT sc.song_id) AS n FROM song_credit sc "
                "JOIN credit_role r ON r.id = sc.role_id GROUP BY r.code"
            ).fetchall()
            total = int(
                conn.execute("SELECT COUNT(*) FROM song WHERE deleted_at IS NULL").fetchone()[0]
            )
        result = {code: 0 for code, _, _ in ROLE_DEFS}
        for row in rows:
            result[str(row["code"])] = int(row["n"])
        result["_songs"] = total
        return result

    def stats(self) -> Dict[str, int]:
        """各实体表规模，供迁移前后对照与体检。"""
        tables = (
            "song", "artist", "singer", "song_credit", "song_singer",
            "lyrics", "tag", "song_tag", "song_external_link", "song_revision",
        )
        with self._connect() as conn:
            return {t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in tables}

    # ── 情绪标签（v2.5.0 氛围选歌）──────────────────────────────

    @staticmethod
    def parse_emotion(raw: str) -> List[str]:
        """把 emotion 列的管道分隔字符串拆成标签列表。"""
        return [t.strip() for t in str(raw or "").split(EMOTION_SEPARATOR) if t.strip()]

    @staticmethod
    def join_emotion(tags: Iterable[str]) -> str:
        """把标签列表合并为管道分隔字符串（去重、保序）。"""
        seen: List[str] = []
        for t in tags:
            t = str(t or "").strip()
            if t and t not in seen:
                seen.append(t)
        return EMOTION_SEPARATOR.join(seen)

    def search_by_emotion(self, tags: Iterable[str], limit: int = 200) -> List[Dict[str, Any]]:
        """按情绪标签查歌：任一标签命中即算匹配，命中标签数多者在前。

        并列时按 id 升序（与「稳定排序 + rowid 顺序」一致）。
        只取推荐要用的 4 列，不把整段歌词等大字段一起拉进内存。
        limit<=0 视为不限制。
        """
        wanted = [t for t in (str(x or "").strip() for x in tags) if t]
        if not wanted:
            return []
        placeholders = ",".join("?" * len(wanted))
        sql = (
            "SELECT s.name AS name, s.introduction AS introduction, "
            f"{_SINGERS_SUBQUERY} AS singers, {_EMOTION_SUBQUERY} AS emotion, "
            "COUNT(*) AS hits FROM song s "
            f"{_EMOTION_JOIN} {_MAIN_LYRICS_JOIN} "
            f"WHERE t.name IN ({placeholders}) AND {_HAS_LYRICS} "
            "GROUP BY s.id ORDER BY hits DESC, s.id"
        )
        params: List[Any] = list(wanted)
        cap = int(limit) if limit and int(limit) > 0 else 0
        if cap:
            sql += " LIMIT ?"
            params.append(cap)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "name": str(r["name"]),
                "singers": str(r["singers"] or ""),
                "emotion": str(r["emotion"] or ""),
                "introduction": str(r["introduction"] or ""),
            }
            for r in rows
        ]

    def all_annotated_songs(self, limit: int = 200) -> List[Dict[str, Any]]:
        """已标注歌曲全量（无标签过滤，供无匹配回退时随机选歌）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT s.name AS name, s.introduction AS introduction, "
                f"{_SINGERS_SUBQUERY} AS singers, {_EMOTION_SUBQUERY} AS emotion "
                f"FROM song s {_MAIN_LYRICS_JOIN} WHERE {_HAS_LYRICS} "
                "ORDER BY s.id LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [
            {
                "name": str(r["name"]),
                "singers": str(r["singers"] or ""),
                "emotion": str(r["emotion"] or ""),
                "introduction": str(r["introduction"] or ""),
            }
            for r in rows
        ]

    def mark_emotion(self, safe_name: str, tags: Iterable[str]) -> bool:
        """写入一首歌的情绪标签与标注时间戳。tags 为空视为清空（重标）。"""
        safe = safe_song_name(safe_name)
        if not safe:
            return False
        values = self.parse_emotion(self.join_emotion(tags))
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM song WHERE safe_name = ? AND deleted_at IS NULL", (safe,)
            ).fetchone()
            if row is None:
                return False
            song_id = int(row["id"])
            write_tags(conn, song_id, TAG_KIND_EMOTION, values, now)
            write_annotation(conn, song_id, now if values else 0.0)
            return True

    def set_emotion(self, name: str, tags: Iterable[str]) -> bool:
        """按歌名（或归一化歌名）设置情绪标签，供命令行维护用。"""
        values = self.parse_emotion(self.join_emotion(tags))
        now = time.time()
        with self._connect() as conn:
            song_id = self._resolve_song_id(conn, name)
            if song_id is None:
                return False
            write_tags(conn, song_id, TAG_KIND_EMOTION, values, now)
            write_annotation(conn, song_id, now if values else 0.0)
        return True

    def clear_emotion(self, name: str) -> bool:
        """清空某首歌的情绪标签（等价旧版 UPDATE songs SET emotion=''）。"""
        return self.set_emotion(name, [])

    def pending_emotions(
        self, limit: int = 50, newest_first: bool = False
    ) -> List[Dict[str, Any]]:
        """待标注队列：没有情绪标签且歌词非空（没歌词没法定整体情绪）。

        失败的歌不写标签，下轮仍会进队列，天然断点续跑。
        newest_first=True 时按 id 倒序（新歌优先）——同步后自动标注用它，
        保证刚爬到的歌先被标上；离线脚本保持默认的 id 正序，慢慢排空存量。
        """
        order = "s.id DESC" if newest_first else "s.id ASC"
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT s.name AS name, s.safe_name AS safe_name, "
                f"l.plain_text AS lyrics, {_SINGERS_SUBQUERY} AS singers "
                f"FROM song s {_MAIN_LYRICS_JOIN} "
                f"WHERE {_HAS_LYRICS} AND {_NO_EMOTION} "
                f"ORDER BY {order} LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def emotion_stats(self) -> Dict[str, int]:
        """情绪标签覆盖统计：总数 / 已标注 / 各标签命中数。"""
        with self._connect() as conn:
            stats: Dict[str, int] = {
                "total": int(
                    conn.execute("SELECT COUNT(*) FROM song WHERE deleted_at IS NULL").fetchone()[0]
                ),
                "annotated": int(
                    conn.execute(
                        "SELECT COUNT(DISTINCT st.song_id) FROM song_tag st "
                        "JOIN tag t ON t.id = st.tag_id WHERE t.kind = 'emotion'"
                    ).fetchone()[0]
                ),
            }
            for row in conn.execute(
                "SELECT t.name AS name, COUNT(*) AS n FROM song_tag st "
                "JOIN tag t ON t.id = st.tag_id WHERE t.kind = 'emotion' GROUP BY t.name"
            ):
                stats[str(row["name"])] = int(row["n"])
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
            return [
                str(r["name"])
                for r in conn.execute("SELECT name FROM song WHERE deleted_at IS NULL")
            ]
