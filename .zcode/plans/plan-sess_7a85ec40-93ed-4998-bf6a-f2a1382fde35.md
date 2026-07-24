# 重写 `stream-bridge-deep-dive.md` 的 §3.4，使其与全文风格一致

## 问题

§3.4《调度器（scheduler）是不是订阅者？》目前只有 8 行：一个加粗结论 + 3 个 bullet + 1 句收尾。它和全文风格严重脱节——全文（§0 前置概念、§1 朴素写法、§3.3 IM 通道、§3.4.1 定时任务派发链路）的套路是：

- 带 `####` 子标题分场景
- 贴代码块，逐行用 `- **第 N 行** ...` 讲解
- 用比喻把抽象概念落地
- 末尾有"核心设计点回顾"
- ASCII 流向图

§3.4 一个都没有，读起来像占位符。

## 解决方案：把 §3.4 扩写成与 §3.3 / §3.4.1 同密度的"完成回调钩子"小专题

**纯文档改动，零代码变更。** 只编辑 `backend/docs/BACKEND_AGENT_DESIGN/stream-bridge-deep-dive.md` 一个文件，替换 §3.4 现有 8 行（行 833–841），扩写成一个完整的小节。§3.4.1（定时任务派发链路）保持原样不动。

## 扩写后的 §3.4 结构（对标 §3.3 / §3.4.1 的密度）

扩写后的小节包含以下子标题，每个都贴代码 + 逐行讲解：

1. **结论先行 + 为什么这个问题值得单独澄清**（保留"不是订阅者"的结论，但补一段动机：解释它和 §3.3 的"两类订阅者"结论怎么对上，以及为什么"调度器靠回调而不是订阅"是反直觉的、值得讲透）。

2. **`####` 先看清"完成回调钩子"长什么样：从 worker 到调度器的三跳链路**
   - 贴 `worker.py:710-714` 的调用代码 + 逐行讲解（`finally` 块位置、`is not None` 守卫、`try/except` 吞异常为 non-fatal、传整个 `record`）。
   - 给一张 ASCII 数据流图：`RunRecord finalized → ctx.on_run_completed(record) → handle_run_completion(record)`，明确"这是函数调用，不是消息总线"。

3. **`####` 钩子是怎么接上去的：依赖注入 + 单一赋值点（逐行讲 deps.py:423）**
   - 贴 `deps.py:406-424` 的 `get_run_context`，逐行讲那条密集的三元表达式：双重 `getattr` 守卫、绑定的是**方法引用**（`handle_run_completion`）而非 lambda、调度服务未配置时落 `None`。
   - 补一句"`RunContext` 是 `frozen=True`，构造后不可改 → 钩子必须在构造时注入"，呼应 §3.4.1 阶段 3 的依赖注入主题。
   - 点明 `app.state.scheduled_task_service` 这个单例在 `app.py:256-269` 启动时挂上。

4. **`####` 调度器收到回调干了什么：handle_run_completion 逐行讲（service.py:252-301）**
   - 贴 `handle_run_completion` 全文，逐行讲解：从 `record.metadata` 读 `scheduled_task_id` / `scheduled_task_run_id` / `user_id`、**早退守卫**（非定时任务的 run 直接 return——这就是"钩子全局接、但只对定时任务生效"的机制）、`RunStatus → task_run 终态`的三路映射（success/interrupted/failed，强调 interrupted≠failed）、写 `scheduled_task_runs` 行 + 对 `once` 类型父任务收尾（completed/cancelled/failed）。
   - 强调 metadata 是"反向回写的钥匙"——呼应 §3.4.1 阶段 3 `dispatch_task` 塞进去的那两个 key。

5. **`####` 为什么"回调"而不是"订阅"：两种机制的本质对比**
   - 一张对比表（维度：数据流向、是否需要事件流、跨进程要求、失序/竞态处理），把 `bridge.subscribe`（拉模式、事件流、跨 HTTP）和 `on_run_completed`（推模式、单次函数调用、进程内）并排摆。
   - 用比喻落地："订阅像订报纸（每期都送、可补订历史）；回调像快递签收回执（只回一句'货到了'，不关心沿途）。调度器只要'货到了'这个信号，不要每份报纸。"

6. **`####` 回到 fan-out 的结论：订阅者到底有谁**
   - 重申结论：订阅者只有两类（浏览器/客户端 + 流式 IM 通道），调度器不在其中。但补上"调度器是 run 的**生产者之一**（它经 §3.4.1 派发链路把 run 启起来），却不是 run 事件流的**消费者**"——把"生产者"和"订阅者"两个角色分清，消除最后的混淆点。
   - 收尾呼应 §3.4.1：定时任务的完整生命周期 = §3.4.1 的派发链路（启动）+ §3.4 的回调钩子（收尾），两者一起构成调度账本 ↔ agent run 的双向绑定。

## 文中要落到实处的准确性要点（来自代码核实）

- 调用点在 `worker.py:710-714`，确在 `finally:`（始于 `worker.py:630`）内、`bridge.publish_end(run_id)`（718）之前。
- `on_run_completed` 是 `RunContext`（`worker.py:130-145`，`frozen=True`）的字段，类型 `Any | None`，**单个回调，不是列表**。
- 唯一赋值点 `deps.py:423`；绑定 `scheduled_task_service.handle_run_completion`（bound method）。
- `handle_run_completion` 对非定时任务 run 早退 return，所以钩子虽全局接，只对定时任务生效。
- `once` 任务在回调里收尾到 completed/cancelled/failed；周期任务只更新 `last_error`，继续按计划跑（对应 §3.4.1 已讲过的 task_status 取值）。

## 不改的东西

- §3.4.1 整节（定时任务派发链路）原样保留。
- 全文其它部分、锚点、行号引用不动（§3.4 行号引用 `worker.py:710-712`、`deps.py:423` 都已核实准确，保留）。
- 末尾收尾句"fan-out 的订阅者只有两类……"保留其含义但融入新结构。

## 风格自检（提交前对照）

扩写完逐项核对：有 `####` 子标题？✓ / 贴了代码块？✓ / 逐行讲解？✓ / 有比喻？✓ / 有对比表？✓ / 有 ASCII 图？✓ / 结尾呼应全文？✓。确保不再出现"一句话 + bullet"的简略写法。