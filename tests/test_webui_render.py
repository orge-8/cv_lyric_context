# -*- coding: utf-8 -*-
"""custom 页渲染冒烟（Node 驱动，真实插件 API 数据）。

声明式页面由宿主渲染，custom 页的渲染逻辑全在插件自己的 JS 里——
DOM id 拼错、渲染崩溃、并发踩闸这些问题只有真机才暴露。本测试用最小
DOM stub + 模拟宿主桥接，把页面 JS 完整跑一遍：

1. 用真实 ``SongStore`` 装配插件，抓取 6 个读 API 的真实返回；
2. Node 里注入 ``window.maibot`` 假桥接（记录调用、统计并发峰值）；
3. 断言 KPI / 仪表 / 徽章 / 表格 / 检索详情 / 触发链路全部正确渲染。

依赖 Node（``node`` 在 PATH 或常见安装位置）；找不到则 skip——
与宿主契约测试同一口径：SKIP 不等于通过，交付前需在带 Node 的环境复跑。
"""

import asyncio
import importlib
import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

import pytest

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(_PLUGIN_DIR), str(_PLUGIN_DIR.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_MOD = importlib.import_module(_PLUGIN_DIR.name)
if not hasattr(_MOD, "create_plugin"):
    _MOD = importlib.import_module(f"{_PLUGIN_DIR.name}.plugin")

from vcpedia_store import SongStore  # noqa: E402


def _find_node() -> str:
    found = shutil.which("node")
    if found:
        return found
    # Windows 常见安装位置（WorkBuddy 托管 runtime / Program Files）
    candidates = [
        pathlib.Path(os.path.expanduser("~")) / ".workbuddy" / "binaries" / "node",
        pathlib.Path("C:/Program Files/nodejs"),
    ]
    for base in candidates:
        if not base.is_dir():
            continue
        for exe in base.rglob("node.exe"):
            return str(exe)
        plain = base / "node.exe"
        if plain.is_file():
            return str(plain)
    return ""


class _Ctx:
    def __init__(self) -> None:
        self.logger = logging.getLogger("test-webui-render")


def _capture_api_payloads() -> dict:
    """真实插件 + 临时曲库，抓 6 个读 API 的返回形状。"""
    plug = _MOD.create_plugin()
    plug.set_plugin_config({})
    plug._ctx = _Ctx()
    with tempfile.TemporaryDirectory() as tmp:
        plug._store = SongStore(os.path.join(tmp, "songs.db"))
        plug._store.count()
        plug._store.upsert({"name": "测试曲", "lyrics": "第一行歌词\n第二行歌词", "singers": "洛天依"})
        plug._store.upsert({"name": "没词曲", "lyrics": ""})
        plug._sync_task = None
        plug._sync_stats = None
        plug._webui_task_kind = ""
        plug._refetch_stop = False
        from recent_songs import RecentSongsLog

        plug._recent_songs = RecentSongsLog(tmp)

        async def _run():
            return {
                "stats": await plug.api_stats(),
                "task": await plug.api_task(),
                "search": await plug.api_search(keyword="测试"),
                "song": await plug.api_song(name="测试曲"),
                "empty": await plug.api_empty(),
                "recent": await plug.api_recent(),
            }

        return asyncio.run(_run())


#: Node 侧的最小 DOM stub + 假桥接 + 断言。页面 JS 与 API 数据经 argv 传入。
_NODE_HARNESS = r"""
"use strict";
const fs = require("fs");
const pageJs = fs.readFileSync(process.argv[2], "utf-8");
const apiData = JSON.parse(fs.readFileSync(process.argv[3], "utf-8"));
const fullHtml = fs.readFileSync(process.argv[4], "utf-8");

class El {
  constructor(tag) {
    this.tagName = tag; this.children = []; this._cls = new Set();
    this.attrs = {}; this.style = {}; this.dataset = {};
    this._text = ""; this._html = ""; this.hidden = false;
    this.disabled = false; this.value = ""; this.listeners = {};
    this.classList = {
      add: (c) => this._cls.add(c), remove: (c) => this._cls.delete(c),
      toggle: (c, f) => { f ? this._cls.add(c) : this._cls.delete(c); },
      contains: (c) => this._cls.has(c),
    };
  }
  get className() { return [...this._cls].join(" "); }
  set className(v) { this._cls = new Set(v.split(/\s+/).filter(Boolean)); }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); this._html = ""; this.children = []; }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); this._text = ""; this.children = []; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  appendChild(c) { this.children.push(c); return c; }
  addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); }
  dispatch(ev) { (this.listeners[ev] || []).forEach((fn) => fn({ key: "Enter" })); }
  querySelectorAll() {
    const out = [];
    const walk = (n) => n.children.forEach((c) => {
      if (c._cls.has("cvl-result") && c._cls.has("active")) out.push(c);
      walk(c);
    });
    walk(this); return out;
  }
}
const registry = new Map();
for (const m of fullHtml.matchAll(/id="([^"]+)"/g)) registry.set(m[1], new El("div"));
const document = {
  getElementById: (id) => registry.get(id) || null,
  createElement: (t) => new El(t),
  documentElement: new El("html"), body: new El("body"), head: new El("head"),
  readyState: "complete", addEventListener: () => {},
};
const window = { addEventListener: () => {}, dispatchEvent: () => {} };
const calls = [];
let maxConcurrent = 0, current = 0;
window.maibot = {
  ready: Promise.resolve({ theme: "light" }), context: { theme: "light" },
  page: { id: "lyrics", layout: "document" },
  query: (name, args) => {
    calls.push(["query", name, args]);
    current++; maxConcurrent = Math.max(maxConcurrent, current);
    return new Promise((r) => setTimeout(() => { current--; r(apiData[name]); }, 5));
  },
  action: (name, args) => { calls.push(["action", name, args]); return Promise.resolve({ ok: true, reason: "" }); },
};
const intervals = [];
const realTimeout = setTimeout;
const stubInterval = (fn, ms) => { intervals.push(ms); return 0; };
const fn = new Function("window", "document", "setInterval", "setTimeout", "clearTimeout", "clearInterval", pageJs);
fn(window, document, stubInterval, realTimeout, clearTimeout, () => {});

const failures = [];
const assert = (cond, msg) => { if (!cond) failures.push(msg); };
const get = (id) => registry.get(id);
realTimeout(() => {
  assert(get("cvl-kpi-total").textContent === "2", "KPI 曲库总数应为 2");
  assert(get("cvl-kpi-coverage").textContent === "50.0%", "覆盖率应为 50.0%");
  assert(get("cvl-kpi-empty").textContent === "1", "缺歌词应为 1");
  assert(get("cvl-status-pill").textContent === "曲库就绪", "状态徽章应为曲库就绪");
  assert(get("cvl-gauge-arc").attrs["stroke-dasharray"].startsWith("117.8"), "仪表弧长应为半圆的 50%");
  assert(get("cvl-gauge-text").textContent === "50.0", "仪表数字应为 50.0");
  assert(get("cvl-task-pill").textContent === "空闲", "任务徽章应为空闲");
  assert(get("cvl-btn-cancel").disabled === true, "无任务时取消按钮应禁用");
  assert(get("cvl-empty-body").innerHTML.includes("没词曲"), "缺歌词清单应含没词曲");
  assert(get("cvl-empty-total").textContent === "共 1 首", "缺歌词总数应为共 1 首");
  assert(intervals.includes(4000) && intervals.includes(15000), "应注册 4s/15s 轮询");
  assert(maxConcurrent <= 2, "桥接并发峰值应 <=2，实际 " + maxConcurrent);
  const qn = calls.filter((c) => c[0] === "query").map((c) => c[1]);
  assert(["stats", "task", "recent", "empty"].every((n) => qn.includes(n)), "首批四个读查询都应发出");
  get("cvl-keyword").value = "测试";
  get("cvl-btn-search").dispatch("click");
  realTimeout(() => {
    const results = get("cvl-results").children;
    assert(results.length === 1, "检索应渲染 1 条结果");
    assert(results.length && results[0].innerHTML.includes("测试曲"), "结果应含歌名");
    if (results.length) results[0].dispatch("click");
    realTimeout(() => {
      assert(get("cvl-detail").innerHTML.includes("测试曲"), "详情应含歌名");
      assert(get("cvl-detail").innerHTML.includes("第一行歌词"), "详情应含歌词预览");
      assert(get("cvl-detail").innerHTML.includes("洛天依"), "详情应含歌手");
      get("cvl-limit").value = "5";
      get("cvl-btn-sync").dispatch("click");
      realTimeout(() => {
        const act = calls.find((c) => c[0] === "action" && c[1] === "start_sync");
        assert(act && act[2].limit === "5", "start_sync 应以字符串 limit=5 触发");
        if (failures.length) { console.error("FAIL:\n" + failures.join("\n")); process.exit(1); }
        console.log("render smoke: 20 checks passed");
        process.exit(0);
      }, 200);
    }, 200);
  }, 200);
}, 400);
"""


def test_custom_page_renders_with_real_api_data():
    node = _find_node()
    if not node:
        pytest.skip("未找到 Node，custom 页渲染冒烟未执行（交付前需复跑）")
    payloads = _capture_api_payloads()
    html_path = _PLUGIN_DIR / "webui" / "lyrics.html"
    html = html_path.read_text(encoding="utf-8")
    import re

    script = re.search(r"<script>(.*)</script>", html, re.S)
    assert script, "页面必须含内联 script"
    with tempfile.TemporaryDirectory() as tmp:
        js_path = pathlib.Path(tmp) / "page.js"
        data_path = pathlib.Path(tmp) / "api.json"
        harness_path = pathlib.Path(tmp) / "harness.js"
        js_path.write_text(script.group(1), encoding="utf-8")
        data_path.write_text(json.dumps(payloads, ensure_ascii=False), encoding="utf-8")
        harness_path.write_text(_NODE_HARNESS, encoding="utf-8")
        proc = subprocess.run(
            [node, str(harness_path), str(js_path), str(data_path), str(html_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
    assert proc.returncode == 0, (
        f"custom 页渲染冒烟失败:\n{proc.stdout}\n{proc.stderr}"
    )