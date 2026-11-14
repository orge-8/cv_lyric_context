# -*- coding: utf-8 -*-
"""契约单一事实源守卫（v2.19.0，联动加固 P0-A）。

纪律：
1. devkit 事实源存在时，插件内镜像必须与之**逐字节一致**——
   改契约只改 devkit 再同步镜像，不允许只改一侧；
2. 契约文件本身结构合法（键集/类型/枚举声明齐全）；
3. 提供方两个 API 的 @API 声明与契约一致（api_version / timeout_ms）。
"""

import importlib
import pathlib
import sys

import pytest

_TESTS_DIR = pathlib.Path(__file__).resolve().parent
_PLUGIN_DIR = _TESTS_DIR.parent
for _p in (str(_PLUGIN_DIR), str(_PLUGIN_DIR.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import contract_cv_music as contract  # noqa: E402


def test_mirror_matches_devkit_source():
    """devkit 事实源与仓库镜像必须完全一致（devkit 不在时跳过）。"""
    if not contract.DEVKIT_PATH.is_file():
        pytest.skip("devkit 事实源不在本机（独立发布环境），仅镜像生效")
    assert contract.MIRROR_PATH.read_bytes() == contract.DEVKIT_PATH.read_bytes(), (
        "契约镜像与 devkit 事实源不一致：先改 devkit contracts/cv_music_v1.json，"
        "再同步两侧 tests/contracts/ 镜像"
    )


def test_contract_structure():
    spec = contract.load_contract()
    assert spec["contract"] == "cv_music"
    assert spec["provider_plugin_id"] == "org.mai-mai.cv-lyric-context"
    assert spec["consumer_plugin_id"] == "github.orge-8.music-request"
    assert set(spec["apis"]) == {"get_song_bilibili", "get_recent_songs"}
    assert contract.timeout_ms() == 3000
    assert set(contract.match_enum()) == {"", "exact", "base", "prefix"}


def test_api_declarations_match_contract():
    """@API 装饰器声明的 version / timeout_ms 必须与契约一致。

    直接读组件注册元数据（get_components），不依赖 Host。
    """
    mod = importlib.import_module(_PLUGIN_DIR.name)
    if not hasattr(mod, "create_plugin"):
        mod = importlib.import_module(f"{_PLUGIN_DIR.name}.plugin")
    plug = mod.create_plugin()
    apis = {
        item["name"]: item
        for item in plug.get_components()
        if str(item.get("type", "")).upper() == "API"
    }
    spec = contract.load_contract()
    for name, api_spec in spec["apis"].items():
        assert name in apis, f"缺少 @API 声明: {name}"
        meta = apis[name].get("metadata") or {}
        assert str(meta.get("version")) == api_spec["api_version"], name
        assert meta.get("public") is True, f"{name} 必须 public=True 才能跨插件调用"
        assert int(meta.get("timeout_ms", 0)) == contract.timeout_ms(), (
            f"{name} 的 timeout_ms 声明与契约不一致（宿主默认 60s，必须显式收紧）"
        )