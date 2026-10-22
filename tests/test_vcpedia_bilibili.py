"""VCPedia B站视频标识（bb_id）解析与落库的回归测试。

背景：`song` 表从 v1 起就有 `bilibili_aid` / `bilibili_bvid` 两列，但**从未
被写入过**——抓取侧没有解析代码。要让点歌插件按原曲 BV 精确取流，先把这两列
填上。

页面结构的事实边界（2026-10-21 实抓 7 个词条确认，样本内联在下方夹具里）：

    | 词条       | Songbox 数 | 首个 bb_id      | 说明                        |
    |------------|-----------|-----------------|-----------------------------|
    | 权御天下    | 2         | av2075941       | 第 2 个是洛天依官方重制版     |
    | 达拉崩吧    | 3         | av9372087       | 第 2/3 个是翻唱              |
    | 世末歌者    | 2         | BV1Qs411k7Qv    | 第 2 个是同 UP 的另一投稿     |
    | 万古生香    | 1         | av78977256      | —                           |
    | 大氿歌      | 1         | av45020985      | —                           |
    | 普通DISCO   | **0**     | （无）          | 退回首个 BilibiliVideo=2129461 |

两条铁律：
1. **首个 Songbox 才是原曲**——后续 Songbox 属于重制版/翻唱，取错会播错版本；
2. Songbox 参数里有嵌套模板（`再生 = {{bilibiliCount|id=9372087}}`），
   块切分必须按 `{{ }}` 深度做，朴素找最近的 `}}` 会切在最内层。
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from vcpedia_store import SongStore
from vcpedia_wikitext_parser import (
    normalize_bilibili_ref,
    parse_bilibili_id,
    parse_wikitext,
)

# ── 夹具：贴近实抓结构的片段（保留嵌套模板与多 Songbox）──────────────────

QUANYU = """{{tabs/core
|label1=原版　
|text1={{anchor|原版}}
{{VOCALOID_Songbox
|image = 封面-权御天下.jpg
|演唱 = [[洛天依]]
|歌曲名称 = 权御天下
|UP主 = [[乌龟Sui]]
|bb_id = av2075941
|wyy_id = 30854398
|其他资料 = 2015年2月18日首公开<br>再生数为{{BilibiliCount|id=2075941}}
}}
{{VOCALOID_Songbox
|歌曲名称 = 权御天下
|UP主 = [[洛天依]]官方账号
|bb_id = BV1FwuyzAEsw
}}
"""

DALA = """{{VOCALOID_Songbox
|演唱     = [[洛天依]]、[[言和]]
|歌曲名称 = 达拉崩吧
|UP主     = [[ilem]]
|bb_id    = av9372087
|投稿时间 = 2017年3月25日
|再生     = {{bilibiliCount|id=9372087}}
}}
{{VOCALOID_Songbox
|歌曲名称 = 达拉崩吧 王浩然视角
|UP主     = [[栗央Kurio]]
|bb_id    = av9878039
}}
{{VOCALOID_Songbox
|歌曲名称 = 吃鸡有神仙
|UP主     = [[折腾5号]]
|bb_id    = av17017664
}}
"""

SHIMO = """{{VOCALOID_Songbox
|歌曲名称 = 世末歌者
|UP主 = [[COP]]
|bb_id = BV1Qs411k7Qv
}}
{{VOCALOID_Songbox
|歌曲名称 = 世末歌者
|UP主 = [[COP]]
|bb_id = av13787072
}}
"""

PUTONG = """{{tabs/core|label1=原版}}
== 简介 ==
《'''普通DISCO'''》是[[ilem]]于2015年3月21日投稿至[[bilibili]]的原创曲。
{{BilibiliVideo|id=2129461}}
另有{{Hide|翻唱|{{BilibiliVideo|id=2130097}}}}
"""


# ── 解析：正常样本 ────────────────────────────────────────────


def test_first_songbox_wins_over_later_remake() -> None:
    """权御天下：必须取首个 Songbox 的原版，不能取洛天依官方重制版。"""
    got = parse_bilibili_id(QUANYU)
    assert got["aid"] == 2075941, got
    assert got["bvid"] == ""
    assert got["source"] == "songbox"


def test_first_songbox_wins_over_covers() -> None:
    """达拉崩吧：三个 Songbox 里只认 ilem 原曲。"""
    got = parse_bilibili_id(DALA)
    assert got["aid"] == 9372087, got


def test_bv_form_is_preserved() -> None:
    """bb_id 写成 BV 号时保留 bvid、aid 留空（世末歌者首个是 BV）。"""
    got = parse_bilibili_id(SHIMO)
    assert got["bvid"] == "BV1Qs411k7Qv", got
    assert got["aid"] is None


def test_nested_template_does_not_break_block_split() -> None:
    """Songbox 参数里的嵌套模板不能把块切碎，否则第二个 bb_id 会串位。"""
    # DALA 的第 6 行是 `|再生 = {{bilibiliCount|id=9372087}}`：
    # 若按最近的 }} 切块，首个块会在 9372087 处结束，bb_id 仍在块内 —— 但
    # 第二个 Songbox 的块边界会错位，导致 av9878039 被当成首个结果。
    assert parse_bilibili_id(DALA)["aid"] == 9372087


def test_falls_back_to_first_bilibili_video() -> None:
    """无 Songbox 时退回正文首个 BilibiliVideo（普通DISCO 的原曲 2129461）。"""
    got = parse_bilibili_id(PUTONG)
    assert got["aid"] == 2129461, got
    assert got["source"] == "bilibili_video"


def test_no_bilibili_info_returns_empty() -> None:
    """完全没有 B站信息时返回空结果（不能返回 {"aid": None} 糊弄调用方）。"""
    assert parse_bilibili_id("{{消歧义}}\n* '''[[九九八十一(乐正绫)]]'''") == {
        "aid": None,
        "bvid": "",
        "source": "",
    }


def test_empty_source_is_safe() -> None:
    assert parse_bilibili_id("")["aid"] is None
    assert parse_bilibili_id(None)["aid"] is None  # type: ignore[arg-type]


# ── 归一化：值形态 ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("av2075941", {"aid": 2075941, "bvid": ""}),
        ("AV2075941", {"aid": 2075941, "bvid": ""}),
        ("2075941", {"aid": 2075941, "bvid": ""}),
        ("BV1zs411S7sz", {"aid": None, "bvid": "BV1zs411S7sz"}),
        ("[[https://www.bilibili.com/video/av2130097 av2130097]]", {"aid": 2130097, "bvid": ""}),
        ("", {}),
        ("—", {}),
        ("待补充", {}),
    ],
)
def test_normalize_bilibili_ref(raw: str, expected: dict) -> None:
    assert normalize_bilibili_ref(raw) == expected


def test_bv_wins_over_digits_inside_it() -> None:
    """BV 号里有数字，不能先被 av 数字规则吃掉。"""
    assert normalize_bilibili_ref("BV1zs411S7sz")["bvid"] == "BV1zs411S7sz"
    assert normalize_bilibili_ref("BV1zs411S7sz")["aid"] is None


# ── parse_wikitext 集成 ──────────────────────────────────────


def test_parse_wikitext_exposes_bilibili_fields() -> None:
    parsed = parse_wikitext("权御天下", QUANYU)
    assert parsed["bilibili_aid"] == 2075941
    assert parsed["bilibili_bvid"] == ""
    assert parsed["bilibili_source"] == "songbox"


# ── 落库：写入与单调累积 ─────────────────────────────────────


def _read_bili(db_path: Path, name: str) -> tuple:
    # 必须显式 close：sqlite3.Connection 的上下文管理器只 commit/rollback，
    # **不关闭连接**；Windows 上未关闭的连接会持有文件锁，导致临时目录清理失败。
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT bilibili_aid, bilibili_bvid FROM song WHERE name = ?", (name,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"{name} 未入库"
    return row["bilibili_aid"], row["bilibili_bvid"]


def test_upsert_writes_bilibili_columns() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = SongStore(Path(tmp) / "t.db")
        store.upsert(
            {"name": "权御天下", "bilibili_aid": 2075941, "bilibili_bvid": ""}
        )
        assert _read_bili(store.db_path, "权御天下") == (2075941, "")


def test_upsert_bv_form_is_stored() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = SongStore(Path(tmp) / "t.db")
        store.upsert(
            {"name": "世末歌者", "bilibili_aid": None, "bilibili_bvid": "BV1Qs411k7Qv"}
        )
        assert _read_bili(store.db_path, "世末歌者") == (None, "BV1Qs411k7Qv")


def test_upsert_keeps_old_bilibili_when_new_is_empty() -> None:
    """单调累积：重抓解析不到 bb_id 时不能把已采集的抹掉。

    这是与 9 个创作者字段「整体覆盖」语义的唯一分歧点。全量重抓上万个词条时，
    页面模板改动 / 被改成消歧义页都会让新值为空，BV 号本身不会变。
    """
    with tempfile.TemporaryDirectory() as tmp:
        store = SongStore(Path(tmp) / "t.db")
        store.upsert(
            {"name": "权御天下", "bilibili_aid": 2075941, "bilibili_bvid": "BV1FwuyzAEsw"}
        )
        # 第二次抓取什么都没解析出来
        store.upsert({"name": "权御天下", "introduction": "改过简介"})
        assert _read_bili(store.db_path, "权御天下") == (2075941, "BV1FwuyzAEsw")


def test_upsert_overwrites_bilibili_when_new_value_present() -> None:
    """有值时必须能更新（词条把 bb_id 改正了要能生效）。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = SongStore(Path(tmp) / "t.db")
        store.upsert({"name": "达拉崩吧", "bilibili_aid": 111})
        store.upsert({"name": "达拉崩吧", "bilibili_aid": 9372087})
        assert _read_bili(store.db_path, "达拉崩吧")[0] == 9372087


def test_upsert_ignores_invalid_aid() -> None:
    """脏 aid（非数字 / 0 / 负数）不落库也不抛异常，单首坏数据不该中断同步。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = SongStore(Path(tmp) / "t.db")
        store.upsert({"name": "脏数据", "bilibili_aid": "av-something", "bilibili_bvid": ""})
        assert _read_bili(store.db_path, "脏数据") == (None, "")
        store.upsert({"name": "零值", "bilibili_aid": 0})
        assert _read_bili(store.db_path, "零值") == (None, "")


# ── 跨插件只读 API：get_song_bilibili ───────────────────────────
#
# 点歌插件要按原曲 BV 精确取流，靠这个 API 拿标识。契约与
# `get_recent_songs` 同一套：零网络、结构永远完整、不抛异常。
# 特别地，`store` 是 property，未初始化时**抛 RuntimeError**——
# `getattr(self, "store", None)` 拦不住（default 只对 AttributeError 生效），
# 所以这条路径必须有独立用例守着。

import asyncio  # noqa: E402
import importlib  # noqa: E402
import logging  # noqa: E402
import sys  # noqa: E402

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
for _p in (str(_PLUGIN_DIR), str(_PLUGIN_DIR.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_MOD = importlib.import_module(_PLUGIN_DIR.name)
if not hasattr(_MOD, "create_plugin"):
    _MOD = importlib.import_module(f"{_PLUGIN_DIR.name}.plugin")

_API_KEYS = {"schema_version", "reason", "found", "name", "aid", "bvid", "match", "singers"}


class _Ctx:
    def __init__(self) -> None:
        self.logger = logging.getLogger("test-bili-api")


def _plugin(tmp_path, records=None, *, started=True):
    plug = _MOD.create_plugin()
    plug.set_plugin_config({})
    plug._ctx = _Ctx()
    plug._store = SongStore(Path(tmp_path) / "lib.db") if started else None
    for record in records or []:
        plug._store.upsert(record)
    return plug


def test_api_returns_aid(tmp_path) -> None:
    plug = _plugin(tmp_path, [{"name": "权御天下", "bilibili_aid": 2075941}])
    out = asyncio.run(plug.api_get_song_bilibili(name="权御天下"))
    assert set(out) == _API_KEYS, out
    assert out["found"] is True
    assert out["aid"] == 2075941
    assert out["reason"] == ""


def test_api_returns_bvid(tmp_path) -> None:
    plug = _plugin(tmp_path, [{"name": "世末歌者", "bilibili_bvid": "BV1Qs411k7Qv"}])
    out = asyncio.run(plug.api_get_song_bilibili(name="世末歌者"))
    assert out["found"] is True
    assert out["bvid"] == "BV1Qs411k7Qv"
    assert out["reason"] == ""


def test_api_distinguishes_song_without_bilibili(tmp_path) -> None:
    """歌在库里但没采集到标识：found=True + reason 说明，调用方应回退搜索。"""
    plug = _plugin(tmp_path, [{"name": "旧歌"}])
    out = asyncio.run(plug.api_get_song_bilibili(name="旧歌"))
    assert out["found"] is True
    assert out["bvid"] == "" and out["aid"] is None
    assert "尚未采集" in out["reason"], out


def test_api_unknown_song(tmp_path) -> None:
    plug = _plugin(tmp_path, [])
    out = asyncio.run(plug.api_get_song_bilibili(name="根本不存在的歌"))
    assert out["found"] is False
    assert "没有这首歌" in out["reason"]


def test_api_blank_name(tmp_path) -> None:
    plug = _plugin(tmp_path, [])
    out = asyncio.run(plug.api_get_song_bilibili(name="   "))
    assert out["found"] is False
    assert "name" in out["reason"]


def test_api_degrades_when_store_not_ready(tmp_path) -> None:
    """曲库未初始化时 store property 抛 RuntimeError，API 必须降级而不是抛出。"""
    plug = _plugin(tmp_path, started=False)
    out = asyncio.run(plug.api_get_song_bilibili(name="权御天下"))
    assert set(out) == _API_KEYS
    assert out["found"] is False
    assert "未就绪" in out["reason"], out


def test_api_name_is_normalized(tmp_path) -> None:
    """歌名带标点/空格时按 safe_name 归一化仍能命中。"""
    plug = _plugin(tmp_path, [{"name": "权御天下", "bilibili_aid": 2075941}])
    out = asyncio.run(plug.api_get_song_bilibili(name="权御天下！"))
    assert out["found"] is True, out
    assert out["aid"] == 2075941


# ── 歌名多级匹配（v2.14.1）─────────────────────────────────────
#
# 点歌插件传来的是用户原话（`普通DISCO` / `普通disco` / `普通DISCO 洛天依`），
# 而库里歌名形态很杂。`safe_song_name` **不转小写**，只做精确匹配会大量漏命中。
# 逐级放宽，但**每一级都要求唯一命中**：同名多版本时不猜，返回 None 让点歌
# 回退平台搜索——宁可回退，不能播错版本。

def _store_with(tmp_path, names: list[str]):
    """建一个装了指定歌名的临时曲库（aid 依次递增便于区分）。"""
    store = SongStore(Path(tmp_path) / "match.db")
    for index, name in enumerate(names):
        store.upsert({"name": name, "bilibili_aid": 1000 + index})
    return store


def test_exact_match(tmp_path) -> None:
    store = _store_with(tmp_path, ["普通DISCO"])
    ref = store.bilibili_ref("普通DISCO")
    assert ref is not None
    assert ref["name"] == "普通DISCO"
    assert ref["match"] == "exact"


def test_case_insensitive_match(tmp_path) -> None:
    """safe_song_name 不转小写，「普通disco」必须能命中「普通DISCO」。"""
    store = _store_with(tmp_path, ["普通DISCO"])
    ref = store.bilibili_ref("普通disco")
    assert ref is not None, "大小写差异不该导致漏命中"
    assert ref["name"] == "普通DISCO"


@pytest.mark.parametrize("query", ["普通DISCO！", "普通DISCO", " 普通DISCO "])
def test_punctuation_and_space_tolerant(tmp_path, query: str) -> None:
    store = _store_with(tmp_path, ["普通DISCO"])
    assert store.bilibili_ref(query) is not None


def test_strip_bracket_suffix_from_library_name(tmp_path) -> None:
    """库里是 `Binary Life(二元生命)`，点「Binary Life」应命中。"""
    store = _store_with(tmp_path, ["Binary Life(二元生命)"])
    ref = store.bilibili_ref("Binary Life")
    assert ref is not None
    assert ref["name"] == "Binary Life(二元生命)"
    assert ref["match"] == "base"


def test_strip_bracket_suffix_from_query(tmp_path) -> None:
    """反向：查询带括号补充、库里是基名。"""
    store = _store_with(tmp_path, ["Binary Life"])
    ref = store.bilibili_ref("Binary Life（日文版）")
    assert ref is not None
    assert ref["name"] == "Binary Life"
    assert ref["match"] == "base"


def test_ambiguous_base_is_not_guessed(tmp_path) -> None:
    """`九尾妖狐` 在库里有 PoKeR / 肯德龟 两版 → 不猜，返回 None。

    这是本模块最重要的一条纪律：猜错就是播错歌，宁可让点歌回退搜索。
    """
    store = _store_with(tmp_path, ["九尾妖狐(PoKeR)", "九尾妖狐(肯德龟)"])
    assert store.bilibili_ref("九尾妖狐") is None


def test_exact_wins_over_ambiguous_base(tmp_path) -> None:
    """歧义只发生在放宽后的层级；精确命中时照常返回。"""
    store = _store_with(tmp_path, ["九尾妖狐(PoKeR)", "九尾妖狐(肯德龟)"])
    ref = store.bilibili_ref("九尾妖狐(PoKeR)")
    assert ref is not None and ref["name"] == "九尾妖狐(PoKeR)"


def test_no_substring_false_positive(tmp_path) -> None:
    """`Binary Life` 不能被 `Binary Life 2` 这类同前缀无关项命中。"""
    store = _store_with(tmp_path, ["Binary Life 2"])
    assert store.bilibili_ref("Binary Life") is None


def test_prefix_strip_for_song_plus_artist(tmp_path) -> None:
    """点歌侧常传「歌名 歌手」整串。"""
    store = _store_with(tmp_path, ["普通DISCO"])
    ref = store.bilibili_ref("普通DISCO 洛天依")
    assert ref is not None
    assert ref["name"] == "普通DISCO"
    assert ref["match"] == "prefix"


def test_prefix_strip_does_not_invent_matches(tmp_path) -> None:
    """库里只有「现场版」时，「歌名 歌手」不能剥成一半去命中它。"""
    store = _store_with(tmp_path, ["普通DISCO 现场版"])
    assert store.bilibili_ref("普通DISCO 洛天依") is None


def test_bracketless_name_with_unpaired_parens(tmp_path) -> None:
    """`9(:D` 这类没有配对括号的歌名不能被剥成 `9`，否则会误命中。"""
    store = _store_with(tmp_path, ["9(:D"])
    assert store.bilibili_ref("9") is None
    assert store.bilibili_ref("9(:D") is not None


def test_blank_query(tmp_path) -> None:
    store = _store_with(tmp_path, ["普通DISCO"])
    assert store.bilibili_ref("") is None
    assert store.bilibili_ref("   ") is None


# ── 歌名多级匹配（v2.14.1）─────────────────────────────────────
#
# 点歌插件传来的是用户原话（`普通DISCO` / `普通disco` / `普通DISCO 洛天依`），
# 而库里歌名形态很杂。`safe_song_name` **不转小写**，只做精确匹配会大量漏命中。
# 逐级放宽，但**每一级都要求唯一命中**：同名多版本时不猜，返回 None 让点歌
# 回退平台搜索——宁可回退，不能播错版本。

def _store_with(tmp_path, names: list[str]):
    """建一个装了指定歌名的临时曲库（aid 依次递增便于区分）。"""
    store = SongStore(Path(tmp_path) / "match.db")
    for index, name in enumerate(names):
        store.upsert({"name": name, "bilibili_aid": 1000 + index})
    return store


def test_exact_match(tmp_path) -> None:
    store = _store_with(tmp_path, ["普通DISCO"])
    ref = store.bilibili_ref("普通DISCO")
    assert ref is not None
    assert ref["name"] == "普通DISCO"
    assert ref["match"] == "exact"


def test_case_insensitive_match(tmp_path) -> None:
    """safe_song_name 不转小写，「普通disco」必须能命中「普通DISCO」。"""
    store = _store_with(tmp_path, ["普通DISCO"])
    ref = store.bilibili_ref("普通disco")
    assert ref is not None, "大小写差异不该导致漏命中"
    assert ref["name"] == "普通DISCO"


@pytest.mark.parametrize("query", ["普通DISCO！", "普通DISCO", " 普通DISCO "])
def test_punctuation_and_space_tolerant(tmp_path, query: str) -> None:
    store = _store_with(tmp_path, ["普通DISCO"])
    assert store.bilibili_ref(query) is not None


def test_strip_bracket_suffix_from_library_name(tmp_path) -> None:
    """库里是 `Binary Life(二元生命)`，点「Binary Life」应命中。"""
    store = _store_with(tmp_path, ["Binary Life(二元生命)"])
    ref = store.bilibili_ref("Binary Life")
    assert ref is not None
    assert ref["name"] == "Binary Life(二元生命)"
    assert ref["match"] == "base"


def test_strip_bracket_suffix_from_query(tmp_path) -> None:
    """反向：查询带括号补充、库里是基名。"""
    store = _store_with(tmp_path, ["Binary Life"])
    ref = store.bilibili_ref("Binary Life（日文版）")
    assert ref is not None
    assert ref["name"] == "Binary Life"
    assert ref["match"] == "base"


def test_ambiguous_base_is_not_guessed(tmp_path) -> None:
    """`九尾妖狐` 在库里有 PoKeR / 肯德龟 两版 → 不猜，返回 None。

    这是本模块最重要的一条纪律：猜错就是播错歌，宁可让点歌回退搜索。
    """
    store = _store_with(tmp_path, ["九尾妖狐(PoKeR)", "九尾妖狐(肯德龟)"])
    assert store.bilibili_ref("九尾妖狐") is None


def test_exact_wins_over_ambiguous_base(tmp_path) -> None:
    """歧义只发生在放宽后的层级；精确命中时照常返回。"""
    store = _store_with(tmp_path, ["九尾妖狐(PoKeR)", "九尾妖狐(肯德龟)"])
    ref = store.bilibili_ref("九尾妖狐(PoKeR)")
    assert ref is not None and ref["name"] == "九尾妖狐(PoKeR)"


def test_no_substring_false_positive(tmp_path) -> None:
    """`Binary Life` 不能被 `Binary Life 2` 这类同前缀无关项命中。"""
    store = _store_with(tmp_path, ["Binary Life 2"])
    assert store.bilibili_ref("Binary Life") is None


def test_prefix_strip_for_song_plus_artist(tmp_path) -> None:
    """点歌侧常传「歌名 歌手」整串。"""
    store = _store_with(tmp_path, ["普通DISCO"])
    ref = store.bilibili_ref("普通DISCO 洛天依")
    assert ref is not None
    assert ref["name"] == "普通DISCO"
    assert ref["match"] == "prefix"


def test_prefix_strip_does_not_invent_matches(tmp_path) -> None:
    """库里只有「现场版」时，「歌名 歌手」不能剥成一半去命中它。"""
    store = _store_with(tmp_path, ["普通DISCO 现场版"])
    assert store.bilibili_ref("普通DISCO 洛天依") is None


def test_bracketless_name_with_unpaired_parens(tmp_path) -> None:
    """`9(:D` 这类没有配对括号的歌名不能被剥成 `9`，否则会误命中。"""
    store = _store_with(tmp_path, ["9(:D"])
    assert store.bilibili_ref("9") is None
    assert store.bilibili_ref("9(:D") is not None


def test_blank_query(tmp_path) -> None:
    store = _store_with(tmp_path, ["普通DISCO"])
    assert store.bilibili_ref("") is None
    assert store.bilibili_ref("   ") is None


# ── B站标识回填任务（v2.14.2）──────────────────────────────────
#
# 真机 2026-10-22 实测：`/歌词 补歌词` 只挑**歌词为空**的条目（43 首全是确无
# 歌词的，一首都没补回），而 B站标识缺失的是**全部历史条目**——两者要筛的
# 对象完全不同，所以需要独立入口。
#
# 回填必须走 `update_bilibili`（只写两列），不能复用 `upsert`：后者会整体
# 覆盖歌词与 9 个创作者字段，等于把已同步的内容再刷一遍。


def _refill_store(tmp_path):
    return SongStore(Path(tmp_path) / "refill.db")


def test_songs_missing_bilibili_skips_filled(tmp_path) -> None:
    store = _refill_store(tmp_path)
    store.upsert({"name": "已有aid", "bilibili_aid": 111})
    store.upsert({"name": "缺标识"})
    store.upsert({"name": "已有bvid", "bilibili_bvid": "BV1zs411S7sz"})
    assert store.songs_missing_bilibili(10) == ["缺标识"]
    assert store.count_missing_bilibili() == 1


def test_songs_missing_bilibili_limit(tmp_path) -> None:
    store = _refill_store(tmp_path)
    for index in range(5):
        store.upsert({"name": f"歌{index}"})
    assert len(store.songs_missing_bilibili(3)) == 3
    assert store.songs_missing_bilibili(0) == []


def test_update_bilibili_writes_only_two_columns(tmp_path) -> None:
    """最关键的一条：回填不能碰歌词与创作者。"""
    store = _refill_store(tmp_path)
    store.upsert({
        "name": "权御天下",
        "lyrics": "原歌词内容\n第二行",
        "lyricist": "作词人甲",
        "composer": "作曲人乙",
        "introduction": "简介原文",
    })
    assert store.update_bilibili("权御天下", 2075941, "") is True
    # 注意：`get()` 返回的是旧 21 列的兼容视图，不含 bilibili 列，
    # 所以 B站标识要用 bilibili_ref 读
    assert store.bilibili_ref("权御天下")["aid"] == 2075941
    got = store.get("权御天下")
    assert got["lyrics"] == "原歌词内容\n第二行", "歌词被覆盖了"
    assert got["lyricist"] == "作词人甲", "创作者被覆盖了"
    assert got["introduction"] == "简介原文", "简介被覆盖了"


def test_update_bilibili_is_monotonic(tmp_path) -> None:
    """再次回填解析不到值时保留旧标识。"""
    store = _refill_store(tmp_path)
    store.upsert({"name": "世末歌者", "bilibili_bvid": "BV1Qs411k7Qv"})
    assert store.update_bilibili("世末歌者", None, "") is False
    assert store.bilibili_ref("世末歌者")["bvid"] == "BV1Qs411k7Qv"


def test_update_bilibili_unchanged_returns_false(tmp_path) -> None:
    store = _refill_store(tmp_path)
    store.upsert({"name": "达拉崩吧", "bilibili_aid": 9372087})
    assert store.update_bilibili("达拉崩吧", 9372087, "") is False


def test_update_bilibili_unknown_song(tmp_path) -> None:
    store = _refill_store(tmp_path)
    assert store.update_bilibili("不存在", 123, "") is False


class _FakeWikiClient:
    """假 VCPedia 客户端：按标题返回预置 wikitext。"""

    def __init__(self, pages: dict) -> None:
        self.pages = pages
        self.fetched: list[str] = []

    def fetch_wikitext(self, title: str):
        self.fetched.append(title)
        return self.pages.get(title)


def _worker_plugin(tmp_path, names: list[str]):
    plug = _MOD.create_plugin()
    plug.set_plugin_config({})
    plug._ctx = _Ctx()
    plug._store = SongStore(Path(tmp_path) / "worker.db")
    for name in names:
        plug._store.upsert({"name": name})
    plug._refetch_stop = False
    return plug


def test_worker_fills_and_counts(tmp_path) -> None:
    plug = _worker_plugin(tmp_path, ["权御天下", "无标识歌"])
    fake = _FakeWikiClient({"权御天下": QUANYU, "无标识歌": "{{消歧义}}\n* 别的歌"})
    plug._get_client = lambda: fake
    stats = plug._refetch_bilibili_worker(["权御天下", "无标识歌"])
    assert stats == {"tried": 2, "filled": 1, "no_id": 1, "errors": 0, "stopped": False}
    assert plug.store.bilibili_ref("权御天下")["aid"] == 2075941
    assert fake.fetched == ["权御天下", "无标识歌"]


def test_worker_counts_fetch_failure_as_error(tmp_path) -> None:
    plug = _worker_plugin(tmp_path, ["拉不到的歌"])
    fake = _FakeWikiClient({})  # 返回 None
    plug._get_client = lambda: fake
    stats = plug._refetch_bilibili_worker(["拉不到的歌"])
    assert stats["errors"] == 1 and stats["filled"] == 0


def test_worker_respects_stop_flag(tmp_path) -> None:
    plug = _worker_plugin(tmp_path, ["甲", "乙", "丙"])
    fake = _FakeWikiClient({"甲": QUANYU, "乙": QUANYU, "丙": QUANYU})
    plug._get_client = lambda: fake

    def fake_worker_stop(_name):
        plug._refetch_stop = True
        return QUANYU

    fake.fetch_wikitext = fake_worker_stop
    stats = plug._refetch_bilibili_worker(["甲", "乙", "丙"])
    assert stats["stopped"] is True
    assert stats["tried"] == 1, "中止后不该继续抓"


def test_command_pattern_matches_aliases() -> None:
    """命令别名要能匹配，且不能和「补歌词」串味。"""
    import re

    plug = _MOD.create_plugin()
    info = getattr(type(plug).cmd_refetch_bilibili, "__maibot_component_info__")
    pattern = info.command_pattern
    for text in (
        "/歌词 补BV",
        "/歌词 补bv 100",
        "/歌词 补b站",
        "/歌词 补B站 50",
        "/歌词 refetch_bilibili",
        "／歌词 补bv",
    ):
        assert re.match(pattern, text), text
    for text in ("/歌词 补歌词", "/歌词", "/歌词 补BVx", "/歌词 搜索 权御天下"):
        assert not re.match(pattern, text), text


# ── API 返回原曲歌手（v2.14.3）────────────────────────────────
#
# 调用方（点歌插件）用它判断「用户点的是原曲还是翻唱」：
# prefix 层级意味着查询串带了歌名之外的 token，若那个 token 正是原曲歌手
# （「世末积雨云 洛天依」）就该采纳原曲；若不是（「普通DISCO 三无」）就是
# 点名翻唱，应回退平台搜索。没有这个字段，调用方只能二选一：要么误杀
# 原曲歌手，要么误播原曲——真机 2026-10-22 两者都发生过。


def _api_plugin_with_singers(tmp_path, *, name: str, singers: list[str],
                             aid: int | None = 1579079):
    plug = _MOD.create_plugin()
    plug.set_plugin_config({})
    plug._ctx = _Ctx()
    plug._store = SongStore(Path(tmp_path) / "singers.db")
    plug._store.upsert({
        "name": name,
        "bilibili_aid": aid,
        "singers": "、".join(singers),
    })
    return plug


def test_api_returns_singers(tmp_path) -> None:
    plug = _api_plugin_with_singers(
        tmp_path, name="世末积雨云", singers=["洛天依"]
    )
    out = asyncio.run(plug.api_get_song_bilibili(name="世末积雨云 洛天依"))
    assert out["found"] is True
    assert out["match"] == "prefix"
    assert out["singers"] == ["洛天依"], out


def test_api_singers_empty_for_unknown_song(tmp_path) -> None:
    plug = _api_plugin_with_singers(tmp_path, name="别的歌", singers=[])
    out = asyncio.run(plug.api_get_song_bilibili(name="根本不存在"))
    assert out["singers"] == []
    assert set(out) == _API_KEYS


# ── API 返回原曲歌手（v2.14.3）────────────────────────────────
#
# 调用方（点歌插件）用它判断「用户点的是原曲还是翻唱」：
# prefix 层级意味着查询串带了歌名之外的 token，若那个 token 正是原曲歌手
# （「世末积雨云 洛天依」）就该采纳原曲；若不是（「普通DISCO 三无」）就是
# 点名翻唱，应回退平台搜索。没有这个字段，调用方只能二选一：要么误杀
# 原曲歌手，要么误播原曲——真机 2026-10-22 两者都发生过。


def _api_plugin_with_singers(tmp_path, *, name: str, singers: list[str],
                             aid: int | None = 1579079):
    plug = _MOD.create_plugin()
    plug.set_plugin_config({})
    plug._ctx = _Ctx()
    plug._store = SongStore(Path(tmp_path) / "singers.db")
    plug._store.upsert({
        "name": name,
        "bilibili_aid": aid,
        "singers": "、".join(singers),
    })
    return plug


def test_api_returns_singers(tmp_path) -> None:
    plug = _api_plugin_with_singers(
        tmp_path, name="世末积雨云", singers=["洛天依"]
    )
    out = asyncio.run(plug.api_get_song_bilibili(name="世末积雨云 洛天依"))
    assert out["found"] is True
    assert out["match"] == "prefix"
    assert out["singers"] == ["洛天依"], out


def test_api_singers_empty_for_unknown_song(tmp_path) -> None:
    plug = _api_plugin_with_singers(tmp_path, name="别的歌", singers=[])
    out = asyncio.run(plug.api_get_song_bilibili(name="根本不存在"))
    assert out["singers"] == []
    assert set(out) == _API_KEYS


# ── 匹配分层：精确命中优先于归一化命中（v2.14.4）──────────────
#
# 真机库（7753 首）实盘点：库里同时存在 `Say No` 与 `SAY NO!`，两者归一化键
# 都是 `SAY NO`(NOCASE)。此前把所有条件并成一个 OR，查 `SAY NO!` 会命中两条
# → 被判成歧义而返回 None，可用户输入**完全等于**其中一条。
# 修法：逐层查、命中即返回，只有同一层内多候选才算歧义。


def test_exact_name_beats_colliding_safe_name(tmp_path) -> None:
    """`SAY NO!` 精确命中同名记录，不被另一条同归一化键的歌挤成歧义。"""
    store = _store_with(tmp_path, ["Say No", "SAY NO!"])
    ref = store.bilibili_ref("SAY NO!")
    assert ref is not None, "精确同名不该被判成歧义"
    assert ref["name"] == "SAY NO!"
    assert ref["match"] == "exact"


def test_other_spelling_still_resolves(tmp_path) -> None:
    """查 `Say No` 时同样只命中它自己。"""
    store = _store_with(tmp_path, ["Say No", "SAY NO!"])
    ref = store.bilibili_ref("Say No")
    assert ref is not None and ref["name"] == "Say No"


def test_nocase_layer_still_ambiguous(tmp_path) -> None:
    """同一层内出现多个候选仍然算歧义——分层不能破坏唯一性保护。

    构造真实存在的形态：`ABC` 与 `abc` 是两条独立记录（safe_name 不同，
    能共存），NOCASE 层会同时命中；而查询 `Abc` 对两者的精确度一样 → 不猜。
    """
    store = _store_with(tmp_path, ["ABC", "abc"])
    assert store.bilibili_ref("Abc") is None
    # 精确写法则各自命中
    assert store.bilibili_ref("ABC")["name"] == "ABC"
    assert store.bilibili_ref("abc")["name"] == "abc"


def test_punctuation_only_query_resolves(tmp_path) -> None:
    """只有标点差异时走 safe_name 层，仍能命中。"""
    store = _store_with(tmp_path, ["Say No"])
    ref = store.bilibili_ref("Say No!")
    assert ref is not None and ref["name"] == "Say No"


# ── LIKE 通配符转义（全检补充覆盖）──────────────────────────────
#
# base 层用 `name LIKE ? ESCAPE` 做前缀粗筛，歌名里的 `%` / `_` 必须被转义，
# 否则一条查询就能捞出整个曲库（`%` 匹配任意串、`_` 匹配任意单字）。


def test_like_wildcards_in_song_name_are_escaped(tmp_path) -> None:
    """库里有 `100%_ Pure`，查 `100%` 不能命中它（% 不是通配符）。"""
    store = _store_with(tmp_path, ["100% Pure", "普通DISCO"])
    ref = store.bilibili_ref("100%")
    assert ref is None, "百分号应被当作普通字符，不该通配命中"


def test_underscore_is_not_single_char_wildcard(tmp_path) -> None:
    store = _store_with(tmp_path, ["A_B", "普通DISCO"])
    # `AXB` 若把 _ 当通配符就会命中 A_B
    assert store.bilibili_ref("AXB") is None
    assert store.bilibili_ref("A_B") is not None


def test_escape_like_helper() -> None:
    from vcpedia_store import _escape_like

    assert "\\" in _escape_like("100%")
    assert "\\" in _escape_like("a_b")
    assert _escape_like("普通DISCO") == "普通DISCO"


def test_wildcard_query_does_not_dump_library(tmp_path) -> None:
    """直接把 `%` 当查询串，也不该一次捞出一堆歌。"""
    store = SongStore(Path(tmp_path) / "wild.db")
    for index in range(10):
        store.upsert({"name": f"歌{index}", "bilibili_aid": 1000 + index})
    assert store.bilibili_ref("%") is None
    assert store.bilibili_ref("_") is None


# ── LIKE 通配符转义（全检补充覆盖）──────────────────────────────
#
# base 层用 `name LIKE ? ESCAPE` 做前缀粗筛，歌名里的 `%` / `_` 必须被转义，
# 否则一条查询就能捞出整个曲库（`%` 匹配任意串、`_` 匹配任意单字）。


def test_like_wildcards_in_song_name_are_escaped(tmp_path) -> None:
    """库里有 `100%_ Pure`，查 `100%` 不能命中它（% 不是通配符）。"""
    store = _store_with(tmp_path, ["100% Pure", "普通DISCO"])
    ref = store.bilibili_ref("100%")
    assert ref is None, "百分号应被当作普通字符，不该通配命中"


def test_underscore_is_not_single_char_wildcard(tmp_path) -> None:
    store = _store_with(tmp_path, ["A_B", "普通DISCO"])
    # `AXB` 若把 _ 当通配符就会命中 A_B
    assert store.bilibili_ref("AXB") is None
    assert store.bilibili_ref("A_B") is not None


def test_escape_like_helper() -> None:
    from vcpedia_store import _escape_like

    assert "\\" in _escape_like("100%")
    assert "\\" in _escape_like("a_b")
    assert _escape_like("普通DISCO") == "普通DISCO"


def test_wildcard_query_does_not_dump_library(tmp_path) -> None:
    """直接把 `%` 当查询串，也不该一次捞出一堆歌。"""
    store = SongStore(Path(tmp_path) / "wild.db")
    for index in range(10):
        store.upsert({"name": f"歌{index}", "bilibili_aid": 1000 + index})
    assert store.bilibili_ref("%") is None
    assert store.bilibili_ref("_") is None
