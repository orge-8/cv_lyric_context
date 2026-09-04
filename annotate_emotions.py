"""离线批量标注歌曲情绪标签（v2.5.0 氛围选歌的数据准备）。

对库里「emotion 为空且歌词非空」的歌，取完整歌词调 LLM 打情绪标签，
写回 songs.emotion（管道分隔，如 甜美|温柔）。断点续跑：标注失败不写列，
下轮仍会进队列。纯标准库实现，与 MaiBot 运行时完全解耦——在开发机上跑，
不占 bot 资源。

用法（在插件目录下执行）：
    # 1. 配置 key（二选一）
    set MAIBOT_ANNOTATE_API_KEY=sk-xxx        # 或 OPENAI_API_KEY
    #    端点/模型默认 OpenAI 官方；自建/中转写 annotate_config.json：
    #    {"base_url": "https://api.xxx.com/v1", "model": "deepseek-chat"}
    #    （该文件已 gitignore，不入库）

    # 2. 先试 3 首看看质量
    python annotate_emotions.py --limit 3 --dry-run

    # 3. 全量跑（4000 首约 1.5~2 小时，随时 Ctrl+C，下轮续跑）
    python annotate_emotions.py

    # 重标某首歌：直接清空后重跑
    #   sqlite3 data/vcpedia_songs.db "UPDATE songs SET emotion='' WHERE name='xx'"

标签集固定 7 个：甜美、温柔、积极、帅气、搞怪、伤感、愤怒。
LLM 返回与白名单取交集，交集为空视为失败（下轮重试）。
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from emotion_annotate import build_prompt, parse_tags  # noqa: E402
from vcpedia_store import SongStore  # noqa: E402

MAX_RETRIES = 2          # JSON 解析失败/空交集的重试次数
REQUEST_INTERVAL = 0.2   # 顺序模式每首间隔（秒）

_CONFIG_FILE = Path(__file__).resolve().parent / "annotate_config.json"


def _load_config() -> Dict[str, str]:
    """读本地 annotate_config.json（可选，放 db/base_url/model/ca_bundle，不放 key）。"""
    if _CONFIG_FILE.is_file():
        try:
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if v}
        except (json.JSONDecodeError, OSError) as e:
            print(f"[warn] {_CONFIG_FILE.name} 读取失败，忽略：{e}")
    return {}


def _build_ssl_context(ca_bundle: str) -> ssl.SSLContext:
    """部分网络出口有 TLS 中间人代理，需要指定代理根证书（同 vcpedia_client）。"""
    if ca_bundle and Path(ca_bundle).is_file():
        return ssl.create_default_context(cafile=ca_bundle)
    return ssl.create_default_context()


def call_llm(
    prompt: str,
    base_url: str,
    api_key: str,
    model: str,
    temperature: float,
    ssl_ctx: ssl.SSLContext,
    timeout: float = 60.0,
) -> str:
    """调 OpenAI 兼容 /chat/completions，返回回复文本。网络异常直接抛出。"""
    payload = json.dumps({
        "model": model,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return str(data["choices"][0]["message"]["content"] or "")


def annotate_one(
    song: Dict[str, Any],
    base_url: str,
    api_key: str,
    model: str,
    temperature: float,
    ssl_ctx: ssl.SSLContext,
) -> List[str]:
    """标注一首：调用 → 解析 → 白名单交集。失败抛异常/返回空列表。"""
    prompt = build_prompt(song.get("name"), song.get("lyrics"))
    last_err: Optional[Exception] = None
    for _ in range(MAX_RETRIES + 1):
        try:
            tags = parse_tags(call_llm(
                prompt, base_url, api_key, model, temperature, ssl_ctx,
            ))
            if tags:
                return tags
            last_err = ValueError("回复无可识别标签")
        except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError,
                IndexError, OSError, ValueError) as e:
            last_err = e
        time.sleep(1.0)
    raise RuntimeError(f"标注失败: {last_err}")


def _worker(job: Dict[str, Any]) -> List[str]:
    """线程池入口：只做网络请求，不碰 SQLite。"""
    return annotate_one(**job)


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="批量标注歌曲情绪标签")
    parser.add_argument("--db", default="", help="数据库路径（缺省取 annotate_config.json 的 db）")
    parser.add_argument("--limit", type=int, default=0, help="本轮最多标注多少首（0=不限）")
    parser.add_argument("--model", default="", help="模型名（默认取配置或 deepseek-chat）")
    parser.add_argument("--base-url", default="", help="OpenAI 兼容端点（默认取配置或官方）")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--concurrency", type=int, default=1, help="并发数（1=顺序+限速）")
    parser.add_argument("--ca-bundle", default="", help="TLS 中间人环境下的代理根证书路径")
    parser.add_argument("--dry-run", action="store_true", help="只标注打印，不写库")
    args = parser.parse_args(argv[1:])

    cfg = _load_config()
    base_url = (args.base_url or cfg.get("base_url") or "https://api.openai.com/v1").strip()
    model = (args.model or cfg.get("model") or "deepseek-chat").strip()
    api_key = (os.environ.get("MAIBOT_ANNOTATE_API_KEY")
               or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        print("未找到 API key。请设置环境变量 MAIBOT_ANNOTATE_API_KEY（或 OPENAI_API_KEY）。")
        return 1

    # strip：粘贴路径常带结尾空格/换行，打印时看不出来却会让 is_file() 判否；
    # 报错用 repr，让零宽字符等隐身字符现形
    db_arg = (args.db or cfg.get("db") or "data/vcpedia_songs.db").strip()
    db_path = Path(db_arg)
    if not db_path.is_file():
        print(f"数据库不存在：{db_path.resolve()!r}")
        return 1
    store = SongStore(db_path)
    pending = store.pending_emotions(limit=args.limit if args.limit > 0 else 10 ** 9)
    total = len(pending)
    if total == 0:
        print("没有待标注的歌曲（emotion 为空且歌词非空的条目为 0）。")
        return 0
    print(f"待标注 {total} 首 | 端点 {base_url} | 模型 {model} | 并发 {args.concurrency}"
          + (" | dry-run 不写库" if args.dry_run else ""))
    ssl_ctx = _build_ssl_context(args.ca_bundle or str(cfg.get("ca_bundle") or ""))

    jobs = [
        {
            "song": song,
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "temperature": args.temperature,
            "ssl_ctx": ssl_ctx,
        }
        for song in pending
    ]

    done = failed = 0
    tag_count: Dict[str, int] = {}
    started = time.time()

    def _finish(song: Dict[str, Any], tags: List[str]) -> None:
        nonlocal done, failed
        if not tags:
            failed += 1
            print(f"  ✗ {song.get('name')}: 无有效标签，跳过（下轮重试）", flush=True)
            return
        if not args.dry_run:
            store.mark_emotion(str(song.get("safe_name") or song.get("name")), tags)
        done += 1
        for t in tags:
            tag_count[t] = tag_count.get(t, 0) + 1
        print(f"  ✓ {song.get('name')}: {'|'.join(tags)}", flush=True)
        if done % 50 == 0:
            rate = done / max(1e-9, time.time() - started)
            print(f"  —— 进度 {done}/{total}（失败 {failed}），约 {rate:.1f} 首/秒 ——", flush=True)

    if args.concurrency <= 1:
        for i, (job, song) in enumerate(zip(jobs, pending)):
            try:
                tags = annotate_one(**job)
            except Exception as e:  # 单首失败不中断整轮
                tags = []
                print(f"  ✗ {song.get('name')}: {e}", flush=True)
                failed += 1
                continue
            _finish(song, tags)
            if i < len(jobs) - 1:
                time.sleep(REQUEST_INTERVAL)
    else:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            for song, tags in zip(pending, pool.map(_worker, jobs)):
                _finish(song, tags)

    print(f"\n完成：标注 {done}，失败 {failed}，耗时 {time.time() - started:.0f}s")
    if tag_count:
        print("本轮标签分布：",
              "、".join(f"{k}×{v}" for k, v in sorted(tag_count.items(), key=lambda x: -x[1])))
    if not args.dry_run:
        print("全库统计：", json.dumps(store.emotion_stats(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    sys.exit(main(sys.argv))
