# -*- coding: utf-8 -*-
"""多来源字段级补全（v2.18.0）回归测试。

背景：``SongStore.upsert`` 是单来源全量覆盖（record 缺失即清空），用于
VCPedia 全量同步；多来源补全景下这会抹掉已采到的字段。mohobot 的两条原则
（归一化只做候选不做唯一键、非空值永不被空值覆盖）落地为 ``merge_missing``。
本文件守住：不覆盖、不猜歧义、不跨版本合并、关系字段按整块判空。
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from vcpedia_merge import (  # noqa: E402
    alias_keys,
    bracket_core,
    candidate_names,
    classify_suffix,
    is_version_name,
    merge_fields,
    normalize_name,
)
from vcpedia_store import SongStore  # noqa: E402


def make_store(tmp_path):
    return SongStore(tmp_path / "songs.db")


def scalar(db_path, name):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT year, introduction, bilibili_bvid FROM song WHERE name = ?", (name,)
        ).fetchone()
    finally:
        conn.close()


def main_lyrics(db_path, name):
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT l.plain_text FROM lyrics l JOIN song s ON s.id = l.song_id "
            "WHERE s.name = ? AND l.is_translated = 0", (name,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ── 纯函数：归一化只生成候选 ──────────────────────────────────────

def test_normalize_name_and_alias_keys_are_candidates_only():
    assert normalize_name("Ｓａｙ Ｎｏ！") == "sayno"
    assert normalize_name("Binary Life(二元生命)") == "binarylife二元生命"
    assert bracket_core("Binary Life(二元生命)") == "Binary Life"
    assert alias_keys("X(洛天依)") == {"x洛天依", "x"}
    assert "X(洛天依)" in candidate_names("X(洛天依)")


def test_version_names_are_not_merged_into_the_original():
    assert classify_suffix("普通DISCO(中文版)") == "version"
    assert classify_suffix("九尾妖狐(PoKeR)") == "disambiguation"
    assert is_version_name("普通DISCO(中文版)") is True
    assert is_version_name("九尾妖狐(PoKeR)") is False
    # 版本名只保留原名候选，剥括号的核名会指向另一首歌
    assert candidate_names("普通DISCO(中文版)") == ["普通DISCO(中文版)"]


def test_merge_fields_only_fills_empty_and_reports_conflicts():
    outcome = merge_fields(
        {"year": 2015, "introduction": "原简介", "bvid": ""},
        {"year": 2016, "introduction": "新简介", "bvid": "BV1xx"},
        ("year", "introduction", "bvid"),
    )
    assert outcome.filled == ["bvid"]
    assert outcome.values["year"] == 2015
    assert outcome.values["introduction"] == "原简介"
    assert outcome.values["bvid"] == "BV1xx"
    assert set(outcome.conflicts) == {"year", "introduction"}
    assert merge_fields({"a": ""}, {"a": ""}, ("a",)).filled == []


# ── SongStore.merge_missing ───────────────────────────────────────

def test_fills_only_empty_fields_and_never_overwrites(tmp_path):
    store = make_store(tmp_path)
    store.upsert({"name": "测试曲", "year": 2015, "introduction": "原简介", "lyrics": "原歌词"})
    result = store.merge_missing({
        "name": "测试曲", "year": 2016, "introduction": "新简介",
        "bilibili_bvid": "BV1xx", "lyrics": "新歌词", "singers": "洛天依",
    })
    assert result["status"] == "filled"
    assert "bilibili_bvid" in result["filled"] and "singers" in result["filled"]
    assert "lyrics" not in result["filled"], "库里已有主歌词，整块不补"
    assert set(result["conflicts"]) == {"year", "introduction"}
    row = scalar(store.db_path, "测试曲")
    assert row["year"] == 2015 and row["introduction"] == "原简介"
    assert row["bilibili_bvid"] == "BV1xx"
    assert main_lyrics(store.db_path, "测试曲") == "原歌词"


def test_second_merge_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    store.upsert({"name": "幂等曲"})
    payload = {"name": "幂等曲", "year": 2020, "introduction": "简介"}
    assert store.merge_missing(payload)["status"] == "filled"
    assert store.merge_missing(payload)["status"] == "unchanged"
    assert scalar(store.db_path, "幂等曲")["year"] == 2020


def test_ambiguous_candidates_are_rejected_without_writing(tmp_path):
    store = make_store(tmp_path)
    store.upsert({"name": "Say No"})
    store.upsert({"name": "SAY NO!"})
    result = store.merge_missing({"name": "say no", "year": 2016})
    assert result["status"] == "ambiguous"
    assert result["filled"] == []
    assert len(result["candidates"]) == 2
    # 归一化键相同不能当成唯一键直接 UPDATE
    assert scalar(store.db_path, "Say No")["year"] is None
    assert scalar(store.db_path, "SAY NO!")["year"] is None


def test_version_name_does_not_merge_into_original_through_base_lookup(tmp_path):
    store = make_store(tmp_path)
    store.upsert({"name": "普通DISCO"})
    result = store.merge_missing({"name": "普通DISCO(中文版)", "year": 2016})
    assert result["status"] == "version_name"
    assert result["filled"] == []
    assert scalar(store.db_path, "普通DISCO")["year"] is None


def test_not_found_and_invalid_are_explicit(tmp_path):
    store = make_store(tmp_path)
    assert store.merge_missing({"name": ""})["status"] == "invalid"
    assert store.merge_missing({"name": "库里没有的歌"})["status"] == "not_found"
    assert store.count() == 0, "补全入口不新增歌曲"
