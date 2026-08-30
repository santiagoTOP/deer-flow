# get_checkpointer 深度讲解：为什么不能直接在路由里 `InMemorySaver()` 就完事

本文是 `run-manager-deep-dive.md` / `stream-bridge-deep-dive.md` 的姊妹篇。那两篇讲的是 `run_mgr = get_run_manager(request)` 和 `bridge = get_stream_bridge(request)` 为什么存在；这篇讲的是紧挨着它们的另一行——`checkpointer = get_checkpointer(request)` 为什么存在。

具体到源头，本文讲的就是 `backend/app/gateway/deps.py:366` 这一行：

```python
get_checkpointer: Callable[[Request], Checkpointer] = _require("checkpointer", "Checkpointer")
```

它和 `get_run_manager`（`deps.py:365`）、`get_stream_bridge`（`deps.py:364`）是同一行连着的三个兄弟声明。三篇文章回答的是同一个大问题的三个面：**一次 HTTP 请求里启动的 agent run，凭什么能活得比这次请求久？** StreamBridge 负责"事件流"这一半，RunManager 负责"执行 + 状态"这一半，而 `get_checkpointer` 负责的是更底下那一层——**"agent 的存档（checkpoint）该存在哪儿、谁来开这个数据库连接、进程死了/重启了存档还留不留得住"**。建议三篇对照着读。

本文面向第一次接触这套架构的同学。先把所有需要的前置概念一个一个铺出来，然后再讲 6 个麻烦，每段代码逐行解释。

---

## 第 0 步：先把要用的术语讲清楚

在讲任何代码之前，先把后面会反复出现的概念讲明白。这些是理解"为什么朴素写法会出问题"的地基。已经读过姊妹篇 `run-manager-deep-dive.md` / `stream-bridge-deep-dive.md` 的同学可以快速跳过带 ※ 标记的小节（那里会注明"姊妹篇讲过"，本篇只复用结论）。

### 0.1 Checkpointer——agent 的"存档"

※ 概念基础和姊妹篇 `stream-bridge-deep-dive.md` §0.7 重叠，本篇只复用结论。

LangGraph 跑一个 agent，不是"一口气跑完"，而是**一步、一步地跑**——每跑完一步（一个节点、一次模型调用），就把自己当时的完整状态（消息历史、变量、当前走到哪了）**存一份快照**。这份快照就叫 **checkpoint**，干这件事的东西就叫 **checkpointer**。

打个比方：checkpointer 就像玩游戏时的**"存档"功能**——你打到一个关卡（跑完一步），按一下存档（写一个 checkpoint），即使断电（进程崩溃）、下次开机（重启进程）也能从存档点继续打（恢复 agent 状态、接着跑）。

DeerFlow 里 checkpointer 是按 `thread_id`（哪段对话）来分的：每个 thread 有自己一条 checkpoint 链。`thread_id` 一固定，这个 thread 的所有存档就都串在一起。

关键点：**checkpointer 是一个"会真正去读写存储"的对象**——它背后要么是进程内存，要么是一个数据库连接（SQLite / Postgres）。它不是一个无副作用的数据结构。这就埋下了后面所有麻烦的根：一个"会占用数据库连接"的东西，不能像 `dict()` 一样随手 new。

### 0.2 `BaseCheckpointSaver` 的三件套——存档机的按钮

checkpointer 在 LangGraph 里是一个接口（`BaseCheckpointSaver` 的子类），不管后端是内存还是数据库，对外都暴露同一组按钮。这一篇里会反复出现的是这三个异步方法：

- `aget_tuple(config)`——**读最新一档**。给定一个 `{"configurable": {"thread_id": ...}}`，返回这个 thread 最近一次的存档（没有就返回 `None`）。
- `aput(config, checkpoint, metadata, new_versions)`——**写一档**。把当前状态作为一个新 checkpoint 存进去。
- `aput_writes(config, writes, task_id)`——**写增量**。把某一步里产生的若干条"写入"（哪个 node 写了什么）追加进去，方便断点续跑。

打个比方：这三件套就像存档机上的三个按钮——**"读档"、"存档"、"追加记录"**。checkpointer 是机器，这三个方法是按钮；不管机器背后是硬盘还是云端，按钮都长一样。

后面你会看到，DeerFlow 的路由代码（比如 `backend/packages/harness/deerflow/runtime/goal.py:434-447`）就是按这三个按钮来确保一个 thread 的根 checkpoint 存在、把 goal 状态写进去。这就是 `get_checkpointer(request)` 拿回来的东西最终被怎么用。

### 0.3 `app.state`——进程级"工具箱"

FastAPI 应用对象 `app` 上挂着一个属性叫 `app.state`，类型是 Starlette 的 `State`。它就是一个**进程级的"工具箱"**：你在上面随手塞东西、随手取东西，对象会跟着 `app` 一起活到进程结束。

打个比方：`app.state` 就像餐厅前台桌子底下那个**公共工具箱**——所有服务员（路由函数）都能伸手进去拿剪刀、拿胶带（拿 checkpointer、拿 run_manager），不用各自从家里带。工具箱是进程级别的：一个 worker 进程就有一个工具箱，进程死了工具箱也跟着没了。

这一篇里最重要的一件事就是：**checkpointer 在启动时被"放进工具箱"（`app.state.checkpointer = ...`），在每个请求里被"从工具箱拿出来"（`get_checkpointer(request)`）**。放进去和拿出来的代码，后面会逐行讲。

⚠️ 注意"进程级"这三个字。它意味着 **多 worker 部署时，每个 worker 有自己独立的一个工具箱、独立的一个 checkpointer 实例**。这正是后面麻烦 5（多 worker）的根——多个进程各自开连接，必须连到同一个外部数据库，不能各开一个 SQLite 文件互不通信。

### 0.4 FastAPI 的 lifespan——开机/关机的总闸

※ 概念基础和姊妹篇重叠，这里聚焦在"checkpointer 的生命周期挂在 lifespan 上"这一点。

FastAPI 给了一个机制叫 **lifespan**：你给它一个 `async` 生成器函数，它会在**应用启动时执行 `yield` 之前的那段代码，应用关闭时执行 `yield` 之后的那段代码**。这就给了一个"开机做初始化、关机做清理"的总闸。

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # —— 这里在开机时跑 ——
    app.state.checkpointer = ...   # 建 checkpointer、放进工具箱
    yield                          # ★ 应用在这里活着，正常处理请求
    # —— 这里在关机时跑 ——
    ...                            # 关掉 checkpointer 的数据库连接
```

DeerFlow 的 lifespan 在 `backend/app/gateway/app.py:171` 定义，里面 `yield` 之前调用 `langgraph_runtime(app, startup_config)`（`deps.py:222`）来建所有的运行时单例——checkpointer 就是其中之一。打个比方：lifespan 就是餐厅**"开门营业 / 打烊关门"的总闸**——开门时把锅碗瓢盆摆好（建 checkpointer）、打烊时洗干净收起来（关连接）。`get_checkpointer(request)` 之所以能工作，前提就是"开门时已经把它摆好了"。

### 0.5 `AsyncExitStack`——一摞"用完要还"的资源

※ 概念基础和姊妹篇重叠，这里聚焦在"checkpointer 的关机回收靠它"。

Python 标准库里有个东西叫 `ExitStack`，异步版叫 `AsyncExitStack`。它干的事很简单：**你把一堆"用完要还"的东西（上下文管理器）依次 `enter` 进去，它保证在退出时按相反顺序逐个 `close` 掉**。

打个比方：`AsyncExitStack` 就像一摞**"用完要还的餐具盘"**——你每拿一个盘子（建一个资源）就摞上去，吃完之后食堂大妈会从最上面那个开始一个个收（关资源），保证每个都不落下、顺序也对。

DeerFlow 里 checkpointer 是这么进去的（`deps.py:267`）：

```python
app.state.checkpointer = await stack.enter_async_context(make_checkpointer(config))
```

这句话是"建一个 checkpointer、把它的生命周期托付给这摞盘子"。`AsyncExitStack` 在 lifespan 关机时（`deps.py:258` 那个 `async with` 退出时）会自动把它的数据库连接关掉。这就是后面麻烦 4（关机回收）的机制。

### 0.6 `@asynccontextmanager`——能"开/关"的工厂

`make_checkpointer` 这个函数头上有个装饰器 `@asynccontextmanager`（`async_provider.py:167`）。它的作用是把一个"内部有 `yield` 的普通 async 函数"变成一个**"能开能关"的上下文管理器**——调用它得到一个对象，`async with` 进去时跑到 `yield`（产出资源），退出时跑 `yield` 之后的清理代码。

```python
@contextlib.asynccontextmanager
async def make_checkpointer(app_config) -> AsyncIterator[Checkpointer]:
    # —— 进 async with 时跑：建连接 ——
    ...
    yield saver          # ★ 产出 checkpointer，外面用
    # —— 出 async with 时跑：关连接 ——
```

打个比方：`@asynccontextmanager` 就像一个**"会自己开门关门的车库"**——你说"开门"（`async with`），它把车（checkpointer）推出来给你用；你说"关门"（退出 `async with`），它顺手把车推回去、锁好（关连接）。它和 §0.5 的 `AsyncExitStack` 是配套的：`AsyncExitStack` 负责记住"该关哪些门"，`@asynccontextmanager` 负责"门本身会怎么关"。

为什么 checkpointer 要用这个？因为它**建的时候是异步的**（开数据库连接要 `await`，不能在 import 时跑），**关的时候也要清理**（释放连接池）。这正好是 `@asynccontextmanager` 的拿手好戏。后面麻烦 2 会展开。

### 0.7 HTTP 503——"还没准备好"的信号

HTTP 状态码 503 的官方含义是 **Service Unavailable**——"服务（暂时）不可用"。FastAPI 里要返回它，写 `raise HTTPException(status_code=503, detail="...")`。

打个比方：503 就像餐厅门口挂的**"正在准备中，暂不营业"**牌子——不是你点错菜了（那是 404），不是你不许进来（那是 403），是**餐厅本身还没开张**。客人（客户端）看到这个牌子的正常反应是"过会儿再来"，而不是"我哪儿做错了"。

DeerFlow 里，`_require` 工厂拿不到 checkpointer 时就抛 503（`deps.py:357`）。语义是：**"这个进程的 checkpointer 还没建好（或建失败了），所以这个请求没法服务，但你换一个健康的进程、或者重启一下就好了"**。这是个比 `NoneType has no attribute 'aget_tuple'`（深层的、把人看懵的崩溃）友好得多的信号。后面麻烦 6 会展开。

### 0.8 worker / `GATEWAY_WORKERS`——进程数

※ 概念基础和姊妹篇重叠，这里聚焦在"checkpointer 的后端选择受 worker 数制约"。

部署 DeerFlow 时，可以用多个进程（worker）来跑 Gateway，每个进程都能处理请求。进程数由环境变量 `GATEWAY_WORKERS` 控制，默认是 1。

打个比方：worker 就像餐厅**雇佣了几个厨师**——每个厨师独立做菜（独立进程），但他们得共用同一口大锅（同一个外部数据库）才能让客人无论找哪个厨师都能看到一致的菜单进度。

关键点：**多 worker 下，每个进程的 `app.state.checkpointer` 是各自独立的实例，但它们必须连到同一个外部 Postgres**——否则 A 厨师存的档，B 厨师根本看不到。而 SQLite 是一个**文件**，多个进程同时写一个文件会互相踩，所以多 worker 时强制要求 Postgres。这就是后面麻烦 5（以及 `deps.py:49-80` 的 `_enforce_postgres_for_multi_worker`）的根。

---

好，前置概念讲完了。现在我们开始讲代码。

## 第 1 步：朴素写法长什么样

如果没有 `get_checkpointer`，一个"给某 thread 写入 goal"的路由大概会写成这样（**这是反面教材，真实代码不是这样**）：

```python
from langgraph.checkpoint.memory import InMemorySaver   # 每次请求现 import 现建

@router.put("/{thread_id}/goal")
async def set_thread_goal(thread_id: str, body: ThreadGoalRequest):
    checkpointer = InMemorySaver()                        # ← 每个请求 new 一个新的
    await write_thread_goal(checkpointer, thread_id, goal)# 用完就扔，函数返回它就被 GC
    return ThreadGoalResponse(goal=goal)
```

逐行解释：

- **第 1 行** `from langgraph.checkpoint.memory import InMemorySaver` 每次请求都重新引入内存版 saver。
- **第 4 行** `checkpointer = InMemorySaver()` 这是核心反面——**每个请求都 new 一个全新的 checkpointer**。
- **第 5 行** `await write_thread_goal(checkpointer, ...)` 用它写 goal，写到这个 checkpointer 的内存里。
- **函数返回后** 这个 `checkpointer` 没人引用了，连同它内存里刚写下的 goal 一起被垃圾回收——**白写了**。

**这几行代码的根本问题**：`checkpointer` 被当成了"一个随手 new 的临时对象"，而**不是一个"有生命周期的、要占用资源的、要跨请求共享的进程级单例"**。

它派生的具体症状：

- 用内存版（`InMemorySaver`）→ 函数一返回存档就没了，下个请求来取根本取不到。
- 用数据库版（`AsyncPostgresSaver`）→ 每个请求都开一次连接池，几十个请求就把数据库连接打满。
- 想"开机建一次、进程内共享" → 不知道建好的实例往哪儿放、怎么让路由拿到。
- 想换后端（测试用内存、生产用 Postgres）→ 写死在路由里，改不动。

下面 6 个麻烦，全都是从"checkpointer 是进程级资源、不能随手 new"这一个病根派生的。`get_checkpointer(request)` + `_require` 工厂 + lifespan 绑定，就是来拔这个病根的。

---

## 第 2 步：6 个麻烦逐一展开

### 麻烦 1：真正的后端要开数据库连接，不能每次 new

#### 场景

生产环境里，你要让 agent 的存档**进程死了也不丢、重启后能接着跑**。这意味着 checkpointer 不能用内存版（`InMemorySaver`，进程一死全没了），得连一个真正的数据库——DeerFlow 支持 **SQLite**（单文件，轻量）和 **Postgres**（多进程共享，生产用）。

连数据库是要"开连接"的——Postgres 还得开一整个**连接池**（`AsyncConnectionPool`，预先建好几条连接复用，避免每次查库都握手）。开连接池是个**有代价的操作**：要 TCP 握手、要认证、要占数据库那边的连接名额。

#### 朴素写法为什么做不到

朴素写法在每个请求里 `InMemorySaver()`——这压根没连数据库，存档活不过一次请求。就算你把它换成"每个请求 new 一个 `AsyncPostgresSaver`"，也会**每个请求都开一次连接池**：

```python
# 仍然是反面教材
@router.put("/{thread_id}/goal")
async def set_thread_goal(thread_id, body):
    pool = AsyncConnectionPool("postgresql://...")   # 每个请求开一次池（很贵）
    checkpointer = AsyncPostgresSaver(conn=pool)
    await write_thread_goal(checkpointer, ...)
    await pool.close()                               # 每个请求关一次池
```

几十个并发请求，几十个连接池，数据库那边的最大连接数瞬间被打爆。而且连接池的价值在于**复用**——每次开关完全抹掉了复用的好处。**连接池必须是进程级的、长生命的，才划算。**

#### DeerFlow 怎么解决的：`make_checkpointer` 工厂 + 启动时建一次

DeerFlow 把"怎么造 checkpointer"封装成了一个工厂 `make_checkpointer`（`backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py:167-202`），并**在进程启动时只建一次**，建好放进 `app.state`，所有请求共用这一个实例。

先看建 Postgres 后端的那段（连接池怎么开的），`async_provider.py:50-67`：

```python
def _build_postgres_pool(conn_string: str):
    """Build an AsyncConnectionPool with TCP keepalive and connection checking."""
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    return AsyncConnectionPool(                # ← 进程级的连接池
        conn_string,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "keepalives": 1,                   #    TCP 保活
            "keepalives_idle": 60,
            "keepalives_interval": 10,
            "keepalives_count": 6,
        },
        check=AsyncConnectionPool.check_connection,  # ★ 用前先 ping 一下
    )
```

逐行讲关键的：

- `AsyncConnectionPool(...)` 这个对象**就是"连接池"本身**——它持有若干条到 Postgres 的连接，谁要用就从池里借一条、用完还回来。建一次、用一整个进程的生命周期，这才是它该有的用法。
- `keepalives`/`keepalives_idle`/... 一组 TCP 保活参数，防止空闲连接被中间网络设备掐断。这些参数只有在"池是长寿命的"前提下才有意义——每个请求开关一次根本用不上保活。
- `check=AsyncConnectionPool.check_connection` 借连接前先 ping 一下，确保还回来的连接没死掉。同样，这是"长寿命池"才会关心的事。

再看工厂本体怎么把池子包成 checkpointer，`async_provider.py:152-162`：

```python
if db_config.backend == "postgres":
    if not db_config.postgres_url:
        raise ValueError("database.postgres_url is required for the postgres backend")

    AsyncPostgresSaver, _ = _ensure_postgres_imports()
    pool = _build_postgres_pool(db_config.postgres_url)   # ← 建池子（一次）
    async with pool:                                      # ★ 池子的生命周期托付给 async with
        saver = AsyncPostgresSaver(conn=pool)             #    把池子交给 saver
        await saver.setup()                               #    建表（第一次用要初始化 schema）
        yield saver                                       # ★ 产出 saver，外面用
    # 出 async with 时，pool 自动 close —— 连接全部归还/关闭
```

逐行：

- `pool = _build_postgres_pool(...)` 建连接池。这一步**昂贵且全局唯一**，所以只在启动时跑一次。
- `async with pool:` 把池子托付给 `async with`——退出时自动关池。这正是 §0.6 讲的"能开能关"。
- `AsyncPostgresSaver(conn=pool)` 把池子交给 saver；saver 以后每次写 checkpoint，都是从池里借一条连接、用完还回去，**不是自己开关连接**。
- `await saver.setup()` 第一次启动时建好 checkpoint 相关的表（幂等，已有就不建）。
- `yield saver` 产出这个 saver；`make_checkpointer` 的调用方（lifespan）拿到它放进 `app.state`。

朴素写法**每个请求开关一次池子，把数据库连接打爆、把池子的复用价值清零**。DeerFlow 用"启动时建一次 + 进程内共享"彻底解决——`make_checkpointer` 把"怎么造"封装好，lifespan 负责"造一次"，`get_checkpointer(request)` 负责"取那一个"。

---

### 麻烦 2：异步构造需要事件循环，import 时跑不了

#### 场景

你可能会想：既然 checkpointer 是个进程级单例，那干嘛不在模块顶层（import 时）就建好？像这样（**反面教材**）：

```python
# 模块顶层，import 时执行 —— 反面教材
pool = AsyncConnectionPool("postgresql://...")   # ← import 时就要跑
checkpointer = AsyncPostgresSaver(conn=pool)
```

#### 朴素写法为什么做不到

这里有三个层层叠加的问题：

1. **异步初始化跑不了**。`pool.open()`、`saver.setup()` 都是 `async` 的，要 `await`。但模块顶层**没有事件循环**——import 是同步发生的，根本没有 `await` 的地方。你要么得把异步初始化塞进 `asyncio.run()`（这会自己开一个临时的、和 FastAPI 主循环无关的循环，建出来的连接属于那个临时循环，用不了），要么干脆不初始化（那 saver 就是半成品）。
2. **import 时机太早**。配置（`config.yaml`）可能还没读、数据库可能还没准备好、甚至 `asyncio` 主事件循环都还没启动。在 import 阶段去开数据库连接，等于在"餐厅还没开门、还没接水电"的时候就把锅架上了。
3. **连接会绑死在错误的循环上**。`psycopg` 的异步对象一旦在某个事件循环里 `await` 过，就和那个循环绑定了。如果你在 import 时用一个临时循环建连接，等到 FastAPI 真正的主循环来用它，会直接报 "Future attached to a different loop"。

**核心矛盾：checkpointer 的构造是异步的、依赖配置、依赖运行时的数据库，而 import 时这三样一样都没有。**

#### DeerFlow 怎么解决的：在 lifespan 里 `await`

DeerFlow 把 checkpointer 的构造放到 **lifespan 里**——那时事件循环已经在跑、配置已经加载、数据库也已就绪。具体在 `langgraph_runtime`（`backend/app/gateway/deps.py:222-343`）这个 async 上下文管理器里，第 267 行：

```python
async with AsyncExitStack() as stack:                                   # deps.py:258
    config = startup_config

    app.state.stream_bridge = await stack.enter_async_context(make_stream_bridge(config))

    await init_engine_from_config(config.database)                      # deps.py:265  先建持久化引擎

    app.state.checkpointer = await stack.enter_async_context(make_checkpointer(config))   # ★ deps.py:267
    app.state.store = await stack.enter_async_context(make_store(config))
    ...
```

逐行讲关键的：

- `async with AsyncExitStack() as stack:` 进入 lifespan 的总托付盘（§0.5）。这整段代码在**事件循环里**跑，所以能 `await`。
- `await init_engine_from_config(config.database)` 先把持久化引擎建好（Postgres 的 auto-create-database 逻辑要先跑）。
- `app.state.checkpointer = await stack.enter_async_context(make_checkpointer(config))` 这一句是主角。`make_checkpointer(config)` 是 §0.6 讲的"能开能关的工厂"，`enter_async_context` 把它 `async with` 起来、拿到它 `yield` 出来的 saver、把这个 saver 赋给 `app.state.checkpointer`，同时把"关连接"的清理动作记到 `stack` 上。

> `enter_async_context` 英文文档原文（contextlib）：*Enter the supplied async context manager. If successful, also push its `__aexit__` method as a callback and return the result of the `__aenter__` method.*
>
> 翻译：把传入的 async 上下文管理器"开"起来（跑它的 `__aenter__`，也就是工厂里 `yield` 之前那段），同时把它的"关"动作（`__aexit__`，即工厂 `yield` 之后那段）作为一个回调记下来；返回值就是 `__aenter__` 的结果（即工厂 `yield` 出来的那个 saver）。

为什么这么写能同时解决三个问题？

- **异步初始化跑得了**：整段在 lifespan 的事件循环里，`await ... enter_async_context(...)` 干净地把 `await saver.setup()` 这类异步初始化跑完。
- **时机对**：lifespan 是"应用启动好、开始接请求之前"跑的，配置和数据库都已就绪。
- **连接绑在主循环上**：连接池在 FastAPI 的主事件循环里 `await`，之后所有请求也在这个循环里用它，循环一致。

朴素写法在 import 时建 checkpointer，**异步初始化跑不了、时机太早、连错循环**。DeerFlow 把它挪进 lifespan，三样都解决了。

---

### 麻烦 3：后端选择依赖 config，不能写死

#### 场景

DeerFlow 要同时应付好几种部署：

- **本地开发 / 单元测试**：用 `InMemorySaver`，不依赖任何数据库，跑完即弃，快。
- **单机小部署**：用 SQLite，一个文件，不用装数据库服务。
- **生产 / 多机**：用 Postgres，多进程共享、可恢复。

用哪个后端，**由 `config.yaml` 决定**，不该写死在代码里。而且历史上 DeerFlow 的配置格式还演进过——曾经有一个独立的 `checkpointer:` 段，后来统一进了 `database:` 段。新老格式都得兼容。

#### 朴素写法为什么做不到

如果后端选择写死在路由里（或写死在某个工厂里），那"换后端"就得改代码、重新发版——这显然不行。朴素写法根本没有"根据配置选后端"这一层，它默认就是某一个（比如写死内存）。

#### DeerFlow 怎么解决的：`make_checkpointer` 的优先级逻辑

`make_checkpointer`（`async_provider.py:167-202`）的 docstring 明确写了优先级：

> *Priority:*
> *1. Legacy `checkpointer:` config section (backward compatible)*
> *2. Unified `database:` config section*
> *3. Default InMemorySaver*
>
> 翻译：优先级——① 老的独立 `checkpointer:` 配置段（向后兼容）；② 统一的 `database:` 配置段；③ 都没有就默认用 `InMemorySaver`。

代码（`async_provider.py:183-202`）：

```python
if app_config is None:
    app_config = get_app_config()

# Legacy: standalone checkpointer config takes precedence
if app_config.checkpointer is not None:                         # ① 老格式优先
    async with _async_checkpointer(app_config.checkpointer) as saver:
        yield saver
        return

# Unified database config
db_config = getattr(app_config, "database", None)
if db_config is not None and db_config.backend != "memory":     # ② 新格式
    async with _async_checkpointer_from_database(db_config) as saver:
        yield saver
        return

# Default: in-memory                                          # ③ 兜底
from langgraph.checkpoint.memory import InMemorySaver
yield InMemorySaver()
```

逐行：

- `if app_config.checkpointer is not None:` ① 如果 `config.yaml` 里有老的 `checkpointer:` 段，用它（`_async_checkpointer`，`async_provider.py:90-123`，支持 `type: memory/sqlite/postgres`）。`return` 确保只走一条路。
- `if db_config is not None and db_config.backend != "memory":` ② 否则看新的统一 `database:` 段，只要后端不是 `memory`（memory 走兜底更简单），就用 `_async_checkpointer_from_database`（`async_provider.py:131-164`）。
- `yield InMemorySaver()` ③ 都没有就默认内存——这也是"啥都没配也能跑起来"的开发体验兜底。

注意三条路最终都 `yield` 一个 checkpointer（同一个接口 `Checkpointer`），**调用方根本不用关心后端是啥**。`get_checkpointer(request)` 拿到的类型是 `Checkpointer`（一个 type alias，`deps.py:28` 的 `from langgraph.types import Checkpointer`），它可能是 `InMemorySaver`、`AsyncSqliteSaver`、`AsyncPostgresSaver` 中的任意一个——但路由代码只调 `aget_tuple`/`aput`，接口一致就行。

> `Checkpointer` 的定义（langgraph）：`Checkpointer = None | bool | BaseCheckpointSaver`——一个 Union 类型别名。它的运行时实体都是 `BaseCheckpointSaver` 的子类。
>
> 翻译：`Checkpointer` 不是类，是个类型别名，等于"None 或 bool 或 `BaseCheckpointSaver`"。DeerFlow 里实际产出的都是 `BaseCheckpointSaver` 的具体子类（那三个 saver）。

朴素写法**后端写死、换不了、新老配置都不认**。DeerFlow 用 `make_checkpointer` 的三段优先级 + 统一接口，让"换后端"变成"改 config.yaml"，代码一行不动。

---

### 麻烦 4：关机要按顺序回收，否则 PoolClosed

#### 场景

进程要关了（部署发版、手动重启、收到 SIGTERM）。这时可能**还有正在跑的 agent run**——某个后台任务正跑到一半，马上就要写一个 checkpoint 进去。如果你"先关连接池、再让那个 run 写"，它写的时候池子已经关了，直接抛 `PoolClosed`，这个 run 的最后一步状态就丢了。

这不是假想的场景——DeerFlow 真的踩过这个坑。

#### 朴素写法为什么做不到

历史 bug（issue #3373，注释在 `backend/app/gateway/deps.py:88-111`）：

> *A run still mid-graph would otherwise leak into asyncio.run() shutdown, where langgraph's `_checkpointer_put_after_previous` `aput` races the closed pool and raises `PoolClosed`.*
>
> 翻译：一个还在跑图（graph）中间的 run，会泄漏进 `asyncio.run()` 的关机流程里；在那里，langgraph 内部那个"排在前一次 put 之后执行的" `_checkpointer_put_after_previous` 正在 `aput`，它和"已关闭的连接池"赛跑，一旦池先关了就抛 `PoolClosed`。

根因是**关机顺序错了**：`AsyncExitStack` 退出时会按"后进先出"关掉 checkpointer 的连接池，但此时 in-flight 的 run 还没结束，它正准备往这个池子写。朴素写法（或者"关机时直接关池"）没有"先等 run 写完、再关池"这一步。

#### DeerFlow 怎么解决的：先排干 in-flight run，再让 `AsyncExitStack` 关池

DeerFlow 在 lifespan 的 `finally` 块里**先 drain（排干）in-flight 的 run，再让 `AsyncExitStack` 退出**。`deps.py:332-343`：

```python
        try:
            yield                                   # 应用活着、正常接请求
        finally:
            # Drain in-flight run tasks BEFORE the AsyncExitStack tears down the
            # checkpointer (and its connection pool). A run still mid-graph would
            # otherwise leak into asyncio.run() shutdown ... (issue #3373).
            run_manager = getattr(app.state, "run_manager", None)
            if run_manager is not None:
                await _drain_inflight_runs(run_manager)   # ★ 先把还在跑的 run 等到结束
            await close_engine()                          #    再关持久化引擎
        # 出 async with AsyncExitStack 时，才关 checkpointer 的连接池
```

逐行讲关键的：

- `yield` 应用在这里正常服务。`yield` 之后（即关机时）才进 `finally`。
- `await _drain_inflight_runs(run_manager)` **★ 这是关键**：在关连接池之前，先把所有还在跑的 run 等到它们结束（或超时）。`_drain_inflight_runs`（`deps.py:88-111`）内部还会"屏蔽"掉 lifespan 自己被取消的情况（第二次 SIGINT），保证排干动作一定能跑完。

> `_drain_inflight_runs` 的 docstring（`deps.py:88-95`）：*Shields the (internally-bounded) drain so that even if the lifespan coroutine is itself cancelled mid-shutdown — a second SIGINT or the server's graceful-shutdown timeout, i.e. the same signal storm behind #3373 — the checkpointer pool is not closed while run tasks are still writing checkpoints.*
>
> 翻译：给这个排干动作套上"屏蔽"（`asyncio.shield`），使得即使 lifespan 协程自己在关机途中被取消（第二次 SIGINT、或者服务器的优雅关机超时——也就是 #3373 背后那场"信号风暴"），连接池也不会在 run 还在写 checkpoint 时被关掉。

- `await close_engine()` 关掉持久化引擎。
- 整个 `async with AsyncExitStack() as stack:` 退出时，`stack` 才按"后进先出"关掉 checkpointer 的池子——而此时 run 已经 drain 完了，不会再有人往池子写。

⚠️ 注意顺序的精妙：**run drain → 引擎关 → 池关**。checkpointer 的池子是最先建（`deps.py:267`，先进 stack）、最后关（最后出 stack）的。`finally` 里的 drain 插在"池关之前"，正好堵住了 #3373 那个赛跑窗口。朴素写法没有 drain 这一步，池子一关，正在写的 run 就 `PoolClosed`。DeerFlow 用"先 drain 再关"把这个窗口彻底关上。

---

### 麻烦 5：多 worker 下 SQLite 会互相踩

#### 场景

生产环境为了吞吐，开多个 worker（`GATEWAY_WORKERS=4`）。每个 worker 是独立进程，各自有自己的 `app.state.checkpointer`。现在问题来了：**4 个进程的 checkpointer 该连到哪儿？**

如果它们各自连一个 SQLite 文件——SQLite 的写是**文件级锁**，多个进程同时写一个文件，必然互相阻塞、甚至写坏。就算每个进程连各自的 SQLite 文件——那 A 进程存的档，B 进程根本看不到，run 在 A 里跑、用户下次请求被负载均衡到 B，B 说"没这个 thread"。

#### 朴素写法为什么做不到

朴素写法（每个请求 `InMemorySaver()`）在多 worker 下更惨——**每个请求的存档都是请求局部的，别说跨进程，跨请求都没有**。就算你退一步用"每进程一个 SQLite 文件"，也躲不开"写锁互踩 + 跨进程不可见"两个问题。

#### DeerFlow 怎么解决的：启动闸强制"多 worker 必须 Postgres"

DeerFlow 的态度很直接：**你要用多 worker，就必须用 Postgres**——因为只有 Postgres 是真正的多进程共享数据库。这个约束在启动时用一个"闸"强制检查，`_enforce_postgres_for_multi_worker`（`backend/app/gateway/deps.py:49-80`），在 lifespan 一进来就调（`deps.py:256`）：

```python
def _enforce_postgres_for_multi_worker(config: AppConfig) -> None:
    """Refuse to start when GATEWAY_WORKERS > 1 and safety preconditions are not met."""
    try:
        workers = int(os.environ.get("GATEWAY_WORKERS", "1"))
    except (TypeError, ValueError):
        workers = 1

    if workers <= 1:
        return                                  # 单 worker，不查，随便用

    backend = getattr(config.database, "backend", None)
    if backend != "postgres":
        raise SystemExit(                       # ★ 多 worker 但不是 postgres → 拒绝启动
            f"GATEWAY_WORKERS={workers} requires database.backend='postgres', "
            f"but database.backend is '{backend}'. SQLite cannot support "
            f"concurrent multi-process access. Set GATEWAY_WORKERS=1 or switch to Postgres."
        )

    run_ownership = getattr(config, "run_ownership", None)
    if run_ownership is None or not run_ownership.heartbeat_enabled:
        raise SystemExit(                       # ★ 多 worker 还必须开 heartbeat
            f"GATEWAY_WORKERS={workers} requires run_ownership.heartbeat_enabled=true. ..."
        )
```

逐行讲关键的：

- `workers = int(os.environ.get("GATEWAY_WORKERS", "1"))` 读进程数。默认 1。
- `if workers <= 1: return` 单 worker 直接放行——SQLite 也好、内存也好，单进程不会互踩。
- `if backend != "postgres": raise SystemExit(...)` ★ **多 worker 但后端不是 Postgres，进程直接退出**（`SystemExit`，不是抛异常然后被 catch，是干脆不启动）。错误信息非常明确地告诉运维"要么把 worker 数设回 1，要么换 Postgres"。

> docstring 原文（`deps.py:51-54`）：*SQLite write-locks cannot support concurrent multi-process access.*
>
> 翻译：SQLite 的写锁没法支撑多进程并发访问。

- 第二个 `SystemExit` 多 worker 还必须开 `run_ownership.heartbeat_enabled`（run 的租约心跳，姊妹篇 `run-manager-deep-dive.md` §0.6 / 麻烦 4 讲过 lease）。否则没有心跳，每个 run 的 lease 都是 NULL，启动时的孤儿回收会把别的 worker 还活着的 run 当孤儿杀掉。

这个闸**在 `langgraph_runtime` 一进来、还没建任何 checkpointer 之前就跑**（`deps.py:256`，早于 `deps.py:267` 建 checkpointer）。这样错误信息清晰、进程立刻退出，不会"建到一半才崩"。

朴素写法**多 worker 下要么写锁互踩、要么跨进程不可见**。DeerFlow 用启动闸把"多 worker 必须 Postgres + heartbeat"做成硬约束，从根上堵死了 SQLite 多进程这条路。

---

### 麻烦 6：缺失时要给干净的 503，而不是深层 NoneType 崩

#### 场景

想象一种故障：checkpointer 没建成功——可能是 `make_checkpointer` 抛了异常（数据库连不上）、可能是 lifespan 还没跑到 `deps.py:267`、可能是某次重构把 `app.state.checkpointer` 的赋值漏了。这时 `app.state.checkpointer` 是 `None`（或干脆不存在）。

这时一个请求进来了，路由调 `checkpointer.aget_tuple(...)`。如果代码是直接 `getattr` 不检查，会抛 `AttributeError: 'NoneType' object has no attribute 'aget_tuple'`——一个**深层的、栈底报的、把人看懵的崩溃**，最终变成 500，客户端只看到一句 "Internal Server Error"，运维只看到一长串 traceback，根本不知道是 checkpointer 没建好。

#### 朴素写法为什么做不到

朴素写法（`getattr(request.app.state, "checkpointer", None)` 然后直接用）正是这个下场：

```python
# 反面教材
checkpointer = getattr(request.app.state, "checkpointer", None)
await write_thread_goal(checkpointer, ...)   # checkpointer 是 None → 深层崩 → 500
```

**问题不是"崩了"，而是"崩得太晚、太深、信号太弱"**——本该在"取依赖"这一步就明确告诉客户端"checkpointer 没准备好"，却拖到"用它写 goal"那一步才在 NoneType 上崩。

#### DeerFlow 怎么解决的：`_require` 工厂——取不到就立刻抛 503

这就是本文主角那一行的源头——`_require` 工厂（`backend/app/gateway/deps.py:351-361`）：

```python
def _require(attr: str, label: str) -> Callable[[Request], T]:
    """Create a FastAPI dependency that returns ``app.state.<attr>`` or 503."""

    def dep(request: Request) -> T:
        val = getattr(request.app.state, attr, None)        # ← 从工具箱取
        if val is None:
            raise HTTPException(status_code=503, detail=f"{label} not available")  # ★ 取不到就 503
        return cast(T, val)

    dep.__name__ = dep.__qualname__ = f"get_{attr}"         #    给闭包起个好认的名字
    return dep
```

逐行（这一段是全文最核心的，逐行细讲）：

- **函数签名** `def _require(attr: str, label: str) -> Callable[[Request], T]:` 这是一个**工厂函数**——它本身不是依赖，它**生产依赖**。吃两个字符串，吐一个"能当依赖用的函数"。
- **`attr` 参数**：工具箱里的属性名。对 checkpointer 这一行就是 `"checkpointer"`——后面 `getattr(request.app.state, "checkpointer", None)` 就是用它。
- **`label` 参数**：给人看的标签，只用在 503 的错误信息里。对 checkpointer 就是 `"Checkpointer"`——报错时会拼成 `"Checkpointer not available"`。
- **`def dep(request: Request) -> T:`** 这是真正被路由用的那个函数（闭包）。它吃一个 `request`，返回 `T`（对 checkpointer，`T` 就是 `Checkpointer`）。它的形状 `Request -> T` 正好是 FastAPI 依赖的标准形状（见下面 ⚠️）。
- **`val = getattr(request.app.state, attr, None)`** 从 `app.state` 这个工具箱里取 `attr`。取不到默认 `None`，不抛错——判断留给下一行。
- **`if val is None: raise HTTPException(status_code=503, ...)` ★** 这是整个工厂的灵魂：**取不到（None）就立刻、就地抛 503**。语义是"这个进程的 checkpointer 还没准备好，请稍后再试"。它抛在"取依赖"这一步，而不是拖到"用它"那一步——错误信号又早又准。
- **`return cast(T, val)`** 把取到的值返回。`cast` 是 typing 的"假转"——运行时是 no-op，纯粹骗过类型检查器（告诉它"我知道这是 `T`，别报警"）。
- **`dep.__name__ = dep.__qualname__ = f"get_{attr}"`** 把这个闭包的名字改成 `get_checkpointer`，方便调试和 FastAPI 的依赖缓存识别。

于是本文的主角那一行（`deps.py:366`）就很好懂了：

```python
get_checkpointer: Callable[[Request], Checkpointer] = _require("checkpointer", "Checkpointer")
```

- **左边类型注解** `Callable[[Request], Checkpointer]` 说明 `get_checkpointer` 是一个"吃 Request、吐 Checkpointer"的函数。
- **右边** `_require("checkpointer", "Checkpointer")` 调工厂，产出那个 `dep` 闭包。
- 整行的效果：定义了一个名叫 `get_checkpointer` 的函数，它每次被调用时，从 `app.state` 取 `checkpointer`，取不到就 503。

#### 6 个兄弟 getter——同一套机制

`_require` 是个通用工厂，checkpointer 只是它的一个用户。`deps.py:364-369` 这一整块全是它的产出：

| 全局名 | `attr` | `label` | 返回类型 | 单例在哪建 |
|---|---|---|---|---|
| `get_stream_bridge` (364) | `"stream_bridge"` | `"Stream bridge"` | `StreamBridge` | `deps.py:261` |
| `get_run_manager` (365) | `"run_manager"` | `"Run manager"` | `RunManager` | `deps.py:309` |
| `get_checkpointer` (366) | `"checkpointer"` | `"Checkpointer"` | `Checkpointer` | `deps.py:267` |
| `get_run_event_store` (367) | `"run_event_store"` | `"Run event store"` | `RunEventStore` | `deps.py:305` |
| `get_feedback_repo` (368) | `"feedback_repo"` | `"Feedback"` | `FeedbackRepository` | `deps.py:277/282` |
| `get_run_store` (369) | `"run_store"` | `"Run store"` | `RunStore` | `deps.py:276/281` |

它们**共享同一套"取不到就 503"的机制**。其中 `get_stream_bridge` 和 `get_run_manager` 已经在姊妹篇里讲了；本篇讲的是 `get_checkpointer`。三个兄弟紧挨着写在 `deps.py:364-366`，是因为它们是同一类东西——"启动时建一次、放进 `app.state`、每个请求取一次、取不到就 503"的进程级运行时单例。

⚠️ **一个容易误解的点：形状像 FastAPI 依赖，但实际是直接调用。** `_require` 的 docstring 说它 "Create a FastAPI dependency"，`dep` 的形状（`Request -> T`）也确实是 FastAPI 依赖的标准形状。但**在 DeerFlow 的路由里，这些 getter 是被"直接调用"的，不是用 `Depends(...)` 包起来的**。看真实用法，`backend/app/gateway/routers/threads.py:836`：

```python
@router.get("/{thread_id}/goal", response_model=ThreadGoalResponse)
@require_permission("threads", "read", owner_check=True)
async def get_thread_goal(thread_id: str, request: Request) -> ThreadGoalResponse:
    checkpointer = get_checkpointer(request)          # ← 直接调用，传 request 进去
    goal = await read_thread_goal(checkpointer, thread_id)
    ...
```

`get_checkpointer(request)` 是一个普通的函数调用——路由先通过 FastAPI 正常拿到 `request: Request`，再**手动**把 `request` 传给 `get_checkpointer`。全 `backend/app` 搜不到一处 `Depends(get_checkpointer)`（其它 `get_*` 同理）。所以这些 getter 虽然有"依赖"的形状，但本仓库的实际用法是"直接调用"——这一点别被 docstring 里的 "FastAPI dependency" 字样带偏。

#### 取回来之后怎么用——`aget_tuple`/`aput`

`get_checkpointer(request)` 取回来的那个 saver，最终被它的下游这样用。以"确保某 thread 有个根 checkpoint"为例，`backend/packages/harness/deerflow/runtime/goal.py:434-447`：

```python
async def ensure_thread_checkpoint(checkpointer: Any, thread_id: str) -> None:
    """Create an empty root checkpoint for *thread_id* when none exists."""
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    checkpoint_tuple = await _call_checkpointer_method(checkpointer, "aget_tuple", "get_tuple", config)  # ← 读档
    if checkpoint_tuple is not None:
        return                                              # 已有根 checkpoint，不用建
    metadata = {"step": -1, "source": "input", "writes": None, "parents": {}, "created_at": now_iso()}
    await _call_checkpointer_method(checkpointer, "aput", "put", config, empty_checkpoint(), metadata, {})  # ★ 写档
```

逐行讲关键的：

- `config = {"configurable": {"thread_id": ...}}` 这是 checkpointer 用来定位"哪条存档链"的钥匙——按 `thread_id` 分。
- `await _call_checkpointer_method(checkpointer, "aget_tuple", ...)` 读档：取这个 thread 最新一档。`_call_checkpointer_method`（`goal.py:411-422`）是个兼容层——优先调异步的 `aget_tuple`，saver 没有就退而调同步的 `get_tuple`（同步的会被 `asyncio.to_thread` 丢到线程池跑，不堵事件循环）。
- `await _call_checkpointer_method(checkpointer, "aput", ...)` ★ 写档：如果这个 thread 还没有任何存档，就建一个空的根 checkpoint（`step: -1` 表示"还没真正开始跑"）。

这就是 `get_checkpointer(request)` 拿回来的东西的最终归宿——被 `aget_tuple`/`aput`/`aput_writes` 这三件套（§0.2）反复调用，支撑起 agent 的存档/读档/断点续跑。

朴素写法**取不到 checkpointer 时深层崩成 500**。DeerFlow 用 `_require` 在"取依赖"这一步就地抛 503——又早又准，运维一看 "Checkpointer not available" 就知道是这个进程的 checkpointer 没建好，重启或换进程即可。

---

## 第 3 步：把 6 个麻烦的本质串起来

朴素写法 `checkpointer = InMemorySaver()`（每个请求现 new 一个）的根本问题是：

> **把 checkpointer 当成了"一个随手 new 的临时对象"，而不是"一个有生命周期的、要占用资源的、要跨请求共享的进程级单例"。**

而现实里，一个 checkpointer 的生命周期和一次 HTTP 请求的生命周期是**完全不同步**的两件事：

| 维度 | 朴素写法（每次请求 new） | 真实写法（`app.state` 单例 + `_require`） |
|---|---|---|
| **寿命** | 一个请求那么短，函数返回就 GC | 整个进程的生命周期（lifespan 建、lifespan 关） |
| **资源占用** | 每个请求开一次连接池，瞬间打爆数据库 | 进程开一次池，请求借还复用 |
| **后端可换** | 写死在代码里，换后端要发版 | 看 `config.yaml`，`make_checkpointer` 三段优先级选 |
| **关机回收** | 没有，连接泄漏 / run 写到一半池关了 → PoolClosed | `finally` 先 drain run，再让 `AsyncExitStack` 关池 |
| **多 worker** | 各开各的，互踩或不可见 | 启动闸强制多 worker 必须 Postgres + heartbeat |
| **缺失表现** | 深层 NoneType 崩 → 500，信号又弱又晚 | `_require` 就地抛 503，又早又准 |

把 checkpointer 和请求"焊死"（每次 new），就会出现 6 个麻烦——因为现实里它的生命周期**就是不该和请求同步**：

- ① 真正的后端要开数据库连接，不能每次 new → 得有 `make_checkpointer` 工厂 + 启动时建一次
- ② 异步构造需要事件循环，import 时跑不了 → 得在 lifespan 里 `await` 建
- ③ 后端选择依赖 config，不能写死 → `make_checkpointer` 三段优先级 + 统一接口
- ④ 关机要按顺序回收，否则 PoolClosed → `finally` 先 drain run 再关池（修 #3373）
- ⑤ 多 worker 下 SQLite 会互相踩 → 启动闸强制多 worker 必须 Postgres
- ⑥ 缺失时要给干净的 503，而不是深层崩 → `_require` 取不到就地 503

`get_checkpointer(request)` + `_require` 工厂 + lifespan 绑定，就是**把 checkpointer 的生命周期和 HTTP 请求的生命周期解耦**的那个中间层。它具体做的事：

- **建一次**：让 checkpointer 在进程启动时建好（lifespan + `make_checkpointer`），不是每个请求 new（麻烦 1、2）。
- **可配置**：让"用哪个后端"由 `config.yaml` 决定，代码不动（麻烦 3）。
- **可回收**：让连接池在关机时按正确顺序关，不丢 run 的最后一档（麻烦 4）。
- **多进程安全**：让多 worker 部署强制连同一个 Postgres，不踩写锁（麻烦 5）。
- **缺失优雅**：让"checkpointer 没建好"变成干净的 503，不是深层 500（麻烦 6）。
- **进程内共享**：让所有请求共用同一个 saver 实例，借还连接池里的连接。

代码上，这一切都从那一行开始：

```python
get_checkpointer: Callable[[Request], Checkpointer] = _require("checkpointer", "Checkpointer")
```

这一行本身只是"注册一个取依赖的函数"；但它背后牵着一整条链——`app.py:171` 的 lifespan → `deps.py:222` 的 `langgraph_runtime` → `deps.py:267` 把 `make_checkpointer` 的产出放进 `app.state.checkpointer` → 每个请求里 `get_checkpointer(request)` 取出来 → 下游 `goal.py:434` 的 `aget_tuple`/`aput` 用它存档读档。一张全景图：

```
                    ┌─────────────────────────────────────────────────────────────┐
                    │            进程启动（lifespan，app.py:171）                  │
                    │                                                             │
                    │  langgraph_runtime（deps.py:222）                           │
                    │    │                                                        │
                    │    │  _enforce_postgres_for_multi_worker  ← 麻烦5 多worker闸  │
                    │    │                                                        │
                    │    │  async with AsyncExitStack() as stack:                 │
                    │    │    app.state.checkpointer =                            │
                    │    │       await stack.enter_async_context(                 │
                    │    │            make_checkpointer(config))  ← 麻烦1/2/3     │
                    │    │                  │                                     │
                    │    │                  ▼                                      │
                    │    │       [InMemorySaver / AsyncSqliteSaver /              │
                    │    │        AsyncPostgresSaver(conn=pool)]                  │
                    │    │                                                        │
                    │    │  yield  ← 应用开始接请求                                │
                    │    │                                                        │
                    │    │  finally: await _drain_inflight_runs() ← 麻烦4 排干    │
                    │    └─────────────────────────────────────────────────────── │
                    └─────────────────────────────────────────────────────────────┘
                                          │ app.state.checkpointer（进程级工具箱）
                                          ▼
                    ┌─────────────────────────────────────────────────────────────┐
                    │                  每次 HTTP 请求                              │
                    │                                                             │
                    │  checkpointer = get_checkpointer(request)                   │
                    │       │  ← _require 工厂：getattr app.state，取不到抛 503    │
                    │       │     （麻烦6）                                        │
                    │       ▼                                                     │
                    │  await checkpointer.aget_tuple(config)   ── 读档            │
                    │  await checkpointer.aput(config, ...)    ── 写档            │
                    │  （goal.py:434-447 等）                                     │
                    └─────────────────────────────────────────────────────────────┘
```

所以它不是"代码好看"或"封装一下"——是这 6 个麻烦里**任意一个**单拿出来，朴素写法都过不去。

它和姊妹篇的 `get_stream_bridge` / `get_run_manager` 是同一类东西的三面：

- `get_stream_bridge`（`deps.py:364`）解耦**事件流**的生命周期——让 run 的事件能跨请求、跨订阅者流动。
- `get_run_manager`（`deps.py:365`）解耦**执行 + 状态**的生命周期——让 run 能后台跑、可查可取消、可恢复。
- `get_checkpointer`（`deps.py:366`，本篇）解耦**存档（持久化）**的生命周期——让 agent 的 checkpoint 有正确的数据库后端、正确的开关机顺序、正确的多进程语义。

三个紧挨着写在 `deps.py:364-366`，是因为它们是同一类东西：**"启动时建一次、放进 `app.state`、每个请求取一次、取不到就 503"的进程级运行时单例**。理解了其中一个，另两个就是同一套机制的换皮。
