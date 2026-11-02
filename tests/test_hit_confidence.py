# -*- coding: utf-8 -*-
"""命中证据强度（v2.17.0）回归测试。

背景（真机 2026-11-02）：群友在群里贴了一段多首歌的歌词，另一位群友评价
「空有其表」。bot 回的是「《洛天依的自白》确实有点空有其表」——把对作品的
评价挂到了一首歌上。实际注入块里三条命中是：

    - 「一个又一个不」 出自《木雕鸽子》（6 字子串）
    - 「绝体绝命」 出自《洛天依的自白》（4 字整句，该曲歌词里恰有这一整行）
    - 「请为我悼念悼念]」 出自《绝体绝命》（8 字整句）

前两条都是「够撞上、不够证明」：4 字整句多半是曲名/成语，6 字子串多半是
通用短语，却和 8 字真引用享受同样的注入格式（歌名 + 歌手 + P主 + 歌词窗），
模型顺手就拿它定了名。

契约：
1. 共享字数 >= _CONFIDENT_SHARED_CHARS(7) 才算强证据；
2. 弱证据在注入文本里**不出现歌名、歌手、P主**，只留片段与相近歌词；
3. 强证据格式不变；弱条目在混合注入里标「曲目未确认」并禁止据此点名；
4. 规划器侧弱证据不给点播查询串，且全部为弱时不鼓励调用歌曲工具。
"""
import importlib
import logging
import pathlib
import sys

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR.parent))

_MOD = importlib.import_module(_PLUGIN_DIR.name)
if not hasattr(_MOD, "create_plugin"):
    _MOD = importlib.import_module(f"{_PLUGIN_DIR.name}.plugin")

_AssetIndex = _MOD._AssetIndex
_index_song_into = _MOD._index_song_into
_chatter_ngrams = _MOD._chatter_ngrams
_clean = _MOD._clean
MIN_CHARS = _MOD._CONFIDENT_MIN_CHARS
MIN_COVERAGE = _MOD._CONFIDENT_MIN_COVERAGE


class _Ctx:
    def __init__(self):
        self.logger = logging.getLogger("test-hit-confidence")


_SONGS = {
    "洛天依的自白": ("洛天依", "王二小",
                ["大小姐和大少爷的反派生涯", "莫名成反派 无人来救援",
                 "绝体绝命", "少女就是我"]),
    "绝体绝命": ("洛天依", "阿良良木健",
             ["来来 我最牵挂的朋友", "请为我 悼念 悼念", "我身上恶疾已蔓延 无尽的繁衍"]),
    "木雕鸽子": ("斯莱茵", "听雨",
             ["亦或是一个又一个不经意间突然亮起的灵感光芒"]),
    "我的悲伤是水做的": ("星尘", "著小生zoki", ["其实也并不喜欢吃鱼"]),
}


def _plugin():
    """装有最小索引的插件实例（复刻真机事故里的几首歌）。"""
    index = _AssetIndex(subspan_n=6)
    lyrics = {}
    for name, (singers, uploader, lines) in _SONGS.items():
        _index_song_into(index, name, singers, uploader, lines)
        lyrics[_clean(name)] = [line.replace(" ", "") for line in lines]
    plug = _MOD.create_plugin()
    plug.set_plugin_config({})
    plug._ctx = _Ctx()
    plug._songs_by_line = index.songs_by_line
    plug._meta_by_name = index.meta_by_name
    plug._db_meta = index.db_meta
    plug._lyrics_by_name = lyrics   # 供覆盖率判定查所在行
    plug._indexed_names = index.indexed_names
    plug._subspans_by_gram = index.subspans_by_gram
    plug._subspan_n = 6
    plug._subspan_blocked = _chatter_ngrams(6)
    plug._song_record = lambda name: {}  # type: ignore[assignment] - 测试里不查库
    return plug


# ---------------------------------------------------------------- 证据强度判定

def test_thresholds():
    """长度 6 字 + 覆盖率 60%：两条判据缺一不可。"""
    assert MIN_CHARS == 6
    assert MIN_COVERAGE == 0.6


def test_four_char_exact_hit_is_weak():
    """真机事故句：「绝体绝命」4 字整句 —— 撞车，不是引用。"""
    plug = _plugin()
    assert plug.record_hit("s1", "绝体绝命") == ["洛天依的自白"]
    assert plug._hits["s1"][-1][3] is False


def test_eight_char_exact_hit_is_confident():
    plug = _plugin()
    assert plug.record_hit("s1", "请为我悼念悼念") == ["绝体绝命"]
    assert plug._hits["s1"][-1][3] is True


def test_six_char_subspan_hit_is_weak():
    """真机事故句：「一个又一个不」6 字子串。"""
    plug = _plugin()
    assert plug.record_hit("s1", "一个又一个不") == ["木雕鸽子"]
    assert plug._hits["s1"][-1][3] is False


def test_seven_char_subspan_hit_is_confident():
    """v2.10.0 的目标场景必须保住：共享 7 字的子串仍算可信。"""
    plug = _plugin()
    assert plug.record_hit("s1", "可是我并不喜欢吃鱼～～") == ["我的悲伤是水做的"]
    assert plug._hits["s1"][-1][3] is True


# ---------------------------------------------------------------- 回复侧文本

def test_weak_hit_never_names_the_song():
    plug = _plugin()
    plug.record_hit("s1", "绝体绝命")
    text = plug._build_reply_text("s1")
    assert "「绝体绝命」" in text
    assert "洛天依的自白" not in text, "弱证据不能出现歌名"
    assert "王二小" not in text, "弱证据不能出现 P主"
    assert "不要点名歌名" in text


def test_confident_hit_still_names_the_song():
    plug = _plugin()
    plug.record_hit("s1", "请为我悼念悼念")
    text = plug._build_reply_text("s1")
    assert "出自《绝体绝命》" in text
    assert "阿良良木健" in text


def test_mixed_hits_mark_weak_entries():
    plug = _plugin()
    plug.record_hit("s1", "请为我悼念悼念")     # 强，《绝体绝命》
    plug.record_hit("s1", "一个又一个不")       # 弱，《木雕鸽子》
    text = plug._build_reply_text("s1")
    assert "出自《绝体绝命》" in text
    assert "木雕鸽子" not in text
    assert "曲目未确认" in text
    assert "不要据此点名歌名、歌手或作者" in text


# ---------------------------------------------------------------- 规划器侧文本

def test_planner_weak_only_has_no_play_query():
    plug = _plugin()
    plug.record_hit("s1", "绝体绝命")
    text = plug._build_planner_text("s1")
    assert "可点播查询" not in text
    assert "洛天依的自白" not in text
    assert "不要据此调用任何与歌曲相关的工具" in text


def test_planner_confident_keeps_song_name():
    plug = _plugin()
    plug.record_hit("s1", "请为我悼念悼念")
    text = plug._build_planner_text("s1")
    assert "《绝体绝命》" in text


def test_planner_mixed_marks_weak():
    plug = _plugin()
    plug.record_hit("s1", "请为我悼念悼念")
    plug.record_hit("s1", "一个又一个不")
    text = plug._build_planner_text("s1")
    assert "《绝体绝命》" in text
    assert "（曲目未确认）" in text
    assert "不要据此选歌或搜索" in text


# ---------------------------------------------------------------- 结构契约

def test_hits_deque_entry_is_4_tuple():
    plug = _plugin()
    plug.record_hit("s1", "绝体绝命")
    entry = plug._hits["s1"][-1]
    assert len(entry) == 4
    assert isinstance(entry[3], bool)
