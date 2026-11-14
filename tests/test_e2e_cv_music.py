# -*- coding: utf-8 -*-
"""cv_music 契约端到端测试（提供方视角，v2.19.0 联动加固）。

与 test_vcpedia_bilibili.py 的区别：那里的 API 返回靠**手写断言**守着，
消费方的假数据靠契约 sample_result 构造——两边各自绿，不代表接在一起能跑。
本文件加载**真实对端插件** music-request，把本插件的真实
`api_get_song_bilibili` 接到它的 `_vcpedia_pinned_song` 后端上，验证：

1. 真实 API 的返回形状通过契约全量校验（键集/类型/枚举/schema_version）；
2. 消费方拿到真实返回后能正确短路搜索（av 号 / BV 号两条路径）；
3. prefix + 原曲歌手的判定在真实数据上成立（singers 真的从库里流过去）；
4. 未收录 / 有歌无标识 / 点名翻唱者 / 曲库未就绪 → 消费方回退搜索。

对端目录不存在时整文件 skip（单仓库 CI 可跑）；对端存在但导入失败则**报错**
——那是真实回归，不能静默跳过。全程零网络：时长探测被替换成定值。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _TESTS_DIR.parent
_PEER_DIR = _PLUGIN_DIR.parent / "music-request"

if not (_PEER_DIR / "plugin.py").is_file():
    pytest.skip(
        "对端插件 music-request 不在本机（单仓库 CI 属正常）",
        allow_module_level=True,
    )

# 注意：必须包含插件父目录（Desktop）——cv 无 __init__.py，靠命名空间包
# 方式导入 `<目录名>.plugin`，父目录不在 sys.path 就解析不到。
for _p in (str(_TESTS_DIR), str(_PLUGIN_DIR), str(_PLUGIN_DIR.parent),
           str(_PEER_DIR), str(_PEER_DIR / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from contract_cv_music import validate_result  # noqa: E402
from vcpedia_store import SongStore  # noqa: E402

# 本插件（提供方）：与 test_vcpedia_bilibili.py 相同的命名空间包导入方式
_CV_MOD = importlib.import_module(_PLUGIN_DIR.name)
if not hasattr(_CV_MOD, "create_plugin"):
    _CV_MOD = importlib.import_module(f"{_PLUGIN_DIR.name}.plugin")

# 对端（消费方）：包式加载，辅助模块挂到 music_request.* 命名空间，
# 避免与本插件的顶层模块重名（真机 Runner 也是包式加载）。
from fakehost import FakeHost, bind_context, build_context, get_default_config, load_plugin_module  # noqa: E402

_MUSIC_MOD = load_plugin_module(_PEER_DIR)
_MUSIC_PLUGIN_ID = "github.orge-8.music-request"
_CV_PLUGIN_ID = "org.mai-mai.cv-lyric-context"
_BILI_API = f"{_CV_PLUGIN_ID}.get_song_bilibili"


class _Ctx:
    def __init__(self) -> None:
        self.logger = logging.getLogger("test-e2e-cv")


def _cv_plugin(tmp_path, records=None, *, started=True):
    """真实 cv 插件实例 + 临时曲库（不跑 on_load，零网络零写盘）。"""
    plug = _CV_MOD.create_plugin()
    plug.set_plugin_config({})
    plug._ctx = _Ctx()
    plug._store = SongStore(Path(tmp_path) / "e2e.db") if started else None
    for record in records or []:
        plug._store.upsert(record)
    return plug


class _RealApiBridge:
    """模拟 Host 的 api.call 转发：按全名路由到真实 cv API 方法。

    只接受契约里登记的全名；短名/未知名字直接抛错——真机 Host 对
    未注册 API 也是报错，消费方必须能兜住（test_vcpedia_lookup 已覆盖）。
    """

    def __init__(self, cv_plug) -> None:
        self._cv = cv_plug
        self.calls: list[tuple[str, dict]] = []

    async def call(self, api_name: str, **kwargs):
        self.calls.append((api_name, kwargs))
        if api_name == _BILI_API:
            return await self._cv.api_get_song_bilibili(**kwargs)
        raise RuntimeError(f"未注册的 API: {api_name}")


def _music_plug(cv_plug):
    """真实 music 插件实例，ctx.api 接到真实 cv API 上。"""
    plug = _MUSIC_MOD.create_plugin()
    host = FakeHost(plugin_id=_MUSIC_PLUGIN_ID)
    ctx = build_context(_MUSIC_PLUGIN_ID, rpc_call=host.rpc_call)
    config = get_default_config(_MUSIC_MOD.MusicRequestConfig)
    config["bilibili"]["vcpedia_lookup"] = True
    bind_context(plug, ctx, config)
    bridge = _RealApiBridge(cv_plug)
    ctx.api = bridge

    searches: list[str] = []

    async def fake_search(keyword: str, platform: str):
        searches.append(keyword)
        return []

    async def fake_duration(_video_id):
        return 200  # 常规曲目时长，不触发直连时长闸；零网络

    plug._search = fake_search  # type: ignore[method-assign]
    plug._probe_bili_duration = fake_duration  # type: ignore[method-assign]
    return plug, bridge, searches


# ---------- 真实 API 形状 ----------


def test_real_api_shape_matches_contract(tmp_path) -> None:
    """命中/未命中/无标识/空参/未就绪五种形态都必须通过契约全量校验。"""
    cv = _cv_plugin(tmp_path, [
        {"name": "普通DISCO", "bilibili_aid": 2129461},
        {"name": "旧歌"},
    ])
    for query in ("普通DISCO", "根本不存在的歌", "旧歌", "   "):
        out = asyncio.run(cv.api_get_song_bilibili(name=query))
        validate_result("get_song_bilibili", out)
    cv_off = _cv_plugin(tmp_path, started=False)
    validate_result(
        "get_song_bilibili", asyncio.run(cv_off.api_get_song_bilibili(name="普通DISCO"))
    )


# ---------- 真实链路：命中短路 ----------


def test_e2e_aid_hit_pins_original(tmp_path) -> None:
    """av 号原曲：真实曲库 → 真实 API → 消费方直连，不再搜索。"""
    cv = _cv_plugin(tmp_path, [{"name": "普通DISCO", "bilibili_aid": 2129461}])
    music, bridge, searches = _music_plug(cv)
    song = asyncio.run(music._vcpedia_pinned_song("普通DISCO"))
    assert song is not None
    assert song.song_id == "av2129461"
    assert song.name == "普通DISCO"
    assert song.platform == _MUSIC_MOD.PLATFORM_BILI
    assert searches == []
    assert bridge.calls == [(_BILI_API, {"name": "普通DISCO"})]


def test_e2e_bvid_hit_pins_original(tmp_path) -> None:
    """BV 号原曲：bvid 优先于 aid（真实库里两者可并存）。"""
    cv = _cv_plugin(tmp_path, [
        {"name": "世末歌者", "bilibili_aid": 13787072, "bilibili_bvid": "BV1Qs411k7Qv"},
    ])
    music, _, searches = _music_plug(cv)
    song = asyncio.run(music._vcpedia_pinned_song("世末歌者"))
    assert song is not None and song.song_id == "BV1Qs411k7Qv"
    assert searches == []


def test_e2e_prefix_original_singer_adopted(tmp_path) -> None:
    """「世末积雨云 洛天依」：真实 singers 字段流到消费方，原曲歌手 → 采纳。"""
    cv = _cv_plugin(tmp_path, [
        {"name": "世末积雨云", "bilibili_aid": 1579079, "singers": "洛天依"},
    ])
    music, _, searches = _music_plug(cv)
    song = asyncio.run(music._vcpedia_pinned_song("世末积雨云 洛天依"))
    assert song is not None and song.song_id == "av1579079"
    assert searches == []


# ---------- 真实链路：回退搜索 ----------


def test_e2e_unknown_song_falls_back(tmp_path) -> None:
    cv = _cv_plugin(tmp_path, [{"name": "普通DISCO", "bilibili_aid": 2129461}])
    music, _, searches = _music_plug(cv)
    songs = asyncio.run(music._search_ranked("根本不存在的歌", _MUSIC_MOD.PLATFORM_BILI))
    assert songs == []
    assert searches == ["根本不存在的歌"]


def test_e2e_song_without_bilibili_id_falls_back(tmp_path) -> None:
    """歌在库里但没采集标识：found=True 也要回退（契约语义第 2 条）。"""
    cv = _cv_plugin(tmp_path, [{"name": "旧歌"}])
    music, _, searches = _music_plug(cv)
    songs = asyncio.run(music._search_ranked("旧歌", _MUSIC_MOD.PLATFORM_BILI))
    assert songs == []
    assert searches == ["旧歌"]


def test_e2e_cover_singer_named_falls_back(tmp_path) -> None:
    """「普通DISCO 三无」：真实 singers 不含三无 → 不钉原曲，回退搜索。

    命令路径（_search_ranked）不带用户原话，prefix 命中且额外 token 非
    原曲歌手时按保守策略回退——与工具路径的判定一致。
    """
    cv = _cv_plugin(tmp_path, [
        {"name": "普通DISCO", "bilibili_aid": 2129461, "singers": "洛天依、言和"},
    ])
    music, _, searches = _music_plug(cv)
    songs = asyncio.run(music._search_ranked("普通DISCO 三无", _MUSIC_MOD.PLATFORM_BILI))
    assert songs == []
    assert searches and searches[0] == "普通DISCO 三无"


def test_e2e_store_not_ready_falls_back(tmp_path) -> None:
    """cv 曲库未就绪：API 降级返回 found=False，消费方照常回退。"""
    cv = _cv_plugin(tmp_path, started=False)
    music, _, searches = _music_plug(cv)
    songs = asyncio.run(music._search_ranked("普通DISCO", _MUSIC_MOD.PLATFORM_BILI))
    assert songs == []
    assert searches == ["普通DISCO"]
