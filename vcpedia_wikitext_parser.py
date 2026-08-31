"""VCPedia wikitext 解析：从词条源码提取创作人员、简介与完整歌词。

移植自 mohobot 的 mohobot/music_knowledge/vcpedia.py 解析部分（MIT License，
仓库 https://github.com/CarefreeSongs712/mohobot），去掉了对 bs4 / SQLAlchemy 的依赖。

VCPedia 词条形态要点：
- 创作人员写在 {{信息|演唱=...}} 或 |作词=... 表格行里，键名常带 <br/> 复合；
- 歌词章节标题不统一（== 歌词 == / == 普通的歌词 ==，还有用繁体「歌詞」的），
  正文多用 <poem> 包裹，行内嵌 {{color|样式|歌词}} / {{交叉颜色|c1=|c2=|歌词}} 等模板；
- 少数页面用 {{LyricsKai|...|original=歌词}} 模板；
- 还有不用 <poem> 的排版：整段 <div> + <br> 分行、每行一个 {{color|...}}，
  或把歌词塞在 {{Lyrics}} 的 lb-textN 参数里（见 salvage_colored_lines）；
- 歌词章节必须截到下一个同级标题为止——「二次创作」章节会收录几十首翻唱词。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

_TAG_RE = re.compile(r"<[^>]*>")  # 判断命名参数前先抹掉标签（如 <ref name="一">）

# 兜底排版里承载歌词的行内颜色模板（见 salvage_colored_lines）
_COLOR_TPL_RE = re.compile(
    r"\{\{\s*(?:color|coloredlink|crosscolor|交叉颜色|shadowcolor|lj)\s*\|", re.I
)

# STAFF 表 / 信息行常见的键别名（按顺序匹配）
CREDIT_ALIASES: Dict[str, List[str]] = {
    "uploader": ["UP主", "投稿者", "发布者", "UP"],
    "singers": ["演唱", "歌手", "演唱者"],
    "lyricist": ["作词", "词作", "作詞", "填词", "填詞"],
    "composer": ["作曲", "曲作", "作曲者"],
    "arranger": ["编曲", "编曲者", "編曲"],
    "mixer": ["混音", "混合", "remix", "混音后期"],
    "tuner": ["调教", "调校", "调声", "調教", "VOCALOID调教"],
    "mastering": ["母带", "母带处理"],
    "pv": ["PV", "视频制作", "映像", "影片", "MV编导", "MV制作"],
    "illustrator": ["曲绘", "绘", "插画", "绘图", "曲繪"],
}


def _clean_value(v: str) -> str:
    """清洗 wikitext 值：去链接/模板/全角空格/多余空白。"""
    if not v:
        return ""
    v = re.sub(r"\[\[([^\]|]*)\|?[^\]]*\]\]", r"\1", v)      # [[a|b]] → a
    v = re.sub(r"\[(?:https?://)?[^\s\]]+\s([^\]]*)\]", r"\1", v)  # [url 文字] → 文字
    v = re.sub(r"<ref[^>]*>.*?</ref>", "", v, flags=re.S)
    v = re.sub(r"<[^>]+>", "", v)
    v = v.replace("\u3000", " ")
    v = v.replace("&amp;", "&")
    # 去掉结尾残留的 }}(模板收尾)等
    v = re.sub(r"[}]+$", "", v).strip()
    v = v.strip("|").strip()
    parts = [p for p in re.split(r"[\n]|(?:\|\|)", v) if p.strip()]
    if parts:
        v = parts[0].strip()
    return re.sub(r"\s+", " ", v).strip()


def _clean_credit_values(v: str) -> str:
    """清洗创作人员值：模板内可能用 <br/> 分隔多人，归一为顿号分隔。"""
    v = v.replace("<br/>", "、").replace("<br>", "、")
    v = _clean_value(v)
    # 多个值取全部（如 "平安夜的噩梦／H.K.君"），不要只取第一个
    return v.replace("／", "/")


def _normalize_credit_key(raw: str) -> str:
    """把 wikitext 键名规范化到 CREDIT_ALIASES 的标准键。

    真实词条中键常带 <br/> 复合，如 "作编曲<br/>作词<br/>吉他<br/>混音"：
    先取 <br 前的内容精确匹配，再退化为前缀匹配。
    """
    raw0 = raw.split("<br")[0].strip()
    for key, aliases in CREDIT_ALIASES.items():
        for a in aliases:
            if raw0 == a or raw0.startswith(a):
                return key
    # 复合键：取第一个别名（作编曲 → 编曲方向）
    for key, aliases in CREDIT_ALIASES.items():
        for a in aliases:
            if raw.startswith(a):
                return key
    return ""


def parse_credits(lines: List[str]) -> Dict[str, str]:
    """从 wikitext 逐行解析创作人员与年份，返回 {标准键: 值}。

    兼容两种形态：{{信息|演唱=洛天依|作词=...}} 一行内多次赋值，
    以及 "|作词=青柠" 这类表格行。每行只取第一个命中键，防止串扰。
    """
    credits: Dict[str, str] = {}
    for line in lines:
        line = (line or "").strip()
        if not line:
            continue
        tokens = re.split(r"[|{}]", line)
        for token in tokens:
            token = token.strip()
            if not token:
                continue
            m = re.match(r"^([^=：:]{1,16})\s*[=：:]\s*(.+)$", token)
            if m:
                key_raw, val_raw = m.group(1).strip(), m.group(2).strip()
                key = _normalize_credit_key(key_raw)
                if key and key not in credits:
                    val = _clean_credit_values(val_raw)
                    if val and len(val) < 200:
                        credits[key] = val
                        break
        if "year" not in credits:
            m = re.search(r"(20\d{2})\s*年", line)
            if m:
                credits["year"] = m.group(1)
    return credits


# ── 歌词模板展开 ────────────────────────────────────────────────

def _template_tail(tpl: str) -> str:
    """把 {{color|样式|正文}} / {{交叉颜色|c1=..|c2=..|正文}} 里的正文取出。

    只把**模板外层**的 '|' 当参数/正文分段，正文内部嵌套模板的 '|' 保持原样，
    由 _expand_inline_templates 递归展开。
    """
    # 调用方可能把模板前的换行/空白一起带进来（如 <poem> 后紧跟空行再起模板），
    # 不 strip 的话 tpl[2:-2] 切掉的是空白而不是 '{{'，body 里会残留 '{{'，
    # 后续深度扫描从 1 起再也回不到 0，整个模板被当成单个参数跳过 -> 解析为空。
    tpl = tpl.strip()
    if len(tpl) < 4:
        return ""
    body = tpl[2:-2]  # 去掉 {{ }}
    if body.lstrip().startswith("LyricsKai"):
        return extract_lyricskai(tpl)
    if "|" not in body:
        return ""
    # 深度扫描找出外层段（跳过嵌套模板内部）
    parts: List[str] = []
    depth = 0
    cur = ""
    i = 0
    while i < len(body):
        if body.startswith("{{", i):
            depth += 1
            cur += "{{"
            i += 2
        elif body.startswith("}}", i):
            depth -= 1
            cur += "}}"
            i += 2
        elif body[i] == "|" and depth == 0:
            parts.append(cur)
            cur = ""
            i += 1
        else:
            cur += body[i]
            i += 1
    parts.append(cur)

    def is_param(p: str) -> bool:
        p = p.strip()
        if not p:
            return True
        # 含嵌套模板的段是正文容器，即使其中含 c1=/c2= 也不能按命名参数跳过
        if "{{" in p:
            return False
        # 命名参数（ltcolor = #fff / 段落=1 / 策划<br />作词 = [[xxx]]）：
        # '=' 出现在第一个换行之前、且去掉标签后的键名不长。
        # 只判断"含 =" 会误伤正文里内嵌的 <ref name="一">——整段歌词会被当成
        # 命名参数跳过，一首歌直接解析为空；反过来要求"='紧跟段首"又会漏掉
        # 键名里带 <br /> 的参数，把 STAFF 表当成歌词。
        head = _TAG_RE.sub("", p.split("\n", 1)[0])
        if "=" in head:
            key, _, value = head.partition("=")
            key = key.strip()
            if key.isdigit():
                # 位置参数 {{color|1=#4a2206|2=歌词}}：值就是正文，去掉 2= 前缀后
                # 再走下面的样式判断（1=#4a2206 是色值，照旧跳过）
                p = value.strip()
            elif key and len(key) <= 40:
                return True
            # elif 键名为空或过长：不是命名参数，继续按正文判断
        # 纯色值/样式/数字（不含中文与非样式符号）
        if re.match(r"^[#\w;:,.()\- ]+$", p) and not re.search(r"[\u4e00-\u9fff]", p) \
                and len(p) <= 80:
            return True
        return False

    start = 1  # 跳过模板名 parts[0]
    while start < len(parts) and is_param(parts[start]):
        start += 1
    if start >= len(parts):
        return ""
    # 位置参数 {{color|1=#4a2206|2=歌词}}：正文前面带 "2="，去掉编号前缀
    texts = [re.sub(r"^\s*\d+\s*=", "", p) for p in parts[start:]]
    # 正文可能有多段，段间应为换行而不是字面 '|'
    return "\n".join(texts).strip(" \n|")


def _expand_inline_templates(text: str) -> str:
    """把歌词里的行内模板 {{color|样式|正文}} / {{ruby|...|正文}} 展开为正文。

    逐字符扫描处理嵌套（如 {{color|black|-{歌词}-}} 内含花括号对）。
    """
    out: List[str] = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("{{", i):
            j = i + 2
            depth = 1
            while j < n and depth:
                if text.startswith("{{", j):
                    depth += 1
                    j += 2
                elif text.startswith("}}", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            if depth != 0:
                # 模板没闭合（如章节截断把收尾的 }} 切掉）：视为在文本末尾闭合，
                # 取出正文，而不是整行丢弃（丢弃会吞掉歌词的第一句）。
                out.append(_template_tail(text[i:] + "}}"))
                break
            out.append(_template_tail(text[i:j]))
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _fully_expand_templates(text: str) -> str:
    """迭代展开全部行内模板，直到不再出现 '{{'（最多 10 轮防死循环）。"""
    for _ in range(10):
        if "{{" not in text:
            break
        next_text = _expand_inline_templates(text)
        if next_text == text:
            break
        text = next_text
    return text


def _is_template_junk(seg: str) -> bool:
    """判断歌词段落是否为模板样式残留（应丢弃）。"""
    s = seg.strip()
    if not s:
        return True
    if s in ("交叉颜色", "color", "颜色", "ruby", "ps", "|", "-{", "}-"):
        return True
    # 行内的 '|'（如 "A1|"）属于模板分段残留
    if "|" in s and not re.search(r"[\u4e00-\u9fff]", s.replace("|", "")):
        return True
    if re.match(r"^[#\w;:,.()\- ]+$", s) and len(s) <= 60 \
            and not re.search(r"[\u4e00-\u9fff]", s):
        return True
    return False


def _unwrap_poem(inner: str) -> str:
    """展开 <poem> 内部内容：处理 {{color|样式|正文}} / {{交叉颜色|a|b|正文}} 等。

    策略：按括号深度拆块；只有**模板外**的 '|' 才是歌词段落分隔。
    """
    out: List[str] = []
    buf = ""
    depth = 0
    i = 0
    while i < len(inner):
        if inner.startswith("{{", i):
            # 模板开始：先把模板前已累积的正文独立成段，避免块级模板
            # （如 {{LyricsKai...}}）与前面的歌词混在同一 buf。
            # 模板前的空白一律丢弃——留着会让 _template_tail 的切片下标错位。
            if depth == 0:
                if buf.strip():
                    out.append(buf)
                buf = ""
            depth += 1
            buf += "{{"
            i += 2
            continue
        if inner.startswith("}}", i):
            depth -= 1
            buf += "}}"
            i += 2
            if depth == 0:
                out.append(_template_tail(buf))
                buf = ""
            continue
        if depth == 0 and inner[i] == "|":
            # 模板外 '|' = 歌词段落分隔
            if buf.strip():
                out.append(buf)
            buf = ""
            i += 1
            continue
        buf += inner[i]
        i += 1
    if buf.strip():
        out.append(buf)
    merged: List[str] = []
    for piece in out:
        if not piece:
            continue
        for raw_seg in piece.split("\n"):
            expanded = _fully_expand_templates(raw_seg).strip()
            for seg in expanded.replace("|", "\n").split("\n"):
                seg = seg.strip()
                if not seg or _is_template_junk(seg):
                    continue
                merged.append(seg)
    text = "\n".join(merged)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def salvage_colored_lines(tail: str) -> str:
    """兜底排版：既没有 <poem> 也不用 LyricsKai 的页面。

    《八重回归》《传说史册》《Atlantis》《九生相》这类页面不用 <poem>：歌词直接
    放在 {{color|样式|歌词}}（或 crosscolor / 交叉颜色 / shadowcolor / lj）里，
    用 <br> 或模板边界分行，外层是 <div> 或 {{Lyrics}} 容器。

    这里**只取颜色类模板的正文**，不整段展开——整段展开会把 STAFF 模板的参数
    （ltcolor = #fff、group1 = 策划）也一起当成歌词。正文里 <br> 换换行、去标签
    与 wiki 标记，且每行至少 4 个汉字才算歌词（挡掉颜色图例里的「紫色字」之类）。

    最后要求至少 3 行合格，否则返回空：只有 STAFF 模板的页面（如《寄生虫》的
    歌词章节其实没有词）不该被塞进一堆模板残渣。
    """
    lines: List[str] = []
    for m in _COLOR_TPL_RE.finditer(tail):
        start = m.start()
        depth, j = 0, start
        while j < len(tail):
            if tail.startswith("{{", j):
                depth += 1
                j += 2
            elif tail.startswith("}}", j):
                depth -= 1
                j += 2
                if depth == 0:
                    break
            else:
                j += 1
        chunk = _fully_expand_templates(tail[start:j])
        chunk = re.sub(r"<br\s*/?>", "\n", chunk, flags=re.I)
        chunk = re.sub(r"<[^>]+>", "", chunk)
        chunk = chunk.replace("'''", "").replace("''", "")
        for ln in chunk.splitlines():
            ln = ln.strip()
            if ln and len(re.findall(r"[\u4e00-\u9fff]", ln)) >= 4:
                lines.append(ln)
    if len(lines) < 3:
        return ""
    # 嵌套模板会让同一句被展开两次（如 shadowcolor>textHover>color）
    return "\n".join(ln for i, ln in enumerate(lines) if i == 0 or ln != lines[i - 1])


def _lyricskai_param(body: str, key: str) -> str:
    """取 LyricsKai 某个命名参数（如 original / translated）的正文。

    写法很不一致：`|original=`、`|original = `、`|original =` 都有，所以键名两侧
    都允许空白。行里的 `#NoHover` 是 LyricsKai/hover 的分段标记，不是歌词。
    """
    m = re.search(rf"\|\s*{key}\s*=", body)
    if not m:
        return ""
    out: List[str] = []
    started = False
    for raw in m.string[m.end():].split("\n"):
        # 另起一个命名参数（如 |translated= 独占一行）：到此为止
        if re.match(r"^\s*\|[a-zA-Z]+\s*=", raw):
            break
        ln = raw
        # 与歌词最后一行同行的参数（如 歌词|translated=），行内截断更稳
        cut = re.search(r"\|[a-zA-Z]+\s*=", ln)
        if cut:
            ln = ln[:cut.start()]
        ln = ln.strip()
        if ln.startswith("#"):  # LyricsKai/hover 的分段标记
            continue
        if not ln:
            # 参数值开头的空行跳过；段落之间的空行保留（分段用）
            if started:
                out.append("")
            continue
        out.append(ln)
        started = True
    text = _expand_inline_templates("\n".join(out).strip())
    return text.replace("-{", "").replace("}-", "")


def strip_templates(text: str) -> str:
    """去掉所有 {{...}} 模板（含嵌套），只留模板外的正文。"""
    out: List[str] = []
    depth = 0
    i = 0
    while i < len(text):
        if text.startswith("{{", i):
            depth += 1
            i += 2
            continue
        if text.startswith("}}", i):
            depth = max(0, depth - 1)
            i += 2
            continue
        if depth == 0:
            out.append(text[i])
        i += 1
    return "".join(out)


def salvage_plain_text(tail: str) -> str:
    """第二档兜底：歌词直接写在 <div>/<span> 里，一个颜色模板都没有。

    《那个夏天》这类页面整段用 <div style="..."> + <br /> 分行、个别字用 <span>
    调字号，既没有 <poem> 也没有颜色模板。先去掉模板（STAFF 表随之消失），再把
    <br> 换换行、去标签，剩下的就是歌词。同样要求 ≥3 行「≥4 汉字」才认——只有
    STAFF 模板的页面（如《寄生虫》）去完模板什么都不剩。
    """
    text = strip_templates(tail)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</?(?:div|p|span|font|b|i|u|small|big)[^>]*>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("'''", "").replace("''", "")
    text = re.sub(r"^\s*[*#:;].*$", "", text, flags=re.M)
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if len(re.findall(r"[\u4e00-\u9fff]", ln)) >= 4]
    if len(lines) < 3:
        return ""
    return "\n".join(ln for i, ln in enumerate(lines) if i == 0 or ln != lines[i - 1])


def extract_lyricskai(tail: str) -> str:
    """从 {{LyricsKai|...|original=...}} 模板提取歌词。

    部分页面（如 九九八十一(乐正绫)）的歌词在 LyricsKai 的 original= 参数里，
    行内是 {{color|black|-{歌词}-}} 形式；初音未来等日文歌还带 |translated= 中文
    译文，一并收录——用户可能引用原文也可能引用译文。
    """
    m = re.search(r"\{\{LyricsKai\b", tail)
    if not m:
        return ""
    # 找模板闭合：depth 从 1 起，忽略 -{-}/-} 花括号对
    depth = 1
    end = len(tail)
    i = m.start() + 2
    while i < len(tail):
        if tail.startswith("{{", i):
            depth += 1
            i += 2
        elif tail.startswith("}}", i):
            depth -= 1
            if depth == 0:
                end = i
                break
            i += 2
        else:
            i += 1
    if depth != 0:
        return ""
    body = tail[m.start() + 2:end]
    text = _lyricskai_param(body, "original")
    if not text.strip():
        return ""
    translated = _lyricskai_param(body, "translated")
    if translated.strip():
        text = f"{text}\n{translated}"
    return text


def parse_lyrics(source: str) -> str:
    """从 wikitext 提取完整歌词（空字符串表示未找到）。

    只截取到下一个同级标题为止——「二次创作」章节收录了所有衍生作品歌词，
    若一并抓取会把几十首翻唱词灌进原曲。
    """
    # 歌詞/歌词 两种写法都有页面在用（如 If You Want Me 用的是「歌詞」）
    m = re.search(r"^={2,4}\s*[^=\n]*歌[词詞][^=\n]*\s*={2,4}\s*$", source, re.M)
    if not m:
        return ""
    level = len(m.group(0)) - len(m.group(0).lstrip("="))
    tail = source[m.end():]
    # 截到下一个同级（或更高级）标题，如 "== 二次创作 =="
    nxt = re.search(rf"^={{1,{level}}}\s*[^=\n]", tail, re.M)
    if nxt:
        tail = tail[:nxt.start()]
    poems = list(re.finditer(r"<poem[^>]*>(.*?)</poem>", tail, re.S))
    if poems:
        texts = [_unwrap_poem(pm.group(1)) for pm in poems]
        text = "\n\n".join(t for t in texts if t.strip())
    elif re.search(r"<poem\b", tail):
        # <poem> 没闭合（如《山遥路远》）。MediaWiki 渲染时会自动闭合到页面/章节末尾，
        # 但正则要求成对的 </poem>，匹配不到就整个落空。取 <poem> 之后到章节末尾。
        m_open = re.search(r"<poem[^>]*>(.*)$", tail, re.S)
        text = _unwrap_poem(m_open.group(1)) if m_open else ""
    else:
        text = extract_lyricskai(tail)
        if not text.strip():
            # LyricsKai 也没有：<div>/<br> + 颜色模板 的排版
            text = salvage_colored_lines(tail)
        if not text.strip():
            # 连颜色模板都没有：歌词直接写在 <div>/<span> 里
            text = salvage_plain_text(tail)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"\{\{[^}]*\}\}", "", text)
    text = text.replace("\u3000", " ").strip()
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def parse_introduction(source: str) -> str:
    """从 wikitext 提取简介（「简介」章节，样式化标题也能命中）。"""
    m = re.search(r"^={2,4}\s*[^=\n]*简介[^=\n]*\s*={2,4}\s*$", source, re.M)
    if not m:
        return ""
    tail = source[m.end():]
    lines: List[str] = []
    for line in tail.splitlines():
        if re.match(r"^\s*={2,4}\s*[^=\n]*\s*={2,4}\s*$", line):
            break
        s = line.strip()
        if not s or s.startswith("{{"):
            continue
        s = re.sub(r"<ref[^>]*>.*?</ref>", "", s, flags=re.S)
        s = re.sub(r"<[^>]+>", "", s)
        s = s.replace("[[", "").replace("]]", "").replace("\u3000", " ")
        lines.append(s)
    return " ".join(lines).strip()[:500]


def parse_wikitext(name: str, source: str) -> Dict[str, Any]:
    """解析词条 wikitext，返回 {name, credits, introduction, lyrics}。"""
    return {
        "name": name,
        "credits": parse_credits(source.splitlines()),
        "introduction": parse_introduction(source),
        "lyrics": parse_lyrics(source),
    }
