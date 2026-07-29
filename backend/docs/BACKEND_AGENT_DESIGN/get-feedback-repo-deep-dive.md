# get_feedback_repo 深度讲解：为什么不能在路由里 `FeedbackRepository(...)` 就完事

本文是 `stream-bridge-deep-dive.md` / `run-manager-deep-dive.md` / `get-checkpointer-deep-dive.md` 的姊妹篇。前三篇讲的是 `bridge = get_stream_bridge(request)`、`run_mgr = get_run_manager(request)`、`checkpointer = get_checkpointer(request)` 为什么存在；这篇讲的是紧挨着它们的另一行——`feedback_repo = get_feedback_repo(request)` 为什么存在。

具体到源头，本文讲的就是 `backend/app/gateway/deps.py:368` 这一行：

```python
get_feedback_repo: Callable[[Request], FeedbackRepository] = _require("feedback_repo", "Feedback")
```

它和 `get_checkpointer`（`deps.py:366`）只隔一行——四个兄弟声明连写在 `deps.py:364-369`。前三个兄弟回答的是同一个大问题的三个面：**一次 HTTP 请求里启动的 agent run，凭什么能活得比这次请求久？** StreamBridge 负责"事件流"，RunManager 负责"执行 + 状态"，checkpointer 负责"存档（checkpoint）"。

本篇讲的却是**另一个方向**。前三篇的主角都是"必须活久、必须正确关停"的重型进程级资源——它们各自拖着一个数据库连接池、一整套开关机顺序、一堆多 worker 协调机制。而 `get_feedback_repo` 拿回来的 `FeedbackRepository` **根本没有这些负担**：

- 它**不持有长寿命连接池**，每个方法自己开一个短命 session、用完即还；
- 它**不进 `AsyncExitStack`**，没有"关机要先 drain 再关池"的顺序问题；
- 它**不参与 run 的生命周期**，agent 跑得怎样、进程怎么死，跟它不相干。

那它为什么还要走 `app.state` 单例 + `_require` 这套和 checkpointer 一模一样的机制？因为它的麻烦落在**另一个维度**——前三篇解决"进程级资源该不该活久"，本篇解决的是**反向的、却同样要解耦的问题**：

> **有些功能本身就是可选的——没配数据库时，它诚实地"不可用"，而不是假装可用却在背后丢数据；配了数据库时，它又得保证多用户隔离、写入幂等、读侧不拖垮列表查询。**

预先点破这个差别，因为它是贯穿全文的基调：Feedback 的"轻"不是因为它不重要，而是因为它把"重"的部分（连接池生命周期、关机顺序）**故意外包给了底层的 `async_sessionmaker`**，自己只管"业务逻辑正确"。本文第 2 步的 6 个麻烦，没有一个和开关机有关——全是**可选性 / 隔离 / 幂等 / 读侧聚合**。

本文面向第一次接触这套架构的同学。先把所有需要的前置概念一个一个铺出来，然后再讲 6 个麻烦，每段代码逐行解释。已经读过前三篇姊妹篇的同学可以快速跳过带 ※ 标记的小节。

---

## 第 0 步：先把要用的术语讲清楚

在讲任何代码之前，先把后面会反复出现的概念讲明白。带 ※ 标记的小节姊妹篇讲过，本篇只复用结论；不带 ※ 的是本篇才需要的新概念。

### 0.1 ※ `app.state`——进程级"工具箱"

※ 概念基础和 `get-checkpointer-deep-dive.md` §0.3 完全重叠，本篇复用结论。

FastAPI 应用对象 `app` 上挂着一个属性叫 `app.state`，是 Starlette 的 `State` 类型，就是一个**进程级的"工具箱"**：启动时往里塞东西，每个请求伸手进去取。`app.state.feedback_repo` 就是这么一个槽位。多 worker 下每个进程有自己独立的工具箱，但只要它们连同一个外部数据库，看到的反馈数据就是一致的。

### 0.2 ※ `_require` 工厂——取不到就 503

※ 概念基础和 `get-checkpointer-deep-dive.md` §麻烦 6 完全重叠，本篇复用结论，并在麻烦 1 里再贴一次完整代码细讲。

`_require(attr, label)`（`deps.py:351-361`）是个**高阶函数**：吃两个字符串（属性名、给人看的标签），产出一个闭包 `dep(request)`。这个闭包从 `request.app.state.<attr>` 取值，取到 `None` 就立刻抛 `HTTPException(503, f"{label} not available")`，否则返回取到的值。本文主角那一行就是它的产出。

### 0.3 ※ lifespan——开机/关机总闸

※ 概念基础和姊妹篇重叠，这里只复用结论。

FastAPI 的 lifespan 在"应用启动好、开始接请求之前"跑一段初始化代码（往 `app.state` 塞单例），在"应用关闭时"跑一段清理代码。`app.state.feedback_repo` 就是在 lifespan 里的 `langgraph_runtime`（`deps.py:222`）这一段塞进工具箱的。

### 0.4 FeedbackRepository——"按用户分柜台的留言簿"

`FeedbackRepository`（`backend/packages/harness/deerflow/persistence/feedback/sql.py:19`）是一个**仓库（repository）**对象。仓库是干嘛的？——把"对某个数据表的所有读写操作"收拢到一个对象上。对反馈来说，这个表是 `feedback`，仓库就是这个表的"前台"。

打个比方：`FeedbackRepository` 就像餐厅里那本**按用户分柜台的留言簿**——每个客人（用户）吃完一道菜（一个 run）可以撕一张纸条写评价（打分 +1/-1、可选留言），交给前台（仓库）；前台帮你登记、查找、统计、销毁。前台只认"这是几号桌（thread_id）的哪道菜（run_id）、谁写的（user_id）"这三样，不关心菜是怎么做出来的。

它的构造函数（`sql.py:20-21`）长这样：

```python
class FeedbackRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory
```

逐行：

- `def __init__(self, session_factory: ...) -> None:` 构造函数只吃一样东西——一个 **session 工厂**（§0.5 讲）。注意：它**不吃数据库连接、不吃连接池、不吃任何"重资源"**。它只保存一个"能借出 session 的工厂"的引用。
- `self._sf = session_factory` 把这个工厂存起来。之后仓库的每个方法要用数据库时，都通过 `self._sf()` 现借一个 session。

这就是 Feedback"轻"的源头：**仓库对象本身几乎不持有状态，只持有"一个工厂的引用"。** 和 checkpointer（自己攥着一整个连接池）比，它轻得像一张名片。这个差别决定了它根本不需要进 `AsyncExitStack`（麻烦 3 会展开）。

### 0.5 `async_sessionmaker`——"图书馆借阅证"

`async_sessionmaker`（来自 SQLAlchemy）是一个**异步会话工厂**。会话（session）是"一次数据库对话"的抽象——你拿一个会话，在里面查、改、提交（commit）、然后归还。工厂就是"能反复生产会话"的对象。

打个比方：`async_sessionmaker` 就像图书馆的**借阅证**——你（仓库的某个方法）要做数据库操作，出示借阅证（`self._sf()`）借一本书（一个 session），看完还回去（`async with` 退出时自动还）；下次再来，再借一本。借阅证本身是长期持有的（进程级单例），但每本书（每个 session）是短命的（用完即还）。

这和 checkpointer 的连接池（`AsyncConnectionPool`，姊妹篇 §0.5）有什么不同？——**粒度不同、寿命不同**：

| | checkpointer 的连接池 | feedback 仓库的 session_factory |
|---|---|---|
| 工厂本身寿命 | 进程级长寿命，启停要管 | 进程级长寿命，启停几乎不用管 |
| 每次借的东西 | 一条到数据库的 TCP 连接 | 一个 session（包着一条连接） |
| 归还方式 | checkpointer 自己攥着池子，借还自己调度 | `async with self._sf() as session:` 自动借还 |
| 谁负责关池 | `AsyncExitStack` 在关机时关 | **底层 session_factory 的引擎**——但那不是仓库的事 |

关键点：仓库每次 `async with self._sf()` 借出来的 session，**用完（出 `async with`）会自动归还连接**。所以仓库自己**不持有任何"需要主动关掉"的资源**——它借的都及时还了。这就是为什么 `app.state.feedback_repo` 这个单例**不需要进 `AsyncExitStack`**（§0.6 下面讲）。

> 一个微妙但重要的细节：`feedback_repo` 和 `run_store` 共用的是**同一个** `async_sessionmaker`（`deps.py:271` 的注释 "one get_session_factory() call for all"）。这个工厂背后真正的数据库引擎（engine）是在更早的 `await init_engine_from_config(...)`（`deps.py:265`）建好的。所以"关连接"的责任在引擎层，仓库只是引擎的一个消费者。麻烦 3 会再讲这层分工。

### 0.6 ※ `AsyncExitStack`——一摞"用完要还"的资源

※ 概念基础和姊妹篇重叠，这里聚焦在"feedback_repo 为什么**不**进这摞盘子"。

`AsyncExitStack` 是 Python 标准库的异步资源回收器：你把一堆"用完要还"的东西依次 `enter` 进去，它保证退出时按相反顺序逐个 `close`。checkpointer 的连接池就是这么进去的（`deps.py:267` 的 `await stack.enter_async_context(make_checkpointer(config))`）。

本篇要强调的反而是：**`feedback_repo` 没有进这摞盘子**。看装配代码（`deps.py:271-282`），它是**直接赋值**：

```python
app.state.feedback_repo = FeedbackRepository(sf)      # 直接赋值，没有 enter_async_context
```

为什么它有资格"不进"？因为它（§0.4/§0.5）自己**不持有需要主动关掉的资源**——它只是攥着一个 `session_factory` 的引用，每次借的 session 都当场还了。真正需要关的是底层那个 engine（`close_engine()`，`deps.py:343`），而 engine 的回收不是仓库的职责。

这个"缺席"是**有意为之**：把"资源生命周期"外包给底层，仓库自己保持轻薄。这也是本篇和前三篇最大的气质差别——前三篇的主角全都**自己攥着重资源、必须进 `AsyncExitStack` 排好关停顺序**；本篇的主角**不攥资源、可以不进**。

### 0.7 `resolve_user_id` / `AUTO`——"当前调用者是谁"

这是本篇**最关键**的新概念，麻烦 4 的地基。

DeerFlow 是多租户的：不同用户登录后，看到的是各自的数据。反馈也不例外——A 给某条 run 点的赞，B 不该看见、更不该删掉。要做到这点，仓库的每个方法都得知道"**当前这次调用的调用者是谁**"。

但"当前调用者"这个东西，不是仓库自己能算出来的——它在 HTTP 请求的认证中间件里。仓库方法有两种拿到它的方式：

1. 调用方显式传进来（比如路由从认证里取到 `user_id` 再传给仓库）；
2. 仓库自己从一个进程级的"上下文变量"里读。

DeerFlow 用一个叫 `resolve_user_id` 的函数（`backend/packages/harness/deerflow/runtime/user_context.py:166-195`）统一这两条路。它吃一个参数 `user_id`，这个参数有**三种可能的取值**，对应三种行为：

```python
AUTO: Final[_AutoSentinel] = _AutoSentinel()          # 一个单例哨兵，是仓库方法的默认值

def resolve_user_id(value, *, method_name="..."):
    if isinstance(value, _AutoSentinel):              # ① AUTO（默认）：从 contextvar 读
        user = _current_user.get()
        if user is None:
            raise RuntimeError("... no user context is set ...")
        return str(user.id)
    return value                                       # ② 显式 str：原样用；③ 显式 None：原样返回 None
```

三种语义（docstring 在 `user_context.py:171-183`）：

- **`AUTO`（默认）**：从进程级的 contextvar `_current_user` 里读当前登录用户。读不到就抛 `RuntimeError`——意思是"你这个仓库方法在请求上下文里被调用，却没人设置当前用户，这是调用方的 bug"。这是请求路径里的常态。
- **显式 `str`**：调用方明确指定一个 user_id，覆盖 contextvar。用于测试、admin 覆盖流程。
- **显式 `None`**：**故意不过滤**——仓库方法会跳过 `WHERE user_id = ...` 这一句。这是留给 migration 脚本和 CLI 工具的"管理通道"，让它们能跨用户操作。

打个比方：`AUTO` 就像你进餐厅**刷脸**（系统自动认出你是谁），显式 `str` 就像你**报身份证号**（你主动告诉系统你是谁），显式 `None` 就像你是**卫生检查员**——出示特殊证件，可以翻所有客人的留言，不受"只看自己"的限制。

`AUTO` 这个值本身是个**哨兵单例**（`_AutoSentinel`，`user_context.py:149-163`），不是 `None`、不是空字符串——它是个"占位符"，表示"我没指定，请帮我从上下文里推断"。用哨兵而不是用 `None` 当默认值，是为了**把"我没指定（AUTO）"和"我指定了不要过滤（None）"这两种截然不同的语义区分开**（§0.5 的表格里 None 是显式管理通道）。这是个很经典的设计技巧：用专属类型当默认值，避免和真实取值撞车。

### 0.8 `UniqueConstraint`——"一人一票"

数据库表可以加**唯一约束（unique constraint）**：声明"某几列的组合在这张表里不能重复"。`feedback` 表的模型（`backend/packages/harness/deerflow/persistence/feedback/model.py:16`）就有这么一条：

```python
__table_args__ = (UniqueConstraint("thread_id", "run_id", "user_id", name="uq_feedback_thread_run_user"),)
```

意思是：**同一个 `(thread_id, run_id, user_id)` 组合，在 `feedback` 表里最多只能有一行**。

打个比方：就像节目投票，**一个观众（user_id）对一个节目（run_id，归属某场演出 thread_id）只能投一票**——你再点，只是把原来的票**改掉**，不会变成两票。这正是"一人一票"的数据库表达。

这条约束是麻烦 5（幂等）的最后一道防线——就算应用层的 upsert 逻辑出了 bug，数据库也会用 `IntegrityError` 拒绝第二条重复记录。但它**只**约束 `(thread_id, run_id, user_id)` 三列的组合，所以同一个 run 可以被很多不同用户各打一次分，同一个用户也可以在不同的 run 上各打一次分。

### 0.9 HTTP 503 / 400 / 404——本篇会出现的几种状态码

本篇的反馈端点（`backend/app/gateway/routers/feedback.py`）会用到几个 HTTP 状态码，先把它们的语义讲清楚，免得后面混淆：

- **503 Service Unavailable**——"这个功能（在这个进程里）**暂时不可用**"。本篇里特指"没配数据库，`feedback_repo` 是 `None`"。客户端看到 503 应该想"过会儿再试或换个环境"，而不是"我哪儿做错了"。这是 §0.2 的 `_require` 抛的。
- **400 Bad Request**——"你发来的请求**本身有问题**"。本篇里特指 `rating` 不是 +1 或 -1（`feedback.py:70-71`、`121-122`）。客户端改了重发就行。
- **404 Not Found**——"你要操作的东西**不存在**"。本篇里特指 run 不存在（`feedback.py:77-80`、`129-132`）、或要删的反馈不存在（`feedback.py:181-184`）。
- **409 Conflict**——注意，**反馈场景里 409 几乎不出现**。409 通常表示"和现有状态冲突"（比如 run-manager 里"同一个 thread 已经有 run 在跑"）。反馈用 **upsert（幂等的 PUT）** 而不是"冲突就报错"，所以连点两次不会 409，而是静默改票（麻烦 5）。这是个值得留意的对比——run 那边用 409 表达"冲突"，反馈这边用 upsert 把"冲突"消解成"覆盖"。

好，前置概念讲完了。现在我们开始讲代码。

---

## 第 1 步：朴素写法长什么样

如果没有 `get_feedback_repo`，一个"给某 run 打分"的路由大概会写成这样（**这是反面教材，真实代码不是这样**）：

```python
from deerflow.persistence.feedback import FeedbackRepository

@router.put("/{thread_id}/runs/{run_id}/feedback")
async def upsert_feedback(thread_id: str, run_id: str, body: FeedbackUpsertRequest):
    repo = FeedbackRepository(make_some_session_factory())   # ← 每个请求自己造工厂、自己造仓库
    await repo.upsert(run_id=run_id, thread_id=thread_id,    # ← 不分用户
                      rating=body.rating, user_id=None)       # ← user_id=None：不过滤任何人
    return {"success": True}
```

逐行解释：

- **第 1 行** `from deerflow.persistence.feedback import FeedbackRepository` 路由文件直接 import 仓库类。
- **第 4 行** `repo = FeedbackRepository(make_some_session_factory())` **这是核心反面**——**每个请求都 new 一个全新的仓库，而且每次还得先造一个 session 工厂**。造工厂这件事（背后建数据库 engine、开连接）是有代价的，每请求都干一遍，要么慢、要么把连接打爆。
- **第 5 行** `await repo.upsert(...)` 调仓库写打分。
- **第 6 行** `user_id=None` —— 按 §0.7，**显式 `None` 是"不过滤用户"的管理通道**！这里把它当默认值用，意味着写进去的反馈**不分用户**，所有人共享一摊——A 和 B 的打分会串台。

**这几行代码的根本问题**：把仓库当成了"一个随手 new 的临时对象"，丢掉了三样东西：

1. **配置驱动的可用性边界**——根本没考虑"没配数据库时这个功能该不该能用"。要么假定 DB 一定在（一访问就崩成 500），要么硬塞个内存版（假装可用、数据重启即丢、误导用户）。
2. **用户隔离**——`user_id=None` 把多租户隔离彻底关掉了。
3. **幂等**——这里虽然调了 upsert，但朴素写法往往图省事直接 INSERT，连点两次就出两条记录。

下面 6 个麻烦，全是从这三个病根派生的。`get_feedback_repo(request)` + `_require` 工厂 + lifespan 绑定 + 仓库内部的 `resolve_user_id` / upsert / 唯一约束，就是来拔这些病根的。

---

## 第 2 步：6 个麻烦逐一展开

### 麻烦 1：没配数据库时，反馈功能该不该可用

#### 场景

DeerFlow 要同时应付好几种部署：

- **本地开发 / 单元测试**：可能压根没配数据库（`database.backend=memory`），跑个前端看看 UI 就行。
- **单机小部署**：用 SQLite 文件。
- **生产 / 多机**：用 Postgres。

反馈这个功能**强依赖数据库**——它要把打分写进 `feedback` 表、要能查、要能统计。如果没有数据库，反馈往哪儿写？

这里有个微妙的产品决策：**反馈是终端用户能直接看见的功能**（聊天界面上的👍👎按钮）。如果没配数据库，你让这个按钮怎么表现？

- **选项 A：假装可用，背后用内存版**。用户点了👍，前端收到 200，以为打分成功了。实际上数据存在本进程内存里，进程一重启就没了——用户被骗了，他还以为自己的反馈被记下了。
- **选项 B：诚实地报"不可用"**。用户点👍，前端收到 503，按钮灰掉或弹"反馈功能未启用"。用户**知道**这事没成，不会被误导。

#### 朴素写法为什么做不到

朴素写法（第 1 步那段）**根本没考虑这个分叉**——它默认数据库一定在，`make_some_session_factory()` 一定成功。一旦在没配 DB 的环境里跑，要么 import 时就崩、要么运行时抛个深层的连接错误，最终变成 500。客户端只看到 "Internal Server Error"，运维只看到一长串 traceback，根本不知道是"反馈功能没启用"还是"代码有 bug"。

或者走另一个极端——朴素写法为了"不崩"，可能会塞个内存版的 `FeedbackRepository`。这就掉进了选项 A 的坑：**假装可用、误导用户**。

#### DeerFlow 怎么解决的：if/else 显式置 None + `_require` 就地 503

DeerFlow 的选择很明确——**选选项 B：诚实地报不可用**。实现分两半。

**第一半：启动时按配置决定 feedback_repo 是真仓库还是 None。** 看 lifespan 里的装配（`deps.py:271-282`）：

```python
# Initialize repositories — one get_session_factory() call for all.
sf = get_session_factory()
if sf is not None:                                          # ① 有数据库
    from deerflow.persistence.feedback import FeedbackRepository
    from deerflow.persistence.run import RunRepository

    app.state.run_store = RunRepository(sf)
    app.state.feedback_repo = FeedbackRepository(sf)        # ← 给真仓库
else:                                                       # ② 没数据库
    from deerflow.runtime.runs.store.memory import MemoryRunStore

    app.state.run_store = MemoryRunStore()
    app.state.feedback_repo = None                          # ← ★ 显式置 None，不假装
```

逐行：

- `sf = get_session_factory()` 取数据库的 session 工厂。配了数据库（Postgres/SQLite）就有值，没配（`backend=memory`）就是 `None`。
- `if sf is not None:` ① 有数据库——构造一个真 `FeedbackRepository(sf)` 挂到 `app.state.feedback_repo`。这时反馈功能是真的能用的。
- `else:` ② 没数据库——**注意这里没有给 feedback 造一个内存版**（对比一下：同在一个 else 块里的 `run_store` 就用了 `MemoryRunStore()`）。`app.state.feedback_repo` 被**显式置成 `None`**。

为什么 run_store 给了内存版、feedback_repo 偏偏给 `None`？这是个有意的产品取舍：**run 是核心功能**（没 run 整个应用就跑不起来），所以哪怕没 DB 也要用内存版让它能跑（代价是重启即丢，开发时能接受）；**反馈是辅助功能**（没它应用照样用），所以没 DB 时宁可不启用，也不假装。`run_store` 选 A、`feedback_repo` 选 B——同样的 if/else，两条不同的策略，反映的是两个功能在产品上的**重要性差异**。

**第二半：取依赖时，None 就立刻抛 503。** 这就是本文主角那一行的源头——`_require` 工厂（`deps.py:351-361`）：

```python
def _require(attr: str, label: str) -> Callable[[Request], T]:
    """Create a FastAPI dependency that returns ``app.state.<attr>`` or 503."""

    def dep(request: Request) -> T:
        val = getattr(request.app.state, attr, None)        # 从工具箱取
        if val is None:
            raise HTTPException(status_code=503, detail=f"{label} not available")  # ★ 取到 None 就 503
        return cast(T, val)

    dep.__name__ = dep.__qualname__ = f"get_{attr}"
    return dep
```

逐行：

- `def _require(attr, label):` 工厂函数，吃属性名和标签。
- `def dep(request: Request) -> T:` 真正在每个请求里跑的闭包。
- `val = getattr(request.app.state, attr, None)` 从 `app.state` 取 `attr`（这里是 `"feedback_repo"`）。取不到默认 `None`，不抛错。
- `if val is None: raise HTTPException(503, f"{label} not available")` ★ **灵魂所在**：取到 `None` 就立刻、就地抛 503。对 feedback，`label="Feedback"`，所以错误信息是 **`"Feedback not available"`**。
- `return cast(T, val)` 取到了就返回（`cast` 是 typing 的假转，运行时 no-op）。

于是本文主角那一行（`deps.py:368`）就好懂了：

```python
get_feedback_repo: Callable[[Request], FeedbackRepository] = _require("feedback_repo", "Feedback")
```

- 左边类型注解 `Callable[[Request], FeedbackRepository]`——"吃 Request、吐 FeedbackRepository 的函数"。
- 右边 `_require("feedback_repo", "Feedback")` 调工厂，产出那个闭包，赋值给 `get_feedback_repo`。

效果：每次路由调 `feedback_repo = get_feedback_repo(request)`，要么拿到真仓库，要么**立刻**抛 503 `"Feedback not available"`——绝不会把 `None` 往下传、拖到 `repo.upsert(...)` 那一步才在 `NoneType` 上崩成 500。

#### 为什么 503 比 NoneType 深层崩友好

设想反馈功能没启用（`app.state.feedback_repo = None`），用户点了👍。两种实现：

- **朴素写法**（直接 `getattr` 不检查，把 `None` 往下传）：路由调 `None.upsert(...)` → `AttributeError: 'NoneType' object has no attribute 'upsert'` → 栈底崩 → 500 "Internal Server Error"。运维看到一长串 traceback，客户端只看到 "Internal Server Error"，**谁都不知道是"反馈没启用"**。
- **DeerFlow**（`_require` 就地 503）：路由调 `get_feedback_repo(request)` → 当场抛 503 `"Feedback not available"`。运维一看日志就知道**这个进程没配数据库**，要么重启配 DB、要么换进程；客户端看到 503 也知道"这功能现在不可用"。

503 的核心价值是**信号又早又准**：早——抛在"取依赖"这一步，不拖到"用它"；准——detail 写明是哪个功能不可用。这正是 HTTP 503 的官方语义（§0.9）："服务暂时不可用，不是你的错，过会儿再来。"

朴素写法**要么没数据库就崩成 500、要么假装可用却丢数据**。DeerFlow 用"显式置 None + `_require` 就地 503"两个动作配合，把"反馈功能可不可用"变成了一个**清晰、诚实、对用户友好的信号**。

---

### 麻烦 2：进程级单例 + 共享 session_factory，不能每次 new

#### 场景

生产环境，成百上千的用户在打分。每个打分请求都要拿到一个能干活的 `FeedbackRepository`。如果每个请求都自己造一个仓库（像第 1 步那样），会发生什么？

#### 朴素写法为什么做不到

朴素写法每个请求都 `repo = FeedbackRepository(make_some_session_factory())`。问题不在"造仓库"本身（仓库对象很轻，§0.4），问题在**造 session_factory**——工厂背后是数据库 engine，engine 背后是连接资源。每请求都造一个新工厂：

- 重复建 engine / 连接池，慢；
- 多个工厂各开各的连接，把数据库连接名额打爆；
- 各自的连接互不复用，抹掉了连接池的全部价值。

#### DeerFlow 怎么解决的：启动时建一次，全进程共享同一个仓库 + 同一个 session_factory

DeerFlow 在 lifespan 里**只建一次** `FeedbackRepository`，全进程共用（`deps.py:277`）：

```python
app.state.feedback_repo = FeedbackRepository(sf)
```

这里有两层"单例"，要分清楚：

1. **仓库是进程级单例**——整个进程只有一个 `FeedbackRepository` 实例，挂在 `app.state.feedback_repo` 上，所有请求通过 `get_feedback_repo(request)` 拿到的都是**同一个**。
2. **session_factory 也是进程级单例**——传给仓库构造函数的 `sf`，是 `get_session_factory()`（`deps.py:271`）返回的那个**全进程唯一**的工厂。

而且这个 `sf` **不是 feedback 独享的**。看 `deps.py:271` 那行注释：

```python
# Initialize repositories — one get_session_factory() call for all.
sf = get_session_factory()
```

"one ... call for all"——**一个 session_factory，给所有仓库共用**。同一个 `sf` 被传给了 `RunRepository(sf)`（`deps.py:276`）、`FeedbackRepository(sf)`（`:277`）、`ScheduledTaskRepository(sf)`（`:293`）等等。它们共享同一个底层 engine 和连接池，只是各自操作不同的表。

逐行再确认（`deps.py:272-277`）：

```python
if sf is not None:
    from deerflow.persistence.feedback import FeedbackRepository
    from deerflow.persistence.run import RunRepository

    app.state.run_store = RunRepository(sf)            # run 仓库用这个 sf
    app.state.feedback_repo = FeedbackRepository(sf)    # ← 反馈仓库用同一个 sf
```

这样设计的好处：

- **engine / 连接池只建一次**——lifespan 里 `init_engine_from_config`（`deps.py:265`）建好 engine，`get_session_factory()` 在它之上建工厂，所有仓库共享。连接复用率高，不会打爆。
- **仓库本身极轻**——它只是攥着 `sf` 的引用（§0.4），所以"进程级单例"这个地位对它毫无负担（不像 checkpointer 单例要攥着一整个池子）。
- **请求路径零开销**——路由调 `get_feedback_repo(request)` 只是 `getattr` 取一下那个单例，不建任何东西。

⚠️ **注意区分"仓库是单例"和"仓库内部的 session 不是单例"。** 仓库（`FeedbackRepository` 实例）是进程级单例，但这**不意味着**它内部用的 session 也是单例——恰恰相反，仓库**每个方法**都通过 `self._sf()` 现借一个新 session、用完即还（麻烦 3 讲）。单例的是"工厂"，不是"产品"。这一点想清楚，就能理解为什么 feedback 单例是线程/协程安全的——它不持有任何"会跨请求串味"的可变状态，所有可变状态都在方法局部的短命 session 里。

朴素写法**每请求重建工厂、打爆连接**。DeerFlow 用"启动建一次 + 全进程共享同一个 sf"把这件事彻底解决——而且因为仓库本身轻，这个单例几乎是零成本的。

---

### 麻烦 3：每个方法开短命 session，借完即还（为什么 Feedback 不需要连接池生命周期）

#### 场景

并发场景：同一时刻有好几个请求在打分、查分、删分。它们都拿到同一个 `FeedbackRepository` 单例（麻烦 2），都通过同一个 `sf` 借 session。会不会互相踩？session 该怎么管理？

#### 这正是 Feedback"轻"的根源

答案藏在仓库**每个方法**的写法里。看 `create`（`sql.py:32-60`）：

```python
async def create(self, *, run_id, thread_id, rating, user_id=AUTO, message_id=None, comment=None) -> dict:
    if rating not in (1, -1):
        raise ValueError(f"rating must be +1 or -1, got {rating}")
    resolved_user_id = resolve_user_id(user_id, method_name="FeedbackRepository.create")
    row = FeedbackRow(feedback_id=str(uuid.uuid4()), ...)
    async with self._sf() as session:            # ★ 现借一个 session
        session.add(row)
        await session.commit()                   #    提交
        await session.refresh(row)
        return self._row_to_dict(row)
    # 出 async with：session 自动归还（连接还回池），不用手动关
```

逐行讲关键的：

- `async with self._sf() as session:` ★ **这是全文最重要的一行**。`self._sf()` 调工厂，借出一个新 session；`async with` 保证**出这个块时 session 自动关闭**（连接还回底层 engine 的池子）。这个 session 是**方法局部的短命对象**——只在这个方法的一次调用里活着，方法一返回就没了。
- `session.add(row); await session.commit()` 在这个短命 session 里干完活（加一行、提交）。
- 出 `async with` 之后不用写任何清理代码——session 已经被 `async with` 自动归还了。

**仓库的每个方法都是这个套路**——`get`、`list_by_run`、`upsert`、`delete`、`aggregate_by_run`……无一例外，都是 `async with self._sf() as session:` 包一段、用完即还。看 `list_by_run`（`sql.py:90-92`）：

```python
stmt = stmt.order_by(FeedbackRow.created_at.asc()).limit(limit)
async with self._sf() as session:            # 同样：现借
    result = await session.execute(stmt)
    return [self._row_to_dict(r) for r in result.scalars()]
# 还了
```

#### 为什么这样就不需要连接池生命周期

对比一下 checkpointer（姊妹篇 §麻烦 1/§0.5）：checkpointer **自己攥着**一个长寿命的 `AsyncConnectionPool`，这个池子是"进程级的、要占数据库连接名额的、关机时要按顺序关掉的"重资源。所以 checkpointer 必须进 `AsyncExitStack`、必须 lifespan 管、必须关心关机顺序（姊妹篇的 issue #3373）。

Feedback 仓库完全反过来：

- 它**不攥连接池**，只攥一个 `session_factory` 的引用；
- 它每次借的 session **用完当场还**（`async with` 退出即还），从不长期持有；
- 所以它**没有"需要主动关掉"的资源**——借的都及时还了，账上永远清零。

那"关连接"这件事谁负责？——**底层 engine**。`sf` 背后的 engine 是在 `deps.py:265` 的 `init_engine_from_config` 建的，在关机时由 `deps.py:343` 的 `close_engine()` 关。仓库只是 engine 的一个**消费者**，engine 的生命周期不归仓库管。

这就解释了 §0.6 那个"feedback_repo 不进 AsyncExitStack"的事实——**不是忘了，是不需要**。`AsyncExitStack` 是给"自己攥着要关的资源"的对象准备的（checkpointer、stream_bridge）；feedback 仓库两手空空，没有要关的东西，自然不进。

打个比方：checkpointer 像自己**买了个游泳池**放家里（要装、要维护、要排水、搬家时要拆）；feedback 仓库像**去公共泳池办了张卡**（要用就去游、游完就走，泳池的开张和关门不归它管）。所以前者要排进"关停清单"，后者不用。

朴素写法（如果它要"正确"地管 session）要么每请求造工厂（麻烦 2）、要么也得自己攥个池子（变成 checkpointer 那种重对象）。DeerFlow 用"方法级短命 session + 外包 engine 生命周期"这个组合，让 feedback 仓库**既共享了底层连接池、又自己保持轻薄**——这是它"轻"的全部秘密。

---

### 麻烦 4：多租户隔离——谁的分只能谁看、谁删（resolve_user_id + AUTO）

#### 场景

DeerFlow 多租户：A 和 B 都登录了。

- A 给某条 run 点了👍。B 查这条 run 的反馈时，**不该看到** A 的打分（那是 A 的隐私）。
- B 想删某条反馈——**只能删自己的**，删不掉 A 的。
- 更恶意一点：B 拿着 A 的 `feedback_id` 去 `DELETE`，系统该**拒绝**。

这是多租户系统的基本盘：**数据按用户隔离**。

#### 朴素写法为什么做不到

朴素写法的查询长这样（反面教材）：

```python
stmt = select(FeedbackRow).where(FeedbackRow.thread_id == thread_id,
                                 FeedbackRow.run_id == run_id)   # ← 没有 user_id 过滤！
```

没有 `WHERE user_id = ...`，意味着查出来的是**这个 run 上所有人的打分**——A 的👍、B 的👎、C 的评论，全暴露给任何一个调用者。删除也一样，谁能拿到 `feedback_id` 谁就能删，不管是不是自己的。**全表串台。**

#### DeerFlow 怎么解决的：每个方法先 `resolve_user_id`，再带 user_id 过滤

DeerFlow 的仓库方法**无一例外**，第一件事都是 `resolve_user_id`（§0.7）。看 `list_by_run`（`sql.py:77-92`）：

```python
async def list_by_run(self, thread_id, run_id, *, limit=100, user_id=AUTO) -> list[dict]:
    resolved_user_id = resolve_user_id(user_id, method_name="FeedbackRepository.list_by_run")  # ① 解析调用者
    stmt = select(FeedbackRow).where(FeedbackRow.thread_id == thread_id, FeedbackRow.run_id == run_id)
    if resolved_user_id is not None:                                                              # ② 非管理通道 → 过滤
        stmt = stmt.where(FeedbackRow.user_id == resolved_user_id)
    stmt = stmt.order_by(FeedbackRow.created_at.asc()).limit(limit)
    async with self._sf() as session:
        result = await session.execute(stmt)
        return [self._row_to_dict(r) for r in result.scalars()]
```

逐行讲隔离逻辑：

- `resolved_user_id = resolve_user_id(user_id, ...)` ① 把传入的 `user_id`（默认 `AUTO`）解析成真正的值。在请求路径里，路由没显式传 `user_id`，所以走 `AUTO`——从 contextvar 读出当前登录用户（§0.7）。如果当前没登录用户，`resolve_user_id` 会**主动抛 `RuntimeError`**（`user_context.py:186-187`），而不是返回 None 让隔离悄悄失效。这是个**fail-loud** 的设计：宁可报错，也不允许"无主"的查询溜过去。
- `if resolved_user_id is not None:` ② 只有当解析出的不是 `None` 时，才加 `WHERE user_id = ...` 过滤。
- `stmt.where(FeedbackRow.user_id == resolved_user_id)` ★ **隔离语句**。加上这句后，查出来的就只有"当前调用者的"记录。A 调用只查到 A 的，B 调用只查到 B 的。

为什么有 `if resolved_user_id is not None` 这个判断？——因为 §0.7 的**显式 `None` 是管理通道**。migration 脚本或 CLI 工具会显式传 `user_id=None`，表示"我是管理员，要跨用户看全部"。这时 `resolve_user_id` 原样返回 `None`，跳过过滤，就能看到所有用户的记录。**请求路径绝不会走这条**（请求路径是 `AUTO` → 解析成具体用户），所以普通用户永远只能看自己的。

#### `get` 和 `delete` 的"不是你的就当不存在"

隔离在 `get`（`sql.py:62-75`）里有个更细腻的体现：

```python
async def get(self, feedback_id, *, user_id=AUTO) -> dict | None:
    resolved_user_id = resolve_user_id(user_id, ...)
    async with self._sf() as session:
        row = await session.get(FeedbackRow, feedback_id)
        if row is None:
            return None                                          # 真的不存在
        if resolved_user_id is not None and row.user_id != resolved_user_id:
            return None                                          # ★ 存在但不是你的 → 也返回 None
        return self._row_to_dict(row)
```

逐行看隔离那条：

- `if resolved_user_id is not None and row.user_id != resolved_user_id: return None` ★ 这条很关键：**如果查到的记录存在、但不是当前调用者的，也返回 `None`**——和"记录真的不存在"返回同样的值。

为什么要这样？**防枚举攻击**。设想反面——如果"不是你的记录"返回 403 或抛错、"真不存在"返回 404，攻击者就能通过状态码的差异**猜出某个 feedback_id 是否存在**（哪怕看不到内容）。两边都返回 `None` / 404，攻击者从响应里**分不出"不存在"和"不是你的"**，枚举就没了意义。这是个安全上的小细节，但正是多租户系统该有的严谨。

`delete`（`sql.py:110-125`）也是同样逻辑：不是你的记录 `return False`（没删成），和"记录不存在"一样。调用方路由据此返回 404（`feedback.py:186-187`）。

#### 谁负责把"当前用户"塞进 contextvar

仓库方法依赖 contextvar（`_current_user`）里有当前用户。这个 contextvar 是**认证中间件**在请求进来时设的——认证中间件解出登录用户后，把它 set 到 contextvar，之后这个请求里所有走 `AUTO` 的仓库调用都能读到。路由层只是用 `user_id = await get_current_user(request)`（`feedback.py:73` 等）显式取一次，然后**显式传给仓库**（`feedback.py:87` 的 `user_id=user_id`）——这时 `resolve_user_id` 走的是"显式 str"分支（§0.7 的②）。两条路（contextvar 自动读、路由显式传）最终都汇到同一个 `resolved_user_id`，都加同样的 `WHERE user_id = ...`。

朴素写法**不分用户、全表串台**。DeerFlow 用"`resolve_user_id` 三态 + 每个方法都过滤 + 不是你的当不存在"三件套，把多租户隔离焊死了——普通用户只能看/改自己的，管理员有显式 `None` 通道，攻击者枚举不了。

---

### 麻烦 5：upsert 幂等 + 唯一约束——一人一票，不重复

#### 场景

用户在聊天界面点👍。然后改主意了，又点👎。或者手抖，连点了两下👍。系统该怎么表现？

期望行为：**一个用户对一个 run，永远只有一条反馈**——点了赞再点踩，是**改票**（👍→👎），不是多出一条；连点两下，第二次是**无操作**（或刷新成同样的值），不是多出两条。

这就是"**幂等**"——同一个请求重复发，结果和发一次一样。

#### 朴素写法为什么做不到

朴素写法图省事，写反馈就是纯 INSERT：

```python
# 反面教材
async def naive_create(run_id, thread_id, rating, user_id):
    row = FeedbackRow(run_id=run_id, ..., rating=rating, user_id=user_id)
    async with session:
        session.add(row); await session.commit()     # ← 每次 INSERT 一条新的
```

连点两下 → INSERT 两次 → 数据库里两条同样的👍。点踩 → 又 INSERT 一条👎 → 这条 run 上同时有👍和👎两条，统计时 `aggregate_by_run` 算出来"一正一负"，完全乱了。**根本不幂等。**

#### DeerFlow 怎么解决的：ORM 唯一约束（底线）+ 仓库 upsert（乐观路径）

DeerFlow 在**两层**保证"一人一票"。

**第一层（底线）：ORM 模型上的唯一约束。** 看 `model.py:13-16`：

```python
class FeedbackRow(Base):
    __tablename__ = "feedback"
    __table_args__ = (
        UniqueConstraint("thread_id", "run_id", "user_id", name="uq_feedback_thread_run_user"),
    )
```

逐行：

- `UniqueConstraint("thread_id", "run_id", "user_id", ...)` §0.8 讲过——声明"这三列的组合在表里唯一"。也就是说，**同一个 `(thread_id, run_id, user_id)` 最多只能有一行**。
- 这条约束是**数据库层的物理保证**：就算应用层逻辑全坏了、硬要 INSERT 第二条，数据库也会抛 `IntegrityError` 拒绝。它是最后一道防线。

注意它的范围：唯一的是**三列组合**，不是单列。所以：

- 同一个用户对同一个 run → 最多一条（受约束）。
- 同一个 run 被不同用户各打分 → 每人一条，互不影响（不同 user_id，不撞约束）。
- 同一个用户对不同 run → 各一条（不同 run_id，不撞约束）。

**第二层（乐观路径）：仓库的 `upsert` 方法。** 光有约束还不够——它只会让重复 INSERT **报错**，不会让重复请求**静默改票**。要做到"连点不报错、改票自动覆盖"，得在应用层用 upsert。看 `sql.py:127-165`：

```python
async def upsert(self, *, run_id, thread_id, rating, user_id=AUTO, comment=None) -> dict:
    """Create or update feedback for (thread_id, run_id, user_id). rating must be +1 or -1."""
    if rating not in (1, -1):
        raise ValueError(f"rating must be +1 or -1, got {rating}")
    resolved_user_id = resolve_user_id(user_id, ...)
    async with self._sf() as session:
        stmt = select(FeedbackRow).where(
            FeedbackRow.thread_id == thread_id,
            FeedbackRow.run_id == run_id,
            FeedbackRow.user_id == resolved_user_id,
        )
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()                    # ① 先查：这个三元组有没有已存在的记录？
        if row is not None:                                  # ② 有 → UPDATE（改票）
            row.rating = rating
            row.comment = comment
            row.created_at = datetime.now(UTC)
        else:                                                # ③ 没有 → INSERT（新建）
            row = FeedbackRow(feedback_id=str(uuid.uuid4()), ...)
            session.add(row)
        await session.commit()
        await session.refresh(row)
        return self._row_to_dict(row)
```

逐行讲幂等逻辑：

- `stmt = select(FeedbackRow).where(thread_id == ..., run_id == ..., user_id == resolved_user_id)` 按"一人一票"的三元组查——当前用户对这个 run 有没有已存在的反馈？
- `row = result.scalar_one_or_none()` ① 取结果。注意 `scalar_one_or_none`——它期望**最多一行**。为什么最多一行？因为有 §0.8 的唯一约束兜底，这个三元组在表里**物理上不可能有多行**。所以这里敢用 `scalar_one_or_none`（多于一行会抛错，而约束保证不会多于一行）。
- `if row is not None:` ② 已存在 → **改票**：把 `rating` 改成新值、更新 `comment`、刷新 `created_at`。**不新增行。** 这就是"点赞再点踩 = 改票"的实现。
- `else:` ③ 不存在 → **新建**一条。
- `await session.commit()` 无论走哪条，最后都提交。

这个 upsert 是**乐观路径**——它假设"绝大多数时候不会撞约束"（要么没记录要新建、要么有记录要改），所以用普通的 SELECT + UPDATE/INSERT，不走数据库的 `ON CONFLICT` 这种重武器。万一乐观路径失手（极端竞态下两个并发 upsert 都走到 else 分支都 INSERT），第二层的唯一约束会兜住——其中一个 INSERT 撞 `IntegrityError`，不会真的产生重复行。

#### 路由层的幂等语义：PUT

路由这边用的是 **PUT**（`feedback.py:61`），不是 POST：

```python
@router.put("/{thread_id}/runs/{run_id}/feedback", response_model=FeedbackResponse)
@require_permission("threads", "write", owner_check=True, require_existing=True)
async def upsert_feedback(thread_id, run_id, body, request):
    ...
    feedback_repo = get_feedback_repo(request)
    return await feedback_repo.upsert(run_id=run_id, thread_id=thread_id,
                                      rating=body.rating, user_id=user_id, comment=body.comment)
```

PUT 的 HTTP 语义本来就是**幂等**的（RFC 7231：同一个 PUT 请求重复发，服务端状态应当和发一次一样）。这里用 PUT 配合仓库的 upsert，**端到端幂等**：前端连点、重试、网络抖动重发，服务端永远只有一条记录、值是最后一次的。这和 §0.9 提到的"反馈场景几乎不出现 409"是一体两面——run 那边用 409 报告"冲突"，反馈这边用 upsert **把冲突消解成覆盖**，根本不产生冲突。

> 注意：仓库**另有一个 `create` 方法**（`sql.py:32`）和对应的 POST 路由（`feedback.py:112`），它是**非幂等**的纯新建（不带 upsert 逻辑）。这是给"明确要新建、且接受 message_id 维度"的场景用的（POST 路由支持 `message_id`，PUT 不支持）。绝大多数前端"👍👎"按钮走的是 PUT（幂等），更安全。两者并存是有意的——一个管"幂等改票"、一个管"新建带消息维度"。

朴素写法**纯 INSERT、连点出多条、点踩改不了赞**。DeerFlow 用"唯一约束兜底 + upsert 乐观改票 + PUT 幂等语义"三层，让"一人一票"在数据库层、仓库层、HTTP 履约层都成立。

---

### 麻烦 6：读侧 enrichment——不让打分查询拖垮消息列表（防 N+1）

#### 场景

用户打开一个 thread 的聊天记录，前端发 `GET /api/threads/{thread_id}/messages`（`thread_runs.py:716`）。这个接口要返回这个 thread 里**所有消息**，而且——**每条 AI 消息上要带上"当前用户对这条 run 打了几分"**（这样前端才能在 AI 回复旁边显示用户自己的👍/👎状态）。

这是个典型的"读侧 enrichment（增富）"场景：主查询是"列消息"，每条消息还要附加一个"副产品"（用户的反馈）。

#### 朴素写法为什么做不到（N+1 查询问题）

朴素写法会这么干（反面教材）：

```python
messages = await event_store.list_messages(thread_id)        # 1 次查询：取出所有消息
for msg in messages:                                          # N 次：对每条 AI 消息
    if msg.is_ai:
        fb = await feedback_repo.list_by_run(thread_id, msg.run_id, user_id=user_id)  # ← 又查一次
        msg["feedback"] = fb[0] if fb else None
return messages
```

这就是臭名昭著的 **N+1 查询**：1 次查消息 + N 次（每条 AI 消息一次）查反馈 = N+1 次数据库往返。一个 thread 有 50 条 AI 消息，就是 51 次查询。每次查询都有网络/连接开销，列表接口会慢得不能忍。

#### DeerFlow 怎么解决的：一次批量查 + 内存里拼回

DeerFlow 在仓库里提供了**两个批量查询方法**，专门服务这种 enrichment 场景：

**方法一：`list_by_thread_grouped`（`sql.py:190-203`）——一次查整个 thread，按 run_id 分组成 dict。**

```python
async def list_by_thread_grouped(self, thread_id, *, user_id=AUTO) -> dict[str, dict]:
    """Return feedback grouped by run_id for a thread: {run_id: feedback_dict}."""
    resolved_user_id = resolve_user_id(user_id, ...)
    stmt = select(FeedbackRow).where(FeedbackRow.thread_id == thread_id)   # ★ 一次查整个 thread
    if resolved_user_id is not None:
        stmt = stmt.where(FeedbackRow.user_id == resolved_user_id)
    async with self._sf() as session:
        result = await session.execute(stmt)
        return {row.run_id: self._row_to_dict(row) for row in result.scalars()}  # ★ 内存里按 run_id 分组
```

逐行：

- `select(FeedbackRow).where(thread_id == thread_id)` ★ **一次查询**就把这个 thread（当前用户）的所有反馈全捞出来。不是一条一条查，是**一把全拿**。
- `return {row.run_id: ... for row in result.scalars()}` ★ 在**内存里**把结果按 `run_id` 分组成一个 dict（`{run_id: feedback_dict}`）。这样调用方拿到 dict，按 `run_id` 直接 O(1) 取，不用再查库。

效果：从 N+1 次查询降到**1 次**。

**方法二：`list_by_run_ids`（`sql.py:205-224`）——只查一批指定的 run_id。**

```python
async def list_by_run_ids(self, thread_id, run_ids: set[str], *, user_id=AUTO) -> dict[str, dict]:
    """Return feedback for only the selected runs in one thread."""
    if not run_ids:
        return {}                                              # 空集合 → 直接返回，不查
    resolved_user_id = resolve_user_id(user_id, ...)
    stmt = select(FeedbackRow).where(
        FeedbackRow.thread_id == thread_id,
        FeedbackRow.run_id.in_(run_ids),                       # ★ 用 IN 一次查一批
    )
    if resolved_user_id is not None:
        stmt = stmt.where(FeedbackRow.user_id == resolved_user_id)
    async with self._sf() as session:
        result = await session.execute(stmt)
        return {row.run_id: self._row_to_dict(row) for row in result.scalars()}
```

逐行讲关键的：

- `if not run_ids: return {}` **守卫**：传进来的 `run_ids` 是空集就直接返回空 dict，**根本不查库**。这是个省钱的小细节——分页场景下一页可能没有任何 AI 消息要带 feedback，这时没必要发一条空查询。
- `FeedbackRow.run_id.in_(run_ids)` ★ 用 SQL 的 `IN` 操作符**一次查一批 run_id**，而不是每个 run_id 查一次。这就是把 N 次查询压成 1 次的标准手段。
- 返回值同样是 `{run_id: feedback_dict}` 的 dict，方便调用方按 run_id 取。

#### 消费方怎么用：两个"有没有要带的"守卫

光有批量查询方法还不够，调用方得**用对**。看 `list_thread_messages`（`thread_runs.py:732-755`）怎么消费 `list_by_thread_grouped`：

```python
# Attach feedback to the last AI message of each run. Only query when there
# is an AI message to attach it to — threads with no completed AI turn yet
# would otherwise pay for a grouped feedback lookup whose result is unused.
feedback_map: dict[str, dict] = {}
if last_ai_per_run:                                           # ★ 守卫1：这个 thread 有 AI 消息吗？
    feedback_repo = get_feedback_repo(request)
    feedback_map = await feedback_repo.list_by_thread_grouped(thread_id, user_id=user_id)

last_ai_indices = set(last_ai_per_run.values())
for i, msg in enumerate(messages):
    if i in last_ai_indices:
        run_id = msg["run_id"]
        fb = feedback_map.get(run_id)                         # 内存里 O(1) 取
        msg["feedback"] = ({...} if fb else None)
    else:
        msg["feedback"] = None
```

逐行讲两个守卫：

- `if last_ai_per_run:` ★ **守卫1**：先在内存里算出"这个 thread 有没有 AI 消息、分别是哪些 run 的"。如果一个 AI 消息都没有（比如用户刚发第一条、agent 还没回），`last_ai_per_run` 是空 dict，**整个 feedback 查询都不发**——因为查了也没用（没有 AI 消息可挂）。注释说得很直白："threads with no completed AI turn yet would otherwise pay for a grouped feedback lookup whose result is unused"（没 AI 回复的 thread 会白白付一次查询费，结果还用不上）。
- `fb = feedback_map.get(run_id)` 拿到分组 dict 后，**按 run_id 在内存里 O(1) 取**——不再碰数据库。

再看分页那条 `_enrich_thread_message_page`（`thread_runs.py:855-871`）消费 `list_by_run_ids`：

```python
feedback_map: dict[str, dict] = {}
feedback_run_ids = {run_id for row in data
                    if isinstance((run_id := row.get("run_id")), str)
                    and row.get("seq") == last_ai_seq_by_run.get(run_id)}   # 先算出"哪些 run 需要带 feedback"
if feedback_run_ids:                                                        # ★ 守卫2：有需要带的吗？
    feedback_repo = get_feedback_repo(request)
    feedback_map = await feedback_repo.list_by_run_ids(thread_id, feedback_run_ids, user_id=user_id)

for row in data:
    run_id = row.get("run_id")
    row["feedback"] = None
    if row.get("seq") == last_ai_seq_by_run.get(run_id):
        feedback = feedback_map.get(run_id)                                 # 内存里取
        if feedback:
            row["feedback"] = {...}
```

- `feedback_run_ids = {...}` 先用一个集合推导算出"这一页里，哪些 run 的最后一条 AI 消息需要带 feedback"（只取每个 run 的最后一条 AI 消息挂 feedback，不是每条都挂）。
- `if feedback_run_ids:` ★ **守卫2**：只有这个集合非空才查。分页的某一页可能恰好没有"run 的最后一条 AI 消息"（都在别的页），这时不发查询。

这两个守卫体现的思想是：**enrichment 是副产品，不该为它付无谓的查询费。** 主查询（消息列表）已经算出了"哪些位置需要 feedback"，那就只在这些位置存在时才去查 feedback，而且一次查全。

#### 顺带：`aggregate_by_run`——统计走数据库侧聚合

除了"带单个用户的反馈"，反馈还有个"统计"场景：某条 run 总共收到几个👍几个👎。看 `aggregate_by_run`（`sql.py:226-240`）：

```python
async def aggregate_by_run(self, thread_id, run_id) -> dict:
    """Aggregate feedback stats for a run using database-side counting."""
    stmt = select(
        func.count().label("total"),
        func.coalesce(func.sum(case((FeedbackRow.rating == 1, 1), else_=0)), 0).label("positive"),
        func.coalesce(func.sum(case((FeedbackRow.rating == -1, 1), else_=0)), 0).label("negative"),
    ).where(FeedbackRow.thread_id == thread_id, FeedbackRow.run_id == run_id)
    async with self._sf() as session:
        row = (await session.execute(stmt)).one()
        return {"run_id": run_id, "total": row.total, "positive": row.positive, "negative": row.negative}
```

逐行讲关键的：

- `func.count()` / `func.sum(case(...))` ★ 这些是 **SQL 函数**——`count`、`sum`、`case` 都被翻译成数据库侧的聚合操作。也就是说，"数有几个👍"这件事是**数据库算完再把结果（三个数字）传回来**，不是把所有行捞回 Python 再循环数。
- 注意：这个方法**不带 user_id 过滤**（参数里没有 `user_id`）——因为它是"全 run 的统计"（所有用户对这个 run 的👍👎总数），是给"看这条 run 整体评价"的场景用的，不分用户。这是个**有意的不隔离**——统计本来就是跨用户的，隔离没意义。

为什么要把聚合下推到数据库？——如果朴素地 `select * ` 捞回所有行再在 Python 里 `sum`，1000 条反馈就要传 1000 行过网络、在 Python 里循环。下推到 SQL，网络只传 3 个数字（total/positive/negative），快得多。这就是 docstring 里 "using database-side counting" 的意思。

朴素写法**N+1 查询、列表接口被拖垮**。DeerFlow 用"批量查询方法（grouped / by_run_ids）+ 内存拼回 + 两个'有没有要带的'守卫 + 数据库侧聚合"这套组合，让反馈这个副产品**不会拖累主列表查询**。

---

## 第 3 步：把 6 个麻烦的本质串起来

朴素写法 `repo = FeedbackRepository(make_some_session_factory())`（每个请求自己造工厂、不分用户、纯 INSERT）的根本问题是：

> **把仓库当成了"一个随手 new 的临时对象"，丢掉了配置驱动的可用性边界、进程级单例、多用户隔离、写入幂等、读侧批量——把它当成了一个没有任何业务约束的数据结构。**

而现实里，一个"反馈仓库"要面对的约束远不止"能读写"：

| 维度 | 朴素写法（每请求 new） | 真实写法（`app.state` 单例 + `_require` + 仓库内隔离/幂等） |
|---|---|---|
| **可用性边界** | 假定 DB 一定在，没 DB 就崩 500；或假装可用丢数据 | 有 DB 给真仓库、没 DB 显式 None + `_require` 503 |
| **单例/连接** | 每请求造工厂，连接打爆 | 进程级单例仓库，全仓库共享同一个 session_factory |
| **资源生命周期** | 要么不管（泄漏）、要么自己攥池子（变重） | 方法级短命 session，外包 engine 生命周期，不进 AsyncExitStack |
| **多租户隔离** | 不带 user_id 过滤，全表串台 | `resolve_user_id` 三态 + 每方法过滤 + 不是你的当不存在 |
| **幂等** | 纯 INSERT，连点出多条 | 唯一约束兜底 + upsert 乐观改票 + PUT 语义 |
| **读侧性能** | N+1 查询，列表被拖垮 | 批量查询 + 内存拼回 + "有没有要带的"守卫 + DB 侧聚合 |

把仓库当临时对象，就会出现 6 个麻烦——因为现实里它的约束**就是这 6 个维度**：

- ① 没配数据库时该不该可用 → `deps.py:271-282` if/else + `_require` 503
- ② 不能每请求 new → lifespan 建一次进程级单例 + 共享 session_factory
- ③ 不该自己攥重资源 → 方法级短命 session，外包 engine 生命周期
- ④ 多用户数据隔离 → `resolve_user_id` + 每方法 WHERE user_id + 不是你的当 None
- ⑤ 一人一票幂等 → 唯一约束 + upsert + PUT
- ⑥ 读侧别拖垮列表 → 批量查询 + 内存拼回 + 守卫 + DB 侧聚合

`get_feedback_repo(request)` 就是这一切的入口。它本身只是一行 `_require` 的产出（取 `app.state.feedback_repo`、取到 None 就 503）；但它背后牵着一条完整的链——`app.py` 的 lifespan → `deps.py:271-282` 按 DB 配置决定 feedback_repo 是真仓库还是 None → 每个请求里 `get_feedback_repo(request)` 取出来 → 仓库内部用 `resolve_user_id` 隔离、用 upsert 幂等、用批量查询防 N+1。一张全景图：

```
                ┌─────────────────────────────────────────────────────────────────┐
                │              进程启动（lifespan，app.py）                        │
                │                                                                 │
                │  langgraph_runtime（deps.py:222）                              │
                │    │                                                            │
                │    │  await init_engine_from_config(...)  ← 建底层 engine       │
                │    │                                                            │
                │    │  sf = get_session_factory()                                │
                │    │  ┌── if sf is not None: ──────────────────────────┐        │
                │    │  │   app.state.feedback_repo =                    │        │
                │    │  │       FeedbackRepository(sf)  ← 麻烦1/2 真仓库 │        │
                │    │  │       （共享 RunRepository 等的同一个 sf）      │        │
                │    │  ├── else: ──────────────────────────────────────┤        │
                │    │  │   app.state.feedback_repo = None ← 麻烦1 假装NO│        │
                │    │  └────────────────────────────────────────────────┘        │
                │    │  （注意：feedback_repo 不进 AsyncExitStack ← 麻烦3）       │
                │    │                                                            │
                │    │  yield  ← 应用开始接请求                                   │
                │    └──────────────────────────────────────────────────────────  │
                └─────────────────────────────────────────────────────────────────┘
                                      │ app.state.feedback_repo（None-able 单例）
                                      ▼
                ┌─────────────────────────────────────────────────────────────────┐
                │                        每次 HTTP 请求                            │
                │                                                               │
                │  feedback_repo = get_feedback_repo(request)                   │
                │       │  ← _require：getattr app.state，None 就抛 503（麻烦1）│
                │       ▼                                                       │
                │  ┌──────────── 写路径（打分/改票/删除）────────────┐            │
                │  │ user_id = await get_current_user(request)     │            │
                │  │ resolved = resolve_user_id(user_id)  ← 麻烦4  │            │
                │  │ async with self._sf() as session:   ← 麻烦3  │            │
                │  │   upsert(): 先查后改/插           ← 麻烦5    │            │
                │  │   （UniqueConstraint 兜底）        ← 麻烦5    │            │
                │  └───────────────────────────────────────────────┘            │
                │  ┌──────────── 读路径（列表 enrichment）──────────┐            │
                │  │ if 有 AI 消息要带 feedback:        ← 守卫     │            │
                │  │   feedback_map = await                          │            │
                │  │     list_by_thread_grouped / list_by_run_ids   │← 麻烦6    │
                │  │   （一次批量查，内存里按 run_id 拼回）          │            │
                │  └───────────────────────────────────────────────┘            │
                └─────────────────────────────────────────────────────────────────┘
```

所以它不是"代码好看"或"封装一下"——是这 6 个麻烦里**任意一个**单拿出来，朴素写法都过不去。

它和前三篇姊妹篇是**同一套机制（`app.state` 单例 + `_require` 503）**，但回答的是**截然不同的问题**：

- `get_stream_bridge`（`deps.py:364`）解耦**事件流**的生命周期——让 run 的事件能跨请求、跨订阅者流动。**重资源**，进 `AsyncExitStack`。
- `get_run_manager`（`deps.py:365`）解耦**执行 + 状态**的生命周期——让 run 能后台跑、可查可取消、可恢复。**重资源**，进 `AsyncExitStack`。
- `get_checkpointer`（`deps.py:366`）解耦**存档（持久化）**的生命周期——让 checkpoint 有正确的后端、开关机顺序、多进程语义。**重资源**，进 `AsyncExitStack`。
- `get_feedback_repo`（`deps.py:368`，本篇）解耦的是**一个可选功能**的生命周期——没 DB 时诚实不可用、有 DB 时隔离 + 幂等 + 读侧不拖垮。**轻资源**，**不进** `AsyncExitStack`。

前三篇的共同主题是"**进程级资源必须活久且正确关停**"——它们都自己攥着重资源，所以必须有 lifespan 建一次、`AsyncExitStack` 排好关停顺序、多 worker 强制 Postgres。

本篇的主题是**它的反面**：**有些功能本就可选，要诚实地表达它的可用/不可用；而在可用时，麻烦不在"活久"，在"隔离、幂等、读侧聚合"。** feedback 仓库故意把"重"的部分（连接池生命周期）外包给底层 engine，自己保持轻薄——所以它能用同一套 `_require` 机制，却**不需要**前三篇那一整套关停编排。它的"缺席 AsyncExitStack"不是疏忽，而是**"我没有什么要关的"**这句话的物理表达。

一句金句收尾：前三篇讲的是"**重要的东西必须活得久、死得体面**"，这一篇讲的是"**可选的东西可以不活，但只要活着，就得守规矩**"——而 `get_feedback_repo(request)` + `_require` 那一行 503，就是"它现在到底活没活"的诚实回答。
