# 中V歌词识别 · 上下文注入 (cv_lyric_context)

中文 VOCALOID（洛天依、言和、乐正绫等）歌词识别插件：入站消息命中歌词时，
向 MaiBot 的 LLM 上下文注入歌曲信息，让 bot 能自然接住歌词话题。

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
```

注入的 system 内容形如：

```
【歌词识别】用户最近在会话中发送了以下歌词原文：
- 「某句歌词」 出自《歌名》（歌手、UP主）
用户可能在引歌词、玩歌词接龙或聊这首歌。请在回复中自然地运用这些歌曲信息……
```

## 无命令、无主动发言

插件只被动识别 + 注入上下文，不会自己发消息；bot 的回复仍由 MaiBot 主体生成，
只是"知道"了歌词背后的歌。

## 数据

- `assets/knowledge_db.db`：3412 首中V歌曲（歌名、UP主、歌手，用于注入时补元数据）
- `assets/song_lyric_keywords.txt`：歌词句 -> 歌名 关键词表（匹配源）。
  加载时会过滤含汉字少于 2 个的句子（纯数字/纯英文），避免圆周率类歌曲的
  数字串误命中，实际入库 57227 句。

## 配置（config.toml，Runner 自动生成）

| 键 | 默认 | 说明 |
|---|---|---|
| `plugin.enabled` | true | 是否启用 |
| `plugin.min_line_len` | 4 | 参与匹配的歌词句最短字数（过滤过短误报） |
| `plugin.ttl_seconds` | 600 | 命中后多久内注入有效（秒） |
| `plugin.max_inject` | 3 | 单次注入最多携带的歌曲数（歌名去重） |

## 排障日志

首次触发时各打印一行字段名诊断，日常运行打印命中与注入结果：

| 日志 | 含义 |
|---|---|
| `中V歌词识别已加载: N 句 / M 首` | 插件已加载、词库就绪 |
| `[诊断] chat.receive.after_process 字段: [...]` | 入站 hook 的实际字段名 |
| `歌词命中: 「…」-> 《…》 (会话=…)` | 识别成功 |
| `[诊断] before_model_request 字段: [...]` | 注入 hook 的实际字段名 |
| `已向 LLM 上下文注入歌曲信息（items）` | 注入成功 |
| `歌词命中但请求载荷中没有 items/messages` | 运行时既不给 items 也不给 messages，把字段名反馈给开发者 |
| `hook 会话 … 无命中` 类情况 | 会话 ID 与消息侧不一致（本插件两侧都取 `session_id`，一般不会出现） |

## 本地测试

```bash
cd MaiBot插件开发
python check_plugin.py plugins/cv_lyric_context
python test_context.py           # 全流程模拟测试（30 项断言）
```

## 注意

- 若你的 MaiBot 版本中 `chat.receive.*` 系列 hook 不存在（老版本），插件会退回到
  `ON_MESSAGE` 事件；若该事件在版本里也未派发，则无法识别入站消息，需要换用对应
  版本的消息 hook（可看日志里打印的字段名确认）。
- 幂等保护：同一请求中若已注入过（内容含 `【歌词识别】` 标记），重试时不会重复叠加。
- 同一会话内相同文本 10 秒内重复到达只登记一次，避免两套监听重复计数。
