# -*- coding: utf-8 -*-
"""紧凑索引（v2.20.0 运行时内存优化）回归测试。

背景：dict[str, list[str]] 形态的子串/整句索引在真实库上占 ~300MB，
98% 的桶只有 1 首歌——大头是 Python 对象开销。CompactIndex 用
「键 blob + array 偏移 + 歌名 ID」压实，行为必须与 dict 版完全一致。

本文件守住：
1. from_mapping 压实后 get/items/len 与源 dict 全等（含桶序）；
2. setdefault 桶视图的 insert(0,...)/append 与 dict 版逐操作等价
   （_index_song_into / _add_subspans_into 的写入形态）；
3. __setitem__ 整体替换、clear、== {} / == dict 的测试契约；
4. 随机操作序列下与参考 dict 的逐步等价（模糊测试）。
"""
import importlib
import pathlib
import random
import sys

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from compact_index import CompactIndex  # noqa: E402


def _compact(d: dict) -> CompactIndex:
    names = sorted({s for bucket in d.values() for s in bucket})
    return CompactIndex.from_mapping(dict(d), names)


def test_compact_equals_source():
    d = {
        "并不喜欢吃鱼": ["我的悲伤是水做的"],
        "我最亲爱的朋": ["绝体绝命", "见字如面"],
        "亲爱的朋友请": ["光明的未来", "绝体绝命"],
        "abc": ["歌A", "歌B", "歌A"],  # 源里若有重复，压实去重
    }
    c = _compact(d)
    assert len(c) == len(d)
    for k, v in d.items():
        expect = list(dict.fromkeys(v))  # 去重保序
        assert c.get(k) == expect, k
        assert c[k] == expect, k
        assert k in c
    assert c.get("不存在") is None
    assert dict(c.items()) == {k: list(dict.fromkeys(v)) for k, v in d.items()}


def test_bucket_view_insert_matches_dict():
    """_index_song_into 的写入形态：setdefault + insert(0, name)。"""
    d = {"句一": ["旧歌"]}
    c = _compact(d)
    for name in ("新歌A", "旧歌", "新歌B"):
        db = d.setdefault("句一", [])
        cb = c.setdefault("句一", [])
        if name not in db:
            db.insert(0, name)
        if name not in cb:
            cb.insert(0, name)
    assert c.get("句一") == d["句一"] == ["新歌B", "新歌A", "旧歌"]
    # 新键走 insert 路径
    db = d.setdefault("句二", [])
    cb = c.setdefault("句二", [])
    db.insert(0, "某歌")
    cb.insert(0, "某歌")
    assert c.get("句二") == d["句二"] == ["某歌"]
    assert len(c) == len(d) == 2


def test_bucket_view_append_matches_dict():
    """关键词循环的写入形态：setdefault + append。"""
    d = {"片段": ["歌A"]}
    c = _compact(d)
    for name in ("歌B", "歌A", "歌C"):
        db = d.setdefault("片段", [])
        cb = c.setdefault("片段", [])
        if name not in db:
            db.append(name)
        if name not in cb:
            cb.append(name)
    assert c.get("片段") == d["片段"] == ["歌A", "歌B", "歌C"]


def test_setitem_replaces_whole_bucket():
    c = _compact({"k": ["旧歌"]})
    c["k"] = ["新歌"]
    assert c["k"] == ["新歌"]
    c["k2"] = ["另一首"]
    assert c["k2"] == ["另一首"]
    # 替换后再走 setdefault 追加
    b = c.setdefault("k", [])
    if "再加" not in b:
        b.append("再加")
    assert c["k"] == ["新歌", "再加"]


def test_eq_dict_and_empty():
    c = CompactIndex()
    assert c == {}
    assert not c
    c["x"] = ["歌"]
    assert c == {"x": ["歌"]}
    assert c != {"x": ["别的歌"]}
    assert c
    c.clear()
    assert c == {}
    assert len(c) == 0


def test_clear_releases_and_reusable():
    c = _compact({"a": ["歌A"], "b": ["歌B"]})
    c.clear()
    assert c.get("a") is None
    b = c.setdefault("a", [])
    b.insert(0, "新歌")
    assert c.get("a") == ["新歌"]


def test_fuzz_against_dict():
    """随机操作序列：每一步之后与参考 dict 全量等价。"""
    rng = random.Random(20261115)
    keys = [f"歌词片段{i:03d}" for i in range(40)]
    songs = [f"歌{i}" for i in range(12)]
    d: dict = {}
    c = CompactIndex.from_mapping({}, [])
    for step in range(600):
        op = rng.random()
        k = rng.choice(keys)
        s = rng.choice(songs)
        if op < 0.5:
            db = d.setdefault(k, [])
            cb = c.setdefault(k, [])
            if s not in db:
                db.insert(0, s)
            if s not in cb:
                cb.insert(0, s)
        elif op < 0.8:
            db = d.setdefault(k, [])
            cb = c.setdefault(k, [])
            if s not in db:
                db.append(s)
            if s not in cb:
                cb.append(s)
        elif op < 0.9:
            v = [s, rng.choice(songs)]
            d[k] = v
            c[k] = v
        else:
            # 只读分支：契约不含 pop（运行时索引只增不删，清空走 clear）
            assert c.get(k) == d.get(k), (step, k)
        if step % 50 == 0:
            assert len(c) == len(d), (step, len(c), len(d))
            for kk in d:
                assert c.get(kk) == d[kk], (step, kk)
    assert dict(c.items()) == d


def test_source_untouched():
    """from_mapping 只读源 dict：调用方持有的桶引用不被污染。"""
    d = {"a": ["歌A"], "b": ["歌B"]}
    bucket_a = d["a"]
    c = CompactIndex.from_mapping(d, ["歌A", "歌B"])
    assert d == {"a": ["歌A"], "b": ["歌B"]}
    assert bucket_a == ["歌A"]
    assert c.get("a") == ["歌A"]


def test_unknown_names_get_ids():
    """names 表之外的歌名（运行时增量）自动分配新 ID。"""
    c = CompactIndex.from_mapping({"k": ["表外歌"]}, ["表内歌"])
    assert c.get("k") == ["表外歌"]
    b = c.setdefault("k", [])
    b.append("表内歌")
    assert c.get("k") == ["表外歌", "表内歌"]
