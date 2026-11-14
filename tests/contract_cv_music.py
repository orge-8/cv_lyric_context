# -*- coding: utf-8 -*-
"""cv_music 跨插件契约的测试侧加载器（提供方 cv_lyric_context）。

单一事实源 = devkit `contracts/cv_music_v1.json`（本机开发环境）。
插件仓库独立发布时 devkit 不在用户机器上，因此随仓库携带一份**镜像**
（tests/contracts/cv_music_v1.json）；devkit 存在时优先读 devkit，
并有一条守卫用例断言两份内容完全一致（见 test_contract_source.py）。

改契约的流程：先改 devkit 事实源 → 同步镜像 → 双侧测试同批更新。
"""

import json
import pathlib

_TESTS_DIR = pathlib.Path(__file__).resolve().parent
MIRROR_PATH = _TESTS_DIR / "contracts" / "cv_music_v1.json"
# Desktop/cv_lyric_context/tests -> Desktop/tools/maibot-devkit/contracts
DEVKIT_PATH = (
    _TESTS_DIR.parent.parent / "tools" / "maibot-devkit" / "contracts" / "cv_music_v1.json"
)


def contract_path() -> pathlib.Path:
    """当前生效的契约文件路径（devkit 优先，镜像兜底）。"""
    return DEVKIT_PATH if DEVKIT_PATH.is_file() else MIRROR_PATH


def load_contract() -> dict:
    return json.loads(contract_path().read_text(encoding="utf-8"))


def api_keys(api_name: str) -> set:
    """某 API 返回 dict 的完整键集。"""
    return set(load_contract()["apis"][api_name]["keys"])


def entry_keys(api_name: str) -> set:
    """某 API 返回列表元素的键集（仅 get_recent_songs 有）。"""
    return set(load_contract()["apis"][api_name]["entry_keys"])


def schema_version(api_name: str) -> int:
    return int(load_contract()["apis"][api_name]["schema_version"])


def match_enum() -> list:
    return list(load_contract()["apis"]["get_song_bilibili"]["match_enum"])


def timeout_ms() -> int:
    return int(load_contract()["timeout_ms"])


def sample_result(api_name: str, **overrides) -> dict:
    """按契约构造一个**形状完整**的样例返回（供消费方测试当假数据）。

    默认是「未命中」形态；overrides 覆盖任意字段。所有键都来自契约，
    杜绝手写假字典缺键导致的「契约漂移测试照样绿」。
    """
    spec = load_contract()["apis"][api_name]
    out: dict = {}
    for key, typ in spec["keys"].items():
        if key == "schema_version":
            out[key] = spec["schema_version"]
        elif typ == "bool":
            out[key] = False
        elif typ == "str":
            out[key] = ""
        elif typ == ["int", "null"]:
            out[key] = None
        elif typ == "list[str]":
            out[key] = []
        elif typ == "list[entry]":
            out[key] = []
        elif typ == "int":
            out[key] = 0
        elif typ == "float":
            out[key] = 0.0
        else:
            raise ValueError(f"契约里出现未知类型: {key}={typ!r}")
    out.update(overrides)
    return out


def validate_result(api_name: str, result: dict) -> None:
    """断言返回形状完全符合契约（键集、类型、枚举、schema_version）。"""
    spec = load_contract()["apis"][api_name]
    assert isinstance(result, dict), f"{api_name} 返回必须是 dict: {result!r}"
    assert set(result) == set(spec["keys"]), (
        f"{api_name} 键集漂移: 多={set(result) - set(spec['keys'])} "
        f"缺={set(spec['keys']) - set(result)}"
    )
    assert result["schema_version"] == spec["schema_version"], (
        f"{api_name} schema_version 漂移: {result['schema_version']}"
    )
    for key, typ in spec["keys"].items():
        value = result[key]
        if typ == "int":
            assert isinstance(value, int) and not isinstance(value, bool), (key, value)
        elif typ == "bool":
            assert isinstance(value, bool), (key, value)
        elif typ == "str":
            assert isinstance(value, str), (key, value)
        elif typ == ["int", "null"]:
            assert value is None or (isinstance(value, int) and not isinstance(value, bool)), (key, value)
        elif typ == "list[str]":
            assert isinstance(value, list) and all(isinstance(x, str) for x in value), (key, value)
        elif typ == "list[entry]":
            assert isinstance(value, list), (key, value)
            for item in value:
                assert set(item) == set(spec["entry_keys"]), (key, item)
                assert isinstance(item["name"], str) and isinstance(item["artist"], str)
                assert isinstance(item["at"], (int, float))
    if api_name == "get_song_bilibili":
        assert result["match"] in spec["match_enum"], f"match 越界: {result['match']!r}"