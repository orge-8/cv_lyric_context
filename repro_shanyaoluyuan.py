# -*- coding: utf-8 -*-
"""本地复现《山遥路远》重抓管线：parse_wikitext -> build_record -> clean_lyrics。

页面结构按用户诊断输出还原：
- 章节：简介 / 歌曲 / 歌词 / STAFF的話
- 歌词章节：<poem> 未闭合，首个 {{color|#572f58| 跨行未闭合，
  MediaWiki 渲染时自动闭合到章节末尾。
"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

BASE = Path(__file__).resolve().parent

SOURCE = """{{信息|演唱=乐正绫|UP主=Yu H.|作词=绿无|作曲=MeLo}}
'''《山遥路远》'''是Yu H.投稿，乐正绫演唱的中文原创歌曲。

== 简介 ==
《山遥路远》讲述了一段跨越山海的旅程。

== 歌曲 ==
{{BilibiliVideo|id=BV1xxx}}

== 歌词 ==
{{LyricsKai/poem 题头模板}}
<poem>
{{color|#572f58|我借你梦想的时间　让你走得足够遥远
我让你心中的山川　跋涉去不用归还
清风浪海　翘马花剑
踏过千重雪浪
望尽万仞孤烟
你眉间一点朱砂
是我不敢惊动的春天
}}'''
== STAFF的話 ==
写给所有在路上的人。
"""


def main() -> None:
    from vcpedia_wikitext_parser import parse_wikitext
    from vcpedia_sync import build_record
    from vcpedia_text_clean import clean_lyrics

    parsed = parse_wikitext("山遥路远", SOURCE)
    raw_lyrics = parsed.get("lyrics") or ""
    print(f"[1] parse_lyrics 原始输出: {len(raw_lyrics)} 字")
    print(raw_lyrics)
    print("-" * 60)

    cleaned = clean_lyrics(raw_lyrics)
    print(f"[2] clean_lyrics 之后: {len(cleaned)} 字")
    print(cleaned)
    print("-" * 60)

    record = build_record("山遥路远", parsed, "Category:乐正绫歌曲")
    final = record.get("lyrics") or ""
    lines = final.splitlines()
    print(f"[3] build_record 最终入库: {len(final)} 字, {len(lines)} 行")
    for ln in lines[:8]:
        print(f"    | {ln}")

    target = "我借你梦想的时间"
    ok = len(raw_lyrics) > 50 and len(cleaned) > 50 and target in final
    print()
    print("结论:", "管线正常，目标行已在最终歌词中" if ok else "!! 管线某环节把歌词清空了")

    # 对照：旧版解析器（无未闭合 poem 分支）在此结构下的行为
    import re
    tail = SOURCE[SOURCE.index("== 歌词 ==") + len("== 歌词 =="):]
    nxt = re.search(r"^={1,2}\s*[^=\n]", tail, re.M)
    if nxt:
        tail = tail[: nxt.start()]
    poems = re.findall(r"<poem[^>]*>(.*?)</poem>", tail, re.S)
    print(f"[对照] 旧版成对 </poem> 正则在此页面匹配数: {len(poems)}（0 即旧解析器必然返回空）")


if __name__ == "__main__":
    main()
