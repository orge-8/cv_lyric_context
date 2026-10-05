# -*- coding: utf-8 -*-
"""跨插件只读 API ``get_recent_songs`` 的契约测试（v2.9.0 新增）。

契约纪律（与 README「跨插件 API」一节一一对应）：
- 只读：零网络、零写盘——调用前后文件 mtime/内容不变、内存记录不变；
- 结构永远完整：空/坏记录 → ``songs=[]`` + ``reason``，不抛异常；
- 纯 dict：bool/int/float/str/None，可过 msgpack；
- 记录点：歌词命中即登记（歌手取自歌曲库），节流落盘、环形上限。
"""

import asyncio
import copy
import importlib
import json
import logging
import pathlib
import sys
import time
from types import SimpleNamespace

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(_PLUGIN_DIR), str(_PLUGIN_DIR.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_MOD = importlib.import_module(_PLUGIN_DIR.name)
if not hasattr(_MOD, "create_plugin"):
    _MOD = importlib.import_module(f"{_PLUGIN_DIR.name}.plugin")

from recent_songs import (  # noqa: E402
    ARTIST_MAX_CHARS,
    NAME_MAX_CHARS,
    RECENT_SONGS_LIMIT,
    RecentSongsLog,
)

ALL_KEYS = {"schema_version", "reason", "active", "songs"}
ENTRY_KEYS = {"name", "artist", "at"}


class _Ctx:
    def __init__(self):
        self.logger = logging.getLogger("test-cross-api")


def _plugin(tmp_path, *, started=True):
    plug = _MOD.create_plugin()
    plug.set_plugin_config({})
    plug._ctx = _Ctx()
    if started:
        plug._recent_songs = RecentSongsLog(str(tmp_path))
    return plug


def _hit_plugin(tmp_path):
    """能跑 record_hit 的最小装配（不碰 SQLite / SDK 能力）。"""
    plug = _plugin(tmp_path)
    plug._songs_by_line = {"测试歌词": ["测试曲"]}
    plug._song_record = lambda name: {"name": name, "singers": "某P主、某歌手"}  # type: ignore[assignment]
    return plug


# ---------------------------------------------------------------- get_recent_songs


def test_empty_degrades_with_reason(tmp_path):
    out = asyncio.run(_plugin(tmp_path).api_get_recent_songs())
    assert set(out) == ALL_KEYS
    assert out["schema_version"] == 1
    assert out["songs"] == [] and out["active"] is True
    assert out["reason"] == "暂无歌曲命中记录"


def test_not_started_reports_inactive(tmp_path):
    out = asyncio.run(_plugin(tmp_path, started=False).api_get_recent_songs())
    assert out["active"] is False and out["songs"] == []
    assert "未启动" in out["reason"] or "停用" in out["reason"]


def test_entries_shape_order_and_limit(tmp_path):
    plug = _plugin(tmp_path)
    now = time.time()
    plug._recent_songs.add("第一首", "P主甲", at=now - 100)
    plug._recent_songs.add("第二首", "", at=now - 50)
    plug._recent_songs.add("第三首", "P主乙、歌手丙", at=now - 10)

    out = asyncio.run(plug.api_get_recent_songs())
    assert out["reason"] == ""
    assert [s["name"] for s in out["songs"]] == ["第三首", "第二首", "第一首"], "最新在前"
    for entry in out["songs"]:
        assert set(entry) == ENTRY_KEYS
        assert isinstance(entry["name"], str) and isinstance(entry["artist"], str)
        assert isinstance(entry["at"], float)
    # artist 空串原样返回（不猜）
    assert out["songs"][1]["artist"] == ""

    limited = asyncio.run(plug.api_get_recent_songs(limit=2))
    assert [s["name"] for s in limited["songs"]] == ["第三首", "第二首"]
    for value in out.values():
        assert value is None or isinstance(value, (bool, int, float, str, list, dict))


def test_api_readonly(tmp_path):
    """只读：反复调用不改内存记录、不写盘、不清 dirty 标记。"""
    plug = _plugin(tmp_path)
    plug._recent_songs.add("测试曲", "某P主")
    plug._recent_songs.flush(force=True)
    path = pathlib.Path(plug._recent_songs.path)
    before_bytes = path.read_bytes()
    before_stat = path.stat()
    entries_before = copy.deepcopy(plug._recent_songs._entries)

    for _ in range(3):
        assert len(asyncio.run(plug.api_get_recent_songs())["songs"]) == 1

    after_stat = path.stat()
    assert path.read_bytes() == before_bytes, "只读 API 不得改写记录文件"
    assert (after_stat.st_mtime, after_stat.st_size) == (before_stat.st_mtime, before_stat.st_size)
    assert plug._recent_songs._entries == entries_before
    assert plug._recent_songs.dirty is False, "只读 API 不得制造待写状态"


def test_corrupt_file_degrades(tmp_path):
    (tmp_path / "recent_songs.json").write_text("{ not json", encoding="utf-8")
    plug = _plugin(tmp_path)
    out = asyncio.run(plug.api_get_recent_songs())
    assert out["songs"] == [] and out["reason"] == "暂无歌曲命中记录"
    assert (tmp_path / "recent_songs.json.broken").exists(), "坏文件应备份重建而非抛出"

    # 结构不对（dict 但没有 entries / entries 不是 list）也当空档案
    (tmp_path / "recent_songs.json").write_text('{"entries": "oops"}', encoding="utf-8")
    assert RecentSongsLog(str(tmp_path)).count == 0


def test_ring_limit_drops_oldest(tmp_path):
    log = RecentSongsLog(str(tmp_path))
    for i in range(RECENT_SONGS_LIMIT + 5):
        log.add(f"第{i}首", "P主", at=1000 + i)
    assert log.count == RECENT_SONGS_LIMIT, "超出上限必须丢最旧"
    names = [s["name"] for s in log.recent(limit=100)]
    assert names[0] == f"第{RECENT_SONGS_LIMIT + 4}首" and "第0首" not in names
    log.flush(force=True)
    on_disk = json.loads((tmp_path / "recent_songs.json").read_text(encoding="utf-8"))
    assert on_disk["schema"] == 1 and len(on_disk["entries"]) == RECENT_SONGS_LIMIT


def test_fields_sanitized_and_truncated(tmp_path):
    log = RecentSongsLog(str(tmp_path))
    log.add("第一行\n第二行\r\n" + "字" * 300, "P主\n甲" + "歌" * 300)
    entry = log.recent(limit=1)[0]
    assert "\n" not in entry["name"] and "\r" not in entry["name"]
    assert len(entry["name"]) <= NAME_MAX_CHARS
    assert "\n" not in entry["artist"] and len(entry["artist"]) <= ARTIST_MAX_CHARS
    assert log.add("", "无名字") is False, "空名字不记录"
    assert log.count == 1


def test_flush_is_throttled_then_forced(tmp_path):
    """add 只动内存；首次 flush 立即落盘；此后窗口内被节流；force 绕过；写完清 dirty。"""
    log = RecentSongsLog(str(tmp_path))
    log.add("第一首", "P主")
    assert not (tmp_path / "recent_songs.json").exists(), "add 不应立即写盘"
    assert log.dirty is True

    # 新进程没有"上次写盘时刻"，首个命中立即落盘（别把第一条丢掉）
    assert log.flush() is True
    assert log.dirty is False
    path = tmp_path / "recent_songs.json"
    mtime = path.stat().st_mtime

    # 此后 30s 窗口内的写请求被节流：热路径上不会每条命中都同步落盘
    log.add("第二首", "P主")
    assert log.flush(throttle_sec=3600) is False, "节流窗口内不应写盘"
    assert path.stat().st_mtime == mtime

    # force 绕过节流（on_unload 用）
    assert log.flush(force=True) is True
    entries = json.loads(path.read_text(encoding="utf-8"))["entries"]
    assert entries[-1]["name"] == "第二首"
    assert log.flush(force=True) is False, "无脏数据时 force 也不重复写"


# ---------------------------------------------------------------- 记录点行为


def test_record_hit_appends_recent_song(tmp_path):
    plug = _hit_plugin(tmp_path)
    matched = plug.record_hit("session-1", "测试歌词")
    assert matched == ["测试曲"]
    songs = asyncio.run(plug.api_get_recent_songs())["songs"]
    assert len(songs) == 1
    assert songs[0]["name"] == "测试曲"
    assert songs[0]["artist"] == "某P主、某歌手", "歌手应取自歌曲库 singers 列"
    assert songs[0]["at"] > 0


def test_record_hit_dedup_window_adds_once(tmp_path):
    """同一会话短时间内重复同一组句子只登记一次（沿用既有去重），不重复记。"""
    plug = _hit_plugin(tmp_path)
    plug.record_hit("session-1", "测试歌词")
    plug.record_hit("session-1", "测试歌词")
    assert len(asyncio.run(plug.api_get_recent_songs())["songs"]) == 1


def test_record_hit_survives_missing_store(tmp_path):
    """歌曲库不可用（或取歌手失败）时：命中记录仍成立，artist 留空、不抛异常。"""
    plug = _hit_plugin(tmp_path)

    def _boom(name):
        raise RuntimeError("库不可用")

    plug._song_record = _boom  # type: ignore[assignment]
    assert plug.record_hit("session-1", "测试歌词") == ["测试曲"]
    songs = asyncio.run(plug.api_get_recent_songs())["songs"]
    assert len(songs) == 1 and songs[0]["artist"] == ""


def test_record_hit_without_log_is_noop(tmp_path):
    """on_load 未跑（_recent_songs=None）：record_hit 照常工作，不炸。"""
    plug = _hit_plugin(tmp_path)
    plug._recent_songs = None
    assert plug.record_hit("session-1", "测试歌词") == ["测试曲"]
    out = asyncio.run(plug.api_get_recent_songs())
    assert out["active"] is False and out["songs"] == []


# ---------------------------------------------------------------- 组件注册


def test_api_component_registered():
    """Runner 视角：API 必须被 collect_components 收集到（防装饰器被插队错绑）。"""
    from maibot_sdk.components import collect_components

    plug = _MOD.create_plugin()
    apis = {c.get("name"): c for c in collect_components(plug) if c.get("type") == "API"}
    assert "get_recent_songs" in apis, apis
    meta = apis["get_recent_songs"].get("metadata") or {}
    assert str(meta.get("version")) == "1"
    assert meta.get("public") is True
    assert meta.get("handler_name") == "api_get_recent_songs"
