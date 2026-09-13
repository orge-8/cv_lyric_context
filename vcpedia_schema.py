"""歌曲库 Schema：借鉴 CVSA（中V档案馆）的关系模型，把「平铺字段」拆成实体与关系。

改造动机（对照旧版 21 列单表 songs）：

1. 创作者角色（UP主 / 作词 / 作曲 / 编曲 / 混音 / 调校 / 母带 / PV / 曲绘）原本是
   9 个并列文本列，本质是 (歌曲, 创作者, 角色) 三元关系。拆成 artist + credit_role
   + song_credit 之后才能表达「一人兼多角色」「一个角色多人」「角色未知」。
2. 歌姬原本只是一个分隔串，无法承载「哪位歌姬用了哪个声库 / 引擎」。拆成
   singer + song_singer，engine 与 voicebank 允许 NULL —— 只知道歌姬、不知道
   引擎是常态，这一点直接来自 CVSA 的 SingerOfSong 设计。
3. 歌词原本是 songs.lyrics 单列，只能存一种语言、一种版本。拆成 lyrics 表后同一
   首歌可挂多语言、原文与翻译并列，并预留 ttml（逐字时间轴）与 lrc。
4. 分类与情绪原本是两个字符串列（逗号串 / 管道串）。统一为 tag + song_tag，
   用 kind 区分语义、用 position 保序，分类还支持 parent_id 成树。
5. 补上 CVSA 的审计思路：created_at / updated_at / deleted_at 软删除，以及
   song_revision 记录元信息被重写时的前后值。

对外仍然提供一个名为 ``songs`` 的 **兼容视图**，列名与顺序与旧 21 列完全一致，
因此插件里已有的 ``SELECT * FROM songs`` / ``WHERE singers LIKE ?`` 等语句无需改动。

写入不经过视图（视图只读）：视图无法安全地反写关系表 —— 例如 ``UPDATE songs
SET emotion=...`` 若重建 song_singer，会把单独存放的 engine 抹掉。所有写入统一走
SongStore 的显式方法；需要命令行改数据时用 migrate_knowledge_db.py 的
``--set-emotion`` / ``--clear-emotion``。
"""

from __future__ import annotations

import re
import sqlite3
import time
from typing import Any, Iterable, List, Optional, Sequence, Tuple

# ── 角色字典 ────────────────────────────────────────────────────────
# 与旧 21 列里的 9 个非歌手 credit 列一一对应（顺序即展示顺序）。
ROLE_DEFS: Tuple[Tuple[str, str, int], ...] = (
    ("uploader", "UP主", 10),
    ("lyricist", "作词", 20),
    ("composer", "作曲", 30),
    ("arranger", "编曲", 40),
    ("mixer", "混音", 50),
    ("tuner", "调校", 60),
    ("mastering", "母带", 70),
    ("pv", "PV", 80),
    ("illustrator", "曲绘", 90),
)

#: credit 角色 code 列表（= 旧版除 singers 外的 credit 列）
CONTENT_ROLE_CODES: Tuple[str, ...] = tuple(code for code, _, _ in ROLE_DEFS)

TAG_KIND_CATEGORY = "category"
TAG_KIND_EMOTION = "emotion"

EMOTION_SEPARATOR = "|"

#: 多值字段分隔符：顿号、半/全角逗号、斜杠、分号、换行。
#: 实测数据里 singers 有 625 首用 \n、415 首用 、、还有 "洛天依\n、\n乐正绫"
#: 这种混合分隔符带空段，所以必须用 + 合并连续分隔符并丢弃空段。
_SPLIT_RE = re.compile(r"[、,，/;；\n\r]+")

#: 情绪标签串分隔符（旧字段是管道分隔）。
_EMOTION_SPLIT_RE = re.compile(r"[|]+")


def split_multi(text: Any) -> List[str]:
    """把「顿号/逗号/斜杠/分号/换行」分隔的多值字段拆成去重保序的列表。"""
    raw = str(text or "")
    if not raw.strip():
        return []
    out: List[str] = []
    for seg in _SPLIT_RE.split(raw):
        seg = seg.strip()
        if seg and seg not in out:
            out.append(seg)
    return out


def split_singers(text: Any) -> List[Tuple[str, Optional[str], Optional[str]]]:
    """拆歌姬字段，返回 [(歌姬名, 引擎, 声库)]。

    兼容 ``星尘#Synthesizer_V`` 这类「歌姬#引擎」写法：旧版 clean_credit 会把
    ``#`` 之后整段丢掉，这里改为保留到 engine 列，展示不受影响。
    实测现有 3412 首里没有带 ``#`` 的值，因此对历史数据是纯增益、无行为变化。
    """
    result: List[Tuple[str, Optional[str], Optional[str]]] = []
    for seg in split_multi(text):
        engine: Optional[str] = None
        voicebank: Optional[str] = None
        if "#" in seg:
            head, _, tail = seg.partition("#")
            seg = head.strip()
            tail = tail.strip().replace("_", " ")
            if tail:
                engine = tail
        if seg:
            result.append((seg, engine, voicebank))
    return result


def split_emotion(text: Any) -> List[str]:
    """拆情绪标签串（旧格式为管道分隔），去重保序。"""
    out: List[str] = []
    for seg in _EMOTION_SPLIT_RE.split(str(text or "")):
        seg = seg.strip()
        if seg and seg not in out:
            out.append(seg)
    return out


def safe_song_name(name: str) -> str:
    """歌名归一化键：只留字母数字与空格/连字符/下划线（中文 isalnum 为真）。"""
    return "".join(
        ch for ch in (name or "") if ch.isalnum() or ch in (" ", "-", "_")
    ).strip()


# ── DDL：规范化表 ───────────────────────────────────────────────────

DDL_TABLES = """
CREATE TABLE IF NOT EXISTS song (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL,
    safe_name         TEXT    NOT NULL,
    type              TEXT,
    original_song_id  INTEGER REFERENCES song(id) ON DELETE SET NULL,
    duration          INTEGER,
    year              INTEGER,
    cover_url         TEXT    NOT NULL DEFAULT '',
    bilibili_aid      INTEGER,
    bilibili_bvid     TEXT,
    published_at      TEXT,
    introduction      TEXT    NOT NULL DEFAULT '',
    localized_names   TEXT    NOT NULL DEFAULT '',
    fetched_at        REAL    NOT NULL DEFAULT 0,
    lyrics_checked_at REAL    NOT NULL DEFAULT 0,
    created_at        REAL    NOT NULL DEFAULT 0,
    updated_at        REAL    NOT NULL DEFAULT 0,
    deleted_at        REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_song_safe_name ON song(safe_name);
CREATE INDEX IF NOT EXISTS idx_song_name ON song(name);

CREATE TABLE IF NOT EXISTS credit_role (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL,
    name_zh    TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_credit_role_code ON credit_role(code);

CREATE TABLE IF NOT EXISTS artist (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    aliases    TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    deleted_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_artist_name ON artist(name);

CREATE TABLE IF NOT EXISTS song_credit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    song_id    INTEGER NOT NULL REFERENCES song(id) ON DELETE CASCADE,
    artist_id  INTEGER NOT NULL REFERENCES artist(id),
    role_id    INTEGER NOT NULL REFERENCES credit_role(id),
    position   INTEGER NOT NULL DEFAULT 0,
    created_at REAL    NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_song_credit ON song_credit(song_id, artist_id, role_id);
CREATE INDEX IF NOT EXISTS idx_song_credit_song ON song_credit(song_id);
CREATE INDEX IF NOT EXISTS idx_song_credit_artist ON song_credit(artist_id, role_id);

CREATE TABLE IF NOT EXISTS singer (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    aliases    TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    deleted_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_singer_name ON singer(name);

CREATE TABLE IF NOT EXISTS song_singer (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    song_id    INTEGER NOT NULL REFERENCES song(id) ON DELETE CASCADE,
    singer_id  INTEGER NOT NULL REFERENCES singer(id),
    engine     TEXT,
    voicebank  TEXT,
    position   INTEGER NOT NULL DEFAULT 0,
    created_at REAL    NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_song_singer ON song_singer(song_id, singer_id);
CREATE INDEX IF NOT EXISTS idx_song_singer_song ON song_singer(song_id);
CREATE INDEX IF NOT EXISTS idx_song_singer_singer ON song_singer(singer_id);

CREATE TABLE IF NOT EXISTS lyrics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    song_id       INTEGER NOT NULL REFERENCES song(id) ON DELETE CASCADE,
    language      TEXT    NOT NULL DEFAULT 'zh',
    is_translated INTEGER NOT NULL DEFAULT 0,
    plain_text    TEXT    NOT NULL DEFAULT '',
    ttml          TEXT,
    lrc           TEXT,
    source        TEXT    NOT NULL DEFAULT '',
    created_at    REAL    NOT NULL DEFAULT 0,
    updated_at    REAL    NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_lyrics ON lyrics(song_id, language, is_translated);
CREATE INDEX IF NOT EXISTS idx_lyrics_song ON lyrics(song_id);

CREATE TABLE IF NOT EXISTS tag (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL DEFAULT 'category',
    name       TEXT NOT NULL,
    parent_id  INTEGER REFERENCES tag(id) ON DELETE SET NULL,
    created_at REAL NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_tag_kind_name ON tag(kind, name);

CREATE TABLE IF NOT EXISTS song_tag (
    song_id  INTEGER NOT NULL REFERENCES song(id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (song_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_song_tag_tag ON song_tag(tag_id);

CREATE TABLE IF NOT EXISTS song_external_link (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    song_id     INTEGER NOT NULL REFERENCES song(id) ON DELETE CASCADE,
    platform    TEXT NOT NULL,
    url         TEXT NOT NULL,
    platform_id TEXT,
    label       TEXT
);
CREATE INDEX IF NOT EXISTS idx_ext_link_song ON song_external_link(song_id);

CREATE TABLE IF NOT EXISTS song_annotation (
    song_id     INTEGER PRIMARY KEY REFERENCES song(id) ON DELETE CASCADE,
    annotated_at REAL   NOT NULL DEFAULT 0,
    model       TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS song_revision (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    song_id    INTEGER NOT NULL,
    field      TEXT    NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    changed_at REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_song_revision ON song_revision(song_id, changed_at);

CREATE TABLE IF NOT EXISTS sync_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_DDL_SEED_ROLES = (
    "INSERT INTO credit_role (code, name_zh, sort_order) VALUES (?, ?, ?) "
    "ON CONFLICT(code) DO UPDATE SET name_zh = excluded.name_zh, "
    "sort_order = excluded.sort_order"
)

# ── DDL：songs 兼容视图 ─────────────────────────────────────────────
# 列名与顺序刻意与旧 21 列保持一致，让既有的 SELECT * / PRAGMA table_info 行为不变。
#
# 用**相关标量子查询**而非 GROUP BY 派生表：派生表会被无条件整体物化，导致连
# `WHERE id = ?` 都要把全表聚合算一遍（实测单行 16ms）；改用相关子查询后 id 谓词
# 能下推到基表索引，单行降到 0.5ms。所有可空列仍用 COALESCE 兜成空串/0，保持旧
# 表 NOT NULL DEFAULT '' 的语义（下游有 `emotion == ""` 之类的精确断言）。

def _role_credit_sql(code: str) -> str:
    return (
        "COALESCE((SELECT group_concat(a.name, '、' ORDER BY sc.position, sc.id) "
        "FROM song_credit sc "
        "JOIN artist a ON a.id = sc.artist_id "
        "JOIN credit_role r ON r.id = sc.role_id "
        f"WHERE sc.song_id = s.id AND r.code = '{code}' AND a.deleted_at IS NULL), '') "
        f"AS {code}"
    )


_VIEW_COLUMNS_HEAD = (
    "s.id        AS id",
    "s.name      AS name",
    "s.safe_name AS safe_name",
)

_SINGERS_SQL = (
    "COALESCE((SELECT group_concat(si.name, '、' ORDER BY ss.position, ss.id) "
    "FROM song_singer ss JOIN singer si ON si.id = ss.singer_id "
    "WHERE ss.song_id = s.id AND si.deleted_at IS NULL), '') AS singers"
)

_VIEW_COLUMNS_TAIL = (
    "s.year                       AS year",
    "s.introduction               AS introduction",
    "COALESCE((SELECT plain_text FROM lyrics "
    "WHERE song_id = s.id AND language = 'zh' AND is_translated = 0), '') AS lyrics",
    "COALESCE((SELECT group_concat(t.name, ',' ORDER BY st.position, t.id) "
    "FROM song_tag st JOIN tag t ON t.id = st.tag_id "
    "WHERE st.song_id = s.id AND t.kind = 'category'), '') AS categories",
    "COALESCE((SELECT group_concat(t.name, '|' ORDER BY st.position, t.id) "
    "FROM song_tag st JOIN tag t ON t.id = st.tag_id "
    "WHERE st.song_id = s.id AND t.kind = 'emotion'), '') AS emotion",
    "COALESCE((SELECT annotated_at FROM song_annotation "
    "WHERE song_id = s.id), 0) AS emotion_annotated_at",
    "s.fetched_at                 AS fetched_at",
    "s.lyrics_checked_at          AS lyrics_checked_at",
)

_VIEW_FROM = "FROM song s\nWHERE s.deleted_at IS NULL"


def build_view_sql() -> str:
    """拼出 songs 兼容视图 DDL（由 ROLE_DEFS 生成，避免手写 9 段 SQL 错位）。

    列顺序严格复刻旧 21 列表：uploader, singers, 其余 8 个创作者角色,
    year, introduction, lyrics, categories, emotion, emotion_annotated_at,
    fetched_at, lyrics_checked_at。
    """
    columns = list(_VIEW_COLUMNS_HEAD)
    columns.append(_role_credit_sql(ROLE_DEFS[0][0]))  # uploader
    columns.append(_SINGERS_SQL)
    columns.extend(_role_credit_sql(code) for code, _, _ in ROLE_DEFS[1:])
    columns.extend(_VIEW_COLUMNS_TAIL)
    body = ",\n".join("    " + c for c in columns)
    return f"CREATE VIEW songs AS\nSELECT\n{body}\n{_VIEW_FROM};\n"


DDL_VIEW = build_view_sql()

LEGACY_BACKUP_PREFIX = "songs_legacy_v1"

_MISSING = object()


def table_kind(conn: sqlite3.Connection, name: str) -> Optional[str]:
    """返回 'table' / 'view' / None。"""
    row = conn.execute(
        "SELECT type FROM sqlite_master WHERE name = ?", (name,)
    ).fetchone()
    return str(row[0]) if row else None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


def _free_legacy_name(conn: sqlite3.Connection) -> str:
    """给旧表找一个没被占用的备份名（重复迁移时不互相覆盖）。"""
    name = LEGACY_BACKUP_PREFIX
    idx = 1
    while table_kind(conn, name) is not None:
        idx += 1
        name = f"{LEGACY_BACKUP_PREFIX}_{idx}"
    return name


def _role_ids(conn: sqlite3.Connection) -> dict[str, int]:
    return {str(code): int(rid) for rid, code in conn.execute("SELECT id, code FROM credit_role")}


def _resolve_artist(conn: sqlite3.Connection, name: str, now: float) -> int:
    row = conn.execute("SELECT id FROM artist WHERE name = ?", (name,)).fetchone()
    if row:
        return int(row[0])
    conn.execute(
        "INSERT INTO artist (name, created_at, updated_at) VALUES (?, ?, ?)",
        (name, now, now),
    )
    return int(conn.execute("SELECT id FROM artist WHERE name = ?", (name,)).fetchone()[0])


def _resolve_singer(conn: sqlite3.Connection, name: str, now: float) -> int:
    row = conn.execute("SELECT id FROM singer WHERE name = ?", (name,)).fetchone()
    if row:
        return int(row[0])
    conn.execute(
        "INSERT INTO singer (name, created_at, updated_at) VALUES (?, ?, ?)",
        (name, now, now),
    )
    return int(conn.execute("SELECT id FROM singer WHERE name = ?", (name,)).fetchone()[0])


def _resolve_tag(conn: sqlite3.Connection, kind: str, name: str, now: float) -> int:
    row = conn.execute(
        "SELECT id FROM tag WHERE kind = ? AND name = ?", (kind, name)
    ).fetchone()
    if row:
        return int(row[0])
    conn.execute(
        "INSERT INTO tag (kind, name, created_at) VALUES (?, ?, ?)", (kind, name, now)
    )
    return int(
        conn.execute(
            "SELECT id FROM tag WHERE kind = ? AND name = ?", (kind, name)
        ).fetchone()[0]
    )


def write_tags(
    conn: sqlite3.Connection, song_id: int, kind: str, values: Sequence[str], now: float
) -> None:
    """整体替换某首歌某类标签（保序）。"""
    conn.execute(
        "DELETE FROM song_tag WHERE song_id = ? AND tag_id IN "
        "(SELECT id FROM tag WHERE kind = ?)",
        (song_id, kind),
    )
    for pos, value in enumerate(values):
        tag_id = _resolve_tag(conn, kind, value, now)
        conn.execute(
            "INSERT INTO song_tag (song_id, tag_id, position) VALUES (?, ?, ?) "
            "ON CONFLICT(song_id, tag_id) DO UPDATE SET position = excluded.position",
            (song_id, tag_id, pos),
        )


def write_credits(
    conn: sqlite3.Connection, song_id: int, credits: dict[str, Any], now: float
) -> None:
    """整体替换某首歌的创作者关系。

    credits 形如 {"uploader": "某人、另一个", "lyricist": ...}，键取
    CONTENT_ROLE_CODES；未出现的键视为「本次不修改」，出现但为空串则清空该角色。
    """
    roles = _role_ids(conn)
    for code in CONTENT_ROLE_CODES:
        if code not in credits:
            continue
        role_id = roles.get(code)
        if role_id is None:
            continue
        conn.execute(
            "DELETE FROM song_credit WHERE song_id = ? AND role_id = ?",
            (song_id, role_id),
        )
        for pos, person in enumerate(split_multi(credits.get(code))):
            artist_id = _resolve_artist(conn, person, now)
            conn.execute(
                "INSERT INTO song_credit (song_id, artist_id, role_id, position, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(song_id, artist_id, role_id) DO UPDATE SET position = excluded.position",
                (song_id, artist_id, role_id, pos, now),
            )


def write_singers(conn: sqlite3.Connection, song_id: int, raw: Any, now: float) -> None:
    """整体替换某首歌的演唱关系，保留 engine / voicebank。"""
    conn.execute("DELETE FROM song_singer WHERE song_id = ?", (song_id,))
    for pos, (name, engine, voicebank) in enumerate(split_singers(raw)):
        singer_id = _resolve_singer(conn, name, now)
        conn.execute(
            "INSERT INTO song_singer (song_id, singer_id, engine, voicebank, position, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(song_id, singer_id) DO UPDATE SET "
            "engine = excluded.engine, voicebank = excluded.voicebank, position = excluded.position",
            (song_id, singer_id, engine, voicebank, pos, now),
        )


def write_lyrics(
    conn: sqlite3.Connection, song_id: int, text: Any, now: float,
    language: str = "zh", source: str = "",
) -> None:
    """写入主歌词（language='zh'、非翻译）。空文本则删除该行，保持「无歌词」语义。"""
    body = str(text or "")
    if not body.strip():
        conn.execute(
            "DELETE FROM lyrics WHERE song_id = ? AND language = ? AND is_translated = 0",
            (song_id, language),
        )
        return
    conn.execute(
        "INSERT INTO lyrics (song_id, language, is_translated, plain_text, source, created_at, updated_at) "
        "VALUES (?, ?, 0, ?, ?, ?, ?) "
        "ON CONFLICT(song_id, language, is_translated) DO UPDATE SET "
        "plain_text = excluded.plain_text, updated_at = excluded.updated_at, "
        "source = CASE WHEN excluded.source != '' THEN excluded.source ELSE lyrics.source END",
        (song_id, language, body, source, now, now),
    )


def write_annotation(conn: sqlite3.Connection, song_id: int, annotated_at: float, model: str = "") -> None:
    if annotated_at <= 0:
        conn.execute("DELETE FROM song_annotation WHERE song_id = ?", (song_id,))
        return
    conn.execute(
        "INSERT INTO song_annotation (song_id, annotated_at, model) VALUES (?, ?, ?) "
        "ON CONFLICT(song_id) DO UPDATE SET annotated_at = excluded.annotated_at, "
        "model = CASE WHEN excluded.model != '' THEN excluded.model ELSE song_annotation.model END",
        (song_id, annotated_at, model),
    )


def record_revision(
    conn: sqlite3.Connection, song_id: int, field: str, old: Any, new: Any, now: float
) -> None:
    """记录一次元信息变更（旧值 != 新值时才写），对应 CVSA 的 meta.history。"""
    old_s = "" if old is None else str(old)
    new_s = "" if new is None else str(new)
    if old_s == new_s:
        return
    conn.execute(
        "INSERT INTO song_revision (song_id, field, old_value, new_value, changed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (song_id, field, old_s, new_s, now),
    )


def migrate_legacy_songs(conn: sqlite3.Connection, now: Optional[float] = None) -> Optional[dict]:
    """把旧的单表 songs 迁移到规范化结构，返回统计；非旧库返回 None。

    兼容三种历史形态：7 列（uuid 版）、18 列（无情绪列）、21 列（当前）。
    迁移完成后旧表改名为 songs_legacy_v1（或 _2、_3…）保留，可随时回退。
    """
    if table_kind(conn, "songs") != "table":
        return None

    now = time.time() if now is None else float(now)
    has = _columns(conn, "songs")
    rows = conn.execute("SELECT * FROM songs").fetchall()
    roles = _role_ids(conn)

    stats = {
        "songs": 0, "credits": 0, "singers": 0, "lyrics": 0,
        "categories": 0, "emotions": 0, "safe_name_fixed": 0,
    }
    used_safe: set[str] = set()

    for row in rows:
        data = dict(row)
        raw_id = data.get("id")
        song_id = int(raw_id) if isinstance(raw_id, int) else None
        name = str(data.get("name") or "").strip()
        safe = str(data.get("safe_name") or "").strip() or safe_song_name(name)
        if not safe or safe in used_safe:
            safe = f"__legacy_{raw_id if raw_id is not None else len(used_safe)}__"
            while safe in used_safe:
                safe += "_"
            stats["safe_name_fixed"] += 1
        used_safe.add(safe)

        year_raw = data.get("year")
        if isinstance(year_raw, bool):
            year: Optional[int] = None
        elif isinstance(year_raw, int):
            year = year_raw
        elif isinstance(year_raw, str) and year_raw.strip().isdigit():
            year = int(year_raw.strip())
        else:
            year = None

        fetched_at = float(data.get("fetched_at") or 0) if "fetched_at" in has else 0.0
        lyrics_checked = (
            float(data.get("lyrics_checked_at") or 0) if "lyrics_checked_at" in has else 0.0
        )

        if song_id is None:
            conn.execute(
                "INSERT INTO song (name, safe_name, year, introduction, fetched_at, "
                "lyrics_checked_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (name, safe, year, str(data.get("introduction") or ""),
                 fetched_at, lyrics_checked, now, now),
            )
            song_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        else:
            conn.execute(
                "INSERT INTO song (id, name, safe_name, year, introduction, fetched_at, "
                "lyrics_checked_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (song_id, name, safe, year, str(data.get("introduction") or ""),
                 fetched_at, lyrics_checked, now, now),
            )
        stats["songs"] += 1

        credits = {code: data.get(code) for code in CONTENT_ROLE_CODES if code in has}
        write_credits(conn, song_id, credits, now)
        stats["credits"] += int(
            conn.execute(
                "SELECT COUNT(*) FROM song_credit WHERE song_id = ?", (song_id,)
            ).fetchone()[0]
        )

        if "singers" in has:
            write_singers(conn, song_id, data.get("singers"), now)
            stats["singers"] += int(
                conn.execute(
                    "SELECT COUNT(*) FROM song_singer WHERE song_id = ?", (song_id,)
                ).fetchone()[0]
            )

        if "lyrics" in has:
            write_lyrics(conn, song_id, data.get("lyrics"), now)
            if str(data.get("lyrics") or "").strip():
                stats["lyrics"] += 1

        if "categories" in has:
            values = split_multi(data.get("categories"))
            write_tags(conn, song_id, TAG_KIND_CATEGORY, values, now)
            stats["categories"] += len(values)

        if "emotion" in has:
            values = split_emotion(data.get("emotion"))
            write_tags(conn, song_id, TAG_KIND_EMOTION, values, now)
            if values:
                stats["emotions"] += 1
                stamp = float(data.get("emotion_annotated_at") or 0)
                write_annotation(conn, song_id, stamp if stamp > 0 else now)

    backup = _free_legacy_name(conn)
    conn.execute("DROP INDEX IF EXISTS idx_songs_name")
    conn.execute(f"ALTER TABLE songs RENAME TO {backup}")
    # 旧表上的自动索引改名后会留下 "_idx_songs_*" 之类的名字，不影响新结构。
    stats["backup_table"] = backup
    return stats


def ensure_schema(conn: sqlite3.Connection, now: Optional[float] = None) -> Optional[dict]:
    """建表 -> 播种角色字典 -> 迁移旧库 -> 重建兼容视图。返回迁移统计或 None。"""
    conn.executescript(DDL_TABLES)
    conn.executemany(_DDL_SEED_ROLES, ROLE_DEFS)

    stats = migrate_legacy_songs(conn, now=now)

    if table_kind(conn, "songs") == "view":
        conn.execute("DROP VIEW songs")
    conn.executescript(DDL_VIEW)
    return stats
