# RunManager 深度讲解：为什么不能直接在路由里 `asyncio.create_task(agent.run())` 就完事

本文是 `stream-bridge-deep-dive.md` 的姊妹篇。那篇讲的是 `bridge = get_stream_bridge(request)` 为什么存在；这篇讲的是紧挨着它的另一行——`run_mgr = get_run_manager(request)` 为什么存在。

两篇文章回答的是同一个大问题的两个面：**一次 HTTP 请求里启动的 agent run，凭什么能活得比这次请求久？** StreamBridge 负责"事件流"这一半，RunManager 负责"执行 + 状态"这一半。建议两篇对照着读。

本文面向第一次接触这套架构的同学。先把所有需要的前置概念一个一个铺出来，然后再讲 6 个麻烦，每段代码逐行解释。

---

## 第 0 步：先把要用的术语讲清楚

在讲任何代码之前，先把后面会反复出现的概念讲明白。这些是理解"为什么朴素写法会出问题"的地基。已经读过姊妹篇 `stream-bridge-deep-dive.md` 的同学可以快速跳过带 ※ 标记的小节（那里会注明"姊妹篇讲过"，本篇只复用结论）。

### 0.1 run——一次 agent 执行

在 DeerFlow 里，用户在聊天框发一句话，后端不是"跑完再返回"，而是**派一个后台任务去跑 agent**，这个后台任务的一次完整执行就叫一个 **run**。

每个 run 有：

- 一个全局唯一的 `run_id`（UUID）
- 一个归属的 `thread_id`（哪段对话）
- 一个**状态**（pending / running / success / error / …，下面 0.5 会细讲）
- 一个**后台任务对象** `task`（Python 的 `asyncio.Task`，下面 0.3 讲）

打个比方：run 就像餐厅里的一张**订单**——客人点了一份菜（用户发了消息），厨房开了张订单（创建 run），厨师开始做（agent 开始跑），做完端上桌（run 成功结束）。订单有编号（run_id）、归属哪桌（thread_id）、有状态（"排队中"/"做菜中"/"上菜了"/"取消了"）。

关键点：**订单的生命周期和点单那一下是两码事。** 客人点完单（HTTP 请求返回）就回去干别的了，订单还在厨房里跑——厨师还在做菜，端上桌前客人随时可以问"我的菜到哪了"（再来一个 HTTP 请求查 run 状态）、可以喊"不要了"（取消 run）。这正是朴素写法做不到的，后面会展开。

### 0.2 进程内存 vs 共享存储（数据库）

一个 Python 进程的内存，是这个进程**独占的、易失的**：

- **独占**：进程 A 内存里的数据，进程 B 看不到（厨师 A 口袋里的便条，厨师 B 翻不到）。
- **易失**：进程一死（崩溃、重启、被 kill），内存里的数据**全没了**（便条烧了）。

所以要"跨进程共享"或"进程死了也不丢"的信息，得放到**进程外面的共享存储**里——DeerFlow 用的是数据库（Postgres 或 SQLite）。共享存储就像餐厅**前台的黑板**：所有厨师都能看到、都能写，前台下班前擦了才算完。

RunManager 同时管这两个地方：

- 进程内存里一张表 `_runs`（快，但进程死了就没了）——记"本进程正在跑哪些 run"
- 数据库里一张 `runs` 表（慢一点，但持久、跨进程都能看）——记"历史上所有 run 的元信息"

两者的分工是本篇麻烦 2 的核心。

### 0.3 `asyncio.create_task`——后台跑一个协程（以及它的代价）

※ 概念基础和姊妹篇 §0.3 / §0.4 重叠，但这里聚焦在 `create_task` 这一个点。

Python 里 `async def` 定义的函数叫协程。直接调用 `foo()` 不会真的跑它，只是返回一个"协程对象"；要让它跑起来，得 `await foo()`（等它跑完）或者 `asyncio.create_task(foo())`（**后台跑，不等它**）。

```python
task = asyncio.create_task(agent.run())   # 派出去后台跑，立刻返回一个 task 对象
# 这一行之后，主流程继续往下走，agent 在后台慢慢跑
```

`asyncio.create_task` 就像餐厅经理**把订单塞进厨房窗口**就转身去招呼下一桌了——做菜的事交给厨师，经理不等着。

但这里有个**致命的代价**：`task` 这个对象是**一个普通的 Python 变量**。它存在哪里，取决于你把它存在哪里。

- 如果你写 `task = asyncio.create_task(...)` 在某个函数里，那 `task` 就是这个函数的**局部变量**——函数一返回，局部变量就没人在引用了。
- 没人引用的 task 会不会被取消？理论上 `asyncio` 会"尽量"让它跑完，但**你再也没有任何途径从外面观察它、取消它、查它的状态**——它成了一个"野任务"。

打个比方：你把订单塞进厨房窗口，但**没在订单本上登记**。客人后来问"我的菜呢？"——你翻遍订单本找不到这张单，只能干瞪眼；客人喊"不要了"——你也没法通知厨房停手。

这就是朴素写法的根本病根，第 1 步会展开。RunManager 干的第一件事，就是**把每个 task 登记到一个进程级的表里**，让它"有处可查"。

### 0.4 `asyncio.Event` / `asyncio.Lock`——铃铛和单人电话亭

这两个是 Python 异步里的同步原语，RunManager 大量使用。

**`asyncio.Event`——铃铛。** 一个 Event 内部有个"是否被按过"的标志。协程可以 `await event.wait()`（等别人按铃），另一个协程 `event.set()`（按铃），所有等着的协程就被唤醒。和姊妹篇 §0.6 的 `Condition` 不同的是：Event 是**一次性的广播**——按一次铃，永远记住"按过了"，之后所有 `wait()` 立刻通过；要复位得手动 `event.clear()`。

RunManager 用 Event 实现"取消信号"：run 的 record 上挂一个 `abort_event`，agent worker 在循环里**主动检查** `abort_event.is_set()`（详见姊妹篇麻烦 1 讲的"主动检查比被动接异常好"）。要取消一个 run，就 `abort_event.set()` 按铃，worker 下一轮检查就会发现，然后干净地收尾。

**`asyncio.Lock`——单人电话亭。** 一把锁，同一时刻只有一个协程能 `async with lock:` 进去，别的想进就得在外面排队。`async with` 进入时拿锁、退出时放锁。

RunManager 用 Lock 保护它那张内存表 `_runs`：所有"读改写 `_runs`"的操作都要先拿锁，避免两个协程同时改同一张表导致数据错乱（比如一个在删、一个在遍历，遍历的就崩了）。

### 0.5 状态机——红绿灯，只能在合法状态间转换

run 在它的生命周期里会经历不同的**状态**。DeerFlow 把可能的状态定义成一个枚举（`backend/packages/harness/deerflow/runtime/runs/schemas.py:6-14`）：

```python
class RunStatus(StrEnum):
    """Lifecycle status of a single run."""

    pending = "pending"          # 已创建，还没开始跑
    running = "running"          # 正在跑
    success = "success"          # 成功结束
    error = "error"              # 出错结束
    timeout = "timeout"          # 超时结束
    interrupted = "interrupted"  # 被取消（用户点停止、断线取消、shutdown 强停）
```

逐行：

- `class RunStatus(StrEnum):` 继承 `StrEnum`，表示这是个"字符串枚举"——每个成员既是个枚举常量，又是个字符串（`RunStatus.pending == "pending"` 为真）。这样序列化到 JSON、存进数据库都方便。
- 六个成员就是 run 可能处的六种状态。前两个（pending/running）是"**活动态**"——run 还没结束；后四个（success/error/timeout/interrupted）是"**终态**"——run 彻底结束了，不会再变。

状态不是随便变的——它是个**状态机**（像红绿灯）：红→绿→黄→红 有规定的顺序，不能红直接跳红。run 的合法转换大致是：

```
pending → running → success / error / timeout
                  ↘
pending/running → interrupted（被取消）
```

为什么强调"状态机"这个概念？因为后面会看到，RunManager 的很多代码都在**守护这些转换的合法性**——比如"已经 success 的 run 不能再被改成 running"（麻烦 2、5 会反复用到这个守护）。

### 0.6 lease——"我正在处理这个 run"的租约

※ 这个概念姊妹篇 §0.9 已经讲过（"共享单车"比喻），本篇复用结论，并补一个本篇才用到的细节。

lease（租约）是分布式系统里的常见机制。把 run 想象成一辆共享单车：谁要处理它先**扫码租走**（拿到 lease，记下 `owner_worker_id` 和 `lease_expires_at`），别人就不能动了；处理完释放。如果租的人摔了（进程崩了），租约到期后别人能接手。

本篇要补的一个关键细节是 **`grace_seconds`（宽限期）**。判定一辆单车的租约"过期"，不是看 `lease_expires_at` 这一秒一到就立刻判定，而是**再宽限 `grace_seconds` 秒**。为什么？因为不同 worker 的**时钟可能不完全同步**——W1 的时钟比 W2 快 5 秒，那 W1 看 W2 的租约就会觉得"早过期了"，从而错误地"接管"一个 W2 还在好好跑着的 run。宽限期就是给时钟误差留的预算：只要误差小于 `grace_seconds`，就不会误判。

DeerFlow 默认 `lease_seconds=30`、`grace_seconds=10`（`config/run_ownership_config.py:32-43`）。这意味着：一个 run 的租约 30 秒到期，但别的 worker 要再等 10 秒（共 40 秒没续租）才会认定拥有者已死、接管它。这 10 秒就是留给 NTP 时钟同步误差的。

### 0.7 并发竞争 / 唯一约束——"两个请求同时建 run"

并发编程里有个经典麻烦：**两个请求几乎同时到达，各自以为自己是第一个。**

设想：用户连点两下"发送"，两次 `POST /runs/stream` 几乎同时到达后端。两个路由处理函数各自检查"这个 thread 有没有正在跑的 run"，都没查到（因为对方还没来得及创建），于是各自创建了一个 run，各自启动了 agent。结果：**同一个 thread 上同时跑两个 agent**，两个 agent 都在往同一个 checkpoint 写状态，数据撞车，对话历史损坏。

怎么防？数据库有个机制叫**唯一约束（unique constraint）**：给某列（或某几列的组合）加个约束，数据库**保证**这张表里不会出现两行该列相同的记录。如果第二个 INSERT 撞了约束，数据库直接报错拒绝。

DeerFlow 用了一个更巧的版本——**部分唯一索引**（partial unique index）。它不是"整张表 run_id 唯一"（那没法存历史），而是"**在 status 是 pending/running 的行里，thread_id 唯一**"。也就是说：同一个 thread 同时只能有一个活动 run，但历史 run（success/error 等）随便存多少条都行。这个索引定义在 `backend/packages/harness/deerflow/persistence/run/model.py:62-68`，麻烦 3 会贴出来细讲。

### 0.8 worker / `GATEWAY_WORKERS`——进程数，以及多 worker 的硬门槛

※ 概念基础姊妹篇 §0.8 讲过（"多个厨师多个厨房"），本篇补一个生产部署的硬性要求。

生产部署时，Gateway（API 服务）可以跑多个进程来分摊负载，进程数由环境变量 `GATEWAY_WORKERS` 控制。但**不是随便就能开多 worker**——DeerFlow 在启动时有一道守卫（`backend/app/gateway/deps.py:49-85`），开多 worker 必须同时满足两个条件：

1. **数据库必须是 Postgres**。SQLite 用的是文件级写锁，没法支持多进程并发写同一个数据库文件，硬上会到处报"database is locked"。Postgres 是真正的多进程并发数据库，才行。
2. **`run_ownership.heartbeat_enabled` 必须开**（见麻烦 4）。不开的话，每个 run 的 lease 是 NULL，而 NULL lease 会被当成"过期"（见 0.6 的 `is_lease_expired`）——结果就是每次滚动发布或扩容，新 worker 一启动就会把老 worker 还在跑的 run 全当成"孤儿"接管掉、标成 error。这等于自己人杀自己人。

这道守卫的意义：**多 worker 是有代价的，得先把配套机制（Postgres + 心跳）准备好才许开。** 不满足就直接 `SystemExit` 拒绝启动，而不是让用户在生产里踩坑。

---

好，前置概念讲完了。现在我们开始讲代码。

## 第 1 步：朴素写法长什么样

如果没有 RunManager，一个"创建 run 并流式返回"的路由大概会写成这样（**这是反面教材，真实代码不是这样**）：

```python
async def stream_run_naive(thread_id, body, request):
    agent = build_agent(...)
    task = asyncio.create_task(agent.astream(...))   # 派出去后台跑
    return StreamingResponse(consume(task), ...)      # 立刻返回 SSE 流
```

逐行解释：

- **第 1 行** `async def stream_run_naive(thread_id, body, request):`
  定义一个异步函数，它是 FastAPI 的路由处理函数。三个参数：`thread_id`（会话 ID）、`body`（请求体，用户消息）、`request`（FastAPI 请求对象）。

- **第 2 行** `agent = build_agent(...)`
  构造一个 agent 对象（能聊天、能调工具的机器人实例）。

- **第 3 行** `task = asyncio.create_task(agent.astream(...))`
  **这是关键的一行**。`asyncio.create_task(...)` 把 agent 的执行塞到后台事件循环里跑，立刻返回一个 `task` 对象。注意：**`task` 是这个函数的局部变量。** 函数返回后，没有任何别的东西引用它。

- **第 4 行** `return StreamingResponse(consume(task), ...)`
  返回一个 SSE 流式响应，消费 agent 产出的 chunk（这部分姊妹篇已详谈，本篇不重复）。

**这 4 行代码的根本问题**：run 的全部信息——task 对象、它的状态、它的进度——都**只活在这个路由函数的局部变量里**。一旦响应返回（第 4 行），函数栈帧销毁，`task` 这个变量名就没了。agent 在后台或许还在跑，但**从外部看，这个 run 彻底"失联"了**：

- 客人再发一个请求问"我的 run 跑到哪了？"——没处查，因为没人记得这个 task。
- 客人想取消——没处取消，因为拿不到 task 对象。
- 进程重启——agent 跟着死，连个"它曾经存在过"的痕迹都没留下。
- 客人又发了一条新消息——没人检查"上一个 run 还在跑"，两个 agent 撞车。

下面 6 个麻烦，全是从"run 的生命周期被焊死在创建它的那次请求里"这一个病根派生的。RunManager 就是来拔这个病根的。

---

## 第 2 步：6 个麻烦逐一展开

### 麻烦 1：第二次请求想看这个 run（列表 / 详情 / join）

#### 场景

用户发了消息，agent 在后台跑起来了。然后——

- 用户刷新页面，前端发 `GET /threads/{tid}/runs` 要展示**历史 run 列表**（哪次 run 用了多少 token、什么状态、哪个模型）。
- 用户点开某条 run 看 `GET /threads/{tid}/runs/{rid}` 要看**这条 run 的详情**。
- 用户在另一个标签页打开了同一线程，发 `GET /threads/{tid}/runs/{rid}/join` 要**attach 到正在跑的 run 的流**（姊妹篇麻烦 3 讲的 fan-out）。

这些请求都是**新的 HTTP 请求**，跟当初创建 run 的那次请求不是同一次。它们怎么找到那个正在后台跑的 run？

#### 朴素写法为什么做不到

回到第 1 步：`task` 是创建函数的局部变量，响应一返回，外部再也拿不到。第二次请求进来时，**没有任何全局的地方能查到"run_id 对应哪个 task"**。join 一个 run？没法 join——连 run 在哪都不知道。

#### RunManager 怎么解决的：进程级注册表

RunManager 的核心就是一张**进程级的字典** `_runs`，把每个 run 登记下来。看构造函数（`backend/packages/harness/deerflow/runtime/runs/manager.py:195-215`）：

```python
def __init__(self, store=None, *, run_ownership_config=None):
    self._runs: dict[str, RunRecord] = {}                        # ← 核心：run_id -> 记录
    self._runs_by_thread: dict[str, dict[str, None]] = {}        # 二级索引：thread_id -> run_id 集合
    self._lock = asyncio.Lock()                                   # 保护上面两张表的锁
    self._store = store                                           # 持久化层（麻烦 2 讲）
    self._worker_id = worker_id or _generate_worker_id()          # 本进程的唯一 ID（麻烦 4 讲）
    self._run_ownership_config = run_ownership_config             # lease/心跳配置
    self._heartbeat_task = None                                   # 心跳后台任务（麻烦 4 讲）
    self._heartbeat_stop = None
```

逐行讲关键的：

- `self._runs: dict[str, RunRecord] = {}`
  **这就是注册表**——一个字典，key 是 `run_id`，value 是 `RunRecord`（这个 run 的全部信息）。它挂在 `RunManager` 实例上，而 `RunManager` 实例又挂在 `app.state` 上（见本节后面的装配代码），所以它是**进程级单例**——整个进程里只有一个，所有请求都能访问到。创建 run 时往里塞一条，查询 run 时从里查，run 结束后清理。这是"第二次请求能找到 run"的物理基础。
- `self._runs_by_thread: dict[str, dict[str, None]] = {}`
  一个**二级索引**：key 是 `thread_id`，value 是"这个 thread 下所有 run_id 的集合"。为什么要它？因为前端经常要"列出某 thread 的所有 run"，如果只有 `_runs`，得**遍历整张表**过滤 thread_id（O(全部 run)）；有了这个索引，直接按 thread 取（O(该 thread 的 run 数)）。注意这里用 `dict[str, None]` 而不是 `set`，是为了**保留插入顺序**（Python 3.7+ dict 是有序的，set 不是）。
- `self._lock = asyncio.Lock()`
  一把锁（0.4 讲过的单人电话亭）。所有读写 `_runs` 和 `_runs_by_thread` 的操作都得先拿这把锁，避免并发改表导致数据错乱。

这两张表有个**关键不变量**——必须**在锁内同步修改**（`_index_run_locked` / `_unindex_run_locked`，`manager.py:217-227`）。也就是说，"往 `_runs` 塞一条"和"往 `_runs_by_thread` 登记一下"这两个操作之间不能有 `await`，必须在同一次持锁里完成。否则：协程 A 刚往 `_runs` 塞完、还没来得及更新索引就让出 CPU，协程 B 进来查 `_runs_by_thread`，就会**漏掉这条刚塞进去的 run**。这个不变量在 `_thread_records_locked` 的 docstring（`manager.py:229-241`）里写得很清楚。

#### RunRecord——一条 run 长什么样

`_runs` 字典里存的是 `RunRecord` 对象。它是 run 的"身份证 + 状态本"，定义在 `manager.py:148-184`。字段不少，但分四组就好记：

```python
@dataclass
class RunRecord:
    """Mutable record for a single run."""

    # —— 标识组 ——
    run_id: str                              # 全局唯一 ID
    thread_id: str                           # 归属哪个会话
    assistant_id: str | None                 # 用哪个 assistant 配置
    user_id: str | None = None               # 谁的 run（多租户隔离）

    # —— 状态组 ——
    status: RunStatus                        # 0.5 讲的六态之一
    on_disconnect: DisconnectMode            # 断线时怎么办（cancel / continue）
    multitask_strategy: str = "reject"       # 并发策略（麻烦 3）
    created_at: str = ""                     # 创建时间
    updated_at: str = ""                     # 最后更新时间
    error: str | None = None                 # 出错时的错误信息
    stop_reason: str | None = None           # 成功结束的原因（比如被中间件截断）

    # —— 执行组 ——
    task: asyncio.Task | None = None         # ★ 后台 agent 任务对象（朴素写法漏掉的那个）
    abort_event: asyncio.Event = ...         # 取消铃铛（0.4 讲过）
    abort_action: str = "interrupt"          # 取消时是 interrupt 还是 rollback
    finalizing: bool = False                 # 是否正在做取消后的清理
    model_name: str | None = None            # 用的哪个模型

    # —— 多 worker 归属组（麻烦 4、5 用）——
    store_only: bool = False                 # ★ 这条记录是"纯存储读取"的吗（麻烦 2）
    owner_worker_id: str | None = None       # 哪个 worker 拥有这个 run
    lease_expires_at: str | None = None      # 租约何时过期

    # —— token 计量组（给前端列表用）——
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    llm_call_count: int = 0
    lead_agent_tokens: int = 0
    subagent_tokens: int = 0
    middleware_tokens: int = 0
    token_usage_by_model: dict = ...
    message_count: int = 0
    last_ai_message: str | None = None
    first_human_message: str | None = None
    metadata: dict = ...
    kwargs: dict = ...
```

几个要点：

- `task: asyncio.Task | None` —— **这正是朴素写法丢失的那个东西。** 现在 it 被钉在 record 的一个字段上，而 record 又钉在 `_runs` 字典里，`_runs` 又钉在进程级单例上。**三级钉住，run 再也不会"失联"。** 注意 `repr=False` 表示打印 record 时不显示这个字段（task 对象的 repr 很长很丑）。
- `abort_event: asyncio.Event` —— 取消信号铃铛。要取消 run 就 `abort_event.set()`，agent worker 在循环里检查它（姊妹篇麻烦 1 讲过的"主动检查"）。
- `store_only: bool` —— 这个标志**只在从数据库反序列化出来的 record 上是 True**，麻烦 2 会专门讲它。
- `owner_worker_id` / `lease_expires_at` —— 多 worker 归属和租约，麻烦 4、5 的主角。

#### `get_run_manager(request)`——怎么拿到这个单例

RunManager 是进程级单例，但路由函数怎么拿到它？答案就是本文开头那行：`run_mgr = get_run_manager(request)`。

它的定义在 `backend/app/gateway/deps.py:351-365`：

```python
def _require(attr: str, label: str) -> Callable[[Request], T]:
    """Create a FastAPI dependency that returns ``app.state.<attr>`` or 503."""

    def dep(request: Request) -> T:
        val = getattr(request.app.state, attr, None)        # 从 app.state 取
        if val is None:
            raise HTTPException(status_code=503, detail=f"{label} not available")
        return cast(T, val)

    dep.__name__ = dep.__qualname__ = f"get_{attr}"
    return dep


get_run_manager: Callable[[Request], RunManager] = _require("run_manager", "Run manager")
```

逐行：

- `def _require(attr, label):` 这是个**工厂函数**——它不直接返回 RunManager，而是**生产一个"取 RunManager 的函数"**。这种写法叫"高阶函数"。为什么要这样？因为有好几个单例（stream_bridge、run_manager、checkpointer…）取法都一样，用一个工厂统一生成，避免写六遍重复代码（看 `deps.py:364-369`，`get_stream_bridge` / `get_run_manager` / `get_checkpointer` 全是 `_require(...)` 生成的）。
- `def dep(request: Request) -> T:` 内部定义的函数，它才是真正在每个请求里跑的那个。参数是 `request`——FastAPI 会把当前请求对象传进来。
- `val = getattr(request.app.state, attr, None)`
  从 `request.app.state` 上取名字叫 `attr` 的属性（这里 `attr="run_manager"`）。`request.app` 是当前 FastAPI 应用实例，`.state` 是它的全局状态对象——**单例就挂在这里**。`getattr(..., None)` 表示"取不到就返回 None"，不报错。
- `if val is None: raise HTTPException(503, ...)`
  如果取不到（说明应用还没启动完，RunManager 还没挂上去），返回 **HTTP 503（Service Unavailable）**。这是个兜底——正常请求进来时单例一定在，但在应用启动的极早期窗口内可能有请求，这时候宁可明确报 503 也不能让路由崩。
- `dep.__name__ = ... f"get_{attr}"` 把内部函数的名字改成 `get_run_manager`，这样报错信息里能看到真实的依赖名，而不是匿名的 `dep`。
- 最后一行 `get_run_manager = _require("run_manager", "Run manager")` 调用工厂，生成并赋值。

所以 `run_mgr = get_run_manager(request)` 本质就是"从 app 全局状态上把那个唯一的 RunManager 实例取出来，取不到就 503"。RunManager 实例是**应用启动时**（`deps.py:309-312`）挂上去的：

```python
app.state.run_manager = RunManager(
    store=app.state.run_store,
    run_ownership_config=run_ownership_config,
)
```

这一行在应用启动的 `langgraph_runtime` 生命周期里跑一次，构造一个 RunManager 挂到 `app.state.run_manager`。之后每个请求的 `get_run_manager(request)` 拿到的都是**同一个实例**。

#### 有了注册表，第二次请求怎么用

现在第二次请求进来，就能找到 run 了。看 `join_run` 端点（`backend/app/gateway/routers/thread_runs.py:618-624`）：

```python
@router.get("/{thread_id}/runs/{run_id}/join")
async def join_run(thread_id, run_id, request):
    run_mgr = get_run_manager(request)
    record = await run_mgr.get(run_id)              # ★ 从注册表查
    if record is None or record.thread_id != thread_id:
        raise HTTPException(status_code=404, ...)
    ...
```

`run_mgr.get(run_id)` 就是去 `_runs` 字典里查这个 run_id。查到了，就有了 `record.task`（agent 任务对象）、`record.status`（状态）……后面无论是 attach 到流、还是返回详情，都有据可依。

朴素写法没有这张注册表，第二次请求**根本无处可查**——这是 RunManager 解决的第一个、也是最基础的一个麻烦。

---

### 麻烦 2：进程重启后 run 历史不能丢

#### 场景

进程跑着跑着重启了（崩溃、发版、手动 `make stop` 再 `make start`）。重启后用户打开页面，想看**昨天那场 run 的列表**——用了多少 token、最后一条 AI 消息是啥、是什么状态。

注意：重启后内存全清空了（0.2 讲过"易失"）。如果 run 信息只存在 `_runs` 字典里，重启后**一张白纸**，用户的历史全没了。

#### 朴素写法为什么做不到

朴素写法连单进程内的注册表都没有（麻烦 1），更别提跨重启持久化了。`task` 对象本身就是内存里的、随进程死而死的。

#### RunManager 怎么解决的：双层架构（内存 + 数据库）

RunManager 采用**双层架构**：

- **内存层**（`_runs` 字典）：快，O(1) 查找，但进程死了就没了。
- **持久层**（`RunStore`，本质是数据库的 `runs` 表）：慢一点（要走数据库），但**持久、跨重启都在**。

每次 run 的状态变化，RunManager 都会**同时写两层**：内存里改一份（给当前进程的请求快速查），数据库里也写一份（给重启后和别的进程用）。

持久层的接口是 `RunStore`（抽象基类 `backend/packages/harness/deerflow/runtime/runs/store/base.py:17`），有两个实现：

- `MemoryRunStore`（`store/memory.py`）——纯内存的，进程死了也没。给开发/测试用。
- `RunRepository`（`backend/packages/harness/deerflow/persistence/run/sql.py:28`）——真正的 SQLAlchemy + 数据库实现，生产用。

装配时二选一（`deps.py:271-282`）：

```python
sf = get_session_factory()
if sf is not None:
    from deerflow.persistence.run import RunRepository
    app.state.run_store = RunRepository(sf)          # 有数据库 → 用持久实现
else:
    from deerflow.runtime.runs.store.memory import MemoryRunStore
    app.state.run_store = MemoryRunStore()           # 没数据库 → 用内存实现（开发）
```

逐行：

- `sf = get_session_factory()` 取数据库会话工厂。配了数据库（Postgres/SQLite）就有值，没配就是 None。
- `if sf is not None:` 有数据库——构造 `RunRepository(sf)`，所有读写都走真实的数据库表。
- `else:` 没数据库（比如纯本地开发没配 DB）——用 `MemoryRunStore()`，它内部也是个字典，行为像数据库但活在本进程内存里。这样开发时不用起数据库也能跑，代价是重启即丢。

然后这个 `app.state.run_store` 被传给 RunManager 构造函数（`deps.py:309` 的 `store=app.state.run_store`），就成了 RunManager 持久化的后端。

#### `store_only`——"我这条记录是纯读出来的"

双层架构带来一个微妙问题：重启后，用户来查昨天的 run。RunManager 的 `_runs` 字典里**没有**这条 run（内存清空了），但数据库 `runs` 表里有。怎么办？

`get` 方法（`manager.py:527`）的逻辑是：先查内存 `_runs`，没有就**回退到数据库查**，查到了就用 `_record_from_store`（`manager.py:378-414`）把数据库的行**反序列化**成一个 `RunRecord` 对象返回。关键在于，这个反序列化出来的 record 会带上 `store_only=True`（`manager.py:399`）：

```python
@staticmethod
def _record_from_store(row: dict[str, Any]) -> RunRecord:
    """Build a read-only runtime record from a serialized store row."""
    return RunRecord(
        run_id=row["run_id"],
        ...
        store_only=True,          # ← 标记：这条记录没有活 task
        ...
    )
```

`store_only=True` 的字面意思："我这条记录只是从存储里读出来的，**本进程没有对应的活 agent task 在跑**。" 它和姊妹篇麻烦 4 里反复出现的 `store_only` 是同一个东西——姊妹篇是从 StreamBridge 的角度提它（"跨进程能不能读到事件流"），本篇从 RunManager 的角度讲透它**怎么来的**：**就是从数据库反序列化出来的那一瞬间打上的标记。**

为什么需要这个标记？因为"有 record"和"有活 task"是两回事：

- 本进程创建并启动的 run：内存 `_runs` 里有，`record.task` 是个活 task，`store_only=False`。这种 run 你能取消（能拿到 task）、能 stream 事件（task 在产出）。
- 重启后从数据库读出来的 run：内存 `_runs` 里没有，现读现造了个 record，**`record.task` 是 `None`**，`store_only=True`。这种 run 你**不能取消**（没 task 可 cancel）、在内存 bridge 模式下也 stream 不到事件（task 早死了）。姊妹篇麻烦 4 那道 `if record.store_only and not bridge.supports_cross_process: raise 409` 守卫，查的就是这个标志。

#### 写两层：状态变化的持久化

run 的状态每次变化（pending→running、running→success 等），RunManager 都会调 `_persist_status`（`manager.py:337-376`）把新状态写进数据库。这里有个**守护逻辑**值得讲——它对应 0.5 说的"状态机合法性"：

数据库的 `update_status`（`sql.py:208-222`）不是无条件 UPDATE，而是带个 WHERE 守卫：

```python
async def update_status(self, run_id, status, *, error=None, stop_reason=None) -> bool:
    ...
    async with self._sf() as session:
        result = await session.execute(
            update(RunRow)
            .where(RunRow.run_id == run_id,
                   RunRow.status.in_(("pending", "running", "interrupted")))   # ← 守卫
            .values(**values)
        )
        await session.commit()
        return result.rowcount != 0
```

逐行看 `.where(...)` 那个守卫：只有当数据库里这行 run 的**当前状态**还是 `pending/running/interrupted`（活动态）时，才允许 UPDATE。如果已经是 `success/error`（终态），UPDATE 匹配 0 行，`rowcount == 0`，返回 `False`。

**这守护了什么？** 守护"终态不可变"。设想一个竞态：W1 上的 run 刚跑完标成 `success`，与此同时 W2 误以为它死了、走接管流程要把它标成 `error`（麻烦 5 会讲）。如果没有这个守卫，W2 的晚到写会**覆盖** W1 的 success，run 就从"成功"被改成了"出错"——明明成功了却显示错误，用户懵了。有了守卫，W2 的 UPDATE 因为状态已是终态而匹配 0 行，success 得以保全。这就是 0.5 强调的"状态机"在代码里的落地。

---

### 麻烦 3：同一个 thread 不能并发跑两个 run（多任务策略）

#### 场景

用户在一个 thread 里已经发了一条消息，agent 正在跑（还没跑完）。这时用户**又发了一条**——可能是手抖连点，也可能是故意想"打断重来"。后端怎么办？

DeerFlow 给了三种策略（`multitask_strategy`）：

- **`reject`（默认）**：拒绝新 run。返回 409，告诉前端"上一条还没跑完呢"。前端通常弹个提示。
- **`interrupt`**：打断旧 run（保留它的 checkpoint），启动新 run。旧 run 标成 `interrupted`，新 run 接着跑。
- **`rollback`**：回滚旧 run（撤销它的 checkpoint，回到这次 run 之前的状态），启动新 run。比 interrupt 更彻底——像旧 run 从没发生过。

不管哪种策略，**底线是：同一时刻同一 thread 只能有一个活动 run**。为什么？因为 agent 会持续写 checkpoint（姊妹篇 §0.7 讲过），两个 agent 同时写同一个 thread 的 checkpoint，数据必然损坏。

#### 朴素写法为什么做不到

朴素写法里每个请求各自 `asyncio.create_task(agent.run())`，**互不通信**。两个请求同时到达，各自创建 run、各自启动 agent，谁也不知道对方的存在——于是两个 agent 同时往同一个 checkpoint 写，撞车。这正是 0.7 讲的"并发竞争"。

#### RunManager 怎么解决的：`create_or_reject` 的三步原子操作

创建 run 走的是 `create_or_reject`（`manager.py:920-1086`）。它是个**精心设计的三步原子操作**，核心思路是"先在数据库层保证唯一，再在内存层登记"。先看骨架（精简后）：

```python
async def create_or_reject(self, thread_id, ...):
    run_id = str(uuid.uuid4())
    lease_expires_at = self._compute_lease_expires_at()
    record = RunRecord(run_id=run_id, ..., owner_worker_id=self._worker_id, ...)

    async with self._lock:                                      # ① 持本地锁
        local_inflight = [r for r in self._thread_records_locked(thread_id)
                          if r.status in (pending, running) or r.finalizing]
        if multitask_strategy == "reject" and local_inflight:
            raise ConflictError(...)                            # 本地已有 → 拒绝

        if self._store is not None:                             # ② 持锁的同时写数据库
            await self._store.create_run_atomic(...)            #    数据库是跨进程真相之源

        self._runs[run_id] = record                             # ③ 数据库成功后，才登记本地
        self._index_run_locked(record)

        if multitask_strategy in ("interrupt", "rollback"):     # interrupt/rollback：取消本地旧 run
            for r in local_inflight:
                r.abort_event.set()
                if r.task: r.task.cancel()
                r.status = RunStatus.interrupted

    # 锁外：把被取消的旧 run 状态落库
    for r in interrupted_records:
        await self._persist_status(r, RunStatus.interrupted)

    return record
```

逐步解释这个三步为什么这么设计：

**① 本地 inflight 检查（同 worker 守卫）。** 先在持锁状态下，查本进程内存里这个 thread 有没有活动 run。这是**快路径**——绝大多数情况，同 thread 的 run 都在同一个 worker（粘性会话），内存里一查就知道。如果策略是 reject 且本地有 inflight，直接 `raise ConflictError`，根本不碰数据库。

**② 持本地锁的同时写数据库（跨进程原子性的关键）。** 这是整个方法最精妙的一步。注意 `await self._store.create_run_atomic(...)` 是**在 `async with self._lock` 内部**执行的——也就是说，**持着本地锁去等数据库**。

为什么这么设计？docstring（`manager.py:938-944`）说了：

> Lock ordering invariant: the local `self._lock` is held across the local check, the store insert, and the local register, so the store insert can never succeed while a same-worker ConflictError is about to fire.

翻译：**"锁序不变量"——本地检查、数据库插入、本地登记三步都在同一把锁内完成。这样数据库插入永远不会在"本地即将抛 ConflictError"的同时成功**（否则会在数据库里留下一条孤儿 pending 行）。

打个比方：你要往账本（数据库）登记一笔新订单，但你得先确认本班次（本 worker）没有冲突订单。如果"确认"和"登记"中间**松手**了（让出锁），别的协程可能插进来，结果你这边确认完准备登记、那边也确认完准备登记，两人都往账本写了——撞了。所以必须**攥着锁一气呵成**：确认→登记→记本地，全程不松手。

那跨进程的并发怎么防？靠数据库的**部分唯一索引**（0.7 讲过）。`create_run_atomic` 在数据库层做 INSERT，撞了 `uq_runs_thread_active` 索引（同 thread 同时只能有一个 pending/running）就报 IntegrityError，RunManager 把它翻译成 `ConflictError`（`manager.py:1015-1018`）。**数据库是跨进程的真相之源**——即使两个 worker 同时通过了各自的本地检查，数据库这边也只有一个能 INSERT 成功。

**③ 数据库成功后，才登记本地。** 注意顺序：`self._runs[run_id] = record` 在 `create_run_atomic` **之后**。这保证了"**内存里能看到的 run，数据库里一定有**"——这是个**可见性边界**（`_persist_new_run_to_store` 的 docstring，`manager.py:313-321` 讲了这个原则）。反过来不成立（数据库里有、内存里可能没有，因为别的进程建的），但那由 `get` 方法的"内存查不到就回退数据库"兜住。

#### 部分唯一索引——数据库层的并发守卫

看一下这个索引的定义（`backend/packages/harness/deerflow/persistence/run/model.py:55-68`）：

```python
__table_args__ = (
    Index("ix_runs_thread_status", "thread_id", "status"),
    Index("ix_runs_lease", "lease_expires_at"),
    # Cross-process atomicity guarantee: at most one pending/running run per
    # thread. Must live in ORM ``__table_args__`` ...
    Index(
        "uq_runs_thread_active",
        "thread_id",
        unique=True,                                            # ← 唯一索引
        sqlite_where=text("status IN ('pending', 'running')"),  # ← 只对活动行生效
        postgresql_where=text("status IN ('pending', 'running')"),
    ),
)
```

逐行：

- `Index("uq_runs_thread_active", "thread_id", unique=True, ...)` 创建一个叫 `uq_runs_thread_active` 的索引，建在 `thread_id` 列上，`unique=True` 表示唯一。
- `sqlite_where=...` / `postgresql_where=...` 这是**部分索引**的关键——这个唯一约束**只对满足 `status IN ('pending', 'running')` 的行生效**。也就是说，只有"活动 run"受唯一约束；历史 run（success/error 等）不参与约束，所以一个 thread 可以存成百上千条历史 run。

效果：数据库**从物理上保证**，同一 thread 同时最多只有一行 status 是 pending/running。两个 worker 同时 INSERT，第二个必撞约束。这是麻烦 3 跨进程安全的地基。

注释里那句"Must live in ORM `__table_args__` (not just the migration)"也值得注意——因为这个索引必须在"空库 bootstrap"路径（`create_all + stamp head`，不走 migration）里也存在，所以既写在 ORM 模型里、又写在 migration `0004_run_ownership.py` 里，两边都定义。

朴素写法没有任何并发守卫——两个请求各跑各的 agent，checkpoint 必然撞车。RunManager 用"本地锁 + 数据库唯一索引"的双重保障把这件事管死了。

---

### 麻烦 4：多 worker 下，run 归谁所有（lease + heartbeat）

#### 场景

生产部署开了多 worker（W1、W2、W3），nginx 把请求分发到不同 worker。W1 上创建了一个 run（agent task 在 W1 的事件循环里跑）。现在：

- W2 收到一个"取消 run R"的请求。W2 怎么知道 R 还在不在跑？归谁管？W1 还活着吗？
- W3 重启了，发现数据库里 R 还标着 running，但它知道 R 不在自己这儿——R 到底还活着吗？

核心问题：**进程内存隔离（0.2 讲过），W2 看不到 W1 的内存，没法直接知道 W1 的 task 还在不在。** 得有个"跨进程的存活信号"。

#### 朴素写法为什么做不到

朴素写法连单进程注册表都没有，更别提跨进程协调。run 只活在创建它的那个进程的局部变量里，别的进程**根本不知道它存在**。

#### RunManager 怎么解决的：lease（租约）+ heartbeat（心跳）

这就是 0.6 讲的 lease 机制上场了。每个 run 在数据库的 `runs` 表里有两列（`model.py:48-50`，migration `0004` 加的）：

```python
# Multi-worker run ownership
owner_worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

- `owner_worker_id`：**谁拥有这个 run**（哪个 worker 在跑它的 agent task）。
- `lease_expires_at`：**租约何时过期**。

run 创建时，`create_or_reject` 会算出这两个值塞进 record（`manager.py:953, 970-971`）。`owner_worker_id` 是本进程的 `_worker_id`，`lease_expires_at` 由 `_compute_lease_expires_at`（`manager.py:905-918`）算：

```python
def _compute_lease_expires_at(self) -> str | None:
    if self._run_ownership_config is None:
        return None
    if not self._run_ownership_config.heartbeat_enabled:
        return None                                          # 单 worker 模式：不发租约
    lease_seconds = self._run_ownership_config.lease_seconds
    return (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
```

逐行：

- 两个 `if ... return None`：如果没配 ownership，或者**没开心跳**（单 worker 模式），返回 `None`——即 NULL lease。这很重要：NULL lease 在 `is_lease_expired` 里被当成"已过期"（`time.py:31-32`），这样单 worker 重启后，所有 inflight run 都会被对账当孤儿回收（见麻烦 5），保留了"单 worker 也要恢复孤儿"的老行为。
- 否则返回"当前时间 + lease_seconds（默认 30 秒）"的 ISO 时间戳。

#### 心跳循环——周期性续租

租约会过期，所以拥有者得**不停地续租**，证明"我还活着"。这就是心跳。看 `_heartbeat_loop`（`manager.py:1222-1264`）：

```python
async def _heartbeat_loop(self) -> None:
    if self._run_ownership_config is None or self._heartbeat_stop is None:
        return
    lease_seconds = self._run_ownership_config.lease_seconds
    interval = max(1, lease_seconds // 3)                    # 续租间隔 = lease/3
    stop = self._heartbeat_stop
    cycle = 0

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)   # 等 interval 或被叫停
            break                                            # 被叫停 → 退出
        except TimeoutError:
            pass                                             # interval 到了 → 继续往下

        cycle += 1
        try:
            await self._renew_leases()                       # ★ 每 interval 续租一次
        except Exception:
            logger.warning("Heartbeat renewal cycle failed", exc_info=True)

        if cycle % 3 == 0:                                   # 每 3 个周期对账一次
            try:
                await self._reconcile_orphans_periodic()
            except Exception:
                logger.warning("Periodic orphan reconciliation failed", exc_info=True)
```

逐行讲关键的：

- `interval = max(1, lease_seconds // 3)` 续租间隔是 `lease_seconds / 3`。默认 lease 30 秒，所以每 10 秒续一次。**为什么是 1/3 而不是更接近 lease？** 留容错——如果续租恰好失败一次（网络抖动、数据库卡），还有两次机会在 lease 真正过期前续上。这是一种"心跳要远勤于过期"的工程惯例。
- `await asyncio.wait_for(stop.wait(), timeout=interval)` 这一行的妙处：它**既等 interval，又响应停止信号**。`stop.wait()` 等停止事件（应用关闭时 `stop.set()`），`timeout=interval` 限制最多等 interval 秒。哪个先到就走哪个：到点了（TimeoutError）就去续租；被叫停了（wait 返回）就 break 退出。这比 `await asyncio.sleep(interval)` 强——sleep 不响应停止信号，关闭时得傻等。
- `await self._renew_leases()` 每 interval 调一次续租（下面细讲）。
- `if cycle % 3 == 0:` 每 3 个周期（= 每 `lease_seconds` 秒）对账一次。为什么续租是 1/3 而对账是 1/1？续租是"我的 run 我自己续"，要勤；对账是"扫别人的孤儿 run"，可以懒一点——因为启动时已经全扫过一次（见麻烦 5），运行中只需要补"启动后才过期的"。
- **两个 `try/except`**：续租和对账都**包死了异常**。这极其关键——docstring（`1230-1232`）说：

  > Both operations are guarded so a transient failure cannot take the heartbeat task down — a dead heartbeat means no lease is renewed again, and every active run eventually looks orphaned to peers.

  翻译：**心跳任务绝不能因为一次偶发异常而整个挂掉**——心跳一旦挂了，就再也没人续租，所有 run 的租约都会慢慢过期，别的 worker 就会把它们全当孤儿接管（=杀掉）。这相当于自己人全军覆没。所以每个操作都 try/except，宁可这一轮续租失败（下一轮再来），也不能让心跳循环本身崩溃。

#### `_renew_leases`——续租的具体逻辑

续租实现在 `_renew_leases`（`manager.py:1266-1322`）：

```python
async def _renew_leases(self) -> None:
    if self._store is None or self._run_ownership_config is None:
        return
    lease_seconds = self._run_ownership_config.lease_seconds
    new_expiry = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()

    async with self._lock:
        active_runs = [(rid, record) for rid, record in self._runs.items()
                       if record.status in (pending, running)
                       and record.owner_worker_id == self._worker_id
                       and (record.task is None or not record.task.done())]

    for run_id, record in active_runs:
        updated = await self._store.update_lease(
            run_id, owner_worker_id=self._worker_id, lease_expires_at=new_expiry)
        if updated:
            record.lease_expires_at = new_expiry              # 续上了
        else:
            # 续不上 → 被别的 worker 接管了 → 主动停掉本地 task
            record.abort_event.set()
            if task_active: record.task.cancel()
```

逐行：

- `active_runs = [...]` 在持锁状态下，挑出**本进程拥有的、还活动的** run。三个过滤条件：状态是 pending/running、owner 是自己、task 还没结束（或 task 还没 spawn——docstring 1276-1283 解释了为什么 `task is None` 也要算：create_run_atomic 落库和 worker spawn task 之间有个窗口，这期间 run 虽然 task 是 None 但本进程仍打算执行它，不能误判成孤儿）。
- `await self._store.update_lease(...)` 对每个 run 调数据库的 `update_lease`。看这个 SQL（`sql.py:440-456`）：

  ```python
  async def update_lease(self, run_id, *, owner_worker_id, lease_expires_at) -> bool:
      ...
      result = await session.execute(
          update(RunRow)
          .where(RunRow.run_id == run_id,
                 RunRow.owner_worker_id == owner_worker_id,           # ← 只续自己的
                 RunRow.status.in_(("pending", "running")))            # ← 且还活动的
          .values(...))
      return result.rowcount != 0
  ```

  注意 WHERE 守卫：**只有当数据库里这行的 owner 还是自己、且状态还活动时，才续**。如果别人已经把 owner 改了（接管了），或者状态已经终态了，UPDATE 匹配 0 行，返回 `False`。这是个条件 UPDATE，原子地"检查 + 更新"，避免"先 SELECT 检查、再 UPDATE"中间的竞态。

- `if updated:` 续上了——更新内存里 record 的 `lease_expires_at`。
- `else:` 续不上——**说明这个 run 已经被别的 worker 接管了**（owner 变了，或状态被改成 error 了）。这时本进程要**主动停掉自己的 task**：`abort_event.set()` + `task.cancel()`。为什么？因为这个 run 已经不归自己了，继续跑就是浪费 CPU，而且跑完写状态还会**覆盖**接管者的 error 状态（虽然有 0.5 的守护，但能不撞就不撞）。docstring（1306-1310）说得很直白："Stop the local task so we don't waste CPU or overwrite the takeover status on finalisation."

#### 多 worker 的启动门槛

回到 0.8 提到的启动守卫（`deps.py:49-85`）。它保证：**开多 worker（`GATEWAY_WORKERS > 1`）必须同时满足数据库是 Postgres + 心跳开启**。docstring（54-59）给的理由：

> 1. The DB backend must be Postgres — SQLite write-locks cannot support concurrent multi-process access.
> 2. `run_ownership.heartbeat_enabled` must be True — without heartbeat, every run has a NULL lease, so reconciliation treats all inflight runs as orphans and Worker B would kill Worker A's live runs on every rolling update or scale-up.

翻译：①SQLite 的文件锁撑不住多进程并发；②不开心跳的话所有 run 都是 NULL lease（0.6 讲过 NULL = 过期），对账会把别的 worker 的活 run 全杀掉。所以这两条是开多 worker 的**硬性前提**，不满足直接 `SystemExit` 拒绝启动——宁可启动失败，也不能在生产里让 worker 互相屠杀。

朴素写法在多 worker 下彻底失效——它没有 lease、没有 owner、没有心跳，run 只活在创建进程的局部变量里，别的 worker 既看不到它、也不知道创建者还活着没。RunManager 用 lease + heartbeat 这套机制，把"run 归谁所有"和"拥有者还活着吗"这两个问题，变成了**数据库里两列的值 + 周期性 UPDATE**。

---

### 麻烦 5：worker 崩了，它的 run 谁来收尸（孤儿恢复 + 接管）

#### 场景

W1 拥有的 run R 正在跑，W1 突然 OOM 崩了（或被 kill、或网络分区）。R 在数据库里还标着 `running`，但它的 agent task 已经随 W1 一起死了——**再也不会有任何事件产出，永远不会自己结束**。

这时如果有个客户端（浏览器重连、或 IM 通道）来订阅 R 的流，会发生什么？

- **傻等**：流式端点等 `END_SENTINEL`，但 W1 死了永远不会发。客户端挂在那儿等到天荒地老。
- **状态误导**：R 还标着 running，前端以为它还在跑，但其实已经凉了。

需要一个机制：**检测到"拥有者死了"，把孤儿 run 收尸（标成 error），并通知订阅者**。

#### 朴素写法为什么做不到

朴素写法没有 lease、没有 owner 记录，**没有任何途径判断"R 的拥有者还活着吗"**。R 在数据库里永远是 running，谁也不知道它其实早死了。

#### RunManager 怎么解决的：两条收尸路径

RunManager 有两条互补的路径处理孤儿 run：

1. **启动时全量对账**（`reconcile_orphaned_inflight_runs`）——进程启动时扫一遍数据库，所有"lease 已过期但仍标 running"的 run，标成 error。
2. **运行时被动接管**（`cancel` 的非本地路径）——客户端主动来取消时，顺带检测并接管。

##### 路径 1：启动对账

看 `reconcile_orphaned_inflight_runs`（`manager.py:1088-1144`）：

```python
async def reconcile_orphaned_inflight_runs(self, *, error, before=None) -> list[RunRecord]:
    if self._store is None:
        return []
    grace_seconds = self._run_ownership_config.grace_seconds if self._run_ownership_config else 10
    rows = await self._store.list_inflight_with_expired_lease(
        before=before, grace_seconds=grace_seconds)          # ★ 数据库扫过期 lease 的活动 run

    recovered = []
    for row in rows:
        record = self._record_from_store(row)
        async with self._lock:
            live_record = self._runs.get(record.run_id)
            if live_record is not None and live_record.status in (pending, running):
                continue                                      # 本地还活着 → 跳过（防误杀自己）

        record.status = RunStatus.error
        record.error = error
        persisted = await self._persist_status(record, RunStatus.error, error=error)
        if persisted:
            recovered.append(record)
    return recovered
```

逐行：

- `rows = await self._store.list_inflight_with_expired_lease(...)` 数据库扫一遍，返回所有"status 是 pending/running 且 lease 已过期（或 NULL）"的行。SQL 在 `sql.py:479-503`，WHERE 条件是 `status IN ('pending','running') AND _lease_expired_or_null(lease_expires_at, cutoff)`，其中 `cutoff = now - grace_seconds`（0.6 讲的宽限期）。
- `for row in rows:` 逐个处理。
- `live_record = self._runs.get(record.run_id)` **防御性检查**：这条 run 在本进程内存里还活着吗？为什么需要？设想 W1 重启了，重启后它之前跑的 run R 其实在新进程里又"复活"了（checkpoint 恢复，agent 接着跑）。如果对账 blindly 把 R 标成 error，就把 W1 自己正在跑的活 run 误杀了。所以先查内存——本地还活着的跳过。
- `record.status = RunStatus.error; persisted = await self._persist_status(...)` 标成 error 并落库。注意这里复用了 0.5/麻烦 2 讲的状态守护——如果这个 run 已经被别的 worker 接管（变 error）或正常结束了（变 success），`_persist_status` 的 UPDATE 匹配 0 行，返回 False，跳过。

这个对账在应用启动时被调用（`deps.py:313-327`）：

```python
recovered_runs = await app.state.run_manager.reconcile_orphaned_inflight_runs(
    error="Gateway restarted before this run reached a durable final state.",
    before=now_iso(),
)
await _publish_recovered_run_stream_end(app.state.stream_bridge, recovered_runs, ...)
await _mark_latest_recovered_threads_error(...)
```

逐行：

- `reconcile_orphaned_inflight_runs(...)` 启动时跑一次全量对账，返回所有被收尸的 run。
- `_publish_recovered_run_stream_end(...)`（`deps.py:114`）对每个被收尸的 run 调 `bridge.publish_end(run_id)`——**给订阅者发结束信号**。这就是姊妹篇麻烦 4 §4.6 那段"孤儿 run 恢复"的具体实现：R 的 agent 死了不会自己发 END，所以 RunManager 代替它发，让所有还在 `subscribe` 等着的客户端能收到 END、正常退出，而不是傻等。
- `_mark_latest_recovered_threads_error(...)` 把这些 thread 的状态也标成 error（thread_meta 层面）。

docstring（1314-1317）说清了单/多 worker 的行为差异：

> In single-worker mode (SQLite / backend=memory), no run has a lease, so all inflight rows are reclaimed (unchanged behaviour). In multi-worker mode (Postgres), only runs with an expired lease are reclaimed; runs owned by another live worker are skipped.

翻译：单 worker 模式（没 lease）→ 所有 inflight 都回收（因为 NULL lease 全算过期，这正是保留旧行为）；多 worker 模式 → 只回收 lease 真过期的，别人还活着的 run 不动。

##### 路径 2：运行时被动接管（`cancel` 的非本地路径）

启动对账只在重启时跑一次。那两次重启之间，如果一个 worker 崩了但没人重启怎么办？运行时还有第二条路：**客户端主动来取消时，顺带接管**。

看 `cancel` 方法（`manager.py:762-903`）。它分两条路径——**本地路径**（这个 run 归本进程）和**非本地路径**（run 不在本进程，得查数据库）。本地路径就是普通的 `task.cancel()`，不展开。重点看非本地路径（`manager.py:840-903`）：

```python
# 非本地路径 —— 内存里没有这个 run，查数据库
if not self.heartbeat_enabled:
    return CancelOutcome.not_active_locally              # 单 worker 模式：不在本地就 409

row = await self._store.get(run_id)
store_status = row.get("status")
if store_status not in ("pending", "running"):
    return CancelOutcome.not_cancellable                 # 已终态：没法取消

lease_expires_at = row.get("lease_expires_at")
if not is_lease_expired(lease_expires_at, grace_seconds=grace_seconds):
    return CancelOutcome.lease_valid_elsewhere           # ★ lease 还有效：拥有者可能还活着

# lease 过期了 → 拥有者大概死了 → 接管
taken = await self._store.claim_for_takeover(
    run_id, grace_seconds=grace_seconds, error=take_over_msg)
if taken:
    return CancelOutcome.taken_over                      # 接管成功
```

逐行讲三条分支：

- `if not self.heartbeat_enabled: return not_active_locally` 单 worker 模式（没开心跳），run 不在本地内存里 = 它根本不存在（或已结束），返回 `not_active_locally`，HTTP 层映射成 409。
- `if not is_lease_expired(...): return lease_valid_elsewhere` **这是关键判断**。`is_lease_expired`（`time.py:23-39`）判断"lease 是否已过期（含宽限期）"。如果**没过期**，说明拥有者还可能活着（它的心跳还在续租），本进程不该乱动——返回 `lease_valid_elsewhere`，HTTP 层返回 **409 + Retry-After**（告诉客户端"lease 还在别处有效，过会儿重试"）。这就是姊妹篇 §4.6 提到的 409 + Retry-After 的来源。
- 否则（lease 过期了）→ 拥有者大概率死了 → `claim_for_takeover` 尝试接管。

##### `claim_for_takeover`——为什么用条件 UPDATE 而非"先读后写"

接管用的是数据库的条件 UPDATE（`sql.py:458-477`）：

```python
async def claim_for_takeover(self, run_id, *, grace_seconds, error) -> bool:
    cutoff = datetime.now(UTC) - timedelta(seconds=grace_seconds)
    result = await session.execute(
        update(RunRow)
        .where(RunRow.run_id == run_id,
               RunRow.status.in_(("pending", "running")),
               _lease_expired_or_null(RunRow.lease_expires_at, cutoff))   # ← 条件：lease 已过期
        .values(status="error", error=error, ...))
    return result.rowcount != 0
```

为什么用"带 WHERE 的条件 UPDATE"而不是"先 SELECT 检查 lease、再 UPDATE"？

**为了关闭竞态窗口。** 设想"先读后写"：

```
T1: W2 SELECT run R → lease_expires_at = 已过期（判定可接管）
T2: W1（其实还活着）心跳恰好在这一刻续租成功 → lease 变成"未过期"
T3: W2 UPDATE run R SET status='error' → 把 W1 还活着的 run 杀了！
```

T1 到 T3 之间有个**时间窗口**，W1 的续租可能在这个窗口里插进来，让 W2 的"已过期"判断失效。但 W2 还是盲目地 UPDATE 了——误杀。

条件 UPDATE 把"检查 + 更新"压成**数据库的一个原子操作**：UPDATE 的 WHERE 在数据库内部求值，求值和更新之间没有间隙。如果 W1 在 W2 的 UPDATE 到达前续租了（lease 变未过期），WHERE 条件不满足，UPDATE 匹配 0 行，返回 False——W2 自然不会误杀。这就是"条件 UPDATE 关闭竞态窗口"的威力。

回到 `cancel`，`claim_for_takeover` 返回 `taken=True` → `CancelOutcome.taken_over`；返回 `False`（lease 被续上了）→ 重新读一次区分（`manager.py:890-903`），通常归到 `lease_valid_elsewhere`。

##### HTTP 层怎么把这些 outcome 翻译成响应

看 `cancel_run` 端点（`thread_runs.py:573-615`）：

```python
@router.post("/{thread_id}/runs/{run_id}/cancel")
async def cancel_run(thread_id, run_id, request, ...):
    run_mgr = get_run_manager(request)
    record = await run_mgr.get(run_id)
    ...
    outcome = await run_mgr.cancel(run_id, action=action)

    if outcome in (CancelOutcome.cancelled, CancelOutcome.taken_over):
        return Response(status_code=202)                  # 本地取消 / 接管成功：202

    if outcome == CancelOutcome.lease_valid_elsewhere:
        await _raise_lease_valid_elsewhere(run_id, run_mgr, record)   # → 409 + Retry-After

    raise HTTPException(status_code=409, ...)             # 其它：409
```

逐行：

- `outcome = await run_mgr.cancel(...)` 调 cancel，拿到 `CancelOutcome` 枚举（`manager.py:1435-1443`）之一。
- `if outcome in (cancelled, taken_over): return 202` 两种"取消成功"的情况：`cancelled`（本地取消）和 `taken_over`（接管了死 worker 的 run）都返回 **202 Accepted**——告诉客户端"取消请求已被受理"。
- `if outcome == lease_valid_elsewhere: _raise_lease_valid_elsewhere(...)` lease 还在别处有效——返回 **409 + Retry-After**。`Retry-After` 头的值是从剩余 lease 时间算出来的，告诉客户端"多久之后再来试"。
- 其它（`not_cancellable` / `not_active_locally` / `unknown`）→ 409。

朴素写法在多 worker 下既检测不到崩溃、也无法协调接管——run 永远卡在 running 状态。RunManager 用"启动对账 + 运行时被动接管 + 条件 UPDATE 关闭竞态"这三件套，把孤儿 run 收尸这件事彻底自动化了。

---

### 麻烦 6：优雅停机（shutdown）——run 不能跟着进程一起暴毙

#### 场景

`make stop`、滚动发布、收到 SIGTERM——进程要退出了。这时进程里可能还有好几个 run 在后台跑（task 还没结束）。如果进程**直接退出**会发生什么？

agent 在执行过程中会**持续写 checkpoint**（姊妹篇 §0.7 讲过）。如果进程强退，正在写 checkpoint 的操作可能被**打断到一半**——半截 checkpoint 留在硬盘上，后面读出来当正常状态用（姊妹篇 §0.5 的 `CancelledError` 写半截的坑，在这里以另一种形式重现）。

更具体的坑：langgraph 内部有个 `_checkpointer_put_after_previous` 机制，它在**独立的后台 task** 里（不在 `run_agent` 的调用栈上）调 `checkpointer.aput(...)` 写 checkpoint。进程退出时，gateway 的 `AsyncExitStack` 会先把 checkpointer 的**连接池关掉**（比如 Postgres 连接池）。如果这时还有 run task 在 mid-graph，那个后台 `aput` 就会撞上一个**已关闭的连接池**，抛 `psycopg_pool.PoolClosed`——而且这个异常在 langgraph 内部 task 里，`run_agent` 的 try/except **抓不到**，最终冒泡成 `asyncio.run()` 关闭时的未处理异常。这就是 issue #3373。

#### 朴素写法为什么做不到

朴素写法的 task 是"野任务"，进程退出时 `asyncio` 会试图取消所有 task，但：

- 取消是**粗暴的 `CancelledError`**，可能在 checkpoint 写到一半时打断（姊妹篇 §0.5 的坑）。
- 没有任何"等它干净收尾"的机制——进程说退就退，task 爱写到哪算哪。
- 没有协调 checkpointer 连接池的关闭顺序——pool 先关、task 后写，必撞 PoolClosed。

#### RunManager 怎么解决的：`shutdown`——有界地 drain inflight run

`shutdown` 方法（`manager.py:1339-1432`）干的就是"**在 checkpointer 连接池关闭之前，让所有 inflight run 干净地停下来**"。看骨架：

```python
async def shutdown(self, *, timeout: float = 5.0) -> None:
    await self.stop_heartbeat()                              # ① 先停心跳（别和 drain 抢）
    deadline = loop.time() + timeout

    async with self._lock:
        inflight = [r for r in self._runs.values()
                    if r.status in (pending, running) and r.task and not r.task.done()]
        for record in inflight:
            record.abort_action = "interrupt"
            record.abort_event.set()                         # 按取消铃
            record.task.cancel()                             # 取消 task

    if not inflight:
        return

    tasks = [r.task for r in inflight]
    _, pending = await asyncio.wait(tasks, timeout=timeout)  # ② 有界等待它们收尾

    # ③ 没自己结束的，标 interrupted 落库
    async with self._lock:
        for record in inflight:
            task = record.task
            if task not in pending and not task.cancelled():
                task.exception()                             # 自己结束的：保留它的真实状态
                continue
            if record.status in (pending, running):
                record.status = RunStatus.interrupted
            to_persist.append(record)

    # ④ 有界地把状态落库（受 timeout 预算约束）
    if to_persist:
        remaining = deadline - loop.time()
        await asyncio.wait_for(asyncio.gather(...), timeout=remaining)
```

逐步解释：

**① `await self.stop_heartbeat()`** 先停心跳。为什么？心跳（麻烦 4）会周期性 `update_lease`，如果 drain 进行中它还在跑，可能和 drain 的状态写入撞车。先停掉，避免干扰。

**② `await asyncio.wait(tasks, timeout=timeout)`** 这是核心。`asyncio.wait` 等一组 task 完成，最多等 `timeout`（默认 5 秒）。返回 `(done, pending)`——哪些在 timeout 内结束了，哪些还在跑。

为什么是"**有界等待**"而不是无限等？docstring（1362-1366）说：

> The whole drain, including the trailing status persistence, is bounded by ``timeout`` so a run stuck in cleanup (or a slow store under DB pressure) cannot hang worker shutdown

翻译：**整个 drain（含落库）都受 timeout 约束**，这样一个卡在清理里的 run（或数据库压力大时慢吞吞的 store）不会拖死整个进程退出。进程退出是有时间预算的（比如 uvicorn 的 graceful shutdown 超时），drain 必须在这个预算内完成，否则会触发更暴力的信号风暴。

注意前面 `record.abort_event.set()` + `record.task.cancel()`——这走的是和麻烦 1 一样的"**主动取消**"路径（姊妹篇讲过的"主动检查比被动接异常好"）。task 收到 cancel 后，agent worker 的 `run_agent` 会在循环间隙检查到 `abort_event`，**干净地写完最后一个 checkpoint** 再退出，而不是写到一半被硬切。

**③ 区分"自己结束的"和"被迫中断的"。** 这一步很细腻：

```python
if task not in pending and not task.cancelled():
    task.exception()                  # 自己结束的：retrieve 异常，保留真实状态
    continue
if record.status in (pending, running):
    record.status = RunStatus.interrupted
```

- 如果一个 run 在 timeout 内**自己跑完了**（比如恰好成功结束），它的 `record.status` 已经被 `run_agent` 设成 `success`——这种要**保留真实状态**，不能粗暴覆盖成 interrupted。docstring（1358-1361）专门强调："a run that completes (e.g. success) during the drain keeps its real terminal status instead of being blanket-overwritten."
- `task.exception()` 这一行的作用：retrieve 那个自己结束的 task 可能抛出的异常，避免它被报为"never retrieved"（asyncio 的一个坑——done 的 task 如果有未取的异常会告警）。这里取一下就是"消费掉"它。
- 只有**没自己结束的**（还在 pending，或被 cancel 了）才标 `interrupted`。

**④ 有界落库。** 最后把状态写进数据库，也包在 `asyncio.wait_for(..., timeout=remaining)` 里——`remaining` 是 timeout 减去已经用掉的时间，即"还剩多少预算"。docstring（1404-1407）解释：`_call_store_with_retry` 在数据库压力大时会指数退避（backoff），可能很慢，必须限制它不能把 shutdown 拖过 budget。

#### 调用顺序：drain 必须在 checkpointer 关闭之前

`shutdown` 在哪里被调？看 `deps.py` 的生命周期（`deps.py:332-343`）：

```python
try:
    yield                                                     # 应用正常运行
finally:
    # Drain in-flight run tasks BEFORE the AsyncExitStack tears down the
    # checkpointer (and its connection pool). ...
    run_manager = getattr(app.state, "run_manager", None)
    if run_manager is not None:
        await _drain_inflight_runs(run_manager)               # ★ 先 drain
    await close_engine()                                      # 再关 checkpointer
```

逐行：

- `try: yield` 这是 `langgraph_runtime` 这个 async context manager 的"正常服务期"——应用跑在这里。
- `finally:` 应用关闭时进这里。
- `await _drain_inflight_runs(run_manager)` **先** drain 所有 inflight run。这一步在 `close_engine()` **之前**——这正是 issue #3373 的修复要点：run task 还在写 checkpoint 时，连接池**还没关**，所以它们能干净地把最后一个 checkpoint flush 出去。
- `await close_engine()` **再**关 checkpointer 引擎（及其连接池）。这时所有 run task 都已 drain 完，不会再有 `aput` 撞已关闭的 pool。

`_drain_inflight_runs` 本身（`deps.py:88-111`）还套了一层 `asyncio.shield`——防止 drain 这个 task 自己被取消（比如连续两次 SIGINT、或 graceful shutdown 超时）：

```python
async def _drain_inflight_runs(run_manager: RunManager) -> None:
    drain = asyncio.create_task(run_manager.shutdown(timeout=_RUN_DRAIN_TIMEOUT_SECONDS))
    try:
        await asyncio.shield(drain)                           # shield：保护 drain 不被外部取消
    except asyncio.CancelledError:
        try:
            await asyncio.shield(drain)                       # 被取消也要等 drain 跑完（它是有界的）
        except Exception:
            logger.exception(...)
        raise
```

`asyncio.shield(drain)` 的作用：即使外层（生命周期协程）被取消了，`drain` 这个 task **不会被取消**，会继续跑完。为什么这么执着？因为 drain 是有界的（最多 5 秒），让它跑完不会无限挂；但如果中途被取消，就又回到了"checkpointer 还没关、run task 还在写"的 #3373 状态。所以宁可多等几秒，也要让 drain 干净跑完。

朴素写法的野任务在进程退出时既不会干净收尾、也不协调资源关闭顺序，必然踩 `CancelledError` 写半截 checkpoint 或 PoolClosed 的坑。RunManager 的 `shutdown` 把"有序 drain + 有界等待 + 状态区分 + 资源关闭顺序"全管起来了。

---

## 第 3 步：把 6 个麻烦的本质串起来

朴素写法 `asyncio.create_task(agent.run())` 的根本问题是：

> **它把"run 的执行生命周期"焊死在了"创建它的那次 HTTP 请求"的局部作用域里——请求一返回，run 就失联了。**

而现实里，run 的生命周期和 HTTP 请求的生命周期是**完全不同步**的两件事：

| 维度 | HTTP 请求 | agent run |
|---|---|---|
| 寿命 | 几秒到几分钟，处理完即结束 | 一次完整执行，可能跑几十秒到几分钟，结束后还要被查询很久 |
| 数量 | 1 个客户端 1 次请求 | 一个 run 会被多次后续请求观察（查状态、取消、join 流） |
| 位置 | 落在某个 worker 进程 | 可能在同进程，也可能跨进程（多 worker） |
| 可恢复 | 请求结束就没了 | 有 checkpoint + 数据库记录，能跨重启查、能恢复 |
| 终止 | 响应返回即终止 | 有状态机，要干净地从活动态过渡到终态 |

把两者焊死，就会出现 6 个麻烦——因为现实里它们的生命周期**就是会错位**：

- ① run 要活得比创建请求久 → 第二次请求来查，得有地方查（注册表）
- ② 进程重启后 run 历史不能丢 → 得有持久化层
- ③ 同一 thread 不能并发跑两个 run → 得有并发守卫
- ④ 多 worker 下 run 归谁所有 → 得有 lease + 心跳
- ⑤ worker 崩了 run 要被收尸 → 得有孤儿恢复 + 接管
- ⑥ 进程退出 run 不能暴毙 → 得有有序 drain

`RunManager` 就是**把 run 的执行生命周期和 HTTP 请求的生命周期解耦**的那个中间层。它具体做的事：

- **进程级注册表** `_runs`：让 run 活过创建它的那次请求，任何后续请求都能按 run_id 找到它（麻烦 1）。
- **双层架构** 内存 + 数据库：让 run 的元信息跨重启存活、跨进程可见（麻烦 2）。
- **原子创建 + 唯一索引**：保证同 thread 同时只有一个活动 run（麻烦 3）。
- **lease + heartbeat**：在跨进程的数据库里表达"run 归谁、拥有者还活着吗"（麻烦 4）。
- **对账 + 条件 UPDATE 接管**：自动收尸崩溃 worker 留下的孤儿 run（麻烦 5）。
- **有序 drain**：进程退出前让 run 干净停下来，协调资源关闭顺序（麻烦 6）。

代码上，这一切都从那一行开始：

```python
run_mgr = get_run_manager(request)
```

它从 `app.state` 取出那个**进程级单例**——RunManager 实例。这个实例在应用启动时构造一次（`deps.py:309-312`），挂着内存注册表、数据库后端、worker ID、心跳配置；在应用关闭时通过 `shutdown` 干净 drain 所有 inflight run（`deps.py:340-342`）。中间所有跟 run 生命周期相关的操作——创建、查询、取消、列表、接管、恢复——都经它中转。

所以它不是"代码好看"或"封装一下"——是这 6 个麻烦里**任意一个**单拿出来，朴素写法都过不去。它和姊妹篇的 `StreamBridge` 是同一类东西的两面：

- **`StreamBridge` 解耦"事件流"的生命周期**——agent 产出的事件要被任意数量、任意时刻、跨进程的 HTTP 订阅者消费。
- **`RunManager` 解耦"执行 + 状态"的生命周期**——agent 的 run 要活过创建请求、跨重启可查、跨 worker 可协调、能被干净取消和收尸。

两者一起，把"一次 HTTP 请求里启动的 agent run"从请求的短暂生命周期里彻底解放出来，让它成为一个**独立的、有状态的、可被多方协调的**长期实体。这就是 `run_mgr = get_run_manager(request)` 存在的全部理由。
