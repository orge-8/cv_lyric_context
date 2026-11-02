# -*- coding: utf-8 -*-
"""子串匹配（v2.10.0，v2.16.0 改为最长共享段取胜）回归测试。

背景（真机 2026-10-06）：群友把《我的悲伤是水做的》的「其实也并不喜欢吃鱼」
记成「可是我并不喜欢吃鱼」，整句精确匹配接不住。子串匹配在整句未命中时
按 N 字滑窗查片段索引兜底。本文件守住三层语义：

1. 索引侧：_add_subspans_into 只在整句准入后写片段，归属歌数超限不登记；
2. 匹配侧：按**最长连续共享段**取胜者（v2.16.0 之前是窗口位置先到先得，
   真机 2026-11-02《绝体绝命》事故就死在这上面）；仅共享一个最小窗口
   且多歌并列时判为歧义不猜；同歌多窗口合成一段、片段即共享段原文；
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
_index_song_into = _MOD._index_song_into
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
    # 整句就是歌词原文：多个重叠窗口同歌，合成一段、只出一条命中
    hits = _subspan_find(_clean("其实也并不喜欢吃鱼"), 6, index.subspans_by_gram, frozenset())
    assert len(hits) == 1
    assert hits[0][1] == ["我的悲伤是水做的"]
    # 片段是整段共享原文，不是 6 字窗口
    assert hits[0][0] == _clean("其实也并不喜欢吃鱼")


def test_find_winner_take_all_by_longest_shared_run():
    """真机 2026-11-02 事故：跨两行的引用被邻居歌抢走。

    「来来我最亲爱的朋友 请为我悼念悼念」在《绝体绝命》里是两行
    （「来来 我最亲爱的朋友」/「请为我 悼念 悼念」），整句永远命不中；
    它与《见字如面》共享 7 字、《光明的未来》共享 6 字。旧逻辑按窗口位置
    先到先得，把结果判给了这两首邻居，真正的歌一次都没沾上。
    """
    index = _make_index(6)
    _index_song_into(index, "绝体绝命", "洛天依", "阿良良木健",
                     ["来来 我最亲爱的朋友", "请为我 悼念 悼念"])
    _index_song_into(index, "见字如面", "", "", ["我最亲爱的朋友你在哪里"])
    _index_song_into(index, "光明的未来", "", "", ["亲爱的朋友请跟我来"])
    hits = _subspan_find(
        _clean("来来我最亲爱的朋友 请为我悼念悼念"), 6, index.subspans_by_gram, frozenset(),
    )
    assert hits == [(_clean("来来我最亲爱的朋友"), ["绝体绝命"])]


def test_find_tie_at_longer_run_keeps_all_songs():
    """同一段长共享（多首歌同词）属真歧义，全部保留，交给调用方取 bucket 首位。"""
    index = _make_index(6)
    _index_song_into(index, "原曲", "", "", ["来来我最亲爱的朋友"])
    _index_song_into(index, "翻唱", "", "", ["来来我最亲爱的朋友"])
    hits = _subspan_find(_clean("来来我最亲爱的朋友"), 6, index.subspans_by_gram, frozenset())
    assert len(hits) == 1
    assert set(hits[0][1]) == {"原曲", "翻唱"}


def test_find_minimal_window_tie_is_ambiguous():
    """只共享一个最小窗口、且多首歌并列：证据为零，不猜。"""
    index = _make_index(6)
    _index_song_into(index, "歌A", "", "", ["我最亲爱的朋友"])
    _index_song_into(index, "歌B", "", "", ["我最亲爱的朋友们"])
    key = _clean("我最亲爱的朋")  # 只有 1 个 6 字窗口
    assert _subspan_find(key, 6, index.subspans_by_gram, frozenset()) == []


def test_find_minimal_window_single_owner_still_found():
    """最小窗口但归属唯一：仍然接住（v2.10.0 的原始能力不能被这道歧义闸误伤）。"""
    index = _make_index(6)
    _index_song_into(index, "歌A", "", "", ["我最亲爱的朋友"])
    hits = _subspan_find(_clean("我最亲爱的朋"), 6, index.subspans_by_gram, frozenset())
    assert hits == [(_clean("我最亲爱的朋"), ["歌A"])]


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
