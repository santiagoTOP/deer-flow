# `run_agent` 深度讲解：为什么不能直接在路由里 `async for chunk in agent.astream(input): yield chunk` 就完事

本文是 `stream-bridge-deep-dive.md` 和 `run-manager-deep-dive.md` 的姊妹篇，三部曲的第三篇。

那两篇各管一头：

- `stream-bridge-deep-dive.md` 管**事件流分发**——agent 产出的事件凭什么能被任意数量、任意时刻、跨进程的 HTTP 订阅者消费。
- `run-manager-deep-dive.md` 管**状态 + 生命周期**——一个 run 凭什么能活过创建它的那次 HTTP 请求、能跨重启可查、能被干净取消和收尸。

这两篇合起来，回答的是"**事件**和**状态**怎么脱离请求存活"。但还差一个最关键的没讲：**那个真正驱动 agent graph 跑起来、把 chunk 一批批喂给 Bridge、并在最后一锤定音"这次 run 到底算成功还是算失败"的函数**——它就是本文的主角 `run_agent`，定义在 `backend/packages/harness/deerflow/runtime/runs/worker.py:246-719`。

三者在同一行代码里汇合（`backend/app/gateway/services.py:746-761`）：

```python
task = asyncio.create_task(
    run_agent(
        bridge,               # ← stream-bridge 篇的主角（事件往哪儿喂）
        run_mgr,              # ← run-manager 篇的主角（状态归谁管）
        record,               # ← 这条 run 的身份证
        ctx=run_ctx,          # ← 基础设施（checkpointer/store/...）
        agent_factory=...,    # ← agent 怎么造
        graph_input=...,      # ← 第一轮喂给 graph 的输入（用户消息）
        config=...,           # ← langgraph 的 RunnableConfig
        ...
    )
)
record.task = task            # ★ 把 task 钉回 record 上（run-manager 篇麻烦 1）
```

`create_task` 把 `run_agent` 派到后台跑（run-manager 篇 §0.3 讲过 `create_task` 的代价）；`record.task = task` 这一行把它钉回 RunManager 的注册表（run-manager 篇麻烦 1 讲过"三级钉住，run 再也不会失联"）。这两件事姊妹篇已经讲透。**本篇要讲的是被派到后台的那个函数本身——它里面究竟干了什么。**

本文面向第一次接触这套架构的同学。先把要用的概念铺一遍（很多复用姊妹篇结论，带 ※ 标记），然后给一段朴素写法当反面教材，再展开 6 个麻烦，每段代码逐行解释。

---

## 第 0 步：先把要用的术语讲清楚

### 0.1 ※ run / thread / checkpoint

概念基础和 `run-manager-deep-dive.md` §0.1、§0.7 完全重叠，本篇复用结论：

- **run**：一次 agent 执行，有 `run_id`、归属某个 `thread_id`、有自己的状态。
- **thread**：一段对话。同一个 thread 下可以有多条 run（按时间排列），但同一时刻最多一条活动 run（run-manager 篇麻烦 3 的并发守卫）。
- **checkpoint**：agent graph 在某一刻的完整状态快照，存在 checkpointer 里。agent 每走一步（每个 node 执行完）都会写一个新 checkpoint。thread 的对话历史、当前消息列表、各种 channel 值，全都装在最新的那个 checkpoint 里。

本篇要补的一个视角：**checkpoint 是 agent 的"存档"。** 这一点是后面麻烦 2（rollback）、麻烦 3（goal 续跑）的全部基础——agent 之所以能"回到跑之前的状态"、之所以能"接着上一轮继续跑"，都是因为 checkpoint 能读、能写、能对比。

### 0.2 ※ StreamBridge —— 事件的"邮局"

概念基础和 `stream-bridge-deep-dive.md` 重叠。这里只补一个视角：**在 `run_agent` 眼里，Bridge 是个"邮局窗口"**——它只管把事件往窗口一塞（`bridge.publish`），不关心有几个订阅者、订阅者是谁、在哪台机器上。它的三个方法（`stream_bridge/base.py`）：

- `publish(run_id, event, data)`（`base.py:42-44`）：塞一个事件进窗口。
- `publish_end(run_id)`（`base.py:46-48`）：**告诉邮局"这个 run 的事件到此为止了"**——订阅者收到这个信号就知道流结束了，可以收摊。
- `cleanup(run_id, delay=0)`（`base.py:65-71`）：过了 `delay` 秒后，把这个 run 在邮局里的存档清掉。

`run_agent` 是**生产者那一头**——它要保证：① 每个有意义的 chunk 都 publish；② **不管怎么收场（成功、失败、被取消），最后一定 publish_end**，否则订阅者会傻等（stream-bridge 篇麻烦 4 讲过这个坑）。这条"一定 publish_end"的铁律是本文麻烦 6 的命门。

### 0.3 ※ RunManager / RunRecord / abort_event

概念基础和 `run-manager-deep-dive.md` §0.4、§0.5、麻烦 1 重叠。复用结论：

- **RunManager** 是进程级单例，管着一张 `_runs` 字典（run_id → RunRecord）。`run_agent` 接到的 `run_manager` 参数就是这个单例。
- **RunRecord** 是这条 run 的"身份证 + 状态本"（run-manager 篇麻烦 1 贴过完整字段）。`run_agent` 接到的 `record` 参数就是它。
- **`record.abort_event`** 是一个 `asyncio.Event`（铃铛，run-manager 篇 §0.4 讲过）。用户点"停止"时，RunManager 按这个铃（`abort_event.set()`），`run_agent` 在循环里**主动检查**这个铃，发现按了就干净收尾。

本篇要反复用到 RunRecord 上的几个字段：`status`（状态）、`abort_event`（取消铃）、`abort_action`（取消时走 interrupt 还是 rollback）、`model_name`、`created_at`/`updated_at`、`finalizing`（是否正在做取消后清理）。

### 0.4 `agent.astream(stream_mode=...)`——LangGraph 的流式 API

这是 `run_agent` 最核心的调用。`agent` 是个 langgraph graph 对象，`astream` 是它的异步流式方法——喂一个 input 进去，它会一边执行 graph 一边把中间产物（chunk）一个个 `yield` 出来。`stream_mode` 参数决定 yield 出来的 chunk 长什么样。

合法的 mode 定义在本文件顶部（`worker.py:98`）：

```python
_VALID_LG_MODES = {"values", "updates", "checkpoints", "tasks", "debug", "messages", "custom"}
```

最常用的两个：

- **`values`**：每个 chunk 是"当前 graph 的完整状态快照"。一个 chunk 就是整个 `{"messages": [...], "title": ..., ...}` 的全集。LangGraph 每走完一个 node，就 yield 一次最新的完整状态。⚠️ 注意：`values` 模式会**重放整个消息历史**——这一点是麻烦 5"历史去重"的坑根。
- **`messages`**：每个 chunk 是 LLM 正在生成的一条消息的增量片段（token-by-token 流式输出）。前端打字机效果靠它。

一个关键细节（`run_agent` 专门为此写了两个分支）：**`astream` 的返回值结构取决于你传了几个 mode**。

- **单个 mode、不带 subgraphs**（`worker.py:492-505`）：`async for chunk in agent.astream(..., stream_mode="values")` 直接 yield **裸 chunk**。
- **多个 mode、或带 subgraphs**（`worker.py:506-525`）：`async for item in agent.astream(..., stream_mode=["values","messages"], subgraphs=True)` yield 的是 **`(mode, chunk)` 元组**（带 subgraphs 时甚至是三元组 `(ns, mode, chunk)`）。

这就是为什么 `_stream_once` 里要有个 `_unpack_stream_item`（`worker.py:1552-1575`）来拆包——两种调用形式吐的东西结构不一样。

⚠️ 还有个 mode 叫 `events`，它**不是 `astream` 的合法 mode**（要用得换成另一个 API `astream_events`，但那玩意儿没法同时产 `values` 快照）。所以 `run_agent` 见到请求里有 `events` 直接跳过（`worker.py:291-295`、`455-457`）。

### 0.5 graph state / channel_values —— checkpoint 的内部结构

一个 checkpoint 的核心是一个叫 `checkpoint` 的字典，里面有个 `channel_values` 字段，装着 graph 的"频道值"——agent 的所有状态都挂在频道上：`messages` 频道是消息列表、`title` 频道是对话标题、`goal` 频道是用户定的目标……每个 channel 还有个版本号，记在 `channel_versions` 里。

为什么要知道这个？因为麻烦 2 的 **rollback** 物理上就是这么实现的：把跑之前存的那个旧 checkpoint 的 `channel_values` 原样写回去（其实是以"新 checkpoint"的形式覆盖），thread 就回到了跑之前的样子。麻烦 6 的标题回写、duration 持久化，也都是直接往 `channel_values` 里塞东西。详见 `_rollback_to_pre_run_checkpoint`（`worker.py:1058-1148`）和 `_ensure_interrupted_title`（`worker.py:1322-1401`）。

### 0.6 协作式取消：主动检查铃铛 vs 被动接异常

`run_agent` 有**两条取消路径**，理解它俩的分工是麻烦 4 的关键。

**主动路径——检查 `abort_event`。** RunManager 按铃（`record.abort_event.set()`）只是"挂号"，不会立刻打断 agent。`run_agent` 在**流循环的每一次迭代**开头检查铃铛（`worker.py:497`、`513`）：

```python
async for chunk in agent.astream(...):
    if record.abort_event.is_set():        # ★ 主动检查铃铛
        break                               # 干净地退出循环
```

为什么主动检查比被动接异常好？因为主动检查发生在**两个 chunk 之间的安全间隙**——一个 chunk 已经处理完、下一个还没开始，这时退出不会留下半截状态。而被动取消（下面）可能在任意时刻打断，运气不好就打在 checkpoint 写一半的当口（stream-bridge 篇 §0.5 讲过这个坑）。

**被动路径——`except asyncio.CancelledError`。** 如果进程退出（run-manager 篇麻烦 6 的 `shutdown`）、或 RunManager 直接 `task.cancel()`，asyncio 会往协程里抛 `CancelledError`。`run_agent` 在 `worker.py:596` 专门接住它，做和主动路径**一模一样**的收尾（rollback 或 interrupt）。这是兜底——主动路径是常态，被动路径是"万一主动检查还没来得及执行就被硬切了"。

### 0.7 goal / goal continuation —— agent 的"自我追问"

DeerFlow 有个"目标"机制：用户给 thread 定一个目标（比如"帮我查清楚 X 并写份报告"），agent 跑完用户那一轮后，**自己判断这个目标做没做完**。如果没做完，它会**再追加一轮隐藏的续跑**——用一个专门的 evaluator 模型评估，发现目标还没满足，就合成一条"继续推进"的内部消息，再跑一轮。如此循环，直到目标满足、或撞到续跑次数上限、或用户喊停。

这个机制在 `run_agent` 里就是 `worker.py:535-550` 的那个 `while` 循环：

```python
await _stream_once(graph_input, ...)        # ① 先跑用户那一轮（可见）
while ...:                                  # ② 再循环跑隐藏的 goal 续跑轮
    continuation_input = await _prepare_goal_continuation_input(...)
    if continuation_input is None: break
    await _stream_once(continuation_input, ...)
```

续跑是**对用户隐藏的**（续跑的输入是 agent 自己合成的内部消息，不是用户新发的），但走的是同一套 `_stream_once`、产出的事件照样往 Bridge 喂。判断"要不要续跑"的逻辑很复杂（`_prepare_goal_continuation_input`，`worker.py:866-1055`，有大量竞态守护），本文麻烦 3 只讲它和 `run_agent` 的协作关系，不深挖 evaluator 内部。

### 0.8 middlewares 能"硬停" run —— stop_reason 的来源

DeerFlow 的 agent 跑在一串 middleware 链上（backend/AGENTS.md 有详细讲）。其中有几类是**守卫型 middleware**，会在 agent 执行过程中监测异常并**强制结束这一轮**：

- `loop_detection`：发现 agent 在原地打转（反复调同一个工具）→ 截断。
- `token_budget`：发现这一轮 token 超预算 → 截断。
- `safety_finish_reason`：发现 agent 要执行被禁的工具/不安全操作 → 截断。
- `subagent_limit`：发现子 agent 数量超限 → 截断。

它们的截断方式很特别：**不是抛异常，而是悄悄 strip 掉 LLM 回复里的 `tool_calls`**——agent 看到"没有工具要调了"，自然就结束了，从 graph 外部看就像"正常结束"。但它们会往 `runtime.context["stop_reason"]` 这个键上盖个章（`loop_capped` / `token_capped` / `safety_capped` / `subagent_limit_capped`）留个记号。

`run_agent` 在收尾时读这个记号（`worker.py:579-593`）：run 看着像正常 `success`，但 `stop_reason` 非空，就说明是被 middleware 提前掐的——这个信息会落到 run record 上，前端能显示"被循环检测截断"之类的原因。⚠️ 这就是为什么"success"不等于"agent 跑到底了"——下面的对比表格会专门区分。

---

好，前置概念讲完了。现在看反面教材。

## 第 1 步：朴素写法长什么样

如果一个刚接触 langgraph 的人要实现"跑 agent 并把结果流式返回"，他大概率会写出这样（**这是反面教材，真实代码不是这样**）：

```python
async def stream_run_naive(thread_id, input_text, request):
    agent = build_agent(thread_id)
    config = {"configurable": {"thread_id": thread_id}}
    async for chunk in agent.astream(
        {"messages": [{"role": "user", "content": input_text}]},
        config=config,
        stream_mode="values",
    ):
        yield serialize(chunk)          # 直接 yield 给 HTTP 响应
```

逐行：

- **第 1 行**定义一个异步生成器函数（有 `yield`），它直接挂在 HTTP 路由上，返回一个 SSE 流。
- **第 2 行**构造一个 agent。
- **第 3 行**配好 `thread_id`（让 checkpointer 知道存到哪）。
- **第 4-9 行**直接 `async for` 消费 `agent.astream(...)`，每来一个 chunk 就 `yield` 出去，顺着 HTTP 响应流给前端。

看着挺干净，对吧？但这 6 行假设了一件事：**这次 run 的生命周期 = 这次 HTTP 请求的生命周期。** 整个 agent 执行都焊死在这个生成器函数的栈帧里，请求一断、栈帧一销毁，run 就没了。从这个错误前提出发，会派生出 6 个麻烦：

- **麻烦 1**：事件直接 yield 给 HTTP 响应——但 run 要活在后台（被取消、被查询、跨请求 join），事件得有一个独立于请求的出口。朴素写法把流和请求焊死，请求一断流就断。
- **麻烦 2**：用户想"打断重来"（rollback 回到跑之前）——但朴素写法根本没存"跑之前的状态"，无处可回。
- **麻烦 3**：agent 要会"自我追问"（goal 续跑多轮）——朴素写法只跑一轮 `astream` 就完事，没有续跑循环。
- **麻烦 4**：中途喊停——朴素写法没有任何取消检查点，`astream` 一旦跑起来就只能等它自己结束。
- **麻烦 5**：LLM 挂了（model-call 失败）但 graph 会把它包装成一条"普通错误消息"继续流——朴素写法没人检查，run 就被误标成 `success`。
- **麻烦 6**：不管怎么收场（成功/失败/取消），事件流都得有个明确的终止符、状态都得落库——朴素写法没 `finally`，异常一路冒泡，流就挂在那儿没尾。

下面逐一展开 `run_agent` 怎么解决这 6 个麻烦。

---

## 第 2 步：6 个麻烦逐一展开

### 麻烦 1：run 要活得比 HTTP 请求久，事件得走 Bridge 而不是 HTTP 响应

#### 场景

用户发了消息，后端不是"跑完再返回"，而是派一个后台任务跑 agent（run-manager 篇 §0.1 讲过的"订单"模型）。这次 run 要：

- 在后台慢慢跑（可能几十秒），期间用户可以刷新页面、另开标签页、关掉浏览器再重连。
- 它产出的 chunk（token 流、状态快照）要能被**任意数量、任意时刻**的 HTTP 订阅者消费——不只是当初创建 run 的那次请求。

也就是说，agent 产出的事件，目的地不是"那次 HTTP 请求"，而是一个**独立于请求的中转站**。run-manager 篇讲过 run 怎么脱离请求存活；stream-bridge 篇讲过这个中转站（Bridge）怎么工作。本篇讲的是：**`run_agent` 作为生产者，怎么把 chunk 正确地喂给这个中转站。**

#### 朴素写法为什么做不到

回看第 1 步：朴素写法 `yield serialize(chunk)` 把 chunk 直接吐给 HTTP 响应。这意味着 chunk 的唯一消费者就是"当前这次 HTTP 请求"。请求一断（用户关页面、网络抖动），`async for` 的消费端没了，生成器也就停了——agent 没跑完也被迫中止。更别说"另开一个标签页 join 这个 run 的流"了，根本不可能。

#### `run_agent` 怎么解决的：`bridge.publish` + 后台 task

`run_agent` 一上来就 publish 一个 metadata 事件（`worker.py:350-357`），告诉订阅者"这个 run 开张了"：

```python
# 2. Publish metadata — useStream needs both run_id AND thread_id
await bridge.publish(
    run_id,
    "metadata",
    {
        "run_id": run_id,
        "thread_id": thread_id,
    },
)
```

逐行：

- `await bridge.publish(run_id, "metadata", {...})` 把一个 `event="metadata"` 的事件塞进 Bridge 的窗口（0.2 讲过，Bridge 是邮局）。注意目的地是 **`run_id`**，不是某个 HTTP 连接——任何订阅了这个 `run_id` 的请求都能收到。这正是和朴素写法的根本区别：**chunk 的目的地是 run，不是请求。**
- 事件体里同时带上 `run_id` 和 `thread_id`——前端 `useStream` hook 靠这两个字段把流和 UI 绑定起来。

真正的 chunk 流式产出在 `_stream_once`（`worker.py:490-525`）里。看它的单 mode 分支（`worker.py:492-505`）：

```python
async def _stream_once(input_payload: Any, stream_config: RunnableConfig) -> None:
    nonlocal llm_error_fallback_message
    async with _checkpoint_thread_lock(thread_id):
        if len(lg_modes) == 1 and not stream_subgraphs:
            single_mode = lg_modes[0]
            async for chunk in agent.astream(input_payload, config=stream_config, stream_mode=single_mode):
                if record.abort_event.is_set():            # ★ 麻烦 4 讲
                    break
                await bridge.publish(run_id, sse_event, serialize(chunk, mode=single_mode))   # ★ 喂给 Bridge
                ...
```

逐行讲关键的：

- `async with _checkpoint_thread_lock(thread_id):` 进一个**按 thread 串行化的锁**（`worker.py:79-94`），保证同一个 thread 的 checkpoint 写入不会并发。这个锁是 per-thread 的，不会阻塞别的 thread——目的是 trouble 3 的多任务策略打底（run-manager 篇麻烦 3 讲过"同一 thread 同时只能一个活动 run"，这个锁是同一道防线的内存层）。
- `async for chunk in agent.astream(...)` 就是 0.4 讲的 LangGraph 流式 API，一个一个 chunk 吐出来。
- `await bridge.publish(run_id, sse_event, serialize(chunk, ...))` **★ 这是和朴素写法最核心的一行区别**：chunk 不再 `yield` 给 HTTP，而是 `publish` 给 Bridge。`serialize`（`worker.py:56` 导入）把 langgraph 内部的 chunk 对象转成 JSON 可序列化的 dict；`sse_event` 是事件名（由 `_lg_mode_to_sse_event` 把 mode 映射成 SSE 事件名，`worker.py:1404-1413`，目前是 1:1 映射）。

多 mode / subgraphs 分支（`worker.py:506-525`）逻辑一样，只是多了一步 `_unpack_stream_item`（`worker.py:517`、定义在 `1552-1575`）把 `(mode, chunk)` 元组拆开：

```python
async for item in agent.astream(input_payload, config=stream_config, stream_mode=lg_modes, subgraphs=stream_subgraphs):
    if record.abort_event.is_set():
        break
    mode, chunk = _unpack_stream_item(item, lg_modes, stream_subgraphs)   # 拆包
    if mode is None:
        continue
    ...
    await bridge.publish(run_id, sse_event, serialize(chunk, mode=mode))  # ★ 同样喂给 Bridge
```

为什么 `run_agent` 是后台 task、事件必须经 Bridge？回到 `services.py:746-761` 那段：路由把 `run_agent` 用 `asyncio.create_task` 派到后台，**立刻返回 record 给前端**（不等 agent 跑完）。前端的 SSE 订阅请求是**另一个**独立的 HTTP 请求，它去 Bridge 上 `subscribe(run_id)` 拿事件（stream-bridge 篇讲过这个 fan-out）。所以 `run_agent` 必须是后台 task（不能阻塞路由返回），它产出的事件必须发到一个独立于请求的地方（Bridge），这两件事是配套的。

朴素写法把流焊死在请求上，请求一断流就断、别的请求也 join 不进来。`run_agent` 用"后台 task + Bridge 中转"把这件事解耦了。

---

### 麻烦 2：用户要"打断重来"——rollback / interrupt 必须预先拍快照

#### 场景

run-manager 篇麻烦 3 讲过多任务策略：用户在一个已有活动 run 的 thread 里又发消息，DeerFlow 给三种选择——`reject`（拒绝新的）、`interrupt`（打断旧的，**保留它的 checkpoint**）、`rollback`（打断旧的，**回滚到这次 run 开始之前**）。

`rollback` 比 `interrupt` 彻底：interrupt 相当于"旧 run 当没跑完，但它已经写的 checkpoint 还在"（半截成果保留）；rollback 相当于"旧 run 当从没发生过，thread 状态回到它开跑之前"。用户选 rollback 通常是觉得旧 run 走错方向了，想干干净净重来。

要实现 rollback，**必须在 run 开跑之前就把"那时的状态"存下来**——因为一旦 run 开始写 checkpoint，旧的就被覆盖了（checkpointer 是追加写，但 thread 的"当前指针"会一直指向最新的）。等用户喊 rollback 时再想去捞"跑之前的状态"，捞不回来了。

#### 朴素写法为什么做不到

朴素写法只 `async for chunk in agent.astream(...)`，agent 一边跑一边往 checkpointer 写新 checkpoint，把旧的顶下去了。从头到尾没有任何"跑之前先存档"的动作。用户喊 rollback，无档可回。

#### `run_agent` 怎么解决的：跑前拍 checkpoint 快照 + workspace 快照

`run_agent` 在跑 agent 之前（标 running 之后、真正 `astream` 之前），专门做两件"存档"的事。

**第一件：拍 workspace 快照**（`worker.py:320-328`），记录这次 run 跑之前工作区有哪些文件：

```python
if event_store is not None:
    workspace_changes_user_id = get_effective_user_id()
    try:
        pre_run_workspace_snapshot = await capture_workspace_snapshot(
            thread_id,
            user_id=workspace_changes_user_id,
        )
    except Exception:
        logger.warning("Could not capture pre-run workspace snapshot for run %s", run_id, exc_info=True)
```

这个快照在 finally 里会和"跑完后的快照"对比（`record_workspace_changes`，`worker.py:636-646`），算出"这次 run 改了哪些文件"——给前端的"工作区变更"卡片用。注意它包了 `try/except`：拍不到只 warn 不中断，因为这是个辅助功能，不能因为快照拍失败就让整个 run 跑不起来。

**第二件：拍 checkpoint 快照**（`worker.py:331-347`）——这才是 rollback 的命根子：

```python
# Snapshot the latest pre-run checkpoint so rollback can restore it.
if checkpointer is not None:
    try:
        config_for_check = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        ckpt_tuple = await checkpointer.aget_tuple(config_for_check)        # ★ 读当前最新 checkpoint
        if ckpt_tuple is not None:
            ckpt_config = getattr(ckpt_tuple, "config", {}).get("configurable", {})
            pre_run_checkpoint_id = ckpt_config.get("checkpoint_id")        # 记下 checkpoint_id
            pre_run_snapshot = {
                "checkpoint_ns": ckpt_config.get("checkpoint_ns", ""),
                "checkpoint": copy.deepcopy(getattr(ckpt_tuple, "checkpoint", {})),     # ★ 深拷贝整个 checkpoint
                "metadata": copy.deepcopy(getattr(ckpt_tuple, "metadata", {})),         # ★ 深拷贝 metadata
                "pending_writes": copy.deepcopy(getattr(ckpt_tuple, "pending_writes", []) or []),  # 深拷贝 pending writes
            }
            pre_existing_message_ids = _collect_pre_existing_message_ids(pre_run_snapshot)   # 麻烦 5 用
    except Exception:
        snapshot_capture_failed = True                                     # ★ 标记：快照没拍成
        logger.warning("Could not capture pre-run checkpoint snapshot for run %s", run_id, exc_info=True)
```

逐行讲关键的：

- `ckpt_tuple = await checkpointer.aget_tuple(config_for_check)` 把当前 thread 的**最新 checkpoint** 整个读出来。这就是"跑之前的状态"——agent 还没动过它。
- `pre_run_checkpoint_id = ckpt_config.get("checkpoint_id")` 记下这个 checkpoint 的 id，rollback 时要靠它定位。
- `pre_run_snapshot = {...}` 把 checkpoint 的三个核心部分——`checkpoint`本体（含 `channel_values`，0.5 讲过）、`metadata`、`pending_writes`（还没落盘的写入）——**深拷贝**三份存到局部变量。⚠️ `copy.deepcopy` 是必须的：`aget_tuple` 返回的 tuple 里的对象是活引用，不深拷贝的话，后面 agent 一写 checkpoint，这些引用跟着变，快照就毁了。
- `snapshot_capture_failed = True` 拍快照失败时**不打断 run**，只设个标志。rollback 时（麻烦 4 会看到）会先检查这个标志，发现快照没拍成就直接放弃 rollback——宁可 rollback 不生效，也不能拿个半截快照瞎回滚（`_rollback_to_pre_run_checkpoint` 的 `worker.py:1072-1074`）。

`pre_existing_message_ids` 这一行是麻烦 5 的伏笔，先记住它把"跑之前就存在的消息 id"收集成了一个 set。

#### 快照怎么用：`_rollback_to_pre_run_checkpoint`

等用户真的喊 rollback（通过 `abort_action == "rollback"`，麻烦 4 会讲怎么触发），`run_agent` 会调 `_rollback_to_pre_run_checkpoint`（`worker.py:1058-1148`）。它的核心思路是：**把存的那个旧 checkpoint，以"新 checkpoint"的形式写回去**，让 thread 的当前指针指回过去。看关键几行（`worker.py:1094-1117`，精简）：

```python
restore_marker = _new_checkpoint_marker()                        # 造一个新的 checkpoint id + 时间戳
checkpoint_to_restore = {
    **checkpoint_to_restore,                                     # 用旧 checkpoint 的 channel_values
    "id": restore_marker["id"],                                  # 但换成新 id
    "ts": restore_marker["ts"],                                  # 换成新时间戳
}
...
restored_config = await _call_checkpointer_method(
    checkpointer, "aput", "put",                                 # ★ 写一个新 checkpoint
    restore_config,
    checkpoint_to_restore,                                       # 内容是旧的
    metadata_to_restore,
    new_versions,
)
```

逐行：

- `_new_checkpoint_marker()` 造一对全新的 `id` 和 `ts`。为什么不直接用旧 checkpoint 的 id？因为 checkpointer 里旧 id 那条记录可能已经被覆盖/语义变了；用一个**全新的 id 写一条新记录**，内容是旧的 `channel_values`，是最稳的"回到过去"方式——thread 的当前指针一指向这条新记录，状态就回到了跑之前。
- `_call_checkpointer_method(checkpointer, "aput", "put", ...)` 调 checkpointer 的 `aput`（异步）或 `put`（同步）写这条新 checkpoint。这个 helper（`worker.py` 从 `goal.py` 导入，`worker.py:41`）会自动选异步或同步方法，屏蔽不同 checkpointer 实现的差异。

后面的 `worker.py:1127-1148` 还会把快照里的 `pending_writes`（当时还没落盘的写入）也补写回去，确保状态完整。

⚠️ 注意 interrupt 和 rollback 的区别在代码上就一行（麻烦 4 会看到，`worker.py:556-571`）：`abort_action == "rollback"` 才调上面这个回滚函数；否则（`interrupt`）只 `set_status(interrupted)`，**不动 checkpoint**——半截成果保留。这个区别全靠这里预先拍的快照才成立。

朴素写法没存档，rollback 无从谈起。`run_agent` 在跑之前先把 checkpoint 和 workspace 各拍一份快照，rollback 时把快照重写成新 checkpoint 写回——这就是"打断重来"的物理基础。

---

### 麻烦 3：agent 要会"自我追问"——goal continuation 隐藏轮循环

#### 场景

0.7 讲过 goal 机制：用户给 thread 定个目标，agent 跑完用户那一轮后，自己判断目标做没做完，没做完就**再追加一轮隐藏的续跑**，直到做完或撞上限。

这意味 `run_agent` 不能只跑一轮 `astream` 就完事——它得是一个"**跑一轮 → 评估目标 → 决定要不要再来一轮 → 再跑一轮 → …**"的循环。而且续跑轮对用户是隐藏的（输入是 agent 内部合成的消息），但事件照样要流给前端（让用户看到 agent 在继续干活）。

#### 朴素写法为什么做不到

朴素写法就是单发一发地跑一次 `agent.astream(...)`，跑完就结束。没有任何"跑完之后再评估目标、再续跑"的逻辑。goal 机制根本无法实现。

#### `run_agent` 怎么解决的：先跑可见轮，再 while 循环跑隐藏续跑轮

看 `worker.py:527-550` 这一段（注释保留了原文，信息量很大）：

```python
# 7. Stream the requested turn, then optionally continue hidden goal turns.
# Clear any stale stop_reason before the first (user-visible) turn only.
# Continuation turns preserve a cap reason from the user turn: a run that
# hits a cap during the user turn IS capped even if hidden goal-evaluator
# turns complete cleanly afterward (#4176 review).
if isinstance(runtime.context, dict):
    runtime.context.pop("stop_reason", None)                     # ★ 只在可见轮前清 stop_reason
await _stream_once(graph_input, initial_runnable_config)         # ① 跑用户那一轮（可见）
while not record.abort_event.is_set() and not llm_error_fallback_message and (journal is None or not journal.had_llm_error_fallback):
    continuation_input = await _prepare_goal_continuation_input(  # ② 问：要不要续跑？
        bridge=bridge,
        checkpointer=checkpointer,
        thread_id=thread_id,
        run_id=run_id,
        model_name=record.model_name,
        app_config=ctx.app_config,
        evaluator_model_factory=_get_goal_evaluator_model,
        abort_event=record.abort_event,
        ...
    )
    if continuation_input is None or record.abort_event.is_set():
        break                                                     # 不用续跑 / 被取消 → 退出
    await _stream_once(continuation_input, _continuation_runnable_config())   # ③ 续跑一轮（隐藏）
```

逐步：

- **`runtime.context.pop("stop_reason", None)`** 在可见轮开跑前，清掉 `runtime.context` 里可能残留的旧 `stop_reason`（0.8 讲过这是 middleware 盖的章）。⚠️ **只在可见轮前清，续跑轮不清**——注释说得很明白：如果可见轮就被 middleware 掐了（比如循环检测），那哪怕后续的 goal-evaluator 续跑轮干净跑完，这条 run **仍然算被掐的**（#4176 review 的修正）。这是个微妙的语义：可见轮的 cap 状态要"贯穿"整个 run。
- **① `await _stream_once(graph_input, ...)`** 跑用户那一轮——`graph_input` 就是用户发的消息（`services.py:723` 的 `normalize_input` 处理过的）。这一轮的事件（token 流、状态快照）会流给前端，用户能看到 agent 在干活。
- **`while not record.abort_event.is_set() and ...`** 续跑循环的**三个退出条件**，缺一不可：
  - `not record.abort_event.is_set()`：用户喊停了（麻烦 4）→ 不再续跑。
  - `not llm_error_fallback_message`：可见轮 LLM 挂了（麻烦 5，`llm_error_fallback_message` 被填了）→ 不再续跑（都出错了还续什么）。
  - `journal is None or not journal.had_llm_error_fallback`：journal 那边也记录了 LLM 错误（双保险，因为有些 fallback marker 不一定在流里被抓到）→ 不再续跑。
- **② `continuation_input = await _prepare_goal_continuation_input(...)`** 这一步是"问"：目标做完了吗？要不要再来一轮？返回值是续跑的输入（一个 `{"messages": [...]}`），返回 `None` 表示"不用续跑"（目标做完了、或撞了续跑次数上限、或 evaluator 失败了）。这函数（`worker.py:866-1055`）内部巨复杂——读 checkpoint、调 evaluator 模型评估、检查各种竞态（用户中途发新消息了、`/goal clear` 了、checkpoint 被别人改了），本文不展开。
- **③ `await _stream_once(continuation_input, _continuation_runnable_config())`** 真的续跑一轮。注意两点：续跑用的也是 `_stream_once`，事件照样 `bridge.publish` 给前端（用户能看到 agent 继续干活）；但用的 config 是 `_continuation_runnable_config()`，不是 `initial_runnable_config`。

#### 续跑为什么要换 config：`_continuation_runnable_config`

看 `worker.py:411-418`：

```python
def _continuation_runnable_config() -> RunnableConfig:
    continuation_config = dict(config)
    configurable = dict(continuation_config.get("configurable", {}) or {})
    configurable["checkpoint_ns"] = ""                           # ★ namespace 清空
    configurable.pop("checkpoint_id", None)                      # ★ 删掉 checkpoint_id
    configurable.pop("checkpoint_map", None)                     # ★ 删掉 checkpoint_map
    continuation_config["configurable"] = configurable
    return RunnableConfig(**continuation_config)
```

逐行讲为什么要删这三个：

- **`checkpoint_ns = ""`**：langgraph 的 subgraph 有自己的 namespace。续跑要从 thread 的根 namespace 接着跑，不能卡在某个 subgraph 的 ns 里。
- **`pop("checkpoint_id")` / `pop("checkpoint_map")`**：可见轮跑完后，config 里可能残留"从某个特定 checkpoint 接着跑"的指针。续跑要**从 thread 当前的最新 checkpoint 接着跑**（上一轮的成果要带进来），不能被旧指针锁定。删掉这俩，langgraph 就会自动取最新的。

朴素写法只跑一轮，goal 机制无从谈起。`run_agent` 用"可见轮 + while 续跑轮"的结构，让 agent 能在用户没再发消息的情况下自己多跑几轮，把目标做完——而这一切对续跑的 config 处理、退出条件判断，都是朴素写法完全没有的。

---

### 麻烦 4：中途喊停——abort_event 协作式取消的双路径

#### 场景

agent 跑到一半，用户点了"停止"。或者用户发了新消息，多任务策略是 interrupt/rollback，要把旧 run 停下来（run-manager 篇麻烦 3）。后端要做的是：**让正在跑的 agent 干净地停下来，而不是粗暴地杀掉它。**

0.6 讲过为什么"干净停"比"粗暴杀"重要：粗暴杀（`task.cancel()` 强抛异常）可能打在 checkpoint 写一半的当口，留下半截状态（stream-bridge 篇 §0.5 的坑）。干净停是让 agent 在两个 chunk 之间的安全间隙退出。

而且停完之后要按多任务策略决定**怎么收尾**——是 interrupt（保留 checkpoint）还是 rollback（回滚到跑前），这两个收尾动作完全不同（麻烦 2 讲过 rollback 要用预拍快照）。

#### 朴素写法为什么做不到

朴素写法的 `async for chunk in agent.astream(...)` 一旦开始，没有任何"检查要不要停"的地方——它会一直跑到 agent 自己结束。想停？只能从外面 `task.cancel()` 强抛异常，但那又落回"粗暴杀"的坑。

#### `run_agent` 怎么解决的：主动检查 + 被动兜底，两条路径同一套收尾

**主动检查——流循环里查铃铛。** 0.6 贴过，`_stream_once` 的两个分支在每次 `async for` 迭代开头都检查 `record.abort_event.is_set()`（`worker.py:497`、`513`）。用户点停止，RunManager 按铃（`abort_event.set()`），`run_agent` 在下一个 chunk 的间隙发现铃响了，`break` 退出循环——这是常态路径，干净利落。

**主动检查完之后，按 abort_action 收尾。** 退出循环后，`run_agent` 在 `worker.py:552-571` 处理"被主动取消"的情况：

```python
# 8. Final status
if record.abort_event.is_set():
    await run_manager.set_finalizing(run_id, True)               # ★ 标记"正在收尾"
    action = record.abort_action                                 # interrupt 还是 rollback？
    if action == "rollback":
        await run_manager.set_status(run_id, RunStatus.error, error="Rolled back by user")
        try:
            await _rollback_to_pre_run_checkpoint(...)           # ★ 麻烦 2 的回滚函数
            ...
        except Exception:
            logger.warning(...)
    else:                                                        # interrupt
        await run_manager.set_status(run_id, RunStatus.interrupted)   # 只标 interrupted，不动 checkpoint
```

逐行：

- `await run_manager.set_finalizing(run_id, True)` 把 record 的 `finalizing` 标志设为 True（`manager.py:679-687`）。这个标志的作用马上讲。
- `action = record.abort_action` 取消策略：`"rollback"` 还是 `"interrupt"`（默认）。这个值是 RunManager 在创建/取消 run 时根据多任务策略设的（run-manager 篇麻烦 3）。
- **rollback 分支**：先把状态标 `error`（rollback 的 run 算失败），然后调麻烦 2 讲的 `_rollback_to_pre_run_checkpoint` 把 checkpoint 回滚到跑之前。
- **interrupt 分支**：只把状态标 `interrupted`，**完全不碰 checkpoint**——agent 跑到哪算哪，半截成果保留。

**被动兜底——`except asyncio.CancelledError`。** 万一主动检查还没来得及执行（比如 agent 正卡在一个很长的 LLM 调用里，chunk 迟迟不来），外面 `task.cancel()` 了，`CancelledError` 抛进来。`run_agent` 在 `worker.py:596-615` 接住它：

```python
except asyncio.CancelledError:
    await run_manager.set_finalizing(run_id, True)
    action = record.abort_action
    if action == "rollback":
        await run_manager.set_status(run_id, RunStatus.error, error="Rolled back by user")
        try:
            await _rollback_to_pre_run_checkpoint(...)
            ...
        except Exception:
            logger.warning(...)
    else:
        await run_manager.set_status(run_id, RunStatus.interrupted)
        ...
```

⚠️ **注意：这和上面主动路径的代码几乎一字不差。** 这是故意的——不管 run 是怎么停的（主动检查发现 / 被动抛异常），收尾动作必须**完全一致**：都 `set_finalizing`、都按 `abort_action` 走 rollback 或 interrupt。这样不管走哪条路径，thread 的最终状态都是对的。

#### `set_finalizing` + `wait_for_prior_finalizing` 怎么配合多任务策略

`finalizing` 这个标志是干嘛的？回想 run-manager 篇麻烦 3：同一 thread 最多一个活动 run，新 run 要等旧 run 收尾。但"收尾"不是一瞬间——rollback 要写 checkpoint（可能几百毫秒），interrupt 也要等 agent 干净退出。如果新 run 在旧 run 刚 break、还没 rollback 完时就开跑，它会撞上旧 run 还在写的 checkpoint。

所以：旧 run 收尾前 `set_finalizing(True)`（标"我还在收拾"），收尾完在 finally 里 `set_finalizing(False)`（`worker.py:715-716`，麻烦 6 会看到）。而**新 run 开跑前**，`run_agent` 第一件事就是等（`worker.py:298`）：

```python
try:
    await run_manager.wait_for_prior_finalizing(thread_id, run_id)   # ★ 等同 thread 的旧 run 收完尾
```

`wait_for_prior_finalizing`（`manager.py:689-711`）的逻辑是：轮询检查同 thread 有没有比自己早的、还处于 `finalizing` 状态的 run，有就等（sleep 10ms 再查），没有才返回。这样新 run 永远不会在旧 run 还没收拾干净时去抢 checkpoint。

朴素写法没有取消检查点、没有 finalizing 协调，要么停不下来、要么粗暴杀留半截状态。`run_agent` 用"主动检查铃铛 + 被动接异常，两路同一套收尾 + finalizing 协调多任务"把这件事管死了。

---

### 麻烦 5：LLM 挂了但看着像"正常结束"——error fallback marker 检测 + 历史去重

#### 场景

agent 跑的时候，LLM 提供方挂了（限流、超时、模型抽风）。按理说这种 run 应该标 `error`。但 DeerFlow 的 model-call middleware 做了一件"宽容"的事：**它不抛异常（抛异常会直接打断 run），而是生成一条"错误兜底消息"塞进消息列表**，内容大概像"LLM 服务暂时不可用"，然后让 agent 继续——从 graph 外部看，agent 就像"正常结束"了一样。

这种设计的代价：如果不专门检查，`run_agent` 会把这种 run 标成 `success`——明明 LLM 挂了，却显示成功。用户和监控系统都被骗了。

更阴的坑：因为 `values` 模式会**重放整个消息历史**（0.4 讲过），**上一次 run 留下的 fallback marker 会出现在本次 run 的流里**——如果不区分，一次 LLM 故障会让这个 thread 上**之后所有的 run** 都被误标成 error。

#### 朴素写法为什么做不到

朴素写法只管 `yield chunk`，不检查 chunk 里有没有 fallback marker。agent 正常结束（没有 tool_calls 了）就当 success。LLM 挂了的 run 伪装成成功，历史 marker 还会误伤后续 run。

#### `run_agent` 怎么解决的：流循环里抓 marker + 历史去重

**第一步：流循环里每个 chunk 都扫一遍。** 看麻烦 1 贴过的 `_stream_once`，里面有一行容易被忽略（`worker.py:500`，多 mode 分支是 `worker.py:521`）：

```python
async for chunk in agent.astream(...):
    if record.abort_event.is_set():
        break
    llm_error_fallback_message = llm_error_fallback_message or _extract_llm_error_fallback_message(chunk, pre_existing_message_ids)   # ★ 扫 chunk
    ...
```

逐行：

- `_extract_llm_error_fallback_message(chunk, pre_existing_message_ids)` 把刚产出的 chunk 扔进去扫，找有没有带 `deerflow_error_fallback` 标记的消息。这个函数（`worker.py:1466-1522`）逻辑是：如果 chunk 是 `values` 模式的大状态字典，就只扫它的 `messages` 列表（快路径）；否则深递归扫整个 chunk（`updates` 这种小 chunk 用）。
- `llm_error_fallback_message = llm_error_fallback_message or ...` 用 `or` 做短路：一旦前面已经抓到过 fallback message，后面就不再覆盖（保留第一次抓到的）。所以这个变量要么一直是 `None`（没挂），要么一旦非 None 就定格。

`pre_existing_message_ids` 就是麻烦 2 伏笔里那个 set——**历史去重**的关键。看它怎么用的：进 `_extract_llm_error_fallback_message` → `_try_extract_from_message`（`worker.py:1440-1463`）：

```python
def _try_extract_from_message(obj: Any, pre_existing_ids: set[str] | None = None) -> str | None:
    if pre_existing_ids:                                          # ★ 有历史 id 集合
        msg_id = _message_id(obj)
        if msg_id is not None and msg_id in pre_existing_ids:
            return None                                           # ★ 这条是历史消息 → 忽略它的 marker
    additional_kwargs = getattr(obj, "additional_kwargs", None)
    if isinstance(additional_kwargs, dict) and additional_kwargs.get("deerflow_error_fallback"):
        return _error_fallback_message_from_metadata(additional_kwargs, getattr(obj, "content", None))
    ...
```

逐行讲历史去重：

- `if msg_id in pre_existing_ids: return None` 这一行是命门。`pre_existing_message_ids` 是**这次 run 开跑之前**就存在于 checkpoint 里的消息 id 集合（麻烦 2 的 `_collect_pre_existing_message_ids` 收集的，`worker.py:1525-1549`）。如果扫到的某条消息的 id 在这个集合里，说明它是**历史消息**（上次 run 留下的），它身上的 fallback marker 是**上次 run 的账**，不该算到本次 run 头上——直接 `return None` 忽略。
- 只有**本次 run 新产生的消息**（id 不在历史集合里）带 `deerflow_error_fallback` 标记，才算本次 run 真的挂了。

**第二步：抓到了就把 run 标 error。** 流循环结束后，`run_agent` 在 `worker.py:552-594` 判定最终状态，专门有这个分支（`worker.py:572-577`）：

```python
elif llm_error_fallback_message or (journal is not None and journal.had_llm_error_fallback):
    error_msg = llm_error_fallback_message
    if error_msg is None and journal is not None:
        error_msg = journal.llm_error_fallback_message            # journal 那边的记录兜底
    error_msg = error_msg or "LLM provider failed after retries"
    await run_manager.set_status(run_id, RunStatus.error, error=error_msg)   # ★ 标 error 而非 success
```

逐行：

- `elif llm_error_fallback_message or journal.had_llm_error_fallback` 两个信号源取并集：流循环抓到的（`llm_error_fallback_message`）、或 journal 回调抓到的（`journal.had_llm_error_fallback`，因为有些 fallback marker 不一定经过流，但会触发 LLM end 回调被 journal 记下）。任一非空 → 进 error 分支。
- `await run_manager.set_status(run_id, RunStatus.error, error=error_msg)` ★ **把这条 run 标成 `error`，而不是 success**。这样监控、用户看到的都是诚实的"失败"。

这个分支在判断顺序上排在 `if record.abort_event.is_set()`（麻烦 4，被取消）之后、`else`（正常 success）之前——优先级很清楚：被取消 > LLM 挂 > 正常成功。

朴素写法对 LLM 挂掉一无所知，还会被历史 marker 误伤。`run_agent` 用"流循环扫 marker + 历史 id 去重 + 优先级判定"三件套，让 LLM 故障被如实标记，且不会跨 run 传染。

---

### 麻烦 6：不管怎么收场，流必须有终、状态都得落——finally 全收尾

#### 场景

run 总要结束的——可能成功、可能 LLM 挂了（麻烦 5）、可能被取消（麻烦 4）、可能抛了别的异常。不管哪种结局，都有几件**必须做**的事，一件都不能漏：

1. **事件流必须有终止符**——订阅者在等 `END_SENTINEL`（stream-bridge 篇讲过），不发的话它们会傻等到天荒地老。
2. **这次 run 产生的所有副作用都得落盘**——token 用量、工作区变更、子 agent 步骤事件、对话标题、run 时长……这些都是"跑完才知道"的数据，得在收尾时持久化。
3. **thread 的状态要更新**——run 成功了 thread 该回到 idle，run 失败了 thread 状态也该反映出来。
4. **`finalizing` 标志要清掉**——麻烦 4 讲过新 run 会等旧 run 的 `finalizing` 清掉，不清理的话新 run 永远卡住。

这些事有个共同特点：**不管 run 怎么死的都得做**。所以它们不能放在某个 `if`/`elif` 分支里（那只能覆盖一种结局），必须放在 `finally` 块里——`try` 块无论正常结束、抛异常、被取消，`finally` 都会执行。

#### 朴素写法为什么做不到

朴素写法没 `try/finally`，`async for` 跑完就结束。如果中途抛异常，异常一路冒泡，yield 给 HTTP 的流直接断掉，订阅者收不到终止符，token 用量也没落盘——一个"半截烂尾"的 run。

#### `run_agent` 怎么解决的：一个 90 行的 finally 块，逐项收尾

看 `worker.py:630-719` 这一大块。它按"重要性从低到高、依赖关系从前到后"的顺序，逐项处理。先看骨架（精简注释）：

```python
finally:
    # ① 刷子 agent 步骤事件
    if subagent_events is not None:
        await subagent_events.flush()

    # ② 记工作区变更
    if event_store is not None and pre_run_workspace_snapshot is not None:
        try:
            await record_workspace_changes(event_store, thread_id, run_id, pre_run_workspace_snapshot, ...)
        except Exception:
            logger.warning(...)

    # ③ 刷 journal + 落 token 用量
    if journal is not None:
        try:
            await journal.flush()
        except Exception:
            logger.warning(...)
        try:
            completion = journal.get_completion_data()
            await run_manager.update_run_completion(run_id, status=record.status.value, **completion)
        except Exception:
            logger.warning(...)

    # ④ 给"被中断的首轮"补个标题
    if checkpointer is not None and record.status == RunStatus.interrupted:
        ...

    # ⑤ 标题回写 thread_store
    if checkpointer is not None and thread_store is not None:
        ...

    # ⑥ 落 run 时长
    if checkpointer is not None and record.status == RunStatus.success:
        ...

    # ⑦ 更新 thread 状态
    if thread_store is not None:
        try:
            final_status = "idle" if record.status == RunStatus.success else record.status.value
            await thread_store.update_status(thread_id, final_status)
        except Exception:
            logger.debug(...)

    # ⑧ 跑完成钩子
    if ctx.on_run_completed is not None:
        try:
            await ctx.on_run_completed(record)
        except Exception:
            logger.warning(...)

    # ⑨ 清 finalizing 标志
    if record.finalizing:
        await run_manager.set_finalizing(run_id, False)

    # ★★★ 命门两行 ★★★
    await bridge.publish_end(run_id)                              # ★ 流必须有终
    asyncio.create_task(bridge.cleanup(run_id, delay=60))         # ★ 延迟清理
```

逐项讲：

**① `subagent_events.flush()`（`worker.py:633-634`）**：麻烦 1 没细讲，`_SubagentEventBuffer`（`worker.py:184-243`）是个缓存——子 agent 的 `task_*` 步骤事件先攒着，攒满 25 条或遇到 `subagent.end` 才批量写进 `event_store`（避免每个步骤都开事务，注释 `worker.py:190-202` 讲了为什么）。finally 里强制 flush 一次，保证中断/异常路径下没攒满的那批也不丢。⚠️ 注意 `subagent_events` 在函数开头预绑成 `None`（`worker.py:288`），所以"流还没开始就异常"时这里也不会崩。

**③ journal flush + `update_run_completion`（`worker.py:649-660`）**：`RunJournal`（`worker.py:307` 懒导入）是个 LangChain 回调处理器，挂在 `config["callbacks"]`（`worker.py:389-390`）上，记录 LLM 的 token 用量（`on_llm_end` 触发）。`journal.get_completion_data()` 把累计的 token 数（输入/输出/按模型分）、消息数、最后一条 AI 消息等打包，`run_manager.update_run_completion`（`manager.py:416-451`）把它们落到 RunRecord 和数据库——前端列表页"这条 run 用了多少 token"就靠这个。

**④ `_ensure_interrupted_title`（`worker.py:662-668`）**：一个特殊场景——如果 run 是**首轮**（thread 第一次跑）又被 `interrupted` 了，对话还没生成标题（标题通常是 agent 跑完根据内容生成的），但用户在前端看到的列表得有个名字。这时用 `TitleMiddleware`（`worker.py:1330` 懒导入）根据已有内容合成个 fallback 标题写进 checkpoint（`_ensure_interrupted_title`，`worker.py:1322-1401`）。注意前面的 `wait_for_prior_finalizing` + `has_later_started_run` 两个守卫（`worker.py:664-665`）：要等旧 run 收尾、且确认没有更新的 run 抢先写过标题，才动手——避免标题覆盖冲突。

**⑤ ⑥ 标题/时长回写（`worker.py:671-700`）**：从最新 checkpoint 读出 `title`（agent 生成的对话标题）写回 `thread_store`；成功 run 还算个时长（`updated_at - created_at`）写进 checkpoint metadata（`_persist_run_duration`，`worker.py:1307-1319`）。这俩都是"方便前端展示"的辅助数据。

**⑦ thread 状态更新（`worker.py:703-708`）**：`run_manager` 管 run 状态，但 thread 也有个自己的状态（`thread_store`）。run 成功了 thread 回到 `"idle"`，否则 thread 状态跟着 run 状态走（`error`/`interrupted`）。这样前端"会话列表"能正确显示每个 thread 当前是空闲还是出错。

**⑨ 清 finalizing（`worker.py:715-716`）**：麻烦 4 讲过，取消收尾时会 `set_finalizing(True)`，这里收完尾必须 `set_finalizing(False)`——否则同 thread 的下一个 run 会在 `wait_for_prior_finalizing` 里永远等下去。

**★ 命门两行（`worker.py:718-719`）★**：

```python
await bridge.publish_end(run_id)                              # ★ 流必须有终
asyncio.create_task(bridge.cleanup(run_id, delay=60))         # ★ 延迟清理
```

- **`await bridge.publish_end(run_id)`** 这一行是整篇文档的命门。0.2 讲过 `publish_end` 是"告诉邮局这个 run 的事件到此为止"。所有在 `subscribe(run_id)` 等着的订阅者收到这个信号，才会 yield `END_SENTINEL` 正常退出（stream-bridge 篇麻烦 4 讲过订阅者怎么消费 END）。⚠️ **这一行在 finally 里、在所有 `if` 分支之后——意味着不管 run 是成功、失败、被取消、还是抛了未预期异常，`publish_end` 一定会被调用。** 这就是"流必须有终"的铁律：订阅者永远不会傻等。如果它放在某个分支里，那个分支没走到，订阅者就永远等不到 END——这正是朴素写法的坑。
- **`asyncio.create_task(bridge.cleanup(run_id, delay=60))`** 清理 Bridge 里这个 run 的存档（事件队列、计数器，`stream_bridge/memory.py:163-167`）。⚠️ 注意三个细节：① 用 `create_task` **派到后台**而不是 `await`——清理不阻塞 run 收尾；② `delay=60` 延迟 60 秒——给"run 刚结束、订阅者的新 HTTP 请求才刚到"这种迟到订阅留个窗口，60 秒内还能从 Bridge 拿到缓存的事件；③ 它在 `publish_end` **之后**——先通知结束，再清理存档，顺序不能反。

朴素写法没 finally，异常一来流断尾丢。`run_agent` 用一个 90 行的 finally 块，把"流的终止符 + 所有副作用落盘 + 状态更新 + finalizing 清理"全部保证执行——这才是"不管怎么收场都干净"的工程实现。

---

## 第 3 步：把 6 个麻烦的本质串起来

朴素写法 `async for chunk in agent.astream(input): yield chunk` 的根本问题是：

> **它把"agent 的执行"焊死在了"这次 HTTP 请求"的栈帧里——请求一断，执行就没了。**

而现实里，agent 执行的生命周期和 HTTP 请求的生命周期是**完全不同步**的两件事：

| 维度 | HTTP 请求 | agent 执行（`run_agent`） |
|---|---|---|
| 寿命 | 几秒，处理完即结束 | 一轮几十秒，goal 续跑可能多轮，结束后还要被查询很久 |
| 消费者 | 1 个客户端 1 次请求 | 一个 run 的事件要被任意数量、任意时刻的订阅者消费 |
| 取消 | 响应返回即结束 | 要能干净地从中间停下，还要区分 interrupt / rollback |
| 失败语义 | HTTP 错误码 | LLM 挂了要识别（不能伪装成功），还要不被历史 marker 误伤 |
| 终态 | 响应返回即终止 | 不管怎么收场，流必须有终符、所有副作用都得落盘 |

把两者焊死，就会出现 6 个麻烦——因为现实里它们的生命周期**就是会错位**：

| 麻烦 | 错位点 | `run_agent` 的解决 | 关键代码 |
|---|---|---|---|
| ① 事件的目的地 | chunk 该去请求还是去 run？ | 后台 task + `bridge.publish` 把 chunk 喂给 run（不是请求） | `worker.py:350-357, 502/523` |
| ② rollback 要有档可回 | 跑之前不存档，事后无档可回 | 跑前拍 checkpoint + workspace 快照，rollback 时重写 checkpoint | `worker.py:320-347, 1058-1148` |
| ③ goal 要自我追问 | 只跑一轮无法续跑 | 可见轮 + `while` 隐藏续跑轮循环 | `worker.py:527-550` |
| ④ 中途要能停 | 没取消检查点，停不下来 | 流循环主动查铃铛 + 被动接异常，两路同一套收尾 + finalizing 协调 | `worker.py:497/513, 552-615` |
| ⑤ LLM 挂了别伪装成功 | 不查 fallback marker，误标 success | 流循环扫 marker + 历史 id 去重 + 优先级判定标 error | `worker.py:500/521, 572-577` |
| ⑥ 不管怎么收场都要收尾 | 没 finally，异常一来流断尾丢 | 90 行 finally：publish_end + 所有副作用落盘 + 状态更新 | `worker.py:630-719` |

`run_agent` 就是**把"agent 的执行"从"HTTP 请求"的栈帧里解放出来**的那个函数。它具体做的事：

- **派到后台当 task**（配合 `services.py:746` 的 `create_task`），让执行脱离请求存活——事件发往 Bridge（run_id 为目的地），不是 yield 给请求。
- **跑前存档**（checkpoint + workspace 快照），让 rollback 在物理上可行。
- **跑可见轮 + 隐藏续跑轮**的循环，让 goal 自我追问机制能工作。
- **双路径取消**（主动查铃铛 + 被动接异常）+ finalizing 协调，让中断干净、多任务不撞车。
- **扫 fallback marker + 历史去重**，让 LLM 故障被如实标记、不跨 run 传染。
- **90 行 finally 兜底**，让流一定有终、副作用一定落盘、状态一定更新。

这一切，代码上都从 `services.py:746-761` 那两行开始：

```python
task = asyncio.create_task(run_agent(bridge, run_mgr, record, ...))
record.task = task
```

`create_task` 把 `run_agent` 派到后台（脱离请求），`record.task = task` 把它钉回 RunManager 注册表（run-manager 篇讲的"三级钉住"）。然后 `run_agent` 在后台接管一切：驱动 graph、喂事件给 Bridge、判定最终状态、保证收尾。

所以现在三部曲的分工完全清楚了——它们是**同一行 `create_task` 里三个参数**各自主导的领域：

```
asyncio.create_task(
    run_agent(                                    ┐
        bridge,        # stream-bridge 篇：事件分发 │  run-agent 篇（本文）：
        run_mgr,       # run-manager 篇：状态生命周期│  把三者串起来的执行引擎
        record,                                    ┘
        ctx=..., agent_factory=..., graph_input=..., config=...,
    )
)
```

- **`bridge`（stream-bridge 篇）**——管"事件从产出到任意订阅者的分发"。
- **`run_mgr` + `record`（run-manager 篇）**——管"run 的状态、生命周期、并发协调、孤儿恢复"。
- **`run_agent` 本身（本文）**——管"执行"：驱动 graph、决定 chunk 怎么产、判定这次 run 到底算成功还是失败、保证不管怎么收场都干净收尾。

三者缺一不可：没有 Bridge，事件没地方去；没有 RunManager，run 活不过请求；没有 `run_agent`，没有东西真正驱动 agent 跑、也没有人判定 run 的成败。它们合在一起，把"在 HTTP 请求里启动一次 agent 执行"这件事，彻底从请求的短暂生命周期里解放出来——这就是 `asyncio.create_task(run_agent(...))` 存在的全部理由。
