# -*- coding: utf-8 -*-
"""回复侧注入走宿主 extra_prompt 的契约测试（v2.15.1）。

背景（真机事故，2026-10-31）：v2.15.0 为了缓存友好，把回复侧注入从 system 改成
「往 items 末尾追加一条 UserMessageItem」。那条 item 没有任何宿主框语义，replyer
把它当成**最新的用户发言**，于是去回答歌词块、而不是回答真正说话的人。真机 thinking：

    User says lyric recognition block with lyric "这样下去的话" and instruct
    natural reply.

参考插件 life-frequency v1.25.3 的真机做法：回复侧只写 ``extra_prompt``，由宿主
折进「回复要求」。位置本来就在请求尾部（缓存前缀不受影响），语义由宿主框住。

修复契约：
1. 回复侧**只**写 extra_prompt，绝不改 items / messages；
2. 已有 extra_prompt 必须保留（与别的插件共享同一条通道），本文追加在其后；
3. 注入文本带 INJECT_MARKER，重复调用幂等；
4. 超预算截断自己的尾巴，不挤掉别人的内容；
5. 载荷里没有 extra_prompt 时告警并跳过——契约不匹配必须留痕。
6. 静态契约：回复侧不许再挂到 before_model_request（那个 hook 没有 extra_prompt）。
"""

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

INJECT_MARKER = _MOD.INJECT_MARKER
MAX_CHARS = _MOD._MAX_EXTRA_PROMPT_CHARS

OTHER_PLUGIN_PROMPT = "（别的插件的回复要求）今晚语气轻一点。"


class _Ctx:
    def __init__(self):
        self.logger = logging.getLogger("test-replyer-extra-prompt")


def _plugin(with_hit: bool = True):
    plug = _MOD.create_plugin()
    plug.set_plugin_config({})
    plug._ctx = _Ctx()
    if with_hit:
        plug._hits = {"s1": deque([(time.time(), "测试歌词", "测试曲")], maxlen=20)}
        plug._meta_by_name = {}
        plug._song_record = lambda name: {}  # type: ignore[assignment]
    return plug


def _call(**kwargs):
    plug = _plugin()
    return plug, asyncio.run(plug.inject_song_context(**kwargs))


# ---------------------------------------------------------------- 核心契约

def test_writes_only_extra_prompt():
    _, res = _call(session_id="s1", extra_prompt="", items=[], messages=[])

    modified = res.get("modified_kwargs") or {}
    assert set(modified) == {"extra_prompt"}, f"回复侧只许写 extra_prompt，实际写了 {sorted(modified)}"
    assert INJECT_MARKER in modified["extra_prompt"]


def test_does_not_touch_items_or_messages():
    """事故根因就藏在这里：v2.15.0 会改写 items。改了就会再犯。"""
    items = [{"item_type": "UserMessageItem", "meta": {"item_id": "u1"},
              "parts": [{"type": "text", "text": "你好"}]}]
    messages = [{"role": "system", "content": "人设"},
                {"role": "user", "content": "你好"}]

    _, res = _call(session_id="s1", extra_prompt="", items=items, messages=messages)
    modified = res.get("modified_kwargs") or {}

    assert "items" not in modified, "回复侧不得再追加 UserMessageItem —— 这就是误认消息的根因"
    assert "messages" not in modified
    assert "item_schema_version" not in modified


def test_keeps_existing_extra_prompt():
    """与别的插件共用同一条通道，不能把别人的内容顶掉。"""
    _, res = _call(session_id="s1", extra_prompt=OTHER_PLUGIN_PROMPT)

    merged = (res.get("modified_kwargs") or {})["extra_prompt"]
    assert merged.startswith(OTHER_PLUGIN_PROMPT), "已有 extra_prompt 被挤掉"
    assert INJECT_MARKER in merged
    assert merged.index(OTHER_PLUGIN_PROMPT) < merged.index(INJECT_MARKER)


def test_idempotent_by_marker():
    already = OTHER_PLUGIN_PROMPT + f"\n{INJECT_MARKER}已经注入过"
    _, res = _call(session_id="s1", extra_prompt=already)
    assert "modified_kwargs" not in res, "重复注入会叠两遍"


def test_no_hit_no_injection():
    plug = _plugin(with_hit=False)
    res = asyncio.run(plug.inject_song_context(session_id="s1", extra_prompt=""))
    assert "modified_kwargs" not in res


def test_missing_session_id_no_injection():
    plug = _plugin()
    res = asyncio.run(plug.inject_song_context(extra_prompt=""))
    assert "modified_kwargs" not in res


# ---------------------------------------------------------------- 预算

def test_budget_truncates_own_text_not_others():
    """预算紧张时截断自己的尾巴，别人的 prompt 一个字都不能动。"""
    long_other = "别" * (MAX_CHARS - 20)
    _, res = _call(session_id="s1", extra_prompt=long_other)

    merged = (res.get("modified_kwargs") or {})["extra_prompt"]
    assert merged.startswith(long_other), "别人的 prompt 被截断了"
    assert len(merged) <= MAX_CHARS


def test_budget_full_skips_without_clobbering():
    full = "别" * MAX_CHARS
    _, res = _call(session_id="s1", extra_prompt=full)
    assert "modified_kwargs" not in res, "预算已满时不得覆盖别人的 extra_prompt"


# ---------------------------------------------------------------- 留痕

def test_missing_extra_prompt_warns(caplog):
    """契约不匹配必须 WARNING：否则真机上只表现为「命中了但没注入」。"""
    plug = _plugin()
    with caplog.at_level(logging.WARNING):
        res = asyncio.run(plug.inject_song_context(session_id="s1", items=[]))

    assert "modified_kwargs" not in res
    assert any("extra_prompt" in r.getMessage() for r in caplog.records), caplog.text


def test_log_reports_channel_and_size(caplog):
    plug = _plugin()
    with caplog.at_level(logging.INFO):
        asyncio.run(plug.inject_song_context(session_id="s1", extra_prompt=""))

    assert any("extra_prompt" in r.getMessage() for r in caplog.records), "注入日志缺少通道名"


# ---------------------------------------------------------------- 静态契约

def test_replyer_hook_is_before_request_not_before_model_request():
    """before_model_request 的字段表里没有 extra_prompt，挂错 hook 会静默失效。

    只认**作为字符串字面量出现**的 hook 名（装饰器的实际注册值）；注释里提到它
    是允许的，否则等于禁止解释为什么不能用。
    """
    src = (_PLUGIN_DIR / "plugin.py").read_text(encoding="utf-8")

    assert '"maisaka.replyer.before_request"' in src
    assert '"maisaka.replyer.before_model_request"' not in src, (
        "回复侧又挂回 before_model_request 了 —— 那个 hook 没有 extra_prompt"
    )
