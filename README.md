# 中V歌词识别 · 歌曲库 (cv_lyric_context)

中文 VOCALOID（洛天依、言和、乐正绫等）歌词识别 + 歌曲库插件，两件事：

1. **认歌词** —— 入站消息命中歌词时，向 MaiBot 的 LLM 上下文注入歌曲信息，
   让 bot 能自然接住歌词话题
2. **管歌库** —— 内置 VCPedia 爬虫，可以从 [VCPedia](https://vcpedia.cn)
   同步 4000+ 首中V歌曲（含作词作曲等完整创作信息）到本地 SQLite，
   既能扩充歌词识别的词库，也能用 `/歌词` 命令或聊天直接查歌

词库有三个来源，可以叠加使用：

| 来源 | 数据 | 怎么来 |
|---|---|---|
| 内置基础库 | 3412 首旧快照，57227 句歌词 | 自带 |
| VCPedia 同步 | 4000+ 首，含完整创作信息与歌词 | `/歌词 同步`（后台爬取） |
| 歌词文件 | 你自己放的歌 | 丢进 `lyrics_inbox/`（插件数据目录下），`/加歌` |

## 安装

**纯标准库实现，没有任何第三方依赖**（网络请求走 `urllib`，数据库走内置 `sqlite3`），
不需要 `pip install`，也没有 `requirements.txt`。

| 项 | 要求 |
|---|---|
| MaiBot 宿主 | 1.0.0 ~ 1.99.x |
| 插件 SDK（`maibot_sdk`） | 2.0.0 ~ 2.99.x |
| Python | 3.9+（用宿主自带的解释器即可） |

步骤：

1. 下载本仓库：`git clone https://github.com/orge-8/cv_lyric_context.git`，
   或直接下载 zip 解压。
2. 把整个 `cv_lyric_context/` 目录放进 MaiBot 的 `plugins/` 下，
   **目录名保持 `cv_lyric_context`**——Runner 按目录名加载插件，
   里面有 `_manifest.json` 和 `plugin.py`，不要只挑文件拷。
3. **完整重启 MaiBot**。插件目录的变动不会热加载，必须重启进程。
4. 启动日志里出现 `中V歌词识别已加载: N 句 / M 首` 就是装好了。

首次启动时 Runner 会自动在插件目录下生成 `config.toml`，
`/歌词`、`/歌词 状态`、`/歌词 搜索`、`/歌词 歌曲` 这些只读命令开箱可用。
要跑同步 / 补歌词这类重任务，先按下面的「配置」一节填好 `crawler.sync_admin_ids`。

## 快速开始

```bash
# 1. 在 QQ 里发（先小批量试跑，确认数据正常再跑全量）
/歌词 同步 100
# 2. 同步完会自动重建识别词库，新歌立刻可被识别，不用重启
# 3. 试试
/歌词 搜索 普通DISCO
/歌词 歌曲 普通DISCO
```

`Category:洛天依歌曲` 递归后约 4000 个词条，按默认 0.8 秒间隔全量同步约 1 小时。

## 命令

| 命令 | 说明 |
|---|---|
| `/歌词` | 显示用法 |
| `/歌词 状态` | 本地歌曲数量、空歌词条数、上次同步时间、同步进度，附解析器自检结果 |
| `/歌词 同步 [数量]` | 后台增量同步，可限定本次抓取数量（如 `/歌词 同步 100`） |
| `/歌词 取消` | 中止正在进行的同步或批量补歌词 |
| `/歌词 重抓 <歌名>` | 重新抓取单个词条，刷新歌词 / 简介（常规同步不更新已入库的歌） |
| `/歌词 补歌词 [数量]` | 批量重抓歌词为空的条目并回填（后台执行，非歌曲页自动跳过；不填数量用 `sync_batch_limit`） |

「补歌词」会给**确认无歌词**的条目（非歌曲页、无歌词章节）打上时间戳，在 `crawler.refill_cooldown_days` 天内跳过，避免它们堵在队首被反复重抓；抓取失败的不打，下次仍会重试。存量见 `/歌词 状态` 的「待补 / 近期已确认无歌词」。
| `/歌词 搜索 <关键词>` | 按歌名 / 歌手 / P主搜索 |
| `/歌词 歌曲 <歌名>` | 查看歌曲详情与歌词 |
| `/加歌` | 导入 `lyrics_inbox/` 里的歌词文件 |

聊天里直接问也行，LLM 会自动调用 `search_vcpedia_song` / `get_vcpedia_lyrics`。

> **重任务命令默认关闭。** `/歌词 同步`、`/歌词 补歌词`、`/歌词 重抓` 会向 VCPedia
> 连续发起成百上千次请求，因此要先在插件配置 `crawler.sync_admin_ids` 里填入你的
> QQ 号才能触发（多个用英文逗号分隔）；没填时这几条命令一律拒绝并提示去配置。
> 本机控制台（宿主标记 `is_local_operator` 的调用）默认不受限，可用
> `crawler.allow_local_operator = false` 一并纳入白名单校验。
> `/歌词`、`/歌词 状态`、`/歌词 搜索`、`/歌词 歌曲`、`/歌词 取消` 都是本地只读操作，
> 任何人都能用。

## 工作原理

```
用户消息 "某句歌词"
   │
   ├─ 监听入站消息 ──> 清洗文本（全半角/标点/大小写）
   │   新版: HookHandler chat.receive.after_process
   │   旧版: EventHandler ON_MESSAGE（回退）
   │                        在 57227 句关键词表中 O(1) 精确匹配
   │                        整句未命中 -> 子串匹配兜底（v2.10.0）：
   │                        按 N 字滑窗（默认 6，0=关闭）查约 30 万个
   │                        歌词片段，连续共享 >= N 字即命中——接得住
   │                        「记岔开头/只记得半句」（"可是我并不喜欢
   │                        吃鱼" -> 《我的悲伤是水做的》）
   │                        命中 -> 按会话登记 (时间戳, 歌词, 歌名)
   │
   └─ HookHandler (maisaka.planner.before_request + maisaka.replyer.before_model_request,
                   均 BLOCKING)
                            把 TTL 内的命中同时注入到规划器与回复器两条 LLM 链路：
                            · 规划器: 决定调用哪些工具前先看到歌词语境
                              （优先推荐/检索用户正在聊的歌），prompt/messages/
                              items 三种载荷结构都能追加
                            · 回复器: LLM 请求前，把命中整理成 system 内容注入
                              · 新版运行时传 items  -> 追加 SystemMessageItem 快照
                                （Context Item schema v1）
                              · 旧版运行时传 messages -> 追加 {"role": "system"}
                            两条链路各自幂等（含【歌词识别】标记就不重复叠加）

歌词文件 "某歌.txt"  ->  放进 lyrics_inbox/（插件数据目录下）
   │                     （/加歌 命令或插件加载时自动扫描）
   ├─ 解析: 去 LRC 时间轴与元数据标签行，歌名取文件名 / [ti:] / 首行
   ├─ 过滤: 含汉字少于 2 个的句子丢弃
   ├─ 补元数据: 文件写了用文件的，没写就反查基础库与 VCPedia 库
   ├─ 合并写入同目录的 user_songs.json（同名歌合并歌词）
   ├─ 立即并入内存词库（不用重载插件）
   └─ 归档: 成功 -> imported/，失败 -> failed/

/歌词 同步  ->  VCPedia（Anubis PoW 反爬）
   ├─ 解 PoW 挑战换 auth cookie（缓存约一周，失效自动重解）
   ├─ MediaWiki api.php list=categorymembers 全量分页枚举，支持递归子分类
   ├─ 词条取 wikitext，解析出演唱/P主/作词/作曲/编曲/混音/调教/母带/PV/曲绘/年份/简介/歌词
   ├─ 写入 data/vcpedia_songs.db（增量，已入库的跳过）
   └─ 同步完自动重建识别词库（也可随时用 /歌词 搜索 查询）
```

注入的 system 内容形如：

```
【歌词识别】用户最近在会话中发送了以下歌词原文：
- 「某句歌词」 出自《歌名》（演唱：洛天依，P主：某P，年份：2020，作词：A，作曲：B，编曲：C，调教：D，混音：E，PV：F，曲绘：G）
  前后歌词：上一句 ／ 上一句 ／ 「被识别的这一句」 ／ 下一句 ／ 下一句
用户可能在引歌词、玩歌词接龙或聊这首歌。请在回复中自然地运用这些歌曲信息……
```

- 命中行的**前后歌词**对接龙/续唱最有用——模型终于知道下一句是什么了
- 创作信息（年份/词/曲/编/调教/混音/PV/曲绘）来自歌曲库，库里没有的歌自动跳过
- 三类内容都有独立配置开关，见下方 `plugin.inject_*`；接龙用不到的信息可以关掉省 token

## 只在你主动要求时发言

插件平时只被动识别 + 注入上下文，不会自己发消息；bot 的回复仍由 MaiBot 主体生成，
只是"知道"了歌词背后的歌。唯一的例外是命令——发 `/歌词 ...` 或 `/加歌` 会收到一条回复。

## 数据

**只读素材**（随仓库分发，在插件目录 `assets/` 下；插件只会读它，不会写）

| 文件 | 说明 |
|---|---|
| `assets/knowledge_db.db` | 内置基础库，3412 首中V歌曲（歌名、P主、歌手） |
| `assets/song_lyric_keywords.txt` | 歌词句 -> 歌名 关键词表，57227 句（匹配源） |

**用户数据**（全部落在宿主分配给本插件的**插件数据目录**，即 `ctx.paths.data_dir`）

| 路径 | 说明 |
|---|---|
| `lyrics_inbox/` | 歌词文件收件箱，含 `imported/` `failed/` 两个归档子目录 |
| `user_songs.json` | 歌词文件收件箱导入的自定义歌单 |
| `vcpedia_songs.db` | VCPedia 同步下来的歌（SQLite，`songs` + `sync_meta` 表） |
| `anubis_cookies.txt` | 反爬 cookie，失效自动重解，可安全删除 |
| `recent_songs.json` | 最近命中过的歌（有界环形 30 条，v2.9.0 新增，供跨插件只读查询） |

> **为什么用户数据不在插件目录里**：整目录更新或重装插件会把 `assets/` 覆盖掉，
> 用户的歌单和收件箱会一起丢。所以这些路径全部由数据目录推导，插件源码目录保持只读。
> 实际路径可在 WebUI 插件详情页看到；本文档里 `lyrics_inbox/` 之类的相对路径都指它。
>
> 2.8.2 及以前放在 `assets/lyrics_inbox/`、`assets/user_songs.json` 的存量数据，
> 2.8.3 起在插件加载时**自动搬迁到数据目录**并打日志（数据目录已有同名内容时不覆盖，
> 搬不动只记 warning 不阻断加载），不需要手工处理。

`song_lyric_keywords.txt` 加载时会过滤含汉字少于 2 个的句子（纯数字/纯英文），
避免圆周率类歌曲的数字串误命中。

2.8.4 起还会过滤**日常聊天文本**，五条规则（只看清洗后的键）：

1. **单字重复 ≥ 4 次**（"哈哈哈哈…"“啦啦啦啦…”）：基础词库仅 26 个去重键，全是语气段；
2. **2~3 字片段重复 ≥ 3 次**（"对不起对不起对不起""我不想我不想我不想""hahahahahaha"）：
   这类句子恰是吵架/撒娇/笑声刷屏常用语；
3. **ASCII 低信息键**（"emmmmmmmm"）：整个键只由一两种字符构成；
4. **命中日常高频用语表**（约 390 条，精确匹配，不做子串）：问候/致谢/道歉/祝福/
   告别/情感/疑问/请求/情绪/日常/网络十一类通用口语，如「生日快乐」「新年快乐」
   「不好意思」「好久不见」「在吗在吗」「求求你了」「相信自己」「马上就好」。
   这些句子在歌词库里也有零星出处（`生日快乐` 出自《Come Back》、`不好意思`
   出自《鸽子》），但群聊里的出现率高几个数量级，宁可丢掉这几十句歌词的识别能力；
5. **片段整倍重复且片段在用语表内**（"我要回家"×4）：4 字以上整句重复多是歌曲
   专属歌词（`水煮包子`×3 保留），只有片段本身是日常用语时才算刷屏。

为什么不用「整句能否被常用词拼出」这类通用判定？实测那样会拦掉 800+ 键，
其中大半是真歌词（`你说你想`、`请听我说`、`请来找我`）——误伤率不可接受。
所以第 4 条只做**精确匹配**：子串匹配会误伤「我喜欢你的笑容」，而「我讨厌你」
被拦、「我讨厌的你」保留。

群聊里这类文本命中只会把歌曲上下文无端注进与歌无关的闲聊（真机实录：群友单纯
发 9 连哈，bot 被注入带着提了一嘴《躲在医院厕所雾化的二人》）。在真机曲库
（7672 首 / 24.2 万行歌词）上合计过滤约 300 个去重键（0.16%），逐条人工过目，
无一是辨识度歌词；4 字片段 2 连（"不能打架不能打架"）刻意保留——实词密度高、
日常复现率低。过滤器吞吐约 24 万行/秒，全量建索引仍在 2 秒内、且跑在后台线程。

旧位置（`assets/lyrics_inbox/`、`assets/user_songs.json`）仍保留在 `.gitignore` 里，
以防回滚到老版本时被误提交。

## 添加新歌：丢歌词文件进收件箱

**只需要一个歌词文件**，`.txt` 或 `.lrc` 都行，放进插件数据目录下的 `lyrics_inbox/`：

```
<插件数据目录>/
  └── lyrics_inbox/
      ├── 普通朋友.txt          <- 放这里
      ├── 千本樱.lrc            <- LRC 也行
      ├── imported/             <- 导入成功后自动归档到这里
      └── failed/               <- 导入失败的文件放这里，不会反复重试
```

找不到数据目录时，在 QQ 里发一次 `/加歌`——收件箱为空时插件会把**完整路径**回给你。

然后在 QQ 里发 **`/加歌`**（`/导入歌词`、`/扫描歌词` 同义），插件扫描收件箱、
把歌写进同目录的 `user_songs.json`，并回复导入结果：

```
歌词导入完成：成功 2 个，失败 0 个。
- 《普通朋友》 入库 32 句，歌名取自文件名
- 《千本樱》 入库 48 句，歌名取自LRC标签
当前自定义歌单共 2 首。
```

`plugin.auto_import_inbox` 默认为 `true`，所以**重载插件或重启 MaiBot 时也会自动
扫一遍收件箱**，不一定要用命令。导入的歌立即生效，不用再重载。

### `/歌词`、`/加歌` 没反应怎么办

命令发完什么都没发生，按顺序查：

1. **看日志里有没有 `命令执行成功: lyrics_xxx`** —— 没有说明命令压根没触发，
   跳到第 2、3 条；有说明触发了但消息没发出去，接着看
   `回复发送失败` 或 `命令载荷里没有 stream_id`。
2. **`[E_CAPABILITY_DENIED] … 未获授权能力: send.text`**
   —— manifest 的 `capabilities` 没写对。必须写点分的精确能力名 `send.text`，
   写 `send_message` 这种粗粒度名字无效。**改完要重启 MaiBot**（manifest 在插件
   加载前校验，热重载不生效）。
3. **重启 MaiBot** —— 新增的 `@Command` 组件要重新注册，热重载插件不一定生效。
4. **到 WebUI 的「Bot 配置 → 命令」里看 `lyrics_status` 等在不在列表里** ——
   1.2.0 起插件命令统一在这里管理（可配置放行用户/聊天流），没出现就是没注册上。

日志里出现命令执行成功但 `回复发送失败` 时，命令本身已经跑完（比如导入已完成，
会有 `歌词文件已入库: …`），只是结果没发出来；此时插件会退回让 bot 自己接一句话，
不会让你完全看不到反馈。

不依赖命令的退路：歌词文件放进 `lyrics_inbox/`（插件数据目录下）后**重载插件或重启
MaiBot**，加载时会自动导入，日志里会有 `歌词文件已入库: …`。

### 歌名怎么来的

按顺序取，取到为止：

1. **文件名**（去掉扩展名和 ` (1)` 这类副本后缀）——推荐，最省事
2. **LRC 的 `[ti:歌名]` 标签**
3. **文件第一个非空行**——该行会被当作歌名，不计入歌词

文件名是 `lyrics` / `歌词` / `新建文本文档` 这类通用名时，自动跳到第 2、3 条。

### 歌手和 P 主从哪来

按优先级，取到为止：

1. **LRC 标签** —— `[ar:歌手]` 填歌手，`[by:]` / `[au:]` / `[re:]` 填 P 主
2. **文件名约定** —— 见下节，库里没有的歌用这个手工指定
3. **从基础库反查** —— 歌名命中 `knowledge_db.db`（3412 首，歌手 3405 条、P主 3340 条）
   就自动补上缺失的字段

《珍珠》的例子：歌词文件里什么都不写，因为库里有这首歌，导入后自动变成
`演唱：洛天依，P主：洛天依官方账号`。

### 文件名约定（库里没有的歌）

改文件名就能指定歌手和 P 主，不用碰文件内容：

| 文件名 | 歌名 | 歌手 | P主 |
|---|---|---|---|
| `珍珠.txt` | 珍珠 | — | — |
| `珍珠 - 洛天依.txt` | 珍珠 | 洛天依 | — |
| `珍珠 - 洛天依 - 某P.txt` | 珍珠 | 洛天依 | 某P |
| `珍珠【某P】.txt` | 珍珠 | — | 某P |
| `珍珠 - 洛天依【某P】.txt` | 珍珠 | 洛天依 | 某P |
| `【洛天依】珍珠.txt` | 珍珠 | 洛天依 | — |
| `珍珠 - 洛天依、言和.txt` | 珍珠 | 洛天依、言和 | — |

规则：

- 分隔符 ` - `（半角减号**两侧都要空格**）或全角 `－` `—`（不要求空格）
- 括号 `【】` `[]` 标注 P 主；但括号在**最开头**时算歌手（中V 常见的 `【歌姬】歌名` 写法）
- **半角减号两侧无空格时不拆**，所以 `X-02.txt`、`光 -Hikari-.txt` 这类歌名不会被拆坏
- 如果完整文件名能在库里查到，就**完全不拆分**，直接用原名 + 库的元数据

规则细节：

- **只补空缺**，写了就不覆盖，库里有也不覆盖手填值
- `user_songs.json` 里留空的字段不会冲掉库里的信息（早期版本会，已修）
- 库里的歌手可能带换行（如 `言和\n洛天依`），注入时会压成「演唱：言和、洛天依」
- 库里没有的歌就留空，注入时不加空括号

想改已经导入过的歌，直接编辑数据目录下 `user_songs.json` 的 `singers` / `uploader` 字段。

### 歌词文件怎么处理

- 自动剥掉 LRC 时间轴（`[00:12.34]`，一行多个也能剥）
- 跳过 `[ti:]` `[ar:]` `[al:]` `[by:]` `[offset:]` 等元数据标签行
  （`[ar:]` / `[by:]` / `[au:]` / `[re:]` 的内容会取作歌手/P主，见上一节）
- 空行去掉，文件内重复行只留一句
- 含汉字少于 2 个的行（纯数字/纯英文）会被过滤，防止圆周率类歌曲误命中
- 编码自动识别：UTF-8（含 BOM）/ GBK / Big5
- 单个文件最多入库 `plugin.max_lines_per_song` 行（默认 2000）

同名歌不会重复添加，新歌词会**合并**进已有条目。

### 也可以直接编辑 `user_songs.json`（在插件数据目录下）

手改、批量改时用这个：

```json
[
  {
    "name": "歌名",
    "singers": "洛天依",
    "uploader": "某P",
    "lyrics": ["第一句歌词", "第二句歌词"]
  }
]
```

`lyrics` 也支持直接写字符串，用 `\n` 换行。

**生效方式**：插件在 `on_load` 时读取，所以在 MaiBot 里**重载插件**
（WebUI 关掉再打开）或重启 MaiBot 即可，无需重装。同名歌词句以自定义歌优先。

## 数据流向与隐私

**聊天内容不出本地。** 歌词识别全程在进程内做关键词匹配，不落盘、不上传。

| 方向 | 内容 | 去向与条件 |
|---|---|---|
| 入站 | 消息正文 | 只在本进程内存里匹配；不写文件、不发网络 |
| 出站 | 歌名 / 分类名 / 词条名 | 仅在你主动触发同步或使用查询工具时，请求 `crawler.base_url`（默认 <https://vcpedia.cn>，MediaWiki 公开站点）。请求里**不含**聊天记录、QQ 号、群号、图片 |
| 出站 | 歌名 + 歌词 + 固定 prompt | `emotion.annotate_on_sync` 与 `recommend_cv_song` 走宿主的 `llm.generate` 能力，由**你自己配置的模型服务商**处理；插件不直连任何模型 API、不持有 API key |
| 本地落盘 | 收件箱、自定义歌单、曲库、cookie 缓存 | **全部在宿主给的插件数据目录**（`ctx.paths.data_dir`）下；`anubis_cookies.txt` 只用于通过 VCPedia 的反爬校验，可随时删除 |

其余边界：

- **插件源码目录只读**：`assets/` 里只有随包分发的素材（曲库元数据、关键词表），
  插件不往那儿写任何用户数据——整目录更新/重装插件会覆盖那些文件。
  用户的收件箱与歌单统一落在插件数据目录，路径由 `ctx.paths.data_dir` 推导。
- 插件**不读宿主的数据库或日志**，也不读其它插件的文件。`sqlite3` 只用在自己数据目录下的运行时曲库、随仓库分发的只读素材库 `assets/knowledge_db.db`，以及你在 `plugin.extra_song_dbs` 里显式指定的外部曲库上——素材库与外部曲库一律以 `mode=ro` 只读打开。
- 不修改宿主的任何文件，也不改动其它插件或适配器的组件开关状态；配置读写交给宿主的配置管理层，插件不自己写回 `config.toml`。
- 仓库内不含任何凭据。离线批量标注脚本（`annotate_emotions.py`）的 key 从**环境变量**读取，`annotate_config.json` 已在 `.gitignore` 中；`config.toml` 由宿主运行时生成，同样不入库。
- `verify_ssl = false` 默认关闭（默认值 `true`）；只有自己搭了中间人代理、又不想配 `ca_bundle` 时才该打开，插件会在打开时打 warning 日志。详见「公司网络 / 安全软件导致证书错误」。

## VCPedia 同步

### 同步下来的数据怎么用

两处，自动的：

1. **扩充歌词识别词库** —— 插件启动时，以及**每次同步完成后**，都会读取
   `data/vcpedia_songs.db`，日志里出现
   `外部歌曲库 vcpedia_songs.db: 新增 N 首歌` /
   `歌词库: 同步后重建索引，新增 N 首进入识别词库`。
   之后这些新歌的歌词句也能被识别命中，歌手/P主自动带上，**不需要重启 MaiBot**。
   重建是幂等的：已有的歌跳过，只有新增的进索引（已有歌词的更新不会重索引）。
2. **直接查歌** —— `/歌词 搜索` `/歌词 歌曲`，或聊天里直接问（走 LLM 工具）。

规则：

- **基础库已有的歌名不重复索引** —— 它的歌词已经在 `song_lyric_keywords.txt` 里了，
  VCPedia 只补新歌。所以日志里的"新增 N 首"通常小于库里的总条目数，这是对的。
- VCPedia 的歌手/P主**不覆盖**基础库已有的值，只补空缺。
- 没同步过、库是空的，都只是跳过，不影响歌词识别。
- 词库索引从 v2.6.2 起在后台线程构建完成后一次性换入，插件加载不再阻塞事件循环；
  外部库的歌词也不全量常驻内存（见「性能与内存」一节）。

### 反爬与礼貌爬取

站点全站启用 **Anubis PoW 反爬**（明文请求含 `api.php` 一律 403），
插件内置解题逻辑与 cookie 缓存，无需手工配置。

实现上参考了 [mohobot](https://github.com/CarefreeSongs712/mohobot) 的
`music_knowledge/vcpedia.py`（MIT），并修正了它两处会失败的地方：

- `pass-challenge` 的 `response` 必须传真实 hash。mohobot 传固定值 `1`，
  在 Anubis 1.27 会返回 `invalid response.`
- `redir` 必须用重定向后的最终 URL。站点根路径会 301 到 `/首页`，
  challenge 是在 `/首页` 下发的，传初始 URL 会导致校验失败

请保持 `crawler.request_interval` 不要太小，别把站爬崩了。

### 换个分类，或接自己的歌库

`crawler.categories` 可以改，父分类会自动递归子分类：

```toml
[crawler]
categories = "Category:洛天依歌曲,Category:殿堂曲,Category:传说曲"
```

注意两件事：

1. **改完配置必须重启 MaiBot（或重载插件）**。WebUI 保存配置只写文件，不会推送给
   插件运行器——改完直接 `/歌词 同步` 跑的还是旧分类。同步开始的消息里会回显
   当前分类，跑之前对一眼。
2. **换分类是增量不是替换**。已有的歌不会删，新分类的歌追加进同一个
   `data/vcpedia_songs.db`。想同时保留多个歌手就写成逗号分隔的列表，别来回换。

`plugin.extra_song_dbs` 可以额外接别的 SQLite（多个用英文逗号分隔）。
库里只要有 `songs(name, singers, uploader, lyrics)` 四列就能用（多出的列忽略）：

```toml
[plugin]
# 相对路径按本插件的数据目录解析（即宿主给这个插件的目录）
extra_song_dbs = "我的歌库.db"
# 也可以直接写绝对路径
# extra_song_dbs = "D:/songlibs/我的歌库.db"
```

留空则只加载内置爬虫同步下来的库。相对路径不允许越出插件数据目录，
写成 `../../` 之类越界的会被拒绝并记日志。

## 配置（config.toml，Runner 自动生成）

| 键 | 默认 | 说明 |
|---|---|---|
| `plugin.enabled` | true | 是否启用 |
| `plugin.min_line_len` | 4 | 参与匹配的歌词句最短字数（过滤过短误报），也用于歌词文件导入 |
| `plugin.subspan_min_chars` | 6 | 子串匹配滑窗长度：整句未命中时按 N 字滑窗找连续共享片段，接得住「记岔开头/只记得半句」（0 = 关闭）。越小接得越短、误报越多；修改后自动整库重建词库 |
| `plugin.ttl_seconds` | 600 | 命中后多久内注入有效（秒） |
| `plugin.max_inject` | 3 | 单次注入最多携带的歌曲数（歌名去重） |
| `plugin.inject_context_lines` | 2 | 注入命中歌词的前后各几行（0 表示只注入命中的那一句） |
| `plugin.inject_basic_credits` | true | 注入年份与作词/作曲/编曲 |
| `plugin.inject_full_staff` | true | 注入调教/混音/PV/曲绘等完整 STAFF |
| `plugin.auto_import_inbox` | true | 插件加载时自动导入 `lyrics_inbox/` 里的歌词文件 |
| `plugin.max_lines_per_song` | 2000 | 单个歌词文件最多入库的行数 |
| `plugin.extra_song_dbs` | 空 | 额外的歌曲库（SQLite）路径（相对插件数据目录或绝对路径），留空只用内置爬虫同步下来的库 |
| `plugin.max_results` | 5 | 搜索歌曲时最多返回几条 |
| `plugin.detail_lyric_lines` | 30 | `/歌词 歌曲` 展示的歌词行数（0 = 不展示） |
| `plugin.lyric_preview_chars` | 120 | 工具返回歌词时的预览字数 |
| `crawler.base_url` | `https://vcpedia.cn` | VCPedia 站点根地址 |
| `crawler.categories` | `Category:洛天依歌曲` | 爬取分类，多个用英文逗号分隔 |
| `crawler.category_depth` | 2 | 子分类递归深度 |
| `crawler.request_interval` | 0.8 | 两次请求的最小间隔（秒），请保持礼貌爬取 |
| `crawler.timeout` | 20 | 单次请求超时（秒） |
| `crawler.max_fail` | 30 | 连续失败达到该次数时提前中止同步 |
| `crawler.sync_batch_limit` | 0 | 单次同步最多抓取多少首，`0` 表示不限 |
| `crawler.allow_sync_command` | true | 是否允许 `/歌词 同步` 触发同步 |
| `crawler.sync_admin_ids` | 空 | 允许触发 `/歌词 同步` `/歌词 补歌词` `/歌词 重抓` 的 QQ 号（英文逗号分隔）。**留空 = 谁都不能触发**，见「命令」一节 |
| `crawler.allow_local_operator` | true | 是否放行本机控制台（宿主标记 `is_local_operator` 的调用）；关掉后本机也要走上面的白名单 |
| `crawler.refill_cooldown_days` | 7 | `/歌词 补歌词` 跳过多少天内已确认无歌词的条目，`0` 表示每次都重抓 |
| `crawler.verify_ssl` | true | 是否校验 SSL 证书（见下） |
| `crawler.ca_bundle` | 空 | CA 证书文件路径（PEM），用于有 TLS 中间人的网络 |
| `recommend.recommend_enabled` | true | 是否启用 `recommend_cv_song` 推荐工具 |
| `recommend.target_singers` | 洛天依 | 目标歌手白名单（英文逗号分隔），推荐优先返回他们演唱的歌 |
| `recommend.known_virtual_singers` | 洛天依,言和,… | 已知虚拟歌手名单；候选歌歌手列表里出现「名单中非目标歌手」即被过滤 |
| `recommend.allow_unknown_singer` | true | 歌手字段为空的歌是否允许推荐（放行但标注「歌手未知」） |
| `recommend.recommend_count` | 3 | LLM 未指定数量时默认推荐几首 |
| `recommend.soft_exclude_size` | 10 | 最近推荐软排除窗口，窗口内不重复推荐（0 = 不去重） |
| `emotion.annotate_on_sync` | true | 同步完成后自动为待标注的歌打情绪标签（新歌优先） |
| `emotion.annotate_on_sync_limit` | 30 | 每轮同步后最多标注多少首（防打爆 LLM 配额） |
| `emotion.llm_task` | utils | 标注用的模型任务名/模型标识（utils / planner / replyer / 具体模型名）；留空走默认路由，若未配好会 fallback 向量模型报 400 |
| `emotion.annotate_timeout_ms` | 20000 | 标注单首歌的 LLM 超时（毫秒） |
| `emotion.annotate_budget_seconds` | 180 | 每轮标注总时间预算（秒），超时即停下轮继续 |
| `integration.play_tool_enabled` | true | 在规划器注入里提示可用的点歌工具（见下节） |
| `integration.play_tool_name` | `search_and_play_music` | 点歌工具名；留空则只给「可点播查询」串、不指定工具，便于换用其它点歌插件 |
| `integration.play_tool_hint` | 空 | 追加在点歌提示末尾的自定义说明（如本群点歌限额） |
| `integration.max_play_candidates` | 2 | 注入里最多给出几条可点播的「歌名 歌手」查询串 |

## 点歌联动（v2.7.0）

配合点歌插件 [github.cateye.music-request]（`search_and_play_music` 工具）使用。
链路是**经 LLM 规划器**的松耦合，不硬依赖：

```text
用户在群里贴歌词
  → 本插件识别出歌名/歌手
  → maisaka.planner.before_request 注入：歌名 + 「可点播查询：歌名 歌手」
  → 用户说「放一下」
  → 规划器调用 search_and_play_music(query="歌名 歌手")
  → 点歌插件把歌发到当前会话
```

为什么必须由本插件给出**「歌名 歌手」**：只给歌名时点歌插件容易搜到翻唱或同名曲；
歌词本身也不能直接当 query（搜不到）。所以注入里为前 `max_play_candidates` 首歌
各生成一条查询串，并明确告诉规划器「query 填这一整串，不要只填一句歌词、不要自己另猜歌名」。

行为约束（写进注入文案）：**只在用户明确想听时调用**，不主动放歌、不为同一首歌连续调用。
若不想参与联动，把 `integration.play_tool_enabled` 设为 `false` 即可，
注入会退回原来的形态（只提示 `recommend_cv_song` / `cv_song_search`）。

排障：

| 现象 | 处置 |
|---|---|
| 贴了歌词但 bot 不放歌 | 先确认注入是否命中：真机日志里应有 `已向规划器注入歌曲信息（prompt/items）` |
| 注入有了但仍不放歌 | 规划器可能没选中该工具。点歌工具默认在 deferred 池，注入文案已提示先 `tool_search` 检索；也可把 `integration.play_tool_name` 留空并依赖模型自行判断 |
| 播了但放错歌 | 检查歌曲库里的歌手字段是否为空（`/歌词 歌曲` 看「演唱」）；歌手为空时查询串只有歌名，命中率自然下降 |

## 氛围选歌（v2.5.0）

给曲库加了**情绪标签**层后，bot 可以根据聊天氛围主动推歌：

- 标签固定 7 个：**甜美、温柔、积极、帅气、搞怪、伤感、愤怒**（一首歌可多标签）。
- LLM 对话中判断用户想听歌/氛围合适时，会自己调用 `recommend_cv_song` 工具
  （传入情绪标签），工具内完成：标签匹配 → 随机抽取 → **最近推荐去重**
  （默认 10 首窗口内不重复）→ **歌手归属校验**（合唱、含其他虚拟歌手的歌
  不进推荐池；歌手字段为空的放行但标注「歌手未知」）。
- 歌词识别注入行为完全不受影响——校验只作用于推荐环节。

### 同步后自动标注（v2.6.0，默认开启）

`/歌词 同步` 的结果消息发出后，插件会**自动**给待标注的歌打情绪标签：

- **新歌优先**：按 id 倒序取队列，刚同步进来的歌先标，标完立刻能被 `recommend_cv_song` 推荐
- 每轮最多 `emotion.annotate_on_sync_limit`（默认 30）首，总预算 180 秒，超时剩下的下轮继续
- **任何失败都不影响同步**：单首失败只记 warning；连续 3 次失败熔断本轮，
  日志会直接给出三选一处置（配 model_config / 改 llm_task / 关开关）
- 同步回执之外会单独补一条「（顺带完成 N/M 首情绪标注）」
- 关闭：`emotion.annotate_on_sync = false`，热重载立即生效

**llm_task 怎么填**：默认 `utils`；若真机日志出现
`plugin.org.mai-mai.cv-lyric-context ... embedding ... 400 Field required: input.contents`，
说明该任务被路由到了向量模型——三选一：① model_config.toml 给任务
`plugin.org.mai-mai.cv-lyric-context` 配文本模型（根治）；② `emotion.llm_task` 改
`planner` / `replyer` 或具体模型名；③ 关掉 `annotate_on_sync`。

**与离线脚本的分工**：同步自动标注走**新歌优先**，负责让新歌即插即用；
离线脚本 `annotate_emotions.py` 走**从老到新**，负责慢慢排空历史存量（约 4000 首）。
两条路径写同一张表，互不冲突：标过的自动出队，失败的留在队列里下轮重试。

### 批量标注情绪标签（真机执行，一次性）

标注用独立脚本 `annotate_emotions.py`，**不进 MaiBot 运行时**，key 不落插件配置。

**最省事的方式：双击 `run_annotate.bat`**（不用开 PowerShell、不用敲命令）。
右键用记事本打开它，改这 4 行一次即可：

```bat
set "PYTHON=python"                                  :: python 不在 PATH 就填完整路径
set "API_KEY=PASTE_YOUR_API_KEY_HERE"                :: 你的 key
set "DB=D:\MaiBot\data\plugins\org.mai-mai.cv-lyric-context\vcpedia_songs.db"
set "BASE_URL=https://ark.cn-beijing.volces.com/api/v3"
set "MODEL=REPLACE_WITH_YOUR_MODEL_ID"               :: 方舟 Model ID
```

双击运行后先干跑 3 首（不写库），问你是否全量跑，回 `y` 就开始。
想提速就给 `set "EXTRA=--concurrency 4"`。

**曲库在 MaiBot 分配给插件的持久化目录**（默认 `data/plugins/<plugin_id>/vcpedia_songs.db`，
相对 MaiBot 根目录），不在插件源码目录的 `data/`——脚本必须用 `--db` 显式指向它
（或写进 `annotate_config.json` 的 `db` 字段，就不用每次带参数了）：

```powershell
# 0. 找到真机运行时曲库（PowerShell，MaiBot 根目录下）
Get-ChildItem D:\MaiBot\data -Recurse -Filter vcpedia_songs.db |
  Select-Object FullName, Length
# 认准大的那个（几 MB 级 = 有歌的；28KB = 空壳）

cd D:\MaiBot\plugins\cv_lyric_context
$DB = "D:\MaiBot\data\plugins\cv_lyric_context\vcpedia_songs.db"  # 按上面结果改

# 1. 配置 key（环境变量）
$env:MAIBOT_ANNOTATE_API_KEY = "sk-xxx"        # 或 OPENAI_API_KEY
# 端点/模型默认 OpenAI 官方 + deepseek-chat；自建/中转写 annotate_config.json：
#   {"base_url": "https://api.xxx.com/v1", "model": "deepseek-chat"}
# （该文件已 gitignore，不入库）

# 2. 先试 3 首看质量（--dry-run 只打印不写库）
python annotate_emotions.py --db $DB --limit 3 --dry-run

# 3. 全量跑：约 4000 首（有歌词的），1.5~2 小时
#    断点续跑：失败/中断的歌不写标签，下轮自动重试；Ctrl+C 随时安全退出
python annotate_emotions.py --db $DB

# 查看覆盖情况（标签分布统计）
python -c "import sys; from vcpedia_store import SongStore; print(SongStore(sys.argv[1]).emotion_stats())" $DB

# 某首标得不准？清掉重标（v2.8.0 起情绪存于标签表，用 CLI 而不是 UPDATE songs 裸 SQL）：
python migrate_knowledge_db.py $DB --clear-emotion "歌名"
# 手动打标签
python migrate_knowledge_db.py $DB --set-emotion "歌名" "温柔|积极"
```

标注依据是**整首歌的歌词**（超长歌词保留开头/结尾各 1500 字），
prompt 要求「不要因为单句歌词而改变整体判断」，temperature 0.2。

无歌词的条目不参与标注，也不会被推荐。建议跑标注时停掉 MaiBot，避免 SQLite 写锁偶发冲突
（就算冲突也不丢数据：失败的歌不写列，重跑自动补）。

### 公司网络 / 安全软件导致证书错误

同步报 `CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain`，
说明你的网络出口做了 TLS 中间人（代理或安全软件把站点证书换成了自签的）。两种解法：

**第一步：搞清楚是哪张证书**

在**跑 MaiBot 的那台机器**上打开 <https://vcpedia.cn> → 地址栏锁图标 →
「连接是安全的」→ 证书图标 →「详细信息」/「证书路径」，
看这条链**最顶层**那张是谁：

- 最顶层是系统内置的公共 CA（`DigiCert`、`ISRG Root X1` 之类）→ 链路正常，
  证书报错另有原因；
- 最顶层是代理 / 安全软件 / 公司网关的名字（`TestCorp Proxy Root CA` 之类）→
  链路里被插了一层，**那张就是你要找的根证书**。

不想开浏览器，可以用 openssl 反查签发者（Git Bash 自带）：

```bash
# 被中间人替换时，这里会显示代理 CA 的名字而不是公共 CA
openssl s_client -connect vcpedia.cn:443 -servername vcpedia.cn </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -issuer
```

**第二步：导出并填进配置**

1. `Win + R` → `certmgr.msc` 回车
2. 展开「受信任的根证书颁发机构」→「证书」
   （找不到就去「中间证书颁发机构」里再找一遍）
3. 按「颁发给」排序，搜第一步拿到的那个名字
4. 右键 → 所有任务 → 导出 → 选 **Base64 编码 X.509 (.CER)**
5. 存好后在 `config.toml` 里填：

```toml
[crawler]
ca_bundle = "D:/certs/proxy-root-ca.cer"
```

`.cer` 和 `.pem` 内容一样（都是 Base64 PEM），改不改后缀都行。

如果代理是本机软件，直接去它那儿拿更快：

| 软件 | 位置 |
|---|---|
| Fiddler | Tools → Options → HTTPS → Actions → Export Root Certificate to Desktop |
| Charles | Help → SSL Proxying → Save Charles Root Certificate |
| mitmproxy | `~/.mitmproxy/mitmproxy-ca-cert.pem` |
| Clash Verge | 设置里一般有「系统代理 CA」；找不到就用上面的 `openssl` 命令反查 |

**临时方案：关掉校验**

```toml
[crawler]
verify_ssl = false
```

这会跳过证书链校验，**存在被中间人窃听的风险**。只在确认那个代理是你自己的
（公司网关、本机安全软件）时才用，公网上不要开。插件关闭校验时会打一条 warning 日志。

> **默认行为**：`crawler.verify_ssl` 的默认值是 **`true`**——即**开箱即用就是完整证书链校验**，
> 不会静默降级。只有用户显式把它设为 `false` 才会走 `ssl.CERT_NONE` 分支，且那一刻
> 日志里必有 warning（`已关闭 SSL 证书校验（verify_ssl=false）…`）。优先用上面的
> `ca_bundle`（只追加信任代理根证书，系统根证书库仍然生效）。

`ca_bundle` 指向的文件不存在或格式非法时，会自动回退到系统证书并记日志，不会让插件起不来。

## 性能与内存（v2.6.2，v2.10.0 增补）

一轮针对常驻内存与事件循环阻塞的优化，行为完全不变
（新旧代码索引逐条对比一致：58139 句关键词 / 3412 首元数据）：

- **加载不再阻塞事件循环**：词库索引（解析 5.7MB 关键词文件 + 构建约 6 万条索引 +
  读两个 SQLite 库）改到后台线程构建，完成后一次性原子换入，同进程其他插件不再被卡。
- **歌词不再全量常驻**：只有真实多行（或单行可展示）的歌词进内存索引；
  基础库里无换行的整段 blob（超长、永远不可能命中）与 VCPedia 库的全量歌词
  不再常驻，改走 200 首上限的 LRU 按需查库。同步规模越大，省得越多。
- **推荐查询瘦身**：`recommend_cv_song` 的候选查询从 `SELECT *` 改为只取
  歌名/歌手/情绪/简介四列，并把标签过滤下推给 SQL，不再把全库歌词读进内存。
- **注入路径**：命中行定位从每行最多 3 次文本归一化降为 1 次（单趟扫描，全等优先）；
  歌曲创作信息查询加有界缓存，不再每首歌新开一次数据库连接。
- **其他**：同步入库复用单个连接（仍逐首提交，状态计数依旧实时）；
  Anubis PoW 求解复用前缀哈希；加载期汉字计数改为提前退出；
  关键词里的重复歌名字符串去重；禁用/卸载插件时释放全部核心索引。
- **子串索引开销（v2.10.0）**：滑窗 6 字时子串索引约 30 万个片段，
  构建耗时 +0.3s（实测 0.97s，基线约 0.7s），常驻内存 +40~60MB；
  归属歌数超过 3 的通用片段不登记，卸载/禁用时随索引一起释放。
  滑窗设为 0（关闭子串匹配）即回到 v2.9.0 的内存占用。

## 故障排查

绝大部分问题看一行日志就能定位，所以先查「日志速查」，再对症处理。
爬取侧的常见故障另外收在「同步失败怎么办」；
命令发出去没反应，见前面「`/歌词`、`/加歌` 没反应怎么办」一节。

### 日志速查

首次触发时各打印一行字段名诊断，日常运行打印命中与注入结果：

| 日志 | 含义 |
|---|---|
| `中V歌词识别已加载: N 句 / M 首 / 子串片段 K（滑窗 W 字，0=关闭）` | 插件已加载、词库就绪（含 v2.10.0 子串索引规模） |
| `[诊断] chat.receive.after_process 字段: [...]` | 入站 hook 的实际字段名 |
| `歌词命中: 「…」-> 《…》 (会话=…)` | 整句精确匹配成功 |
| `歌词命中（子串）: 「…」-> 《…》 (会话=…)` | 整句没记对，靠滑窗片段兜底命中（v2.10.0）；片段即「」里那截 |
| `[诊断] import_lyrics 字段: [...]` | `/加歌` 命令首次触发，列出载荷里的实际字段名 |
| `收到歌词导入命令: raw=… stream_id=…` | 命令已触发；`stream_id=<空>` 说明取不到会话，回复发不出去 |
| `歌词导入结果已回复: sent=True/False` | 结果是否发出去；False 时插件会退回让 bot 自己接话 |
| `命令载荷里没有 stream_id，无法回复结果` | 命令触发了但回不了话，把字段列表反馈给开发者 |
| `[诊断] before_model_request 字段: [...]` | 回复器注入 hook 的实际字段名 |
| `[诊断] planner.before_request 字段: [...]` | 规划器注入 hook 的实际字段名 |
| `已向 LLM 上下文注入歌曲信息（items）` | 回复器注入成功 |
| `已向规划器注入歌曲信息（prompt/messages/items）` | 规划器注入成功 |
| `歌词命中但请求载荷中没有 items/messages` | 回复器侧既无 items 也无 messages，把字段名反馈给开发者 |
| `歌词命中但 planner 请求载荷中没有 prompt/messages/items` | 规划器侧无可用载荷，把字段名反馈给开发者 |
| `歌词收件箱导入: 成功 N 个 / 失败 M 个` | 本次加载扫过收件箱的结果 |
| `歌词文件已入库: xxx.txt -> 《歌名》（新增 N 句）` | 某个歌词文件导入成功 |
| `歌词收件箱导入失败: …` | 扫描/写盘异常，看完整堆栈 |
| `歌词库已加载: 本地 N 首歌曲，上次同步 …` | 内置 VCPedia 库就绪 |
| `歌词库: 解析器自检通过（新版：…）` | 进程里装的是新解析器，未闭合 `<poem>` 的页面能解析 |
| `歌词库: 解析器自检失败——旧版：…` | WARNING。磁盘文件可能是新的，但进程里的模块还是旧的，**完整重启 MaiBot** |
| `外部歌曲库 vcpedia_songs.db: 新增 N 首歌` | 爬到的歌接进了识别词库 |
| `VCPedia: 同步失败: …` | 爬取异常，多为反爬拦截、限流或证书问题 |
| `hook 会话 … 无命中` 类情况 | 会话 ID 与消息侧不一致（本插件两侧都取 `session_id`，一般不会出现） |

### 同步失败怎么办

| 现象 | 原因与处理 |
|---|---|
| `CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain` | 网络出口有 TLS 中间人。配 `crawler.ca_bundle` 指到代理根证书，或临时 `verify_ssl = false`。详见上一节 |
| `pass-challenge 返回 403（invalid response.）` | 站点 Anubis 版本升级改变了校验方式，需对照前端 `main.mjs` 调整 |
| 同步全量失败、日志刷 403/超时 | 请求过密被临时限流。调大 `crawler.request_interval`，等一段时间再试 |
| 本地 `getaddrinfo failed` / 连不上站 | 网络环境的 DNS 拒绝解析该域名，换 DNS 或网络环境 |
| `未找到 Anubis challenge 脚本` | 站点可能已关掉反爬，或页面结构变了；看返回的状态码判断 |
| 库里没有某首歌 | 该曲不在配置的分类下；换 `crawler.categories` 或确认歌名 |
| 歌词为空 | 词条本身没有歌词章节（相声、纯音乐、器乐等非歌曲条目） |
| `/歌词 重抓` 回来「歌词 0 行」 | 先发「`/歌词 状态`」看解析器自检：不通过＝进程里是旧模块，完整重启 MaiBot；通过＝该页面结构特殊，用 `check_lyrics_parse.py <歌名>` 追踪解析步骤并贴输出 |
| 想抽查剩下那些空歌词条目 | `python list_empty_lyrics.py 30` 列出名字（会标出哪些还没确认过），挑几首跑 `python check_lyrics_parse.py <歌名>` 看是真的没歌词还是解析漏了 |
| 命令发完没反应 | 见「`/歌词` 没反应怎么办」 |

## 知识库结构（v2.8.0 起）

v2.8.0 之前，曲库是一张 21 列的宽表 `songs`：创作者被平铺成 `lyricist` /
`composer` / `arranger` / `mixer` / `tuner` / `mastering` / `pv` / `illustrator`
八个文本列，歌姬、分类、情绪各是一个分隔串。这种结构表达不了
「一人兼多角色」「一个角色多人」「角色未知」，也没法回答「这位 P 主还写过什么」。

现在改为规范化实体 + 一个同名兼容视图（思路参考中V档案馆 CVSA 的关系模型）：

| 表 | 作用 |
|---|---|
| `song` | 歌曲主体：名称、年份、时长、B站 aid/bvid、派生曲 `original_song_id`、软删除 |
| `credit_role` | 角色字典：UP主 / 作词 / 作曲 / 编曲 / 混音 / 调校 / 母带 / PV / 曲绘 |
| `artist` | 创作者档案（去重，带 `aliases` 别名） |
| `song_credit` | **(歌曲, 创作者, 角色)** 三元关系 —— 取代原来 8 个平铺列 |
| `singer` / `song_singer` | 歌姬档案与演唱关系；`engine` / `voicebank` 允许为空（只知道谁唱、不知道用什么声库是常态） |
| `lyrics` | 歌词多版本：`language` + `plain_text` + 预留 `ttml`（逐字时间轴）/ `lrc` + `is_translated` |
| `tag` / `song_tag` | 分类与情绪统一为标签（`kind` 区分，`position` 保序），分类支持 `parent_id` 成树 |
| `song_external_link` | 网易云 / B站 / VOCADB 等多平台外链 |
| `song_annotation` | 情绪标注时间戳 |
| `song_revision` | 元信息修订记录（歌名 / 年份 / 简介变更时可回溯） |

**兼容视图**：`songs` 这个名字仍然可用，列名与顺序与旧 21 列完全一致，
所以 `SELECT * FROM songs WHERE singers LIKE ?` 这类既有语句无需改动。
视图用相关标量子查询实现（不是 GROUP BY 派生表），这样 `WHERE id = ?` 能下推到
基表索引 —— 实测单行读取 0.5ms，比派生表写法快约 30 倍。

**视图只读**：写入一律走 `SongStore`。若让 `UPDATE songs` 反写关系表，重建
`song_singer` 时会把单独存放的 `engine` 抹掉，所以不提供写代理。

### 迁移

旧库在插件启动时会**自动迁移**（首次打开 `SongStore` 时完成），旧表改名为
`songs_legacy_v1` 保留以备回退。也可以手工执行，先预览再正式迁移：

```bash
cd <插件目录>

# 预览：复制到临时文件试跑，报告统计与逐字段校验，不改动原库
python migrate_knowledge_db.py data/vcpedia_songs.db

# 正式迁移：先做文件级备份，再迁移，并自动校验零丢失
python migrate_knowledge_db.py data/vcpedia_songs.db --apply

# 确认无误后回收旧表空间
python migrate_knowledge_db.py data/vcpedia_songs.db --apply --drop-legacy

# 体检：各表规模、各角色覆盖比例、情绪标签分布
python migrate_knowledge_db.py data/vcpedia_songs.db --stats
```

迁移已实测：3412 首旧库逐字段零丢失，耗时约 1.7s，库体积仅增约 4%。

> 真机部署时迁移会在首次启动自动发生。稳妥起见可先在真机执行一次
> `--apply`（会自动备份）再重启 MaiBot。

### 结构升级后新开的能力

除了原有方法，`SongStore` 新增了几个关系型查询：

| 方法 | 用途 |
|---|---|
| `credits_of(歌名)` | 取某首歌的分工：`{"lyricist": ["词作A"], "composer": ["曲作B"], ...}` |
| `singers_of(歌名)` | 取演唱者及其引擎/声库（未知为 `None`） |
| `songs_by_singer(歌姬)` | 反查某位歌姬唱过的歌 |
| `songs_by_artist(创作者, 角色)` | 反查某位创作者参与的歌，可限定角色 |
| `tags_of(歌名, kind)` | 取分类或情绪标签（保序） |
| `role_breakdown()` / `stats()` | 数据完整度体检 |

## 跨插件只读 API（v2.9.0 新增）

### `get_recent_songs` — 只读查询最近命中过的歌

```python
@API("get_recent_songs", version="1", public=True)
async def api_get_recent_songs(self, limit: int = 10) -> dict
```

**参数**：`limit`（默认 10，钳到 1..100，最新在前）。

**返回**（结构永远完整的纯 dict，任何情况下不抛异常）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | `int` | 恒为 `1` |
| `reason` | `str` | 空 = 数据正常；否则说明降级原因（见下） |
| `active` | `bool` | 插件已启用且 `on_load` 完成（记录已就绪） |
| `songs` | `list` | `[{"name": str, "artist": str, "at": float}]`，最新在前 |

**`reason` 的取值**：`""`（正常）/ `插件尚未完成启动或已停用（最近歌曲记录未就绪）`
（`active=false`，不要用它的数据）/ `暂无歌曲命中记录`。

**数据来源**：`recent_songs.json`——**歌词命中那一刻**登记一条（有界环形 30 条，
超出丢最旧），字段只有 `name / artist / at`。`artist` 取歌曲库 `singers` 列
（聚合的歌手/P主，形如 `某P主、某歌手`），库里查不到就是空串（**不猜**）。
写盘是**节流**的（首次立即落盘，此后默认 30s 窗口，卸载时强制落盘），
热路径上不做同步 IO；因此刚命中后的极短时间内，API 读到的可能是内存态中
尚未落盘、但**已经存在**的条目（API 读内存，不读文件）。

**与既有内存态的区别**：`_hits` 是**会话内 TTL**（注入上下文用，不落盘），
`_recent_recommends` 只记**最近推荐**（软排除用，不落盘）。本记录回答的是
"最近有人在群里聊到/点了哪首歌"，跨重启保留。

**纪律承诺（有测试守着）**：本 API **只读内存态**——零网络、零写盘、不触发落盘
（`tests/test_cross_plugin_api.py` 断言调用前后文件 mtime/内容不变、
内存记录不变、`dirty` 标记不被制造）。调用方可按任意频率轮询。

> **本版同时抬高 `sdk.min_version`：2.0.0 → 2.1.0**。理由：`@API` 组件在 SDK
> **2.1.0** 才引入（实测 2.0.0 / 2.0.1 的 `maibot_sdk` 没有 `API`，2.1.0 起可用）。
> 此前声明的 2.0.0 是**低估**——本版开始真正用 `@API`，按"声明要准"改为实际引入版本。

## 本地测试

```bash
# 1. pytest 套件（tests/ 下的 WebUI 配置显示 + 跨插件 API 契约用例，需要 pytest）
pytest -q

# 2. 知识库结构回归（103 项断言，纯标准库，不依赖 MaiBot SDK）
python test_vcpedia_schema.py

# 放到 MaiBot 插件工作区里时也可以指定其它副本
python test_vcpedia_schema.py /path/to/cv_lyric_context

# 3. LLM 调用参数回归（7 项断言，纯标准库）
python test_annotate_llm.py
```

覆盖：三种历史老库形态（7/18/21 列）的自动迁移、逐字段零丢失、公开 API 语义、
情绪标签排序、关系型查询，以及新版存储层独有能力（`upsert(conn=)` 连接复用、
`open_writer`、LIKE 通配符转义、情绪查询只返回 4 列）。

> **v2.9.0 门禁实测（2026-10-05）**：devkit `run_gates` PASS 2 / SKIP 0 / FAIL 0
> （`check_plugin` PASS 35 / WARN 4 / FAIL 0、pytest 24 passed）；三层自检
> pytest 24 + `test_vcpedia_schema.py` 103/103 + `test_annotate_llm.py` 7/7 全绿。
> 本仓库没有 `tests/smoke_test.py`（自检形态就是上面三条），插件中心自查器
> `check_submission.py` 报 1 个 FAIL：命中 `vcpedia_client.py` 的 `ssl.CERT_NONE`。
> 该代码是 v2.x 起就有的**用户显式开关**（`crawler.verify_ssl`，**默认 true 即默认开启校验**，
> 关闭时打 warning 日志），本版未改动它。
>
> **全检加固（2026-10-05）**：命中记录的降级不再静默——取歌手失败、写盘失败、
> 加载期坏文件被备份重建，三处都会打 warning（带歌名/路径/原因），
> 免得"API 里 songs/artist 一直为空"在真机上查不出线索。

> **根目录的两个 `test_*.py` 是脚本式自检，不是 pytest 套件。** 它们用 `check()`
> 记录失败而不 `assert`，被 pytest 收集会「断言全灭也显示通过」，所以用例函数
> 一律 `case_*` 命名（pytest 收集不到），`pytest.ini` 也把默认范围收紧到 `tests/`。
> 请按上面的命令用 `python` 跑，不要用 `pytest test_vcpedia_schema.py`。

> 注意：本仓库除 `test_vcpedia_schema.py` 外没有更上层的 `test_context.py`
> 全流程模拟测试——那个夹具在插件开发工作区里。改动核心逻辑（词库索引、
> 上下文注入、内存治理）后仍建议在开发工作区跑一轮全流程回归。

## 代码结构

**插件运行时**（MaiBot 只加载 `plugin.py`，其余由它 import）

| 文件 | 职责 |
|---|---|
| `plugin.py` | 入口：配置模型、生命周期、歌词识别与注入、`/加歌` |
| `lyrics_import.py` | 歌词文件收件箱的解析与导入（纯标准库，可单独测） |
| `vcpedia_mixin.py` | VCPedia 歌曲库能力：`/歌词` 系列命令 + 三个 LLM 工具（`search_vcpedia_song`、`get_vcpedia_lyrics`、`recommend_cv_song`） |
| `vcpedia_client.py` | Anubis PoW 解题 + MediaWiki API 取 wikitext |
| `vcpedia_sync.py` | 同步流程：分类枚举、词条解析、入库、熔断 |
| `vcpedia_wikitext_parser.py` | wikitext -> 结构化创作信息 |
| `vcpedia_text_clean.py` | wiki 标记清洗（`{{color}}`、`{{ruby}}`、`<ref>`、`[[链接\|文本]]`） |
| `vcpedia_schema.py` | 知识库结构：规范化实体表 + 角色字典 + `songs` 兼容视图 + 老库自动迁移 |
| `vcpedia_store.py` | 歌曲库 SQLite 读写（对外 API 不变，内部写规范化表；含关系型查询） |
| `assets/knowledge_db.db`<br>`assets/song_lyric_keywords.txt` | 内置歌曲元数据库与歌词关键词表（只读素材） |

**辅助脚本**（不注册任何组件、不参与插件运行，仅在排障或批量维护时手动执行；
放在仓库根目录是为了能直接 `import` 上面的运行时模块）

| 文件 | 职责 |
|---|---|
| `migrate_knowledge_db.py` | 库迁移与维护 CLI：预览 / 正式迁移 / 校验 / 回收旧表 / 改情绪标签 |
| `check_lyrics_parse.py` | 诊断单个词条的歌词解析过程：`python check_lyrics_parse.py <歌名>` |
| `list_empty_lyrics.py` | 列出库内「歌词为空」的条目，供人工抽查是「本来没歌词」还是「解析漏了」 |
| `repro_shanyaoluyuan.py` | 本地复现《山遥路远》那条解析管线的固定用例 |
| `test_vcpedia_schema.py` | 知识库结构回归自检（103 项断言，纯标准库，脚本式；用例函数 `case_*` 命名，不被 pytest 收集） |
| `test_annotate_llm.py` | LLM 调用参数回归自检（7 项断言，纯标准库，脚本式） |
| `pytest.ini` | pytest 配置：默认只收集 `tests/` 下的套件 |
| `singer_check.py` | 歌手归属校验纯函数（推荐池过滤用，可独立单测） |
| `recent_songs.py` | 最近命中歌曲的落盘记录（有界环形 + 节流原子写；纯标准库，可脱机单测） |
| `emotion_annotate.py` | 情绪标注纯函数件：prompt 构造 + 标签解析（离线脚本与插件内同步标注共用） |
| `annotate_emotions.py` | 离线批量标注情绪标签脚本（读环境变量里的 key，`--db` 指向运行时曲库，不走 MaiBot 运行时） |
| `run_annotate.bat` | Windows 双击启动器：把 key / 库路径 / 端点填好后调用上面的脚本 |

> 这些脚本都不接受来自聊天消息的输入，也不会被 `plugin.py` 调用。

`VCPediaMixin` 以多继承混入主类（`class CVLyricContextPlugin(VCPediaMixin, MaiBotPlugin)`），
实测 MaiBot SDK 能正确注册继承来的 `@Command` / `@Tool`，不用把代码复制进 `plugin.py`。

## 注意

- 若你的 MaiBot 版本中 `chat.receive.*` 系列 hook 不存在（老版本），插件会退回到
  `ON_MESSAGE` 事件；若该事件在版本里也未派发，则无法识别入站消息，需要换用对应
  版本的消息 hook（可看日志里打印的字段名确认）。
- 幂等保护：同一请求中若已注入过（内容含 `【歌词识别】` 标记），重试时不会重复叠加。
  规划器与回复器两条链路各自独立判重，互不影响。
- 注入覆盖两条链路：规划器（`maisaka.planner.before_request`，影响工具调用决策）
  与回复器（`maisaka.replyer.before_model_request`，影响最终回复）。若某条链路
  没有日志出现，说明该版本 MaiBot 未触发对应 hook 点。
- 同一会话内相同文本 10 秒内重复到达只登记一次，避免两套监听重复计数。
- 同步是增量且可中止的：已入库的歌会跳过，`/歌词 取消` 可随时停。

## 许可证

MIT。VCPedia 解析与清洗逻辑参考自
[mohobot](https://github.com/CarefreeSongs712/mohobot)（MIT License）。
