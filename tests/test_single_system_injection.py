# -*- coding: utf-8 -*-
"""单 system 纪律的回归测试（v2.13.1）。

Bug 现象：识别到歌曲后，注入逻辑**无条件追加**一条新的 system 消息，
而宿主请求里本来已有一条 system（人设/工具说明），于是「一个请求有两个
system」被供应商直接报错。

修复契约：
1. messages 里已有 system → 并进那一条，条目数不变，原内容不丢；
2. messages 里没有 system → 才新增一条，且全场只有一条 system；
3. items 里已有 SystemMessageItem → 并进它的 parts，item 数不变；
4. 不就地改动调用方传入的对象（返回新列表/新 dict）；
5. 端到端：inject_song_context / inject_planner_context 返回的载荷里
   system 数量恒为 1。
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
PLANNER_TEXT = f"{INJECT_MARKER}planner 测试载荷"
REPLY_TEXT = f"{INJECT_MARKER}replyer 测试载荷"


class _Ctx:
    def __init__(self):
        self.logger = logging.getLogger("test-single-system")


def _plugin():
    plug = _MOD.create_plugin()
    plug.set_plugin_config({})
    plug._ctx = _Ctx()
    return plug


def _hit_plugin():
    """装一条 TTL 内的歌词命中，让 _build_system_text 有输出。"""
    plug = _plugin()
    plug._hits = {"s1": deque([(time.time(), "测试歌词", "测试曲")], maxlen=20)}
    plug._meta_by_name = {}
    plug._song_record = lambda name: {}  # type: ignore[assignment]
    return plug


def _count_system(messages):
    return sum(
        1 for m in messages
        if isinstance(m, dict) and m.get("role") == "system"
    )


def _count_system_items(items):
    return sum(
        1 for i in items
        if isinstance(i, dict) and i.get("item_type") == "SystemMessageItem"
    )


# ---------------------------------------------------------------- 纯函数层

def test_merge_messages_reuses_existing_system():
    plug = _plugin()
    messages = [
        {"role": "system", "content": HOST_SYSTEM},
        {"role": "user", "content": "你好"},
    ]
    out = plug._merge_system_into_messages(messages, REPLY_TEXT)

    assert len(out) == 2, "并进已有 system 后条目数不应变化"
    assert _count_system(out) == 1
    assert HOST_SYSTEM in out[0]["content"], "宿主原 system 内容不能被覆盖"
    assert INJECT_MARKER in out[0]["content"]


def test_merge_messages_appends_when_absent():
    plug = _plugin()
    out = plug._merge_system_into_messages(
        [{"role": "user", "content": "你好"}], REPLY_TEXT
    )
    assert len(out) == 2
    assert _count_system(out) == 1
    assert out[-1]["role"] == "system"


def test_merge_messages_handles_list_content():
    """多模态 content（list of parts）要追加 text part，不能退化成字符串拼接。"""
    plug = _plugin()
    messages = [
        {"role": "system", "content": [{"type": "text", "text": HOST_SYSTEM}]},
    ]
    out = plug._merge_system_into_messages(messages, REPLY_TEXT)

    content = out[0]["content"]
    assert isinstance(content, list)
    assert _count_system(out) == 1
    assert any(
        isinstance(p, dict) and INJECT_MARKER in str(p.get("text") or "")
        for p in content
    )


def test_merge_messages_handles_empty_content():
    plug = _plugin()
    out = plug._merge_system_into_messages(
        [{"role": "system", "content": ""}], REPLY_TEXT
    )
    assert _count_system(out) == 1
    assert out[0]["content"] == REPLY_TEXT


def test_merge_messages_does_not_mutate_input():
    plug = _plugin()
    messages = [{"role": "system", "content": HOST_SYSTEM}]
    snapshot = copy.deepcopy(messages)

    plug._merge_system_into_messages(messages, REPLY_TEXT)

    assert messages == snapshot, "不得就地改动调用方传入的消息对象"


def test_merge_items_reuses_existing_system_item():
    plug = _plugin()
    items = [
        {
            "item_type": "SystemMessageItem",
            "meta": {"item_id": "host-sys", "logical_turn_id": None,
                     "timestamp": "2026-01-01T00:00:00"},
            "parts": [{"type": "text", "text": HOST_SYSTEM}],
        },
        {"item_type": "UserMessageItem",
         "meta": {"item_id": "u1", "logical_turn_id": None,
                  "timestamp": "2026-01-01T00:00:00"},
         "parts": [{"type": "text", "text": "你好"}]},
    ]
    out = plug._merge_system_into_items(items, REPLY_TEXT)

    assert len(out) == 2, "并进已有 SystemMessageItem 后 item 数不应变化"
    assert _count_system_items(out) == 1
    assert INJECT_MARKER in out[0]["parts"][-1]["text"]


def test_merge_items_appends_when_absent():
    plug = _plugin()
    out = plug._merge_system_into_items([], REPLY_TEXT)
    assert len(out) == 1
    assert _count_system_items(out) == 1


def test_merge_items_does_not_mutate_input():
    plug = _plugin()
    items = [
        {"item_type": "SystemMessageItem", "meta": {"item_id": "host-sys"},
         "parts": [{"type": "text", "text": HOST_SYSTEM}]},
    ]
    snapshot = copy.deepcopy(items)

    plug._merge_system_into_items(items, REPLY_TEXT)

    assert items == snapshot, "不得就地改动调用方传入的 item 对象"


# ---------------------------------------------------------------- Hook 端到端

def test_replyer_hook_keeps_single_system_in_messages():
    plug = _hit_plugin()
    messages = [
        {"role": "system", "content": HOST_SYSTEM},
        {"role": "user", "content": "测试歌词"},
    ]
    res = asyncio.run(plug.inject_song_context(session_id="s1", messages=messages))

    modified = res.get("modified_kwargs") or {}
    assert "messages" in modified
    assert _count_system(modified["messages"]) == 1, "修复后仍出现两条 system"
    assert len(modified["messages"]) == 2
    assert INJECT_MARKER in modified["messages"][0]["content"]


def test_replyer_hook_keeps_single_system_item():
    plug = _hit_plugin()
    items = [
        {"item_type": "SystemMessageItem", "meta": {"item_id": "host-sys"},
         "parts": [{"type": "text", "text": HOST_SYSTEM}]},
    ]
    res = asyncio.run(plug.inject_song_context(session_id="s1", items=items))

    modified = res.get("modified_kwargs") or {}
    assert "items" in modified
    assert _count_system_items(modified["items"]) == 1
    assert len(modified["items"]) == 1


def test_replyer_hook_appends_one_system_when_payload_has_none():
    plug = _hit_plugin()
    res = asyncio.run(
        plug.inject_song_context(session_id="s1", messages=[{"role": "user", "content": "测试歌词"}])
    )
    modified = res.get("modified_kwargs") or {}
    assert _count_system(modified["messages"]) == 1


def test_replyer_hook_is_idempotent():
    """已注入过的载荷必须原样放行，不能并第二次。"""
    plug = _hit_plugin()
    messages = [
        {"role": "system", "content": HOST_SYSTEM + "\n" + INJECT_MARKER},
        {"role": "user", "content": "测试歌词"},
    ]
    res = asyncio.run(plug.inject_song_context(session_id="s1", messages=messages))
    assert "modified_kwargs" not in res


def test_planner_hook_keeps_single_system_in_messages():
    plug = _hit_plugin()
    messages = [
        {"role": "system", "content": HOST_SYSTEM},
        {"role": "user", "content": "测试歌词"},
    ]
    res = asyncio.run(plug.inject_planner_context(session_id="s1", messages=messages))

    modified = res.get("modified_kwargs") or {}
    assert "messages" in modified
    assert _count_system(modified["messages"]) == 1
    assert len(modified["messages"]) == 2
