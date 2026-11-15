# -*- coding: utf-8 -*-
"""自定义 WebUI 页面（webui.json 协议 v2，custom 模式）契约测试。

v2.19.0 起歌词页从声明式改为 custom HTML（``webui/lyrics.html``，视觉参考
Mai_life 的设计语言，代码全部原创）。覆盖四条链路，缺一条真机上就可能是
「页面整块消失」或「点了按钮 502」：

1. 声明必须过宿主自己的 ``load_webui_extension``（extra=forbid 严格模型，
   custom 页的 entry 必须指向插件内真实 HTML，document 由宿主读取注入）；
2. 页面绑定的每个 ``api`` 都必须在 ``get_components()`` 里静态注册（宿主对 dynamic 返 404）；
3. API 返回值必须过宿主 ``validate_json``（≤512 KiB / ≤12 层 / ≤20000 元素）；
4. HTML 里 ``query()/action()`` 用到的绑定名必须与 webui.json 声明一致
   （custom 页没有声明式节点引用校验，拼错绑定名只会在真机上 502）。

外加本插件特有的两条纪律：

- **条数参数一律声明成 string**：与 ``_webui_int`` 的字符串兜底口径保持一致，
  页面 JS 也统一传字符串，避免 integer 严格校验（``type(value) is int``）踩布尔/浮点坑；
- **触发类操作不做「不限条数」**：命令侧 0 表示全量，页面继承这个语义就成了「误点一下
  跑几小时」，所以 ``_webui_resolve_limit`` 必须永远给出有限值。
"""

import asyncio
import importlib
import json
import logging
import os
import pathlib
import re
import sys

import pytest

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(_PLUGIN_DIR), str(_PLUGIN_DIR.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_MOD = importlib.import_module(_PLUGIN_DIR.name)
if not hasattr(_MOD, "create_plugin"):
    _MOD = importlib.import_module(f"{_PLUGIN_DIR.name}.plugin")

from vcpedia_store import SongStore  # noqa: E402

# 宿主检出目录：默认只放占位示例，不写任何真实用户名/部署布局。
# 实际运行前用环境变量 MAIBOT_HOST_DIR 指向自己的 MaiBot 检出；找不到会 skip。
_DEFAULT_HOST = "C:/Users/<USER>/path/to/MaiBot"
#: 页面上由输入框喂值的参数，必须声明成 string（见模块 docstring）
_STRING_ONLY_PARAMS = {"limit", "skip_days"}


def _host_schema_module():
    host_dir = os.environ.get("MAIBOT_HOST_DIR") or _DEFAULT_HOST
    root = pathlib.Path(host_dir)
    if not (root / "src" / "plugin_runtime" / "webui_schema.py").exists():
        pytest.skip(f"未找到 MaiBot 宿主检出: {root}")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        return importlib.import_module("src.plugin_runtime.webui_schema")
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"无法加载宿主 schema: {exc}")


def _extension():
    return _host_schema_module().load_webui_extension(str(_PLUGIN_DIR))


class _Ctx:
    def __init__(self) -> None:
        self.logger = logging.getLogger("test-webui-pages")


def _plugin(tmp_path, *, songs=(), allow_sync=True, enabled=True):
    """最小装配：真 SongStore（临时目录）+ 命中记录 + 默认配置。"""
    plug = _MOD.create_plugin()
    plug.set_plugin_config({})
    plug._ctx = _Ctx()
    plug._store = SongStore(tmp_path / "vcpedia_songs.db")
    plug._store.count()  # 触发建表
    for name, lyrics in songs:
        plug._store.upsert({"name": name, "lyrics": lyrics})
    plug.config.plugin.enabled = enabled
    plug.config.crawler.allow_sync_command = allow_sync
    plug._sync_task = None
    plug._sync_stats = None
    plug._webui_task_kind = ""
    plug._refetch_stop = False
    if hasattr(plug, "_recent_songs"):
        from recent_songs import RecentSongsLog

        plug._recent_songs = RecentSongsLog(str(tmp_path))
    return plug


# ------------------------------------------------------------------- 声明校验

def test_declaration_loads_under_host_schema():
    ext = _extension()
    assert ext.schema_version == 2
    assert ext.required_capabilities == ["custom_pages"]
    page = ext.pages[0]
    # 1.4.0 起所有插件页面统一收进设置侧栏「插件扩展」分组（Layout.tsx:106-118）；
    # placement:"workspace" 已停用（宿主归一化 + UserWarning），声明里必须删掉，
    # 归一化后恒为 sidebar。workspace_title 仅作手机端分组标题。
    assert page.placement == "sidebar"
    raw = json.loads((_PLUGIN_DIR / "webui.json").read_text(encoding="utf-8"))
    assert "placement" not in raw["pages"][0], "1.4.x 已停用 placement，别再写 workspace"
    assert (ext.workspace_title or "").strip(), "手机端分组标题仍用 workspace_title"
    assert page.icon == "database"
    # custom 页契约：entry 指向插件内 HTML，document 由宿主读取注入（非空）
    assert page.mode == "custom"
    assert page.entry == "webui/lyrics.html"
    assert page.layout == "document"
    assert page.document.strip(), "宿主必须能从 entry 读到非空 HTML"
    assert "document" not in raw["pages"][0], "webui.json 不能内联 document（宿主直接拒绝）"


def test_custom_page_polls_task_progress():
    """custom 页没有声明式的 poll_interval_seconds，轮询写在页面 JS 里。
    「触发后台任务 + 看进度」型页面必须有 ≥3 秒的 setInterval（宿主对声明式
    页面的下限是 3 秒，这里沿用同一口径），否则任务进度只能手动刷新。"""
    html = (_PLUGIN_DIR / "webui" / "lyrics.html").read_text(encoding="utf-8")
    # 回调体里可能含逗号（如 () => { a(); b(); }），用贪婪匹配取最后一个数字参数
    intervals = [int(ms) for ms in re.findall(r"setInterval\(.*?,\s*(\d+)\s*\)", html)]
    assert intervals, "页面 JS 必须有 setInterval 轮询任务进度"
    assert min(intervals) >= 3000, f"轮询间隔不得小于 3 秒: {intervals}"
    assert "window.maibot" in html and ".ready" in html, "必须等宿主桥接就绪后再发请求"


def test_custom_page_respects_host_constraints():
    """宿主 CSP 零网络 + 并发闸 2：页面不得引外部资源，必须自建 ≤2 的调度。"""
    html = (_PLUGIN_DIR / "webui" / "lyrics.html").read_text(encoding="utf-8")
    assert not re.search(r"(?:src|href)\s*=\s*[\"']https?://", html), "CSP 禁止外部资源"
    assert "@import" not in html and "url(http" not in html, "CSP 禁止外部样式/字体"
    assert re.search(r"active\s*<\s*2", html), "必须有并发 ≤2 的请求调度（宿主 pending>=2 直接 throw）"


def test_html_bindings_match_declaration():
    """custom 页没有声明式节点引用校验：JS 里 query()/action() 的绑定名
    必须全部在 webui.json 声明，否则真机上一点就 502。"""
    page = _extension().pages[0]
    declared = set(page.queries) | set(page.actions)
    html = (_PLUGIN_DIR / "webui" / "lyrics.html").read_text(encoding="utf-8")
    used = set(re.findall(r"\b(?:query|action)\(\s*[\"']([a-zA-Z_][a-zA-Z0-9_]*)[\"']", html))
    assert used, "页面 JS 应至少调用一个绑定"
    unknown = used - declared
    assert not unknown, f"JS 调用了未声明的绑定: {sorted(unknown)}"
    # 反向：声明的读绑定至少都被页面用到（写绑定允许只被按钮触发函数间接调用）
    unused_queries = set(page.queries) - used
    assert not unused_queries, f"声明了却没被页面使用的 query: {sorted(unused_queries)}"


def test_manifest_declares_capability():
    manifest = json.loads((_PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
    assert "custom_pages" in (manifest.get("capabilities") or [])


def test_queries_have_no_arguments_and_optional_params():
    page = _extension().pages[0]
    for name, binding in page.queries.items():
        binding.validate_args({})  # 一个参数都不传也要能过
        assert not binding.confirmation, f"query {name} 不得声明 confirmation"
        # custom 页的 APIBinding 没有 arguments 字段（那是声明式 DeclarativeBinding
        # 的表单引用机制）；这里断言它不存在，防止有人把声明式写法混进来。
        assert not hasattr(binding, "arguments"), f"query {name} 不得带声明式 arguments"


def test_numeric_params_declared_as_string():
    """条数参数统一 string：页面 JS 传字符串，插件侧 _webui_int 兜底转换，
    避开宿主 integer 严格校验（type(value) is int）的布尔/浮点坑。"""
    page = _extension().pages[0]
    seen = set()
    for group in (page.queries, page.actions):
        for binding in group.values():
            for pname, parameter in binding.parameters.items():
                if pname in _STRING_ONLY_PARAMS:
                    seen.add(pname)
                    assert parameter.type == "string", (
                        f"参数 {pname} 必须声明为 string（页面输入框只能给字符串），"
                        "数字转换交给插件侧 _webui_int"
                    )
    assert seen, "页面应至少有一处由输入框喂值的条数参数"


def test_bound_apis_are_registered():
    # 必须用文件顶部加载的 _MOD（命名空间包），不能 `from plugin import ...`：
    # test_e2e_cv_music.py 会把对端 music-request 目录插到 sys.path 最前，
    # 裸 `plugin` 会解析到对端的 plugin.py，9 个 API 全部「未注册」（假回归）。
    components = {
        (item.get("name"), str(item.get("metadata", {}).get("version", "1")))
        for item in _MOD.create_plugin().get_components()
        if item.get("type") == "API"
        and item.get("metadata", {}).get("enabled") is True
        and item.get("metadata", {}).get("dynamic") is not True
    }
    page = _extension().pages[0]
    bound = [(b.api, b.version) for b in page.queries.values()]
    bound += [(b.api, b.version) for b in page.actions.values()]
    missing = [pair for pair in bound if pair not in components]
    assert not missing, f"页面绑定了插件没注册的 API: {missing}"


def test_danger_action_has_confirmation():
    page = _extension().pages[0]
    cancel = page.actions["cancel"]
    assert cancel.confirmation, "取消任务是破坏性操作，必须带 confirmation"


# ------------------------------------------------------------------- 读 API

def test_read_apis_survive_host_json_guard(tmp_path):
    schema = _host_schema_module()
    plug = _plugin(tmp_path, songs=[("测试曲", "第一行歌词\n第二行歌词"), ("没词曲", "")])
    payloads = [
        asyncio.run(plug.api_stats()),
        asyncio.run(plug.api_search(keyword="测试")),
        asyncio.run(plug.api_search(keyword="歌词", scope="lyrics")),
        asyncio.run(plug.api_empty()),
        asyncio.run(plug.api_recent()),
        asyncio.run(plug.api_task()),
        asyncio.run(plug.api_song(name="测试曲")),
    ]
    for payload in payloads:
        schema.validate_json(payload, max_bytes=524288)


def test_stats_counts_and_coverage(tmp_path):
    plug = _plugin(tmp_path, songs=[("甲", "有词"), ("乙", ""), ("丙", "也有词")])
    stats = asyncio.run(plug.api_stats())
    assert stats["ready"] is True
    assert stats["total"] == 3
    assert stats["empty_lyrics"] == 1
    assert stats["coverage_percent"] == pytest.approx(66.7, abs=0.1)


def test_search_scope_switch(tmp_path):
    plug = _plugin(tmp_path, songs=[("甲乙", "歌词里有丙丁")])
    by_name = asyncio.run(plug.api_search(keyword="甲乙"))
    by_lyrics = asyncio.run(plug.api_search(keyword="丙丁", scope="lyrics"))
    assert [row["name"] for row in by_name["rows"]] == ["甲乙"]
    assert [row["name"] for row in by_lyrics["rows"]] == ["甲乙"]
    assert by_name["rows"][0]["has_lyrics"] is True
    assert "preview" in by_name["rows"][0], "行内要带预览，弹窗才能只吃 selection"
    # 空关键词不发查询、返回空列表（页面加载时会以空值跑一次）
    empty = asyncio.run(plug.api_search(keyword=""))
    assert empty["rows"] == []


def test_empty_rows_are_dicts(tmp_path):
    """table 节点按 columns[].field 取值：纯字符串列表渲染不出列。"""
    plug = _plugin(tmp_path, songs=[("甲", ""), ("乙", "有词")])
    result = asyncio.run(plug.api_empty())
    assert result["total"] == 1
    assert result["rows"] and isinstance(result["rows"][0], dict)
    assert result["rows"][0]["name"] == "甲"


def test_store_unavailable_degrades(tmp_path):
    plug = _plugin(tmp_path)
    plug._store = None
    stats = asyncio.run(plug.api_stats())
    assert stats["ready"] is False and "歌曲库" in stats["reason"]
    search = asyncio.run(plug.api_search(keyword="任意"))
    assert search["rows"] == [] and search["ready"] is False


# ------------------------------------------------------------------- 写 API

def test_trigger_respects_config_switch(tmp_path):
    plug = _plugin(tmp_path, allow_sync=False)
    result = asyncio.run(plug.api_start_sync(limit="10"))
    assert result["ok"] is False and "关闭" in result["reason"]
    disabled = _plugin(tmp_path, enabled=False)
    assert asyncio.run(disabled.api_start_sync())["ok"] is False


def test_trigger_is_mutually_exclusive(tmp_path):
    plug = _plugin(tmp_path)

    async def _scenario():
        # 占位一个「在跑」的任务：用永不结束的协程模拟
        plug._sync_task = asyncio.ensure_future(asyncio.sleep(60))
        try:
            return await plug.api_start_refetch(limit="5"), await plug.api_cancel()
        finally:
            plug._sync_task.cancel()

    started, cancelled = asyncio.run(_scenario())
    assert started["ok"] is False and "已有" in started["reason"]
    assert cancelled["ok"] is True


def test_trigger_resets_refetch_stop(tmp_path):
    """命令侧每次起任务都复位 _refetch_stop；页面不复位会让补歌词立刻自停。"""
    plug = _plugin(tmp_path)
    plug._refetch_stop = True
    plug.config.crawler.sync_batch_limit = 0

    async def _scenario():
        result = await plug.api_start_refetch(limit="5")
        plug._sync_task.cancel()
        return result

    result = asyncio.run(_scenario())
    assert result["ok"] is True
    assert plug._refetch_stop is False


def test_resolve_limit_never_unbounded(tmp_path):
    plug = _plugin(tmp_path)
    plug.config.crawler.sync_batch_limit = 0
    size, note = plug._webui_resolve_limit("")
    assert size == 50 and note, "配置为不限时必须退回有限值并说明"
    assert plug._webui_resolve_limit("99999")[0] == 500, "必须夹到 _MAX_SYNC_LIMIT"
    assert plug._webui_resolve_limit("0")[0] == 50
    plug.config.crawler.sync_batch_limit = 30
    assert plug._webui_resolve_limit("")[0] == 30


def test_cancel_without_task_reports_reason(tmp_path):
    plug = _plugin(tmp_path)
    result = asyncio.run(plug.api_cancel())
    assert result["ok"] is False and "没有在跑" in result["reason"]
