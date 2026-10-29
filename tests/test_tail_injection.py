# -*- coding: utf-8 -*-
"""末尾注入（缓存友好）的回归测试（v2.15.0）。

背景：供应商的 prompt cache 按「从第 0 个 token 开始的最长公共前缀」命中。
宿主请求的第一条恒为 system（人设/工具说明），v2.14.x 把歌词并进那一条，
于是歌单一换整段 system 就变，后面所有历史全部按未命中价计费。

真机实测（2026-10-27~29，778 个 prompt 快照）：
- 17.5% 的 replyer 请求、11.8% 的 planner 请求完全没有可复用前缀；
- 逐例归因 48/48 全部是首条 SystemMessageItem 变化，差异位置固定落在
  字符 959 处的【歌词识别】标记。

修复契约：
1. tail 模式下，已有的 item / message **一个都不改**（内容、顺序、数量），
   只在末尾追加一条 UserMessageItem / user 消息；
2. 因此注入后的前缀与注入前逐项相同 —— 这是缓存能命中的充要条件；
3. 换歌只改末条，前缀依旧稳定；
4. system 模式保留为回退开关，行为与 v2.14.x 一致；
5. inject_position 缺失或取值非法时回退 tail，且只警告一次；
6. 已注入过的载荷原样放行（幂等）。
"""

import asyncio
import copy
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

INJECT_MARKER = _MOD.INJECT_MARKER

HOST_SYSTEM = "你是狸猫，一个中V曲库丰富的聊天机器人。"
TEXT_A = f"{INJECT_MARKER}命中「今天也是美好」"
TEXT_B = f"{INJECT_MARKER}命中「牵一半」"


class _Ctx:
    def __init__(self):
        self.logger = logging.getLogger("test-tail-injection")


def _plugin(position: str | None = None):
    plug = _MOD.create_plugin()
    plug.set_plugin_config({})
    plug._ctx = _Ctx()
    if position is not None:
        plug.config.plugin.inject_position = position
    return plug


def _hit_plugin(position: str | None = None):
    """装一条 TTL 内的歌词命中，让 _build_system_text / _build_planner_text 有输出。"""
    plug = _plugin(position)
    plug._hits = {"s1": deque([(time.time(), "测试歌词", "测试曲")], maxlen=20)}
    plug._meta_by_name = {}
    plug._song_record = lambda name: {}  # type: ignore[assignment]
    return plug


def _host_items():
    """模拟宿主 replyer 的真实载荷：1 条 system + 若干历史 + 末尾并排的 3 条 user。"""
    return [
        {"item_type": "SystemMessageItem", "meta": {"item_id": "host-sys"},
         "parts": [{"type": "text", "text": HOST_SYSTEM}]},
        {"item_type": "UserMessageItem", "meta": {"item_id": "u1"},
         "parts": [{"type": "text", "text": 'time="22:39:33" user="狸猫"'}]},
        {"item_type": "AssistantMessageItem", "meta": {"item_id": "a1"},
         "parts": [{"type": "text", "text": "接上电波了"}]},
        {"item_type": "UserMessageItem", "meta": {"item_id": "u2"},
         "parts": [{"type": "text", "text": '{"reply_act":"回应歌词"}'}]},
        {"item_type": "UserMessageItem", "meta": {"item_id": "u3"},
         "parts": [{"type": "text", "text": "当前时间：2026-10-28 10:03:17"}]},
    ]


# ---------------------------------------------------------------- 纯函数层

def test_tail_items_appends_user_item_keeps_existing():
    plug = _plugin()
    items = _host_items()
    out = plug._append_tail_user_item(items, TEXT_A)

    assert len(out) == len(items) + 1
    assert out[-1]["item_type"] == "UserMessageItem"
    assert INJECT_MARKER in out[-1]["parts"][0]["text"]


def test_tail_items_prefix_is_identical():
    """缓存契约：注入后前 N 项必须与注入前逐项相等。"""
    plug = _plugin()
    items = _host_items()
    snapshot = copy.deepcopy(items)

    out = plug._append_tail_user_item(items, TEXT_A)

    assert out[: len(items)] == snapshot, "已有 item 被改动，缓存前缀会断裂"
    assert [i["meta"]["item_id"] for i in out[: len(items)]] == [
        i["meta"]["item_id"] for i in snapshot
    ], "item_id 顺序被改动"


def test_tail_messages_appends_user_keeps_system():
    plug = _plugin()
    messages = [
        {"role": "system", "content": HOST_SYSTEM},
        {"role": "user", "content": "你好"},
    ]
    out = plug._append_tail_user_message(messages, TEXT_A)

    assert [m["role"] for m in out] == ["system", "user", "user"]
    assert out[0]["content"] == HOST_SYSTEM, "system 内容不得变化"
    assert INJECT_MARKER in out[-1]["content"]


def test_tail_prefix_stable_when_song_changes():
    """换歌只改末条，前缀（system + 历史）必须完全一致 —— 本改动的全部收益。"""
    plug = _plugin()
    items = _host_items()

    first = plug._append_tail_user_item(items, TEXT_A)
    second = plug._append_tail_user_item(items, TEXT_B)

    n = len(items)
    assert first[:n] == second[:n], "换歌后前缀不一致，缓存会全量失效"
    assert first[-1]["parts"][0]["text"] != second[-1]["parts"][0]["text"]


def test_tail_does_not_mutate_input():
    plug = _plugin()
    items = _host_items()
    snapshot = copy.deepcopy(items)

    plug._append_tail_user_item(items, TEXT_A)

    assert items == snapshot, "不得就地改动调用方传入的 item 对象"


# ---------------------------------------------------------------- 配置分流

def test_illegal_position_falls_back_to_tail():
    plug = _plugin("garbage")
    assert plug._inject_position() == "tail"


def test_position_is_case_and_space_insensitive():
    plug = _plugin("  TAIL ")
    assert plug._inject_position() == "tail"
    plug.config.plugin.inject_position = " System "
    assert plug._inject_position() == "system"


def test_custom_system_mode_still_merges():
    """回退开关：system 模式下行为与 v2.14.x 一致。"""
    plug = _plugin("system")
    items = _host_items()

    out = plug._merge_system_into_items(items, TEXT_A)

    assert len(out) == len(items), "system 模式不应新增 item"
    assert INJECT_MARKER in out[0]["parts"][-1]["text"]


# ---------------------------------------------------------------- Hook 端到端

def test_replyer_hook_tail_appends_and_keeps_system_item():
    plug = _hit_plugin("tail")
    items = _host_items()
    snapshot = copy.deepcopy(items)

    res = asyncio.run(plug.inject_song_context(session_id="s1", items=items))
    modified = res.get("modified_kwargs") or {}

    out = modified["items"]
    assert len(out) == len(snapshot) + 1
    assert out[: len(snapshot)] == snapshot, "system/历史被改动，缓存前缀断裂"
    assert out[-1]["item_type"] == "UserMessageItem"
    assert INJECT_MARKER in out[-1]["parts"][0]["text"]
    # 仍然只有一条 system —— 单 system 纪律不受影响
    assert sum(1 for i in out if i.get("item_type") == "SystemMessageItem") == 1


def test_planner_hook_tail_appends_and_keeps_system_item():
    plug = _hit_plugin("tail")
    items = _host_items()
    snapshot = copy.deepcopy(items)

    res = asyncio.run(plug.inject_planner_context(session_id="s1", items=items))
    modified = res.get("modified_kwargs") or {}

    out = modified["items"]
    assert len(out) == len(snapshot) + 1
    assert out[: len(snapshot)] == snapshot
    assert INJECT_MARKER in out[-1]["parts"][0]["text"]


def test_replyer_hook_tail_messages_keeps_system():
    plug = _hit_plugin("tail")
    messages = [
        {"role": "system", "content": HOST_SYSTEM},
        {"role": "user", "content": "测试歌词"},
    ]
    snapshot = copy.deepcopy(messages)

    res = asyncio.run(plug.inject_song_context(session_id="s1", messages=messages))
    out = (res.get("modified_kwargs") or {})["messages"]

    assert out[: len(snapshot)] == snapshot
    assert out[-1]["role"] == "user"
    assert INJECT_MARKER in out[-1]["content"]


def test_tail_hook_is_idempotent():
    """已注入过的载荷必须原样放行，不能追加第二次。"""
    plug = _hit_plugin("tail")
    items = plug._append_tail_user_item(_host_items(), TEXT_A)

    res = asyncio.run(plug.inject_song_context(session_id="s1", items=items))

    assert "modified_kwargs" not in res


def test_tail_hook_reports_position_in_log(caplog):
    """注入日志必须报出实际位置，便于真机核对是否生效。"""
    plug = _hit_plugin("tail")
    with caplog.at_level(logging.INFO):
        asyncio.run(plug.inject_song_context(session_id="s1", items=_host_items()))

    # getMessage() 已完成 % 格式化，不要再叠加 record.args（会二次格式化）
    assert any("位置=tail" in r.getMessage() for r in caplog.records), "注入日志缺少位置标记"
