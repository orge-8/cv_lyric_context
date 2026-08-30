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
| 歌词文件 | 你自己放的歌 | 丢进 `assets/lyrics_inbox/`，`/加歌` |

## 快速开始

```bash
# 1. 把插件目录放到 MaiBot 的 plugins/ 下，重启 MaiBot
# 2. 在 QQ 里发（先小批量试跑，确认数据正常再跑全量）
/歌词 同步 100
# 3. 同步完会自动重建识别词库，新歌立刻可被识别，不用重启
# 4. 试试
/歌词 搜索 普通DISCO
/歌词 歌曲 普通DISCO
```

`Category:洛天依歌曲` 递归后约 4000 个词条，按默认 0.8 秒间隔全量同步约 1 小时。

## 命令

| 命令 | 说明 |
|---|---|
| `/歌词` | 显示用法 |
| `/歌词 状态` | 本地歌曲数量、上次同步时间、同步进度 |
| `/歌词 同步 [数量]` | 后台增量同步，可限定本次抓取数量（如 `/歌词 同步 100`） |
| `/歌词 取消` | 中止正在进行的同步 |
| `/歌词 搜索 <关键词>` | 按歌名 / 歌手 / P主搜索 |
| `/歌词 歌曲 <歌名>` | 查看歌曲详情与歌词 |
| `/加歌` | 导入 `lyrics_inbox/` 里的歌词文件 |

聊天里直接问也行，LLM 会自动调用 `search_vcpedia_song` / `get_vcpedia_lyrics`。

## 工作原理

```
用户消息 "某句歌词"
   │
   ├─ 监听入站消息 ──> 清洗文本（全半角/标点/大小写）
   │   新版: HookHandler chat.receive.after_process
   │   旧版: EventHandler ON_MESSAGE（回退）
   │                        在 57227 句关键词表中 O(1) 精确匹配
   │                        命中 -> 按会话登记 (时间戳, 歌词, 歌名)
   │
   └─ HookHandler (maisaka.replyer.before_model_request, BLOCKING)
                            LLM 请求前，把 TTL 内的命中整理成 system 内容注入：
                            · 新版运行时传 items  -> 追加 SystemMessageItem 快照
                              （Context Item schema v1）
                            · 旧版运行时传 messages -> 追加 {"role": "system"}
                            两条路径同时给出，运行时只读取自己认识的那个键

歌词文件 "某歌.txt"  ->  放进 assets/lyrics_inbox/
   │                     （/加歌 命令或插件加载时自动扫描）
   ├─ 解析: 去 LRC 时间轴与元数据标签行，歌名取文件名 / [ti:] / 首行
   ├─ 过滤: 含汉字少于 2 个的句子丢弃
   ├─ 补元数据: 文件写了用文件的，没写就反查基础库与 VCPedia 库
   ├─ 合并写入 assets/user_songs.json（同名歌合并歌词）
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
- 「某句歌词」 出自《歌名》（演唱：洛天依，P主：某P）
用户可能在引歌词、玩歌词接龙或聊这首歌。请在回复中自然地运用这些歌曲信息……
```

## 只在你主动要求时发言

插件平时只被动识别 + 注入上下文，不会自己发消息；bot 的回复仍由 MaiBot 主体生成，
只是"知道"了歌词背后的歌。唯一的例外是命令——发 `/歌词 ...` 或 `/加歌` 会收到一条回复。

## 数据

| 文件 | 说明 |
|---|---|
| `assets/knowledge_db.db` | 内置基础库，3412 首中V歌曲（歌名、P主、歌手） |
| `assets/song_lyric_keywords.txt` | 歌词句 -> 歌名 关键词表，57227 句（匹配源） |
| `assets/user_songs.json` | 歌词文件收件箱导入的歌 |
| `data/vcpedia_songs.db` | VCPedia 同步下来的歌（SQLite，`songs` + `sync_meta` 表） |
| `data/anubis_cookies.txt` | 反爬 cookie，失效自动重解，可安全删除 |

`song_lyric_keywords.txt` 加载时会过滤含汉字少于 2 个的句子（纯数字/纯英文），
避免圆周率类歌曲的数字串误命中。

`data/` 目录已在 `.gitignore` 中排除，不会入库。

## 添加新歌：丢歌词文件进收件箱

**只需要一个歌词文件**，`.txt` 或 `.lrc` 都行，放进 `assets/lyrics_inbox/`：

```
assets/lyrics_inbox/
  ├── 普通朋友.txt          <- 放这里
  ├── 千本樱.lrc            <- LRC 也行
  ├── imported/             <- 导入成功后自动归档到这里
  └── failed/               <- 导入失败的文件放这里，不会反复重试
```

然后在 QQ 里发 **`/加歌`**（`/导入歌词`、`/扫描歌词` 同义），插件扫描收件箱、
把歌写进 `assets/user_songs.json`，并回复导入结果：

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

不依赖命令的退路：歌词文件放进 `assets/lyrics_inbox/` 后**重载插件或重启 MaiBot**，
加载时会自动导入，日志里会有 `歌词文件已入库: …`。

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

想改已经导入过的歌，直接编辑 `assets/user_songs.json` 的 `singers` / `uploader` 字段。

### 歌词文件怎么处理

- 自动剥掉 LRC 时间轴（`[00:12.34]`，一行多个也能剥）
- 跳过 `[ti:]` `[ar:]` `[al:]` `[by:]` `[offset:]` 等元数据标签行
  （`[ar:]` / `[by:]` / `[au:]` / `[re:]` 的内容会取作歌手/P主，见上一节）
- 空行去掉，文件内重复行只留一句
- 含汉字少于 2 个的行（纯数字/纯英文）会被过滤，防止圆周率类歌曲误命中
- 编码自动识别：UTF-8（含 BOM）/ GBK / Big5
- 单个文件最多入库 `plugin.max_lines_per_song` 行（默认 2000）

同名歌不会重复添加，新歌词会**合并**进已有条目。

### 也可以直接编辑 `assets/user_songs.json`

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
- 实测 4000 首规模加载约 0.9 秒、内存约 31 MB，不会拖慢启动。

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

`plugin.extra_song_dbs` 可以额外接别的 SQLite（相对 MaiBot 根目录，多个用英文逗号分隔）。
库里只要有 `songs(name, singers, uploader, lyrics)` 四列就能用（多出的列忽略）：

```toml
[plugin]
extra_song_dbs = "data/我的歌库.db"
```

留空则只加载内置爬虫同步下来的库。手填的路径必须落在 MaiBot 根目录内，
写成 `../../` 之类越界的会被拒绝并记日志。

## 配置（config.toml，Runner 自动生成）

| 键 | 默认 | 说明 |
|---|---|---|
| `plugin.enabled` | true | 是否启用 |
| `plugin.min_line_len` | 4 | 参与匹配的歌词句最短字数（过滤过短误报），也用于歌词文件导入 |
| `plugin.ttl_seconds` | 600 | 命中后多久内注入有效（秒） |
| `plugin.max_inject` | 3 | 单次注入最多携带的歌曲数（歌名去重） |
| `plugin.auto_import_inbox` | true | 插件加载时自动导入 `lyrics_inbox/` 里的歌词文件 |
| `plugin.max_lines_per_song` | 2000 | 单个歌词文件最多入库的行数 |
| `plugin.extra_song_dbs` | 空 | 额外的歌曲库（SQLite）路径，留空只用内置爬虫同步下来的库 |
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
| `crawler.verify_ssl` | true | 是否校验 SSL 证书（见下） |
| `crawler.ca_bundle` | 空 | CA 证书文件路径（PEM），用于有 TLS 中间人的网络 |

### 公司网络 / 安全软件导致证书错误

同步报 `CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain`，
说明你的网络出口做了 TLS 中间人（代理或安全软件把站点证书换成了自签的）。两种解法：

**第一步：搞清楚是哪张证书**

在**跑 MaiBot 的那台机器**上执行（用 MaiBot 同一个 Python）：

```bash
python find_root_ca.py                    # 诊断 vcpedia.cn
python find_root_ca.py --host baidu.com   # 换别的站确认是不是全局现象
```

它会连一次站点（不校验证书）并打印：

```
目标站点 : vcpedia.cn:443
证书主体 : commonName=vcpedia.cn
证书签发者: commonName=TestCorp Proxy Root CA, organizationName=TestCorp

>>> 这张证书由另一张 CA 签发，说明链路里多了一层。
>>> 要找的是它的【签发者】那张证书。
```

那个**签发者**就是要找的证书名，脚本会把后面的导出步骤一并打印出来。

**第二步：导出并填进配置**

1. `Win + R` → `certmgr.msc` 回车
2. 展开「受信任的根证书颁发机构」→「证书」
   （找不到就去「中间证书颁发机构」里再找一遍）
3. 按「颁发给」排序，搜第一步拿到的签发者名字
4. 右键 → 所有任务 → 导出 → 选 **Base64 编码 X.509 (.CER)**
5. 存好后在 `config.toml` 里填：

```toml
[crawler]
ca_bundle = "C:/mai/proxy-root-ca.cer"
```

`.cer` 和 `.pem` 内容一样（都是 Base64 PEM），改不改后缀都行。

**用浏览器看也行**：在那台机器上打开 <https://vcpedia.cn> → 地址栏锁图标 →
「连接是安全的」→ 证书图标 →「详细信息」/「证书路径」，最顶层那张就是根，
可以「导出」或「复制到文件」。

如果代理是本机软件，直接去它那儿拿更快：

| 软件 | 位置 |
|---|---|
| Fiddler | Tools → Options → HTTPS → Actions → Export Root Certificate to Desktop |
| Charles | Help → SSL Proxying → Save Charles Root Certificate |
| mitmproxy | `~/.mitmproxy/mitmproxy-ca-cert.pem` |
| Clash Verge | 设置里一般有「系统代理 CA」；找不到就用 `find_root_ca.py` 反查 |

**临时方案：关掉校验**

```toml
[crawler]
verify_ssl = false
```

这这会跳过证书链校验，**存在被中间人窃听的风险**。只在确认那个代理是你自己的
（公司网关、本机安全软件）时才用，公网上不要开。插件关闭校验时会打一条 warning 日志。

`ca_bundle` 指向的文件不存在或格式非法时，会自动回退到系统证书并记日志，不会让插件起不来。

## 排障日志

首次触发时各打印一行字段名诊断，日常运行打印命中与注入结果：

| 日志 | 含义 |
|---|---|
| `中V歌词识别已加载: N 句 / M 首` | 插件已加载、词库就绪 |
| `[诊断] chat.receive.after_process 字段: [...]` | 入站 hook 的实际字段名 |
| `歌词命中: 「…」-> 《…》 (会话=…)` | 识别成功 |
| `[诊断] import_lyrics 字段: [...]` | `/加歌` 命令首次触发，列出载荷里的实际字段名 |
| `收到歌词导入命令: raw=… stream_id=…` | 命令已触发；`stream_id=<空>` 说明取不到会话，回复发不出去 |
| `歌词导入结果已回复: sent=True/False` | 结果是否发出去；False 时插件会退回让 bot 自己接话 |
| `命令载荷里没有 stream_id，无法回复结果` | 命令触发了但回不了话，把字段列表反馈给开发者 |
| `[诊断] before_model_request 字段: [...]` | 注入 hook 的实际字段名 |
| `已向 LLM 上下文注入歌曲信息（items）` | 注入成功 |
| `歌词命中但请求载荷中没有 items/messages` | 运行时既不给 items 也不给 messages，把字段名反馈给开发者 |
| `歌词收件箱导入: 成功 N 个 / 失败 M 个` | 本次加载扫过收件箱的结果 |
| `歌词文件已入库: xxx.txt -> 《歌名》（新增 N 句）` | 某个歌词文件导入成功 |
| `歌词收件箱导入失败: …` | 扫描/写盘异常，看完整堆栈 |
| `歌词库已加载: 本地 N 首歌曲，上次同步 …` | 内置 VCPedia 库就绪 |
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
| 命令发完没反应 | 见「`/歌词` 没反应怎么办」 |

## 本地测试

```bash
cd MaiBot插件开发
python check_plugin.py plugins/cv_lyric_context
python test_context.py           # 全流程模拟测试（117 项断言）
```

## 代码结构

| 文件 | 职责 |
|---|---|
| `plugin.py` | 入口：配置模型、生命周期、歌词识别与注入、`/加歌` |
| `lyrics_import.py` | 歌词文件收件箱的解析与导入（纯标准库，可单独测） |
| `vcpedia_mixin.py` | VCPedia 歌曲库能力：`/歌词` 系列命令 + 两个 LLM 工具 |
| `vcpedia_client.py` | Anubis PoW 解题 + MediaWiki API 取 wikitext |
| `vcpedia_sync.py` | 同步流程：分类枚举、词条解析、入库、熔断 |
| `vcpedia_wikitext_parser.py` | wikitext -> 结构化创作信息 |
| `vcpedia_text_clean.py` | wiki 标记清洗（`{{color}}`、`{{ruby}}`、`<ref>`、`[[链接\|文本]]`） |
| `vcpedia_store.py` | 歌曲库 SQLite 读写 |
| `find_root_ca.py` | 诊断脚本：有 TLS 中间人时，查出该信任哪张根证书（不是插件的一部分，单独跑） |

`VCPediaMixin` 以多继承混入主类（`class CVLyricContextPlugin(VCPediaMixin, MaiBotPlugin)`），
实测 MaiBot SDK 能正确注册继承来的 `@Command` / `@Tool`，不用把代码复制进 `plugin.py`。

## 注意

- 若你的 MaiBot 版本中 `chat.receive.*` 系列 hook 不存在（老版本），插件会退回到
  `ON_MESSAGE` 事件；若该事件在版本里也未派发，则无法识别入站消息，需要换用对应
  版本的消息 hook（可看日志里打印的字段名确认）。
- 幂等保护：同一请求中若已注入过（内容含 `【歌词识别】` 标记），重试时不会重复叠加。
- 同一会话内相同文本 10 秒内重复到达只登记一次，避免两套监听重复计数。
- 同步是增量且可中止的：已入库的歌会跳过，`/歌词 取消` 可随时停。

## 许可证

MIT。VCPedia 解析与清洗逻辑参考自
[mohobot](https://github.com/CarefreeSongs712/mohobot)（MIT License）。
