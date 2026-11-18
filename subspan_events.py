# -*- coding: utf-8 -*-
"""子串索引事件流构建（v2.21.0 构建峰值优化）。

背景（2026-11-18 实测，见 docs/decisions-build-peak.md）：
v2.20.0 构建瞬间峰值 ~500MB，大头是「子串 dict（150 万键 ~254MB）尚未
释放 + 压实临时结构（排序键引用 + keys blob + ID 流 ~60MB）」叠加。
本模块把构建期的子串 dict **完全不物化**：

- 每次 (歌词句, 歌名) 准入事件按全局序号记进三条定长 ``array('I')``
  （gram ID / 事件序号 / 歌名 ID），174 万条事件仅 ~20MB；
- gram 注册表（gram -> 首次出现序 ID）是唯一的 dict 代价（~150MB），
  但换掉了子串 dict 的桶列表、歌名引用与压实临时叠加；
- 构建末尾用**计数排序散射**把事件下标按 gid 分段（段内天然事件序
  升序，无需排序），再按 gram 的**全局字节序**分块流式分组去重取前
  ``max_songs`` 首，直接生成 CompactIndex。

**语义契约（行为零变化是验收线）**：事件序 = 生产 dict 版的插入序，
``_SUBSPAN_MAX_SONGS`` 上限「先到先得」的入选集合与桶内顺序完全一致
（原型对 175 万键全量比对 0 差异）。保序依据：散射按事件下标递增写入，
每个 gid 段内即事件序升序。

只在**全量构建**（_build_asset_index）使用；运行时增量合并仍走
``_add_subspans_into`` 的 CompactIndex 桶视图路径（规模小，无需事件流）。
"""
from __future__ import annotations

from array import array

from compact_index import CompactIndex

__all__ = ["SubspanEventLog"]

_U = "I"  # gram ID / 事件序号 / 歌名 ID 都远小于 2^32


class SubspanEventLog:
    """构建期子串事件流：record() 收集，finalize() 生成 CompactIndex。

    线程模型：与索引构建同在 worker 线程，单线程顺序写入，无并发假设。
    """

    __slots__ = ("_n", "_max_songs", "_gids", "_sids", "_nids",
                 "_grams", "_gram_list", "_names", "_name_ids", "_seq")

    def __init__(self, n: int, max_songs: int,
                 names: list[str], name_ids: dict[str, int]) -> None:
        """
        Args:
            n: 滑窗长度（<=0 时 record 为空操作，finalize 返回空索引）。
            max_songs: 片段归属歌数上限（_SUBSPAN_MAX_SONGS）。
            names / name_ids: 与整句表共享的歌名 ID 空间（原地扩展）。
                事件里的歌名若不在表内则分配新 ID——与 CompactIndex
                .from_mapping 的兜底语义一致。
        """
        self._n = int(n)
        self._max_songs = int(max_songs)
        self._gids = array(_U)
        self._sids = array(_U)
        self._nids = array(_U)
        self._grams: dict[str, int] = {}
        self._gram_list: list[str] = []
        self._names = names
        self._name_ids = name_ids
        self._seq = 0

    def record(self, key: str, name: str) -> None:
        """登记一条 (歌词句, 歌名) 准入事件（调用方已完成整句层去重门控）。

        与 _add_subspans_into 的 dict 版同口径：n<=0 或 len(key)<n 跳过；
        每个 N 字窗口一条事件，事件序号全局递增（保序的依据）。
        """
        n = self._n
        if n <= 0 or len(key) < n:
            return
        nid = self._name_ids.get(name)
        if nid is None:
            nid = len(self._names)
            self._name_ids[name] = nid
            self._names.append(name)
        gids, sids, nids = self._gids, self._sids, self._nids
        grams, gram_list = self._grams, self._gram_list
        seq = self._seq
        for i in range(len(key) - n + 1):
            gram = key[i:i + n]
            gid = grams.get(gram)
            if gid is None:
                gid = len(gram_list)
                grams[gram] = gid
                gram_list.append(gram)
            gids.append(gid)
            sids.append(seq)
            nids.append(nid)
        self._seq = seq + 1

    def finalize(self) -> CompactIndex:
        """计数排序散射 + 全局字节序分块输出，生成子串紧凑索引。

        峰值纪律（原型 v1 教训：分组阶段建 150 万条目 dict 反而 +260MB）：
        - 事件分段用计数排序散射（全程 array），不排事件、不建桶 dict；
        - gid 全局字节序排序只排 int 下标（key=gram 本身），排完转
          array('I') 再进分块循环，临时 list 不与其他大结构叠加；
        - 键按 UTF-8 字节序输出（CompactIndex 二分要求），桶内保持事件序。

        正确性纪律（v2.21.0 开发期教训）：分块必须沿**全局字节序**切，
        不能按 gid 区间切再块内排序——gid 是首次出现序，与字节序无关，
        块内有序拼起来整体乱序，CompactIndex 二分直接失效（键在 blob
        里却查不到）。str 码点序与 UTF-8 字节序一致，sorted(key=gram)
        即全局字节序，与 from_mapping 的 sorted(mapping) 同口径。
        """
        idx = CompactIndex()
        gids, sids, nids = self._gids, self._sids, self._nids
        total = len(gids)
        if total == 0:
            idx._names = self._names
            self._grams = {}
            self._gram_list = []
            return idx

        CHUNK_GIDS = 1 << 16
        num_grams = len(self._gram_list)
        max_songs = self._max_songs

        # 计数排序把事件下标按 gid 分段（全程 array 无大 dict）：
        # counts[g]..counts[g+1] 是 gid g 的事件段；散射按 i 递增写入，
        # 段内天然事件序升序（保序依据，无需再排事件）。
        counts = array(_U, bytes(4 * (num_grams + 1)))
        for g in gids:
            counts[g + 1] += 1
        for i in range(1, num_grams + 1):
            counts[i] += counts[i - 1]
        scatter = array(_U, bytes(4 * total))
        cursor = array(_U, counts[:num_grams])
        for i in range(total):
            g = gids[i]
            scatter[cursor[g]] = i
            cursor[g] += 1
        del gids, sids, cursor
        self._gids = self._sids = array(_U)

        # 全局字节序排序（正确性核心）：gid 是首次出现序，与字节序无关。
        # 必须先得到「按 gram 字节序排列的 gid 全序」，再沿该全序分块，
        # 块间才天然有序（CompactIndex 二分的前提）。按 gid 区间分块再
        # 块内排序是错的：块内有序拼起来整体乱序（v2.21.0 开发期实测：
        # 键在 blob 里、_find 却 -1）。str 码点序 == UTF-8 字节序，与
        # from_mapping 的 sorted(mapping) 同口径。
        # 峰值纪律：此时 grams dict 已可释放，排序 key 直接引用 gram
        # str（不复制字节串）；排完转 array('I')（6MB）即丢 list。
        gram_list = self._gram_list
        self._grams = {}
        order = sorted(range(num_grams), key=gram_list.__getitem__)
        self._gram_list = []
        gid_order = array(_U, order)
        del order

        keys_buf = bytearray()
        offsets = array(_U, [0])
        val_offsets = array(_U, [0])
        vals = array(_U)
        nids_local = nids
        for start in range(0, num_grams, CHUNK_GIDS):
            stop = min(start + CHUNK_GIDS, num_grams)
            # gid_order 已全局字节序 → 块内无需再排序，逐 gid 直接从
            # scatter 段取事件去重取前 max_songs，临时结构只有块级大小。
            for j in range(start, stop):
                g = gid_order[j]
                kb = gram_list[g].encode("utf-8")
                bucket: list[int] = []
                for i in scatter[counts[g]:counts[g + 1]]:
                    nid = nids_local[i]
                    if nid not in bucket and len(bucket) < max_songs:
                        bucket.append(nid)
                keys_buf.extend(kb)
                offsets.append(len(keys_buf))
                vals.extend(bucket)
                val_offsets.append(len(vals))
        del scatter, counts, nids_local, nids, gram_list, gid_order
        self._nids = array(_U)
        idx._keys = bytes(keys_buf)
        idx._offsets = offsets
        idx._val_offsets = val_offsets
        idx._vals = vals
        idx._names = self._names
        idx._size = len(offsets) - 1
        return idx