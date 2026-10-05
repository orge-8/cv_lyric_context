"""最近命中歌曲的落盘记录（有界环形）。

为什么要有它：用户歌单 ``user_songs.json`` 只记"库里有什么"，内存里的
``_recent_recommends`` 只记"最近推荐过什么"（软排除用，且不落盘）——
两者都回答不了"最近有人在群里点了/聊到了哪首歌"。这个模块专门记**命中**。

纯标准库，不依赖 maibot_sdk、不打日志：便于脱机单测，也不会把 ``self.ctx``
漏到 plugin.py 之外。
"""

import json
import os
import time
from typing import Any

RECENT_SONGS_FILE = "recent_songs.json"
#: 环形上限：够消费方回溯一天，又不让文件无限增长
RECENT_SONGS_LIMIT = 30
#: 外部文本（歌名/歌手）入库前限长
NAME_MAX_CHARS = 80
ARTIST_MAX_CHARS = 120
#: 默认落盘节流：命中是低频事件，但热路径上不做同步 IO
DEFAULT_FLUSH_THROTTLE_SEC = 30.0


def _clean_field(value: Any, limit: int) -> str:
    """压成单行并限长（外部来源字段，永不入库多行文本）。"""
    text = str(value or "")
    text = text.replace("\r", " ").replace("\n", " ")
    return " ".join(text.split())[:limit]


class RecentSongsLog:
    """最近命中过的歌曲：``{schema, entries: [{name, artist, at}]}``，环形上限。

     - **坏即空**：文件损坏时备份成 ``.broken`` 并从干净状态开始，绝不抛出；
    - **节流写盘**：``add()`` 只动内存，``flush()`` 才落盘（默认 30s 节流，
      卸载时 ``force=True`` 兜底），避免命中热路径上做同步 IO；
    - **失败留痕**：本模块不打日志（保持纯标准库可单测），但所有降级/失败都会写进
      ``last_error``，由调用方在合适的位置打出来——避免"静默降级 = 故障隐形"；
    - ``recent()`` 是纯只读筛选，供跨插件 API 直接调用。
    """

    def __init__(self, data_dir: str, limit: int = RECENT_SONGS_LIMIT):
        self._limit = max(1, int(limit))
        self._path = os.path.join(str(data_dir), RECENT_SONGS_FILE)
        self._entries: list[dict[str, Any]] = []
        self._dirty = False
        self._last_saved_at = 0.0
        #: 最近一次降级/失败的原因（空串 = 最近一次操作正常）；调用方负责打日志
        self.last_error = ""
        self._load()

    @property
    def path(self) -> str:
        return self._path

    @property
    def count(self) -> int:
        """当前记录条数（供 API 区分"从未命中"与"时间窗内没有"）。"""
        return len(self._entries)

    @property
    def dirty(self) -> bool:
        return self._dirty

    def _load(self) -> None:
        """读取落盘记录；坏文件备份为 ``.broken`` 后当空档案，不抛异常。

        失败原因写进 ``last_error``（含路径与异常），由调用方打日志——
        一次性把全部命中记录丢掉这种事，不能在日志里看不见。
        """
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except Exception as exc:  # noqa: BLE001 —— 任何读取/解析失败都从干净状态开始
            backed = False
            try:
                os.replace(self._path, self._path + ".broken")
                backed = True
            except OSError:
                backed = False
            self.last_error = (
                f"记录文件不可读（{self._path}）：{exc!r}；"
                f"{'已备份为 .broken 并重建' if backed else '备份失败，本次按空档案继续'}"
            )
            return
        entries = raw.get("entries") if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            self.last_error = f"记录文件结构异常（{self._path}）：entries 不是列表，按空档案继续"
            return
        self._entries = [item for item in entries if isinstance(item, dict)][-self._limit:]

    def add(self, name: str, artist: str = "", at: float | None = None) -> bool:
        """登记一次命中（只改内存）。名字为空的记录直接丢弃。"""
        clean_name = _clean_field(name, NAME_MAX_CHARS)
        if not clean_name:
            return False
        entry = {
            "name": clean_name,
            "artist": _clean_field(artist, ARTIST_MAX_CHARS),
            "at": float(at) if at is not None else time.time(),
        }
        self._entries.append(entry)
        if len(self._entries) > self._limit:
            del self._entries[: len(self._entries) - self._limit]
        self._dirty = True
        return True

    def flush(self, *, force: bool = False, throttle_sec: float = DEFAULT_FLUSH_THROTTLE_SEC) -> bool:
        """原子写盘（节流）。返回是否真的写了；失败只返回 False，绝不抛。

        节流语义：**首次** flush 立即落盘（新进程 ``_last_saved_at=0``，没有"上一次
        写盘时刻"可比，不该把第一个命中丢掉）；此后 ``throttle_sec`` 窗口内的
        写请求只在内存里挂着，窗口过后由下一次 flush 补上，``force=True`` 立即落盘
        （``on_unload`` 用）。最坏情况：窗口内未落盘的命中在崩溃时丢失。
        """
        if not self._dirty:
            return False
        now = time.time()
        if not force and (now - self._last_saved_at) < max(0.0, float(throttle_sec)):
            return False
        payload = {"schema": 1, "entries": self._entries}
        tmp = self._path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except OSError as exc:
            # 失败原因留给调用方打日志：内存里的记录还在，下轮 flush 会再试
            self.last_error = f"写盘失败（{self._path}）：{exc!r}"
            return False
        self._last_saved_at = now
        self._dirty = False
        self.last_error = ""
        return True

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        """只读筛选最近命中的歌（最新在前）。

        不改状态、不写盘、不抛异常；``limit`` 钳到 1..100；畸形记录被跳过。
        """
        try:
            cap = int(limit)
        except (TypeError, ValueError):
            cap = 10
        cap = min(max(cap, 1), 100)

        out: list[dict[str, Any]] = []
        for raw in reversed(self._entries):
            if not isinstance(raw, dict):
                continue
            try:
                at = float(raw.get("at"))
            except (TypeError, ValueError):
                continue
            out.append(
                {
                    "name": _clean_field(raw.get("name"), NAME_MAX_CHARS),
                    "artist": _clean_field(raw.get("artist"), ARTIST_MAX_CHARS),
                    "at": at,
                }
            )
            if len(out) >= cap:
                break
        return out
