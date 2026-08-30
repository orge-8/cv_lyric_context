"""中V歌词识别 · 上下文注入插件

工作方式:
1. 监听入站消息（新版用 chat.receive.after_process Hook，旧版回退 ON_MESSAGE 事件），
   用 assets/song_lyric_keywords.txt 的歌词关键词表做 O(1) 精确匹配
   （清洗标点/全半角后）。命中则按会话登记「歌词 -> 歌名」。
2. 在 maisaka.replyer.before_model_request Hook 里，把 TTL 内的命中整理成
   system 内容注入本次模型请求:
   - 新版运行时传 items（Context Item 快照），返回 SystemMessageItem；
   - 旧版运行时传 messages，返回 {"role": "system", "content": ...}。
   两条路径同时给出，运行时只会读取自己认识的那个键，互不干扰。

3. 歌词文件收件箱: 把 .txt / .lrc 歌词文件丢进 assets/lyrics_inbox/，
   插件加载时或收到「/加歌」命令时自动解析入库（写进 assets/user_songs.json），
   成功后文件归档到 lyrics_inbox/imported/。

数据: assets/knowledge_db.db (中文 VOCALOID 歌曲元数据) +
      assets/song_lyric_keywords.txt (歌词句 -> 歌名 关键词表) +
      assets/user_songs.json (自定义/导入的歌曲)
"""
import asyncio
import json
import re
import sqlite3
import sys
import time
import unicodedata
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from maibot_sdk import Command, EventHandler, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, EventType, HookMode, HookOrder

import lyrics_import
from lyrics_import import ImportReport

ASSET_DIR = Path(__file__).parent / "assets"

_LYRIC_TAIL = re.compile(r"是《(.+)》的歌词\s*$")

# 汉字/假名判定，用于过滤纯数字、纯英文的噪声句（例如圆周率歌曲的数字串）
_CJK = re.compile(r"[㐀-䶿一-鿿぀-ヿ]")
_MIN_CJK_CHARS = 2

# 注入内容标记，用于识别"本次请求已经注入过"，避免重试时重复叠加
INJECT_MARKER = "【歌词识别】"

# 同一会话内相同文本在这么短的时间内重复到达视为同一次消息（两套监听的重复触发）
DEDUP_SECONDS = 10

# Context Item 快照结构版本，取自 MaiBot 的 CONTEXT_ITEM_SCHEMA_VERSION
CONTEXT_ITEM_SCHEMA_VERSION = 1


def _clean(text: str) -> str:
    """归一化文本: 全角转半角、去标点空白、转小写，只留字母数字和汉字。"""
    normalized = unicodedata.normalize("NFKC", str(text or "")).lower()
    return "".join(ch for ch in normalized if ch.isalnum())


def _as_lines(raw: Any) -> list[str]:
    """把 user_songs.json 里的 lyrics 字段统一成行列表。"""
    if isinstance(raw, str):
        return raw.splitlines()
    return [str(x) for x in raw or []]


class PluginSection(PluginConfigBase):
    """插件配置。"""

    __ui_label__ = "中V歌词识别设置"

    config_version: str = Field(default="1", description="配置版本号（热更新迁移用，勿手动修改）")
    enabled: bool = Field(default=True, description="是否启用插件")
    min_line_len: int = Field(default=4, ge=2, le=20, description="参与匹配的歌词句最短字数（过滤过短误报）")
    ttl_seconds: int = Field(default=600, ge=30, le=86400, description="命中结果的有效期（秒），过期不再注入")
    max_inject: int = Field(default=3, ge=1, le=10, description="单次注入最多携带的歌曲数")
    auto_import_inbox: bool = Field(
        default=True,
        description="插件加载时自动导入 assets/lyrics_inbox 里的歌词文件",
    )
    max_lines_per_song: int = Field(
        default=lyrics_import.DEFAULT_MAX_LINES,
        ge=50,
        le=20000,
        description="单个歌词文件最多入库的行数",
    )


class CVLyricContextConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)


class CVLyricContextPlugin(MaiBotPlugin):
    config_model = CVLyricContextConfig

    def __init__(self) -> None:
        super().__init__()
        # 清洗后的歌词句 -> [歌名, ...]（个别句子属于多首歌）
        self._songs_by_line: dict[str, list[str]] = {}
        # 歌名 -> (歌手, P主)
        self._meta_by_name: dict[str, tuple[str, str]] = {}
        # 归一化歌名 -> (歌手, P主)，只来自 knowledge_db.db，供歌词文件导入时反查
        self._db_meta: dict[str, tuple[str, str]] = {}
        # 会话 -> 最近命中 [(timestamp, 歌词原文, 歌名), ...]
        self._hits: dict[str, deque[tuple[float, str, str]]] = {}
        # 会话 -> (时间戳, 最近一次登记的文本)，用于两套监听的去重
        self._last_recorded: dict[str, tuple[float, str]] = {}
        # 诊断: hook/事件/命令的实际字段名只打一次，避免刷屏
        self._probed_incoming = False
        self._probed_request = False
        self._probed_command = False

    # ---------- 生命周期 ----------

    async def on_load(self) -> None:
        if not self.config.plugin.enabled:
            self.ctx.logger.info("插件已在配置中禁用，跳过数据加载")
            return
        self._load_assets()
        self.ctx.logger.info(
            "中V歌词识别已加载: %d 句歌词关键词 / %d 首歌元数据",
            len(self._songs_by_line), len(self._meta_by_name),
        )
        if self.config.plugin.auto_import_inbox:
            report = await asyncio.to_thread(self._run_import)
            if report and report.results:
                self.ctx.logger.info(
                    "歌词收件箱导入: 成功 %d 个 / 失败 %d 个",
                    len(report.ok_results), len(report.failed_results),
                )

    async def on_unload(self) -> None:
        self._hits.clear()
        self._last_recorded.clear()

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope == "self":
            self.ctx.logger.info("插件配置已更新: version=%s", version)
            # 若从"禁用"切到"启用"，补一次数据加载
            if self.config.plugin.enabled and not self._songs_by_line:
                self._load_assets()

    # ---------- 数据加载 ----------

    def _load_assets(self) -> None:
        db_path = ASSET_DIR / "knowledge_db.db"
        txt_path = ASSET_DIR / "song_lyric_keywords.txt"

        if db_path.exists():
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                rows = conn.execute("SELECT name, singers, uploader FROM songs").fetchall()
            finally:
                conn.close()
            for name, singers, uploader in rows:
                meta = (str(singers or ""), str(uploader or ""))
                self._meta_by_name[name] = meta
                # 归一化索引: 导入歌词文件时按歌名反查歌手/P主
                self._db_meta[_clean(name)] = meta
        else:
            self.ctx.logger.warning("缺少歌曲元数据库: %s", db_path)

        if txt_path.exists():
            for raw in txt_path.read_text(encoding="utf-8").splitlines():
                if "=>" not in raw:
                    continue
                line, right = raw.split("=>", 1)
                match = _LYRIC_TAIL.search(right)
                if not match:
                    continue
                key = _clean(line)
                song = match.group(1)
                # 过滤纯数字/纯英文句：缺少足够汉字时极易误命中（如圆周率歌词）
                if key and len(_CJK.findall(key)) >= _MIN_CJK_CHARS:
                    self._songs_by_line.setdefault(key, []).append(song)
        else:
            self.ctx.logger.warning("缺少歌词关键词文件: %s", txt_path)

        user_count = self._load_user_songs()
        if user_count:
            self.ctx.logger.info("自定义歌单已加载: %d 首额外歌曲", user_count)

    def _load_user_songs(self) -> int:
        """加载 assets/user_songs.json 里的自定义歌曲，返回成功加载的歌曲数。

        自定义歌曲优先于基础词库（同名歌词句以自定义歌为准）。
        格式见 README 的"添加新歌"一节。
        """
        path = ASSET_DIR / "user_songs.json"
        if not path.exists():
            return 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.ctx.logger.warning("自定义歌单解析失败，已跳过: %s", exc)
            return 0
        if not isinstance(data, list):
            self.ctx.logger.warning("自定义歌单格式应为歌曲列表，已跳过")
            return 0

        loaded = 0
        for song in data:
            if not isinstance(song, dict):
                continue
            name = str(song.get("name") or "").strip()
            if not name:
                continue
            self._index_song(
                name,
                str(song.get("singers") or ""),
                str(song.get("uploader") or ""),
                _as_lines(song.get("lyrics")),
            )
            loaded += 1
        return loaded

    def _index_song(self, name: str, singers: str, uploader: str, lines: list[str]) -> int:
        """把一首歌写进内存词库，返回新增的歌词句数。

        自定义/导入的歌插到同名词句列表最前，命中时优先于基础词库。
        元数据按"新值非空才覆盖"合并：user_songs.json 里的空字段不会冲掉
        knowledge_db.db 里已有的歌手/P主。
        """
        old_singers, old_uploader = self._meta_by_name.get(name, ("", ""))
        self._meta_by_name[name] = (singers or old_singers, uploader or old_uploader)
        added = 0
        for line in lines:
            key = _clean(line)
            if key and len(_CJK.findall(key)) >= _MIN_CJK_CHARS:
                bucket = self._songs_by_line.setdefault(key, [])
                if name not in bucket:
                    bucket.insert(0, name)
                    added += 1
        return added

    # ---------- 歌词文件收件箱 ----------

    def _lookup_db_meta(self, song_name: str) -> tuple[str, str]:
        """按歌名在 knowledge_db.db 里反查 (歌手, P主)，查不到返回空串。"""
        return self._db_meta.get(_clean(song_name), ("", ""))

    def _run_import(self) -> Optional[ImportReport]:
        """扫描 assets/lyrics_inbox 并导入，失败时返回 None（已记日志）。"""
        inbox = ASSET_DIR / lyrics_import.INBOX_DIR_NAME
        try:
            inbox.mkdir(parents=True, exist_ok=True)
            report = lyrics_import.run_import(
                ASSET_DIR,
                min_line_len=self.config.plugin.min_line_len,
                max_lines=self.config.plugin.max_lines_per_song,
                meta_lookup=self._lookup_db_meta,
            )
        except Exception as exc:
            self.ctx.logger.error("歌词收件箱导入失败: %s", exc, exc_info=True)
            return None
        if report.results:
            self._apply_import(report)
        return report

    def _apply_import(self, report: ImportReport) -> None:
        """把导入结果并入内存词库，立即生效，无需重载插件。"""
        by_name = {
            str(s.get("name") or ""): s
            for s in report.songs
            if isinstance(s, dict)
        }
        for result in report.ok_results:
            song = by_name.get(result.song_name)
            if not song:
                continue
            added = self._index_song(
                result.song_name,
                str(song.get("singers") or ""),
                str(song.get("uploader") or ""),
                _as_lines(song.get("lyrics")),
            )
            self.ctx.logger.info(
                "歌词文件已入库: %s -> 《%s》（新增 %d 句）",
                result.file_name, result.song_name, added,
            )

    @staticmethod
    def _pick_stream_id(kwargs: dict) -> str:
        """从命令载荷里取 stream_id，兼容不同版本/结构的字段名。

        拿不到就返回空串：此时不能静默失败，调用方要记日志并降级，
        否则用户会看到"发命令后什么都没发生"。
        """
        for key in ("stream_id", "chat_id", "session_id", "stream"):
            value = kwargs.get(key)
            if value:
                return str(value)
        message = kwargs.get("message")
        if isinstance(message, dict):
            for key in ("stream_id", "chat_id", "session_id"):
                if message.get(key):
                    return str(message[key])
        return ""

    @Command(
        "import_lyrics",
        description="扫描歌词收件箱 assets/lyrics_inbox 并扩充歌曲库",
        pattern=r"^\s*[/／]\s*(?:加歌|导入歌词|扫描歌词|歌词导入)(?:\s.*)?$",
        aliases=["/加歌", "/导入歌词", "/扫描歌词"],
    )
    async def cmd_import_lyrics(self, **kwargs: Any) -> tuple[bool, str, int]:
        """/加歌：导入收件箱里的歌词文件并回报结果。

        返回值第三项是拦截级别（不是权重）：2 = 阻止 bot 再对这条命令生成回复。
        只有在结果确实发出去时才用 2；发不出去就返回 0，让 bot 至少接一句话，
        避免用户看到"命令石沉大海"。
        """
        raw = str(kwargs.get("raw_message") or kwargs.get("text") or "")
        stream_id = self._pick_stream_id(kwargs)
        if not self._probed_command:
            self._probed_command = True
            self.ctx.logger.info("[诊断] import_lyrics 字段: %s", sorted(kwargs.keys()))
        self.ctx.logger.info("收到歌词导入命令: raw=%r stream_id=%r", raw, stream_id or "<空>")

        if not self.config.plugin.enabled:
            text = "歌词插件当前已禁用，无法导入。"
        else:
            report = await asyncio.to_thread(self._run_import)
            if report is None:
                text = "歌词导入失败，请查看插件日志。"
            elif not report.results:
                text = (
                    "收件箱里没有待导入的歌词文件。\n"
                    f"把 .txt 或 .lrc 歌词文件放进 {lyrics_import.INBOX_DIR_NAME}/ 再试一次。"
                )
            else:
                text = (
                    f"歌词导入完成：成功 {len(report.ok_results)} 个，"
                    f"失败 {len(report.failed_results)} 个。\n" + report.summary()
                )

        sent = False
        if stream_id:
            try:
                result = await self.ctx.send.text(text, stream_id)
                sent = result if isinstance(result, bool) else True
            except Exception as exc:
                self.ctx.logger.error("导入结果发送失败: %s", exc, exc_info=True)
        else:
            self.ctx.logger.error(
                "命令载荷里没有 stream_id，无法回复结果（字段=%s，raw=%r）",
                sorted(kwargs.keys()), raw,
            )

        self.ctx.logger.info("歌词导入结果已回复: sent=%s", sent)
        return True, text, 2 if sent else 0

    # ---------- 入站消息: 歌词命中检测 ----------

    def _extract_incoming(self, kwargs: dict) -> tuple[str, str]:
        """从入站消息载荷里取 (会话ID, 文本)。兼容新旧两种载荷结构。"""
        message = kwargs.get("message")
        message = message if isinstance(message, dict) else {}
        text = (
            message.get("processed_plain_text")
            or message.get("plain_text")
            or kwargs.get("plain_text")
            or kwargs.get("text")
            or ""
        )
        session_id = (
            message.get("session_id")
            or kwargs.get("session_id")
            or kwargs.get("stream_id")
            or kwargs.get("chat_id")
            or ""
        )
        return str(session_id or ""), str(text or "").strip()

    def record_hit(self, session_id: str, text: str) -> list[str]:
        """清洗文本后查关键词表，命中则登记并返回歌名列表。"""
        cfg = self.config.plugin
        key = _clean(text)
        if len(key) < cfg.min_line_len:
            return []
        songs = self._songs_by_line.get(key)
        if not songs:
            return []

        # 去重: 同一会话内短时间内收到的相同文本只登记一次
        now = time.time()
        last = self._last_recorded.get(session_id)
        if last and now - last[0] <= DEDUP_SECONDS and last[1] == key:
            return songs
        self._last_recorded[session_id] = (now, key)

        self._hits.setdefault(session_id, deque(maxlen=20)).append((now, text, songs[0]))
        self.ctx.logger.info(
            "歌词命中: 「%s」-> 《%s》 (会话=%s)", text[:30], songs[0], session_id
        )
        return songs

    @HookHandler(
        "chat.receive.after_process",
        name="cv_lyric_detect_receive",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_incoming_message(self, **kwargs: Any):
        """新版运行时: 入站消息完成预处理后触发。"""
        if not self.config.plugin.enabled:
            return {"action": "continue"}
        if not self._probed_incoming:
            self._probed_incoming = True
            self.ctx.logger.info("[诊断] chat.receive.after_process 字段: %s", sorted(kwargs.keys()))
        session_id, text = self._extract_incoming(kwargs)
        if not text or text.startswith("/"):
            return {"action": "continue"}
        if not session_id:
            self.ctx.logger.info("[诊断] 入站消息缺少会话ID，跳过登记: %s", text[:30])
            return {"action": "continue"}
        self.record_hit(session_id, text)
        return {"action": "continue"}

    @EventHandler("cv_lyric_detect_event", event_type=EventType.ON_MESSAGE)
    async def on_message_event(self, **kwargs: Any):
        """旧版运行时回退: ON_MESSAGE 事件（部分版本该事件未派发，属正常）。"""
        if not self.config.plugin.enabled:
            return None
        session_id, text = self._extract_incoming(kwargs)
        if not text or text.startswith("/"):
            return None
        if session_id:
            self.record_hit(session_id, text)
        return None

    # ---------- 出站 LLM 请求: 上下文注入 ----------

    def _session_hits(self, session_id: str) -> list[tuple[float, str, str]]:
        cfg = self.config.plugin
        now = time.time()
        hits = self._hits.get(session_id)
        if not hits:
            return []
        return [h for h in hits if now - h[0] <= cfg.ttl_seconds]

    def _build_system_text(self, session_id: str) -> str:
        """把 TTL 内的命中整理成注入给 LLM 的 system 文本，无命中返回空。"""
        cfg = self.config.plugin
        fresh = self._session_hits(session_id)
        if not fresh:
            return ""
        # 保留时间顺序，歌名去重
        seen: set[str] = set()
        entries: list[str] = []
        for _, lyric, song in reversed(fresh):  # 最近的在前
            if song in seen:
                continue
            seen.add(song)
            singers, uploader = self._meta_by_name.get(song, ("", ""))
            singers, uploader = lyrics_import.flatten_names(singers), lyrics_import.flatten_names(uploader)
            extra = "，".join(
                label
                for label in (
                    f"演唱：{singers}" if singers else "",
                    f"P主：{uploader}" if uploader else "",
                )
                if label
            )
            entries.append(f"- 「{lyric}」 出自《{song}》" + (f"（{extra}）" if extra else ""))
            if len(entries) >= cfg.max_inject:
                break
        if not entries:
            return ""
        body = "\n".join(entries)
        return (
            f"{INJECT_MARKER}用户最近在会话中发送了以下歌词原文：\n"
            f"{body}\n"
            "用户可能在引歌词、玩歌词接龙或聊这首歌。请在回复中自然地运用这些歌曲信息"
            "（歌名/歌手/P主），只在话题相关时提及，不要生硬播报。"
        )

    @staticmethod
    def _build_system_item(text: str) -> dict[str, Any]:
        """构造一个 SystemMessageItem 快照（Context Item schema v1）。"""
        return {
            "item_type": "SystemMessageItem",
            "meta": {
                "item_id": uuid.uuid4().hex,
                "logical_turn_id": None,
                "timestamp": datetime.now().isoformat(),
            },
            "parts": [{"type": "text", "text": text}],
        }

    @staticmethod
    def _item_texts(items: list[Any]) -> str:
        """拼出 items 里所有文本内容，用于判断是否已经注入过。"""
        chunks: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            for part in item.get("parts") or []:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
            if isinstance(item.get("content"), str):
                chunks.append(item["content"])
        return "\n".join(chunks)

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="cv_lyric_context_injector",
        mode=HookMode.BLOCKING,  # BLOCKING 才能返回 modified_kwargs 改写请求参数
        order=HookOrder.EARLY,   # 尽早注入，对后续处理器可见
        error_policy=ErrorPolicy.SKIP,  # 注入失败不阻断主流程
    )
    async def inject_song_context(self, **kwargs: Any):
        if not self.config.plugin.enabled:
            return {"action": "continue"}

        if not self._probed_request:
            self._probed_request = True
            self.ctx.logger.info(
                "[诊断] before_model_request 字段: %s", sorted(kwargs.keys())
            )

        session_id = str(kwargs.get("session_id") or kwargs.get("chat_id") or "")
        if not session_id:
            self.ctx.logger.info("[诊断] 模型请求缺少 session_id，跳过注入")
            return {"action": "continue"}

        system_text = self._build_system_text(session_id)
        if not system_text:
            return {"action": "continue"}

        modified: dict[str, Any] = {}

        # 路径 A（当前版本）: 改写 Context Items
        items = kwargs.get("items")
        if isinstance(items, list):
            if INJECT_MARKER in self._item_texts(items):
                return {"action": "continue"}
            modified["items"] = list(items) + [self._build_system_item(system_text)]
            modified["item_schema_version"] = kwargs.get(
                "item_schema_version", CONTEXT_ITEM_SCHEMA_VERSION
            )

        # 路径 B（旧版回退）: 改写 messages
        messages = kwargs.get("messages")
        if isinstance(messages, list):
            if any(
                isinstance(m, dict) and INJECT_MARKER in str(m.get("content") or "")
                for m in messages
            ):
                return {"action": "continue"}
            modified["messages"] = list(messages) + [{"role": "system", "content": system_text}]

        if not modified:
            self.ctx.logger.info(
                "歌词命中但请求载荷中没有 items/messages（字段=%s），未注入", sorted(kwargs.keys())
            )
            return {"action": "continue"}

        self.ctx.logger.info(
            "已向 LLM 上下文注入歌曲信息（%s）", "/".join(sorted(modified))
        )
        return {"action": "continue", "modified_kwargs": modified}


def create_plugin():
    return CVLyricContextPlugin()
