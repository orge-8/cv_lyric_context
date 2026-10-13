# -*- coding: utf-8 -*-
"""二次元/网络梗过滤（v2.11.0）回归测试。

背景（真机 2026-10-13）：群友发「Ciallo～(∠・ω< )⌒☆ 你好呀！」——Ciallo
既是 ACG 寒暄梗，又是《已经没有人类了》的真实歌词行（真机索引里
「ciallo」片段来自该歌更长的含汉字歌词行），清洗后恰 6 字符达子串滑窗
下限，被子串兜底误命中。本文件守住三层语义：

1. 内置梗表：ciallo 等已入过滤表，精确层（_is_chatter_like）与片段层
   （_chatter_ngrams）双重拦截，且不误伤真实歌词键；
2. config 追加：_register_meme_phrases 整体重建（删词生效）、与内置表
   去重、两级缓存作废；
3. 子串层端到端：索引里登记了梗片段时，_subspan_find 必须被黑名单拦下
   （防线来自黑名单，不依赖索引缺失）。
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
    # 包式加载（与真机一致）：需下沉到 .plugin 子模块
    _MOD = importlib.import_module(f"{_PLUGIN_DIR.name}.plugin")

_ACG_MEME_PHRASES = _MOD._ACG_MEME_PHRASES
_DAILY_CHAT_PHRASES = _MOD._DAILY_CHAT_PHRASES
_register_meme_phrases = _MOD._register_meme_phrases
_chat_filter_phrases = _MOD._chat_filter_phrases
_is_chatter_like = _MOD._is_chatter_like
_chatter_ngrams = _MOD._chatter_ngrams
_subspan_find = _MOD._subspan_find
_AssetIndex = _MOD._AssetIndex
_clean = _MOD._clean


@pytest.fixture(autouse=True)
def _restore_extra_phrases():
    """追加词是模块级状态，测试后清空还原，避免泄漏到其他用例。"""
    yield
    _register_meme_phrases("")


# ---------- 1. 内置梗表 ----------

def test_builtin_meme_table_contains_ciallo():
    # 真机误报案例本身必须在表里（清洗后 6 字符，恰达子串滑窗下限）
    assert "ciallo" in _ACG_MEME_PHRASES


def test_builtin_meme_entries_meet_min_length():
    # 收录标准：清洗后 >= 4 字（低于 4 字的候选键不参与匹配，收了也是死数据）
    assert all(len(k) >= 4 for k in _ACG_MEME_PHRASES)


def test_exact_layer_blocks_meme():
    # 精确层：单发「Ciallo～(∠・ω< )⌒☆」清洗后是 "cialloω"（ω 是希腊字母、
    # isalnum 为真），与纯词根 "ciallo" 两个形态都必须被 _is_chatter_like 拦下
    assert _is_chatter_like(_clean("Ciallo～(∠・ω< )⌒☆"))
    assert _is_chatter_like("ciallo")
    assert _is_chatter_like(_clean("空尼奇瓦～"))
    assert _is_chatter_like(_clean("前方高能"))
    assert _is_chatter_like(_clean("听我说谢谢你"))


def test_exact_layer_still_blocks_daily_phrases():
    # 既有防线不回归：日常用语表照常生效
    assert _is_chatter_like(_clean("生日快乐"))
    assert "生日快乐" in _chat_filter_phrases()


def test_normal_lyric_not_blocked():
    # 梗表不误伤真实歌词键（含审计中发现的唯一子串邻居整句）
    assert not _is_chatter_like(_clean("其实也并不喜欢吃鱼"))
    assert not _is_chatter_like(_clean("可是我并不喜欢吃鱼"))
    assert not _is_chatter_like(_clean("看到前方高能预警"))


# ---------- 2. config 追加 ----------

def test_register_merges_into_filter():
    assert _register_meme_phrases("哈基米曼波 泰裤辣了") == 2
    assert _is_chatter_like("哈基米曼波")
    assert "哈基米曼波" in _chat_filter_phrases()
    assert _is_chatter_like("泰裤辣了")


def test_register_supports_cjk_separators():
    # 空格/中文逗号/顿号混排
    assert _register_meme_phrases("测试梗词甲，测试梗词乙、") == 2
    assert _is_chatter_like("测试梗词甲")
    assert _is_chatter_like("测试梗词乙")


def test_register_normalizes_case_and_width():
    # 归一化与两表同一把尺子（_clean：全角转半角、转小写）
    assert _register_meme_phrases("Ｍｅｍｅ梗词") == 1
    assert _is_chatter_like(_clean("Ｍｅｍｅ梗词"))


def test_register_drops_too_short_pieces():
    # <4 字的词永远匹配不上（min_line_len/_MIN_PURE_REPETITION_CHARS 双重下限），
    # "Ci a l o" 会被空白切成 1~2 字碎片、连同 3 字的「哈基米」一并丢弃
    assert _register_meme_phrases("哈基米 Ci a l o 测试梗词") == 1
    assert _is_chatter_like("测试梗词")


def test_register_replaces_previous_extras():
    _register_meme_phrases("测试梗词甲")
    assert _is_chatter_like("测试梗词甲")
    # 以本次配置为准整体重建：删词也生效
    _register_meme_phrases("测试梗词乙")
    assert not _is_chatter_like("测试梗词甲")
    assert _is_chatter_like("测试梗词乙")


def test_register_dedupes_against_builtin_tables():
    # 与内置梗表/日常表重叠的词不重复计数
    assert _register_meme_phrases("ciallo 生日快乐 哈基米曼波") == 1
    assert _is_chatter_like("生日快乐")


def test_register_empty_clears_extras():
    _register_meme_phrases("测试梗词甲")
    assert _register_meme_phrases("") == 0
    assert not _is_chatter_like("测试梗词甲")


def test_ngram_cache_invalidated_on_register():
    blocked_before = _chatter_ngrams(6)
    _register_meme_phrases("六个字的梗词嗷")
    blocked_after = _chatter_ngrams(6)
    # 7 字词的 6 字窗：「六个字的梗」「个字的梗词嗷」两个片段
    assert "个字的梗词嗷" in blocked_after
    assert "个字的梗词嗷" not in blocked_before


# ---------- 3. 子串层端到端 ----------

def _index_with_gram(gram: str, song: str) -> "_AssetIndex":
    """模拟真机索引状态：梗片段来自已准入的更长歌词行（直写片段表）。"""
    index = _AssetIndex(subspan_n=len(gram))
    index.subspans_by_gram[gram] = [song]
    return index


def test_subspan_find_skips_ciallo_gram():
    # 真机 2026-10-13 误报复现：消息「Ciallo～(∠・ω< )⌒☆ …」清洗后是
    # "cialloω"，其 6 字滑窗 "ciallo" 命中《已经没有人类了》的片段，必须被拦下
    index = _index_with_gram("ciallo", "已经没有人类了")
    blocked = _chatter_ngrams(6)
    assert _subspan_find("cialloω", 6, index.subspans_by_gram, blocked) == []
    assert _subspan_find("ciallo", 6, index.subspans_by_gram, blocked) == []
    # 对照组（防线来自黑名单/闲聊形态判定，而非索引缺失）：
    # 黑名单为空时同索引能命中——注意 _subspan_find 内部还有 _is_chatter_like
    # 一道独立防线，梗词照样拦；非梗片段则放行
    assert _subspan_find("ciallo", 6, index.subspans_by_gram, frozenset()) == []
    control = _index_with_gram("并不喜欢", "我的悲伤是水做的")
    assert _subspan_find("可是我并不喜欢吃鱼", 4, control.subspans_by_gram,
                         frozenset()) == [("并不喜欢", ["我的悲伤是水做的"])]


def test_subspan_find_skips_longer_meme_grams():
    # >=6 字的梗词，其滑窗片段同样全被拦截
    index = _AssetIndex(subspan_n=6)
    for gram in ("哔哩哔哩干", "哩哔哩干杯", "听我说谢谢"):
        index.subspans_by_gram[gram] = ["某歌"]
    blocked = _chatter_ngrams(6)
    hits = _subspan_find(_clean("哔哩哔哩干杯！听我说谢谢你"), 6,
                         index.subspans_by_gram, blocked)
    assert hits == []


def test_subspan_find_still_allows_normal_lyrics():
    # 梗表不拦正常歌词片段
    index = _AssetIndex(subspan_n=6)
    index.subspans_by_gram[_clean("并不喜欢吃鱼")[:6]] = ["我的悲伤是水做的"]
    blocked = _chatter_ngrams(6)
    hits = _subspan_find(_clean("可是我并不喜欢吃鱼"), 6,
                         index.subspans_by_gram, blocked)
    assert hits != []
