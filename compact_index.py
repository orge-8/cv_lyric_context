# -*- coding: utf-8 -*-
"""紧凑歌词索引（v2.20.0 运行时内存优化）。

背景（2026-11-15 实测，真实素材 + 36MB 同步库，subspan_n=6）：
``dict[str, list[str]]`` 形态的子串索引 1,508,873 个片段占 ~254MB，
整句索引 241,235 句占 ~47MB——98% 的桶只有 1 首歌，内存大头是 Python
dict/list/str 对象开销，不是数据本身。4GB 真机上这是不可接受的常驻。

本模块把「键 -> [歌名, ...]」换成三块连续内存：

- ``_keys``：全部键按 UTF-8 字节序排序后拼接的大字节串（只存一份）；
- ``_offsets``：``array('Q')``，每个键在大字节串里的起止偏移（二分查找用）；
- ``_vals``：``array('I')``，歌名在 ``_names`` 表里的 ID（桶内容按插入序）。

查询 = 键编码后在 ``_keys`` 上二分（用偏移数组定位比较区间），命中后按
``_val_offsets`` 切出 ID 段、映射回歌名 str。单次查询 O(log N) 次字节串
比较 + 一次小列表构造，毫秒级以内（真机消息频率下可忽略）。

**语义契约（与 dict[str, list[str]] 对齐，行为零变化是验收线）**：

1. 桶内歌名顺序 = 插入顺序（自定义/导入优先的语义靠它）；
2. 增量写入走 ``setdefault`` 返回的桶视图：视图上的 insert/append 与
   dict 版 ``bucket = d.setdefault(k, []); bucket.insert(0, x)`` 完全等价。
   首次修改时把「压实层桶 + 增量」物化进 overlay 小 dict（标记为整体
   替换），之后视图直接操作该列表。overlay 只存上次全量构建以来的新增，
   规模很小（同步一次几十~几百首），下次全量重建时归零；
3. ``__setitem__`` 整体替换桶（测试与特殊路径用）；
4. Mapping 兼容接口：``get / __getitem__ / __contains__ / __len__ /
   __iter__ / items / keys / values / clear / __eq__ / __bool__``。
   ``__eq__`` 支持与 dict 比较（测试 ``== {}`` / ``== {...}`` 依赖它）。

构建期仍用普通 dict 收集（语义经过 v2.16.0 以来的真机验证），构建末尾
:meth:`CompactIndex.from_mapping` 排序压实（只读源 dict，临时峰值仅
键引用列表 + ID 流 array），调用方随后丢弃源 dict 释放旧内存。
"""
from __future__ import annotations

from array import array
from typing import Iterator, Optional

__all__ = ["CompactIndex"]

# array 类型码：偏移用 I（4 字节，键 blob 与 ID 数都远小于 2^32）；歌名 ID 用 I。
_OFF = "I"
_ID = "I"
# 键编码：UTF-8。CJK 3 字节/字；构建与查询用同一编码，二分按字节序自洽。
_ENC = "utf-8"


class _BucketView:
    """setdefault 返回的可变桶视图：读=合并视角，写=物化后直改 overlay。

    只实现写入侧（_index_song_into / _add_subspans_into / 关键词循环）
    用到的四个操作：``in`` / len / insert / append。
    """

    __slots__ = ("_idx", "_key")

    def __init__(self, idx: "CompactIndex", key: str) -> None:
        self._idx = idx
        self._key = key

    def _merged(self) -> list[str]:
        idx = self._idx
        if self._key in idx._replaced:
            return idx._overlay[self._key]
        pos = idx._find(self._key.encode(_ENC))
        base = idx._base_bucket(pos) if pos >= 0 else []
        ov = idx._overlay.get(self._key)
        if ov:
            return base + [s for s in ov if s not in base]
        return base

    def _materialize(self) -> list[str]:
        idx = self._idx
        if self._key in idx._replaced:
            return idx._overlay[self._key]
        lst = self._merged()
        idx._overlay[self._key] = lst
        idx._replaced.add(self._key)
        return lst

    def __contains__(self, name: str) -> bool:
        return name in self._merged()

    def __len__(self) -> int:
        return len(self._merged())

    def insert(self, i: int, name: str) -> None:
        self._materialize().insert(i, name)

    def append(self, name: str) -> None:
        self._materialize().append(name)


class CompactIndex:
    """键 -> [歌名, ...] 的紧凑只读层 + 增量 overlay。

    线程模型与旧 dict 一致：构建在 worker 线程、换入后事件循环读、
    增量写（同步合并/导入）在 worker 线程但宿主保证不与读并发
    （原实现就是直接改共享 dict，这里不引入新的并发假设）。
    """

    __slots__ = ("_keys", "_offsets", "_val_offsets", "_vals", "_names",
                 "_overlay", "_replaced", "_size")

    def __init__(self) -> None:
        self._keys: bytes = b""
        self._offsets: array = array(_OFF, [0])
        self._val_offsets: array = array(_OFF, [0])
        self._vals: array = array(_ID)
        self._names: list[str] = []
        # 增量层：key(str) -> list[str]
        self._overlay: dict[str, list[str]] = {}
        # 整体替换/已物化的键：overlay 即最终桶，不再与压实层合并
        self._replaced: set[str] = set()
        self._size: int = 0  # 压实层键数（overlay 新增键另算，见 __len__）

    # ── 构建 ────────────────────────────────────────────────────

    @classmethod
    def from_mapping(cls, mapping: dict, names: list[str],
                     name_ids: Optional[dict] = None) -> "CompactIndex":
        """把 dict[str, list[str]] 排序、去重、压实成紧凑层。

        - **不改源数据**：只读源 dict（调用方可能仍持有桶引用）；
          临时峰值 = 键引用列表 + ID 流 array，远小于源 dict 本体。
          调用方在拿到紧凑层后丢弃源 dict 即可释放旧内存。
        - 键按 UTF-8 字节序排序（与查询侧二分一致）；
        - 同键桶内歌名按**首次出现顺序**保留（插入序语义），重复去重
          （等价 dict 版本的 ``if name not in bucket``）。
        - 不在 names 表里的歌名自动分配新 ID；传入 name_ids 时原地更新
          （两张表共享 ID 空间，调用方后续还能继续注册新歌名）。
        """
        obj = cls()
        local_names = names  # 原地扩展，调用方丢弃自己的引用即可
        if name_ids is None:
            name_ids = {n: i for i, n in enumerate(local_names)}
        # 第一遍：排序键（源 dict 的键 str 此时还活着，不额外复制）
        sorted_keys = sorted(mapping)
        # 第二遍：按排序序把桶内容转成 ID 流；同键内天然保插入序，
        # 无需 (键, 序号) 对的全局排序——临时峰值只有键引用列表。
        ids = array(_ID)
        val_offsets = array(_OFF, [0])
        keys_buf = bytearray()
        offsets = array(_OFF, [0])
        # 热路径绑定：150 万键 × 每键多次属性查找是压实耗时大头（实测
        # id_loop 3.2s），绑定局部名 + 单歌桶快路径省掉 set 分配。
        names_get = name_ids.get
        ids_append = ids.append
        offsets_append = offsets.append
        val_offsets_append = val_offsets.append
        for k in sorted_keys:
            bucket = mapping[k]
            keys_buf += k.encode(_ENC)
            offsets_append(len(keys_buf))
            # 单歌桶快路径：实测 98% 的子串桶只有 1 首歌，去重集合纯属
            # 白分配；单元素天然无重复，直接转 ID 追加（语义完全等价）。
            if len(bucket) == 1:
                name = bucket[0]
                nid = names_get(name)
                if nid is None:
                    nid = len(local_names)
                    name_ids[name] = nid
                    local_names.append(name)
                ids_append(nid)
                val_offsets_append(len(ids))
                continue
            # 桶内去重保序（dict 版写入侧本就保证无重复，这里兜底
            # 直接构造的源）；不改动源桶本身。
            seen_ids: set[int] = set()
            for name in bucket:
                nid = names_get(name)
                if nid is None:
                    nid = len(local_names)
                    name_ids[name] = nid
                    local_names.append(name)
                if nid not in seen_ids:
                    seen_ids.add(nid)
                    ids_append(nid)
            val_offsets_append(len(ids))
        del sorted_keys
        obj._keys = bytes(keys_buf)
        obj._offsets = offsets
        obj._val_offsets = val_offsets
        obj._vals = ids
        obj._names = local_names
        obj._size = len(offsets) - 1
        return obj

    # ── 查询 ────────────────────────────────────────────────────

    def _find(self, kb: bytes) -> int:
        """二分找键在压实层的位置，找不到返回 -1。"""
        lo, hi = 0, self._size
        keys, offsets = self._keys, self._offsets
        while lo < hi:
            mid = (lo + hi) // 2
            if keys[offsets[mid]:offsets[mid + 1]] < kb:
                lo = mid + 1
            else:
                hi = mid
        if lo < self._size and keys[offsets[lo]:offsets[lo + 1]] == kb:
            return lo
        return -1

    def _base_bucket(self, pos: int) -> list[str]:
        start, end = self._val_offsets[pos], self._val_offsets[pos + 1]
        names = self._names
        return [names[i] for i in self._vals[start:end]]

    def get(self, key: str, default=None):
        if key in self._replaced:
            return list(self._overlay[key])
        ov = self._overlay.get(key)
        pos = self._find(key.encode(_ENC))
        if pos < 0:
            return list(ov) if ov is not None else default
        base = self._base_bucket(pos)
        if ov:
            # overlay 追加语义：base 在前、overlay 新增在后（与 dict 版
            # setdefault+append 的最终顺序一致）
            return base + [s for s in ov if s not in base]
        return base

    def __getitem__(self, key: str) -> list[str]:
        v = self.get(key)
        if v is None:
            raise KeyError(key)
        return v

    def __contains__(self, key: str) -> bool:
        return key in self._overlay or self._find(key.encode(_ENC)) >= 0

    def __len__(self) -> int:
        extra = sum(1 for k in self._overlay if self._find(k.encode(_ENC)) < 0)
        return self._size + extra

    def __bool__(self) -> bool:
        return bool(self._size or self._overlay)

    def __iter__(self) -> Iterator[str]:
        for k, _ in self.items():
            yield k

    def keys(self):
        return [k for k, _ in self.items()]

    def values(self):
        return [v for _, v in self.items()]

    def items(self) -> Iterator[tuple[str, list[str]]]:
        """全量遍历（测试/诊断用，热路径不走这里）。"""
        keys, offsets = self._keys, self._offsets
        seen: set[str] = set()
        for i in range(self._size):
            k = keys[offsets[i]:offsets[i + 1]].decode(_ENC)
            seen.add(k)
            if k in self._replaced:
                yield k, list(self._overlay[k])
                continue
            ov = self._overlay.get(k)
            base = self._base_bucket(i)
            yield k, (base + [s for s in ov if s not in base] if ov else base)
        for k, v in self._overlay.items():
            if k not in seen:
                yield k, list(v)

    # ── 增量写入 ────────────────────────────────────────────────

    def setdefault(self, key: str, default=None) -> _BucketView:
        """与 dict.setdefault 同位：返回可写桶视图（default 参数仅为签名兼容）。"""
        return _BucketView(self, key)

    def __setitem__(self, key: str, names: list[str]) -> None:
        """整体替换桶：overlay 即最终桶，不再与压实层合并。"""
        self._overlay[key] = list(names)
        self._replaced.add(key)

    def clear(self) -> None:
        self._keys = b""
        self._offsets = array(_OFF, [0])
        self._val_offsets = array(_OFF, [0])
        self._vals = array(_ID)
        self._names = []
        self._overlay.clear()
        self._replaced.clear()
        self._size = 0

    # ── 等价比较（测试用）──────────────────────────────────────

    def __eq__(self, other) -> bool:
        if isinstance(other, CompactIndex):
            if len(self) != len(other):
                return False
            return all(self.get(k) == other.get(k) for k in self)
        if isinstance(other, dict):
            if len(self) != len(other):
                return False
            # 必须拿 other 的值比：self.items() 的 v 是自己的桶，
            # 自比恒等。list() 包一层避免 dict.__eq__ 反射回本方法。
            return all(k in other and self.get(k) == list(other[k])
                       for k in self)
        return NotImplemented

    def __ne__(self, other) -> bool:
        eq = self.__eq__(other)
        return NotImplemented if eq is NotImplemented else not eq

    def __repr__(self) -> str:
        return f"CompactIndex(size={len(self)}, overlay={len(self._overlay)})"
