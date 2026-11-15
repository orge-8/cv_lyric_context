"""歌曲库规范化迁移与维护 CLI。

v2.7.0 起，cv_lyric_context 的知识库从「21 列单表 songs」改为规范化结构
（song / artist / singer / lyrics / tag / ...），对上仍暴露同名 songs 兼容视图。
本脚本负责手工迁移、迁移后校验，以及替代旧文档里的裸 SQL 维护写法。

用法（在插件目录下或任意路径都可，脚本自己定位）：

    # 预览：把库复制到临时文件里试迁移并报告，不动原库（默认行为）
    python migrate_knowledge_db.py
    python migrate_knowledge_db.py "C:/path/to/vcpedia_songs.db"

    # 正式迁移：先做文件级备份，再迁移，并自动逐字段校验
    python migrate_knowledge_db.py --apply

    # 迁移后回收空间：确认无误再删掉 songs_legacy_v1 备份表
    python migrate_knowledge_db.py --apply --drop-legacy

    # 体检：各表规模、角色覆盖、情绪标签分布
    python migrate_knowledge_db.py --stats

    # 维护情绪标签（替代旧写法 UPDATE songs SET emotion='...'）
    python migrate_knowledge_db.py --set-emotion "山遥路远" "温柔|积极"
    python migrate_knowledge_db.py --clear-emotion "山遥路远"

退出码：0 成功，1 失败（含校验不通过）。
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from vcpedia_schema import (  # noqa: E402
    CONTENT_ROLE_CODES,
    ROLE_DEFS,
    TAG_KIND_CATEGORY,
    TAG_KIND_EMOTION,
    ensure_schema,
    split_emotion,
    split_multi,
    table_kind,
)
from vcpedia_store import SongStore, safe_song_name  # noqa: E402

DEFAULT_DB = HERE / "data" / "vcpedia_songs.db"
ROLE_LABELS = {code: label for code, label, _ in ROLE_DEFS}
LEGACY_NAMES = ("songs_legacy_v1", "songs_legacy_v1_2", "songs_legacy_v1_3")


@contextlib.contextmanager
def _open(db: Path, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    """真正会关闭连接的上下文管理器。

    注意 sqlite3 自带的 `with conn:` 只管事务提交/回滚、不关连接；在 Windows 上
    会让临时副本文件被占用而删不掉，所以这里显式 close。
    """
    if readonly:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(db), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _pending_table(conn: sqlite3.Connection) -> Optional[str]:
    """返回「还没迁移」的旧表名：仅当 songs 仍是实体表时才算待迁移。"""
    return "songs" if table_kind(conn, "songs") == "table" else None


def _backup_table(conn: sqlite3.Connection) -> Optional[str]:
    """返回迁移后保留的旧表名（用于对已迁移的库做一致性复核）。"""
    for name in LEGACY_NAMES:
        if table_kind(conn, name) == "table":
            return name
    return None


def _snapshot(conn: sqlite3.Connection, table: str) -> List[Dict[str, Any]]:
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]


def _norm(value: Any) -> str:
    return str(value if value is not None else "")


def _expected_multivalue(raw: Any) -> List[str]:
    return split_multi(raw)


def compare_rows(
    before: List[Dict[str, Any]], after: List[Dict[str, Any]]
) -> List[str]:
    """逐字段比对迁移前后，返回差异描述（空列表 = 零丢失）。"""
    problems: List[str] = []
    if len(before) != len(after):
        problems.append(f"行数不一致：迁移前 {len(before)}，迁移后 {len(after)}")
    after_by_key = {_norm(r.get("name")): r for r in after}
    for old in before:
        name = _norm(old.get("name"))
        new = after_by_key.get(name)
        if new is None:
            problems.append(f"丢失条目：{name!r}")
            continue
        for col in ("name", "safe_name", "introduction", "lyrics"):
            if _norm(old.get(col)) != _norm(new.get(col)):
                problems.append(f"{name!r} 的 {col} 不一致")
        for col in ("singers", "uploader", *CONTENT_ROLE_CODES, "categories"):
            if col not in old:
                continue
            if _expected_multivalue(old.get(col)) != _expected_multivalue(new.get(col)):
                problems.append(
                    f"{name!r} 的 {col} 不一致："
                    f"{_expected_multivalue(old.get(col))} -> {_expected_multivalue(new.get(col))}"
                )
        if "emotion" in old:
            if split_emotion(old.get("emotion")) != split_emotion(new.get("emotion")):
                problems.append(f"{name!r} 的 emotion 不一致")
        if "year" in old:
            old_year = old.get("year")
            new_year = new.get("year")
            if _norm(old_year) != _norm(new_year) and not (
                isinstance(old_year, int) and old_year == new_year
            ):
                problems.append(f"{name!r} 的 year 不一致：{old_year!r} -> {new_year!r}")
    return problems


def report_stats(conn: sqlite3.Connection) -> None:
    total = conn.execute("SELECT COUNT(*) FROM song WHERE deleted_at IS NULL").fetchone()[0]
    print(f"  歌曲总数            {total}")
    print("  角色分工覆盖：")
    rows = {
        str(r["code"]): int(r["n"])
        for r in conn.execute(
            "SELECT r.code AS code, COUNT(DISTINCT sc.song_id) AS n FROM song_credit sc "
            "JOIN credit_role r ON r.id = sc.role_id GROUP BY r.code"
        )
    }
    for code, label, _ in ROLE_DEFS:
        n = rows.get(code, 0)
        pct = f"{100 * n / total:5.1f}%" if total else "  n/a"
        print(f"    {label:<4} {n:>6}  {pct}")
    for label, sql in (
        ("歌姬关系", "SELECT COUNT(*) FROM song_singer"),
        ("创作者档案", "SELECT COUNT(*) FROM artist"),
        ("歌姬档案", "SELECT COUNT(*) FROM singer"),
        ("歌词条目", "SELECT COUNT(*) FROM lyrics"),
        ("标签", "SELECT COUNT(*) FROM tag"),
        ("历史修订", "SELECT COUNT(*) FROM song_revision"),
    ):
        print(f"  {label:<16} {conn.execute(sql).fetchone()[0]}")
    emotion = conn.execute(
        "SELECT COUNT(DISTINCT st.song_id) FROM song_tag st JOIN tag t ON t.id = st.tag_id "
        "WHERE t.kind = 'emotion'"
    ).fetchone()[0]
    print(f"  已标注情绪          {emotion}")
    cats = conn.execute(
        "SELECT t.name, COUNT(*) AS n FROM song_tag st JOIN tag t ON t.id = st.tag_id "
        "WHERE t.kind = 'category' GROUP BY t.name ORDER BY n DESC LIMIT 5"
    ).fetchall()
    if cats:
        print("  分类 Top5：", "，".join(f"{r['name']}({r['n']})" for r in cats))


def do_preview(db: Path) -> int:
    """在临时副本上试迁移，报告统计与校验结果，不触碰原库。

    若库已经迁移过，则改为拿保留的旧表与 songs 视图做一致性复核，
    这样「已迁移」不会被误报成「又迁移了一次」。
    """
    if not db.is_file():
        print(f"数据库不存在：{db}")
        return 1
    with _open(db, readonly=True) as src:
        kind = table_kind(src, "songs")
        pending = _pending_table(src)
        backup = _backup_table(src)
        print(f"数据库        {db}")
        print(f"songs 类型    {kind!r}")
        if pending is None:
            if backup is None:
                print("已是规范化结构，且无旧表残留，无需迁移。")
                return 0
            print(f"该库已完成迁移；拿旧表 {backup} 复核一致性……")
            before = _snapshot(src, backup)
            after = [dict(r) for r in src.execute("SELECT * FROM songs")]
            problems = compare_rows(before, after)
            if problems:
                print(f"复核发现 {len(problems)} 处差异：")
                for p in problems[:20]:
                    print(f"  - {p}")
                return 1
            print(f"复核结果      旧表 {len(before)} 条与视图逐字段一致 ✓")
            print()
            print("确认无误后可用 --apply --drop-legacy 删除旧表回收空间。")
            return 0
        before = _snapshot(src, pending)

    print(f"待迁移条目    {len(before)}")
    print("模式          预览（在临时副本上试跑，不动原库）")

    with tempfile.TemporaryDirectory(prefix="cvsa_preview_") as tmp:
        copy = Path(tmp) / db.name
        shutil.copyfile(db, copy)
        with _open(copy) as conn:
            t0 = time.perf_counter()
            stats = ensure_schema(conn)
            conn.commit()
            elapsed = time.perf_counter() - t0
            after = [dict(r) for r in conn.execute("SELECT * FROM songs")]
        print(f"试迁移耗时    {elapsed:.2f}s")
        if stats:
            print(
                "迁移统计      "
                f"歌曲 {stats['songs']}，创作者关系 {stats['credits']}，"
                f"歌姬关系 {stats['singers']}，歌词 {stats['lyrics']}，"
                f"分类 {stats['categories']}，情绪 {stats['emotions']}"
            )
        problems = compare_rows(before, after)
        if problems:
            print(f"校验不通过，共 {len(problems)} 处差异：")
            for p in problems[:20]:
                print(f"  - {p}")
            return 1
        print("校验结果      逐字段零丢失 ✓")
    print()
    print("预览模式未改动原库。确认无误后加 --apply 正式迁移（会自动做文件级备份）。")
    return 0


def do_apply(db: Path, drop_legacy: bool, keep_backup: bool) -> int:
    if not db.is_file():
        print(f"数据库不存在：{db}")
        return 1

    with _open(db, readonly=True) as src:
        kind = table_kind(src, "songs")
        pending = _pending_table(src)
        if pending is None:
            print(f"已是规范化结构（songs = {kind!r}），无需迁移。")
            if drop_legacy:
                return _maybe_drop_legacy(db)
            backup = _backup_table(src)
            if backup:
                print(f"旧表 {backup} 仍在，可用 --drop-legacy 回收空间。")
            return 0
        before = _snapshot(src, pending)

    if not keep_backup:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_file = db.with_suffix(db.suffix + f".bak-{stamp}")
        shutil.copyfile(db, backup_file)
        print(f"文件级备份    {backup_file}")

    with _open(db) as conn:
        t0 = time.perf_counter()
        stats = ensure_schema(conn)
        conn.commit()
        elapsed = time.perf_counter() - t0
        after = [dict(r) for r in conn.execute("SELECT * FROM songs")]
        legacy_table = (stats or {}).get("backup_table")

    print(f"迁移完成      耗时 {elapsed:.2f}s")
    if stats:
        print(
            f"  歌曲 {stats['songs']}，创作者关系 {stats['credits']}，"
            f"歌姬关系 {stats['singers']}，歌词 {stats['lyrics']}，"
            f"分类 {stats['categories']}，情绪 {stats['emotions']}"
        )
    if legacy_table:
        print(f"  旧表保留为 {legacy_table}（确认无误后可用 --drop-legacy 回收空间）")

    problems = compare_rows(before, after)
    if problems:
        print(f"!! 校验发现 {len(problems)} 处差异（请勿执行 --drop-legacy）：")
        for p in problems[:20]:
            print(f"  - {p}")
        print("可用备份文件回滚。")
        return 1
    print("校验结果      逐字段零丢失 ✓")

    if drop_legacy:
        return _maybe_drop_legacy(db)
    return 0


def _maybe_drop_legacy(db: Path) -> int:
    dropped: List[str] = []
    with _open(db) as conn:
        for name in LEGACY_NAMES:
            if table_kind(conn, name) == "table":
                conn.execute(f"DROP TABLE {name}")
                dropped.append(name)
        conn.commit()
        if dropped:
            conn.execute("VACUUM")
    if dropped:
        print(f"已回收旧表：{'，'.join(dropped)}")
    else:
        print("没有可回收的旧表。")
    return 0


def do_set_emotion(db: Path, name: str, spec: str) -> int:
    store = SongStore(db)
    song = store.get(name)
    if song is None:
        print(f"库中没有这首歌：{name!r}")
        return 1
    tags = [t.strip() for t in spec.replace("|", ",").split(",") if t.strip()]
    ok = store.set_emotion(name, tags)
    print(f"{'已写入' if ok else '写入失败'}：《{song['name']}》-> {'|'.join(tags) or '（清空）'}")
    return 0 if ok else 1


def do_clear_emotion(db: Path, name: str) -> int:
    store = SongStore(db)
    song = store.get(name)
    if song is None:
        print(f"库中没有这首歌：{name!r}")
        return 1
    store.clear_emotion(name)
    print(f"已清空《{song['name']}》的情绪标签")
    return 0


def do_stats(db: Path) -> int:
    if not db.is_file():
        print(f"数据库不存在：{db}")
        return 1
    store = SongStore(db)
    with _open(db) as conn:
        print(f"数据库        {db}")
        print(f"songs 类型    {table_kind(conn, 'songs')}")
        report_stats(conn)
    if store.migration_stats:
        print()
        print(f"注意：本次打开触发了迁移（{store.migration_stats}）")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="cv_lyric_context 知识库规范化迁移与维护",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("db", nargs="?", default=str(DEFAULT_DB), help="数据库路径")
    parser.add_argument("--apply", action="store_true", help="正式迁移（默认只预览）")
    parser.add_argument("--drop-legacy", action="store_true", help="迁移后删除旧表回收空间")
    parser.add_argument("--keep-backup", action="store_true", help="跳过文件级备份（不建议）")
    parser.add_argument("--stats", action="store_true", help="只输出体检统计")
    parser.add_argument("--set-emotion", nargs=2, metavar=("歌名", "标签"), help="设置情绪标签")
    parser.add_argument("--clear-emotion", metavar="歌名", help="清空情绪标签")
    args = parser.parse_args(argv)

    db = Path(args.db).expanduser()
    if args.set_emotion:
        return do_set_emotion(db, args.set_emotion[0], args.set_emotion[1])
    if args.clear_emotion:
        return do_clear_emotion(db, args.clear_emotion)
    if args.stats:
        return do_stats(db)
    if args.apply:
        return do_apply(db, args.drop_legacy, args.keep_backup)
    return do_preview(db)


if __name__ == "__main__":
    raise SystemExit(main())
