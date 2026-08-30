"""中V歌词识别 · 上下文注入插件

工作方式:
1. EventHandler 监听所有入站普通消息，用 song_lyric_keywords.txt 的
   59638 句歌词关键词表做 O(1) 精确匹配（清洗标点/全半角后）。
2. 命中歌词时，记录该会话的「歌词 -> 歌名」命中（带时间戳）。
3. HookHandler 订阅 maisaka.replyer.before_model_request（BLOCKING 模式），
   在 MaiBot 向 LLM 发起请求前，把最近命中的歌曲信息作为 system 消息
   注入 messages（通过 modified_kwargs 覆盖），让 bot 能自然接住歌词话题。

数据: assets/knowledge_db.db (3412 首中文 VOCALOID 歌曲元数据) +
      assets/song_lyric_keywords.txt (歌词句 -> 歌名 关键词表)
"""
import re
import sqlite3
import time
import unicodedata
from collections import deque
from pathlib import Path
from typing import Any

from maibot_sdk import EventHandler, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, EventType, HookMode, HookOrder

ASSET_DIR = Path(__file__).parent / "assets"

_LYRIC_TAIL = re.compile(r"是《(.+)》的歌词\s*$")


def _clean(text: str) -> str:
    """归一化文本: 全角转半角、去标点空白、转小写，只留字母数字和汉字。"""
    normalized = unicodedata.normalize("NFKC", str(text or "")).lower()
    return "".join(ch for ch in normalized if ch.isalnum())


class PluginSection(PluginConfigBase):
    """插件配置。"""

    __ui_label__ = "中V歌词识别设置"

    config_version: str = Field(default="1", description="配置版本号（热更新迁移用，勿手动修改）")
    enabled: bool = Field(default=True, description="是否启用插件")
    min_line_len: int = Field(default=4, ge=2, le=20, description="参与匹配的歌词句最短字数（过滤过短误报）")
    ttl_seconds: int = Field(default=600, ge=30, le=86400, description="命中结果的有效期（秒），过期不再注入")
    max_inject: int = Field(default=3, ge=1, le=10, description="单次注入最多携带的歌曲数")


class CVLyricContextConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)


class CVLyricContextPlugin(MaiBotPlugin):
    config_model = CVLyricContextConfig

    def __init__(self) -> None:
        super().__init__()
        # 清洗后的歌词句 -> [歌名, ...]（个别句子属于多首歌）
        self._songs_by_line: dict[str, list[str]] = {}
        # 歌名 -> (歌手, UP主)
        self._meta_by_name: dict[str, tuple[str, str]] = {}
        # 会话 -> 最近命中 [(timestamp, 歌词原文, 歌名), ...]
        self._hits: dict[str, deque[tuple[float, str, str]]] = {}

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

    async def on_unload(self) -> None:
        self._hits.clear()

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope == "self":
            self.ctx.logger.info("插件配置已更新: version=%s", version)

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
                self._meta_by_name[name] = (str(singers or ""), str(uploader or ""))
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
                if key:
                    self._songs_by_line.setdefault(key, []).append(song)
        else:
            self.ctx.logger.warning("缺少歌词关键词文件: %s", txt_path)

    # ---------- 入站消息: 歌词命中检测 ----------

    def _extract_text(self, kwargs: dict) -> str:
        text = kwargs.get("plain_text")
        if not text:
            msg = kwargs.get("message")
            if isinstance(msg, dict):
                text = msg.get("plain_text") or msg.get("text") or ""
        return str(text or "").strip()

    def _extract_stream(self, kwargs: dict) -> str:
        stream_id = kwargs.get("stream_id") or kwargs.get("chat_id") or kwargs.get("session_id")
        if not stream_id:
            info = kwargs.get("message_base_info")
            if isinstance(info, dict):
                msg_info = info.get("message_info") or {}
                if isinstance(msg_info, dict):
                    group = msg_info.get("group_info") or {}
                    stream_id = (group or {}).get("group_id") or msg_info.get("user_info", {}).get("user_id")
        return str(stream_id or "")

    def detect(self, stream_id: str, text: str) -> list[str]:
        """清洗文本后查关键词表，命中则登记并返回歌名列表。"""
        cfg = self.config.plugin
        key = _clean(text)
        if len(key) < cfg.min_line_len:
            return []
        songs = self._songs_by_line.get(key)
        if not songs:
            return []
        hits = self._hits.setdefault(stream_id, deque(maxlen=20))
        hits.append((time.time(), text, songs[0]))
        return songs

    @EventHandler("cv_lyric_detect", event_type=EventType.ON_MESSAGE)
    async def on_message(self, **kwargs):
        if not self.config.plugin.enabled:
            return None
        stream_id = self._extract_stream(kwargs)
        text = self._extract_text(kwargs)
        if not stream_id or not text or text.startswith("/"):
            return None
        self.detect(stream_id, text)
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
            extra = "、".join(x for x in (singers, uploader) if x)
            entries.append(f"- 「{lyric}」 出自《{song}》" + (f"（{extra}）" if extra else ""))
            if len(entries) >= cfg.max_inject:
                break
        if not entries:
            return ""
        body = "\n".join(entries)
        return (
            "【歌词识别】用户最近在会话中发送了以下歌词原文：\n"
            f"{body}\n"
            "用户可能在引歌词、玩歌词接龙或聊这首歌。请在回复中自然地运用这些歌曲信息"
            "（歌名/歌手），只在话题相关时提及，不要生硬播报。"
        )

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

        session_id = ""
        for key in ("session_id", "chat_id", "stream_id", "chat"):
            if kwargs.get(key):
                session_id = str(kwargs[key])
                break

        system_text = self._build_system_text(session_id) if session_id else ""
        if not system_text:
            return {"action": "continue"}

        # messages 结构以运行时为准：list[dict(role, content)] 是常见形态，
        # 兼容消息对象有 role/content 属性的情况。
        messages = kwargs.get("messages")
        injected = None
        if isinstance(messages, list):
            injected = []
            for msg in messages:
                if isinstance(msg, dict):
                    injected.append(dict(msg))
                elif hasattr(msg, "role") and hasattr(msg, "content"):
                    injected.append({"role": msg.role, "content": msg.content})
                else:
                    injected.append(msg)
            injected.append({"role": "system", "content": system_text})
        else:
            self.ctx.logger.info(
                "歌词命中但 messages 缺失或结构未知（keys=%s），仅记录不注入",
                sorted(kwargs.keys()),
            )
            return {"action": "continue"}

        return {
            "action": "continue",
            "modified_kwargs": {"messages": injected},
        }


def create_plugin():
    return CVLyricContextPlugin()
