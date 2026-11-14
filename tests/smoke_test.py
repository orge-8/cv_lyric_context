# -*- coding: utf-8 -*-
"""冒烟测试：不启动 MaiBot，用 FakeHost 跑完生命周期与关键入口。

运行: python tests/smoke_test.py

cv_lyric_context 此前没有 smoke_test.py（门禁会提示「静态自检通过不等于
插件能加载」）。本文件补齐这一层，验证的是**编排链路**而非业务细节
（业务细节由 tests/ 下 225 项 pytest 守着）：

1. on_load → 组件注册 → on_unload 全生命周期（真实资产索引构建，零网络）；
2. 两个跨插件只读 API 的返回形状过契约全量校验（cv_music v1）；
3. /歌词 帮助与状态命令真的能回话；
4. 规划器注入 hook：有命中 → 注入；点歌工具不在线 → 文案降级不指定工具名；
   在线 → 保留工具名（P1-D 探测链路在真实 hook 里走通）。

全程零网络：FakeHost 拦截一切能力调用，曲库落在临时目录。
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import time
from collections import deque

_TESTS_DIR = pathlib.Path(__file__).resolve().parent
_PLUGIN_DIR = _TESTS_DIR.parent
for _p in (str(_TESTS_DIR), str(_PLUGIN_DIR), str(_PLUGIN_DIR.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PLUGIN_ID = "org.mai-mai.cv-lyric-context"
_PLAY_TOOL = "search_and_play_music"
_PLAY_PROVIDER = "github.orge-8.music-request"


class _FakeApiList:
    """ctx.api.list 的离线替身：只记录调用、返回可控组件清单。"""

    def __init__(self, entries) -> None:
        self.entries = entries
        self.calls: list[dict] = []

    async def list(self, *, plugin_id: str = ""):
        self.calls.append({"plugin_id": plugin_id})
        return self.entries


def main() -> int:
    try:
        import maibot_sdk  # noqa: F401
    except Exception:
        print("SKIP: 未安装 maibot-plugin-sdk，跳过冒烟测试（这不代表通过）")
        return 0

    from fakehost import (
        FakeHost,
        bind_context,
        build_context,
        get_default_config,
        load_plugin_module,
    )
    from contract_cv_music import validate_result

    module = load_plugin_module(_PLUGIN_DIR)
    plugin = module.create_plugin()
    host = FakeHost(plugin_id=PLUGIN_ID)
    ctx = build_context(PLUGIN_ID, rpc_call=host.rpc_call)
    config = get_default_config(getattr(type(plugin), "config_model", None))
    # 冒烟不测收件箱导入（会扫数据目录），关掉让输出干净
    config["plugin"]["auto_import_inbox"] = False
    bind_context(plugin, ctx, config)

    async def run() -> None:
        # ── 生命周期：真实资产索引构建（约 6 万条，零网络） ──
        await plugin.on_load()
        assert plugin.config.plugin.enabled is True, "默认配置未生效"
        assert plugin._songs_by_line, "on_load 后歌词索引为空"

        # 组件注册链路：唯一能在本地覆盖 Runner 注册的手段
        comps = plugin.get_components()
        names = {item["name"] for item in comps}
        for expected in (
            "get_song_bilibili", "get_recent_songs",
            "lyrics_help", "lyrics_status",
            "cv_lyric_planner_injector", "cv_lyric_context_injector",
        ):
            assert expected in names, f"组件缺失: {expected}（实际 {sorted(names)}）"
        # 契约 API 的超时声明必须在组件 metadata 里（宿主据此收紧 RPC 预算）
        for api_name in ("get_song_bilibili", "get_recent_songs"):
            entry = next(c for c in comps if c["name"] == api_name)
            assert entry["type"] == "API", f"{api_name} 组件类型异常: {entry}"
            assert entry["metadata"].get("timeout_ms") == 3000, (
                f"{api_name} 未声明 timeout_ms=3000: {entry['metadata']}"
            )

        # ── 跨插件 API：返回形状过契约全量校验 ──
        miss = await plugin.api_get_song_bilibili(name="冒烟探针不存在的歌")
        validate_result("get_song_bilibili", miss)
        assert miss["found"] is False and miss["schema_version"] == 1

        recent = await plugin.api_get_recent_songs(limit=5)
        validate_result("get_recent_songs", recent)
        assert recent["schema_version"] == 1 and isinstance(recent["songs"], list)

        # ── 命令：帮助与状态真的能回话 ──
        ok, _resp, level = await plugin.cmd_help(stream_id="fake-stream")
        assert ok is True and level == 2, f"帮助命令返回异常: {(ok, _resp, level)}"
        assert "歌词库用法" in (host.sent_texts[-1] or ""), "帮助命令输出异常"

        ok, _resp, _level = await plugin.cmd_status(stream_id="fake-stream")
        assert ok is True, f"状态命令失败: {(ok, _resp)}"

        # ── 注入 hook：装一条强证据命中，验证探测降级全链路 ──
        plugin._hits = {"smoke-s1": deque(
            [(time.time(), "测试歌词句", "测试曲", True)], maxlen=20
        )}
        plugin._meta_by_name["测试曲"] = ("洛天依", "")
        plugin._song_record = lambda name: {}  # type: ignore[assignment]

        # 点歌工具不在线 → 注入降级：有「可点播查询」串、不指定工具名
        ctx.api = _FakeApiList([])
        result = await plugin.inject_planner_context(
            session_id="smoke-s1", prompt="原始规划器 prompt"
        )
        modified = result.get("modified_kwargs") or {}
        injected = modified.get("prompt", "")
        assert module.INJECT_MARKER in injected, f"未注入歌词块: {result}"
        assert "可点播查询" in injected, f"注入缺少可点播查询串: {injected}"
        assert _PLAY_TOOL not in injected, f"点歌工具不在线仍指定工具名: {injected}"

        # 点歌工具在线 → 保留工具名（TTL 缓存过期后重新探测）
        plugin._play_probe = None
        ctx.api = _FakeApiList([
            {"name": _PLAY_TOOL, "plugin_id": _PLAY_PROVIDER,
             "version": "1", "enabled": True}
        ])
        result = await plugin.inject_planner_context(
            session_id="smoke-s1", prompt="原始规划器 prompt"
        )
        injected = (result.get("modified_kwargs") or {}).get("prompt", "")
        assert _PLAY_TOOL in injected, f"点歌工具在线却未指定工具名: {injected}"

        # 重复注入同一 prompt（已含标记）→ 幂等跳过
        result = await plugin.inject_planner_context(
            session_id="smoke-s1", prompt=injected
        )
        assert not result.get("modified_kwargs"), "重复注入未幂等跳过"

        await plugin.on_unload()
        assert not plugin._songs_by_line, "卸载后索引未释放"

    asyncio.run(run())
    print("smoke: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
