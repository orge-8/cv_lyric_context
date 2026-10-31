# 决策记录：歌词注入的角色可辨识性（v2.15.0 回归）

格式：结论 + 理由。新问题列在文末「待定」。拷打强度 4（深挖），2026-10-31 起。

---

## 背景事实（2026-10-31，代码核对 + 真机 replyer 思考原文，非推测）

**代码基线**：`cv_lyric_context` version `2.15.0`，`git log` 最新 `868914b`（docs），
行为提交 `2a431d6`「feat: v2.15.0 注入位置改为末尾追加，修复换歌导致的 prompt cache 全量失效」。
参考插件 `life-frequency` version `1.25.3`。

**触发材料**：用户在真机贴出 replyer 的 thinking 原文，要点四句——

1. `User says lyric recognition block with lyric "这样下去的话" and instruct natural reply.`
2. `The last visible person said @鸣澜 晚安, and then weird user "晚安岚，别把菠萝包啃太晚" perhaps planner generated…`
3. `Then current prompt contains current time and extra reply requirements`
4. 随后用三段篇幅纠结「下一句到底是『所有人都会得救』还是『精神上的疲惫感很重』」

**机制链（逐句对回代码）**

| # | 事实 | 位置 |
|---|---|---|
| F1 | tail 模式追加的是一条 `UserMessageItem` / `{"role":"user"}`，且位于请求**最末尾** | `plugin.py:2166-2173` |
| F2 | 注入文本以祈使句收尾：「请在回复中自然地运用这些歌曲信息…」 | `plugin.py:2005-2007` |
| F3 | 文本开头是第三人称叙述：「用户最近在会话中发送了以下歌词原文」——由 user 角色发出时语义自指紊乱 | `plugin.py:1999` |
| F4 | 宿主 replyer 的 items 末尾本就并排多条 `UserMessageItem`（`reply_act` / 当前时间与目标消息 / 风格要求），插件注释已记录 | `plugin.py:2160-2163` |
| F5 | 末尾同类 item 达 4~5 条，插件未对注入 item 做任何可辨识标注 | `plugin.py:2124-2133` |
| F6 | `_lyric_window()` 按**整行**加「」高亮，而用户常只发行内片段；thinking 第 4 句的纠结即由此而来 | `plugin.py:1906-1941` |

**参考插件带来的关键反证（2026-10-31 读码）**

| # | 事实 | 位置 |
|---|---|---|
| F7 | `life-frequency` 的**回复侧注入只用 `extra_prompt`**，从不自拼 item；该字段由宿主折进「回复要求」，属宿主自建的、模型认得的脚手架 | `life-frequency/plugin.py:7640-7702` |
| F8 | 两个插件用的不是同一个 replyer hook：cv 用 `maisaka.replyer.before_model_request`（拿最终载荷），life-frequency 用 `maisaka.replyer.before_request`（拿 extra_prompt） | `plugin.py:2318` vs `life-frequency/plugin.py:7641` |
| F9 | `life-frequency` 对 **planner** 才追加 `SystemMessageItem`，且带定界 marker（`[proactive-no-quote]`）+ 幂等检查；该做法注明来自 group-welcome v1.3.0 的真机验证 | `life-frequency/plugin.py:806-809, 870-918` |

**结论性判断（已可确证）**

- thinking 第 1/3 句是 F1+F2+F3 的必然结果；第 2 句说明模型已在「辨认末尾这一堆 user item」，角色语义已塌；第 4 句是 F6 的独立缺陷。
- **F7 推翻了原先「缓存 vs 正确性必须二选一」的假设**：宿主本来就有一条既在尾部、又带宿主自己的框语义的通道（`extra_prompt`）。cv 是绕开它自建了一条无标识 user 消息，属自造回归，不是无解的取舍。

---

## 决策 1｜本次工单拆成「正确性回归」与「缓存优化」两条独立线

- **结论：先按 PATCH 拿回正确性，v2.15.0 的缓存目标作为独立课题重新设计，不再由同一个开关承担。**

**理由**：两个问题的量级不对等。缓存损失是**成本项**（按百分比计费），
误认消息是**功能项**（bot 答非所问、甚至开始替用户接歌词）。成本优化不能以功能错乱为代价，
且把两者绑在 `inject_position` 一个字段上，等于每次调缓存都在赌正确性。

---

## 决策 2｜回复侧改用宿主原生 `extra_prompt`，插件不再改写 items / messages

- **结论：方案 C 落地。回复侧 hook 从 `before_model_request` 换到 `before_request`，
  注入只写 `extra_prompt`，`items` / `messages` 一个字节都不动。**

**理由**：

1. **位置与语义可以同时满足**。`extra_prompt` 由宿主折进「回复要求」那条 item，
   本来就在请求尾部 → 前缀缓存收益与 v2.15.0 完全相同；同时它的框语义由宿主提供，
   模型认得那是脚手架而不是用户发言 → 误认消息的根因消失。v2.15.0 是在宿主已提供
   原生通道的情况下自建了一条无标识 user 消息，属自造回归。
2. **有真机背书**。`life-frequency` v1.25.3 用同一条通道跑长期验证，连「摘要头会粘在
   别人提示词最后一行上」（已修）这种细节都踩过了。照抄一个跑通的范式，比自创一个
   未验证的载体风险低一个量级。
3. **hook 选择有官方字段表做依据，不靠猜**（`docs/03 Hook 系统与消息网关.md`）：

   | hook | 字段 |
   |---|---|
   | `maisaka.replyer.before_request` | session_id, request_type, task_name, model_name, **extra_prompt**, attempt, … |
   | `maisaka.replyer.before_model_request` | items, item_schema_version, session_id, …（**无 extra_prompt**） |

   即「同 hook 改字段」不可行，必须换 hook。原以为的风险点（`before_request` 可能取不到
   `session_id`）同样被字段表否掉：它有 `session_id`。
4. **共享通道必须守预算**。`extra_prompt` 是公共资源，超预算时截断自己的尾巴而不是
   放弃注入（丢的是 STAFF 尾部，不是整条识别结果），更不能挤掉别的插件。

**代价与约束（明确记录）**：

- 注入文本长度受 `_MAX_EXTRA_PROMPT_CHARS = 1500` 限制，极端配置（`max_inject=10` +
  `inject_context_lines=5` + 全 STAFF）会被截断；日志里的 `N/M 字` 是唯一线索。
- `plugin.inject_position` 从此只影响规划器，配置语义变窄（已同步 README 与字段说明）。

## 决策 3｜planner 侧的角色问题不在本次修

- **结论：本次只修 replyer。planner 继续自拼 `UserMessageItem`，登记为下一版候选。**

**理由**：planner 只做工具决策、不产出正文，误认的可见后果远低于 replyer；而 planner
侧的正确载体（`SystemMessageItem` + marker，`life-frequency` / `group-welcome` 的真机做法）
会改变规划器请求的消息序列落点，缓存影响未验证。同一版里改两处位置语义，出问题无法归因。

---

## 方案对比（Q1 定案依据，留档）

| 方案 | 载体 | 缓存 | 角色可辨识 | 成本 |
|---|---|---|---|---|
| A 回退 system | 并入首条 `SystemMessageItem` | ✗ 保持 17.5% 失效 | ✓ | 改默认值，1 行 |
| B 回退 + 改措辞 | 同上 | ✗ | ✓+ | 小 |
| **C tail 换宿主通道（已采纳）** | `extra_prompt`（`before_request`） | ✓ | ✓✓（宿主框语义） | 中：换 hook，需真机验证 |
| D tail 但改 SystemMessageItem | items 尾部追加 system | ？待验证落点 | ✓ | 中 |
| E 现状 | 自拼 `UserMessageItem` | ✓ | ✗ **事故** | — |

---

## 待定

- ~~Q1｜终局取 A/B/C/D 哪一个？~~ → **已定：C**（决策 2）
- Q2｜planner 侧的 `SystemMessageItem` 尾注，真机上落在消息序列的哪个位置？—— 决定 D 是否真的缓存友好。
- ~~Q3｜`before_model_request` 的载荷里有没有 `extra_prompt`？~~ → **已答：没有**（官方 hook 字段表）
- Q4｜v2.14.x（system 模式）时代是否出现过同类「误认消息」？—— 决定问题在位置还是在措辞。
- Q5｜真机当前装的是 2.15.0 还是更早？—— **已答：2.15.0**，故本次为 hotfix。
- Q6｜「前后歌词」是否继续给 replyer？命中粒度如何标（F6）—— 已知未修，见决策 3 后的遗留项。
- Q7（新增，需真机验证）｜2.15.1 上线后 replyer thinking 是否还会提到「lyric recognition block」？
  这是本次修复的唯一验收标准，比日志里的「已注入」更有说服力。
