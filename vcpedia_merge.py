# -*- coding: utf-8 -*-
"""多来源歌曲对齐与字段级补全 —— 纯函数（v2.18.0）。

设计参考：mohobot ``mohobot/music_knowledge/normalize.py`` 与 ``merge.py``
（MIT License, Copyright (c) 2026 CarefreeSongs712）。借鉴的是两条数据原则，
**未复制**上游的 SQLAlchemy 模型、来源适配器与同步编排代码：

1. 归一化名只用于**生成候选**，绝不直接当唯一键（上游实测：本地库去括号后
   有 81 个同名冲突组，其中 50 组演唱集合不同，是不同歌曲）；
2. 非空值永不被空值覆盖；同字段多来源取值冲突时保留现值并记录差异，
   不静默丢弃差异。

MIT License / Copyright (c) 2026 CarefreeSongs712
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

本模块与 ``vcpedia_schema.safe_song_name`` 的分工：后者是库内既有的、
用作实体定位的兼容键；本模块的归一化结果**只读、只用于生成候选**，
不参与任何 UPDATE / INSERT 的主键决策。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Sequence, Set

# 归一化保留：字母数字 + 中日韩 + 假名 + 韩文
_KEEP_RE = re.compile(r"[^0-9a-z\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_FEAT_RE = re.compile(r"\b(?:feat|ft|featuring)\.?\s*[^)\]}]*", re.IGNORECASE)
_TRAILING_BRACKET_RE = re.compile(r"\s*[\(（\[【]\s*([^\)）\]】]{1,40})\s*[\)）\]】]\s*$")
_VERSION_RE = re.compile(r"\b(?:ver|version)\.?\s*[\w.]*", re.IGNORECASE)

#: 命中即视为「同一首歌的不同版本」，不是演唱者/社团消歧后缀。
_VERSION_WORDS = (
    "中文版", "日文版", "英文版", "粤语版", "日语版", "国语版",
    "翻唱", "伴奏", "纯音乐", "instrumental", "off vocal", "offvocal",
    "remix", "重制", "重置", "混音版", "现场版", "live", "demo",
    "完整版", "短版", "tv size", "tvsize", "acoustic", "piano",
)


def nfkc(text: Any) -> str:
    """全角/半角与兼容字符统一。"""
    return unicodedata.normalize("NFKC", str(text or ""))


def normalize_name(name: Any) -> str:
    """候选键：NFKC → 小写 → 去 feat./ft. → 仅保留字母数字与 CJK。

    只用于生成候选/建索引，**不可**作为实体的唯一约束。
    """
    s = nfkc(name).strip()
    if not s:
        return ""
    s = _FEAT_RE.sub("", s).lower()
    return _KEEP_RE.sub("", s)


def bracket_core(name: Any) -> str:
    """剥掉尾部括号补充后的核名（只剥一层）：``X(洛天依)`` → ``X``。"""
    s = nfkc(name).strip()
    match = _TRAILING_BRACKET_RE.search(s)
    return s[:match.start()].strip() if match else s


def bracket_suffix(name: Any) -> str:
    """取尾部括号内的后缀，无则空串。"""
    match = _TRAILING_BRACKET_RE.search(nfkc(name).strip())
    return match.group(1).strip() if match else ""


def classify_suffix(name: Any) -> str:
    """尾部括号后缀分类：``version`` / ``disambiguation`` / ``""``。"""
    suffix = bracket_suffix(name).lower()
    if not suffix:
        return ""
    if any(word in suffix for word in _VERSION_WORDS) or _VERSION_RE.search(suffix):
        return "version"
    # 年份、演唱者、社团、P主等一律视为消歧信息，不是版本
    return "disambiguation"


def is_version_name(name: Any) -> bool:
    """歌名是否带版本标记（不同版本不可互相合并）。"""
    s = nfkc(name).lower()
    return (
        classify_suffix(name) == "version"
        or any(word in s for word in _VERSION_WORDS)
        or bool(_VERSION_RE.search(s))
    )


def alias_keys(name: Any) -> Set[str]:
    """一首歌可被哪些归一化写法指代（原名 + 去括号核名，去空去重）。"""
    keys = {normalize_name(name), normalize_name(bracket_core(name))}
    return {key for key in keys if key}


def candidate_names(name: Any) -> List[str]:
    """按由严到宽生成**查询候选**，绝不作为写入键。

    版本名只保留原名候选：剥掉 ``(中文版)`` 之类的核名会指向另一首歌，
    让它进入候选集等于把两个版本合并。
    """
    raw = str(name or "").strip()
    if not raw:
        return []
    out: List[str] = []
    for item in (raw, bracket_core(raw), normalize_name(raw)):
        if item and item not in out:
            out.append(item)
    if is_version_name(raw):
        return out[:1]
    return out


def is_empty(value: Any) -> bool:
    """空值判定：None / 空白串 / 空容器。0 不视为空（年份另有 _coerce_year）。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return not value
    return False


@dataclass
class FieldMerge:
    """一次字段级合并的结果。"""

    values: Dict[str, Any] = field(default_factory=dict)
    filled: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)


def merge_fields(
    existing: Dict[str, Any],
    incoming: Dict[str, Any],
    fields: Sequence[str],
) -> FieldMerge:
    """只补空、不覆盖非空；冲突保留现值并记录字段名。

    ``existing`` 里非空而 ``incoming`` 也非空且不同 => ``conflicts``，
    绝不静默以后来者覆盖前者（多来源同步时最容易丢数据的地方）。
    """
    outcome = FieldMerge()
    for key in fields:
        old = existing.get(key)
        new = incoming.get(key)
        if is_empty(old):
            if not is_empty(new):
                outcome.values[key] = new
                outcome.filled.append(key)
            continue
        outcome.values[key] = old
        if not is_empty(new) and str(old).strip() != str(new).strip():
            outcome.conflicts.append(key)
    return outcome


__all__ = [
    "FieldMerge", "alias_keys", "bracket_core", "bracket_suffix",
    "candidate_names", "classify_suffix", "is_empty", "is_version_name",
    "merge_fields", "nfkc", "normalize_name",
]
