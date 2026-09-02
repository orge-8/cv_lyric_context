"""诊断 VCPedia 词条的歌词解析问题（在 MaiBot 机器上用其 Python 运行）。

用法（在本插件目录下）:
    python check_lyrics_parse.py 山遥路远
    python check_lyrics_parse.py 山遥路远 --insecure

自动读取 config.toml 的 [crawler] 配置（base_url / verify_ssl / ca_bundle）。
输出：解析器版本、所有章节标题、parse_lyrics 的逐步追踪、歌词章节原文，
并把完整 wikitext 落盘到 source_dump.txt，便于逐字节核对。
"""

import argparse
import logging
import re
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_PLUGIN_DIR))

from vcpedia_client import VCPediaClient  # noqa: E402
from vcpedia_wikitext_parser import (  # noqa: E402
    _unwrap_poem,
    extract_lyricskai,
    parse_lyrics,
    parse_wikitext,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

_HEADING_RE = re.compile(r"^={2,4}\s*[^=\n]*歌词[^=\n]*\s*={2,4}\s*$", re.M)


def load_crawler_config() -> dict:
    cfg = {"base_url": "https://vcpedia.cn", "verify_ssl": True, "ca_bundle": ""}
    path = _PLUGIN_DIR / "config.toml"
    if not path.is_file():
        return cfg
    try:
        import tomllib  # Python 3.11+
        with path.open("rb") as f:
            data = tomllib.load(f)
        crawler = data.get("crawler", {}) or {}
    except ModuleNotFoundError:
        crawler = {}
        section = False
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line == "[crawler]"
                continue
            if section and "=" in line:
                key, _, value = line.partition("=")
                crawler[key.strip()] = value.strip().strip('"').strip("'")
    for key in cfg:
        if key in crawler:
            cfg[key] = crawler[key]
    if isinstance(cfg.get("verify_ssl"), str):
        cfg["verify_ssl"] = cfg["verify_ssl"].lower() not in ("false", "0", "no")
    return cfg


def probe_parser_file() -> None:
    """探测磁盘上的解析器文件是否为 v2.3.0+（含未闭合 poem 修复）。"""
    import vcpedia_wikitext_parser as p
    src = Path(p.__file__).read_text(encoding="utf-8")
    print(f"== 解析器文件 == {Path(p.__file__).name}")
    print(f"   未闭合 <poem> 修复: {'已包含' if '<poem> 没闭合' in src else '!! 不包含(旧版)'}")
    print(f"   未闭合模板尾修复: {'已包含' if '模板没闭合' in src else '!! 不包含(旧版)'}")


def dump_headings(source: str) -> None:
    print("\n== 页面全部标题（含级别）==")
    for line in source.splitlines():
        m = re.match(r"^(=+)\s*([^=\n]+?)\s*=+\s*$", line)
        if m:
            print(f"  L{len(m.group(1))} {m.group(2).strip()}")
    if not _HEADING_RE.search(source):
        print("  !! 没有任何能被 parse_lyrics 匹配的「歌词」标题")


def trace_parse(source: str) -> None:
    """逐步追踪 parse_lyrics 内部，定位到底哪一步产出为空。"""
    print("\n== parse_lyrics 逐步追踪 ==")
    m = _HEADING_RE.search(source)
    if not m:
        print("  1. 歌词标题: !! 无匹配 -> 直接返回空")
        print("     含「歌词」的行:")
        for line in source.splitlines():
            if "歌词" in line:
                print(f"       {line[:80]!r}")
        return
    heading = m.group(0)
    level = len(heading) - len(heading.lstrip("="))
    print(f"  1. 歌词标题: {heading!r} (级别 {level})")

    tail = source[m.end():]
    nxt = re.search(rf"^={{1,{level}}}\s*[^=\n]", tail, re.M)
    print(f"  2. 下一个同级标题: {nxt.group(0)!r}" if nxt else "  2. 下一个同级标题: 无")
    if nxt:
        tail = tail[:nxt.start()]
    print(f"  3. 章节正文: {len(tail)} 字符")

    opens = len(re.findall(r"<poem\b", tail))
    closes = len(re.findall(r"</poem\s*>", tail))
    poems = list(re.finditer(r"<poem[^>]*>(.*?)</poem>", tail, re.S))
    print(f"  4. <poem> 开 {opens} 个 / </poem> 关 {closes} 个 / 成对匹配 {len(poems)} 段")

    if poems:
        print("  5. 分支: 成对 poem")
        for i, pm in enumerate(poems):
            text = _unwrap_poem(pm.group(1))
            print(f"     poem[{i}] 展开后 {len(text)} 字: {text[:80]!r}")
    elif opens:
        print("  5. 分支: 未闭合 poem（取 <poem> 到章节末尾）")
        m_open = re.search(r"<poem[^>]*>(.*)$", tail, re.S)
        inner = m_open.group(1) if m_open else ""
        print(f"     <poem> 之后内容: {len(inner)} 字符")
        print(f"     原文前 200: {inner[:200]!r}")
        text = _unwrap_poem(inner)
        print(f"     _unwrap_poem 结果: {len(text)} 字")
        if text:
            print(f"     前 200: {text[:200]!r}")
        else:
            print("     !! 展开为空 —— 逐行看 _unwrap_poem 内部:")
            for i, line in enumerate(inner.split("\n")[:12]):
                print(f"       行{i}: {line[:100]!r}")
    else:
        print("  5. 分支: LyricsKai / 其它（正文里没有 <poem>）")
        text = extract_lyricskai(tail)
        print(f"     extract_lyricskai 结果: {len(text)} 字")
        print(f"     正文前 300: {tail[:300]!r}")

    result = parse_lyrics(source) or ""
    print(f"  6. parse_lyrics 最终: {len(result)} 字 / {len(result.splitlines())} 行")
    if result:
        for i, line in enumerate(result.splitlines()[:6]):
            print(f"     | {line}")


def dump_section(source: str) -> None:
    m = _HEADING_RE.search(source)
    if not m:
        print("\n!! 找不到歌词章节，无法 dump")
        return
    level = len(m.group(0)) - len(m.group(0).lstrip("="))
    tail = source[m.end():]
    nxt = re.search(rf"^={{1,{level}}}\s*[^=\n]", tail, re.M)
    if nxt:
        tail = tail[:nxt.start()]
    print(f"\n== 歌词章节原文（{len(tail)} 字符）==")
    print(tail[:1500])
    if len(tail) > 1500:
        print(f"  …（省略 {len(tail) - 1500 - 500} 字符）…")
        print("  [末尾 500 字符]")
        print(tail[-500:])


def main() -> int:
    parser = argparse.ArgumentParser(description="VCPedia 歌词解析诊断")
    parser.add_argument("title", nargs="?", default="山遥路远", help="词条名")
    parser.add_argument("--base", default="", help="覆盖 base_url")
    parser.add_argument("--ca", default="", help="覆盖 ca_bundle（PEM/CER 路径）")
    parser.add_argument("--insecure", action="store_true", help="跳过 SSL 校验")
    args = parser.parse_args()

    cfg = load_crawler_config()
    base_url = args.base or cfg["base_url"]
    verify_ssl = False if args.insecure else cfg["verify_ssl"]
    ca_bundle = args.ca or cfg["ca_bundle"]

    print(f"== 配置 == base={base_url} verify_ssl={verify_ssl} ca_bundle={ca_bundle or '(无)'}")
    probe_parser_file()

    client = VCPediaClient(
        base_url=base_url, verify_ssl=verify_ssl, ca_bundle=ca_bundle,
        logger=logging.getLogger("check"),
    )
    source = client.fetch_wikitext(args.title)
    if not source:
        print("!! 拉取词条源码失败")
        return 1
    print(f"\n== 源码长度: {len(source)} 字符 ==")

    dump_headings(source)
    parsed = parse_wikitext(args.title, source)
    lyrics = parsed.get("lyrics") or ""
    print(f"\n== 解析结果 == 简介 {len(parsed.get('introduction') or '')} 字, "
          f"歌词 {len(lyrics)} 字 / {len(lyrics.splitlines())} 行")

    trace_parse(source)
    dump_section(source)

    dump_path = _PLUGIN_DIR / "source_dump.txt"
    dump_path.write_text(source, encoding="utf-8")
    print(f"\n== 完整源码已写入 {dump_path} ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
