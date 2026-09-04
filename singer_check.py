"""歌手归属校验：只用于氛围选歌的推荐池过滤，不影响歌词识别注入。

规则（参考 Agent-LuoTianyi 学歌歌手安全校验，按"推荐"场景适配）：

- 检查**完整歌手列表**，而不是只看第一位——VCPedia 的 singers 字段里
  "洛天依\\n言和" 这类合唱条目不少，只看首位会把合唱当独唱放进来。
- 歌手为空 = 放行（标注"歌手未知"）。库里空 singers 占比不小，
  拦截会废掉大半推荐池；返回的 note 会让主 LLM 知道歌手信息缺失。
- 拒绝：含任何「已知虚拟歌手中非目标者」（如目标为洛天依时出现言和）；
  多歌手且并非全部都是目标歌手（合唱）。

纯函数模块，无外部依赖，脚本与插件共用，可独立单测。
"""

from __future__ import annotations

import re
from typing import Iterable, List, Tuple

# 拆分用的分隔符：库里实际是换行（见 lyrics_import.flatten_names），
# 但 VCPedia 页面里也见过顿号/逗号/斜杠写法，一并兼容。
_SEPARATORS = re.compile(r"[\r\n、,，/;；]")


def split_singers(raw: str) -> List[str]:
    """把 singers 字段拆成歌手列表（去空、去重、保序）。"""
    parts = _SEPARATORS.split(str(raw or ""))
    seen: List[str] = []
    for p in parts:
        p = p.strip()
        if p and p not in seen:
            seen.append(p)
    return seen


def check_singer(
    singers_raw: str,
    targets: Iterable[str],
    known_virtual: Iterable[str],
    allow_unknown: bool = True,
) -> Tuple[bool, str]:
    """判断一首歌是否进入推荐池。

    返回 (ok, note)。note 供推荐结果展示：放行时为歌曲歌手串或
    "歌手未知"，拒绝时为简短原因（只进日志/调试，不进用户文案）。
    """
    targets = {t.strip() for t in (str(x or "") for x in targets) if t.strip()}
    known = {k.strip() for k in (str(x or "") for x in known_virtual) if k.strip()}
    singers = split_singers(singers_raw)

    if not singers:
        if allow_unknown:
            return True, "歌手未知"
        return False, "歌手未知且配置不允许未知歌手"

    others = known - targets
    for s in singers:
        if s in others:
            return False, f"含非目标虚拟歌手「{s}」"

    non_target = [s for s in singers if s not in targets]
    if len(singers) > 1 and non_target:
        return False, "合唱（" + "、".join(singers[:5]) + "）"

    return True, "、".join(singers)
