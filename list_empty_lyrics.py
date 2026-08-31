"""列出歌曲库里「歌词为空」的条目，用来人工抽查是不是解析器漏了。

批量补歌词跑完后剩下的空歌词条目，绝大多数是本来就没有歌词章节的页面
（专辑页、纯音乐、器乐），但也可能混着「页面结构特殊导致解析失败」的
（比如《山遥路远》那个 <poem> 后空行 + 模板的坑）。把名字抓出来，挑几首用
check_lyrics_parse.py 逐步骤追踪，就能分辨是哪一类。

用法（在插件目录下执行）：
    python list_empty_lyrics.py              # 列出全部
    python list_empty_lyrics.py 30           # 只列前 30 首
    python list_empty_lyrics.py 30 <db路径>  # 指定数据库

不填数据库路径时，会按「插件目录同级 data」→「MaiBot 根目录下 data」的顺序
找 vcpedia_songs.db，同名的取最大的那个（插件自己的库通常最大）。
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path
from typing import List, Optional

DB_FILE = "vcpedia_songs.db"


def _find_db() -> Optional[Path]:
    here = Path(__file__).resolve().parent
    candidates: List[Path] = [here / DB_FILE]
    roots = [here.parent / "data", here.parent.parent / "data"]
    for base in roots:
        if not base.is_dir():
            continue
        try:
            found = sorted(
                base.rglob(DB_FILE), key=lambda p: p.stat().st_size, reverse=True
            )
        except OSError:
            continue
        candidates.extend(found)
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def _dump(db_path: Path, limit: int) -> int:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(songs)")}
        checked_col = "lyrics_checked_at" in columns
        select = "SELECT name, year, singers, categories"
        if checked_col:
            select += ", lyrics_checked_at"
        select += " FROM songs WHERE lyrics IS NULL OR TRIM(lyrics) = '' ORDER BY id"
        params: tuple = ()
        if limit > 0:
            select += " LIMIT ?"
            params = (limit,)
        rows = conn.execute(select, params).fetchall()
        total = int(conn.execute(
            "SELECT COUNT(*) FROM songs WHERE lyrics IS NULL OR TRIM(lyrics) = ''"
        ).fetchone()[0])
        if checked_col:
            pending = int(conn.execute(
                "SELECT COUNT(*) FROM songs WHERE (lyrics IS NULL OR TRIM(lyrics) = '') "
                "AND lyrics_checked_at = 0"
            ).fetchone()[0])
        else:
            pending = total
    finally:
        conn.close()

    print(f"数据库: {db_path}")
    print(f"歌词为空共 {total} 首，其中未确认过的 {pending} 首")
    if not rows:
        print("（没有空歌词条目）")
        return 0
    print(f"--- 列出 {len(rows)} 首 ---")
    for row in rows:
        year = row["year"] or "?"
        singers = (row["singers"] or "").strip()
        tail = f" — {singers}" if singers else ""
        print(f"{row['name']}（{year}）{tail}")
    print()
    print("挑几首跑：python check_lyrics_parse.py <歌名>")
    return 0


def main(argv: List[str]) -> int:
    limit = 0
    db_arg = ""
    for arg in argv[1:]:
        if arg.isdigit():
            limit = int(arg)
        else:
            db_arg = arg
    db_path = Path(db_arg) if db_arg else _find_db()
    if db_path is None or not db_path.is_file():
        print(f"没找到 {DB_FILE}。用完整路径指定：python list_empty_lyrics.py 30 <db路径>")
        return 1
    return _dump(db_path, limit)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
