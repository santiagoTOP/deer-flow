# langgraph_runtime 深度讲解：为什么不能在 app.py 顶部随手构造运行时单例就完事

本文是 `get-checkpointer-deep-dive.md` / `run-manager-deep-dive.md` / `stream-bridge-deep-dive.md` 的姊妹篇，而且是它们的**上游**。

那三篇讲的是"**取**"——`backend/app/gateway/deps.py:364-366` 这三行紧挨着的声明：

```python
get_stream_bridge: Callable[[Request], StreamBridge] = _require("stream_bridge", "Stream bridge")
get_run_manager: Callable[[Request], RunManager] = _require("run_manager", "Run manager")
get_checkpointer: Callable[[Request], Checkpointer] = _require("checkpointer", "Checkpointer")
```

每个 HTTP 请求进来，路由函数调一句 `get_checkpointer(request)`，就从 `app.state` 上取回一个 checkpointer。那三篇解释了"为什么取的时候取不到要抛 503"、"为什么取出来的是单例而不是每次 new"。

但它们留下了一个更上游的问题：**这些单例到底是哪里来的？** `app.state.checkpointer` 上挂的那个 `AsyncPostgresSaver`，是谁、在什么时候、按什么顺序把它造出来、挂上去的？关机的时候，又按什么顺序把它和它的连接池一起收掉？

答案就是本文要讲的这个函数（`backend/app/gateway/deps.py:222-223`）：

```python
@asynccontextmanager
async def langgraph_runtime(app: FastAPI, startup_config: AppConfig) -> AsyncGenerator[None, None]:
```

它是整个 Gateway 进程的**装配总闸**：进程启动时进、关机时出，进去时把所有运行时单例按特定顺序建出来挂到 `app.state`，出来时按特定顺序把它们连同底层连接池一起回收。整个进程里唯一的"LangGraph 运行时"就诞生和消亡在这一对开/关之间。

打个比方：那三个 getter 像**前台服务员从工具箱里拿剪刀**；而 `langgraph_runtime` 是**开店前把剪刀一把一把摆进工具箱、打烊后再一把一把收起来锁好**的那道工序。前者每来一位客人做一次（每请求一次），后者每天只做两次（开机一次、关机一次）。本文讲的就是后者——为什么这道工序非有不可，不能图省事在 `app.py` 顶部一行 `checkpointer = AsyncPostgresSaver(...)` 就完事。

本文面向第一次接触这套架构的同学。先把所有需要的前置概念一个一个铺出来，然后再讲 6 个麻烦，每段代码逐行解释。已经读过三篇姊妹篇的同学可以快速跳过带 ※ 标记的小节（那里会注明"姊妹篇讲过"，本篇只复用结论）。

---

## 第 0 步：先把要用的术语讲清楚

在讲任何代码之前，先把后面会反复出现的概念讲明白。这些是理解"为什么朴素写法会出问题"的地基。

### 0.1 `app.state`——进程级"工具箱"

※ 概念基础和姊妹篇 `get-checkpointer-deep-dive.md` §0.3 重叠，本篇只复用结论，并补一个本篇才用到的视角。

FastAPI 应用对象 `app` 上挂着一个属性叫 `app.state`，它就是一个**进程级的"工具箱"**：你在上面塞东西、取东西，对象会跟着 `app` 一起活到进程结束。一个 worker 进程就有一个工具箱，进程死了工具箱也跟着没了（姊妹篇 §0.2 讲过进程内存"独占 + 易失"）。

本篇要补的视角是：**工具箱是"取"的终点，但不是"放"的起点。** 东西不是凭空长在 `app.state` 上的——得有人在进程启动时，把每个单例**造出来、再塞进去**；关机时再有人把它们**收回来**。这个"造 → 塞进去 → 用一阵子 → 收回来"的过程，就是 `langgraph_runtime` 干的事。所以读本文时请记住：

- **`app.state` 是"存放点"**——getter 从它取（姊妹篇讲的"取"）。
- **`langgraph_runtime` 是"造放收"的工序**——它负责往 `app.state` 塞、再负责关机时收（本篇讲的"建与还"）。

### 0.2 FastAPI 的 `lifespan`——开机/关机的总闸

※ 概念基础和姊妹篇 `get-checkpointer-deep-dive.md` §0.4 重叠，本篇只复用结论，并补一个关键关系。

FastAPI 允许你给应用配一个 `lifespan` 函数（`backend/app/gateway/app.py:170-171`）：

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""
```

它是个 `@asynccontextmanager`（0.6 会讲）：`yield` 之前是**开机**逻辑（进程启动时跑一次），`yield` 之后是**关机**逻辑（进程退出时跑一次），中间 `yield` 这一下表示"应用正式开始接请求"。打个比方，lifespan 是餐厅**开店/打烊的总闸**——开店前布置（建单例、连数据库、起通道），打烊后收拾（排干 run、关连接池）。

本篇要补的关键关系是：**`lifespan` 是"外总闸"，`langgraph_runtime` 是被它套住的"内总闸"。** 看 `app.py:232-275` 的骨架：

```python
try:
    startup_config = get_app_config()
    ...
except Exception as e:
    raise RuntimeError(...) from e
...
# Initialize LangGraph runtime components (StreamBridge, RunManager, checkpointer, store)
async with langgraph_runtime(app, startup_config):          # ← 内总闸：这里建/收运行时单例
    logger.info("LangGraph runtime initialised")
    await _ensure_admin_user(app)
    ...                                                      # ← 起通道、起调度器
    yield                                                    # ← 应用开始接请求
    ...                                                      # ← 关通道、关调度器、flush 内存
```

逐行看关键点：

- `startup_config = get_app_config()` 在 lifespan 最开始拍一张**配置快照**（0.6 会讲为什么是快照）。
- `async with langgraph_runtime(app, startup_config):` 这一行把"运行时单例的建与还"整个委托给了 `langgraph_runtime`。`async with` 一进去，单例们被造出来挂上 `app.state`；离开这个 `with`（关机时），它们被有序回收。**整个 LangGraph 运行时的生命周期，就被包在了这一对 `async with ... :` 里。**
- `yield` 在 `langgraph_runtime` 的 `with` 块**内部**——意味着只有运行时单例都建好了，应用才开始接请求。这保证了"任何请求来时，`app.state.checkpointer` 一定已经存在"（姊妹篇 getter 讲的那个不变量，物理基础就在这里）。

所以 `lifespan` 和 `langgraph_runtime` 是**两层总闸**：外层 `lifespan` 管整个应用的开机关机（含日志、通道、调度器），内层 `langgraph_runtime` 只管"LangGraph 运行时单例"这一摊。本文讲的就是内层。

### 0.3 `AsyncExitStack`——一摞"用完要按倒序还"的资源

这是本篇的核心概念，姊妹篇只是提了一句，这里讲透。

Python 标准库里有个东西叫 `AsyncExitStack`（`contextlib` 模块）。你可以把它想成一个**"还东西的清单"**：你每"借"一个用完要还的资源，就在清单上记一笔；等你不借了（`__aexit__`），它就**按倒序**把清单上每个资源逐个"还"掉（调它的关闭逻辑）。

关键特性是 **LIFO（后进先出）**：最后借的先还。这非常符合资源管理的直觉——你先打开数据库连接（A），再基于它建 checkpointer（B）；收的时候就得**先收 B 再收 A**，否则 B 还在用 A 就被关了，必崩。AsyncExitStack 自动给你保证这个倒序。

`AsyncExitStack` 有两个核心用法：

- `await stack.enter_async_context(some_async_cm)`——把一个**异步上下文管理器**登记进栈。它会立刻 `await` 这个 cm 的 `__aenter__`（拿到产出值），并把它的 `__aexit__` 排进"还东西清单"。
- `async with stack:`——进入/退出整摞资源。退出时（无论正常还是异常）自动按 LIFO 倒序调用清单上每个 `__aexit__`。

打个比方：AsyncExitStack 就像餐厅前台那个**"借出物品登记本"**——服务员借一把剪刀（登记一笔）、借一卷胶带（再登记一笔），打烊时前台照着本子**从最后一笔往前划**，把胶带先收、剪刀后收。你只要负责"借"的时候登记，"还"的顺序它自动给你排好。

为什么要用栈而不是手动 `try/finally` 一个个关？因为**资源多了之后，手动写倒序 finally 极易出错**——加一个新资源就要记得在 finally 里插一行，而且顺序不能乱。AsyncExitStack 让你"借一个登记一个"，关闭顺序自动正确，这是异步资源管理的标准姿势。

### 0.4 `@asynccontextmanager` + `enter_async_context`——把"建好还要能关"的工厂登记进栈

Python 里有一种函数叫**异步上下文管理器工厂**，长这样（`backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py:167-168`）：

```python
@contextlib.asynccontextmanager
async def make_checkpointer(app_config: AppConfig | None = None) -> AsyncIterator[Checkpointer]:
```

注意它头上的 `@contextlib.asynccontextmanager` 装饰器。**它的作用是把一个"里面写了 `yield` 的 `async def` 函数"变成一个异步上下文管理器**——调用 `make_checkpointer(cfg)` 不会直接给你 checkpointer，而是给你一个"可以 `async with` 的东西"；`async with` 进去时跑 `yield` 之前的代码（建连接池、`setup()`），`yield` 出来那个 checkpointer，`async with` 退出时跑 `yield` 之后的代码（关连接池）。

这种工厂的好处是：**"建"和"还"被绑在了一起**——你不可能光建不还，因为退出 `async with` 必然触发关闭。

但它有个"不方便"：你只能 `async with make_checkpointer(cfg) as cp:` 这样**就地**用它。如果你要建**一堆**这种资源、还要管它们的关闭顺序（0.3 说的 LIFO），逐个嵌套 `async with` 会写成"阶梯状"很丑，而且数量不固定时根本没法嵌套。

这时 `AsyncExitStack` 就上场了。它提供一个方法 `enter_async_context(cm)`，能把"将来要关"的异步上下文管理器**登记进栈**——等价于"我先 `async with` 进去拿产出值，但把"退出时的关闭"推迟到整个栈退出时统一做"。于是你就可以写成（这是 `langgraph_runtime` 的真实写法）：

```python
async with AsyncExitStack() as stack:
    app.state.checkpointer = await stack.enter_async_context(make_checkpointer(config))
    app.state.store = await stack.enter_async_context(make_store(config))
    # ... 想加多少加多少，关闭顺序自动 LIFO
```

这就是 DeerFlow 里三个核心运行时单例（`make_stream_bridge` / `make_checkpointer` / `make_store`，全部是 `@asynccontextmanager`）被装配的标准姿势。**记住这个对应：**"是 `@asynccontextmanager` 工厂" → "用 `enter_async_context` 进栈" → "关闭时自动倒序还"。这是理解麻烦 2 的钥匙。

### 0.5 "活资源" vs 纯数据结构——为什么有的进栈、有的不进栈

0.4 讲了"是异步上下文管理器的进栈"。但 `langgraph_runtime` 里**不是所有东西都进栈**——比如 `make_run_event_store`（`backend/packages/harness/deerflow/runtime/events/store/__init__.py:5-23`）：

```python
def make_run_event_store(config=None) -> RunEventStore:
    """Create a RunEventStore based on run_events.backend configuration."""
    if config is None or config.backend == "memory":
        return MemoryRunEventStore()
    if config.backend == "db":
        ...
        return DbRunEventStore(sf, ...)
    if config.backend == "jsonl":
        return JsonlRunEventStore()
    raise ValueError(...)
```

注意它是**普通 `def`，没有 `@asynccontextmanager`**。`langgraph_runtime` 里它被**直接赋值、不进栈**（`deps.py:305`）：

```python
app.state.run_event_store = make_run_event_store(run_events_config)
```

为什么区别对待？因为资源分两类：

- **活资源（需要"建-用-还"）**：checkpointer、store、stream_bridge——它们背后是**连接池 / redis 客户端 / 文件句柄**。建的时候打开连接、用的时候借连接、不用了必须**主动关**，否则连接泄漏。这种必须是 `@asynccontextmanager`，用 `enter_async_context` 进栈，靠栈在关机时自动关。
- **纯数据结构（用完即弃）**：`MemoryRunEventStore` 内部就是个 `dict`，`DbRunEventStore` 只是把**别人（engine）已经管好的** `session_factory` 借来用，自己不开连接。它没有"需要关"的东西——它的 ABC（`runtime/events/store/base.py`）连 `close()` / `__aenter__` / `__aexit__` 都没有。对它来说，"建出来"就是"全部"，不存在"还"这一步，所以不进栈。

打个比方：checkpointer 像一把**借来的电钻**（用完得拔插头归还，不然一直耗电），run_event_store 像一张**便签纸**（写完就放那儿，不用专门"归还"）。前者进借物登记本（栈），后者不用进。

这条区分很重要，因为朴素写法常犯的错就是"把活资源当纯数据结构 new"——光建不还，连接池永远不关（麻烦 1、6）。`langgraph_runtime` 的精髓之一，就是**精确区分这两类，活资源进栈、纯数据直接赋值**。

### 0.6 启动期配置快照（`startup_config`）vs 请求期热重载（`get_config`）——`startup-only` 边界

这是本篇最容易和姊妹篇混淆的一个点，单独讲清。

DeerFlow 的配置（`config.yaml`）支持**热重载**：改了 `config.yaml`，**下一个请求**就能看到新值，不用重启进程。这靠的是 `get_config()`（`deps.py:186`）——它每次请求都走 `get_app_config()`，后者根据文件 mtime 决定要不要重新读盘。

但**不是所有配置项都能热重载**。有些项绑死了"启动时建一次"的单例——比如 `database.backend` 决定了连 Postgres 还是 SQLite，这个连接池在 `langgraph_runtime` 里建好后就钉死了，改 `config.yaml` 的 `database.backend` 也改不动这个已经活着的连接池。这种项叫 **`startup-only`（必须重启才生效）**。

权威清单在 `backend/packages/harness/deerflow/config/reload_boundary.py:45-75`，每项还配了一句"为什么不能热重载"的理由，而且好几条**字面点名 `langgraph_runtime`**——因为正是它把这些单例建出来的。节选（`reload_boundary.py:46-49, 71-74`）：

```python
STARTUP_ONLY_FIELDS: dict[str, str] = {
    "database": ("init_engine_from_config() runs once during langgraph_runtime() startup; "
                 "the SQLAlchemy engine holds the connection pool and is not rebuilt on config.yaml edits."),
    "checkpointer": ("make_checkpointer() binds the persistent checkpointer once at startup, ..."),
    "run_events": ("make_run_event_store() picks the memory- vs SQL-backed implementation at startup "
                   "and is frozen onto app.state.run_events_config ..."),
    "stream_bridge": ("make_stream_bridge() constructs the stream-bridge singleton once during startup."),
    "run_ownership": ("RunOwnershipConfig is captured once into RunManager at langgraph_runtime() startup; "
                      "the lease heartbeat background task is created and started there, ..."),
    ...
}
```

这恰好就是 `langgraph_runtime` 函数签名里那个参数叫 `startup_config`（而不是 `config`）的原因——它强调"我吃的是**启动时的那张快照**，不是请求时的热配置"。看 `deps.py:223, 259`：

```python
async def langgraph_runtime(app: FastAPI, startup_config: AppConfig) -> AsyncGenerator[None, None]:
    ...
    async with AsyncExitStack() as stack:
        config = startup_config          # ← 用的是启动快照
```

而这张快照从哪来？看 `app.py:182`：`startup_config = get_app_config()`——lifespan 开头拍一张，原封不动传进 `langgraph_runtime(app, startup_config)`（`app.py:240`）。

所以有一条清晰的边界：

- **请求时该热重载的字段**（比如模型参数、各 run 选项）→ 路由里调 `get_config()` 现取（姊妹篇 getter 讲的"取"）。
- **启动时钉死的字段**（`database` / `checkpointer` / `run_events` / `stream_bridge` / `run_ownership`…）→ 在 `langgraph_runtime` 里用 `startup_config` 建一次（本篇讲的"建"）。

理解了这条边界，后面看 `langgraph_runtime` 为什么"把 config 拍快照、建出单例、之后不再读这些字段"就顺理成章了。

### 0.7 多 worker / `GATEWAY_WORKERS`——进程数，以及开多 worker 的硬门槛

※ 概念基础和姊妹篇 `run-manager-deep-dive.md` §0.8 重叠，本篇只复用结论。

生产部署时 Gateway 可以跑多个进程，进程数由环境变量 `GATEWAY_WORKERS` 控制。开多 worker 不是免费的——DeerFlow 在启动时有一道守卫：**开多 worker 必须同时满足 ①数据库是 Postgres（SQLite 文件锁撑不住多进程并发）②`run_ownership.heartbeat_enabled` 开启（否则 NULL lease 会让 worker 互相误杀对方的活 run）**。

这道守卫的实现就是 `_enforce_postgres_for_multi_worker`（`deps.py:49-85`），而它**恰恰是 `langgraph_runtime` 函数体的第一行**（`deps.py:256`）。为什么放在"建任何单例之前"？因为越早失败，错误信息越清晰、对生产越安全——总比建了一半连接池再崩、留下一堆半成品要好。麻烦 5 会展开。

---

好，前置概念讲完了。现在我们开始讲代码。

## 第 1 步：朴素写法长什么样

如果没有 `langgraph_runtime`，一个想"让应用一开始就备好运行时单例"的写法，大概会在 `app.py` 模块顶层（import 时执行）直接构造（**这是反面教材，真实代码不是这样**）：

```python
# app.py 模块顶层 —— 反面教材
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

checkpointer = AsyncPostgresSaver.from_conn_string(DB_URL)   # import 时就建
run_manager = RunManager()                                     # import 时就建
bridge = StreamBridge()                                        # import 时就建

app = FastAPI(...)                                             # 然后才是建 app
```

逐行解释这种写法为什么是错的：

- **第 3 行** `checkpointer = AsyncPostgresSaver.from_conn_string(DB_URL)`
  在模块 import 阶段就构造一个连 Postgres 的 checkpointer。这一行的问题在后面会逐个展开，先记三个致命点。
- **第 4-5 行** `run_manager = RunManager()` / `bridge = StreamBridge()`
  同样在 import 阶段构造。注意它们是**全局变量**，挂在模块上——既不在 `app.state`（getter 取不到），也不在任何生命周期管理之下（关机时没人收）。
- **第 7 行** `app = FastAPI(...)`
  然后才建 app。注意顺序：单例比 app 还先建好，这意味着单例的存活范围比 app 还大——这恰恰是错的，单例应该**随 app 生、随 app 死**。

这 7 行的根本病根是：**把"建"和"还"拆散了，而且"建"的时机（import）和"还"的机制（根本没有）都不对。** 具体有六个麻烦，全是从这一个病根派生的。下面逐一展开，每个都对照 `langgraph_runtime` 的具体一行来看它怎么解决。

---

## 第 2 步：6 个麻烦逐一展开

### 麻烦 1：异步构造需要事件循环，import 时跑不了

#### 场景

DeerFlow 的运行时单例不是"new 一个对象"那么简单——它们的构造涉及**真正的异步 I/O**：

- checkpointer（postgres 后端）要 `await saver.setup()` 跑 DDL 建表（`async_provider.py:160`）。
- store 要 `async with _async_store(config)`（`store/async_provider.py:103`）。
- engine 要 `await init_engine(...)` 建连接池、bootstrap schema（`engine.py:174`）。

这些都是 `async` 操作——它们要往事件循环里挂、要等 I/O 完成。

#### 朴素写法为什么做不到

回到第 1 步：`checkpointer = AsyncPostgresSaver.from_conn_string(DB_URL)` 写在模块顶层，**import 时执行**。但 Python 模块 import 发生在事件循环启动**之前**——这时候没有运行中的事件循环，`await` 这种操作根本无地可挂。哪怕你硬写成 `asyncio.run(make_checkpointer())`，也是在 import 时强行拉起一个临时循环、跑完就关，建出来的连接池挂在那个早已死掉的临时循环上，后面请求里再用就崩了。

更糟的是，这些工厂本身是 `@asynccontextmanager`（0.4 讲过），它们**必须**通过 `async with` 或 `enter_async_context` 来用——import 时既没法 `async with`，也没人负责它们退出时的清理。

#### DeerFlow 怎么解决的：整个生命周期是 `async def`，在 `lifespan` 里 `await`

`langgraph_runtime` 整个函数就是 `async def`（`deps.py:223`），而且它被一个**已经在事件循环里**的协程调用——看 `app.py:240`：

```python
async with langgraph_runtime(app, startup_config):
```

这一行在 `lifespan`（`app.py:171` 的 `async def lifespan`）里，而 `lifespan` 是 FastAPI 在**应用启动后、由运行中的事件循环驱动**调用的。所以当 `langgraph_runtime` 执行时，事件循环已经活着了，里面的所有 `await`（建池、setup、bootstrap）都能正常挂起、等待、恢复。

逐行看 `langgraph_runtime` 内部那些"必须异步"的构造（`deps.py:261, 265, 267-268`）：

```python
app.state.stream_bridge = await stack.enter_async_context(make_stream_bridge(config))   # await 异步构造
...
await init_engine_from_config(config.database)                                          # await 建池 + bootstrap
app.state.checkpointer = await stack.enter_async_context(make_checkpointer(config))     # await setup() DDL
app.state.store = await stack.enter_async_context(make_store(config))                   # await 开 store
```

每个 `await` 都成立，因为函数是 `async def`、由活的事件循环驱动。这就从根本上杜绝了"import 时跑异步"的崩。

**对照第 1 步**：朴素写法把构造放在 import（无循环），`langgraph_runtime` 把构造放进一个在循环里跑的 `async def`、且只在应用生命周期内执行一次。这是它必须存在、且必须是异步函数的第一个理由。

---

### 麻烦 2：多个活资源要"用完按倒序还"——`AsyncExitStack`

#### 场景

Gateway 进程里要建**好几个**活资源（0.5 讲过的 checkpointer、store、stream_bridge），每个都开着连接池/客户端，用完都得关。而且它们之间有**依赖关系**：checkpointer 的连接池、store、RunManager 的持久层，都依赖底层数据库 engine 先建好。关机时顺序就得反过来——**先用依赖别人的（checkpointer/store），再关被依赖的（engine）**，否则先关了 engine，别人还在用它的连接，必崩。

#### 朴素写法为什么做不到

朴素写法（第 1 步）把这些对象当**普通全局变量** new 出来，**根本没有"还"这一步**：

- 连接池随 import 打开，**永不关闭**——进程退出时靠操作系统兜底回收（Unix 上 socket 文件可能残留，Windows 上连接泄漏），干净谈不上。
- 更致命的是**没有顺序**：你不知道谁先死谁后死。engine 可能在 checkpointer 还在写 checkpoint 时就被 OS 强杀了，留下半截 checkpoint（姊妹篇 `run-manager` §麻烦 6 讲的 issue #3373 就是这个的变种）。

#### DeerFlow 怎么解决的：`async with AsyncExitStack() as stack:` + `enter_async_context`

`langgraph_runtime` 用 `AsyncExitStack`（0.3 讲过）把所有活资源登记进栈，退出时自动 LIFO 倒序关闭。看 `deps.py:258-268`：

```python
async with AsyncExitStack() as stack:
    config = startup_config

    app.state.stream_bridge = await stack.enter_async_context(make_stream_bridge(config))

    # Initialize persistence engine BEFORE checkpointer ...
    await init_engine_from_config(config.database)

    app.state.checkpointer = await stack.enter_async_context(make_checkpointer(config))
    app.state.store = await stack.enter_async_context(make_store(config))
```

逐行：

- `async with AsyncExitStack() as stack:` 进入整摞资源管理。0.3 讲过：退出这个 `async with`（正常或异常）时，栈会按 LIFO 倒序调每个登记项的 `__aexit__`。
- `app.state.stream_bridge = await stack.enter_async_context(make_stream_bridge(config))` 把 `make_stream_bridge`（0.4 讲过它是 `@asynccontextmanager`）登记进栈：`enter_async_context` 会立刻 `await` 它的 `__aenter__`（建出 bridge），把产出值赋给 `app.state.stream_bridge`，同时把它的 `__aexit__`（关 redis 客户端/清理内存）排进栈的"还东西清单"。
- `app.state.checkpointer = await stack.enter_async_context(make_checkpointer(config))` 同理：建出 checkpointer 挂上 `app.state`，关池逻辑排进栈。
- `app.state.store = await stack.enter_async_context(make_store(config))` 同理。

**登记顺序 = 关闭的反序。** 这里登记顺序是 `stream_bridge → checkpointer → store`，所以关机时倒序就是 `store → checkpointer → stream_bridge`——后建的先关，符合"依赖别人的先关"直觉。

#### 为什么 `init_engine` 不进栈，而 `make_run_event_store` 也不进栈——两个相反的原因

这里有个**极易混**的细节，单独讲清。`langgraph_runtime` 里有两处"看起来该进栈却没进"：

**① `init_engine_from_config` 不进栈（`deps.py:265`）。** 它建的 engine/连接池是活资源，为什么不进栈？因为 engine 不是用 `@asynccontextmanager` 工厂建的——它是往 `persistence/engine.py` 的**模块级全局变量** `_engine` / `_session_factory`（`engine.py:27-28`）里写。模块全局不在 `AsyncExitStack` 的管辖范围内，所以栈管不了它的关闭。它的关闭靠**手动**：`finally` 块里 `await close_engine()`（`deps.py:343`）显式调用 `engine.py:198` 的 `close_engine()` 去.dispose() 那个全局 engine。这是"活资源但不进栈、手动还"的特殊情况，麻烦 6 会讲它和栈内资源的关闭次序。

**② `make_run_event_store` 不进栈（`deps.py:305`）。** 这和 ①相反——它是"不进栈因为**根本不需要还**"。0.5 讲过：它是普通 `def`、不持有需回收资源（`DbRunEventStore` 只借 engine 已管的 session_factory），所以建出来就够了，没有"关"这一步。

所以 `langgraph_runtime` 里"进不进栈"的判据很清晰：**是 `@asynccontextmanager` 且持有活资源 → 进栈；否则不进。** engine 是"活但不进栈"的唯一例外，靠手动 `close_engine()` 兜，这是个历史包袱式的特例。

朴素写法连"还"的概念都没有，所有活资源都是光建不还。`langgraph_runtime` 用 `AsyncExitStack` 把"建+还"绑成一对、还自动排好倒序，这是它必须存在的第二个理由。

---

### 麻烦 3：构造顺序——engine 必须先于 checkpointer

#### 场景

这是个具体的、容易踩的顺序陷阱。Postgres 后端下，checkpointer 在构造时要干两件事（`async_provider.py:152-162`）：

```python
pool = _build_postgres_pool(db_config.postgres_url)   # 建连接池，且 check=... 会立即验证连接
async with pool:                                        # 打开池（此时就要连得上数据库）
    saver = AsyncPostgresSaver(conn=pool)
    await saver.setup()                                 # 跑 CREATE TABLE DDL
    yield saver
```

两件事都要求**目标数据库已经存在**：建池时会试着连那个库（连不上就抛），`setup()` 要往里建表。但如果这是一个**全新的 Postgres 部署**——数据库 `deerflow` 根本还没被建出来呢——这一步直接失败。

#### 朴素写法为什么做不到

朴素写法在 import 时 `checkpointer = AsyncPostgresSaver.from_conn_string(DB_URL)`，没有任何"先建库再建 checkpointer"的编排。新部署一启动就抛"database does not exist"，而且是在 import 阶段崩，错误信息难看、定位困难。

#### DeerFlow 怎么解决的：`init_engine` 在前（含 auto-create db），checkpointer 在后

`langgraph_runtime` 把顺序写死了（`deps.py:263-268`）：

```python
# Initialize persistence engine BEFORE checkpointer so that
# auto-create-database logic runs first (postgres backend).
await init_engine_from_config(config.database)            # ① 先建 engine（含建库）

app.state.checkpointer = await stack.enter_async_context(make_checkpointer(config))   # ② 再建 checkpointer
app.state.store = await stack.enter_async_context(make_store(config))
```

逐行看为什么 ① 必须在前：

- **① `await init_engine_from_config(config.database)`**（`engine.py:174`）里藏着"建库"逻辑。它内部调 `init_engine`（`engine.py:58`），后者在 bootstrap schema 时若捕获到 "database does not exist"（`engine.py:159-169`），会转去调 `_auto_create_postgres_db(url)`（`engine.py:31-55`）——连到维护库 `postgres` 上执行 `CREATE DATABASE`，把 `deerflow` 库建出来，然后重建 engine、再 bootstrap schema。**这一步之后，目标库一定存在了。**
- **② `make_checkpointer`** 这时才上场建池、`setup()`——因为 ① 已经把库建好了，所以"连库"和"建表"都能成功。

如果顺序反了（先 checkpointer 后 engine），全新部署会在 checkpointer 这一步崩在"database does not exist"，因为还没人建过库。注释里那句"auto-create-database logic runs first"（`deps.py:264`）点破的就是这个顺序约束。

#### 附带：`sf is not None` 的 db vs memory 分支

顺带讲紧挨着的一段（`deps.py:271-282`），因为它也依赖"engine 必须先建好"：

```python
sf = get_session_factory()                                # 取 engine 建好的全局 session_factory
if sf is not None:
    from deerflow.persistence.feedback import FeedbackRepository
    from deerflow.persistence.run import RunRepository
    app.state.run_store = RunRepository(sf)               # 有 DB → 用持久仓库
    app.state.feedback_repo = FeedbackRepository(sf)
else:
    from deerflow.runtime.runs.store.memory import MemoryRunStore
    app.state.run_store = MemoryRunStore()                # 没 DB（backend=memory）→ 用内存仓库
    app.state.feedback_repo = None
```

逐行：

- `sf = get_session_factory()`（`engine.py:188`）返回 `init_engine` 设的那个模块全局。**它必须在 `init_engine_from_config` 之后调**——这就是为什么这段紧跟在 ① 后面。后端是 `memory` 时它是 `None`，否则是个 `async_sessionmaker`。
- `if sf is not None:` 有数据库——用 SQL 实现的仓库（`RunRepository` / `FeedbackRepository`），跨重启持久。
- `else:` 没数据库（纯本地开发、`backend=memory`）——退回内存实现（`MemoryRunStore`），重启即丢但开发时不用起 DB。

这就是"配了数据库就用持久层、没配就用内存层"的二选一，它成立的前提是 engine 已经建好、`get_session_factory()` 能返回正确结果。顺序错了这里也会出错。

朴素写法完全没有这种"先建库、后建依赖库的东西"的编排。`langgraph_runtime` 把构造顺序写死成"engine → checkpointer/store → 依赖 sf 的仓库"，这是它必须存在、且是顺序敏感的第三个理由。

---

### 麻烦 4：进程重启后系统状态要自愈——孤儿对账 + 重启心跳

#### 场景

进程崩了重启（或滚动发版）。重启前，可能有好几个 run 还在跑（数据库 `runs` 表里标着 `running`），但它们的 agent task 已随旧进程一起死了——**永远不会再有事件产出、永远不会自己结束**。这是姊妹篇 `run-manager-deep-dive.md` §麻烦 5 详讲的"孤儿 run"问题。

但孤儿恢复这套逻辑**谁来触发**？它得在进程启动的某个时刻被调用一次。同时，多 worker 部署下，每个 worker 还得**重启自己的心跳后台任务**（否则没人续租，所有 lease 慢慢过期）。这些"开机自愈"动作，自然该归"装配总闸"管。

#### 朴素写法为什么做不到

朴素写法没有 `langgraph_runtime`，开机自愈就没有一个统一的触发点——要么漏掉、要么散落在各处、要么顺序错（比如心跳还没建好就先对账，或对账前还没建 RunManager）。

#### DeerFlow 怎么解决的：建好 RunManager 后，紧接着做三件自愈 + 起心跳

看 `deps.py:307-330`（RunManager 建好之后、`yield` 之前）：

```python
# RunManager with store backing for persistence
run_ownership_config = getattr(config, "run_ownership", None)
app.state.run_manager = RunManager(
    store=app.state.run_store,
    run_ownership_config=run_ownership_config,
)
# Startup recovery: mark inflight runs whose lease has expired as error. ...
from deerflow.utils.time import now_iso

recovered_runs = await app.state.run_manager.reconcile_orphaned_inflight_runs(
    error="Gateway restarted before this run reached a durable final state.",
    before=now_iso(),
)
sb_config = getattr(config, "stream_bridge", None)
cleanup_delay = getattr(sb_config, "recovered_stream_cleanup_delay_seconds", 60.0) if sb_config else 60.0
await _publish_recovered_run_stream_end(app.state.stream_bridge, recovered_runs, cleanup_delay=cleanup_delay)
await _mark_latest_recovered_threads_error(app.state.run_manager, app.state.thread_store, recovered_runs)

# Start the lease heartbeat if enabled (multi-worker deployments).
await app.state.run_manager.start_heartbeat()
```

逐行（重点讲顺序和依赖）：

- **先建 RunManager**（`deps.py:309-312`）`app.state.run_manager = RunManager(store=app.state.run_store, ...)`。注意它依赖 `app.state.run_store`——后者在麻烦 3 那段已经建好了。**顺序：run_store 在前、RunManager 在后**，这是依赖关系决定的。
- **`reconcile_orphaned_inflight_runs`**（`manager.py:1088`）※ 孤儿恢复的逻辑姊妹篇 §麻烦 5 详讲过——扫数据库里"lease 已过期但仍 running"的 run，标成 error，返回 `list[RunRecord]`。本篇只强调：**它必须在 RunManager 建好之后调**（要用 RunManager 的 `store`），所以紧贴在 RunManager 构造之后。
- **`_publish_recovered_run_stream_end`**（`deps.py:114`）对每个被收尸的 run 调 `bridge.publish_end(run_id)`——**代替死掉的 agent 给还等着的订阅者发 END 信号**。※ 姊妹篇 `stream-bridge` §麻烦 4 讲过"孤儿 run 的订阅者会傻等 END"——这里的 `publish_end` 就是补救。注意它依赖 `app.state.stream_bridge`（麻烦 2 已建好），所以也在 bridge 之后。
- **`_mark_latest_recovered_threads_error`**（`deps.py:162`）把"最新一条 run 是被收尸的"thread 的状态也标成 error（thread_meta 层面），让前端列表正确反映"这个会话上次是失败的"。它依赖 `app.state.thread_store`。
- **`start_heartbeat()`**（`manager.py:1188`）启动心跳后台任务。※ 未启用 `heartbeat_enabled` 时是 no-op（单 worker 模式），多 worker 时才真起。**它必须在孤儿对账之后**——因为对账是"启动时全量扫一次"，心跳是"之后周期性续租"；先扫干净再起心跳，避免心跳和对账打架。

这套"建 RunManager → 对账 → 发 END → 标 thread → 起心跳"是严格排序的自愈流水线，每一步都依赖前一步建好的单例。朴素写法根本没有这个统一的开机自愈点。这是 `langgraph_runtime` 必须存在的第四个理由。

---

### 麻烦 5：多 worker 下 SQLite 会互相踩——`_enforce_postgres_for_multi_worker`

#### 场景

※ 多 worker 的并发陷阱姊妹篇 `run-manager` §0.8 / `get-checkpointer` §麻烦 5 都讲过：SQLite 是文件级写锁，撑不住多进程并发写同一个库文件，硬上会到处 `database is locked`；而 Postgres 才是真正的多进程并发数据库。所以要开多 worker（`GATEWAY_WORKERS > 1`），数据库必须是 Postgres。

#### 朴素写法为什么做不到

朴素写法没有启动守卫——你配了 `GATEWAY_WORKERS=4` + SQLite，进程照样起来，然后**在生产里**才暴露成各种诡异的 `database is locked`、连接冲突、数据损坏。出问题时已经晚了。

#### DeerFlow 怎么解决的：`langgraph_runtime` 第一行就硬性拒绝

看 `deps.py:252-256`（函数体最开头）：

```python
# ------------------------------------------------------------------
# Multi-worker safety gate: reject SQLite when GATEWAY_WORKERS > 1.
# SQLite write-locks cannot support concurrent multi-process access.
# ------------------------------------------------------------------
_enforce_postgres_for_multi_worker(startup_config)
```

这是 `langgraph_runtime` 干的**第一件事**——在任何单例被建出来之前。`_enforce_postgres_for_multi_worker`（`deps.py:49-85`）的逻辑（※ 结论姊妹篇讲过，这里只点它在装配链里的位置意义）：

```python
def _enforce_postgres_for_multi_worker(config: AppConfig) -> None:
    try:
        workers = int(os.environ.get("GATEWAY_WORKERS", "1"))
    except (TypeError, ValueError):
        workers = 1
    if workers <= 1:
        return                                          # 单 worker：不查
    backend = getattr(config.database, "backend", None)
    if backend != "postgres":
        raise SystemExit(f"GATEWAY_WORKERS={workers} requires database.backend='postgres', ...")   # ★ 直接退出
    run_ownership = getattr(config, "run_ownership", None)
    if run_ownership is None or not run_ownership.heartbeat_enabled:
        raise SystemExit(f"GATEWAY_WORKERS={workers} requires run_ownership.heartbeat_enabled=true, ...")  # ★ 直接退出
```

**为什么放在函数第一行、任何构造之前？** 因为这是"安全闸"，目标是"不达标就别启动"。越早失败越好：

- 如果放在建好 engine、checkpointer 之后再生 `SystemExit`，进程已经开了一堆连接池才死——留下半成品、错误信息也混乱。
- 放在最开头，要么 `return`（单 worker，正常往下建）要么直接 `SystemExit`（多 worker 但不达标，进程立即退出、给出清晰错误信息）。**宁可启动失败，也不能在生产里踩坑。**

这正是 0.7 说的"开多 worker 是有代价的，得先把配套准备好"在代码里的落地。朴素写法连这个闸都没有，多 worker + SQLite 会静默启动、在生产里出诡异问题。这是 `langgraph_runtime` 必须存在、且守卫要在最前的第五个理由。

---

### 麻烦 6：优雅停机——run 不能随进程暴毙（issue #3373）

#### 场景

`make stop`、滚动发版、收到 SIGTERM——进程要退出了。这时进程里可能还有好几个 run 在后台跑（agent task 正在写 checkpoint）。如果进程**直接退出**会发生什么？

agent 执行中会**持续写 checkpoint**。如果强退，正在写 checkpoint 的操作可能被打断到一半——半截 checkpoint 留在盘上。更具体、也更阴险的坑是 issue #3373：langgraph 内部有个 `_checkpointer_put_after_previous` 机制，它在**独立的后台 task** 里调 `checkpointer.aput(...)` 写 checkpoint。进程退出时，如果 gateway 先把 checkpointer 的**连接池关了**，而这个 put task 还没跑完，它就会撞上一个**已关闭的连接池**，抛 `PoolClosed`——而且这个异常在 langgraph 内部 task 里，主调用栈的 try/except 抓不到，最终冒泡成 `asyncio.run()` 关闭时的未处理异常。

所以关机顺序必须是：**先把所有在跑的 run 排干（让它们干净地写完最后一档），再关 checkpointer 的连接池。**

#### 朴素写法为什么做不到

朴素写法的单例是光建不还的全局变量，进程退出时全靠 OS 粗暴回收 task，没有任何"先排干 run、后关池"的编排——必然撞 #3373：池先被 OS 回收（或被某种顺序关掉），put task 还在写，`PoolClosed` 冒泡。

#### DeerFlow 怎么解决的：`try: yield finally:` 里"先 drain 后 close_engine"

`langgraph_runtime` 的整个生命周期结构是个经典的 `try/finally`（`deps.py:332-343`）：

```python
try:
    yield                                                  # ← 应用正常运行、接请求
finally:
    # Drain in-flight run tasks BEFORE the AsyncExitStack tears down the
    # checkpointer (and its connection pool). A run still mid-graph would
    # otherwise leak into asyncio.run() shutdown, where langgraph's
    # _checkpointer_put_after_previous aput races the closed pool and
    # raises PoolClosed (issue #3373).
    run_manager = getattr(app.state, "run_manager", None)
    if run_manager is not None:
        await _drain_inflight_runs(run_manager)            # ① 先排干 run
    await close_engine()                                   # ② 再关 engine/池
```

逐行，重点讲顺序和 `finally`：

- **`try: yield`** 0.2 讲过——`yield` 这一下表示"应用正式运行"。`yield` 之前的代码是开机（麻烦 1-5），`yield` 之后（`finally` 里）是关机。`try/finally` 保证：**无论应用是正常退出还是异常退出，关机逻辑一定执行。**
- **`finally:` 块** 进程退出（lifespan 的 `async with` 离开、或异常）时进这里。
- **① `await _drain_inflight_runs(run_manager)`** **先**排干所有在跑的 run。`_drain_inflight_runs`（`deps.py:88-111`）把 `RunManager.shutdown(timeout=5.0)`（※ 内部逻辑姊妹篇 §麻烦 6 详讲：先停心跳、取消各 task、`asyncio.wait(timeout)` 有界等待它们干净收尾、保留真实终态）包了一层 `asyncio.shield`：

  ```python
  async def _drain_inflight_runs(run_manager: RunManager) -> None:
      drain = asyncio.create_task(run_manager.shutdown(timeout=_RUN_DRAIN_TIMEOUT_SECONDS))
      try:
          await asyncio.shield(drain)                      # shield：保护 drain 不被外部取消
      except asyncio.CancelledError:
          try:
              await asyncio.shield(drain)                  # 被取消也要等 drain 跑完（它是有界的）
          except Exception:
              logger.exception("In-flight run drain failed after shutdown cancellation")
          raise
      except Exception:
          logger.exception("Failed to drain in-flight runs during shutdown")
  ```

  `asyncio.shield(drain)` 的作用：即使外层（lifespan 协程本身）被取消——比如**第二次 SIGINT** 或服务器的 graceful-shutdown 超时（正是 #3373 那种信号风暴）——`drain` 这个 task **不会被取消**，会继续跑完。为什么这么执着？因为 drain 是**有界的**（最多 5 秒，`_RUN_DRAIN_TIMEOUT_SECONDS=5.0`，`deps.py:46`），让它跑完不会无限挂；但如果中途被取消，就又回到"池还没关、run 还在写"的 #3373 状态。所以宁可多等几秒，也要让 drain 干净跑完——`except CancelledError` 里**再 `await asyncio.shield(drain)` 一次**，哪怕这次自己也得把 CancelledError 重新抛出（`raise`）以尊重关闭信号。
- **② `await close_engine()`** **再**关 engine 及其连接池。这时所有 run task 都已 drain 完，**不会再有 `aput` 撞已关闭的 pool**——#3373 的根因被消除。注意：这里关的是 `engine.py` 的模块全局 engine（麻烦 2 讲过它是"活但不进栈、手动还"的特例），不是栈里的 checkpointer；但 checkpointer 用的 pool 在 postgres 后端下是它**自己**开的（`async_provider.py:153`），那条池的关闭由 `AsyncExitStack` 退出时 `make_checkpointer` 的 `__aexit__` 负责——而栈退出发生在整个 `langgraph_runtime` 的 `async with` 离开时，**也在 `close_engine` 之后**（因为 `close_engine` 在 `finally` 里先跑，栈的 `__aexit__` 在外层 `async with` 退出时跑）。所以最终关闭顺序是：**drain run → close_engine（全局 engine 池）→ AsyncExitStack 倒序关 store/checkpointer/stream_bridge（它们各自的池）**。run 一定最先被排干，任何池都不会在 run 还在写时被关。

#### 为什么 `try/finally` + `AsyncExitStack` 的退出顺序能咬合

这里有个细节值得点明：`langgraph_runtime` 里**同时**有 `async with AsyncExitStack() as stack:`（麻烦 2）和 `try: yield finally:`（本麻烦）。它们的退出顺序怎么咬？

看缩进关系：`try/yield/finally` 是在 `async with AsyncExitStack()` **内部**的（`deps.py:258` 的 `async with` 比 `deps.py:332` 的 `try` 更靠外）。所以退出时是**由内向外**：

```
退出 langgraph_runtime：
  finally 块先跑（仍在 stack 内）：
    ① await _drain_inflight_runs(run_manager)   ← drain run（此时栈内单例的池都还开着，run 能写完最后一档）
    ② await close_engine()                       ← 关全局 engine 池
  然后离开 async with AsyncExitStack()（栈退出）：
    ③ 倒序调各 __aexit__：store → checkpointer → stream_bridge（它们各自的池）
```

所以顺序是 **① drain → ② close_engine → ③ 栈倒序关单例**。关键点：**drain 跑的时候（①），所有池（engine 的、checkpointer 的）都还开着**——这正是 #3373 修复的精髓：让 run 在"池还活着"的环境里干净写完最后一档，然后再逐层关池。如果 drain 放在 close_engine 之后，run 就会在"engine 池已关"的状态下试图写 checkpoint，必撞 PoolClosed。

朴素写法既不排干 run、也不编排关池顺序，必然踩 #3373。`langgraph_runtime` 用"`try/finally` 先 drain + 手动 close_engine"打底、外层 `AsyncExitStack` 兜单例池的倒序关闭，把优雅停机管死了。这是它必须存在、且结构必须是"内 try/finally + 外 AsyncExitStack"的第六个理由。

---

## 第 3 步：把 6 个麻烦的本质串起来

朴素写法（在 `app.py` 顶部随手构造运行时单例）的根本问题是：

> **它把"运行时单例的构造（建）"和"它们的回收（还）"都从应用生命周期里剥离了出来——建得太早（import，没事件循环）、还得太晚（根本没人还）、而且完全没有顺序。**

而现实里，运行时单例从生到死要经历一连串有严格时序的事，这些事在朴素写法里**全都没有归宿**：

| 维度 | 朴素写法（import 时 new） | `langgraph_runtime`（生命周期函数） |
|---|---|---|
| 建的时机 | import 时（无事件循环，异步构造崩） | lifespan 内、活的事件循环里 `await`（麻烦 1） |
| 资源回收 | 不还（连接池永不关，靠 OS 兜底） | `AsyncExitStack` 倒序自动关（麻烦 2） |
| 构造顺序 | 无编排（checkpointer 可能早于建库） | engine 先（含 auto-create db）→ checkpointer/store → 依赖 sf 的仓库（麻烦 3） |
| 重启自愈 | 无触发点（孤儿 run 永远卡 running） | 建 RunManager 后统一对账 + 发 END + 标 thread + 起心跳（麻烦 4） |
| 多 worker 安全闸 | 无（SQLite + 多 worker 静默启动、生产里出事） | 函数第一行硬性拒绝，不达标 `SystemExit`（麻烦 5） |
| 优雅停机 | 无顺序（撞 #3373 `PoolClosed`） | `try/finally` 先 drain run、后关池（麻烦 6） |

把 6 个麻烦和代码行对应起来：

- 麻烦 1（异步构造）→ 整个函数是 `async def`、在 `app.py:240` 的 `async with` 里 `await`（`deps.py:261,265,267,268`）。
- 麻烦 2（活资源倒序还）→ `async with AsyncExitStack() as stack:` + 三个 `enter_async_context`（`deps.py:258,261,267,268`）；`make_run_event_store` 不进栈（`deps.py:305`）。
- 麻烦 3（engine 先于 checkpointer）→ `init_engine_from_config` 在 `make_checkpointer` 之前（`deps.py:265,267`）；`sf is not None` 分支（`deps.py:271-282`）。
- 麻烦 4（重启自愈）→ 建 RunManager（`deps.py:309`）→ 对账（`deps.py:320`）→ 发 END（`deps.py:326`）→ 标 thread（`deps.py:327`）→ 起心跳（`deps.py:330`）。
- 麻烦 5（多 worker 闸）→ 函数第一行 `_enforce_postgres_for_multi_worker`（`deps.py:256`）。
- 麻烦 6（优雅停机）→ `try: yield finally:` 里先 `_drain_inflight_runs` 后 `close_engine`（`deps.py:332-343`）。

### 一张全景图

把 `langgraph_runtime` 放回整条装配链里看，它就是连接"进程启动"和"每个请求取依赖"的那个**中央环节**：

```
┌──────────────────────────────────────────────────────────────────────────┐
│              进程启动 / 关机（外总闸 lifespan，app.py:171）                │
│                                                                          │
│   async with langgraph_runtime(app, startup_config):   ← 内总闸 deps:222 │
│     │                                                                    │
│     │  麻烦5: _enforce_postgres_for_multi_worker()      ─── 第一行闸门   │
│     │                                                                    │
│     │  async with AsyncExitStack() as stack:           ─── 麻烦2 资源栈  │
│     │    │                                                               │
│     │    │  [进栈] stream_bridge = enter_async_context(make_stream_bridge)│
│     │    │                                                               │
│     │    │  麻烦3: await init_engine_from_config()      ─── 先建库(含建库)│
│     │    │           └─ engine.py 全局 _engine/_session_factory 被设好   │
│     │    │                                                               │
│     │    │  [进栈] checkpointer = enter_async_context(make_checkpointer) │
│     │    │  [进栈] store        = enter_async_context(make_store)        │
│     │    │                                                               │
│     │    │  sf = get_session_factory()                  ─── 依赖麻烦3已建│
│     │    │  run_store = RunRepository(sf) 或 MemoryRunStore()            │
│     │    │  thread_store / scheduled_task_repo / ...                    │
│     │    │                                                               │
│     │    │  run_event_store = make_run_event_store(...)  ─── 不进栈(纯数据)│
│     │    │                                                               │
│     │    │  麻烦4: run_manager = RunManager(store=run_store, ...)        │
│     │    │        reconcile_orphaned_inflight_runs()  ─── 孤儿对账       │
│     │    │        _publish_recovered_run_stream_end()  ─── 给订阅者发END │
│     │    │        _mark_latest_recovered_threads_error()                 │
│     │    │        run_manager.start_heartbeat()        ─── 重启心跳      │
│     │    │                                                               │
│     │    │  try:                                                         │
│     │    │      yield  ◀───────────── 应用开始接请求（所有单例已就位）   │
│     │    │  finally:                     ─── 麻烦6 优雅停机              │
│     │    │      ① await _drain_inflight_runs(run_manager)  ← 先排干run   │
│     │    │         └─ asyncio.shield(shutdown(timeout=5.0))              │
│     │    │      ② await close_engine()                     ← 后关engine池│
│     │    │                                                               │
│     │    └─ 离开 async with AsyncExitStack()（LIFO倒序关各单例池）       │
│     └────────────────────────────────────────────────────────────────── │
└──────────────────────────────────────────────────────────────────────────┘
                                  │ app.state.{checkpointer, store, stream_bridge,
                                  │             run_manager, ...}（进程级工具箱）
                                  ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                          每次 HTTP 请求                                    │
│                                                                          │
│   checkpointer = get_checkpointer(request)   ←─┐                        │
│   run_manager  = get_run_manager(request)     ←─┼ 三姊妹 getter          │
│   bridge       = get_stream_bridge(request)   ←─┘ (deps.py:364-366)      │
│   ... 每个都从 app.state 取，取不到抛 503（姊妹篇已详讲）                  │
└──────────────────────────────────────────────────────────────────────────┘
```

这张图和姊妹篇 `get-checkpointer-deep-dive.md` 结尾那张图是**同一张图的两端**：那张图从 `get_checkpointer(request)` 往上看，看到 `app.state.checkpointer` 是 `make_checkpointer` 在 `langgraph_runtime` 里建出来的；本篇这张图从 `langgraph_runtime` 往下看，看到它建出的单例最终被每个请求的 getter 取走。两图互洽，闭合整条链。

### 收束

所以 `langgraph_runtime` 不是"代码好看"或"封装一下"——是这 6 个麻烦里**任意一个**单拿出来，朴素写法都过不去：

- 它把"**建**"放进一个有事件循环的 `async def`（麻烦 1）。
- 它用 `AsyncExitStack` 把"**建+还**"绑成一对、并自动排好倒序（麻烦 2）。
- 它把构造**顺序**写死（engine 先、checkpointer 后，含 auto-create db）（麻烦 3）。
- 它把开机**自愈**集中在 `yield` 之前（对账 + 发 END + 标 thread + 起心跳）（麻烦 4）。
- 它把多 worker **安全闸**放在函数第一行（麻烦 5）。
- 它用 `try/finally` 把关机**顺序**管死（先 drain run、后关池，修 #3373）（麻烦 6）。

它和三姊妹 getter 是同一类东西的**上下游**：

- **`langgraph_runtime`（本篇）**——"**建与还**"：进程开机时把运行时单例按序建出来挂上 `app.state`、关机时按序回收。
- **`get_stream_bridge`（`deps.py:364`）**——"**取**"事件流单例。
- **`get_run_manager`（`deps.py:365`）**——"**取**"执行+状态单例。
- **`get_checkpointer`（`deps.py:366`）**——"**取**"存档单例。

三个 getter 紧挨着写在 `deps.py:364-366`，是因为它们取的都是同一种东西——"**启动时建一次、放进 `app.state`、每个请求取一次、取不到就 503**"的进程级运行时单例。而把"启动时建一次"这一半做对、做全的，就是 `langgraph_runtime`。理解了它，就理解了整条"装配 → 取用 → 回收"链的上游；理解了三姊妹，就理解了下游。两者合起来，才是"一个 Gateway 进程的运行时单例为什么这样设计"的完整答案。

代码上，这一切都从 `app.py:240` 这一行开始：

```python
async with langgraph_runtime(app, startup_config):
```

它把整个 LangGraph 运行时的**诞生和消亡**包在了一对 `async with ... :` 里——进去时六个麻烦各有归宿（建、序、自愈、闸），出来时关机有序（drain、close）。这就是 `langgraph_runtime` 存在的全部理由。

