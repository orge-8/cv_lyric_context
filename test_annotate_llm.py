"""情绪标注的 LLM 传参回归测试（MaiBot 1.2.5 任务名/模型名语义拆分）。

背景：MaiBot 1.2.5 起 llm.generate 的「模型任务名」与「具体模型名」是两个参数
（task_name / model）；1.2.4 及以前只有一个 model，且它按任务名解释。
旧写法把任务名塞进 model=，升级后会变成「找不到名为 utils 的模型」，
情绪标注连续失败 3 次即熔断中止。详见 runtime-gotchas §47。

用法:
    python test_annotate_llm.py
退出码: 0=全部通过, 1=有失败
"""
import asyncio
import sys
import types
from pathlib import Path

_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

import plugin  # noqa: E402
from vcpedia_mixin import VCPediaMixin  # noqa: E402

_TEMPERATURE = 0.2


class _FakeStore:
    def pending_emotions(self, limit, new_first):
        return [{"safe_name": "x", "name": "测试曲", "lyrics": "词"}]

    def mark_emotion(self, safe, tags):
        pass


class _FakeLLM:
    def __init__(self):
        self.calls = []

    async def generate(self, prompt, **kw):
        self.calls.append(kw)
        return {"response": "开心"}


class _FakeLogger:
    def info(self, *a):
        pass

    def warning(self, *a):
        pass

    def error(self, *a, **k):
        pass


class _Obj:
    pass


def _run(task: str, model: str) -> dict:
    """用给定配置真跑一次标注链路，返回 llm.generate 实际收到的 kwargs。"""
    obj = _Obj()
    obj.store = _FakeStore()
    obj.ctx = _Obj()
    obj.ctx.llm = _FakeLLM()
    obj.ctx.logger = _FakeLogger()
    obj._plugin_id_for_log = lambda: "org.mai-mai.cv-lyric-context"
    cfg = types.SimpleNamespace(
        annotate_on_sync=True,
        annotate_on_sync_limit=5,
        llm_task=task,
        llm_model=model,
        annotate_timeout_ms=5000,
        annotate_budget_seconds=10,
    )
    obj._annotate_config = lambda: cfg

    bound = VCPediaMixin._annotate_pending_emotions.__get__(obj)
    asyncio.run(bound(5))
    return obj.ctx.llm.calls[-1]


def main() -> int:
    results: list[bool] = []

    def check(label: str, got, want) -> None:
        ok = got == want
        results.append(ok)
        mark = "[OK]  " if ok else "[FAIL]"
        detail = f"{got}" if ok else f"{got}（期望 {want}）"
        print(f"{mark} {label}: {detail}")

    fields = plugin.EmotionSection.model_fields
    check("EmotionSection 含 llm_task 字段", "llm_task" in fields, True)
    check("EmotionSection 含 llm_model 字段", "llm_model" in fields, True)

    check(
        "默认配置（utils / 空） → 只传 task_name",
        _run("utils", ""),
        {"temperature": _TEMPERATURE, "task_name": "utils"},
    )
    check(
        "只配具体模型名 → 只传 model",
        _run("", "gpt-4o-mini"),
        {"temperature": _TEMPERATURE, "model": "gpt-4o-mini"},
    )
    check(
        "两者都配 → 分键传递、互不串位",
        _run("planner", "gpt-4o-mini"),
        {"temperature": _TEMPERATURE, "task_name": "planner", "model": "gpt-4o-mini"},
    )
    check(
        "都留空 → 不传任何键（走 SDK 默认任务）",
        _run("", ""),
        {"temperature": _TEMPERATURE},
    )
    check(
        "回归防护：任务名绝不漏进 model 键",
        _run("utils", "").get("model"),
        None,
    )

    passed = sum(1 for r in results if r)
    print("=" * 56)
    print(f"测试结果: {passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
