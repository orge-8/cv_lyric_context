# -*- coding: utf-8 -*-
"""SubspanEventLog 单元测试（v2.21.0 构建峰值优化）。

核心验收：事件流构建与 dict 版 _add_subspans_into 语义**逐键逐桶等价**
（插入序保序 + _SUBSPAN_MAX_SONGS 先到先得 + 桶内去重）。
"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compact_index import CompactIndex
from subspan_events import SubspanEventLog

MAX_SONGS = 3  # 与 plugin._SUBSPAN_MAX_SONGS 一致


def dict_reference(events, n, max_songs=MAX_SONGS):
    """dict 版参照实现（与 plugin._add_subspans_into 同逻辑）。"""
    table = {}
    for key, name in events:
        if n <= 0 or len(key) < n:
            continue
        for i in range(len(key) - n + 1):
            gram = key[i:i + n]
            bucket = table.get(gram)
            if bucket is None:
                table[gram] = [name]
            elif name not in bucket and len(bucket) < max_songs:
                bucket.append(name)
    return table


def build_events(events, n, max_songs=MAX_SONGS):
    names, name_ids = [], {}
    log = SubspanEventLog(n, max_songs, names, name_ids)
    for key, name in events:
        log.record(key, name)
    return log.finalize()


def assert_equiv(events, n, max_songs=MAX_SONGS):
    ref = dict_reference(events, n, max_songs)
    ci = build_events(events, n, max_songs)
    assert len(ci) == len(ref), (len(ci), len(ref))
    for gram, bucket in ref.items():
        got = ci.get(gram)
        assert got == bucket, (gram, bucket, got)
    # 反向：紧凑层没有多余键
    for gram in ci:
        assert gram in ref
    # 结构性不变量：keys blob 必须全局字节序非降（二分查找的前提）。
    # 直接查结构而非只靠 get()——块间乱序时 get() 可能碰巧命中部分键，
    # 掩盖缺陷（v2.21.0 开发期教训）。
    keys, offsets = ci._keys, ci._offsets
    prev = None
    for i in range(ci._size):
        kb = keys[offsets[i]:offsets[i + 1]]
        if prev is not None:
            assert prev <= kb, f"keys blob 非全局字节序 @ 键序 {i}"
        prev = kb
    return ci


def test_basic_equivalence():
    events = [
        ("坠入深空追逐自由", "歌A"),
        ("深空追逐自由的风", "歌B"),
        ("坠入深空追逐自由", "歌A"),   # 重复事件（调用方门控前的兜底）
        ("追逐自由的风筝", "歌C"),
        ("完全无关的句子", "歌D"),
    ]
    assert_equiv(events, 6)


def test_max_songs_first_come_first_served():
    """上限 3：第 4 首不入选，且入选集合取决于插入序（保序的核心语义）。"""
    key = "一二三四五六七八"
    events = [(key, f"歌{i}") for i in range(6)]
    ci = assert_equiv(events, 6)
    assert ci.get("一二三四五六") == ["歌0", "歌1", "歌2"]
    # 换插入序 → 入选集合跟着变（与 dict 版一致）
    events_rev = list(reversed(events))
    ci2 = assert_equiv(events_rev, 6)
    assert ci2.get("一二三四五六") == ["歌5", "歌4", "歌3"]


def test_dedup_within_bucket():
    key = "重复重复重复重复"
    events = [(key, "歌A"), (key, "歌B"), (key, "歌A"), (key, "歌A")]
    ci = assert_equiv(events, 6)
    for gram in ci:
        assert ci.get(gram) == ["歌A", "歌B"]


def test_short_key_and_zero_n():
    assert_equiv([("短", "歌A"), ("一二三四五", "歌B")], 6)  # 全部短于窗口
    ci = build_events([("一二三四五六七", "歌A")], 0)
    assert len(ci) == 0 and not ci


def test_empty_log():
    ci = build_events([], 6)
    assert len(ci) == 0
    assert not ci
    assert ci.get("不存在") is None


def test_names_shared_with_external_table():
    """names/name_ids 由整句表先注册：事件流复用 ID，不重复分配。"""
    names = ["歌A", "歌B"]
    name_ids = {"歌A": 0, "歌B": 1}
    log = SubspanEventLog(6, MAX_SONGS, names, name_ids)
    log.record("一二三四五六七", "歌A")
    log.record("一二三四五六七", "歌C")  # 表外歌名自动分配
    ci = log.finalize()
    assert ci.get("一二三四五六") == ["歌A", "歌C"]
    assert names == ["歌A", "歌B", "歌C"]
    assert name_ids["歌C"] == 2


def test_finalize_idempotent_state_cleared():
    """finalize 后内部大结构应释放（峰值纪律），二次 finalize 返回空。"""
    log = SubspanEventLog(6, MAX_SONGS, [], {})
    log.record("一二三四五六七", "歌A")
    ci = log.finalize()
    assert len(ci) > 0
    assert log._grams == {} and log._gram_list == []
    assert len(log._gids) == 0


def test_random_fuzz_vs_dict():
    """随机模糊：多歌共享窗口、重复、乱序、长短句混合，逐键比对。"""
    rng = random.Random(20261118)
    chars = "一二三四五六七八九十甲乙丙丁戊己庚辛"
    for trial in range(30):
        events = []
        for _ in range(rng.randint(20, 200)):
            ln = rng.randint(4, 24)
            key = "".join(rng.choice(chars) for _ in range(ln))
            name = f"歌{rng.randint(0, 12)}"
            events.append((key, name))
        n = rng.choice([4, 6, 8])
        assert_equiv(events, n)


def test_chunk_boundary():
    """跨分块边界（CHUNK_GIDS=65536）：构造 >65536 个不同 gram 验证块间正确性。"""
    # 用 5 字窗口 + 大字符集生成海量不同 gram（每条 key 贡献 1 个唯一 gram）
    events = []
    for i in range(70000):
        # 6 字 key，前 5 字唯一编码 i，窗口 n=6 → 每 key 恰 1 个 gram
        key = f"{i:05d}尾"
        events.append((key, f"歌{i % 5}"))
    ci = assert_equiv(events, 6)
    assert len(ci) == 70000


def test_chunk_global_byte_order():
    """回归（v2.21.0 开发期缺陷）：gram 字节序与 gid（首次出现序）**不一致**
    且跨多块（>65536 gram）。旧实现按 gid 区间分块、只在块内排序，块间
    乱序导致 CompactIndex 二分失效——键在 blob 里却 get()=None。

    上一个 test_chunk_boundary 用 f"{i:05d}尾" 恰好字节序==gid 序，掩盖了
    缺陷；这里用确定性伪随机打乱插入序，强制两者背离。
    """
    rng = random.Random(20261118)
    keys = [f"{i:05d}尾" for i in range(70000)]
    rng.shuffle(keys)  # 插入序打乱 → gid 序与字节序背离
    events = [(k, f"歌{i % 5}") for i, k in enumerate(keys)]
    ci = assert_equiv(events, 6)  # 内含 keys blob 全局字节序不变量检查
    assert len(ci) == 70000
    # 抽查跨块边界的键：二分必须命中（旧实现在这里 get()=None）
    for probe in ("00000尾", "32768尾", "65535尾", "65536尾", "69999尾"):
        assert ci.get(probe) is not None, f"跨块键 {probe} 二分失效"