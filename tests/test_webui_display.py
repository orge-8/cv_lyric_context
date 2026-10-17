# -*- coding: utf-8 -*-
"""WebUI 配置 Schema 显示层回归测试：走插件实例的真实链路。

真机 Runner 调的就是 ``plugin.get_webui_config_schema``（异常被吞 →
空 Schema → 配置页整页空白），所以这里必须走插件实例，而不是直接调
SDK 生成器。
"""
import importlib
import pathlib
import sys

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR.parent))

_MOD = importlib.import_module(_PLUGIN_DIR.name)
if not hasattr(_MOD, "create_plugin"):
    # 包式加载（与真机一致）：__init__.py 可能只是引导文件，需下沉到 .plugin 子模块
    _MOD = importlib.import_module(f"{_PLUGIN_DIR.name}.plugin")
_PLUGIN = _MOD.create_plugin()


def _webui_schema() -> dict:
    return _PLUGIN.get_webui_config_schema(
        plugin_id=getattr(_MOD, "__plugin_id__", _PLUGIN_DIR.name),
        plugin_name=_PLUGIN_DIR.name,
        plugin_version="",
        plugin_description="",
        plugin_author="",
    ) or {}


def _webui_sections() -> dict:
    sections = _webui_schema().get("sections") or {}
    assert sections, "schema 无 sections：Runner 会渲染出整页空白"
    return sections


def test_sections_have_visible_fields():
    sections = _webui_sections()
    assert any(
        not f.get("hidden")
        for s in sections.values()
        for f in (s.get("fields") or {}).values()
    ), "所有字段均被隐藏，配置页会空白"


def test_no_object_fields():
    for s in _webui_sections().values():
        for f in (s.get("fields") or {}).values():
            assert not (f.get("type") == "object" or f.get("ui_type") == "json"), (
                "嵌套对象落 default 文本框会显示 [object Object]"
            )


def test_description_also_visible_as_hint():
    for s in _webui_sections().values():
        for f in (s.get("fields") or {}).values():
            if f.get("hidden"):
                continue
            if (f.get("description") or "").strip():
                assert (f.get("hint") or "").strip(), (
                    f"字段 {f.get('name')} 的说明在可视化模式不可见（需抄进 hint）"
                )


def test_labels_not_raw_keys():
    for s in _webui_sections().values():
        for fname, f in (s.get("fields") or {}).items():
            if f.get("hidden"):
                continue
            label = (f.get("label") or "").strip()
            assert label and label != fname, f"字段 {fname} 仍以英文键名当 label"


def test_section_titles_not_raw_keys():
    for name, s in _webui_sections().items():
        title = (s.get("title") or "").strip()
        assert title and title != name, f"节 {name} 仍以键名当标题"


def test_config_version_hidden_if_present():
    for s in _webui_sections().values():
        for fname, f in (s.get("fields") or {}).items():
            if fname == "config_version":
                assert f.get("hidden") is True, "config_version 应只在源代码模式可见"


def test_labels_have_no_broken_truncation():
    """CJK 硬切容易留下 dangling 括号或「的/是否」收尾，一律视为坏 label。"""
    bad_tail = ("的", "是否", "和", "与", "「", "（", "(", "【", "，", ",", "、")
    for s in _webui_sections().values():
        for fname, f in (s.get("fields") or {}).items():
            if f.get("hidden"):
                continue
            label = (f.get("label") or "").strip()
            assert len(label) <= 20, f"字段 {fname} 的 label 过长（{len(label)} 字）会被前端截断: {label}"
            assert not label.endswith(bad_tail), f"字段 {fname} 的 label 断句不完整: {label}"


def test_format_fields_have_placeholder():
    """格式敏感的文本字段必须给占位示例（灰字提示，不改校验语义）。"""
    required = {"meme_phrases", "extra_song_dbs", "categories", "sync_admin_ids"}
    seen = set()
    for s in _webui_sections().values():
        for fname, f in (s.get("fields") or {}).items():
            if fname in required:
                seen.add(fname)
                assert (f.get("placeholder") or "").strip(), f"字段 {fname} 缺 placeholder 示例"
    assert seen == required, f"配置节里找不到这些字段: {required - seen}"


def test_auxiliary_sections_collapsed():
    """主配置节与权限节保持展开；选歌/标注/联动等配套节默认收起。"""
    sections = _webui_sections()
    assert sections["plugin"].get("collapsed") is not True
    # crawler 里放着「管理员 QQ 号」——新用户第一个要配的开关，折叠起来等于藏起来
    assert sections["crawler"].get("collapsed") is not True, (
        "crawler 独占「同步与权限」页签且含管理员配置，不应默认收起"
    )
    for name in ("recommend", "emotion", "integration"):
        assert sections[name].get("collapsed") is True, f"配套节 {name} 应默认收起"


def test_tabs_cover_every_section():
    """tabs 布局下前端只渲染 tab.sections 里的节，漏一个就等于该节整块消失。"""
    schema = _webui_schema()
    layout = schema.get("layout") or {}
    if layout.get("type") != "tabs":
        return  # 旧宿主走 auto 平铺，不会丢节
    tabs = layout.get("tabs") or []
    assert tabs, "layout 声明为 tabs 却没有任何页签"
    listed = [name for tab in tabs for name in (tab.get("sections") or [])]
    assert sorted(listed) == sorted(schema["sections"]), (
        f"页签未覆盖全部配置节：遗漏 {set(schema['sections']) - set(listed)}"
    )
    assert len(listed) == len(set(listed)), f"同一节被多个页签重复引用: {listed}"
    for tab in tabs:
        assert tab.get("id") and tab.get("title"), f"页签缺 id/title: {tab}"


def test_admin_fields_ranked_first():
    """权限三项必须排在被它们约束的爬取参数之前（用户反馈曾找了很久）。"""
    crawler = _webui_sections()["crawler"]
    ordered = [
        name
        for name, field in sorted(
            crawler["fields"].items(), key=lambda kv: kv[1].get("order") or 0
        )
        if not field.get("hidden")
    ]
    assert ordered[:3] == ["allow_sync_command", "sync_admin_ids", "allow_local_operator"], (
        f"权限字段未排在最前: {ordered[:5]}"
    )


def test_admin_field_speaks_the_user_language():
    """字段文案要能被「我想加管理员」的直觉检索到，而不是内部术语。"""
    crawler = _webui_sections()["crawler"]
    field = crawler["fields"]["sync_admin_ids"]
    assert "管理员" in field["label"], f"label 未出现「管理员」: {field['label']}"
    hint = field["hint"]
    assert "管理员" in hint and "留空" in hint, "hint 须说明这是管理员配置且留空即禁用"
    assert "只读" in hint, "hint 须说明只读命令不受限制，避免用户以为全都锁了"
    # 节标题要能指向权限，否则用户不会点进「爬取」里找
    assert "权限" in crawler["title"], f"节标题未体现权限: {crawler['title']}"
