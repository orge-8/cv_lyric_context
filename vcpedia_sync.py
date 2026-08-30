"""同步编排：枚举分类 → 取 wikitext → 解析 → 清洗 → 入库。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

from vcpedia_store import SongStore, safe_song_name
from vcpedia_text_clean import clean_credit, clean_display_name, clean_introduction, clean_lyrics
from vcpedia_client import VCPediaClient
from vcpedia_wikitext_parser import parse_wikitext

CREDIT_KEYS = (
    "uploader", "singers", "lyricist", "composer", "arranger",
    "mixer", "tuner", "mastering", "pv", "illustrator",
)


class SyncCancelled(Exception):
    """同步被外部中止（插件卸载或用户取消）。"""


class NotSongPage(Exception):
    """词条抓到了但不是歌曲页（无简介也无歌词）。

    属内容判定，不是网络/反爬故障，**不能计入连续失败**——
    否则分类里连续出现一批重定向页/模板页就会把整轮同步提前打断。
    """


@dataclass
class SyncStats:
    """一次同步的结果统计。"""

    total: int = 0
    added: int = 0
    updated: int = 0
    skipped: int = 0
    notsong: int = 0
    failed: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    failures: List[str] = field(default_factory=list)

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        return end - self.started_at

    def summary(self) -> str:
        if self.failed and self.added == 0 and self.updated == 0 and self.total:
            # 全败通常是反爬拦截，给出可操作提示
            return (
                f"同步失败：{self.total} 首全部抓取失败"
                f"（耗时 {self.elapsed:.1f}s）。多为 Anubis 反爬拦截，"
                "请稍后重试或调大请求间隔。"
            )
        parts = [
            f"新增 {self.added}",
            f"更新 {self.updated}",
        ]
        # skipped 建候选列表时就被排除的「库中已有」条目，和上面两项不是同一批，
        # 写在一起会被读成 新增+跳过=本次抓取量。单独表述避免误会。
        if self.skipped:
            parts.append(f"库中已有 {self.skipped} 首未重复抓取")
        if self.notsong:
            parts.append(f"非歌曲页 {self.notsong} 首")
        parts.append(f"失败 {self.failed}")
        return f"同步完成：{'，'.join(parts)}，耗时 {self.elapsed:.1f}s"


def build_record(name: str, parsed: dict, category: str = "") -> dict:
    """把解析结果整理成入库记录（全部文本字段做 wiki 标记清洗）。"""
    credits = parsed.get("credits") or {}
    year_raw = str(credits.get("year") or "")
    return {
        "name": clean_display_name(name),
        "safe_name": safe_song_name(name),
        **{key: clean_credit(str(credits.get(key) or "")) for key in CREDIT_KEYS},
        "year": year_raw,
        "introduction": clean_introduction(parsed.get("introduction") or ""),
        "lyrics": clean_lyrics(parsed.get("lyrics") or ""),
        "categories": category,
    }


class VCPediaSyncer:
    """执行增量同步。"""

    def __init__(
        self,
        client: VCPediaClient,
        store: SongStore,
        logger: logging.Logger,
        max_fail: int = 30,
        progress_every: int = 50,
        progress_cb: Optional[Callable[[SyncStats], None]] = None,
    ) -> None:
        self.client = client
        self.store = store
        self.logger = logger
        self.max_fail = max(1, max_fail)
        self.progress_every = max(1, progress_every)
        self.progress_cb = progress_cb
        self._stop = False

    def stop(self) -> None:
        """请求中止同步（下一首生效）。"""
        self._stop = True

    @property
    def stopping(self) -> bool:
        return self._stop

    def list_titles(self, categories: Sequence[str], max_depth: int) -> List[str]:
        return self.client.fetch_titles(
            categories, max_depth=max_depth, should_stop=lambda: self._stop
        )

    def run(
        self,
        categories: Sequence[str],
        max_depth: int = 2,
        limit: int = 0,
        refresh_existing: bool = False,
    ) -> SyncStats:
        """增量同步。limit > 0 时只处理前 limit 首（用于小规模测试）。"""
        stats = SyncStats()
        self._stop = False
        try:
            titles = self.list_titles(categories, max_depth)
        except Exception as exc:  # noqa: BLE001 - 列表失败即整体失败
            self.logger.warning("VCPedia: 获取歌曲列表失败: %s", exc)
            raise

        if not titles:
            self.logger.warning("VCPedia: 分类下未枚举到任何词条")
            stats.finished_at = time.time()
            return stats

        if not refresh_existing:
            known = self.store.bulk_exists(safe_song_name(t) for t in titles)
            pending = [t for t in titles if safe_song_name(t) not in known]
            stats.skipped = len(titles) - len(pending)
        else:
            pending = list(titles)

        if limit > 0:
            pending = pending[:limit]
        stats.total = len(pending)
        self.logger.info(
            "VCPedia: 待抓取 %d 首（分类内共 %d 首，库中已有 %d 首跳过）",
            stats.total, len(titles), stats.skipped,
        )
        # 枚举一结束就把 total 发布出去，否则 /歌词 状态 在枚举后仍显示「枚举中」
        if self.progress_cb:
            self.progress_cb(stats)

        consecutive_failures = 0
        for index, title in enumerate(pending, start=1):
            if self._stop:
                self.logger.info("VCPedia: 同步被中止（已处理 %d/%d）", index - 1, stats.total)
                break
            try:
                source = self.client.fetch_wikitext(title)
                if not source:
                    raise ValueError("未取到 wikitext")
                parsed = parse_wikitext(title, source)
                if not (parsed.get("introduction") or parsed.get("lyrics")):
                    raise NotSongPage("词条无简介也无歌词，可能不是歌曲页")
            except NotSongPage as exc:
                # 内容判定失败：跳过即可，不累加 consecutive_failures
                stats.notsong += 1
                self.logger.info("VCPedia: 跳过非歌曲页 %s: %s", title, exc)
                continue
            except Exception as exc:  # noqa: BLE001 - 单首失败不影响整体
                consecutive_failures += 1
                stats.failed += 1
                if len(stats.failures) < 10:
                    stats.failures.append(title)
                self.logger.warning("VCPedia: 抓取失败 %s: %s", title, exc)
                if consecutive_failures >= self.max_fail:
                    self.logger.warning("VCPedia: 连续失败 %d 次，提前停止", consecutive_failures)
                    break
                continue

            consecutive_failures = 0
            try:
                if self.store.upsert(build_record(title, parsed, ",".join(categories[:3]))):
                    stats.added += 1
                else:
                    stats.updated += 1
            except Exception as exc:  # noqa: BLE001
                stats.failed += 1
                self.logger.warning("VCPedia: 入库失败 %s: %s", title, exc)

            if self.progress_cb and index % self.progress_every == 0:
                self.progress_cb(stats)

        stats.finished_at = time.time()
        self.store.meta_set("last_sync_at", str(stats.finished_at))
        self.store.meta_set("last_sync_added", str(stats.added))
        self.logger.info("VCPedia: %s", stats.summary())
        return stats
