# lifespan 深度讲解：为什么不能直接在 import 时 `checkpointer = PostgresSaver(...)` 就完事

本文是 `stream-bridge-deep-dive.md` / `run-manager-deep-dive.md` / `get-checkpointer-deep-dive.md` 的姊妹篇。那三篇讲的是 `bridge = get_stream_bridge(request)`、`run_mgr = get_run_manager(request)`、`checkpointer = get_checkpointer(request)` 这三行为什么存在；这篇讲的是**把它们仨"放进工具箱"和"从工具箱拿出来"的那个总闸**——那个总闸就是 `lifespan`。

四篇文章回答的是同一个大问题的四个面：**一次 HTTP 请求里启动的 agent run，凭什么能活得比这次请求久？** StreamBridge 负责"事件流"这一半，RunManager 负责"执行 + 状态"这一半，`get_checkpointer` 负责"agent 的存档存在哪儿"那一层，而 `lifespan` 负责的是最外面那一圈——**"这些会占用数据库连接、会持有外部资源的对象，什么时候被建出来、什么时候被关掉、进程死了/重启了这些资源怎么收拾"**。建议四篇对照着读。

具体到源头，本文讲的就是这两段连在一起、互相嵌套的代码：

- 外层 `lifespan`：`backend/app/gateway/app.py:170-341`
- 内层 `langgraph_runtime`：`backend/app/gateway/deps.py:222-343`

后者是前者调用的一个 async context manager。两段代码合起来，才是一个完整的"开机初始化 → 正常服务 → 关机清理"的生命周期。光看外层、把内层当成黑盒，会漏掉一半的精髓（`lifespan` 里的 `yield` 其实**在** `langgraph_runtime` 里面，关闭顺序的精妙全在内层）。所以本文把两层当一个整体讲透。

本文面向第一次接触这套架构的同学。先把所有需要的前置概念一个一个铺出来，然后再讲 6 个麻烦，每段代码逐行解释。

---

## 第 0 步：先把要用的术语讲清楚

在讲任何代码之前，先把后面会反复出现的概念讲明白。这些是理解"为什么朴素写法会出问题"的地基。已经读过姊妹篇的同学可以快速跳过带 ※ 标记的小节（那里会注明"姊妹篇讲过"，本篇只复用结论）。

### 0.1 lifespan——开机/关机的总闸

※ 概念基础和姊妹篇 `get-checkpointer-deep-dive.md` §0.4 重叠，这里聚焦在"`yield` 把函数劈成两半"这一个点。

FastAPI 给了一个机制叫 **lifespan**：你给它一个 `async` 生成器函数，它会在**应用启动时执行 `yield` 之前的那段代码，应用关闭时执行 `yield` 之后的那段代码**。这就给了一个"开机做初始化、关机做清理"的总闸。看签名（`app.py:170-172`）：

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""
```

逐行：

- `@asynccontextmanager` 这个装饰器来自标准库 `contextlib`。它的作用是把一个"里面有 `yield` 的 async 函数"包装成一个 async context manager——也就是能用在 `async with ...` 里的对象。换句话说，它把"一个函数"变成"一对开机/关机动作"。
- `async def lifespan(app: FastAPI)` 一个 async 函数，FastAPI 会在启动和关闭时调用它。参数 `app` 是当前 FastAPI 应用实例——后面所有"把对象挂到工具箱"的操作，都是往 `app.state` 上塞。
- `-> AsyncGenerator[None, None]` 返回类型是个异步生成器。两个 `None`：第一个是 `yield` 产出的值（这里不产出任何东西给外部，所以是 `None`），第二个是 `send()` 进来的值（FastAPI 不会 `send`，也是 `None`）。**关键是：这个函数里必须有一个 `yield`，它把函数体劈成两半**——`yield` 前面是开机代码，后面是关机代码。在本篇里你会看到这个 `yield` 在 `app.py:275`，但有意思的是，它**被包在另一个 async context manager 里**（下面 0.4 会讲）。

打个比方：lifespan 就像大楼的总电闸——合闸时（启动）逐层送电、逐个设备开机；拉闸时（关机）按相反顺序逐个设备关停、最后断总闸。这个"开机/关机的对称性"是后面麻烦 3/4/6 反复出现的主题。

### 0.2 import-time vs startup-time——★本篇的核心概念

这一节是本篇的灵魂，请重点看。

Python 里，一个 `.py` 文件在被**第一次 `import`** 时，会**从上到下执行一遍模块顶层的代码**——这一刻叫 **import-time**。比如你在模块顶层写 `x = expensive()`，那么 `import` 这个模块时 `expensive()` 就立刻跑了。

而 **lifespan 里的代码**，是在 uvicorn 启动 FastAPI 应用、走到 `lifespan()` 这个函数时才执行的——这一刻叫 **startup-time**（关机时的 `yield` 之后那段叫 **shutdown-time**）。

这两者**差一整个生命周期**：

| | import-time | startup-time |
|---|---|---|
| 什么时候发生 | 模块被第一次 `import` 的那一瞬 | uvicorn 启动应用、跑 lifespan 的时候 |
| 谁触发 | 任何一句 `import app.gateway.app`（比如测试、别的模块、甚至 uvicorn 自己加载） | uvicorn 明确"我现在要起服务了" |
| 跑几次 | 一个进程里每个模块只跑一次（首次 import） | 每次启动进程跑一次 |
| config.yaml 在不在 | 不保证（可能用户还没 `make config`） | 一定在（用户既然要起服务） |
| 数据库在不在 | 不保证 | 一定在（否则 lifespan 会自己起失败） |
| 失败的后果 | `import` 直接崩，连 `/setup`、连测试都进不去 | 服务起不来，但 import 本身没事 |

朴素写法（第 1 步会贴）的根病就是：**把"该 startup-time 做的事"写在了 import-time**。这一字之差，埋下了本篇全部的雷。

打个比方：import-time 是"图纸刚画好、还没开工"的瞬间；startup-time 是"今天正式开业"的那一刻。你总不能在图纸阶段就把厨房的煤气接通、把员工招满（import-time 开数据库连接）；也不能图纸一改就把昨天卖掉的菜全撤销（import-time 的副作用没法对称关停）。lifespan 就是把"开业才做的事"和"打烊才做的事"分别钉死在两个正确的时刻。

### 0.3 `app.state`——进程级"工具箱"

※ 这个概念姊妹篇 `get-checkpointer-deep-dive.md` §0.3 已经讲过（"前台桌子底下的公共工具箱"），本篇只复用结论。

FastAPI 应用对象 `app` 上挂着一个属性 `app.state`，它是**进程级**的"工具箱"：启动时往里塞东西（`app.state.checkpointer = ...`），每个请求里从里取东西（`get_checkpointer(request)`）。一个 worker 进程就有一个工具箱，进程死了工具箱也跟着没了。

本篇最重要的一件事就是：**"往工具箱里塞"的动作全都发生在 lifespan 的开机段（`yield` 之前）**，而三篇姊妹篇讲的"从工具箱里取"的动作发生在每个请求里。本篇讲的就是"塞"这一半。

⚠️ "进程级"三个字意味着：多 worker 部署时，每个 worker 各跑一遍 lifespan、各有自己的工具箱、各开自己的数据库连接。这正是后面麻烦 3 那道多 worker 守卫的根。

### 0.4 `AsyncExitStack` + LIFO teardown——后开的先关

这是本篇麻烦 3/4 的关键工具，得先讲清楚。

Python 标准库 `contextlib` 提供了 `AsyncExitStack`。你可以把它理解成"一摞 `async with`"：往里 `enter_async_context(...)` 一个东西，它就把这个 context manager 登记下来；等 `AsyncExitStack` 自己退出时（它的 `__aexit__`），它会**按登记的反序**逐个调用这些 context manager 的退出逻辑。**反序 = 后进先出 = LIFO（Last In, First Out）。**

举个例子，假设你这么写：

```python
async with AsyncExitStack() as stack:
    a = await stack.enter_async_context(open_A())   # 先开 A
    b = await stack.enter_async_context(open_B())   # 再开 B
    c = await stack.enter_async_context(open_C())   # 再开 C
    ...                                              # 正常用
# 退出时：先关 C，再关 B，最后关 A
```

为什么是"后开的先关"？因为**资源之间往往有依赖**：A 可能是数据库连接池，B 是基于 A 建的会话，C 是基于 B 跑的事务。关的时候必须**从最上层往最底层关**——先关 C（把事务收尾），再关 B（归还会话），最后关 A（关连接池）。如果反过来先关 A（连接池），那 C 这个正在跑的事务就会撞上一个已关闭的连接池，炸了。**LIFO 保证了这种"谁依赖谁"的关闭顺序天然正确。**

打个比方：穿衣服——先穿内衣、再穿衬衫、最后套外套（开机顺序）；脱衣服就得反过来——先脱外套、再脱衬衫、最后脱内衣（LIFO）。你总不能先脱内衣再脱外套，那就尴尬了。

本篇麻烦 4 讲的 issue #3373，本质上就是"如果不按 LIFO、把底层连接池先关了、上层还在用它"，就会撞 `PoolClosed`。`langgraph_runtime` 用 `AsyncExitStack` 把这件事管死了。

### 0.5 "持资源对象" vs "纯数据对象"——不能随手 new 的那一类

把 Python 对象粗分两类：

- **纯数据对象**：一个 `dict`、一个 `list`、一个 dataclass。它们只活在内存里，不占外部资源，丢了就丢了，`new` 一个几乎不要钱。随手写 `x = {}` 没人管你。
- **持资源对象**：一个数据库连接池、一个 checkpointer（背后是数据库连接）、一个 stream bridge（背后是订阅者队列 + 可能的 Redis）、一个 tracer（背后是进程全局的 monkey-patch）。它们**会占用外部资源**——打开连接、持有文件句柄、装一个进程级的 hook。这种对象**不能随手 new、更不能随手丢**：
  - new 它要花代价（建连接、握手）。
  - 用完必须显式关（否则连接泄漏）。
  - 进程死了要收拾（半截事务要回滚、半截 checkpoint 要 flush）。

DeerFlow 里 StreamBridge、RunManager、checkpointer、store、persistence engine、IM channel service、scheduled task service——**全是持资源对象**。本篇讲的就是"这些持资源对象，怎么被生命周期管起来"。

打个比方：纯数据对象像一张便签纸（随手撕、随手扔）；持资源对象像一辆出租车（要租、要还、中途坏了要拖车）。你不会把出租车当便签纸随手"new 一辆"——本篇就是讲怎么把出租车正确地租出去、还回来。

### 0.6 bounded shutdown——关机不能拖死进程

关机这件事有个硬约束：**它必须在有限时间内完成**。

uvicorn（以及前面的容器编排，比如 K8s）给进程退出有个"优雅停机预算"（grace period）。收到 `SIGTERM` 后，进程有比如 30 秒优雅退出；超时还没退，编排层会发 `SIGKILL` 直接强杀——进程连"我正在 flush 数据库"都来不及说完就没了。

所以 lifespan 的每一个关机步骤，都**不能无限等**。如果某个关机钩子（比如"关 IM channel 连接"）卡住了，它会拖住整个进程退出，最终触发 `SIGTERM → SIGKILL` 的信号风暴——而 `SIGKILL` 之下，什么优雅清理都做不了，前面所有"关机步骤"的设计全白费。

DeerFlow 的对策：**每个关机钩子都套一层 `asyncio.wait_for(..., timeout)`，把它关进一个有界的时间盒子里**。最典型的就是 `stop_channel_service()` 外面那层 5 秒超时（`app.py:286-289`），以及 `_drain_inflight_runs` 内部 `RunManager.shutdown(timeout=5.0)`（`deps.py:46, 99`）。某个钩子超时了，记一条 warning、放它过去，继续走下一个关机步骤——宁可丢一个钩子的清理，也不能让整个 worker 拖死。

打个比方：打烊时，经理给每个收尾动作（关灯、关空调、锁门）都定了个最长时间，到点没做完就先放着、往下走——因为再不走，房东就要锁门把你关在里面了（SIGKILL）。

### 0.7 启动步骤的"致命 / 非致命"分级——哪些失败必须拒绝启动

启动时有几十个步骤，但它们对失败的容忍度不一样。DeerFlow 用一个简单的分级策略：

- **致命步骤（fatal）**：失败了直接 `raise`，让进程起不来。这是那些"没有它服务就根本没法跑"的东西——比如 config 加载失败（连日志级别都不知道）、数据库引擎初始化失败（checkpointer 没后端）。
- **非致命步骤（non-fatal）**：失败了 `try/except` 抓住、记一条日志、**继续启动**。这是那些"有了更好、没了也能凑合"的增强项——比如可观测性 tracing（tracing 挂了不影响业务）、tiktoken 预热（预热失败有字符计数兜底）、IM channel 服务（没配 channel 就不启）、孤儿 thread 迁移（迁不了最多留点孤儿数据）。

这个分级在 lifespan 里一眼就能看出来：**致命的步骤直接裸写、外面没 try/except（或 try/except 里 re-raise）；非致命的步骤外面一律裹着 `try: ... except Exception: logger.exception(...)`**。后面第 2 步讲每段代码时，会反复点出"这一段是致命还是非致命"。

为什么这么分？因为"启动失败"比"带病运行"好处理得多——启动失败，编排层（K8s/SYSTEMD）会看到进程退了、会重启或告警；而"带病运行"会让一个半残的服务继续接流量，问题被掩盖、排查更难。所以**核心组件失败宁可启动失败**；**边缘组件失败要让它不拖累核心**。这是 startup 容错的总策略。

---

好，前置概念讲完了。现在我们开始讲代码。

## 第 1 步：朴素写法长什么样

如果没有 lifespan，一个"启动 FastAPI 应用"的模块大概会写成这样（**这是反面教材，真实代码不是这样**）：

```python
# app.py 模块顶层 —— ★ 这些代码在 import 时就执行了（见 0.2）

config = get_app_config()                          # ① import 时读 config.yaml
configure_logging(config)

from some_db import PostgresSaver, create_engine
engine = create_engine(config.database.url)        # ② import 时开数据库连接池
checkpointer = PostgresSaver(engine)               # ③ import 时建 checkpointer
run_manager = RunManager(checkpointer=checkpointer)

from fastapi import FastAPI
app = FastAPI()                                    # ④ 没有 lifespan=
```

逐行解释，重点在"为什么这些写在 import-time 是灾难"：

- **第 ① 行** `config = get_app_config()`
  在**模块被 import 的那一瞬间**去读 `config.yaml`。但 import 可能发生在任何时候——测试时 `import app.gateway.app`、用户还没跑过 `make config`、CI 里……。这时 `config.yaml` 可能根本不存在（它是 gitignored 的运行时文件），`get_app_config()` 直接抛异常，**整个 `import` 崩掉**。后果：连 `/setup` 页面都进不去（因为 import app 就崩了，FastAPI 根本起不来）、测试也没法 import app。

- **第 ②③ 行** `engine = create_engine(...)` / `checkpointer = PostgresSaver(engine)`
  在 import 时就**打开了数据库连接池**、建了 checkpointer（0.5 讲的持资源对象）。问题一连串：
  - **测试地狱**：每个测试 `import app` 一次，就开一个连接池；跑 100 个测试就开 100 个连接池，数据库连接数爆炸、还泄漏（import 时开的连接没有任何关闭路径）。
  - **多 worker 灾难**：每个 worker 各 import 各开自己的连接池，连接数 = worker 数 × 单池连接数，很快把数据库连接打满。
  - **不可替换**：`checkpointer` 是模块级全局常量，测试想注入一个 `FakeCheckpointer` 根本无从下手——它已经在 import 时被钉死成 `PostgresSaver` 了。

- **第 ④ 行** `app = FastAPI()`（没有 `lifespan=`）
  没有关闭路径。连接池在 import 时开了，**永远没有地方关**——进程退出时操作系统回收文件描述符，但数据库那头看到的连接是"被对端硬断"（不是优雅 close），数据库要花更久才回收、还可能留下半截事务。更糟的是：进程退出时如果有 in-flight 的 run task 正在写 checkpoint，它写的那个 `aput` 会撞上"还没来得及正确关闭但已经不可用"的连接池，抛异常（这就是 issue #3373，麻烦 4 会详谈）。

**这几行代码的根本问题**：把"该 startup-time 做的事"写在了 import-time，又**完全跳过了 shutdown-time**。于是：

- 资源在错误的时刻（import 时）被建出来。
- 资源没有对称的关闭路径（没有 shutdown 段）。
- 资源被钉成全局常量，没法替换、没法测试。
- 进程退出时收尾顺序失控，撞各种"已关闭"异常。

下面 6 个麻烦，全是从"资源生命周期和模块/进程生命周期错位、且没有对称关停"这一个病根派生的。lifespan 就是来拔这个病根的。

> **一个对照，帮你看清本篇在系列里的位置。** RunManager 那篇讲的是："run 的**执行生命周期**" vs "**HTTP 请求的生命周期**"的解耦——run 要活过创建它的那次请求。**本篇讲的是**："持资源对象的**生命周期**" vs "**模块 import / 进程退出的生命周期**"的解耦——一个 checkpointer 不该在 import 时出生、不该在进程退出时被硬切。两个解耦，本质上是同一件事（让生命周期错位的东西各归各位），而**同一个总闸 `lifespan` 在做**。

---

## 第 2 步：6 个麻烦逐一展开

### 麻烦 1：config 必须在 startup 时读，且故意不缓存到 `app.state`

#### 朴素写法为什么做不到

回到第 1 步第 ① 行：`config = get_app_config()` 写在模块顶层，import 时就跑。`config.yaml` 是 gitignored 的运行时文件——用户首次拉代码、还没 `make config`、或 CI 跑测试时，这文件压根不在。import 直接崩。

#### lifespan 怎么解决的：把 config 读取放进 startup 段，并刻意不缓存

看 lifespan 的最开头（`app.py:174-191`）：

```python
    # Load config and check necessary environment variables at startup.
    # `startup_config` is a local snapshot used only for one-shot bootstrap
    # work (logging level, langgraph_runtime engines, channels). Request-time
    # config resolution always routes through `get_app_config()` in
    # `app/gateway/deps.py::get_config()` so `config.yaml` edits become
    # visible without a process restart. We deliberately do NOT cache this
    # snapshot on `app.state` to keep that contract enforceable.
    try:
        startup_config = get_app_config()
        configure_logging(startup_config)
        logger.info("Configuration loaded successfully")
        warn_if_auth_disabled_enabled()
    except Exception as e:
        error_msg = f"Failed to load configuration during gateway startup: {e}"
        logger.exception(error_msg)
        raise RuntimeError(error_msg) from e
    config = get_gateway_config()
    logger.info(f"Starting API Gateway on {config.host}:{config.port}")
```

逐行讲关键的：

- 这整段在 `yield` **之前**（开机段），也就是说它**只在 uvicorn 启动应用时跑一次**，不在 import 时跑。这立刻解决了朴素写法的第 ① 类崩点：用户既然要起服务，`config.yaml` 一定在；测试 import app 时也不会触发它。
- `startup_config = get_app_config()` 取一份 config 的**局部快照**，赋给一个**局部变量** `startup_config`（注意：不是 `app.state.config`）。这个名字本身就在说："我只用于一次性 bootstrap，用完就丢。" 后面麻烦 3 会看到，这份 `startup_config` 被传给 `langgraph_runtime(app, startup_config)`，用来一次性把引擎和 store 建出来。
- 注释里那句 **"We deliberately do NOT cache this snapshot on `app.state`"**（我们**刻意**不把这个快照缓存到 app.state）是本节最值得品的设计。为什么不缓存？因为 DeerFlow 有个 **hot-reload 契约**：用户编辑 `config.yaml` 后，**不用重启进程**，下一个请求就能读到新配置。这个契约靠的是"请求时永远通过 `get_config()`（即 `deps.py` 里的 `get_app_config()`）**重新读** config.yaml"。如果这里把快照挂到 `app.state`，路由代码就会图省事去读 `app.state.config`——那一读就是启动时的旧值，hot-reload 就废了。**所以这里宁可只给个局部变量，也要保住"请求时重读"的契约不被绕过。** 这是一个"用约束换正确性"的典型决策。
- `configure_logging(startup_config)` 用刚读到的 config 配日志级别。它必须在 startup 时跑，因为日志级别是 config 里的字段——得先读到 config 才知道配几级。这也呼应了 0.2：logging 这种副作用不能放 import-time（import 时 config 还没读）。
- `warn_if_auth_disabled_enabled()` 如果配了关认证，打个 warning。提醒运维"你把认证关了，生产别这么干"。
- **致命分级（0.7）**：注意这整段被 `try: ... except Exception as e: ... raise RuntimeError(...)` 包着，**except 里 re-raise 了**。也就是说，**config 加载是致命步骤**——读不到 config，进程直接起不来。这和后面那些 `try/except + log 但不 raise` 的非致命步骤形成鲜明对比。理由正是 0.7 说的：config 是核心组件，没它服务没法跑，宁可启动失败也别带病运行。

朴素写法把 config 读取放 import-time、还把它钉成全局常量；lifespan 把它放 startup-time、刻意只给局部变量。一字之差，既解决了 import 时崩、又保住了 hot-reload 契约。

---

### 麻烦 2：副作用必须在 startup 时发生（logging / Monocle / tiktoken 预热）

#### 场景

启动时要干一堆"有副作用"的事：配日志、装可观测性 tracer、预热 tokenizer 缓存、清理残留的上传文件。这些事有个共同点：**它们要么依赖 config（得先读到 config 才能做），要么会动进程级状态（装 tracer、清文件），不能在 import 时做**。

#### 朴素写法为什么做不到

朴素写法把这些写在 import-time 会怎样？以 Monocle tracing 为例——如果 `import app.gateway.app` 就装一个进程级的 tracer，那么**任何一次 `import`（测试、别的模块、lint 工具）都会把 tracer 装上**，即使你根本没打算起服务。进程级 tracer 装上就难卸，污染所有跑在同一个 Python 进程里的东西。这就是"副作用必须在 startup-time 而非 import-time"的直接体现。

#### lifespan 怎么解决的：副作用全放 startup 段，且按"非致命"分级

看 `app.py:193-237`（紧接麻烦 1 的 config 段）：

```python
    # Agent observability (Monocle). Off by default; enabled with
    # MONOCLE_TRACING. Initialized here at startup — not at import time — so a
    # plain `import deerflow.agents` never installs a process-global tracer.
    # Unlike LangSmith/Langfuse, whose validation failures abort the agent run,
    # a bad Monocle config only logs: the Gateway keeps serving without tracing.
    try:
        setup_monocle_tracing_if_enabled()
    except Exception:  # observability must never break startup
        logger.exception("Monocle tracing setup failed; continuing without it")
```

逐行：

- 注释 **"Initialized here at startup — not at import time — so a plain `import deerflow.agents` never installs a process-global tracer."** 这是 0.2（import-time vs startup-time）的**直接、字面例证**。作者明确写了"故意放在 startup 段而非 import 段，这样一个普通的 `import` 不会装上进程级 tracer"。这正是 lifespan 存在意义的一手案例。
- 注释还点出一个对比：LangSmith/Langfuse 的配置校验失败会**中断 agent run**，而 Monocle 的坏配置**只打日志**、网关继续服务。这决定了 Monocle 装失败必须是非致命的。
- `except Exception:  # observability must never break startup` 注释和代码一起表态：**可观测性是非致命步骤（0.7）**——tracer 装失败不影响业务，抓异常、记日志、继续启动。注意这里 `except` 里**没有 re-raise**，和麻烦 1 的 config 段（有 re-raise）形成对照。

再看 tiktoken 预热（`app.py:203-230`，精简后）：

```python
    # Pre-warm tiktoken encoding cache so the first memory-injection request
    # never blocks on the BPE data download (which hits an OpenAI/Azure URL
    # that may be unreachable in restricted networks — see issue #3402).
    try:
        from deerflow.agents.memory import get_memory_manager
        manager = get_memory_manager()
        warm = getattr(manager, "warm", None)
        if not callable(warm):
            logger.info("Memory backend %s has no warm-up hook; skipping ...", type(manager).__name__)
        else:
            warmed = await asyncio.wait_for(
                asyncio.to_thread(warm),
                timeout=5,
            )
            ...
    except TimeoutError:
        logger.warning("tiktoken encoding cache warm-up timed out; ...")
    except Exception:
        logger.warning("tiktoken warm-up skipped", exc_info=True)
```

这段信息量很大，逐点讲：

- **为什么预热**：tiktoken（OpenAI 的 BPE 分词器）第一次用时要下载编码数据，这个下载**会打 OpenAI/Azure 的 URL**，在受限网络里可能打不通（issue #3402）。如果等第一个用户请求来了才下载，那个请求就会卡在"下载分词数据"上。所以启动时提前下好（预热）。
- **为什么放 startup**：同样——这是个**网络副作用**（要联网下载），绝对不能放 import-time（import 时联网？测试怎么办？离线环境怎么办？）。
- `getattr(manager, "warm", None)` + `if not callable(warm)` 用 `getattr` **探测式**地问后端"你有没有 warm 方法"。为什么这么写？因为 memory 后端有多种（DeerMem / noop / 别的），不是每种都实现了 `warm`。用 `getattr` 探测而不是 `isinstance` 判断，是 Python 里的"鸭子类型"惯例——只关心"有没有这个方法"，不关心具体类型。没有 `warm` 的后端（比如 noop）就跳过预热。
- `await asyncio.wait_for(asyncio.to_thread(warm), timeout=5)` 这一行的三个细节都很重要：
  - `asyncio.to_thread(warm)`：`warm`（下载 tiktoken 数据）是个**同步阻塞**操作（它要等网络 IO），直接 `await` 它会卡住整个事件循环。`asyncio.to_thread` 把它扔到一个**工作线程**里跑，事件循环该干别的干别的——这是"把同步阻塞函数包成异步"的标准做法。
  - `asyncio.wait_for(..., timeout=5)`：呼应 0.6，**预热也得有界**。如果下载卡住（受限网络），最多等 5 秒，超时就放弃预热、继续启动。不能让一个预热拖死整个启动。
  - 连用起来：在一个工作线程里跑下载，事件循环最多等它 5 秒。
- **致命分级**：两个 `except`（`TimeoutError` 和 `Exception`）都是**只打 warning、不 re-raise**。预热失败有**字符计数兜底**（注释里说的 "character-based fallback"），所以它是**非致命**的。这又是一个 0.7 的范例：增强项失败，让它不拖累核心。

接着 `app.py:232-237` 是清理残留上传文件，结构一样：`asyncio.to_thread(...)` 扔线程里跑、`except Exception` 抓住只 warning。不再赘述。

朴素写法把这些副作用放 import-time，会污染所有 `import` 它的进程、还在联网/读 config 上随时崩。lifespan 把它们放 startup 段、按非致命分级、每个有副作用的东西都套上 try/except 和有界等待。

---

### 麻烦 3：嵌套 `langgraph_runtime` —— `yield` 在它内部，资源交给 `AsyncExitStack`

#### 场景

config 读好了、副作用做完了，接下来才是重头戏：**把 StreamBridge、checkpointer、store、RunManager 这些持资源对象（0.5）一个个建出来、塞进 `app.state` 工具箱**。这一步是 lifespan 的核心使命——没有它，三篇姊妹篇里的 `get_stream_bridge / get_run_manager / get_checkpointer` 全都 503（工具箱是空的）。

但 lifespan 没有自己干这件事，而是**交给了一个专门的 async context manager：`langgraph_runtime`**（`deps.py:222`）。lifespan 和它是什么关系？看 lifespan 里的这一行（`app.py:239-241`）：

```python
    # Initialize LangGraph runtime components (StreamBridge, RunManager, checkpointer, store)
    async with langgraph_runtime(app, startup_config):
        logger.info("LangGraph runtime initialised")
        ...
        yield                                  # ← lifespan 自己的 yield，在 langgraph_runtime 内部！
        ...                                    # 关机段
```

注意 `yield`（`app.py:275`）的**位置**：它在 `async with langgraph_runtime(...):` **内部**。这意味着 lifespan 把"正常服务期"整段托付给了 `langgraph_runtime`——应用在 `langgraph_runtime` 还活着的时候才活着。

#### 为什么要这么嵌套：对称的关停 + LIFO teardown

这是本篇最巧妙的一处设计。先看 `langgraph_runtime` 的骨架（`deps.py:222-258`）：

```python
@asynccontextmanager
async def langgraph_runtime(app: FastAPI, startup_config: AppConfig) -> AsyncGenerator[None, None]:
    """Bootstrap and tear down all LangGraph runtime singletons."""
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine_from_config
    from deerflow.runtime import make_store, make_stream_bridge
    from deerflow.runtime.checkpointer.async_provider import make_checkpointer
    from deerflow.runtime.events.store import make_run_event_store

    # Multi-worker safety gate: reject SQLite when GATEWAY_WORKERS > 1.
    _enforce_postgres_for_multi_worker(startup_config)

    async with AsyncExitStack() as stack:                  # ★ 一摞 async with
        config = startup_config

        app.state.stream_bridge = await stack.enter_async_context(make_stream_bridge(config))

        # Initialize persistence engine BEFORE checkpointer ...
        await init_engine_from_config(config.database)

        app.state.checkpointer = await stack.enter_async_context(make_checkpointer(config))
        app.state.store = await stack.enter_async_context(make_store(config))

        ...                                                # 还有一堆 app.state.xxx = ...
        app.state.run_manager = RunManager(store=app.state.run_store, run_ownership_config=...)
        ...

        try:
            yield                                          # ★ langgraph_runtime 自己的 yield
        finally:
            # drain + close_engine（麻烦 4 详讲）
            ...
```

逐行讲关键的：

- `_enforce_postgres_for_multi_worker(startup_config)` **多 worker 安全门**（`deps.py:49-85`）。开多 worker（`GATEWAY_WORKERS > 1`）必须数据库是 Postgres 且开心跳——否则 `raise SystemExit` 拒绝启动。这就是 run-manager 那篇 §0.8 讲的那道守卫，本篇不重复，只点一句：**它在所有资源建出来之前跑**，这样不满足条件时进程立刻退出、错误信息清晰，不会留下一堆半开的连接。这又是"启动失败优于带病运行"的体现。

- `async with AsyncExitStack() as stack:` **这是本麻烦的主角。** `AsyncExitStack` 是 0.4 讲的那个"一摞 async with"。把 `make_stream_bridge(config)`、`make_checkpointer(config)`、`make_store(config)` 这些**本身就是 async context manager 的工厂**，用 `stack.enter_async_context(...)` 一个个推进去。每推一个，就拿到它的"开机产物"（stream_bridge / checkpointer / store），同时 `AsyncExitStack` 记下"退出时要调它的关停逻辑"。

  为什么不手写一堆 `try/finally`？看如果手写会怎样：

  ```python
  # ★ 反面：手写 try/finally，关停顺序靠人肉保证
  stream_bridge = await make_stream_bridge(config).__aenter__()
  try:
      await init_engine_from_config(config.database)
      checkpointer = await make_checkpointer(config).__aenter__()
      try:
          store = await make_store(config).__aenter__()
          try:
              ... yield ...
          finally:
              await store.__aexit__(None, None, None)
      finally:
          await checkpointer.__aexit__(None, None, None)
  finally:
      await stream_bridge.__aexit__(None, None, None)
  ```

  这还只有三个资源。DeerFlow 实际要管的有五六个，而且其中任何一个**开机时抛异常**，都要正确关掉"已经开起来的那些"——手写 `try/finally` 嵌套会变成噩梦级的缩进金字塔，而且异常路径极易写错（比如 `__aenter__` 抛异常时，前面已开的资源要不要关、怎么关）。`AsyncExitStack` 把这些全自动化了：

  - **正常退出**：按 LIFO（后进先出）反序调每个的 `__aexit__`——后开的 store 先关、先开的 stream_bridge 最后关（0.4 讲的脱衣服顺序）。
  - **开机中途抛异常**：`AsyncExitStack` 也会把**已经成功 enter 的**那些自动关掉，再向上抛异常。这是手写 try/finally 最容易漏的边界。
  - **退出时某个 `__aexit__` 自己抛异常**：`AsyncExitStack` 会收集所有异常（通过 `__context__` 链），不让一个失败掩盖其他资源的关停。

  这就是"为什么用 `AsyncExitStack` 而不是手写"——**正确性、异常安全、可读性**，三全其美。

- `await init_engine_from_config(config.database)` **在 checkpointer 之前**初始化数据库引擎。注释（`deps.py:263-265`）说："Initialize persistence engine BEFORE checkpointer so that auto-create-database logic runs first (postgres backend)." 也就是说，先建引擎（这一步会顺带"如果数据库不存在就建"），再建 checkpointer——checkpointer 才能连上库。这是开机顺序的依赖（麻烦 5 会专门讲顺序依赖）。

- `app.state.stream_bridge = ...` / `app.state.checkpointer = ...` / `app.state.store = ...` 把这些持资源对象**塞进工具箱**（0.3）。从这一刻起，三篇姊妹篇里的 `get_stream_bridge / get_checkpointer / ...` 才有东西可取。注意它们都挂在 `app.state` 上，而 `app.state` 是进程级的——每个 worker 进程各跑一遍这段、各有自己的实例、各开自己的连接。

- `app.state.run_manager = RunManager(...)` 这个**不**是 `enter_async_context` 进去的，而是直接 new 的。为什么 stream_bridge/checkpointer/store 走 `AsyncExitStack`、而 run_manager 直接 new？因为前三者的工厂本身就是 async context manager（它们的 `__aenter__` 会开连接、`__aexit__` 会关连接），而 RunManager 是个普通对象——它自己**没有连接要开/关**（它持有的是内存表 + 对 store 的引用，store 的开/关由它引用的那个 store 管）。所以 RunManager 的"关停"不是靠 `__aexit__`，而是靠麻烦 4 会讲的、在 `finally` 里**显式调** `run_manager.shutdown()` 来 drain。这种"哪些走栈、哪些手动管"的区分，是对资源性质（0.5）的精细判断。

- `try: yield finally: ...` **这是 `langgraph_runtime` 自己的 yield**（`deps.py:332`）。它的位置决定了：**应用在 `AsyncExitStack` 活着的时候才活着**；应用退出（`yield` 之后）走 `finally`，`finally` 跑完才轮到 `AsyncExitStack` 退出（LIFO 关停那些资源）。

那么 lifespan 的 `yield` 和 `langgraph_runtime` 的 `yield` 是什么关系？两层 async context manager 嵌套，**两个 yield 叠在一起**：

```
lifespan:           开机段（麻烦1/2）
                      └─ async with langgraph_runtime(...):   ← 进入 langgraph_runtime
                            langgraph_runtime 开机段（建 StreamBridge/checkpointer/store/RunManager）
                            │
                            langgraph_runtime 的 yield ──┐
                            │                           │
                            lifespan 的 yield ──────────┤  ★ 应用在这两层 yield 叠起来的时候"活着"
                            │                           │
                            lifespan 关机段（麻烦6）     │
                          └─────────────────────────────┘
                            langgraph_runtime 的 finally（drain + close_engine，麻烦4）
                          ← 退出 async with，触发 AsyncExitStack LIFO 关停资源
    （回到 lifespan）     logger.info("Shutting down API Gateway")
```

这张图是本篇最重要的一张图。它说清楚了一件事：**应用的"正常服务期"被两层 context manager 同时托着**。关机时，**先走 lifespan 的关机段（停 channel/scheduler/flush memory），再走 `langgraph_runtime` 的 finally（drain run + close engine），最后 `AsyncExitStack` LIFO 关掉那些资源**。这个顺序正是为了避免 issue #3373（麻烦 4）和保证 memory flush 在连接池关闭前（麻烦 6）。

朴素写法（第 1 步第 ②③④ 行）在 import 时把 checkpointer 钉成全局常量、没有关停路径、没法测试替换。lifespan 通过"嵌套 `langgraph_runtime` + `AsyncExitStack`"，把开/关正确配对、LIFO 关停、可替换（都在 `app.state` 上，测试能覆盖）一次性解决了。

---

### 麻烦 4：issue #3373 —— `yield` 必须在 `langgraph_runtime` 内、drain 必须在 `close_engine` 之前

#### 场景与病根

这是整个 lifespan 设计里最"血泪"的一处——它修复的是一个真实的 GitHub issue（#3373）。先讲病根。

LangGraph 跑 agent 时，会持续写 checkpoint（三篇姊妹篇都讲过）。关键在于：**写 checkpoint 的 `aput` 不是在 agent run 的调用栈上同步执行的**，而是被 langgraph 内部的一个机制 `_checkpointer_put_after_previous` 扔到了**一个独立的后台 task** 里去调 `checkpointer.aput(...)`。

现在设想进程退出（`make stop` / 滚动发版 / SIGTERM）。如果**连接池先关了**（`close_engine()` 把 Postgres 连接池关掉），而那个后台 `aput` task 还在跑（它要写最后一个 checkpoint）——它就会撞上一个**已关闭的连接池**，抛 `psycopg_pool.PoolClosed`。更糟的是：**这个异常发生在 langgraph 内部的 task 里，agent run 的 try/except 根本抓不到它**，最终它冒泡成 `asyncio.run()` 关闭时的未处理异常，进程退出码不对、日志里一堆 traceback。

这就是 #3373：**关机时，连接池被先关、上层还在用它写 checkpoint，撞 PoolClosed。**

#### 朴素写法为什么做不到

朴素写法（第 1 步）连关停路径都没有——连接池在 import 时开了、进程退出时靠操作系统硬回收，那个后台 `aput` task 在进程退出时还在不在跑、会不会撞 PoolClosed，全凭运气。没有任何地方协调"先 drain run、再关连接池"。

#### lifespan 怎么解决的：`finally` 里先 drain、再 close_engine

看 `langgraph_runtime` 的 `finally`（`deps.py:332-343`）：

```python
        try:
            yield
        finally:
            # Drain in-flight run tasks BEFORE the AsyncExitStack tears down the
            # checkpointer (and its connection pool). A run still mid-graph would
            # otherwise leak into asyncio.run() shutdown, where langgraph's
            # _checkpointer_put_after_previous aput races the closed pool and
            # raises PoolClosed (issue #3373).
            run_manager = getattr(app.state, "run_manager", None)
            if run_manager is not None:
                await _drain_inflight_runs(run_manager)          # ★ 先 drain run
            await close_engine()                                  # ★ 再关连接池
```

逐行：

- 注释把病根说得很清楚：**"Drain in-flight run tasks BEFORE the AsyncExitStack tears down the checkpointer (and its connection pool)."** —— drain 必须在 checkpointer/连接池被关掉之前。因为 drain 的目的就是"让还在跑的 run 把最后一个 checkpoint **干净地 flush 出去**"，这需要连接池还活着；连接池关了再 drain，那个 `aput` 必撞 PoolClosed，#3373 复发。
- `await _drain_inflight_runs(run_manager)` **先** drain。`_drain_inflight_runs` 内部调的是 `RunManager.shutdown(timeout=5.0)`（run-manager 那篇麻烦 6 详讲过）：它给每个 in-flight run 的 task 发取消信号、有界地等它们干净收尾、把最终状态落库。**这一步所有 run task 都干干净净停了，不会再有任何后台 `aput` 在跑。**
- `await close_engine()` **再**关连接池。这时已经没有 run task 在写 checkpoint 了，关连接池就安全了——不会撞 PoolClosed。

**为什么这个顺序依赖"yield 在 langgraph_runtime 内部"？** 因为只有把 `yield` 放在 `langgraph_runtime` 里，`langgraph_runtime` 的 `finally` 才能在 `AsyncExitStack` 退出（= 关 checkpointer/连接池）**之前**插入 drain。如果 lifespan 不嵌套 `langgraph_runtime`、自己直接 `yield`，那 checkpointer 的关停就和 lifespan 的关机段平级，**没有地方插这个"先 drain 再关池"的顺序保证**。这是麻烦 3 那个嵌套设计的根本理由之一。

#### `_drain_inflight_runs` —— 用 `asyncio.shield` 保护 drain 不被信号风暴打断

再看 `_drain_inflight_runs` 本身（`deps.py:88-111`）：

```python
async def _drain_inflight_runs(run_manager: RunManager) -> None:
    """Drain in-flight runs before the checkpointer is torn down (issue #3373).
    ...
    """
    drain = asyncio.create_task(run_manager.shutdown(timeout=_RUN_DRAIN_TIMEOUT_SECONDS))
    try:
        await asyncio.shield(drain)                              # ★ shield：保护 drain 不被外部取消
    except asyncio.CancelledError:
        # Re-shield so this second wait does not abandon the in-flight drain;
        # it is bounded, so this cannot hang. Then re-raise to honour shutdown.
        try:
            await asyncio.shield(drain)                          # 被取消也要等 drain 跑完
        except Exception:
            logger.exception("In-flight run drain failed after shutdown cancellation")
        raise
    except Exception:
        logger.exception("Failed to drain in-flight runs during shutdown")
```

逐行讲关键的：

- `drain = asyncio.create_task(run_manager.shutdown(...))` 把 drain 做成一个独立的 task。`_RUN_DRAIN_TIMEOUT_SECONDS = 5.0`（`deps.py:46`）——`RunManager.shutdown` 自己内部是有 5 秒有界等待的（run-manager 麻烦 6 讲过），所以这个 drain task **最多跑 5 秒就结束**。
- `await asyncio.shield(drain)` **`asyncio.shield` 是这一段的核心。** 它的作用是：**保护 `drain` 这个 task 不被外部取消**。即使外面（lifespan 协程本身）被取消了，`drain` 这个 task **不会被取消**，会继续跑完。为什么非得保护它？因为关机时可能来**信号风暴**——用户连按两次 Ctrl-C、或 uvicorn 的 graceful shutdown 超时再发一次信号。第二次信号会把 lifespan 协程（包括正在 await drain 的它）取消掉。如果不 shield，drain 一被取消，就又回到了"checkpointer 还没关、run task 还在写"的 #3373 状态。所以宁可多等这几秒，也要让 drain 干净跑完。
- `except asyncio.CancelledError:` 如果真被取消了，**再 `shield` 一次**，把已经在跑的 drain 等完。注释说得很直白："Re-shield so this second wait does not abandon the in-flight drain; **it is bounded, so this cannot hang**." —— 敢再等一次，是因为 drain 是有界的（最多 5 秒），不会无限挂起。等完了再 `raise`（把取消向上传，尊重关机信号）。

呼应一下 run-manager 那篇：run-manager 麻烦 6 讲了 `RunManager.shutdown` 内部怎么有界地 drain；**本篇讲的是"谁来调这个 shutdown、在什么时刻调、为什么必须是 close_engine 之前、为什么必须 shield"**。两篇正好把 #3373 的修复从内（RunManager）到外（lifespan）讲完整了。

朴素写法既没有 drain、也没有 close 的顺序保证、更没有 shield——进程退出时 run task 的后台 `aput` 撞 PoolClosed 全凭运气。lifespan 用"yield 嵌套在 langgraph_runtime 内 + finally 先 drain 后 close_engine + shield 保护 drain"这三件套，把 #3373 彻底焊死了。

---

### 麻烦 5：启动步骤的顺序依赖（"谁必须排在谁之后"）

#### 场景

启动时那么多步骤，它们之间有**依赖**——B 要用 A 的产物，就必须排在 A 之后。朴素写法在 import-time 一把梭，根本不管顺序，于是要么 import 崩、要么用的时候才发现工具箱是空的。lifespan 把这些步骤排成了一个**有依赖图的开机序列**。

#### 顺序依赖长什么样

看 `app.py:243-273`（在 `async with langgraph_runtime(...)` **内部**，紧跟 `logger.info("LangGraph runtime initialised")`）：

```python
        # Check admin bootstrap state and migrate orphan threads after admin exists.
        # Must run AFTER langgraph_runtime so app.state.store is available for thread migration
        await _ensure_admin_user(app)

        # Start IM channel service if any channels are configured
        try:
            from app.channels.service import start_channel_service
            channel_service = await start_channel_service(startup_config)
            logger.info("Channel service started: %s", channel_service.get_status())
        except Exception:
            logger.exception("No IM channels configured or channel service failed to start")

        try:
            from app.gateway.services import launch_scheduled_thread_run
            from app.scheduler import ScheduledTaskService

            if getattr(app.state, "scheduled_task_repo", None) is not None and getattr(app.state, "scheduled_task_run_repo", None) is not None:
                scheduled_task_service = ScheduledTaskService(
                    task_repo=app.state.scheduled_task_repo,
                    task_run_repo=app.state.scheduled_task_run_repo,
                    launch_run=lambda **kwargs: launch_scheduled_thread_run(app=app, **kwargs),
                    ...
                )
                app.state.scheduled_task_service = scheduled_task_service
                if startup_config.scheduler.enabled:
                    await scheduled_task_service.start()
        except Exception:
            logger.exception("Failed to initialize scheduled task service")
```

逐段讲顺序依赖：

- **`await _ensure_admin_user(app)` 必须在 `langgraph_runtime` 之后。** 注释写死了："Must run AFTER langgraph_runtime so `app.state.store` is available for thread migration"。`_ensure_admin_user`（`app.py:61-130`）要干两件事：① 检查是不是首次启动（没有 admin 就提示用户去 `/setup`）；② 如果是从"无认证"升级到"有认证"，把那些没有 `user_id` 的孤儿 LangGraph thread 迁移给 admin。第②步要遍历 `app.state.store`（LangGraph store）——而 store 是 `langgraph_runtime` 才建出来塞进工具箱的。所以这一步**必须排在 `langgraph_runtime` 之后**，否则 `app.state.store` 是 None，迁移跑不了。这就是开机顺序依赖的典型一例。

- **IM channel service 也在 `langgraph_runtime` 之后。** channel service 要把外部 IM（飞书/Slack/Telegram…）的消息桥接进 agent，它要用 run_manager 来启动 run。所以也得排在 `langgraph_runtime` 之后（工具箱里有 run_manager 了才能起）。注意它**非致命**（`except Exception: logger.exception(...)`）——没配 channel 就不启，不影响核心服务。

- **scheduled task service 的"条件构造 + 条件启动"。** 这一段值得细看，因为它示范了一种常见的开机模式：

  ```python
  if getattr(app.state, "scheduled_task_repo", None) is not None and getattr(app.state, "scheduled_task_run_repo", None) is not None:
      scheduled_task_service = ScheduledTaskService(...)           # ① 条件构造
      app.state.scheduled_task_service = scheduled_task_service
      if startup_config.scheduler.enabled:
          await scheduled_task_service.start()                     # ② 条件启动
  ```

  两个 `if` 分了两层：

  - **① 条件构造**：只有当 `scheduled_task_repo` 和 `scheduled_task_run_repo` **都**在工具箱里（都不为 None）时，才构造 `ScheduledTaskService`。这两个 repo 是 `langgraph_runtime` 里**只在有数据库时**才建的（`deps.py:287-297`——`sf is not None` 才建，否则置 None）。也就是说，没配数据库（纯内存开发模式）时，根本没有 scheduler 的后端，那 scheduler 也就不构造。这是"有后端才有服务"的依赖守卫。
  - **② 条件启动**：即使构造了 `ScheduledTaskService`，也**只在 `startup_config.scheduler.enabled` 时才 `.start()`**。构造和启动是分开的两步。为什么要分开？因为**关机时要无条件 `.stop()`**（麻烦 6 会看到）——`stop()` 要能对"构造了但没 start"的服务安全调用（不能假设它一定 start 过）。把"构造"和"启动"分开，关机时的 `if ... is not None: ... .stop()` 才好写：只要构造了就 stop，不用再判断有没有 start 过。
  - **`lambda **kwargs: launch_scheduled_thread_run(app=app, **kwargs)`** 这个 lambda 是 scheduler 调用来"真正启动一个 run"的钩子。它把 `app` 闭包进去——scheduler 自己不知道 `app`，它只知道"我到点了，调这个函数去发 run"。这是一种依赖反转：scheduler 不直接依赖 app，而是接受一个 `launch_run` 回调。这也是为什么 `app` 必须先存在（又是顺序依赖）。

#### 为什么这些非致命步骤也裹着 try/except

注意 channel 和 scheduler 两段**都**裹着 `try: ... except Exception: logger.exception(...)`。理由和麻烦 2 一样（0.7）：它们都是**非致命的增强项**——IM 没接、定时任务没开，核心的聊天 agent 照样能跑。所以挂了只记日志、继续启动，不让一个边缘服务拖死整个网关。

朴素写法在 import-time 一把梭，根本保证不了"`_ensure_admin_user` 必须在 store 建好之后""channel 必须在 run_manager 之后"这些依赖——它连"这些对象什么时候被建出来"都控制不了（全在 import 那一瞬间乱序发生）。lifespan 用一个显式的、有顺序的开机序列，把依赖图走对了。

---

### 麻烦 6：关机顺序 + bounded —— 反向收尾，每步有界

#### 场景

`make stop`、滚动发版、SIGTERM——进程要退出了。前面开了那么多东西（config、tracing、StreamBridge、checkpointer、store、RunManager、channel、scheduler、memory 后台队列），关机时要**按开机顺序的反序**逐个收尾，且**每步都有界**（0.6），不能让一个卡住的钩子拖死整个 worker。

#### 关机段逐行

关机段从 lifespan 的 `yield`（`app.py:275`）**之后**开始（`app.py:277-341`）。先看前半段（停那些"应用级"服务）：

```python
        try:
            await auth.close_oidc_service()
        except Exception:
            logger.exception("Failed to close OIDC service")

        # Stop channel service on shutdown (bounded to prevent worker hang)
        try:
            from app.channels.service import stop_channel_service
            await asyncio.wait_for(
                stop_channel_service(),
                timeout=_SHUTDOWN_HOOK_TIMEOUT_SECONDS,          # ★ 有界：5 秒
            )
        except TimeoutError:
            logger.warning(
                "Channel service shutdown exceeded %.1fs; proceeding with worker exit.",
                _SHUTDOWN_HOOK_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.exception("Failed to stop channel service")

        if getattr(app.state, "scheduled_task_service", None) is not None:
            try:
                await app.state.scheduled_task_service.stop()
            except Exception:
                logger.exception("Failed to stop scheduled task service")
```

逐行讲关键的：

- **顺序**：先 `auth.close_oidc_service()`（关 OIDC 认证服务），再 `stop_channel_service()`（停 IM channel），再 `scheduled_task_service.stop()`（停定时任务）。注意这个顺序是**有意的**：先停那些**会产生新 run 的入口**（channel 和 scheduler 都会调 `launch_run` 发新 run），这样后面 drain memory 队列时（见下一段），就不会有新的更新源源不断地涌进来。注释（`app.py:305-310`）说得很清楚："IM channels and the scheduler are already stopped above, so no new IM/scheduler updates arrive during the drain." 这是关机顺序的依赖——和开机顺序一样讲究。

- **`stop_channel_service()` 外面套 `asyncio.wait_for(..., timeout=_SHUTDOWN_HOOK_TIMEOUT_SECONDS)`** 这就是 0.6 讲的 bounded shutdown。`_SHUTDOWN_HOOK_TIMEOUT_SECONDS = 5.0`（`app.py:58`）。channel 关停最多等 5 秒，超时就 `except TimeoutError: logger.warning(...)`——**放它过去，继续走下一个关机步骤**。注释（`app.py:55-57`）写明了为什么要套这层：*"Bounds worker exit time so uvicorn's reload supervisor does not keep firing signals into a worker that is stuck waiting for shutdown cleanup."* —— 不套这层，一个卡住的 channel 关停会让 worker 拖着不退，uvicorn 就一直发信号，演化成信号风暴。

- **`if getattr(app.state, "scheduled_task_service", None) is not None:`** 呼应麻烦 5：只要构造了 scheduler（不管有没有 start），这里就无条件 `.stop()`。这正是麻烦 5 把"构造"和"启动"分开的好处——关机时不用判断 start 没 start，构造了就 stop。

再看 memory flush 这一段（`app.py:323-339`），**它的位置非常关键**：

```python
        # Drain the memory backend's pending-update buffer before the worker
        # exits (best-effort, bounded). IM channels and the scheduler are
        # already stopped above, so no new IM/scheduler updates arrive during
        # the drain; ...
        try:
            app_cfg = get_app_config()
            if app_cfg.memory.enabled:
                from deerflow.agents.memory import get_memory_manager
                manager = get_memory_manager()
                flush_timeout = app_cfg.memory.shutdown_flush_timeout_seconds
                completed = await asyncio.to_thread(manager.shutdown_flush, flush_timeout)
                if completed:
                    logger.info("Memory queue flush completed within %.1fs", flush_timeout)
                else:
                    logger.warning(
                        "Memory queue flush did not finish within %.1fs; remaining updates may be lost",
                        flush_timeout,
                    )
        except Exception:
            logger.exception("Failed to flush memory queue on shutdown")
```

这一段的位置和实现都值得细看：

- **位置：它在 `async with langgraph_runtime(...)` 块内、在 channel/scheduler 已停之后、在 `langgraph_runtime` 的 finally（drain run + close engine）之前。** 这三层位置缺一不可：
  1. **在 channel/scheduler 已停之后**：如前所述，这样 drain memory 队列时不会有新的 IM/scheduler 更新涌入（注释明说）。
  2. **在 `langgraph_runtime` 块内**：这意味着 memory flush **在 `close_engine()` 之前**完成——memory 后台队列要往数据库写更新，得趁连接池还活着。如果 memory flush 排在 `langgraph_runtime` 退出之后（连接池已关），它写更新就会失败。
  3. **在 lifespan 的 yield 之后**：它属于关机段。

- **`await asyncio.to_thread(manager.shutdown_flush, flush_timeout)`** 又是 `asyncio.to_thread`（麻烦 2 讲过）——memory 的 flush 是同步阻塞操作（它要等队列里所有更新写完数据库），扔回工作线程跑，事件循环不被卡死。`flush_timeout` 来自 config（`memory.shutdown_flush_timeout_seconds`），由 `shutdown_flush` 内部 enforce 有界等待。

- **`completed = ...` 根据 `shutdown_flush` 的返回值区分**：返回 True = 在 timeout 内 flush 完了，记 info；返回 False = 没 flush 完，记 warning（"remaining updates may be lost"）。这是"best-effort"的明确表态：尽力 flush，flush 不完就丢——因为不能让 memory flush 拖死整个关机（0.6）。

- 注释里还有一句 K8s 提醒（`app.py:319-322`）：`shutdown_flush_timeout_seconds` 必须**小于** pod 的 `terminationGracePeriodSeconds`（channel stop + memory flush + buffer 三者之和），否则 K8s 会在 flush 半路 SIGKILL 掉 pod，这个 flush 修复的问题就白费了。这是个部署侧的约束，但 lifespan 代码里特意写注释提醒，说明作者很清楚"关机有界"是个系统工程——光代码有界不够，编排层的预算也得对齐。

最后，整个关机段结束时**退出 `async with langgraph_runtime(...)`**（`app.py:341` 之后），这才触发麻烦 4 讲的 `langgraph_runtime` 的 `finally`：先 `_drain_inflight_runs`（drain 所有 in-flight run）、再 `close_engine()`（关连接池）、然后 `AsyncExitStack` LIFO 关掉 stream_bridge/checkpointer/store。**关机的顺序从外到内、从应用到基础设施，一路收尾到底层连接池。** 然后 lifespan 才打印最后一句 `logger.info("Shutting down API Gateway")`（`app.py:341`）。

#### 关机是开机的镜像 + 有界化

把麻烦 5（开机）和麻烦 6（关机）并排看，你会发现一个漂亮的对称：

| 开机顺序（麻烦 5） | 关机顺序（麻烦 6） |
|---|---|
| 1. config / logging / tracing / 预热 | （镜像） |
| 2. `langgraph_runtime` 建 StreamBridge/checkpointer/store/RunManager | 5. 退出 `async with langgraph_runtime` → drain run + close engine |
| 3. `_ensure_admin_user` | （无对应关停，一次性迁移） |
| 4. start channel service | 2. stop channel service（有界 5s） |
| 5. 建并 start scheduled task service | 3. stop scheduled task service |
| | 4. flush memory 队列（在 langgraph_runtime 块内） |

关机**大体上是开机的反序**（停 channel → 停 scheduler → flush memory → drain run → close engine → LIFO 关资源），这正是 0.4 讲的 LIFO 思想在应用层的体现：**后起的先停，先起的最后停**。而每一步都套了 try/except（不致命、不让一个失败拖死其余）和/或 `asyncio.wait_for`（有界、不让一个卡住拖死整个 worker）。

朴素写法（第 1 步）完全没有关机段——连接池 import 时开、进程退出时硬回收，run task 的后台 `aput` 撞 PoolClosed、memory 队列的更新丢光、channel/scheduler 的后台连接乱断。lifespan 用一个"开机镜像 + 每步有界 + 非致命分级"的关机段，把这些全收拾干净。

---

## 第 3 步：把所有麻烦的本质串起来

朴素写法 `checkpointer = PostgresSaver(...)`（模块顶层，import 时执行）的根本问题是：

> **它把"持资源对象的生命周期"焊死在了"模块 import / 进程退出"的生命周期上——import 时被迫建、退出时被硬切，既没有对称的关停、也没有可替换性。**

而现实里，持资源对象的生命周期和模块/进程的生命周期是**完全不同步**的两件事：

| 维度 | 模块 import / 进程退出 | 持资源对象（checkpointer 等） |
|---|---|---|
| 该何时出生 | 模块被第一次 import 的那一瞬 | 用户明确要起服务、且 config/数据库都就绪时 |
| 该何时消亡 | 进程退出（可能很突然） | 显式、有序、有界地关停，且要 flush 完半截状态 |
| 失败容忍 | import 崩 = 一切都进不去 | 启动失败应可控（致命的拒绝启动，非致命的继续） |
| 可替换性 | 模块级全局常量，钉死 | 应能被测试注入 mock |
| 顺序 | import 顺序由 import 链决定，不可控 | 开/关顺序要按依赖图走（LIFO） |

把两者焊死，就会出现 6 个麻烦——因为现实里它们的生命周期**就是会错位**：

- ① config 必须在 startup 时读，且故意不缓存到 `app.state`（保 hot-reload 契约）
- ② 副作用（logging / tracing / 预热）必须在 startup 时发生，不能污染 import
- ③ 持资源对象必须由 `AsyncExitStack` 统一管，LIFO 关停（嵌套 `langgraph_runtime`）
- ④ `yield` 必须嵌在 `langgraph_runtime` 内、drain 必须在 `close_engine` 前（#3373）
- ⑤ 启动步骤必须按依赖图有序进行（谁要在谁之后）
- ⑥ 关机必须是开机的镜像 + 每步有界（防信号风暴）

`lifespan` + `langgraph_runtime` 就是**把持资源对象的生命周期和模块/进程的生命周期解耦**的那两层总闸。它具体做的事：

- **import-time vs startup-time 的分离**：所有有副作用、依赖 config、占资源的初始化，都从 import-time 挪到 startup-time（麻烦 1/2）。
- **`AsyncExitStack` 统管资源开/关**：持资源对象的 `__aenter__`/`__aexit__` 自动配对、LIFO 关停、异常安全（麻烦 3）。
- **`yield` 的精确位置**：把应用服务期嵌在 `langgraph_runtime` 内，给 #3373 的"先 drain 后关池"留出插入点（麻烦 4）。
- **有依赖图的开机序列**：每一步排在它依赖的东西之后（麻烦 5）。
- **开机镜像 + 有界化的关机序列**：反序收尾、每步 `try/except` + `asyncio.wait_for`，不让一个卡死拖垮全局（麻烦 6）。
- **致命/非致命分级**：核心组件失败拒绝启动，边缘组件失败继续服务（贯穿麻烦 1/2/5/6）。

代码上，这一切都从这两行开始：

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
```

它把"开机初始化 / 正常服务 / 关机清理"三件事，钉死成正确的**顺序**与有界的**预算**。它和三篇姊妹篇是同一类东西的不同层：

- **`StreamBridge` 解耦"事件流"的生命周期**——agent 产出的事件要被任意数量、任意时刻、跨进程的 HTTP 订阅者消费。
- **`RunManager` 解耦"执行 + 状态"的生命周期**——agent 的 run 要活过创建请求、跨重启可查、跨 worker 可协调、能被干净取消和收尸。
- **`get_checkpointer` 解耦"存档"的生命周期**——agent 的 checkpoint 要存在正确的后端、由正确的连接写、跨重启不丢。
- **`lifespan` 解耦"持资源对象"的生命周期**——所有持资源对象要在 startup 时建、shutdown 时有序有界地关，不被 import 和进程退出焊死。

四者合起来，从"一次请求"到"一个 run"再到"一份存档"最后到"整个进程的资源"，把 DeerFlow 里所有生命周期错位的东西，各归各位。这就是 `@asynccontextmanager async def lifespan(app)` 存在的全部理由。
