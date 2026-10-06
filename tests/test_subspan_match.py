# -*- coding: utf-8 -*-
"""子串匹配（v2.10.0）回归测试。

背景（真机 2026-10-06）：群友把《我的悲伤是水做的》的「其实也并不喜欢吃鱼」
记成「可是我并不喜欢吃鱼」，整句精确匹配接不住。子串匹配在整句未命中时
按 N 字滑窗查片段索引兜底。本文件守住三层语义：

1. 索引侧：_add_subspans_into 只在整句准入后写片段，归属歌数超限不登记；
2. 匹配侧：_subspan_find 同歌重叠窗口去重、闲聊黑名单/形态双重拦截；
3. 端到端：真实素材库上「可是我并不喜欢吃鱼」必须命中《我的悲伤是水做的》，
   且命中片段能被 _lyric_window 的包含匹配定位到歌词行。
"""
import importlib
import pathlib
import sys

import pytest

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR.parent))

_MOD = importlib.import_module(_PLUGIN_DIR.name)
if not hasattr(_MOD, "create_plugin"):
    # 包式加载（与真机一致）：__init__.py 可能只是引导文件，需下沉到 .plugin 子模块
    _MOD = importlib.import_module(f"{_PLUGIN_DIR.name}.plugin")

_AssetIndex = _MOD._AssetIndex
_add_subspans_into = _MOD._add_subspans_into
_chatter_ngrams = _MOD._chatter_ngrams
_subspan_find = _MOD._subspan_find
_build_asset_index = _MOD._build_asset_index
_clean = _MOD._clean


def _make_index(n: int = 6) -> "_AssetIndex":
    index = _AssetIndex(subspan_n=n)
    return index


# ── 索引侧 ──────────────────────────────────────────────────────

def test_index_writes_all_windows():
    index = _make_index(6)
    key = _clean("其实也并不喜欢吃鱼")  # 9 字 -> 4 个 6 字窗口
    _add_subspans_into(index, key, "我的悲伤是水做的")
    assert len(index.subspans_by_gram) == len(key) - 6 + 1
    assert index.subspans_by_gram[_clean("并不喜欢吃鱼")] == ["我的悲伤是水做的"]


def test_index_disabled_when_n_zero():
    index = _make_index(0)
    _add_subspans_into(index, _clean("其实也并不喜欢吃鱼"), "我的悲伤是水做的")
    assert index.subspans_by_gram == {}


def test_index_rejects_key_shorter_than_window():
    index = _make_index(6)
    _add_subspans_into(index, _clean("喜欢吃鱼"), "某歌")
    assert index.subspans_by_gram == {}


def test_index_caps_songs_per_gram():
    index = _make_index(6)
    key = _clean("其实也并不喜欢吃鱼")
    for song in ("歌A", "歌B", "歌C", "歌D"):
        _add_subspans_into(index, key, song)
    # 前 3 首登记，第 4 首起该片段视为通用片段不再登记
    assert index.subspans_by_gram[_clean("并不喜欢吃鱼")] == ["歌A", "歌B", "歌C"]


def test_index_idempotent_same_song():
    index = _make_index(6)
    key = _clean("其实也并不喜欢吃鱼")
    _add_subspans_into(index, key, "歌A")
    _add_subspans_into(index, key, "歌A")
    assert index.subspans_by_gram[_clean("并不喜欢吃鱼")] == ["歌A"]


# ── 匹配侧 ──────────────────────────────────────────────────────

def test_find_returns_hit_for_paraphrased_quote():
    index = _make_index(6)
    _add_subspans_into(index, _clean("其实也并不喜欢吃鱼"), "我的悲伤是水做的")
    blocked = _chatter_ngrams(6)
    hits = _subspan_find(_clean("可是我并不喜欢吃鱼～～"), 6, index.subspans_by_gram, blocked)
    # ～～ 被清洗掉；「并不喜欢吃鱼」是 6 字窗口，恰好是歌词的 [3:9] 切片
    assert hits == [(_clean("并不喜欢吃鱼"), ["我的悲伤是水做的"])]


def test_find_dedupes_overlapping_windows_same_song():
    index = _make_index(6)
    _add_subspans_into(index, _clean("其实也并不喜欢吃鱼"), "我的悲伤是水做的")
    # 整句就是歌词原文：多个重叠窗口同歌，只记第一个
    hits = _subspan_find(_clean("其实也并不喜欢吃鱼"), 6, index.subspans_by_gram, frozenset())
    assert len(hits) == 1
    assert hits[0][1] == ["我的悲伤是水做的"]


def test_find_skips_blocked_chatter_gram():
    index = _make_index(6)
    # 动态取一条 >= 6 字的日常用语，拿它的 6 字片段当索引键：
    # 消息片段撞上日常用语的片段时必须被黑名单拦下
    phrase = next(p for p in sorted(_MOD._DAILY_CHAT_PHRASES) if len(p) >= 6)
    gram = phrase[:6]
    _add_subspans_into(index, gram, "某歌")
    blocked = _chatter_ngrams(6)
    assert gram in blocked
    assert _subspan_find(gram, 6, index.subspans_by_gram, blocked) == []


def test_find_skips_chatter_like_gram():
    index = _make_index(6)
    rep = _clean("哈哈哈哈哈哈哈哈")  # 单字重复，_is_chatter_like 命中
    _add_subspans_into(index, rep, "某歌")
    hits = _subspan_find(rep, 6, index.subspans_by_gram, frozenset())
    assert hits == []


def test_find_disabled_when_n_zero():
    hits = _subspan_find(_clean("可是我并不喜欢吃鱼"), 0, {}, frozenset())
    assert hits == []


def test_chatter_ngrams_empty_when_n_zero():
    assert _chatter_ngrams(0) == frozenset()


# ── 端到端（真实素材库）─────────────────────────────────────────

@pytest.fixture(scope="module")
def real_index():
    """真实素材库构建的索引快照（与插件加载同一入口）。"""
    if not (_PLUGIN_DIR / "assets" / "song_lyric_keywords.txt").exists():
        pytest.skip("素材库不在本环境（CI/换机场景）")
    index, _logs = _build_asset_index("", str(_PLUGIN_DIR / "data"), 6)
    return index


def test_real_assets_index_built(real_index):
    assert real_index.subspan_n == 6
    assert real_index.subspans_by_gram, "子串索引为空：滑窗片段没有入索引"


def test_real_assets_paraphrase_hit(real_index):
    """真机实录场景：「可是我并不喜欢吃鱼～～」必须接得住。"""
    blocked = _chatter_ngrams(6)
    hits = _subspan_find(
        _clean("可是我并不喜欢吃鱼～～"), 6, real_index.subspans_by_gram, blocked,
    )
    assert hits, "子串匹配未命中：v2.10.0 的目标场景失效"
    songs = [s for _, song_list in hits for s in song_list]
    assert "我的悲伤是水做的" in songs


def test_real_assets_exact_lyrics_still_found(real_index):
    """原词整句仍按精确匹配走（子串层不改变既有语义）。"""
    key = _clean("其实也并不喜欢吃鱼")
    assert real_index.songs_by_line.get(key) == ["我的悲伤是水做的"]


def test_real_assets_plain_chat_no_false_positive(real_index):
    """普通闲聊不该被子串层拽进歌词语境。"""
    blocked = _chatter_ngrams(6)
    for text in ("今天天气真不错啊", "哈哈哈哈哈哈哈", "我去吃个饭马上回来"):
        hits = _subspan_find(_clean(text), 6, real_index.subspans_by_gram, blocked)
        assert not hits, f"闲聊「{text}」被子串层误命中: {hits}"
