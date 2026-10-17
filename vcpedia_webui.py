# -*- coding: utf-8 -*-
"""cv_lyric_context 的 WebUI 自定义页面 API（webui.json 绑定）。

设计原则（与 VCPediaMixin 的分工）：

- **只包壳，不重写**：读操作全部直接调 ``SongStore`` 已有方法；写操作直接复用
  ``VCPediaMixin`` 现成的后台任务链路（``_sync_task`` + ``_run_sync`` /
  ``_run_refetch_batch``），不新造一套任务系统。
- **宿主约束**（`src/plugin_runtime/webui_schema.py`）：单次绑定调用硬超时 10 秒、
  同插件并发上限 2、返回值 ≤512 KiB / ≤12 层 / ≤20000 元素。所以查询一律带
  ``limit`` 并在本模块夹住；触发类操作只负责**起任务**，进度靠页面轮询
  ``lyrics.task`` —— 抓取是分钟级任务，同步等待必然超时。
- **权限**：命令侧走 ``crawler.sync_admin_ids``；页面 API 没有群/QQ 上下文，
  按约定改由 dashboard 登录态把关，但仍**尊重配置里的总开关**
  （``plugin.enabled`` / ``crawler.allow_sync_command``），配置说不许触发就一律拒绝。

模块名沿用插件的 ``vcpedia_`` 前缀，导入风格与 ``vcpedia_mixin.py`` 一致（平铺），
因为 ``plugin.py`` 已把插件目录插进 ``sys.path``。
"""

import asyncio
import time
from typing import Any, Optional

try:  # `@API` 自 SDK 2.1.0 起引入（_manifest.json 的 sdk.min_version 也是 2.1.0）；
    # 这里仍做防御性降级：万一宿主塞了更老的 SDK，插件其余功能不该跟着挂。
    from maibot_sdk import API
except ImportError:  # pragma: no cover - 取决于宿主 SDK 版本
    def API(*_args: Any, **_kwargs: Any):  # type: ignore[no-redef]
        def decorator(func):
            return func

        return decorator

from vcpedia_store import EMOTION_TAGS
from vcpedia_mixin import _MAX_SYNC_LIMIT  # 与命令侧同一个上限，避免两处漂移

#: 页面可请求的单次返回上限（再大就可能撞 512 KiB / 20000 元素）
_ROWS_MAX = 200
#: 缺歌词清单单次最多列多少条
_EMPTY_MAX = 200
#: 出错时反馈里最多带几条失败样本
_FAILURES_MAX = 5


class VCPediaWebUIMixin:
    """把歌曲库与爬虫状态暴露给 dashboard 的自定义页面。

    依赖 ``VCPediaMixin`` 提供的运行时状态：``self.store`` / ``self.config`` /
    ``self.ctx`` / ``self._sync_task`` / ``self._sync_stats``。类顺序上必须写在
    ``VCPediaMixin`` **之后**（`class X(VCPediaMixin, VCPediaWebUIMixin, MaiBotPlugin)`）。
    """

    # ------------------------------------------------------------------ 工具

    @staticmethod
    def _webui_int(value: Any, default: int, maximum: int = _ROWS_MAX) -> int:
        """把页面传来的条数夹到 [1, maximum]；取不到用 default。"""
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        if number <= 0:
            number = default
        return max(1, min(maximum, number))

    def _webui_store(self):
        """拿歌曲库；不可用时返回 (None, 原因)。"""
        try:
            store = self.store
        except Exception as exc:  # RuntimeError：库未就绪 / 路径不可用
            return None, f"歌曲库不可用：{exc}"
        if store is None:
            return None, "歌曲库未初始化"
        return store, ""

    def _webui_task_state(self) -> dict:
        """当前后台任务（同步 / 补歌词）的状态快照。"""
        task = getattr(self, "_sync_task", None)
        running = task is not None and not task.done()
        stats = getattr(self, "_sync_stats", None)
        state: dict[str, Any] = {
            "running": running,
            "kind": str(getattr(self, "_webui_task_kind", "") or ("命令触发" if running else "")),
            "percent": 0.0,
            "total": 0,
            "done": 0,
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "notsong": 0,
            "failed": 0,
            "elapsed": "",
            "finished": False,
            "summary": "",
            "failures": [],
        }
        if stats is None:
            return state
        done = int(stats.added + stats.updated + stats.failed + stats.notsong)
        state.update({
            "total": int(stats.total),
            "done": done,
            "added": int(stats.added),
            "updated": int(stats.updated),
            "skipped": int(stats.skipped),
            "notsong": int(stats.notsong),
            "failed": int(stats.failed),
            "finished": bool(stats.finished_at),
            "failures": [str(item)[:120] for item in list(stats.failures or [])[:_FAILURES_MAX]],
        })
        if stats.total:
            state["percent"] = round(min(100.0, done / stats.total * 100.0), 1)
        elif state["finished"]:
            state["percent"] = 100.0
        seconds = float(stats.elapsed or 0.0)
        state["elapsed"] = f"{int(seconds)}s" if seconds else ""
        try:
            state["summary"] = str(stats.summary())[:400]
        except Exception:  # summary 只用于展示，坏了不影响状态
            state["summary"] = ""
        return state

    def _webui_can_trigger(self) -> str:
        """触发重任务前的总开关校验；返回非空 = 拒绝原因。"""
        if not self.config.plugin.enabled:
            return "插件已在配置中禁用"
        if not self.config.crawler.allow_sync_command:
            return "同步命令已在配置中关闭（crawler.allow_sync_command）"
        return ""

    # ------------------------------------------------------------------ 读

    @API("lyrics.stats", description="曲库规模、歌词覆盖率与情感标注进度", version="1")
    async def api_stats(self, **kwargs: Any) -> dict:
        """看板四项：曲库总数 / 缺歌词 / 已标注情感 / 待标注。"""
        store, reason = self._webui_store()
        if store is None:
            return {"ready": False, "reason": reason, "total": 0, "empty_lyrics": 0,
                    "coverage_percent": 0.0, "emotion_annotated": 0, "emotion_pending": 0,
                    "last_sync": "从未同步", "categories": ""}
        total = int(store.count())
        empty = int(store.count_empty_lyrics())
        try:
            annotated = int(store.emotion_stats().get("annotated", 0))
        except Exception:
            annotated = 0
        try:
            pending = len(store.pending_emotions(limit=_ROWS_MAX))
        except Exception:
            pending = 0
        last = store.meta_get("last_sync_at")
        last_text = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(float(last))) if last else "从未同步"
        )
        return {
            "ready": True,
            "reason": "",
            "total": total,
            "empty_lyrics": empty,
            "coverage_percent": round((total - empty) / total * 100.0, 1) if total else 0.0,
            "emotion_annotated": annotated,
            "emotion_pending": pending,
            "emotion_tags": ",".join(EMOTION_TAGS) if isinstance(EMOTION_TAGS, (list, tuple)) else "",
            "last_sync": last_text,
            "categories": "、".join(self._categories()) if hasattr(self, "_categories") else "",
        }

    @API("lyrics.search", description="按歌名/歌手/UP主或歌词片段检索曲库", version="1")
    async def api_search(
        self,
        keyword: Any = "",
        scope: Any = "name",
        limit: Any = 20,
        **kwargs: Any,
    ) -> dict:
        """scope=name 走 SongStore.search，scope=lyrics 走 search_lyrics 反查。"""
        store, reason = self._webui_store()
        if store is None:
            return {"ready": False, "reason": reason, "rows": [], "scope": "name", "keyword": ""}
        word = str(keyword or "").strip()[:60]
        mode = "lyrics" if str(scope or "").strip() == "lyrics" else "name"
        size = self._webui_int(limit, 20)
        if not word:
            return {"ready": True, "reason": "", "rows": [], "scope": mode, "keyword": ""}
        rows = (store.search_lyrics(word, limit=size) if mode == "lyrics"
                else store.search(word, limit=size))
        return {"ready": True, "reason": "", "scope": mode, "keyword": word,
                "rows": [self._webui_song_row(row) for row in rows]}

    @API("lyrics.song", description="单首歌的完整档案（创作者分工 / 标签 / 歌词预览）", version="1")
    async def api_song(self, name: Any = "", **kwargs: Any) -> dict:
        """给检索结果的详情弹窗用；歌词按插件配置的预览长度截断。"""
        store, reason = self._webui_store()
        if store is None:
            return {"found": False, "reason": reason}
        target = str(name or "").strip()[:80]
        if not target:
            return {"found": False, "reason": "缺少歌名"}
        row = store.get(target)
        if not row:
            return {"found": False, "reason": f"曲库里没有「{target}」"}
        lyrics = str(row.get("lyrics") or "")
        info: dict[str, Any] = {
            "found": True,
            "reason": "",
            "name": str(row.get("name") or target),
            "lyrics_chars": len(lyrics),
            "lyrics_preview": self._clip_lyrics(lyrics) if hasattr(self, "_clip_lyrics") else lyrics[:800],
            "emotion": ",".join(store.parse_emotion(str(row.get("emotion") or ""))),
        }
        info.update(self._webui_song_row(row))
        try:
            credits = store.credits_of(str(row.get("name") or target))
            info["credits"] = "；".join(
                f"{role}:{'/'.join(names)}" for role, names in (credits or {}).items() if names
            )[:400]
        except Exception:
            info["credits"] = ""
        try:
            tags = store.tags_of(str(row.get("name") or target))
            info["tags"] = "、".join(str(tag) for tag in (tags or []))[:200]
        except Exception:
            info["tags"] = ""
        return info

    def _webui_song_row(self, row: dict) -> dict:
        """表格行：只留页面上要显示的字段，字符串一律限长（返回值有 512 KiB 上限）。

        带上 ``preview``：详情弹窗直接读选中行的字段，省掉一次「按行再查一遍」
        （queries 由交互节点同名参数驱动，拿不到表格的 selection，弹窗只能吃行内数据）。
        """
        lyrics = str(row.get("lyrics") or "")
        preview = self._clip_lyrics(lyrics) if hasattr(self, "_clip_lyrics") else lyrics[:480]
        raw_emotion = str(row.get("emotion") or "")
        # 库里存的是分隔符格式（如 "温柔|伤感"），页面上直接给人类可读的形式
        try:
            emotion = "、".join(self.store.parse_emotion(raw_emotion))
        except Exception:
            emotion = raw_emotion
        return {
            "name": str(row.get("name") or "")[:80],
            "singers": str(row.get("singers") or row.get("uploader") or "")[:80],
            "uploader": str(row.get("uploader") or "")[:60],
            "has_lyrics": bool(lyrics.strip()),
            "lyrics_chars": len(lyrics),
            "emotion": emotion[:60],
            "preview": str(preview)[:600],
        }

    @API("lyrics.empty", description="缺歌词条目清单（可按冷却天数过滤）", version="1")
    async def api_empty(self, limit: Any = 30, skip_days: Any = 0, **kwargs: Any) -> dict:
        """``skip_days`` 对应命令侧的 ``refill_cooldown_days``：跳过最近确认过无歌词的条目。

        返回的是 ``rows``（dict 列表）而不是纯名字列表 —— 页面的 table 节点按
        ``columns[].field`` 取值，纯字符串列表渲染不出列。
        """
        store, reason = self._webui_store()
        if store is None:
            return {"ready": False, "reason": reason, "rows": [], "total": 0}
        try:
            days = float(skip_days or 0)
        except (TypeError, ValueError):
            days = 0.0
        window = max(0.0, days) * 86400.0
        total = int(store.count_empty_lyrics(skip_checked_within=window))
        names = store.empty_lyric_names(limit=self._webui_int(limit, 30, _EMPTY_MAX),
                                       skip_checked_within=window)
        return {"ready": True, "reason": "", "total": total,
                "rows": [{"name": str(name)[:80]} for name in names]}

    @API("lyrics.recent", description="最近在群里命中过的歌曲（有界环形记录）", version="1")
    async def api_recent(self, limit: Any = 20, **kwargs: Any) -> dict:
        """数据源是 ``recent_songs.json``，与命令 /歌词 检索无关，靠消息命中写入。"""
        log = getattr(self, "_recent_songs", None)
        if log is None:
            return {"ready": False, "reason": "命中记录未初始化（插件可能被禁用）", "rows": []}
        size = self._webui_int(limit, 20)
        try:
            items = log.recent(size)
        except Exception as exc:
            return {"ready": False, "reason": f"读取命中记录失败：{exc}", "rows": []}
        rows = []
        for item in items or []:
            at = float(item.get("at") or 0.0)
            rows.append({
                "name": str(item.get("name") or "")[:80],
                "artist": str(item.get("artist") or "")[:60],
                "time": time.strftime("%m-%d %H:%M", time.localtime(at)) if at else "",
            })
        return {"ready": True, "reason": "", "rows": rows}

    @API("lyrics.task", description="后台同步/补歌词任务的状态与进度", version="1")
    async def api_task(self, **kwargs: Any) -> dict:
        """页面用轮询它来画进度条 —— 抓取是分钟级任务，只能「触发 + 轮询」。"""
        state = self._webui_task_state()
        state["can_trigger"] = not self._webui_can_trigger()
        state["trigger_block_reason"] = self._webui_can_trigger()
        state["sync_batch_limit"] = int(self.config.crawler.sync_batch_limit)
        return state

    # ------------------------------------------------------------------ 写

    def _webui_start_task(self, factory, kind: str, limit: int) -> dict:
        """起后台任务前的公共校验 + 记录，返回给页面的结果。

        ``factory`` 是**无参可调用**（不是协程对象）：校验失败时不能留下
        「建好却没 await 的协程」，否则真机日志里会刷 RuntimeWarning。
        """
        blocked = self._webui_can_trigger()
        if blocked:
            return {"ok": False, "reason": blocked, "kind": kind}
        task = getattr(self, "_sync_task", None)
        if task is not None and not task.done():
            return {"ok": False, "reason": "已有同步任务在跑，可先取消", "kind": kind}
        self._webui_task_kind = kind
        # 与命令侧一致：先把统计对象清空，避免页面读到最后一次任务的旧数字
        try:
            from vcpedia_sync import SyncStats

            self._sync_stats = SyncStats()
        except Exception:  # pragma: no cover - 导入失败不影响起任务
            pass
        # 补歌词链路会读这个停止标志，命令侧每次起任务前都复位；不复位时
        # 「取消过一次 → 之后每次补歌词立刻自停」。
        self._refetch_stop = False
        self._sync_stream = ""  # 页面触发的任务没有群会话，不往群里回执
        self._sync_task = asyncio.ensure_future(factory())
        return {"ok": True, "reason": "", "kind": kind, "limit": limit}

    def _webui_resolve_limit(self, limit: Any, default_when_unbounded: int = 50) -> tuple[int, str]:
        """页面条数口径：显式值 > 配置的 sync_batch_limit > 保底 50。

        命令侧 0 的含义是「不限」，页面不能继承这个语义 —— 后台里误点一下
        「开始同步」就是小时级任务，所以这里一律给有限值，并把替换原因带回页面。
        """
        try:
            size = int(limit)
        except (TypeError, ValueError):
            size = 0
        if size <= 0:
            size = int(self.config.crawler.sync_batch_limit or 0)
        note = ""
        if size <= 0:
            size = default_when_unbounded
            note = f"未指定条数且配置为「不限」，本次按 {size} 首执行"
        return min(size, _MAX_SYNC_LIMIT), note

    @API("lyrics.start_sync", description="从 VCPedia 增量同步曲库（后台执行）", version="1")
    async def api_start_sync(self, limit: Any = 0, **kwargs: Any) -> dict:
        size, note = self._webui_resolve_limit(limit)
        result = self._webui_start_task(lambda: self._run_sync(size), "同步", size)
        if note:
            result["note"] = note
        return result

    @API("lyrics.start_refetch", description="批量补抓缺歌词条目（后台执行）", version="1")
    async def api_start_refetch(self, limit: Any = 0, **kwargs: Any) -> dict:
        """与 ``/歌词 补歌词`` 同一条链路（``_run_refetch_batch``），冷却天数照配置走。"""
        size, note = self._webui_resolve_limit(limit)
        result = self._webui_start_task(lambda: self._run_refetch_batch(size), "补歌词", size)
        if note:
            result["note"] = note
        return result

    @API("lyrics.cancel", description="取消正在跑的后台任务", version="1")
    async def api_cancel(self, **kwargs: Any) -> dict:
        """等价于「/歌词 取消」。"""
        task = getattr(self, "_sync_task", None)
        if task is None or task.done():
            return {"ok": False, "reason": "当前没有在跑的任务"}
        task.cancel()
        return {"ok": True, "reason": ""}
