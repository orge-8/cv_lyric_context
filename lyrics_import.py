"""歌词文件收件箱 -> 自定义歌单（assets/user_songs.json）

用法: 把 .txt / .lrc 歌词文件丢进 assets/lyrics_inbox/，插件加载时或收到
「/加歌」命令时会自动扫描导入，成功的文件移到 lyrics_inbox/imported/，
失败的移到 lyrics_inbox/failed/（同一文件不会反复重试）。

歌名取值顺序:
1. 文件名（去掉扩展名与 " (1)" 之类的副本后缀）；
   若文件名是 lyrics/歌词 这类通用名则跳过；
2. 文件第一个非标签非空行；LRC 的 [ti:歌名] 标签也算首行元信息。

歌词行处理: 剥掉 LRC 时间轴 [00:12.34]（可重复）、跳过 [ti:/ar:/al:] 等
元数据标签行、文件内去重，最后按"汉字不少于 2 个 + 最短字数"过滤噪声句。

本模块只用标准库，不依赖 maibot_sdk，可单独测试。
"""
from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

INBOX_DIR_NAME = "lyrics_inbox"
IMPORTED_DIR_NAME = "imported"
FAILED_DIR_NAME = "failed"

SONGS_FILE_NAME = "user_songs.json"
LYRIC_SUFFIXES = (".txt", ".lrc")

# 汉字/假名判定，与 plugin.py 的过滤规则保持一致
_CJK = re.compile(r"[㐀-䶿一-鿿぀-ヿ]")
MIN_CJK_CHARS = 2

# LRC 时间轴，形如 [00:12.34] / [01:23] / [01:23:45]
_TIME_TAG = re.compile(r"^\s*\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]\s*")
# LRC 元数据标签行，形如 [ti:歌名]
_META_TAG = re.compile(
    r"^\s*\[(ti|ar|al|by|offset|re|ve|au|length|kana)\s*:\s*(.*?)\]\s*$", re.IGNORECASE
)
# 文件名里 " (1)" "（2）" 这类复制副本后缀
_COPY_SUFFIX = re.compile(r"[（(]\s*\d+\s*[)）]\s*$")
# 文件名里的括号标注（P主，或在开头时的歌手）
_BRACKET = re.compile(r"[【\[]([^】\]]+)[】\]]")
_HEAD_BRACKET = re.compile(r"^\s*[【\[]([^】\]]+)[】\]]\s*")
# 歌手/P主分隔符: 半角减号要求两侧空格，全角不要求
_SPLIT = re.compile(r"\s+[-－—]\s+|[－—]")

# 这些文件名没有信息量，歌名改用文件首行
_GENERIC_STEMS = {
    "lyrics", "lyric", "lrc", "歌词", "文本", "新建文本文档", "未命名",
    "untitled", "text", "document", "新建文本", "无标题",
}

_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5")

# 默认单曲歌词行上限，防止超大文件把词库撑爆
DEFAULT_MAX_LINES = 2000


@dataclass
class StemParts:
    """从文件名拆出的三段信息，拆不到就留空。"""

    name: str = ""
    singers: str = ""
    uploader: str = ""


@dataclass
class ParsedSong:
    """从单个歌词文件解析出的歌曲。"""

    name: str
    lines: list[str] = field(default_factory=list)
    singers: str = ""
    uploader: str = ""
    name_source: str = ""   # filename / first_line / lrc_title
    dropped: int = 0        # 因噪声规则被丢弃的行数


@dataclass
class FileResult:
    """单个文件的导入结果。"""

    file_name: str
    ok: bool
    message: str
    song_name: str = ""
    line_count: int = 0
    name_source: str = ""   # filename / first_line / lrc_title


@dataclass
class ImportReport:
    """一次收件箱扫描的汇总。"""

    results: list[FileResult] = field(default_factory=list)
    songs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok_results(self) -> list[FileResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed_results(self) -> list[FileResult]:
        return [r for r in self.results if not r.ok]

    def summary(self) -> str:
        if not self.results:
            return "收件箱里没有待导入的歌词文件（.txt / .lrc）。"
        parts: list[str] = []
        for r in self.ok_results:
            extra = f"，歌名取自{_source_label(r.name_source)}"
            parts.append(f"- 《{r.song_name}》 {r.message}{extra}")
        for r in self.failed_results:
            parts.append(f"- {r.file_name}：{r.message}")
        return "\n".join(parts) + f"\n当前自定义歌单共 {len(self.songs)} 首。"


def _source_label(source: str) -> str:
    return {"filename": "文件名", "first_line": "首行", "lrc_title": "LRC标签"}.get(
        source, "文件"
    )


def read_text(path: Path) -> str | None:
    """按 utf-8-sig / utf-8 / gb18030 / big5 顺序尝试解码，失败返回 None。"""
    data = path.read_bytes()
    for enc in _ENCODINGS:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def clean_lyric_line(raw: str) -> str:
    """剥掉 LRC 时间轴并去掉首尾空白，返回歌词正文（可能为空）。"""
    line = raw.rstrip("\r\n").strip()
    while True:
        stripped = _TIME_TAG.sub("", line, count=1)
        if stripped == line:
            break
        line = stripped.strip()
    return line.strip()


def base_stem(path: Path) -> str:
    """文件名去掉扩展名和 " (1)" 这类副本后缀。"""
    return _COPY_SUFFIX.sub("", path.stem).strip()


def parse_stem(stem: str) -> StemParts:
    """按约定从文件名里拆出 歌名 / 歌手 / P主。

    约定（拆不出来就留空，不影响后续回退逻辑）:
    - ``珍珠 - 洛天依 - 某P``  -> 歌名「珍珠」，歌手「洛天依」，P主「某P」
    - ``珍珠【某P】``          -> P主「某P」（括号在歌名之后）
    - ``【洛天依】珍珠``        -> 歌手「洛天依」（括号在最开头）

    半角减号要求两侧有空格才认，避免把 ``X-02``、``K-ON`` 这类歌名拆坏。
    全角 ``－`` ``—`` 不要求空格。
    """
    singers = ""
    uploader = ""

    head = _HEAD_BRACKET.match(stem)
    if head:
        singers = head.group(1).strip()
        stem = stem[head.end():]

    brackets = _BRACKET.findall(stem)
    stem = _BRACKET.sub("", stem).strip()

    segments = [s.strip() for s in _SPLIT.split(stem) if s.strip()]
    name = segments[0] if segments else ""
    # 分隔符后面的段落依次填歌手、P主
    for value in segments[1:]:
        if value and not singers:
            singers = value.strip()
        elif value and not uploader:
            uploader = value.strip()
    # 括号标注优先算 P主（最开头的那个已经在上面当作歌手取走了）
    for value in brackets:
        if value and not uploader:
            uploader = value.strip()
        elif value and not singers:
            singers = value.strip()

    if not name or name.lower() in _GENERIC_STEMS:
        name = ""
    return StemParts(name=name, singers=singers, uploader=uploader)


def is_keep_line(line: str, min_line_len: int) -> bool:
    """噪声过滤: 汉字太少（纯数字/纯英文）或过短的句子不入库。"""
    return len(line) >= min_line_len and len(_CJK.findall(line)) >= MIN_CJK_CHARS


def _safe_lookup(
    lookup: Callable[[str], tuple[str, str]] | None, name: str
) -> tuple[str, str]:
    """调用外部 lookup，任何异常都当成"没查到"。"""
    if lookup is None or not name:
        return ("", "")
    try:
        return lookup(name) or ("", "")
    except Exception:
        return ("", "")


def parse_lyrics_file(
    path: Path,
    min_line_len: int = 4,
    max_lines: int = DEFAULT_MAX_LINES,
    meta_lookup: Callable[[str], tuple[str, str]] | None = None,
) -> tuple[ParsedSong | None, str]:
    """解析单个歌词文件，失败时返回 (None, 失败原因)。

    歌名与元数据的取值顺序:
    1. 完整文件名能在库里查到 -> 直接用，不做拆分（避免拆坏 "X-02" 这类歌名）
    2. 按文件名约定拆出 歌名/歌手/P主
    3. 文件名没信息量 -> LRC [ti:] 或首个非空行
    4. 歌手/P主: LRC 标签 > 文件名约定 > 库反查
    """
    text = read_text(path)
    if text is None:
        return None, "无法识别文件编码（请用 UTF-8 或 GBK 保存）"

    meta: dict[str, str] = {}
    body: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        tag = _META_TAG.match(line)
        if tag:
            meta[tag.group(1).lower()] = tag.group(2).strip()
            continue
        content = clean_lyric_line(line)
        if content:
            body.append(content)

    if not body:
        return None, "文件里没有可用的歌词内容"

    stem = base_stem(path)
    name, singers, uploader = "", "", ""
    name_source = ""

    # 1) 完整文件名命中库 -> 原名入库，不拆分
    db_singers, db_uploader = _safe_lookup(meta_lookup, stem)
    if db_singers or db_uploader:
        name, singers, uploader = stem, db_singers, db_uploader
        name_source = "filename"

    # 2) 按文件名约定拆分
    if not name:
        parts = parse_stem(stem)
        name, singers, uploader = parts.name, parts.singers, parts.uploader
        name_source = "filename" if parts.name else ""

    # 3) 文件名没信息量 -> LRC 标题标签或首个非空行
    if not name:
        if meta.get("ti"):
            name = meta["ti"]
            name_source = "lrc_title"
        else:
            name = body[0]
            name_source = "first_line"
            body = body[1:]
            if not body:
                return None, "只有一行内容，取作歌名后没有歌词剩余"

    kept: list[str] = []
    seen: set[str] = set()
    dropped = 0
    for line in body:
        if len(kept) >= max_lines:
            dropped += 1
            continue
        if line in seen:
            continue
        seen.add(line)
        if is_keep_line(line, min_line_len):
            kept.append(line)
        else:
            dropped += 1

    if not kept:
        return None, "歌词行全部被噪声规则过滤（含汉字少于 2 个或过短）"

    # 4) LRC 标签最具体，优先于文件名约定
    singers = meta.get("ar") or singers
    # 中V的 P 主在 LRC 里没有标准标签，按常见写法依次尝试
    uploader = meta.get("by") or meta.get("au") or meta.get("re") or uploader
    # 5) 还缺什么再用库补
    if not singers or not uploader:
        db_singers, db_uploader = _safe_lookup(meta_lookup, name)
        singers = singers or db_singers
        uploader = uploader or db_uploader

    return ParsedSong(
        name=name,
        lines=kept,
        singers=singers,
        uploader=uploader,
        name_source=name_source,
        dropped=dropped,
    ), ""


def load_songs(asset_dir: Path) -> list[dict[str, Any]]:
    """读取 assets/user_songs.json，格式异常时当作空歌单。"""
    path = asset_dir / SONGS_FILE_NAME
    if not path.exists():
        return []
    try:
        data = json.loads(read_text(path) or "[]")
    except Exception:
        return []
    return data if isinstance(data, list) else []


def save_songs(asset_dir: Path, songs: list[dict[str, Any]]) -> None:
    """原子写回 user_songs.json（先写临时文件再替换）。"""
    asset_dir.mkdir(parents=True, exist_ok=True)
    path = asset_dir / SONGS_FILE_NAME
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(songs, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(path)


def merge_song(songs: list[dict[str, Any]], parsed: ParsedSong) -> tuple[bool, int]:
    """把解析结果并入歌单，返回 (是否新增, 合并后的歌词行数)。"""
    for song in songs:
        if not isinstance(song, dict):
            continue
        if str(song.get("name") or "").strip() == parsed.name:
            old = song.get("lyrics")
            lines = old.splitlines() if isinstance(old, str) else [str(x) for x in old or []]
            merged = list(lines)
            known = set(merged)
            for line in parsed.lines:
                if line not in known:
                    merged.append(line)
                    known.add(line)
            song["lyrics"] = merged
            # 只补空字段，不覆盖用户手填过的歌手/P主
            if parsed.singers and not song.get("singers"):
                song["singers"] = parsed.singers
            if parsed.uploader and not song.get("uploader"):
                song["uploader"] = parsed.uploader
            return False, len(merged)
    songs.append(
        {
            "name": parsed.name,
            "singers": parsed.singers,
            "uploader": parsed.uploader,
            "lyrics": parsed.lines,
        }
    )
    return True, len(parsed.lines)


def _unique_target(directory: Path, file_name: str) -> Path:
    """目标文件已存在时追加 _1、_2 … 后缀，避免覆盖历史文件。"""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / file_name
    if not target.exists():
        return target
    stem, suffix = Path(file_name).stem, Path(file_name).suffix
    for i in range(1, 1000):
        candidate = directory / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem}_{int(time.time())}{suffix}"


def scan_inbox(inbox: Path) -> list[Path]:
    """列出收件箱里待导入的歌词文件（只看 inbox 根层，不递归进 imported/failed）。"""
    if not inbox.is_dir():
        return []
    return [
        p for p in sorted(inbox.iterdir())
        if p.is_file() and p.suffix.lower() in LYRIC_SUFFIXES
    ]


def flatten_names(value: str) -> str:
    """把库里带换行的歌手字段（如 "言和\\n洛天依"）压成 "言和、洛天依"。"""
    parts = [p.strip() for p in str(value or "").replace("\r", "\n").split("\n")]
    return "、".join(p for p in parts if p)


def run_import(
    asset_dir: Path,
    min_line_len: int = 4,
    max_lines: int = DEFAULT_MAX_LINES,
    meta_lookup: Callable[[str], tuple[str, str]] | None = None,
) -> ImportReport:
    """扫描收件箱 -> 解析 -> 合并进 user_songs.json -> 归档文件。

    meta_lookup: 按歌名查 (歌手, P主) 的回调，用于给库里已有的歌自动补元数据。
    """
    asset_dir = Path(asset_dir)
    inbox = asset_dir / INBOX_DIR_NAME
    imported_dir = inbox / IMPORTED_DIR_NAME
    failed_dir = inbox / FAILED_DIR_NAME

    files = scan_inbox(inbox)
    if not files:
        return ImportReport(songs=load_songs(asset_dir))

    songs = load_songs(asset_dir)
    results: list[FileResult] = []

    for path in files:
        parsed, err = parse_lyrics_file(path, min_line_len, max_lines, meta_lookup)
        if parsed is None:
            results.append(FileResult(path.name, False, err))
            _archive(path, failed_dir)
            continue
        _, line_count = merge_song(songs, parsed)
        detail = f"入库 {len(parsed.lines)} 句"
        if parsed.dropped:
            detail += f"，过滤 {parsed.dropped} 句"
        credit = " / ".join(x for x in (flatten_names(parsed.singers), flatten_names(parsed.uploader)) if x)
        if credit:
            detail += f"（{credit}）"
        results.append(
            FileResult(
                file_name=path.name,
                ok=True,
                message=detail,
                song_name=parsed.name,
                line_count=line_count,
                name_source=parsed.name_source,
            )
        )
        _archive(path, imported_dir)

    if any(r.ok for r in results):
        save_songs(asset_dir, songs)
    return ImportReport(results=results, songs=songs)


def _archive(path: Path, target_dir: Path) -> None:
    """把处理过的文件移出收件箱，移动失败只记录不影响主流程。"""
    try:
        shutil.move(str(path), str(_unique_target(target_dir, path.name)))
    except OSError:
        pass
