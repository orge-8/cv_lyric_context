# 决策记录：CVSA（中V档案馆）数据源接入

格式：结论 + 理由。新问题列在文末「待定」。

## 背景事实（2026-10-27 实测，非推测）

对象仓库 `project-cvsa/cvsa`（AGPL-3.0，Elysia + Prisma 8 + Bun + Turbo monorepo），
自称「Chinese Vocal Synthesizer Archive」= 中V档案馆。

**最近 PR（全部 alikia2x 提交，集中在 2026-10-26）**

| PR | 标题 | 目标分支 | 规模 |
|---|---|---|---|
| #73 | feat(crawler): discovery pipeline + admin console + deploy setup | develop | 24 commits / 475 files / +69,773 −7,435 |
| #76 | fix(snapshot-db): Prisma 8 init migration + legacy snapshot 迁移 | develop | 16 files / +2,136 −217 |
| #74 / #77 | develop → main 发布合并 | main | — |
| #64 | docs SEO | main | 仍 open，停滞 4 个月 |

**线上实例 `https://api.projectcvsa.com`，version 1.1.0，OpenAPI 在 `/openapi/json`（18 path）**

| 端点 | 鉴权 | 实测 |
|---|---|---|
| `GET /search/result?query=<歌名>` | 免鉴权 | 模糊搜索 + `rank`；参数名必须是 `query`，传 `q` 直接 400 validation |
| `GET /song/{id}/info` | 免鉴权 | name / aid / cover / duration / producer / publishedAt |
| `GET /song/{id}/eta` | 免鉴权 | views / speed / updatedAt |
| `GET /song/{id}/snapshots` | 免鉴权 | 时间序列快照 |
| `/song/{id}/lyric`、`/lyrics` | — | **404，线上无歌词端点** |
| `/videos/unlabelled` 等 | 需鉴权 | `{"message":"Authentication required."}` |

其他实测数据：曲库 id 至少到 50000（>5 万条记录），但大量记录 `name: null`；
20 首中V名曲模糊搜索命中 **15/20**（普通DISCO / 权御天下 / 达拉崩吧 rank=1.0）。

---

## 决策 1｜目标锁定为「补元数据」，不是「换歌词数据源」

- **结论：CVSA 只用于补充歌曲元数据**，VCPedia 抓词链路保留不动。
  指望它替掉 `vcpedia_client.py` 的 Anubis PoW ……这条路当前不成立。

**理由**：CVSA **线上根本没有歌词端点**（`/song/{id}/lyric` 与 `/lyrics` 均实测 404），
而歌词是本插件的核心资产（`vcpedia_songs.db` 7464 首、覆盖率 99.4%）。
仓库 develop 分支虽有 catalog + lyrics CRUD（`GET /song/:id/lyric/:lyricId`），
但尚未部署到线上 API 契约里。以现状判断，CVSA 唯一能稳定提供的增量是：
B 站 aid、封面、时长、P主（producer）、发布时间、播放量/增速。

**顺带记录的对照**：这套元数据恰好是 VCPedia wikitext 里结构化成本高或不稳定的部分；
同时新路是美国 HTTPS JSON API，天然规避两处现存痛点 —— ①Anubis 1.27 PoW（difficulty 4~6，
本机最多夹到 6）②本机 TLS MITM（`proxy-root-ca.cer`）导致的 `CERTIFICATE_VERIFY_FAILED`。
但这两条属于「换掉 VCPedia」的收益，**只有在 lyrics 上线后才兑现**，现在不能计入。

## 决策 2｜架构取「弱依赖 + 本地缓存表」

- **结论：CVSA 只在同步阶段被异步调用，结果落本地 SQLite；命中/查询路径保持完全离线。**

**理由**：

1. 插件的核心卖点是「歌词命中 → 立即注入 planner/replyer」，这条路径不能被外部服务
   的 RTT 拖慢。CVSA 拿一首歌的完整信息需要 2~3 次请求（search → info → eta）。
2. CVSA 目前是单实例裸 API，**无 SLA、无限流承诺、无版本号稳定性保证**，
   且刚经历 migration 事故（#76）。强依赖等于把插件可用性押在它身上。
3. 本地缓存顺带解决速率限制与冷启动；CVSA 挂掉时降级就是「元数据缺失」，
   不影响歌词识别主链路。

## 决策 3｜本轮不动手，观望 catalog/lyrics 上线节奏

- **结论：不进 v2.14.x 任何代码，不改 `_manifest.json`，不 bump 版本。**
  仅保留本决策文档作为后续接线的依据。

**理由**：见下方「动手前必须先过的闸」—— 在配对率未被验证前，写代码可能是在给一个
接不上的数据源做适配。观望成本极低（昼夜十八次 PR 都在 develop 上，线上滞后），
提前动手的返工风险却不低。

---

## 动手前必须先过的闸（重要）

- **配对率 ≥ 80% 的 POC**：CVSA 有 >5 万条记录但大量 `name: null`。
  必须先用本地 3412 首关键词库 / 7464 首曲库去打 `/search/result`，
  统计「命中比例 + rank 分布 + producer 字段非空率」。
  **低于 80% 则整个方案不成立**，不应进入正式版本。
  这条是本决策目前唯一悬空的前提。

## 观望清单（下次复查时，其中任一项发生变化即重开本议题）

1. CVSA 线上 OpenAPI 出现 **`/song/{id}/lyric`（或 lyrics 相关 path）** → 决策 1 的前提改写，
   可以重新评估「替换 VCPedia」。
2. 线上 `version` 从 **1.1.0 上跳**（说明 develop 的 catalog 已发布）→ 元数据质量应同步重测。
3. 出现 **公开的数据授权说明**（现在 license 只有仓库 AGPL-3.0，数据本身授权未明示）→ 解除法律侧阻塞。
4. `search` 的稳定性：出现限流响应码 / `elapsedMs` 明显劣化 → 重新评估调用预算。

## 已知风险（记录在案，未解决）

- **许可**：CVSA 仓库是 **AGPL-3.0**，本插件是 MIT。走 HTTP 调 JSON API 一般不触发 AGPL
  传染（非衍生作品），但**搬运其 crawler / ONNX 分类器代码会触发**。数据授权未见明文。
- **契约未 GA**：#73 刚重写全部 handler 并引入 Prisma 8 鉴权与 crawler 控制台，
  路径与字段仍可能变化；届时适配层需要在六个品类上重做。
- **运营性质**：第三方社区项目，无商业 SLA；`name: null` 说明存在大量未补全数据。

---

## 待定

- POC 由谁跑、什么时候跑（决策 3 说暂缓，但未排期）。
- 本地缓存表落在哪个库里：`vcpedia_songs.db` 加关联表，还是新建 `cvsa_meta.db`。
- 是否需要在插件配置里预留 `cvsa.base_url` / `cvsa.enabled` 两个键（现在不填，
  但要确认将来加键不会破坏已有配置）。
- 除该 REST API 之外是否还存在别的官方出口（例如 docs.projectcvsa.com —— 本机 DNS 不可达，
  尚未核实），动手前需先补齐。
