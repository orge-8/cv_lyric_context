"""VCPedia 歌曲库能力（Mixin）

从 VCPedia（https://vcpedia.cn）爬取中V歌曲词条，落到本地 SQLite，
供 /歌词 系列命令、LLM 工具查询，并扩充 cv_lyric_context 的歌词识别库。

站点启用 Anubis PoW 反爬，api.php 明文请求一律 403，
由 vcpedia_client 完成解题与 cookie 复用。

本模块被 cv_lyric_context 的 plugin.py 以多继承方式混入
（class CVLyricContextPlugin(VCPediaMixin, MaiBotPlugin)）；
实测 MaiBot SDK 能正确注册继承来的 @Command / @Tool。
"""

import asyncio
import time
from pathlib import Path
from typing import Any, Optional

from maibot_sdk import Command, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from vcpedia_client import USER_AGENT, VCPediaClient, VCPediaError
from vcpedia_store import SongStore
from vcpedia_sync import SyncStats, VCPediaSyncer

DB_FILE = "vcpedia_songs.db"
COOKIE_FILE = "anubis_cookies.txt"


class VCPediaMixin:
    """VCPedia 歌曲库能力（Mixin）。宿主需提供 ctx / config.crawler / config.plugin。"""

    # 以下属性由宿主的 __init__ 初始化（见 plugin.py）
    _store: Optional[SongStore]
    _client: Optional[VCPediaClient]
    _syncer: Optional[VCPediaSyncer]
    _sync_task: Optional[asyncio.Task]
    _sync_stats: Optional[SyncStats]
    _sync_stream: str

    # ---------- 生命周期（由宿主调用） ----------

    def _vcpedia_init(self) -> None:
        """初始化歌曲库。宿主的 on_load 在自身数据加载完后调用。"""
        try:
            self._store = SongStore(Path(self.ctx.paths.data_dir) / DB_FILE)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("VCPedia: 歌曲库初始化失败: %s", exc)
            return
        count = self._store.count()
        last = self._store.meta_get("last_sync_at")
        last_text = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(last))) if last else "从未同步"
        self.ctx.logger.info(
            "歌词库已加载: 本地 %d 首歌曲，上次同步 %s", count, last_text
        )
        if count == 0:
            self.ctx.logger.info(
                "歌词库为空，发送「/歌词 同步 100」开始首次同步"
                "（全量耗时较长，可先设 crawler.sync_batch_limit 小批量试跑）"
            )

    async def _vcpedia_shutdown(self) -> None:
        """停止后台同步并释放资源。宿主的 on_unload 调用。"""
        if self._syncer:
            self._syncer.stop()
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
            try:
                await self._sync_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._sync_task = None
        if self._client:
            self._client.close()
        self._client = None
        self._syncer = None
        self.ctx.logger.info("歌词库已卸载")

    def _vcpedia_on_config_update(self, version: str) -> None:
        """站点/限速变化后重建客户端。宿主的 on_config_update 调用。"""
        self.ctx.logger.info("歌词库: 配置已更新: version=%s", version)
        self._client = None
        self._syncer = None
        if self._store is None and self.config.plugin.enabled:
            self._vcpedia_init()

    # ---------- 内部工具 ----------

    @staticmethod
    def _matched_text(kwargs: dict, key: str, text: str, index: int) -> str:
        """取命令参数：优先正则命名捕获组，回退到按空格切分的原始文本。

        不同版本 MaiBot 传入的参数结构不一样（有的给 matched_groups，
        有的只给 text），两条路都要能取到。
        """
        groups = kwargs.get("matched_groups") or {}
        value = str(groups.get(key) or "").strip()
        if value:
            return value
        parts = (text or "").split()
        return parts[index].strip() if len(parts) > index else ""

    @staticmethod
    def _matched_int(kwargs: dict, key: str, text: str, index: int) -> int:
        """同上，但取整数（取不到或非数字时返回 0）。"""
        raw = VCPediaMixin._matched_text(kwargs, key, text, index)
        return int(raw) if raw.isdigit() else 0

    async def _reply(self, stream_id: str, text: str) -> tuple[bool, str, int]:
        """统一回复：确实发出去才返回拦截级别 2，否则返回 0 让 bot 接一句话。

        直接固定返回 2 时，一旦 send 失败（如能力未授权）用户会看到
        「命令石沉大海」——连句报错都没有。
        """
        sent = False
        if stream_id:
            try:
                result = await self.ctx.send.text(text, stream_id)
                sent = result if isinstance(result, bool) else True
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.error("歌词库: 回复发送失败: %s", exc, exc_info=True)
        return True, text, 2 if sent else 0

    @property
    def store(self) -> SongStore:
        if self._store is None:
            raise RuntimeError("歌曲库未初始化，请检查 data_dir 权限或插件是否启用")
        return self._store

    def _categories(self) -> list[str]:
        raw = self.config.crawler.categories or ""
        cats = [c.strip() for c in raw.replace("，", ",").split(",") if c.strip()]
        return cats or ["Category:洛天依歌曲"]

    def _get_client(self) -> VCPediaClient:
        if self._client is None:
            self._client = VCPediaClient(
                base_url=self.config.crawler.base_url,
                cookie_file=Path(self.ctx.paths.data_dir) / COOKIE_FILE,
                timeout=float(self.config.crawler.timeout),
                interval=float(self.config.crawler.request_interval),
                logger=self.ctx.logger,
                verify_ssl=bool(self.config.crawler.verify_ssl),
                ca_bundle=str(self.config.crawler.ca_bundle or ""),
            )
        return self._client

    def _get_syncer(self) -> VCPediaSyncer:
        if self._syncer is None:
            self._syncer = VCPediaSyncer(
                client=self._get_client(),
                store=self.store,
                logger=self.ctx.logger,
                max_fail=int(self.config.crawler.max_fail),
                # 每首回调一次，/歌词 状态 才能看到实时进度（回调只是记个引用，很便宜）
                progress_every=1,
                progress_cb=self._on_sync_progress,
            )
        return self._syncer

    def _on_sync_progress(self, stats: SyncStats) -> None:
        """把同步器内部的统计对象交给插件，供 /歌词 状态 实时读取。

        syncer.run 自己会 new 一个 SyncStats，不回调的话 _sync_stats 一直停在
        cmd_sync 里那个空对象上，进度永远显示 0/?。
        """
        self._sync_stats = stats

    @staticmethod
    def _format_credits(song: dict) -> list[str]:
        labels = (
            ("uploader", "UP主"), ("singers", "演唱"), ("lyricist", "作词"),
            ("composer", "作曲"), ("arranger", "编曲"), ("mixer", "混音"),
            ("tuner", "调教"), ("mastering", "母带"), ("pv", "PV"),
            ("illustrator", "曲绘"),
        )
        return [f"{label}：{song[key]}" for key, label in labels if song.get(key)]

    def _format_song(self, song: dict, lyric_lines: int, page_url: bool = True) -> str:
        lines = [f"《{song['name']}》"]
        if song.get("year"):
            lines[0] += f"（{song['year']}）"
        lines.extend(self._format_credits(song))
        intro = (song.get("introduction") or "").strip()
        if intro:
            if len(intro) > 200:
                intro = intro[:200] + "…"
            lines.append(f"简介：{intro}")
        if lyric_lines > 0:
            lyrics = (song.get("lyrics") or "").strip()
            if lyrics:
                shown = "\n".join(lyrics.splitlines()[:lyric_lines])
                total = len(lyrics.splitlines())
                suffix = f"\n…（共 {total} 行）" if total > lyric_lines else ""
                lines.append(f"歌词：\n{shown}{suffix}")
        if page_url:
            lines.append(f"词条：{self._get_client().page_url(song['name'])}")
        return "\n".join(lines)

    # ---------- Command ----------

    @Command(
        "lyrics_help",
        description="显示歌词库的用法",
        pattern=r"^\s*[/／]\s*歌词\s*$",
    )
    async def cmd_help(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        del kwargs
        text = (
            "歌词库用法：\n"
            "/歌词 状态 — 查看本地歌曲数量与同步进度\n"
            "/歌词 同步 [数量] — 从 VCPedia 增量同步（可限定本次抓取数量）\n"
            "/歌词 搜索 <关键词> — 按歌名/歌手/UP主搜索\n"
            "/歌词 歌曲 <歌名> — 查看歌曲详情与歌词\n"
            "/歌词 取消 — 中止正在进行的同步\n"
            "/加歌 — 导入收件箱里的歌词文件\n"
            "也可以在聊天里直接问，我会自动查询歌曲库。"
        )
        return await self._reply(stream_id, text)

    @Command(
        "lyrics_status",
        description="查看歌词库数量与同步状态",
        pattern=r"^\s*[/／]\s*歌词\s+(?:状态|status)\s*$",
    )
    async def cmd_status(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        del kwargs
        try:
            count = self.store.count()
            last = self.store.meta_get("last_sync_at")
            last_text = (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(float(last))) if last else "从未同步"
            )
        except RuntimeError as exc:
            return False, f"歌曲库不可用：{exc}", 2

        running = self._sync_task is not None and not self._sync_task.done()
        lines = [
            f"本地歌曲：{count} 首",
            f"上次同步：{last_text}",
            f"爬取分类：{'、'.join(self._categories())}",
        ]
        if running and self._sync_stats:
            stats = self._sync_stats
            done = stats.added + stats.updated + stats.failed + stats.notsong
            if stats.total:
                lines.append(
                    f"同步进行中：已完成 {done}/{stats.total} "
                    f"（新增 {stats.added}，失败 {stats.failed}）"
                )
            else:
                # 枚举分类要几十秒，这期间 total 还是 0，别显示成 0/?
                lines.append("同步进行中：正在枚举词条…")
        elif running:
            lines.append("同步进行中：正在枚举词条…")
        if count == 0:
            lines.append("歌曲库为空，发送「/歌词 同步 100」先小批量试跑。")

        text = "\n".join(lines)
        return await self._reply(stream_id, text)

    @Command(
        "lyrics_sync",
        description="从 VCPedia 增量同步歌曲到歌词库（后台执行）",
        pattern=r"^\s*[/／]\s*歌词\s+(?:同步|sync)(?:\s+(?P<limit>\d+))?\s*$",
    )
    async def cmd_sync(self, stream_id: str = "", text: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        if not self.config.plugin.enabled:
            return False, "插件已禁用", 2
        if not self.config.crawler.allow_sync_command:
            return False, "同步命令已在配置中关闭", 2
        if self._sync_task and not self._sync_task.done():
            return False, "已有同步任务在跑，发送「/歌词 取消」可中止", 2

        limit = self._matched_int(kwargs, "limit", text, 2)
        limit = limit or int(self.config.crawler.sync_batch_limit)

        self._sync_stream = stream_id or ""
        self._sync_stats = SyncStats()
        self._sync_task = asyncio.create_task(self._run_sync(limit))
        reply = (
            f"已开始同步（{'全量' if limit == 0 else f'最多 {limit} 首'}），"
            "完成后会回报结果；发送「/歌词 状态」可查看进度。"
        )
        return await self._reply(stream_id, reply)

    @Command(
        "lyrics_cancel",
        description="中止正在进行的歌词库同步",
        pattern=r"^\s*[/／]\s*歌词\s+(?:取消|cancel)\s*$",
    )
    async def cmd_cancel(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        del kwargs
        if not self._sync_task or self._sync_task.done():
            reply = "当前没有正在进行的同步"
        else:
            if self._syncer:
                self._syncer.stop()
            self._sync_task.cancel()
            reply = "已请求中止同步"
        return await self._reply(stream_id, reply)

    @Command(
        "lyrics_search",
        description="在歌词库中搜索歌曲",
        pattern=r"^\s*[/／]\s*歌词\s+(?:搜索|查歌|search)\s+(?P<keyword>\S.*?)\s*$",
    )
    async def cmd_search(self, stream_id: str = "", text: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        keyword = self._matched_text(kwargs, "keyword", text, 2)
        if not keyword:
            return False, "用法：/歌词 搜索 <关键词>", 2
        try:
            songs = self.store.search(keyword, limit=int(self.config.plugin.max_results))
        except RuntimeError as exc:
            return False, f"歌曲库不可用：{exc}", 2
        if not songs:
            reply = f"没有找到与「{keyword}」相关的歌曲"
        else:
            blocks = []
            for song in songs:
                head = f"《{song['name']}》"
                if song.get("year"):
                    head += f"（{song['year']}）"
                extra = [song[k] for k in ("singers", "uploader") if song.get(k)]
                if extra:
                    head += " — " + "、".join(extra)
                blocks.append(head)
            reply = f"找到 {len(songs)} 首：\n" + "\n".join(blocks)
        return await self._reply(stream_id, reply)

    @Command(
        "lyrics_song",
        description="查看歌词库中的歌曲详情与歌词",
        pattern=r"^\s*[/／]\s*歌词\s+(?:歌曲|详情|song)\s+(?P<name>\S.*?)\s*$",
    )
    async def cmd_song(self, stream_id: str = "", text: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        name = self._matched_text(kwargs, "name", text, 2)
        if not name:
            return False, "用法：/歌词 歌曲 <歌名>", 2
        try:
            song = self.store.get(name)
            if song is None:
                hits = self.store.search(name, limit=1)
                song = hits[0] if hits else None
        except RuntimeError as exc:
            return False, f"歌曲库不可用：{exc}", 2
        if song is None:
            reply = (
                f"歌曲库里没有《{name}》。"
                f"可以发送「/歌词 同步」从 VCPedia 补充，或确认歌名是否正确。"
            )
        else:
            reply = self._format_song(song, int(self.config.plugin.detail_lyric_lines))
        return await self._reply(stream_id, reply)

    # ---------- 后台同步 ----------

    async def _run_sync(self, limit: int) -> None:
        syncer = self._get_syncer()
        categories = self._categories()
        try:
            stats = await asyncio.to_thread(
                syncer.run, categories, int(self.config.crawler.category_depth), limit, False
            )
            self._sync_stats = stats
            note = await self._vcpedia_after_sync(stats)
        except asyncio.CancelledError:
            self.ctx.logger.info("VCPedia: 同步任务已取消")
            return
        except VCPediaError as exc:
            self.ctx.logger.warning("VCPedia: 同步失败: %s", exc)
            if self._sync_stream:
                await self.ctx.send.text(f"同步失败：{exc}", self._sync_stream)
            return
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.exception("VCPedia: 同步异常: %s", exc)
            if self._sync_stream:
                await self.ctx.send.text(f"同步异常：{exc}", self._sync_stream)
            return
        if self._sync_stream:
            text = stats.summary()
            if note:
                text = f"{text}\n{note}"
            await self.ctx.send.text(text, self._sync_stream)
        self._sync_stream = ""

    async def _vcpedia_after_sync(self, stats: SyncStats) -> str:
        """同步成功后的收尾钩子，宿主可覆盖。

        返回值会追加到同步结果消息里。默认什么都不做。
        宿主在这里重建内存歌词索引——否则新爬的歌要等重启 MaiBot 才能被识别。
        """
        return ""

    # ---------- Tool ----------

    @Tool(
        "search_vcpedia_song",
        description=(
            "在本地 VCPedia 歌曲库中按歌名、歌手或 UP 主搜索中文 VOCALOID 歌曲，"
            "返回歌名、演唱、UP主、作词作曲等元信息与歌词片段。"
            "当用户提到某首中V歌曲、问某首歌是谁唱的/谁做的，或想确认歌曲信息时使用。"
            "本地库没有结果时可以提示用户先同步。"
        ),
        parameters=[
            ToolParameterInfo(
                name="keyword",
                param_type=ToolParamType.STRING,
                description="搜索关键词，如歌名「普通DISCO」、歌手「洛天依」或 UP 主名",
                required=True,
            ),
        ],
    )
    async def tool_search_song(self, keyword: str, **kwargs: Any) -> dict[str, str]:
        del kwargs
        if not self.config.plugin.enabled:
            return {"content": "歌词库当前已禁用。"}
        try:
            songs = self.store.search(keyword, limit=int(self.config.plugin.max_results))
        except RuntimeError as exc:
            return {"content": f"歌曲库不可用：{exc}"}
        if not songs:
            return {
                "content": (
                    f"本地 VCPedia 歌曲库中没有找到与「{keyword}」相关的歌曲。"
                    "可以让用户发送「/歌词 同步」从 VCPedia 补充数据。"
                )
            }
        preview = int(self.config.plugin.lyric_preview_chars)
        blocks = []
        for song in songs:
            head = f"《{song['name']}》"
            if song.get("year"):
                head += f"（{song['year']}）"
            lines = [head, *self._format_credits(song)]
            intro = (song.get("introduction") or "").strip()
            if intro:
                lines.append(f"简介：{intro[:preview]}")
            lyrics = (song.get("lyrics") or "").strip()
            if lyrics:
                lines.append("歌词片段：" + "\n".join(lyrics.splitlines()[:5]))
            blocks.append("\n".join(lines))
        return {"content": f"找到 {len(songs)} 首相关歌曲：\n\n" + "\n\n---\n\n".join(blocks)}

    @Tool(
        "get_vcpedia_lyrics",
        description=(
            "按歌名取本地 VCPedia 歌曲库中的完整歌词。"
            "当用户想看某首中V歌曲的歌词、要接歌词、或确认某句歌词的出处时使用。"
            "也可以用歌词片段反查歌名。"
        ),
        parameters=[
            ToolParameterInfo(
                name="song_name",
                param_type=ToolParamType.STRING,
                description="歌名；若填的是一句歌词，会自动按歌词内容反查",
                required=True,
            ),
        ],
    )
    async def tool_get_lyrics(self, song_name: str, **kwargs: Any) -> dict[str, str]:
        del kwargs
        if not self.config.plugin.enabled:
            return {"content": "歌词库当前已禁用。"}
        name = (song_name or "").strip()
        if not name:
            return {"content": "需要提供歌名或歌词片段。"}
        try:
            song = self.store.get(name)
            if song is None:
                hits = self.store.search_lyrics(name, limit=3)
                if not hits:
                    hits = self.store.search(name, limit=3)
                if not hits:
                    return {
                        "content": (
                            f"本地歌曲库里没有《{name}》，也没匹配到包含该内容的歌词。"
                            "可以让用户发送「/歌词 同步」从 VCPedia 补充数据。"
                        )
                    }
                if len(hits) == 1:
                    song = hits[0]
                else:
                    names = "、".join(f"《{h['name']}》" for h in hits)
                    song = hits[0]
                    head = f"找到多首可能的歌曲：{names}\n以下为《{song['name']}》的歌词：\n\n"
                    lyrics = (song.get("lyrics") or "").strip()
                    return {
                        "content": head + (lyrics if lyrics else "（该词条暂无歌词）")
                    }
        except RuntimeError as exc:
            return {"content": f"歌曲库不可用：{exc}"}

        lyrics = (song.get("lyrics") or "").strip()
        header = f"《{song['name']}》"
        credits = self._format_credits(song)
        if credits:
            header += "\n" + "\n".join(credits)
        if not lyrics:
            return {"content": f"{header}\n（该词条暂无歌词）"}
        return {"content": f"{header}\n歌词：\n{lyrics}"}
