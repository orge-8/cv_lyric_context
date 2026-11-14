# -*- coding: utf-8 -*-
"""点歌工具可用性探测与注入降级（v2.19.0 联动加固 P1-D）。

此前注入文案无条件写死 `search_and_play_music` 工具名：点歌插件没装/
没启用时，规划器照着文案去调一个不存在的工具，白白浪费一轮决策。

现在注入前用 api.list 探测（TTL 5min 缓存）：
- 确认不在线 → 降级为「不指定工具名」文案（仍可点播，交给模型自选工具）；
- 老 Host 无 api 代理 / RPC 异常 → **保持现状**（不降级，与旧版行为一致）；
- 探测只读、3s 超时兜底，绝不拖慢注入主链路。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import pathlib
import sys
import time
from collections import deque

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(_PLUGIN_DIR), str(_PLUGIN_DIR.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_MOD = importlib.import_module(_PLUGIN_DIR.name)
if not hasattr(_MOD, "create_plugin"):
    _MOD = importlib.import_module(f"{_PLUGIN_DIR.name}.plugin")

_TOOL = "search_and_play_music"
_PROVIDER = "github.orge-8.music-request"


class _FakeApi:
    def __init__(self, listing=None, *, exc: Exception | None = None,
                 delay: float = 0.0) -> None:
        self.listing = listing
        self.exc = exc
        self.delay = delay
        self.list_calls: list[dict] = []

    async def list(self, *, plugin_id: str = ""):
        self.list_calls.append({"plugin_id": plugin_id})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return self.listing


class _Ctx:
    def __init__(self, api=None) -> None:
        self.logger = logging.getLogger("test-play-probe")
        if api is not None:
            self.api = api


def _plugin(api=None):
    plug = _MOD.create_plugin()
    plug.set_plugin_config({})
    plug._ctx = _Ctx(api)
    return plug


def _hit_plugin(api=None):
    """装一条 TTL 内的强证据命中，让注入有「可点播查询」串。"""
    plug = _plugin(api)
    plug._hits = {"s1": deque([(time.time(), "测试歌词句", "测试曲", True)], maxlen=20)}
    plug._meta_by_name = {"测试曲": ("洛天依", "")}
    plug._song_record = lambda name: {}  # type: ignore[assignment]
    return plug


def _entry(name=_TOOL, plugin_id=_PROVIDER):
    return {"name": name, "plugin_id": plugin_id, "version": "1", "enabled": True}


# ---------- 探测本体 ----------


def test_probe_online() -> None:
    api = _FakeApi([_entry()])
    plug = _plugin(api)
    assert asyncio.run(plug._probe_play_tool()) is True
    assert api.list_calls == [{"plugin_id": _PROVIDER}]


def test_probe_offline_empty_list() -> None:
    plug = _plugin(_FakeApi([]))
    assert asyncio.run(plug._probe_play_tool()) is False


def test_probe_offline_other_components() -> None:
    """提供方在线但只有别的组件（工具被禁用了）→ 不在线。"""
    plug = _plugin(_FakeApi([_entry(name="play_local_music")]))
    assert asyncio.run(plug._probe_play_tool()) is False


def test_probe_no_api_proxy_keeps_status_quo() -> None:
    """老 Host 无 ctx.api：返回 None（保持现状），不抛异常。"""
    plug = _plugin()  # _Ctx 不带 api
    assert asyncio.run(plug._probe_play_tool()) is None


def test_probe_rpc_error_keeps_status_quo() -> None:
    plug = _plugin(_FakeApi(exc=RuntimeError("host 内部错误")))
    assert asyncio.run(plug._probe_play_tool()) is None


def test_probe_timeout_keeps_status_quo() -> None:
    """探测超过 3s 预算 → 放弃（None），不能拖慢注入。"""
    plug = _plugin(_FakeApi([_entry()], delay=10))
    started = time.monotonic()
    assert asyncio.run(plug._probe_play_tool()) is None
    assert time.monotonic() - started < 6.0


def test_probe_accepts_unwrapped_dict() -> None:
    """容错：个别 Host/桩返回未解包的 {"apis": [...]}。"""
    plug = _plugin(_FakeApi({"success": True, "apis": [_entry()]}))
    assert asyncio.run(plug._probe_play_tool()) is True


def test_probe_ttl_cache() -> None:
    api = _FakeApi([_entry()])
    plug = _plugin(api)
    assert asyncio.run(plug._probe_play_tool()) is True
    assert asyncio.run(plug._probe_play_tool()) is True
    assert len(api.list_calls) == 1, "TTL 内不该重复发 RPC"
    # 缓存过期后重新探测
    plug._play_probe = (time.monotonic() - _MOD._PLAY_PROBE_TTL_SECONDS - 1, True)
    assert asyncio.run(plug._probe_play_tool()) is True
    assert len(api.list_calls) == 2


def test_probe_negative_result_also_cached() -> None:
    """「不在线」也缓存——点歌插件没装时不能每次注入都白查一遍。"""
    api = _FakeApi([])
    plug = _plugin(api)
    assert asyncio.run(plug._probe_play_tool()) is False
    assert asyncio.run(plug._probe_play_tool()) is False
    assert len(api.list_calls) == 1


# ---------- 文案降级 ----------


def test_instruction_degrades_when_offline() -> None:
    plug = _plugin()
    cfg = plug.config.integration
    normal = plug._play_instruction(cfg, True)
    degraded = plug._play_instruction(cfg, True, tool_available=False)
    assert _TOOL in normal
    assert _TOOL not in degraded
    assert "点歌工具" in degraded  # 仍提示可点播，只是不指定名字


def test_instruction_unchanged_when_unknown() -> None:
    plug = _plugin()
    cfg = plug.config.integration
    assert plug._play_instruction(cfg, True, tool_available=None) == \
        plug._play_instruction(cfg, True)


def test_planner_text_degrades_when_offline() -> None:
    plug = _hit_plugin()
    normal = plug._build_planner_text("s1")
    degraded = plug._build_planner_text("s1", tool_available=False)
    assert _TOOL in normal
    assert _TOOL not in degraded
    assert "可点播查询" in degraded, "降级只去工具名，查询串要保留"


# ---------- hook 集成 ----------


def _run_hook(plug):
    return asyncio.run(
        plug.inject_planner_context(session_id="s1", prompt="宿主原始 prompt")
    )


def test_hook_degrades_when_provider_offline() -> None:
    api = _FakeApi([])
    plug = _hit_plugin(api)
    out = _run_hook(plug)
    modified = out.get("modified_kwargs") or {}
    text = modified.get("prompt") or ""
    assert _MOD.INJECT_MARKER in text
    assert _TOOL not in text
    assert "可点播查询" in text
    assert len(api.list_calls) == 1


def test_hook_keeps_tool_when_online() -> None:
    plug = _hit_plugin(_FakeApi([_entry()]))
    out = _run_hook(plug)
    text = (out.get("modified_kwargs") or {}).get("prompt") or ""
    assert _TOOL in text


def test_hook_keeps_tool_on_old_host() -> None:
    """无 api 代理：注入照常且保留工具名（与旧版一致）。"""
    plug = _hit_plugin()
    out = _run_hook(plug)
    text = (out.get("modified_kwargs") or {}).get("prompt") or ""
    assert _TOOL in text


def test_hook_skips_probe_when_integration_disabled() -> None:
    api = _FakeApi([])
    plug = _hit_plugin(api)
    plug.config.integration.play_tool_enabled = False
    out = _run_hook(plug)
    text = (out.get("modified_kwargs") or {}).get("prompt") or ""
    assert _MOD.INJECT_MARKER in text
    assert api.list_calls == [], "联动关闭时不该发探测 RPC"


def test_hook_probe_cached_across_injections() -> None:
    api = _FakeApi([_entry()])
    plug = _hit_plugin(api)
    _run_hook(plug)
    _run_hook(plug)
    assert len(api.list_calls) == 1, "TTL 内多次注入只探测一次"
