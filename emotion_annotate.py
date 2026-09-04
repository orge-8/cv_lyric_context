"""情绪标注的纯函数件：prompt 构造与标签解析（离线脚本与插件内标注共用）。

单独成模块是为了让「离线脚本 annotate_emotions.py」和「同步后自动标注
（vcpedia_mixin 里的 _annotate_pending_emotions）」用同一份 prompt 与解析逻辑，
避免两套实现各自漂移。这里只有纯函数，不碰数据库、不依赖 MaiBot SDK。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from vcpedia_store import EMOTION_TAGS

# 歌词截断：情绪看整体，超长歌词只保留开头/结尾各一半额度
LYRICS_MAX_CHARS = 3000

# 注意：模板里的 JSON 示例花括号必须双写 {{}}，否则 str.format 会把它当
# 格式化字段抛 KeyError（runtime-gotchas 第 16 条）
PROMPT_TEMPLATE = """你是中文VOCALOID歌曲库的情绪标签助手。
允许使用的标签只有：{tags}。
歌曲名称：{name}
歌曲歌词：
{lyrics}
请根据整首歌歌词的总体表达选择一个或多个最合适的歌曲标签；不要因为单句歌词而改变整体判断。
只能返回JSON对象，不要输出解释。格式：{{"emotion_tags":["温柔"]}}"""


def build_prompt(name: str, lyrics: str) -> str:
    """构造标注 prompt，超长歌词取首尾各一半。"""
    text = str(lyrics or "")
    if len(text) > LYRICS_MAX_CHARS:
        half = LYRICS_MAX_CHARS // 2
        text = text[:half] + "\n……\n" + text[-half:]
    return PROMPT_TEMPLATE.format(
        tags="、".join(EMOTION_TAGS),
        name=str(name or ""),
        lyrics=text,
    )


def parse_tags(reply: str) -> List[str]:
    """从回复中提取标签并与白名单取交集；交集为空返回空列表（视为失败）。"""
    text = (reply or "").strip()
    # 兼容 LLM 用 ```json 包裹的情况
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    raw_tags = obj.get("emotion_tags") if isinstance(obj, dict) else None
    if not isinstance(raw_tags, list):
        return []
    allowed = set(EMOTION_TAGS)
    return [t for t in (str(x or "").strip() for x in raw_tags) if t in allowed]


def extract_llm_text(result: Any) -> str:
    """兼容 SDK 2.x 的 response 字段与旧版 content/text 字段。

    照 group-emoji-react 的写法：success 为假时统一当空串处理。
    """
    if isinstance(result, dict):
        if not result.get("success", True):
            return ""
        return str(result.get("response") or result.get("content") or result.get("text") or "")
    return str(result or "")


def annotate_result(tags: List[str]) -> Dict[str, Any]:
    """把标签列表整理成便于记日志的结构（成功与否 + 标签文本）。"""
    clean = [t for t in tags if t in set(EMOTION_TAGS)]
    return {"ok": bool(clean), "tags": clean, "text": "|".join(clean)}
