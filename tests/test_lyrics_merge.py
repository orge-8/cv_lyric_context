# -*- coding: utf-8 -*-
"""歌词合并（v2.16.0）回归测试。

背景（真机 2026-11-02）：群友发《绝体绝命》的歌词，插件判给了《见字如面》。
根因不在匹配算法，而在**数据层**：基础词库 `song_lyric_keywords.txt` 是预生成的
残缺快照（《绝体绝命》只收了 5 行，库里是 95 行），而 `indexed_names` 是
「这首歌有句子进过索引」的布尔标记。两个外部库的加载都被这个标记短路：

    if name in index.indexed_names:
        continue      # 整首歌更完整的歌词永远进不来

真机实测：基础词库与 VCPedia 库的 2785 首交集里，2086 首在同步库里有更多歌词行，
累计 3.9 万行被永久丢弃。本文件守住：

1. 已索引过的歌**照样合并**新增歌词行（原有行不丢、不重复）；
2. 合并是幂等的（重建两次结果一致）；
3. 歌词为空/全是 blob 的歌不会把 `indexed_names` 点亮（留给重抓补词）。
"""
import importlib
import pathlib
import sqlite3
import sys

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR.parent))

_MOD = importlib.import_module(_PLUGIN_DIR.name)
if not hasattr(_MOD, "create_plugin"):
    _MOD = importlib.import_module(f"{_PLUGIN_DIR.name}.plugin")

_AssetIndex = _MOD._AssetIndex
_index_song_into = _MOD._index_song_into
_load_song_db_into = _MOD._load_song_db_into
_fill_lyrics_from_db_into = _MOD._fill_lyrics_from_db_into
_clean = _MOD._clean


def _make_index(n: int = 6) -> "_AssetIndex":
    return _AssetIndex(subspan_n=n)


def _make_db(path: pathlib.Path, rows, with_singers: bool = True) -> pathlib.Path:
    """造一个外部歌曲库（songs 表）。with_singers=False 时模拟 knowledge_db 的窄表。"""
    conn = sqlite3.connect(str(path))
    try:
        if with_singers:
            conn.execute(
                "CREATE TABLE songs (name TEXT, singers TEXT, uploader TEXT, lyrics TEXT)"
            )
            conn.executemany("INSERT INTO songs VALUES (?, ?, ?, ?)", rows)
        else:
            conn.execute("CREATE TABLE songs (name TEXT, lyrics TEXT)")
            conn.executemany("INSERT INTO songs VALUES (?, ?)", rows)
        conn.commit()
    finally:
        conn.close()
    return path


def _keys_of(index, song: str) -> set[str]:
    return {line for line, songs in index.songs_by_line.items() if song in songs}


def test_extra_db_merges_lines_for_already_indexed_song(tmp_path):
    """词库只有 1 行时，同步库的另外 2 行必须补进来（不能因为已索引就整首跳过）。"""
    index = _make_index()
    _index_song_into(index, "绝体绝命", "洛天依", "阿良良木健", ["请让我去自受自虐"])
    assert "绝体绝命" in index.indexed_names

    db = _make_db(tmp_path / "vcpedia.db", [
        ("绝体绝命", "洛天依", "阿良良木健",
         "来来 我最亲爱的朋友\n请为我 悼念 悼念\n请让我去自受自虐\n"),
    ])
    added, total, err = _load_song_db_into(db, index, {})
    assert err is None and total == 1
    assert added == 1, "补全的歌也要计入‘贡献了歌词’"
    assert _keys_of(index, "绝体绝命") == {
        _clean("请让我去自受自虐"),
        _clean("来来 我最亲爱的朋友"),
        _clean("请为我 悼念 悼念"),
    }


def test_extra_db_merge_is_idempotent(tmp_path):
    index = _make_index()
    db = _make_db(tmp_path / "vcpedia.db", [
        ("某歌", "某歌手", "某P", "第一句歌词啊\n第二句歌词啊\n"),
    ])
    _load_song_db_into(db, index, {})
    before = _keys_of(index, "某歌")
    added, _total, err = _load_song_db_into(db, index, {})
    assert err is None
    assert added == 0, "第二次合并没有新行，不该再计贡献"
    assert _keys_of(index, "某歌") == before


def test_fill_lyrics_merges_for_already_indexed_song(tmp_path):
    """knowledge_db 这条路径同样不能按 indexed_names 短路。"""
    index = _make_index()
    _index_song_into(index, "怪物", "", "", ["人们都装作看懂这结尾"])
    db = _make_db(tmp_path / "knowledge.db", [
        ("怪物", "人们都装作看懂这结尾\n我身上恶疾已蔓延\n"),
    ], with_singers=False)
    filled, err = _fill_lyrics_from_db_into(db, index, {})
    assert err is None
    assert filled == 1
    assert _keys_of(index, "怪物") == {
        _clean("人们都装作看懂这结尾"), _clean("我身上恶疾已蔓延"),
    }


def test_blob_lyrics_do_not_mark_song_indexed():
    """整首挤成一行的 blob 进不了词句索引，也不该把歌标成已索引（等重抓补词）。"""
    index = _make_index()
    blob = "没有人曾体会没有人曾了解" * 20  # 远超单行上限
    added = _index_song_into(index, "某歌", "", "", [blob])
    assert added == 0
    assert "某歌" not in index.indexed_names


def test_empty_lyrics_skipped_without_error(tmp_path):
    index = _make_index()
    db = _make_db(tmp_path / "empty.db", [("空歌", "", "", "   ")])
    added, total, err = _load_song_db_into(db, index, {})
    assert err is None and total == 1
    assert added == 0
    assert _keys_of(index, "空歌") == set()


def test_missing_columns_reports_error(tmp_path):
    db = tmp_path / "bad.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE songs (name TEXT, lyrics TEXT)")
    conn.commit()
    conn.close()
    added, total, err = _load_song_db_into(db, _make_index(), {})
    assert added == 0 and total == 0
    assert err and "缺少字段" in err
