# 更新日志

`cv_lyric_context`（中V歌词识别 · 歌曲库）的版本变更记录。

- 版本号以 `_manifest.json` 的 `version` 字段为准，插件中心与 WebUI 读同一个值。
- 遵循语义化版本：`MAJOR.MINOR.PATCH`——新能力升 MINOR，行为修复升 PATCH，
  影响已部署实例行为的改动一律升 MINOR 并在条目标题里点明。
- 新条目加在文件顶部。日期为提交日期（Asia/Shanghai）。

---

## [2.20.0] - 2026-11-15

### 运行时内存优化（真机 4GB / 可用 213MB 硬压力，识别/注入行为零变化）

动机：真机物理内存 95% 已用、重度换页，插件 v2.19.0 常驻 RSS +374MB
（索引 ~326MB，其中 98% 子串桶只含 1 首歌——大头是 Python dict/list/str
对象开销而非数据）。本版把常驻索引压到 ~55MB（紧凑两表），存活数据合计
~61MB，构建收尾把工作集还给 OS 后稳态 RSS 仅 ~4MB。决策与实测全过程见
`docs/decisions-memory-optimization.md`。

- **紧凑索引 `compact_index.py`（纯标准库，零新增依赖）**：整句索引
  （241,235 键）与 6 字滑窗片段索引（1,508,873 键）从 `dict[str, list[str]]`
  压实为「UTF-8 键 blob + `array('I')` 偏移/歌名 ID 表 + 歌名表」，二分查找；
  两表 ~301MB → **55.3MB**（11.6 + 43.7）。增量写入走 overlay 小 dict +
  桶视图（`setdefault` 返回可 insert/append 的视图），与旧 dict 语义逐操作
  等价；同步/导入的增量合并路径不变。单歌桶快路径（98% 的桶）省去去重集合。
- **常驻歌词按需化**：`lyrics_by_name` 7,808 首（~23MB）裁剪到 222 首
  （0.4MB）——只保留「运行时库查不到」与「用户歌单覆盖」的；其余走既有
  `_lyrics_of → _song_record`（200 条 LRU）→ SQLite 链路。裁剪判定与运行时
  回查**同库同口径**（只认 `data_dir/vcpedia_songs.db`，与 extra_song_dbs
  配置无关）。
- **修复大小写回查缺口（按需化暴露的旧缺陷）**：索引歌名键是 `_clean(name)`
  （NFKC+小写），而 `SongStore.get` 区分大小写——`TiMES→times` 永远落空，
  旧版靠全量常驻歌词掩盖。新增 `SongStore.get_by_clean_key`（插件注入
  `_clean_fn` 同口径流式精筛，取 id 最小命中），`_song_record` 落空兜底。
- **重建峰值治理**：滑窗长度变更重建改 `release_first=True`——先释放旧索引
  再构建（重建窗口期秒级识别暂停，完成后自动恢复），避免 4GB 真机上
  新旧索引共存的瞬间峰值。启动路径不受影响。
- **构建收尾工作集裁剪 `_trim_working_set`**：索引压实后旧 dict 虽已释放，
  CPython/Windows 并不立即把页归还系统（稳态 RSS 仍挂 ~374MB）。收尾在
  worker 线程 `gc.collect()` + `SetProcessWorkingSetSize(-1,-1)` 把已释放页
  裁出工作集；非 Windows / API 失败静默降级。只裁已释放页，不动存活对象。

### 实测（2026-11-15，SDK 2.23.0 隔离环境 + 官方 MaiBot v1.4.5 宿主源码）

| 指标 | v2.19.0 基线 | v2.20.0 |
|---|---:|---:|
| 索引存活数据（deep size） | ~326 MB | **60.9 MB**（两表 55.3 + 歌词 0.4 + 元数据 5.2） |
| 构建后稳态 RSS（进程增量） | +374 MB | **~4 MB**（收尾裁剪后） |
| 热工作集（2 万次真实查询后） | — | ~118 MB，再裁剪回 ~2 MB |
| 构建峰值 RSS | 429 MB | ~500 MB（dict 构建阶段固有，收尾即回落） |
| 整句 get / 子串 get | dict O(1) | ~20 µs / ~21 µs（二分，毫秒级预算内） |
| `_subspan_find` 端到端 | — | ~0.27 ms/条 |

- **行为零变化验收（差分回归，同一真实素材 + 36MB 同步库）**：
  241,235 整句键、1,508,873 片段键全量键值比对 **0 差异**；3,908 条语料
  `_subspan_find` **0 差异**；1,200 条消息 `record_hit` + 1,194 条命中
  证据强度（v2.17.0 分级语义）**0 差异**；7,808 首歌词回查逐行比对 **0 差异**。
- 门禁（devkit run_gates，SDK 2.23.0）：**PASS 4 / SKIP 0 / FAIL 0**
  （check 37 PASS + 5 WARN 存量、contracts 逐字节一致、smoke OK、
  pytest **248 passed / 7 skipped**）；新增 `tests/test_compact_index.py` 9 项。
- 官方最新实测：MaiBot **v1.4.5** PluginLoader 加载探针 **OK**
  （loaded=1 / components=28 / caps=4）；SDK **2.23.0** 隔离全量 pytest 通过。
  manifest 声明区间 host ≤1.4.5 / sdk ≤2.23.0 与实测一致。
- 部署：**只替换插件目录即可生效**（纯标准库，无 pip 步骤）；无配置键变更、
  无 capabilities 变更，但索引结构变化建议完整重启 MaiBot。

### 已知取舍

- 构建耗时增加（压实阶段 ~10s，本机串行实测总构建 ~18s vs 基线 ~8s；
  构建在 worker 线程，不阻塞事件循环）——用启动/重建时间换常驻内存，
  真机低频重建场景可接受。
- 构建瞬间峰值 ~500MB 略高于基线 429MB（dict 构建阶段固有，压实收尾后
  立即回落）；滑窗变更路径已用 release_first 消除新旧共存峰值。
- 查询从 O(1) 哈希变为 O(log N) 二分（~20µs/次），在用户允许的毫秒级
  延迟预算内；LRU 未命中的冷歌词回查多一次 SQLite 查询（毫秒级）。

## [2.19.0] - 2026-11-14

### 新增（点歌联动加固，与 music-request v1.8.0 配套）

- **跨插件契约单一事实源（P0-A，治 G1/G4）**：`get_song_bilibili` /
  `get_recent_songs` 的键集、类型、`schema_version`、match 枚举固化到 devkit
  `tools/maibot-devkit/contracts/cv_music_v1.json`；本仓库
  `tests/contracts/cv_music_v1.json` 为镜像，`tests/contract_cv_music.py`
  提供加载器与 `sample_result` / `validate_result`。门禁新增 contracts 步骤
  校验镜像与事实源**逐字节一致**（漂移即 FAIL，已反向验证）。
- **真实对端端到端测试**：`tests/test_e2e_cv_music.py`（8 项）经 fakehost
  包式加载**真实 music-request 插件模块**，`ctx.api.call` 直连本插件真实
  `api_get_song_bilibili`，覆盖命中直连 / prefix 歌手采纳 / 点名翻唱回退 /
  未收录回退 / 畸形返回回退。对端目录不存在时 skip（单仓库可跑）。
- **注入前可用性探测（P1-D，治 G5）**：`play_tool_enabled` 打开时，注入前经
  `ctx.api.list(plugin_id=github.orge-8.music-request)` 探测
  `search_and_play_music` 是否在线；TTL 5 分钟缓存、3s 超时。确认不在线 →
  注入文案降级为「可点播查询」串、不指定工具名；老 Host 无 `ctx.api` 代理
  或探测异常 → 保持原行为，绝不阻断注入。新增 `tests/test_play_tool_probe.py`
  （17 项：在线/离线/老 Host/超时/TTL 正负缓存/文案降级/hook 集成）。

### 变更

- **两个只读 `@API` 声明 `timeout_ms=3000`（P0-B，治 G2）**：此前未声明，
  宿主按 `component_timeout.py` 默认 **60s** 等待；该 API 是纯本地曲库查询，
  正常毫秒级。消费方（music-request）另有 3s `wait_for` 双保险。
- `_manifest.json` capabilities 增加 `api.list`（探测用）。**改动 capabilities
  需完整重启 MaiBot**。
- 既有跨插件测试的手写假字典全部改为契约构造 + 全量形状校验
  （`test_vcpedia_bilibili.py` 清理 3 处整段重复定义；`test_cross_plugin_api.py`
  同步契约化）。

### 修复

- **文档漂移（P2-E，治 G6）**：manifest description、`IntegrationSection`
  配置描述、README 中的 `github.cateye.music-request` 全部更正为
  `github.orge-8.music-request`（CHANGELOG 历史条目保留原文）。
- README「点歌联动」节补 v2.19.0 加固说明、契约位置互链与探测降级排障行。

### WebUI 显示优化（纯显示层，不改配置键与校验语义）

- **节内字段分组（group 小标题）**：plugin 节 17 个字段按「上下文注入 /
  曲库与导入 / 命令输出」分块，crawler 节按「爬取来源 / 请求与失败策略 /
  SSL 证书」分块（权限三项仍居最前无标题块），emotion 节按「模型配置 /
  时间控制」分块。此前整节平铺一屏，用户找不到功能边界。
- **webui.json 删除 `placement:"workspace"`**：1.4.0 起该值已停用，宿主
  归一化为 sidebar 并发 UserWarning；页面统一收进设置侧栏「插件扩展」。
  `workspace_title` 保留（手机端分组标题）。
- **声明式页面 UX Writing**：「配置的单次批量」→「单次同步批量」（与后台
  任务说明文案对齐）；「演唱 / UP主」→「演唱 / UP 主」（表格列与详情弹窗
  统一）；缺歌词清单 / 最近命中补空态引导文案。
- 回归：`test_webui_display.py` 新增分组块与顺序不变量 2 项；
  `test_webui_pages.py` 的 placement 断言从 1.3.x 语义（必须 workspace）
  更正为 1.4.x 语义（归一化 sidebar + 原始声明不得含 placement）。
  `audit_webui_json.py --host-dir`（v1.4.4 权威校验）**0 error / 0 warning**。
- **修复测试间 sys.path 污染（上轮加固遗留的潜伏 bug）**：
  `test_e2e_cv_music.py` 把对端 music-request 目录插到 sys.path 最前，
  `test_webui_pages.py::test_bound_apis_are_registered` 的裸
  `from plugin import create_plugin` 在全量跑时解析到**对端** plugin.py，
  9 个页面 API 全部误报「未注册」（单独跑绿、全量跑红）。改用文件顶部
  已加载的命名空间包模块 `_MOD`，不再受路径顺序影响。

### WebUI 歌词页 custom 化（视觉参考 Mai_life，代码全部原创）

- **声明式歌词页 → custom HTML 大屏**：新增 `webui/lyrics.html`，
  `webui.json` 改 `mode:"custom"` + `entry:"webui/lyrics.html"` +
  `layout:"document"`，`required_capabilities` 改 `["custom_pages"]`。
  声明式页面没有 CSS 注入通道，视觉上限就是宿主默认渲染；custom 页
  拥有完全视觉自由。**9 个 `lyrics.*` API 原样绑定，后端零改动。**
- **设计语言**（参考 github.com/octmicy/Mai_life，GPL-3.0，**仅借鉴形态、
  代码全部原创**，本插件 MIT）：宿主 `--maibot-*` token 桥接为本地
  `--cvl-*` 变量（带亮/暗两套兜底，暗色跟随宿主 `html.dark` 零 JS）；
  顶栏品牌区 + 状态徽章；4 KPI 瓦片；**原创 SVG 半圆覆盖率仪表**；
  曲库状态 kv 表；任务进度条 + 6 迷你统计 + 失败样本；检索改**分栏阅读**
  （左结果列表右歌词 pane，替代原弹窗，点行即读）；缺歌词 / 最近命中双表。
- **宿主契约对齐**（v1.4.4 源码逐条核实）：CSP 零网络（无外部资源，
  SVG 内联）；自建 **≤2 并发调度队列**（宿主 `pending>=2` 直接 throw）；
  `await maibot.ready` 后才发请求；任务进度 4s 轮询、命中记录 15s；
  条数参数仍传字符串（`_webui_int` 兜底口径不变）；取消按钮保留
  confirmation（宿主弹框）。
- `_manifest.json` capabilities：`declarative_basic` → `custom_pages`
  （与内置 custom 页插件惯例一致；**改 capabilities 需完整重启 MaiBot**）。
- 回归：`test_webui_pages.py` 重写为 custom 契约（19 项：entry/document
  注入、JS 绑定名与声明双向一致、轮询 ≥3s、并发 ≤2、零外部资源、
  manifest 能力）；**新增 `test_webui_render.py`**——Node 最小 DOM stub +
  模拟宿主桥接，用**真实插件 API 返回**驱动页面 JS 完整渲染，20 项断言
  （KPI/仪表/徽章/表格/检索详情/触发链路/并发峰值），无 Node 则 skip。

### 分发包瘦身（assets 压缩，识别质量零改动）

- **内置素材 gzip 压缩随包分发**：`assets/knowledge_db.db`（7.3MB）与
  `assets/song_lyric_keywords.txt`（5.7MB）改为 `.gz`（2.6MB + 1.7MB），
  **合计 13.0MB → 4.3MB，减重 67.2%**。仓库与分发包同步瘦身。
- **加载期解压缓存**：新增纯函数 `_resolve_asset`——未压缩文件优先
  （兼容旧部署/开发环境，零解压开销）；否则解压 `.gz` 到插件数据目录
  `asset_cache/`，按源 `.gz` 的 **mtime+size** 校验缓存失效（插件更新换
  素材自动重解压），`.tmp → replace` 原子替换不留半截缓存。识别逻辑拿到
  的仍是普通文件路径，**匹配语义零改动**。
- **降级语义不变**：损坏/截断的 `.gz`、缺失文件只返回警告，绝不抛异常
  打断索引构建（沿用「素材缺失只降级、不阻断启动」）。
- **方案取舍（实测数据驱动）**：曾评估「直接移除 assets 靠 vcpedia 补偿」——
  真实索引回放 58244 句关键词，裸移除可信报歌名率从 100% 跌到 **42.3%**
  （短句精确命中降为子串命中后被 v2.17.0 证据门控判弱）；加载期短句切分
  补偿可救回到 **85.4%**，但剩余 14.6% 是 vcpedia 歌词版本差异（7919 句 /
  821 首，另一份词源的短句，同步补不回），且索引膨胀 1.87×、构建 +12s。
  故选定**压缩保留**：质量 100% 不变、开箱即用，减重 67%。
- 回归：**新增 `tests/test_asset_resolve.py`**（9 项：未压缩优先 / 解压
  往返一致 / 缓存命中不重解压 / 源变更失效 / 缺失降级 / 损坏降级 / 截断
  降级 / 失败不留半截缓存 / 二进制字节级往返）；`test_subspan_match.py`
  的 `real_index` fixture 兼容 `.gz` 形态、解压缓存写临时目录。
- **等价性实测**：`.gz` 解压路径构建的索引与原始文件基线**逐项完全一致**
  （241235 歌词句 / 7945 元数据 / 1508873 子串片段），关键命中抽查通过。

### 验证

- devkit venv（SDK 2.22.1）全量 pytest：**237 passed**
  （联动加固 +18：探测 17 项 + 契约转绿 1 项；WebUI 分组不变量 +2；
  custom 页契约重写净 +1、渲染冒烟 +1；设 `MAIBOT_HOST_DIR` 后页面测试
  8 项 skip 转绿）；契约相关 92 项、E2E 8 项全绿。
- **新增 `tests/smoke_test.py`**（本仓库此前没有冒烟层）：生命周期 +
  组件注册（含两个 API 的 `timeout_ms=3000` metadata 断言）+ 契约形状 +
  帮助/状态命令 + 注入探测降级全链路，零网络。
- `gate.sh` **PASS 4 / SKIP 0 / FAIL 0**：check_plugin PASS 38 / WARN 5 /
  FAIL 0（WARN 为存量：4 处装饰器命名风格、llm.generate 经 mixin 调用
  静态扫描不到）；contracts PASS；smoke OK；pytest 237 passed。
- **官方最新实测（2026-11-14，交付准入依据）**：
  - 官方渠道核对：MaiBot **v1.4.4**（2026-11-14 发布，较上轮基线 1.4.3 新）、
    SDK **2.23.0**。v1.4.4 源码经 git tag 浅拉取 + archive 导出；
    `plugin_runtime` 与 1.4.3 逐文件对比仅 9 个文件有实质差异，
    **manifest_validator / plugin_loader 零变化**。
  - 官方 v1.4.4 源码 PluginLoader 加载探针（`force_plugin_compatibility=False`）：
    **加载成功，28 组件 / 4 能力**；manifest 收紧后严格区间
    host 1.0.0~1.4.4 / sdk 2.1.0~2.23.0 无警告通过。
  - SDK 2.23.0 隔离 venv（Python 3.13.12 + pytest 9.2.0）：
    pytest **237 passed**、smoke OK、
    `test_vcpedia_schema.py` **103/103**、`test_annotate_llm.py` **7/7**。
  - WebUI 变更后复跑官方 v1.4.4 loader 探针：加载成功，28 组件 / 4 能力；
    `audit_webui_json.py --host-dir` 权威校验 0 error / 0 warning。
  - 据此放宽 `_manifest.json`：host max 1.3.99→**1.4.4**、
    sdk max 2.16.99→**2.23.0**（实测后才 bump，未写占位大数）。
- **custom 页改造后复验（2026-11-14）**：官方 v1.4.4 loader 探针加载成功
  （28 组件 / 4 能力，9 个 `lyrics.*` API 全注册）；`load_webui_extension`
  权威校验通过（entry 读取 + document 注入 + custom_pages 能力）；
  页面 JS 经 `node --check` 语法校验 + Node 渲染冒烟 20 项断言全过
  （真实 API 数据驱动，并发峰值实测 =2）；devkit 与 SDK 2.23.0 两环境
  pytest 均 **237 passed**；gate.sh **PASS 4 / SKIP 0 / FAIL 0**。
- **分发包瘦身后复验（2026-11-15，官方最新实测）**：
  - 官方渠道核对：MaiBot **v1.4.5**（2026-11-14 发布，较上轮 1.4.4 新）、
    SDK **2.23.0**（PyPI 最新）。
  - 官方 **v1.4.5** 源码 PluginLoader 加载探针（`force_plugin_compatibility=False`）：
    **加载成功，28 组件 / 4 能力，instance=yes**，9 个 `lyrics.*` API 全注册。
  - `plugin_runtime` 1.4.4→1.4.5 逐文件对比：`plugin_loader.py` /
    `manifest_validator.py` 去行尾后**零实质差异**（其余差异为行尾/格式），
    加载契约无破坏性变更。
  - 据此放宽 `_manifest.json` host max 1.4.4→**1.4.5**（实测后才 bump）。
  - `.gz` 解压路径构建索引与原始基线**逐项一致**（241235 句 / 7945 元数据 /
    1508873 片段）；devkit（SDK 2.22.1）与 SDK 2.23.0 两环境 pytest 均
    **239 passed / 7 skipped**（新增素材定位 9 项；skip 为无 Node/对端目录
    的环境性跳过）；gate.sh check/contracts/smoke 全 PASS。
- **未真机验证**：探测降级与超时收紧需真机回归（部署指南随交付给出）；
  custom 页需真机 dashboard 打开确认渲染与暗色跟随（本地已用真实 API
  数据 + 模拟宿主桥接做渲染冒烟，但宿主主题探针 / 字体注入未模拟）；
  素材解压缓存需真机首启确认（本地已验证解压往返一致与缓存失效逻辑）。

## [2.18.0] - 2026-11-06

### 新增

- **多来源字段级补全**：新增 `vcpedia_merge.py`（纯函数）与
  `SongStore.merge_missing()`。参考 mohobot
  `mohobot/music_knowledge/normalize.py` / `merge.py`（MIT License,
  Copyright (c) 2026 CarefreeSongs712；许可声明见 `vcpedia_merge.py` 文件头），
  落地两条数据原则：
  1. 归一化名**只用于生成候选**，绝不当写入键——定位复用既有
     `_lookup_song_rows` 的四层候选，命中多行即返回 `ambiguous` 并不写库；
  2. **非空值永不被空值覆盖**，同字段多来源取值冲突时保留现值并记入
     `conflicts`，不静默丢弃差异。
- 版本名保护：`X(中文版)` 这类带版本标记的歌名只允许 `exact` 层命中；通过
  去括号层命中原版 `X` 时返回 `version_name` 并拒绝合并，避免把两个版本并成一首。

### 边界

- `upsert()` 语义**不变**（仍是单来源全量覆盖，供 VCPedia 全量同步）；
  `merge_missing()` 是 additive 入口，不新增歌曲、不改既有同步链路、当前无调用方。
- 关系/正文类字段（创作者、歌姬、主歌词、分类）按**整块为空**才补，
  避免 `write_*` 的整体替换语义抹掉 engine / voicebank / position 等细节。
- `sdk.max_version` 保持 `2.16.99` 不动：官方 SDK 已到 2.21.0，按会话启动规则
  只能实测后再 bump，禁止写 `2.99.99` 之类的占位大数。

### 验证

- `gate.sh` 全绿：check_plugin PASS 38 / WARN 5 / FAIL 0；pytest
  **197 passed / 8 skipped**（含本次新增 8 条）。
- 新增 `tests/test_vcpedia_merge.py`：归一化候选、版本名分类、字段合并语义、
  只补空不覆盖、二次调用幂等、歧义拒绝、跨版本拒绝、not_found / invalid。
- **未真机验证**：本方法目前只作为后续多来源同步的写入入口，尚未接入运行链路。

## [2.17.0] - 2026-11-02

### 修复

- **弱证据命中被当成「用户在引用这首歌」（真机第二起，社交事故级）**。
  群友在群里贴了一段多首歌的歌词，另一位群友评价「空有其表」，bot 回的是
  「《洛天依的自白》确实有点空有其表」——把对作品的评价挂到了一首并非话题中心的歌上。

  当时的注入块里有三条命中：

  | 片段 | 命中 | 类型 | 证据 |
  |---|---|---|---|
  | 「一个又一个不」 | 《木雕鸽子》 | 6 字子串 | 所在歌词行 20 字，只覆盖 30% |
  | 「绝体绝命」 | 《洛天依的自白》 | 4 字整句 | 长度仅 4（该曲歌词里恰好有这一整行） |
  | 「请为我悼念悼念]」 | 《绝体绝命》 | 8 字整句 | 真引用 |

  前两条和真引用享受**完全相同的注入格式**（歌名 + 歌手 + P主 + 歌词窗），模型顺手
  就拿第 2 条定了名。注意根因不是「认错歌」——三条都指对了歌，问题是**没有区分证据
  强度**：撞上一截 6 字片段，和引用一整句歌词，在注入里长得一样。

  修法：`record_hit` 为每条命中计算证据强度（新方法 `_hit_confidence`），两条判据缺一
  不可：
  1. 片段 >= 6 字（4~5 字的整句多半是曲名/成语）；
  2. 子串命中还要求共享段覆盖**所在歌词行** >= 60%（6 字在 9 字行里是引用，在 20 字
     行里是撞车）。

  弱证据在两条注入链路上都不再出现歌名/歌手/P主：回复侧只留
  「「片段」（曲目未确认，仅作歌词参考）」+ 相近歌词，并明确「不要据此点名歌名、
  歌手或作者」；规划器侧同样不给歌名与点播查询串，全部为弱时直接告知
  「不要据此调用任何歌曲工具」。8 字整句引用（「请为我悼念悼念」）照常报歌名。

  真机同轮复现：修复后注入里只剩《绝体绝命》一个歌名，另两条降级为「曲目未确认」。

### 变更

- 会话命中结构 `(时间戳, 片段, 歌名)` → `(时间戳, 片段, 歌名, 是否可信)`；
  日志行尾部加「（可信）」/「（证据不足，注入侧不报歌名）」。
- 判据是硬编码常量 `_CONFIDENT_MIN_CHARS = 6` / `_CONFIDENT_MIN_COVERAGE = 0.6`，
  暂不开成配置项（先看真机表现再决定是否暴露给用户）。

### 已知限制

- 强证据（>= 6 字且覆盖 >= 60%）在 TTL（默认 600 秒）内会继续参与后续轮次的注入。
  本轮若有人在几条消息之后才评论歌词，那个歌名仍可能被带进评论的回复里。
  要根治得做「按消息新鲜度分级」或缩短 TTL，属独立改动，本次未动。

### 测试

- 新增 `tests/test_hit_confidence.py`（12 例）：4 字整句判弱、8 字整句判强、
  6 字子串按覆盖率判弱 / 判强、弱证据不出现歌名、混合注入加「曲目未确认」、
  规划器侧不给点播串、`_hits` 结构契约。
- 全量：189 passed / 8 skipped。

---

## [2.16.0] - 2026-11-02

### 修复

- **跨行引用被判给邻居歌（真机事故）**。群友发「来来我最亲爱的朋友 请为我悼念悼念」，
  日志只打出两条子串命中——《见字如面》与《光明的未来》，真正的《绝体绝命》
  （洛天依 / 阿良良木健）一次都没出现。根因两层，缺任一条都解释不通：

  1. **数据层：同步库的歌词行被「已索引」短路丢弃**。基础词库
     `song_lyric_keywords.txt` 是预生成的残缺快照——《绝体绝命》只收了 5 行，
     同步库里是 95 行；而 `_load_song_db_into` / `_fill_lyrics_from_db_into`
     都用 `if name in index.indexed_names: continue` 跳过整首歌。那 5 行一点亮
     标记，更完整的歌词就永远进不来。真机 36MB 库实测：词库与同步库的 2785 首
     交集里 **2086 首（74.9%）** 在库里有更多行，累计 **39225 行**被永久丢弃。
     `indexed_names` 只能证明「这首歌有句子进过索引」，不能证明「歌词已收全」。
  2. **匹配层：位置先到先得，从不比较共享长度**。`_subspan_find` 按 6 字窗
     从左到右扫，谁先撞上谁定案（`taken` 只是同歌去重）。真正的歌缺位时，
     它把「我最亲爱的朋」（与《见字如面》共享 7 字）和「亲爱的朋友请」
     （与《光明的未来》共享 6 字）坚定地判给了邻居。

  修法：
  - 两个库加载函数取消 `indexed_names` 短路，按 (句, 歌) 幂等合并新增行；
    `indexed_names` 退回纯统计用途（`_index_song_into` 内部已保证不重复插入）。
  - `_subspan_find` 改为按**最长连续共享段**给每首歌打分（n 字窗连中 k 个
    ⇒ n+k-1 字共享，同歌多个不连续段取最长），只保留并列最高分；返回的片段
    是该共享段原文（不再是 6 字窗），`_lyric_window` 的包含匹配因此能直接定位
    真实歌词行。最高分仅达最小窗口（== n）且多首歌并列时判为歧义、不猜。

  真机同句复现：修复前该曲零命中 → 修复后 `[('来来我最亲爱的朋友', ['绝体绝命'])]`；
  《绝体绝命》索引句数 5 → 63。

### 变更

- 索引规模与启动成本（本地实测，含 36MB 同步库）：歌词句 185919 → 241235、
  子串片段 1195350 → 1508873、构建 13.0s → 17.9s、峰值内存 267MB → 360MB
  （`tracemalloc` 口径；构建仍在后台线程，不阻塞事件循环，代价是刚加载的
  头十几秒识别不可用）。
- 日志措辞随语义调整：`外部歌曲库 X: 新增 N 首歌` → `X 首歌贡献了歌词
  （新歌与补全合计）`；`基础词库歌词补漏` → `基础词库歌词补全`。
  **数字含义变了**：补全的歌也计入，不再只数新歌。

### 测试

- 新增 `tests/test_lyrics_merge.py`（6 例）：已索引歌曲仍合并新行、合并幂等、
  blob/空歌词不点亮已索引标记、缺字段报错。
- `tests/test_subspan_match.py` 增 4 例：跨行引用取胜者、长共享并列保留全部、
  最小窗口并列判歧义、最小窗口单归属仍接住；并把「片段 = 整段共享原文」
  写进断言。
- 全量：177 passed / 8 skipped。

---

## [2.15.1] - 2026-10-31

### 修复

- **回复器把歌词注入块当成用户发言（v2.15.0 回归，线上事故级）**。
  真机 replyer 的 thinking 原文：`User says lyric recognition block with lyric
  "这样下去的话" and instruct natural reply.` —— 它去回答歌词块，而不是回答真正
  说话的那个人（同轮的 `@鸣澜 晚安` 被当成「上一句可见的话」跳过了）。

  根因：v2.15.0 为了让注入落在请求尾部（缓存友好），自己往 `items` 末尾追加了一条
  `UserMessageItem`。那条 item 没有任何宿主赋予的框语义，又与宿主自己的三条脚手架
  （`reply_act` / 当前时间与目标消息 / 风格要求）并排，模型无法分辨哪条是真消息。

  修法：回复侧改走**宿主原生通道 `extra_prompt`**（参考 `life-frequency` v1.25.3
  的真机做法）。该字段由宿主折进「回复要求」，位置本就在请求尾部——缓存收益一分不少，
  语义却由宿主框住。**插件不再改写 `items` / `messages`**。
  同时把 hook 从 `maisaka.replyer.before_model_request` 换到
  `maisaka.replyer.before_request`：官方 hook 字段表里 `extra_prompt` 只存在于后者。

### 变更

- 新增 `_MAX_EXTRA_PROMPT_CHARS = 1500` 预算。`extra_prompt` 是与别的插件共享的通道，
  超预算时**截断自己的文本**（丢 STAFF 尾巴）而不是放弃注入（丢整条识别结果），
  更不挤掉别人的内容；已占满则本轮跳过并留日志。
- `_build_system_text` → `_build_reply_text`：它已不代表任何一条具体的消息。
- `plugin.inject_position` 语义收窄为**只影响规划器**（回复侧固定走尾部通道），
  配置说明与 README 同步更新。
- 日志改为 `已向 LLM 上下文注入歌曲信息（extra_prompt，N/M 字）`，N<M 即被截断；
  契约不匹配（载荷里没有 `extra_prompt`）打 WARNING。

### 已知遗留（不在本次范围）

- **规划器侧仍自拼 `UserMessageItem`**（同样的角色可辨识性问题，优先级较低：planner
  只做工具决策，不产出正文）。`life-frequency` 对 planner 用的是带 marker 的
  `SystemMessageItem`，可作为下一版方案，但缓存落点尚未在真机验证。
- 命中行仍按**整行**加「」高亮，用户只发行内片段时模型会误判「下一句」是哪句
  （同一条 thinking 里用了三段纠结这一点）。属文本格式问题，与注入位置解耦。

### 测试

- 新增 `tests/test_replyer_extra_prompt.py`（10 项）：只写 `extra_prompt`、
  不动 `items`/`messages`、保留他人内容、marker 幂等、预算截断与占满跳过、
  缺字段告警、日志含通道名，以及一条**静态契约**——`plugin.py` 里不许再出现
  字符串字面量 `"maisaka.replyer.before_model_request"`。
- `tests/test_tail_injection.py` 收窄为规划器侧（4 项改为 planner 版）。
- `tests/test_single_system_injection.py` 的 4 项回复器用例合并为 1 项
  「回复侧不参与 system 之争」。
- 全量：**167 passed / 8 skipped**；`gate.sh` PASS 2 / FAIL 0（check_plugin 36 PASS / 4 WARN，
  4 条 WARN 均为既有项：装饰器名与 def 名不同源 ×3、`llm.generate` 静态未检出 ×1）。

---

## [2.15.0] - 2026-10-29

### 变更

- **注入位置改为末尾追加**（新增 `plugin.inject_position`，默认 `tail`）。
  注入不再并进 `SystemMessageItem`，改为在请求末尾追加一条 `UserMessageItem`
  （messages 路径则追加一条 `user` 消息）。已有的 system 与全部历史一个都不动。
- `inject_position = "system"` 保留 v2.14.x 行为作为回退开关；
  取值缺失或非法时一律按 `tail` 处理，且只警告一次。
- 注入日志增加「位置=」外显，便于真机核对实际生效值。

### 修复

- 换歌导致 prompt cache 全量失效：供应商按「从第 0 个 token 起的最长公共前缀」
  计缓存，而宿主请求第一条恒为 system。v2.13.1 起歌词被并进那一条，
  歌单一换整段 system 就变，其后所有历史全部按未命中价计费。

  真机实测（2026-10-27 ~ 29，778 个 prompt 快照）：

  | 指标 | replyer | planner |
  |---|---|---|
  | 完全没有可复用前缀的请求 | 17.5% | 11.8% |
  | 逐例归因结果 | 48/48 均为首条 `SystemMessageItem` 变化 | 同 |
  | 差异位置 | 固定落在字符 959 处的【歌词识别】标记 | 同 |

### 测试

- 新增 `tests/test_tail_injection.py`（13 项），核心契约是「注入后前 N 项与注入前
  逐项相等」，另覆盖换歌前缀稳定性、幂等、配置分流与日志外显。
- `tests/test_single_system_injection.py` 改为显式固定 `system` 模式，
  继续验证单 system 纪律（该纪律在 tail 模式下天然成立，但回退路径仍需守住）。

---

## [2.14.6] - 2026-10-22

### 变更

- 提交前整改 + 隐私清理，按插件中心规则逐项核对。
- `sdk.max_version` 由 v2.14.5 放宽的 `2.99.99` 收紧为 `2.16.99`
  —— 「永远兼容」的写法会被插件中心审核硬阻断。

---

## [2.14.5] - 2026-10-22

### 变更

- 放宽 SDK 上限声明到 `2.99.99`（v2.14.6 已收回，见上条）。

## [2.14.4] - 2026-10-22

### 修复

- 歌名匹配改为逐层查：精确命中优先于归一化命中，避免归一化后串到同名曲。

## [2.14.3] - 2026-10-22

### 变更

- 跨插件 API `get_song_bilibili` 增加 `singers` 字段，返回原曲歌手。

## [2.14.2] - 2026-10-22

### 新增

- `/歌词 补BV` 命令：为已有记录回填 B 站标识，不必整库重抓。

## [2.14.1] - 2026-10-21

### 新增

- 歌名多级匹配，按歌名也能命中原曲 B 站标识（此前只认 BV 号路径）。

## [2.14.0] - 2026-10-21

### 新增

- 采集 VCPedia 词条中的原曲 B 站标识（BV 号 + UP 主），随同步写入知识库。

---

## [2.13.1] - 2026-10-19

### 修复

- **一次请求两条 system 导致供应商报错**（用户反馈「识别歌曲插入的消息被设置
  为 system 消息了」）。
- 根因：注入逻辑无条件**追加**一条 `role=system`（items 路径则是新增一个
  `SystemMessageItem`），而宿主请求本身已带一条 system（人设 / 工具说明）。
- 修复：改为**并入**已有 system——messages 路径把文本并进已有那条 system 的
  content（多模态 list content 则追加一个 text part），items 路径并进已有
  `SystemMessageItem` 的 parts；只有在完全没有 system 时才新增一条。
- 纪律固化：`_merge_system_into_messages` / `_merge_system_into_items` 两个
  单一入口承担全部注入改写，planner 与 replyer 两条链路共用。

  > 注：这次修复把动态歌词放到了 prompt 最前面，引入了缓存问题，v2.15.0 已迁移到末尾追加。

## [2.13.0] - 2026-10-17

### 变更

- **配置页可用性轮**（用户反馈「配置项含混、找了很久才找到填管理员的地方」）。
- 「管理员 QQ 号」等三项权限字段提到 `[crawler]` 节最前面，节标题由
  「VCPedia 爬取」改为「同步与命令权限」。
- `[crawler]` 节默认展开（它现在独占页签，且含唯一必须配的开关）。
- 字段文案改成用户语言：`sync_admin_ids` 的 label 由「可触发重任务的 QQ 号」
  改为「管理员 QQ 号（谁能触发同步）」，hint 首句就说清留空即禁用、只读命令不受限。
- 命令被拒时的提示改为给出可视化路径（页签 → 字段名），不再只丢
  `crawler.sync_admin_ids` 这种内部键名。

## [2.12.0] - 2026-10-17

### 新增

- dashboard 自定义「歌词曲库」页面（浏览 / 搜索 / 详情）。
- WebUI 双管线优化轮：可视化模式按 `ui_type` 渲染控件时只输出 label / hint /
  placeholder，从不渲染 description —— 本插件的填法说明原本全写在 description 里，
  用户在配置页一个字都看不到。改为把说明搬进 hint（详见 README「dashboard 自定义页面」）。

### 变更

- 过审整改：收紧 `max_version`，移除离线诊断脚本。

## [2.11.0] - 2026-10-13

### 新增

- 二次元 / 网络梗过滤：寒暄梗（Ciallo、空尼奇瓦、前方高能等）不再被子串兜底
  误识别为歌词。支持配置追加社群专属梗词（`plugin.meme_phrases`）。

## [2.10.0] - 2026-10-06

### 新增

- 子串匹配兜底：整句未命中时按 N 字滑窗在歌词库找连续共享片段，
  接得住「记岔开头 / 只记得半句」的引用（`plugin.subspan_min_chars`，0 = 关闭）。

---

## [2.9.0] - 2026-10-05

### 新增

- 跨插件只读 API `get_recent_songs`：暴露最近命中过的歌曲，供其他插件读取。
- `sdk.min_version` 由 `2.0.0` 提升到 `2.1.0`（`@API` 组件自该版本起可用）。

### 修复

- 最近歌曲记录的降级不再静默：取歌手失败、写盘失败、加载期坏文件被备份重建，
  三处都打 warning（带歌名 / 路径 / 原因），免得「API 里 songs/artist 一直为空」
  在真机上查不出线索（全检第 12 项）。

---

## [2.8.0] - 2026-09-13

### 变更

- VCPedia 知识库结构规范化 + 老库自动迁移：三种历史库形态（7 / 18 / 21 列）
  逐字段零丢失迁移到关系型结构。详见 README「知识库结构」。

## [2.7.0] - 2026-09-11

### 新增

- 点歌联动（`[integration]` 配置节）：识别出歌词时为前几首歌生成「歌名 歌手」
  可点播查询串，并提示规划器在用户表示想听时调用点歌插件
  （`github.cateye.music-request`）的 `search_and_play_music`。

## [2.6.2] - 2026-09-09

### 变更

- 性能与内存优化轮：降低索引构建与同现查询的常驻开销。详见 README「性能与内存」。

## [2.6.0 / 2.5.0] - 2026-09-04

### 新增

- 情绪标签体系（v2.5.0 氛围选歌）：同步后为新歌标注情绪，LLM 可通过
  `recommend_cv_song` 按对话氛围推荐歌曲，自带歌手归属校验与最近推荐去重。
- 同步后自动标注（v2.6.0，默认开启）。

---

## [2.4.x 及更早] - 2026-08-30 ~ 2026-09-02

当时的 git 提交未逐条标注版本号，按时间列出主要能力：

| 日期 | 能力 |
|---|---|
| 2026-09-02 | VCPedia 解析器诊断增强 + 会话级缓存 |
| 2026-09-01 | 补 MIT License |
| 2026-08-31 | 歌词上下文注入（歌名 / 歌手 / P主 / 创作信息）+ 解析器改进 |
| 2026-08-31 | VCPedia 歌词抓取与重抓 |
| 2026-08-30 | VCPedia 歌曲库与同步支持 |
| 2026-08-30 | 歌词收件箱导入 + 自定义歌单 |
| 2026-08-30 | 改用 hook 检测 + items 注入（替代早期的事件监听） |
| 2026-08-30 | 初始版本：歌词识别与上下文注入骨架 |
