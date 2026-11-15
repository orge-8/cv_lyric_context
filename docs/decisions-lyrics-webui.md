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

## 2026-10-17 · v2.13.0 配置页可用性轮（用户反馈驱动）

反馈原文：「建议优化一下配置的展示和 readme」「`/歌词` 这个配置项是用来添加管理员的，
但实际描述有些含混不清，我找了很久才找到原来在这里填写」。

**根因（不是文案差，是信息架构错）**：

1. 权限三项（`allow_sync_command` / `sync_admin_ids` / `allow_local_operator`）放在
   `crawler` 节里，节标题却是「VCPedia 爬取」——用户不会去「爬取」里找「管理员」。
2. 该节默认**收起**（v2.12.0 的决定），唯一必须配的开关被折叠掉。
3. 字段 label 是「可触发重任务的 QQ 号」，用的是内部术语「重任务」；
   用户的心智模型是「加管理员」。
4. 权限字段在第 9 位，前面压着 6 个爬取参数（base_url / categories / depth / interval…）。
5. 命令被拒时只回 `crawler.sync_admin_ids` 键名——用户不知道该去哪个页面点哪里。

**处置**：

- **`layout: tabs` 三分组**（识别与注入 / 同步与权限 / 扩展功能）。理由：5 个节平铺一页时
  权限项被爬取参数淹没；页签让「同步与权限」成为一级入口。**前端只渲染 tab.sections
  里列出的节**（`plugin-config.tsx:1375-1402`），所以未归类的节一律追加到最后一个页签
  ——漏一个就等于该节整块消失，已加回归测试钉住。
- **`crawler` 不再默认收起**。理由：它独占一个页签，且含唯一必须配的项。
- **权限三项用字段 `order` 提到节首**（`_WEBUI_FIELD_PRIORITY`）。理由：被约束的参数
  （抓取数量、限速）应排在授权之后；只改显示顺序，不碰 `model_fields` 定义顺序与校验。
- **节标题改「同步与命令权限」、字段 label 改「管理员 QQ 号（谁能触发同步）」**。
  理由：让用户按自己的词（管理员）就能命中；节级 description 会渲染，写成引导语。
- **label override 改为无条件优先**。理由：原逻辑仅在 label 仍等于键名时生效，
  一旦 SDK 侧补上默认 label 就会静默失效；显示层的手工决定优先更稳。
- **命令拒绝文案给出可视化路径**（页签 → 字段名），保留键名作为括号注释给开发者。
  理由：拒绝文案是用户唯一的信息来源，只给键名等于把人丢进 TOML。

**不做**（记录取舍）：

- **不拆独立 `[admin]` 配置节**。拆了更干净，但 `crawler.sync_admin_ids` →
  `admin.sync_admin_ids` 是**破坏性迁移**：没有配置键迁移机制，老用户升级后
  白名单凭空清空、重任务命令全锁，等于把这次的坑再挖一次。等哪天真要迁移，
  得先做「读旧键 → 写新键 → 记 config_version」的迁移链。
- **不加 tab badge 标「待配置」**。理由：schema 后处理只拿到 `default`，拿不到用户当前值，
  无法区分「没填」和「填了」——宁可不做，也不给假提示。
- **不用 `depends_on` 做「开关关掉就隐藏白名单」**。理由：`_build_field_schema` 会输出
  该键，但前端 `FieldRenderer` 不消费（grep 无引用），写了也没效果。

## 2026-11-14 · v2.19.0 歌词页 custom 化（视觉参考 Mai_life）

用户诉求：「参考麦生活的 webui 显示风格」。调研结论（详见工作区
`Mai_life拆解分析.md`）：麦生活的美观来自 **custom HTML 页面**——声明式页面
没有任何 CSS 注入通道，视觉上限就是宿主默认渲染，上一轮的分组/UX 优化已是
声明式形态的天花板。

- **声明式歌词页整体替换为 custom 页**（`webui/lyrics.html`），不并存。理由：
  一个入口不分裂；custom 页完整承接检索/任务/缺歌词/最近命中全部功能，
  9 个 `lyrics.*` API 原样绑定、后端零改动。宿主 v1.4.4 的
  `pages: List[Union[DeclarativePage, CustomPage]]` 虽允许并存，但两页功能
  重复只会让用户困惑。
- **许可红线**：Mai_life 是 **GPL-3.0**，本插件 MIT，**只借鉴设计语言
  （token 桥接手法、组件形态、交互模式），代码全部原创**——类名 `cvl-*`、
  SVG 仪表、调度队列全部自写，不复制其任何源码。
- **检索从「表格 + 详情弹窗」改「分栏阅读」**（左结果列表右歌词 pane）。
  理由：声明式弹窗只能吃行内 preview（拿不到 selection 再查），custom 页
  可以点行后调 `lyrics.song` 取完整档案（创作者分工/标签），阅读体验升级。
- **条数参数仍声明 string**。理由：custom 页 JS 虽可传真实类型，但保持与
  `_webui_int` 字符串兜底口径一致，避开宿主 integer 严格校验
  （`type(value) is int`）的布尔/浮点坑；测试纪律不变。
- **自建 ≤2 并发调度队列**。理由：宿主桥接 `pending >= 2` 直接 throw，
  轮询（4s 任务 + 统计、15s 命中）与用户操作共用队列，避免互相踩闸。
- **轮询写进页面 JS（4s）而非声明**。理由：custom 页没有
  `poll_interval_seconds` 字段；沿用声明式时代 ≥3s 的口径取 4s 留余量。
- **manifest capabilities `declarative_basic` → `custom_pages`**。理由：
  与内置 custom 页插件（weixin_clawbot_adapter / qq_official_adapter）惯例
  一致；宿主对 webui 能力的权威校验在 webui.json 的 `required_capabilities`。
- **新增 Node 渲染冒烟回归**（`test_webui_render.py`）。理由：custom 页渲染
  逻辑全在插件自己的 JS 里，DOM id 拼错/渲染崩溃只有真机才暴露；用最小
  DOM stub + 模拟宿主桥接 + **真实插件 API 返回**驱动页面完整跑一遍，
  20 项断言。无 Node 则 skip（与宿主契约测试同口径）。

**不做**（记录取舍）：

- **不引外部图表库**。CSP `connect-src 'none'` 零网络，且 8MiB 产物配额下
  内联一个图表库得不偿失；覆盖率仪表用原创 SVG 半圆弧（dasharray 控制填充）。
- **不做暗色手动切换**。宿主 SDK 已按主题切换 `html.dark` 并注入 token，
  页面桥接变量自动跟随，手动切换只会与宿主状态打架。
- **music-request 侧本轮不做**（用户决定）。其配置页保留上一轮三页签优化；
  状态页 custom 化留待后续。

## 待定

- 缺歌词清单的分页（当前 `limit` 截断，量再大需要分页；custom 页可自写
  「加载更多」按钮，不再受声明式 pagination 节点约束）。
- 任务失败明细（`task.failures`）已上页面（失败样本列表，≤5 条）；
  失败多时的展开/折叠交互可再打磨。
- custom 页真机验证：宿主主题探针 / 字体注入 / ResizeObserver 高度上报
  未在本地冒烟中模拟，需真机 dashboard 打开确认渲染与暗色跟随。
