"""诊断 VCPedia 词条的歌词解析问题（在 MaiBot 机器上用其 Python 运行）。

用法（在本插件目录下）:
    python check_lyrics_parse.py 山遥路远
    python check_lyrics_parse.py 山遥路远 --insecure

自动读取 config.toml 的 [crawler] 配置（base_url / verify_ssl / ca_bundle）。
输出：页面所有章节标题、歌词解析结果、目标句子在源码里的原始形态，
帮助判断歌词为什么没被解析出来。
"""

import argparse
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_PLUGIN_DIR))

from vcpedia_client import VCPediaClient  # noqa: E402
from vcpedia_wikitext_parser import parse_wikitext, parse_lyrics  # noqa: E402


def _parser_version_probe() -> None:
    """探测磁盘上的 vcpedia_wikitext_parser.py 是否为 v2.3.0（含未闭合 poem 修复）。"""
    import vcpedia_wikitext_parser as p
    src = Path(p.__file__).read_text(encoding="utf-8")
    has_unclosed_poem = "<poem> 没闭合" in src
    has_tail_fix = "模板没闭合" in src
    mtime = datetime.fromtimestamp(Path(p.__file__).stat().st_mtime)
    print(f"== 解析器文件 == {Path(p.__file__).name}  修改时间 {mtime:%Y-%m-%d %H:%M:%S}")
    print(f"   未闭合 <poem> 修复: {'已包含(v2.3.0)' if has_unclosed_poem else '!! 不包含(旧版)'}")
    print(f"   未闭合模板尾修复: {'已包含(v2.3.0)' if has_tail_fix else '!! 不包含(旧版)'}")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


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
    _parser_version_probe()
    client = VCPediaClient(
        base_url=base_url, verify_ssl=verify_ssl, ca_bundle=ca_bundle,
        logger=logging.getLogger("check"),
    )
    source = client.fetch_wikitext(args.title)
    if not source:
        print("!! 拉取词条源码失败")
        return 1
    print(f"\n== 源码长度: {len(source)} 字符 ==")

    print("\n== 页面章节标题 ==")
    headings = re.findall(r"^(={2,6}\s*[^=\n]+?\s*={2,6})\s*$", source, re.M)
    for h in headings:
        print(" ", h.strip())
    if not any("歌词" in h for h in headings):
        print("  （没有任何包含「歌词」的章节标题！）")

    parsed = parse_wikitext(args.title, source)
    lyrics = parsed.get("lyrics") or ""
    print(f"\n== 解析结果 == 简介 {len(parsed.get('introduction') or '')} 字, "
          f"歌词 {len(lyrics)} 字 / {len(lyrics.splitlines())} 行")
    if lyrics:
        print("歌词前 10 行:")
        for line in lyrics.splitlines()[:10]:
            print("  |", line)
    else:
        print("（歌词为空）")

    print("\n== 「我借你梦想」在源码中的位置（前后各 3 行） ==")
    lines = source.splitlines()
    hit = next((i for i, l in enumerate(lines) if "我借你梦想" in l), -1)
    if hit < 0:
        print("  源码里没有这句话")
        if any("/歌词" in h or h.endswith("歌词") for h in headings):
            print("  提示: 存在歌词章节，但正文可能在子页面（如「%s/歌词」）" % args.title)
    else:
        for i in range(max(0, hit - 3), min(len(lines), hit + 4)):
            mark = ">>" if i == hit else "  "
            print(f"  {mark} {lines[i][:100]}")

    # 附带验证现有解析函数对新写法的覆盖
    direct = parse_lyrics(source)
    print(f"\n== parse_lyrics 直查: {len(direct)} 字 ==")

    # 歌词章节原文 dump + poem 标签配对检查
    m = re.search(r"^={2,4}\s*[^=\n]*歌词[^=\n]*\s*={2,4}\s*$", source, re.M)
    if m:
        level = len(m.group(0)) - len(m.group(0).lstrip("="))
        tail = source[m.end():]
        nxt = re.search(rf"^={{1,{level}}}\s*[^=\n]", tail, re.M)
        if nxt:
            tail = tail[:nxt.start()]
        opens = len(re.findall(r"<poem\b", tail))
        closes = len(re.findall(r"</poem\s*>", tail))
        print(f"\n== 歌词章节原文（{len(tail)} 字符，<poem> 开 {opens} 个 / </poem> 关 {closes} 个） ==")
        if opens != closes:
            print("  !! poem 标签不配对 —— 这就是歌词解析为空的原因")
        print(tail[:2200])
        if len(tail) > 2200:
            print(f"  …（省略 {len(tail) - 2200} 字符）")
            print("  [章节末尾 400 字符]")
            print(tail[-400:])
    else:
        print("\n!! 找不到「歌词」章节标题")
    return 0


if __name__ == "__main__":
    sys.exit(main())
