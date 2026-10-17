# 决策记录：歌词曲库 WebUI（dashboard 自定义页面 + 配置页）

格式：结论 + 理由。新问题列在文末「待定」。

## 2026-10-16 · v2.12.0 页面初建

- **页面走 `placement: workspace`（+ `workspace_title`）**。理由：sidebar 页只在「设置」
  工作区进左侧栏（Layout.tsx 过滤），用户停在别的插件工作区时整页消失。
- **触发类操作不另做鉴权**。理由：dashboard 登录态把关；但仍尊重 `plugin.enabled` 与
  `crawler.allow_sync_command`。代价：能登录 dashboard 的人即可触发对外网的重任务。
- **页面不继承「0 = 不限」**。理由：误点一下就是小时级任务；条数口径 =
  显式值 > `sync_batch_limit` > 保底 50，夹到 500。
- **数字参数在声明里一律 `string`**。理由：input 节点只交字符串，声明 integer 会被宿主
  判 422；解析与夹取放插件侧 `_webui_int` / `_webui_resolve_limit`。
- **单条写入不做**（改歌词/改情感走 `/歌词 重抓`）。理由：与导入链路、标注覆盖规则
  对齐的边界多、收益低。
- **触发类操作传「协程工厂」而非协程对象**。理由：校验失败路径上留下建好却没 await 的
  协程会刷 RuntimeWarning；页面路径同样复位 `_refetch_stop`。

## 2026-10-17 · v2.12.0 WebUI 优化轮（常规强度拷打后执行）

- **补 `poll_interval_seconds: 3`**。理由：`auto_refresh` 只管「交互节点变更」触发重查；
  前端 `if (seconds < 3) return`，不写轮询则「触发任务 → 看进度」断链，只能手动刷新。
  宿主约束：0=关、1/2 拒绝、上限 60；查询均为本地 SQLite 读，3 秒轮询代价可忽略。
- **input 长 label 精简，说明下沉为旁挂 `text` 节点**。理由：自定义页面协议**没有**
  hint/placeholder（宿主 `webui_schema.py` 的 Node 只有 label/when/value），label 超 20 字
  前端截断；`text` 节点是唯一的信息下沉通道。三处：本次条数 / 检索关键词 / 跳过天数。
- **任务区加「任务类型 / 已用时」两行（仅 running 时显示）**。理由：`api_task` 本就暴露
  `kind` / `elapsed`，零 API 改动；进度条之外给出「在跑什么、跑了多久」的即时反馈。
- **配置页坏 label 一律手工 override**（`_WEBUI_LABEL_OVERRIDES` 扩到 19 条）。
  理由：`_webui_label_from_description` 的 12 字硬切对 CJK 无词边界，实测产出
  `「/歌词`、`…的`、`…是否`、`PV/曲` 等断句；手工 label 是唯一可靠解，自动推导仅作
  未来新字段的兜底。截断尾字符集同时补了 `【「`，防 dangling 开括号。
- **格式敏感文本字段补 placeholder**（11 个字段，schema 后处理注入）。理由：placeholder
  是显示层灰字提示，不改校验语义（区别于 `ge/le`、`Literal` 这类会改变加载行为的改法）。
- **crawler / recommend / emotion / integration 四节默认收起**。理由：都是「配一次就不用管」
  的配套功能节；主配置节 15 个字段是核心，整页全开淹没常用项。收起后标题与说明仍可见。
- **不做动态下拉（scope select 保持静态列表）**。理由：声明式页面没有动态 options 通道，
  要动态只能 `mode: "custom"` 自写 HTML，代价是失去全部声明式能力；两个固定选项不值得。

## 待定

- 缺歌词清单的分页（当前 `limit` 截断，量再大需要 pagination 节点）。
- 任务失败明细（`task.failures`）是否上页面：目前只在 summary 里，失败多时才值得单列。
