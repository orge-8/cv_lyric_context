# 中V歌词识别 · 上下文注入 (cv_lyric_context)

中文 VOCALOID（洛天依、言和、乐正绫等）歌词识别插件：入站消息命中歌词时，
向 MaiBot 的 LLM 上下文注入歌曲信息，让 bot 能自然接住歌词话题。

## 工作原理

```
用户消息 "某句歌词"
   │
   ├─ EventHandler (ON_MESSAGE) ──> 清洗文本（全半角/标点/大小写）
   │                                  在 58498 句关键词表中 O(1) 精确匹配
   │                                  命中 -> 按会话登记 (时间戳, 歌词, 歌名)
   │
   └─ HookHandler (maisaka.replyer.before_model_request, BLOCKING)
                                      LLM 请求前，把 TTL 内的命中整理成
                                      system 消息追加进 messages
                                      （modified_kwargs 覆盖，不改原列表）
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
- `assets/song_lyric_keywords.txt`：59638 条歌词句 -> 歌名 关键词表（匹配源）

## 配置（config.toml，Runner 自动生成）

| 键 | 默认 | 说明 |
|---|---|---|
| `plugin.enabled` | true | 是否启用 |
| `plugin.min_line_len` | 4 | 参与匹配的歌词句最短字数（过滤过短误报） |
| `plugin.ttl_seconds` | 600 | 命中后多久内注入有效（秒） |
| `plugin.max_inject` | 3 | 单次注入最多携带的歌曲数（歌名去重） |

## 本地测试

```bash
cd MaiBot插件开发
python check_plugin.py plugins/cv_lyric_context
python test_context.py           # 全流程模拟测试（17 项断言）
```

## 注意

- `maisaka.replyer.before_model_request` 的 kwargs 字段名以运行时为准
  （可用 WebUI `/plugins/runtime/hooks` 查中心表）；插件已对
  `session_id / chat_id / stream_id` 做兜底，messages 兼容
  `list[dict]` 与带 `role/content` 属性的对象。
