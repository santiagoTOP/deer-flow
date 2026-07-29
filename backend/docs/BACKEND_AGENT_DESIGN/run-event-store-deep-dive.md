# get_run_event_store 深度讲解：为什么不能只把事件塞进 SSE 流就完事

本文是 `stream-bridge-deep-dive.md` / `run-manager-deep-dive.md` / `get-checkpointer-deep-dive.md` 的姊妹篇（第四篇）。前三篇讲的是 `bridge = get_stream_bridge(request)`、`run_mgr = get_run_manager(request)`、`checkpointer = get_checkpointer(request)` 为什么存在；这篇讲的是紧挨着它们的另一行——`event_store = get_run_event_store(request)` 为什么存在。

具体到源头，本文讲的就是 `backend/app/gateway/deps.py:367` 这一行：

```python
get_run_event_store: Callable[[Request], RunEventStore] = _require("run_event_store", "Run event store")
```

它和 `get_stream_bridge`（`deps.py:364`）、`get_run_manager`（`deps.py:365`）、`get_checkpointer`（`deps.py:366`）是同一行连着的四个兄弟声明。四篇文章回答的是同一个大问题的四个面：**一次 HTTP 请求里启动的 agent run，凭什么能活得比这次请求久？**

- **StreamBridge** 负责"事件流"这一面——agent 正在跑时产出的**实时事件**，怎么被任意数量、任意时刻、跨进程的 HTTP 订阅者消费。
- **RunManager** 负责"执行 + 状态"这一面——run 的 task 怎么活过创建请求、可查可取消、崩溃了能被收尸。
- **Checkpointer** 负责"存档"这一面——agent 的 checkpoint 该存在哪儿、连接池怎么开怎么关、进程死了存档留不留得住。
- **RunEventStore**（本篇）负责"事件历史"这一面——**run 跑完之后，它产出的那些事件（消息、工具调用、轨迹），该怎么被查询、怎么分页、怎么按对话顺序排、怎么多租户隔离。**

建议四篇对照着读。其中尤其要和姊妹篇 `stream-bridge-deep-dive.md` 对照——因为 StreamBridge 和 RunEventStore 处理的是**同一批事件的两个不同访问模式**（一个"活流"，一个"死档"），很容易搞混，本篇第 0 步会专门把两者的边界划清。

本文面向第一次接触这套架构的同学。先把所有需要的前置概念一个一个铺出来，然后再讲 6 个麻烦，每段代码逐行解释。

---

## 第 0 步：先把要用的术语讲清楚

在讲任何代码之前，先把后面会反复出现的概念讲明白。这些是理解"为什么朴素写法会出问题"的地基。已经读过姊妹篇的同学可以快速跳过带 ※ 标记的小节（那里会注明"姊妹篇讲过"，本篇只复用结论）。

### 0.1 event——run 跑的过程中发生的一件事

agent 在跑一次 run 的过程中，会**不断地发生"事件"**：用户发了一句话（`human_message`）、模型回复了一段话（`llm.ai.response`）、调了一次搜索工具（`tool.call`）、某个子 agent 开始干活（`subagent.start`）、run 结束了（`run.end`）…… 每一个这样的"发生了的事"就是一条 **event**。

打个比方：event 就像餐厅订单背后那张**"操作记录单"**——厨师每做一步（切菜、下锅、尝味、装盘）都在上面记一笔。这张记录单不是给客人看的菜单，是厨房内部用的流水账。

DeerFlow 里一条 event 长这样（以内存版 `MemoryRunEventStore._put_one` 组装的 record 为准，`memory.py:50-60`）：

```python
record = {
    "thread_id": thread_id,      # 归属哪段对话
    "run_id": run_id,            # 归属哪次 run
    "event_type": event_type,    # 事件类型（如 "llm.ai.response"）
    "category": category,        # 分类：message / trace / lifecycle（0.3 讲）
    "content": content,          # 事件内容（文本或结构化对象）
    "metadata": metadata or {},  # 附加信息（如 task_id、caller）
    "seq": seq,                  # ★ 这个 thread 内的全局序号（0.4 讲）
    "created_at": now_iso(),     # 何时发生
}
```

⚠️ 注意：源码里**没有一个 `class RunEvent` 的类**（`base.py` 接口契约第 5 条说的"the RunEvent field structure"指的就是上面这个 dict 的形状）。三个后端（内存 / 数据库 / JSONL）返回的 record 都是这个形状——这就是"接口一致"的体现。

关键点：**event 是 run 留下的"历史痕迹"。** 它记录了"这次 run 里发生过什么"。这些痕迹有两个完全不同的用途（下面 0.2、0.3 讲），而 RunEventStore 就是统一管这些痕迹的地方。

### 0.2 StreamBridge vs RunEventStore——"活流"和"死档"的分界线

※ 这个小节是本篇最重要的一节，必须和姊妹篇 `stream-bridge-deep-dive.md` 对照着读。

同一批 event，DeerFlow 用**两个完全独立的机制**来服务两种访问模式：

| 维度 | StreamBridge（姊妹篇） | RunEventStore（本篇） |
|---|---|---|
| **访问时机** | run **还在跑的时候** | run **跑完之后**（或跑的过程中随时查历史） |
| **访问方式** | **订阅 SSE 流**（push，事件来了就推） | **主动查询**（pull，发一个 HTTP 请求要某段历史） |
| **数据载体** | 进程内存里的"广播总线" + 可选 Redis | 进程内存 / 数据库表 / JSONL 文件 |
| **跨进程可见** | 默认不可见（进程内存）；配 Redis 才可见 | 取决于后端：db/jsonl 跨进程可见，memory 不可见 |
| **典型场景** | 前端边跑边显示 agent 一句一句吐字 | 刷新页面后展示历史消息列表；调试时查某次 run 的完整轨迹 |
| **持久化** | 不持久化（run 结束或进程重启就没了） | db/jsonl 后端**持久化**（重启后历史还在） |

打个比方，同一个事件就像是**一道菜做出来的过程**：
- **StreamBridge** 像是厨房窗口上挂的**"实时叫号屏"**——菜做到哪一步了，厨师边做边喊"下锅了！装盘了！"，外面的服务员（前端订阅者）实时看到。但叫号屏**只播一遍**——你晚来一步就错过了。
- **RunEventStore** 像是订单后面那张**"操作记录单"**——上面记满了厨师做过的每一步，不管你什么时候来翻、翻多少次，**历史都还在**。

**所以两者不是冗余，而是互补：** StreamBridge 解决"实时性"，RunEventStore 解决"持久性和可查询性"。两个地方存的是**同一批 event 的两份副本**——agent 跑的时候，`RunJournal`（事件收集器）既把事件推给 StreamBridge（给实时订阅者），又写进 RunEventStore（给将来的查询者）。

这就解释了为什么"只往 SSE 流塞事件"是错的（第 1 步的反面教材）——那是只要了叫号屏、没要记录单，刷新页面后历史全没。

### 0.3 category——同一批事件分两类用途

event 上面有个 `category` 字段，把所有事件分成两大类（数据库注释 `persistence/models/run_event.py:25` 写得明白：`# "message" | "trace" | "lifecycle"`）：

- **`message`（消息）**：**给用户看的**。比如人类发的那句话、模型回复的那段话。这些是要在聊天界面**展示**给用户看的"消息"。
- **`trace`（轨迹）**：**给开发者看的**。比如模型内部的思考、工具调用的参数和返回、子 agent 的执行细节。这些是用来**调试和审计**的，不该混进用户的消息列表里。
- **`lifecycle`（生命周期）**：run 的状态变化（开始、结束、中断）。

为什么要分？因为**前端查"历史消息列表"时，绝不希望混进一堆调试轨迹**——用户只想看"我说了啥、agent 回了啥"。但开发者查"这次 run 到底发生了什么"时，又需要看到全部细节。同一个存储接口，靠 `category` 字段区分这两种查询。

`RunEventStore` 接口为此提供了两组读方法（`base.py`）：

- `list_messages(...)`——**只返回 `category="message"` 的事件**（接口契约第 3 条）。
- `list_events(...)`——**返回指定 run 的全部事件**（不分类，给调试用）。

打个比方：这就像订单记录单上，给客人看的"菜品进度"和给厨师长看的"操作细节"**用不同颜色的笔写**——翻"客人看的"那页只看菜品进度（message），翻"操作细节"那页能看到全部（all events）。同一张单子，两种翻法。

### 0.4 seq——这个 thread 内的全局单调序号

这是本篇**最核心**的一个概念。每条 event 都带一个 `seq`（sequence number），它是个整数。关键约定（接口契约第 2 条，`base.py:24`）：

> **`seq` is strictly increasing within the same thread.**
> （seq 在同一个 thread 内严格递增。）

注意三个字眼：

1. **同一个 thread 内**——seq 的取值范围是**一个 thread_id 一条数轴**，不是全库一条数轴，也不是一个 run 一条数轴。
2. **严格递增**——后写的 event 的 seq 一定比先写的大，不存在回退或相等。
3. **全局**（thread 内的全局）——**不按 run 分段**。同一个 thread 里，run A 的第一条消息 seq=1、第二条 seq=2，然后 run B 接着第一条 seq=3、第二条 seq=4…… 它们串在同一条时间轴上。

为什么这么设计？因为**前端展示历史消息时，是按 thread 整体翻页的，不是按 run 翻的**。用户打开一个对话，看到的是"我说一句、agent 回一句、我又说一句、agent 又回一句"的连续对话流——这些消息来自多次不同的 run，但必须按**一条统一的时间轴**排好序，才能正确还原对话的先后。如果 seq 是按 run 各自从 1 数的，那同一个 thread 里就会出现多条 seq=1 的消息，根本没法排序。

打个比方：seq 就像订单记录单上**逐笔编的流水号**——不管这张单子是哪天的、哪个厨师做的，只要它还挂在同一个**桌号（thread）**下，流水号就**从头到尾一条线编下去**（1、2、3、4……）。这样你任何时候翻这张单子，按流水号一排，所有操作的先后顺序就一目了然。

后面麻烦 2、麻烦 4 会反复围绕"怎么保证 seq 在 thread 内严格递增"展开——这是 RunEventStore 最难的并发问题。

### 0.5 `app.state`——进程级"工具箱"

※ 概念基础和姊妹篇 `get-checkpointer-deep-dive.md` §0.3 完全一致，本篇只复用结论。

FastAPI 应用对象 `app` 上挂着 `app.state`，是个进程级的"工具箱"。DeerFlow 里 RunEventStore 在启动时被"放进工具箱"（`app.state.run_event_store = ...`），每个请求里被"从工具箱拿出来"（`get_run_event_store(request)`）。工具箱是进程级的——一个 worker 进程就有一个，进程死了它也跟着没了。

⚠️ 这一点对理解三个后端的差异很关键：**`app.state.run_event_store` 是进程级单例，但它背后的存储不一定是进程级的。** Memory 后端是进程内存（死了即丢）；db/jsonl 后端连的是**进程外的数据库/文件**（进程死了数据还在）。同样是"进程级单例"，存活的语义天差地别——后面麻烦 1、麻烦 6 会展开。

### 0.6 lifespan + AsyncExitStack——开机建、关机拆

※ 概念基础和姊妹篇 `get-checkpointer-deep-dive.md` §0.4 / §0.5 重叠，这里聚焦在 RunEventStore 的特殊性。

FastAPI 的 lifespan 在应用启动时建单例、关机时清理。RunEventStore 也是在 lifespan 里建一次（`deps.py:305` 那行 `app.state.run_event_store = make_run_event_store(...)`）。

但这里有个和 checkpointer **不一样**的关键点：

- **checkpointer**（姊妹篇）背后是数据库连接池，是个"重资源"——必须用 `AsyncExitStack` 托付生命周期，关机时要按顺序关连接池（否则踩 `PoolClosed`）。
- **RunEventStore** 的三个后端里，**只有 db 后端间接依赖那个引擎连接池**（它持有 session_factory，session_factory 从引擎借连接）；**memory 和 jsonl 后端根本不占"重资源"**——memory 就是个 dict，jsonl 就是一堆文件句柄，都不需要进 `AsyncExitStack`。

所以你看 `deps.py:299-305`，RunEventStore 是**直接赋值**给 `app.state` 的，**没有走 `stack.enter_async_context(...)`**：

```python
app.state.run_event_store = make_run_event_store(run_events_config)   # 直接赋值，没进 stack
```

这和 checkpointer 那行（`deps.py:267` 的 `await stack.enter_async_context(make_checkpointer(config))`）形成了对照。为什么 RunEventStore 不用进 stack？因为它的 db 后端**共享 checkpointer 建好的那个引擎**（`get_session_factory()` 取的是全局引擎，不是自己开的池）——引擎的生命周期由 `init_engine_from_config` / `close_engine` 那条线统一管，RunEventStore 只是**借**它的 session_factory 用，不负责开关。memory/jsonl 又没有重资源。所以关机时不用为 RunEventStore 单独做清理。

### 0.7 HTTP 503——"还没准备好"的信号

※ 概念基础和姊妹篇 `get-checkpointer-deep-dive.md` §0.7 完全一致，本篇只复用结论。

`_require` 工厂取不到 `app.state.run_event_store` 时就抛 503（`deps.py:357`）。语义是："这个进程的事件存储还没建好（或建失败了），请稍后再试。" 这比让代码在 NoneType 上深层崩成 500 友好得多——后面麻烦 1 会展开。

### 0.8 多租户 / `user_id` 隔离——同一张表里多个用户的数据

本篇独有的概念（姊妹篇没怎么提）。DeerFlow 支持多用户：A 用户的消息不该被 B 用户查到。这个隔离在 RunEventStore 这一层是怎么做的？

每条 event 在数据库里还带一个 `user_id` 列（`persistence/models/run_event.py:22`）：

```python
user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
```

- **写入时**：从当前请求的"用户上下文"里软读 `user_id`，盖在 event 上（这条是谁的事件）。
- **查询时**：`list_messages` 等读方法自动加一个 `WHERE user_id = <当前用户>` 过滤，保证只返回当前用户的事件。

这里有个微妙的设计——怎么区分"调用者**没传** user_id"和"调用者**显式说**不要过滤"？用 `None` 没法区分（"没传"和"传了 None"在 Python 里都是 None）。DeerFlow 用了一个**哨兵值** `AUTO`（`deerflow/runtime/user_context.py`）：

> *`_AUTO` (module-private sentinel, default): read from contextvar; raise `RuntimeError` if unset.*
> *Explicit `str`: use the provided value, overriding contextvar.*
> *Explicit `None`: no WHERE clause — used only by migration scripts and admin CLIs that intentionally bypass isolation.*
>
> 翻译：`_AUTO`（模块私有哨兵，默认值）→ 从 contextvar 读；读不到就抛 RuntimeError。显式传 `str` → 用这个值，覆盖 contextvar。显式传 `None` → 不加 WHERE 过滤——只给迁移脚本和管理 CLI 这种"故意绕过隔离"的场景用。

所以 `user_id` 参数其实是**三态**的：`AUTO`（自动读）/ `str`（显式指定）/ `None`（故意不过滤）。这个三态语义后面麻烦 5 会细讲。

注意：**只有 db 后端做 user_id 隔离**（memory 后端单租户，不隔离；jsonl 后端按 thread 分文件，靠 thread 隔离）。这也是三后端行为差异之一。

### 0.9 worker / `GATEWAY_WORKERS`——进程数

※ 概念基础和姊妹篇重叠，这里聚焦在 RunEventStore 三个后端对多进程的不同表现。

部署开多 worker（`GATEWAY_WORKERS > 1`）时，每个 worker 有自己独立的 `app.state.run_event_store`。三个后端在多 worker 下的表现天差地别：

- **db 后端**：每个 worker 持有自己的 session_factory，但都连到**同一个 Postgres**。seq 由数据库的锁保证全局单调——**多 worker 安全**。
- **memory 后端**：每个 worker 一个独立的内存 dict，**互相看不到**。A worker 写的事件，B worker 查不到。**多 worker 下数据分裂**，只适合开发/测试。
- **jsonl 后端**：每个 worker 往**同一个目录**写文件，但 seq 计数器是**进程内存里的**——两个 worker 各自从内存里数 seq，必然撞号。源码 docstring（`jsonl.py:9-12`）**明确写了这是硬限制**：

  > *Single-process guarantee: the in-memory seq counter is process-local. Multi-process deployments sharing the same directory will produce duplicate or non-monotonic seq values. Use `DbRunEventStore` for multi-process or high-concurrency deployments.*
  >
  > 翻译：单进程保证——seq 计数器是进程局部的。多进程共享同一个目录会产生重复或非单调的 seq 值。多进程或高并发部署请用 `DbRunEventStore`。

这就给三个后端画了清晰的能力边界：**只有 db 后端能上多 worker 生产环境**——后面麻烦 4、麻烦 6 会反复回到这一点。

---

好，前置概念讲完了。现在我们开始讲代码。

## 第 1 步：朴素写法长什么样

如果没有 RunEventStore，一个"展示某 thread 历史消息"的路由大概会写成这样（**这是反面教材，真实代码不是这样**）：

```python
@router.get("/{thread_id}/messages")
async def list_messages(thread_id: str):
    # 错误写法 A：只从还没跑完的 SSE 流里捞
    events = stream_bridge.recent_events(thread_id)   # 流是"活"的，跑完就没了
    return [e for e in events if e["category"] == "message"]
```

或者：

```python
@router.get("/{thread_id}/messages")
async def list_messages(thread_id: str):
    # 错误写法 B：每次请求现 new 一个空的内存存储
    store = MemoryRunEventStore()                      # 空的，啥也没有
    return await store.list_messages(thread_id)        # 永远返回空列表
```

逐行解释：

- **写法 A 第 3 行** `events = stream_bridge.recent_events(thread_id)`：试图从 StreamBridge（姊妹篇那个实时广播总线）里捞历史事件。**但 StreamBridge 根本不存历史**——它的职责是"事件还在跑时广播给订阅者"，run 一结束、或者进程一重启，流里的事件就全清了。用户刷新页面（这时 run 早就跑完了），从这个流里捞到的是**空的**。
- **写法 B 第 3 行** `store = MemoryRunEventStore()`：每个请求 new 一个全新的、空的内存存储。这个 store 里当然什么事件都没有——**永远返回空列表**。

**这两种写法的共同病根**：**把"事件流（实时）"当成了"事件历史（可查询）"，没有给 run 产出的 event 准备一个能持久化、能按 thread 单调排序、能被后续任意请求查询的存储。**

它派生的具体症状：

- 用户刷新页面 → 历史消息全没（流早就清了 / 内存 store 是空的）。
- 开发者想调试"昨天那次 run 到底发生了什么" → 无处可查。
- 想做分页（"看更早的消息"）→ 没有 seq 这种全局单调的游标，没法分。
- 多用户 → 没法按 user_id 隔离，A 能看到 B 的消息。
- 进程重启 → 历史全丢。

下面 6 个麻烦，全都是从"event 没有一个像样的历史存储"这一个病根派生的。`get_run_event_store(request)` + `RunEventStore` 抽象 + 三个后端 + 启动时建一次，就是来拔这个病根的。

---

## 第 2 步：6 个麻烦逐一展开

### 麻烦 1：刷新页面要看到历史消息，进程重启后历史也不能丢

#### 场景

用户在聊天框发了一句话，agent 跑完回复了。用户**关掉浏览器去吃午饭**，下午回来打开页面——前端发 `GET /threads/{tid}/messages`，要展示这段对话的**历史消息**（用户说了啥、agent 回了啥）。

更极端：进程**重启过了**（发版、崩溃恢复）。用户回来一看，历史消息**全没了**——昨天的对话像没发生过一样。

#### 朴素写法为什么做不到

回到第 1 步：写法 A 从 StreamBridge 捞，但流早清了；写法 B new 个空 store，啥也没有。**两个都没把事件落进一个能跨请求、跨重启存活的地方。**

#### DeerFlow 怎么解决的：进程级单例 + 启动时建一次 + `_require` 兜底

DeerFlow 用一个**进程级单例** `RunEventStore`，在应用启动时建一次，所有请求共用。这就是本文主角那一行 `get_run_event_store(request)` 取回来的东西。它建在哪里？看 lifespan 里的安装代码（`deps.py:299-305`）：

```python
# Run event store. The store and the matching ``run_events_config`` are
# both frozen at startup so ``get_run_context`` does not combine a
# freshly-reloaded ``AppConfig.run_events`` with a store still bound to
# the previous backend.
run_events_config = getattr(config, "run_events", None)
app.state.run_events_config = run_events_config
app.state.run_event_store = make_run_event_store(run_events_config)   # ★ 建一次，放进工具箱
```

逐行：

- `run_events_config = getattr(config, "run_events", None)` 从启动配置里取 `run_events:` 段（决定用哪个后端，后面麻烦 6 讲）。注意：这里取的是**启动时的配置快照**，不是每次请求现读的配置——这点很关键，注释里明说了"两者都在启动时冻结"。
- `app.state.run_events_config = run_events_config` 把这个配置快照也挂到 `app.state` 上（和 store 成对冻结，原因见麻烦 6）。
- `app.state.run_event_store = make_run_event_store(run_events_config)` ★ 调工厂建一个 store，挂到 `app.state`。**注意是直接赋值，没走 `AsyncExitStack`**（§0.6 讲过原因：memory/jsonl 不占重资源，db 共享引擎的连接池）。

这一行在应用启动时跑一次。之后每个请求的 `get_run_event_store(request)` 拿到的都是**同一个实例**。

`get_run_event_store(request)` 本身怎么工作？它就是 `_require` 工厂生成的一个闭包（`deps.py:351-361`）。`_require` 的逐行讲解姊妹篇 `get-checkpointer-deep-dive.md` 麻烦 6 已经讲透了，本篇只复用结论：**它从 `app.state.run_event_store` 取值，取不到就抛 503。**

```python
def _require(attr: str, label: str) -> Callable[[Request], T]:
    def dep(request: Request) -> T:
        val = getattr(request.app.state, attr, None)        # 从工具箱取
        if val is None:
            raise HTTPException(status_code=503, detail=f"{label} not available")  # 取不到就 503
        return cast(T, val)
    dep.__name__ = dep.__qualname__ = f"get_{attr}"
    return dep
```

对 `get_run_event_store` 这一行（`deps.py:367`），`attr="run_event_store"`、`label="Run event store"`。所以取不到时抛的 503 是 `"Run event store not available"`——又早又准的信号。

#### 取回来怎么用——查历史消息

`get_run_event_store(request)` 拿到的 store，最终被路由这么用。看 `list_thread_messages` 端点（`backend/app/gateway/routers/thread_runs.py:707-718`，精简）：

```python
@router.get("/{thread_id}/messages")
async def list_thread_messages(thread_id, request, limit=50, before_seq=None, after_seq=None):
    event_store = get_run_event_store(request)                    # ★ 取进程级单例
    rows = await event_store.list_messages(                       # 查历史消息
        thread_id, limit=limit, before_seq=before_seq, after_seq=after_seq)
    return rows
```

`event_store.list_messages(thread_id, ...)` 就是去 store 里查"这个 thread 的历史消息"。因为 store 是进程级单例、且（db/jsonl 后端）连的是进程外的持久存储，所以**用户下午回来、甚至进程重启过**，这里都能查到完整的历史。

朴素写法**没有这个进程级单例、没有持久后端**，刷新就空、重启就没。DeerFlow 用"启动时建一次 + 进程内共享 + 持久后端"彻底解决。

---

### 麻烦 2：seq 必须按 thread 全局单调（不能每个 run 各自从 1 数）

#### 场景

§0.4 讲过：seq 必须**在同一个 thread 内严格递增**，跨 run 串在一条时间轴上。为什么？因为前端展示历史消息时是按 thread 整体翻页的——run A 的两条消息和 run B 的两条消息必须能正确排序，还原对话的先后。

如果每个 run 各自从 1 开始数 seq，那同一个 thread 里就会出现多条 seq=1 的消息，前端按 seq 排序时就乱套了（到底哪条 seq=1 在前？）。

#### 朴素写法为什么做不到

朴素写法（每个请求 new 个空 store）根本没机会撞这个问题——但即便你用一个共享的 store，如果 seq 是按 `(thread_id, run_id)` 组合分配的（每个 run 从 1 数），就会踩上面那个坑。**seq 的取值范围必须是 thread 级的，不是 run 级的。**

#### DeerFlow 怎么解决的：seq 计数器按 thread_id 维护

看 `RunEventStore` 接口契约（`base.py:20-28`）——这 5 条是所有后端必须遵守的"合同"：

```python
class RunEventStore(abc.ABC):
    """Run event stream storage interface.

    All implementations must guarantee:
    1. put() events are retrievable in subsequent queries
    2. seq is strictly increasing within the same thread          # ← 第 2 条就是本麻烦的根
    3. list_messages() only returns category="message" events
    4. list_events() returns all events for the specified run
    5. Returned dicts match the RunEvent field structure
    """
```

第 2 条白纸黑字："seq 在同一个 thread 内严格递增。" 这是接口层就钉死的约定，三个后端都得实现。

具体怎么实现？看 memory 后端的 seq 分配（`memory.py:31-37`）：

```python
self._seq_counters: dict[str, int] = {}  # thread_id -> last assigned seq

def _next_seq(self, thread_id: str) -> int:
    current = self._seq_counters.get(thread_id, 0)
    next_val = current + 1
    self._seq_counters[thread_id] = next_val
    return next_val
```

逐行：

- `self._seq_counters: dict[str, int] = {}` 一张表，**key 是 thread_id，value 是这个 thread 当前已分配的最大 seq**。注意 key **只有 thread_id**，没有 run_id——这就是"seq 按 thread 取值范围"的体现。
- `current = self._seq_counters.get(thread_id, 0)` 取这个 thread 当前的 max seq（没分配过就是 0）。
- `next_val = current + 1` 加 1。
- `self._seq_counters[thread_id] = next_val` 写回。
- `return next_val` 返回新 seq。

效果：不管这条 event 属于哪个 run，只要它写在同一个 thread 下，seq 就**接着这个 thread 的上一次往后数**。run A 写到 seq=2，run B 接着写就是 seq=3、4…… 完美串成一条时间轴。

⚠️ 但这里有个**并发漏洞**：`current = ...get(...)` 和 `self._seq_counters[thread_id] = next_val` 之间如果被别的协程插进来，两个协程会拿到相同的 `current`，算出相同的 `next_val`——**seq 撞号**。memory 后端靠"单进程单事件循环、没有真正的并行"侥幸躲过（同一时刻只有一个协程在跑）；但 db 后端在多进程下就得用锁来堵这个洞——那就是麻烦 4 的主角。

朴素写法要么根本不分配 seq，要么按 run 分配——前者没法排序，后者会撞号。DeerFlow 用"thread 级计数器"钉死了 seq 的语义，后面麻烦 4 再补上并发安全。

---

### 麻烦 3：消息和轨迹混在一起，怎么分别查（category + 四张投影）

#### 场景

§0.3 讲过：同一个存储里既有给用户看的 `message`、也有给开发者看的 `trace`。前端查历史消息时只要 message，开发者调试时要全部。**怎么让这两种查询都高效？**

#### 朴素写法为什么做不到

最直观的写法：把所有 event 存一张表，查 message 时加个 `WHERE category='message'` 过滤。但这样**每次查消息都要扫这个 thread 的全部 event**（包括大量 trace），然后过滤——如果一次 run 产生了几百条 trace、几十条 message，查消息就要扫几百条再扔掉绝大部分，浪费。

#### DeerFlow 怎么解决的：四张投影 + bisect 二分

memory 后端用了**四张投影**（projection）——其实就是同一批 event dict 对象，**按不同的维度预先排好序、分组存好**，这样不同查询直接查对应的投影，O(log) 级就能定位。看构造函数（`memory.py:17-31`）：

```python
def __init__(self) -> None:
    self._events: dict[str, list[dict]] = {}        # thread_id -> seq 排序的全部 event
    # Messages-only projection of ``_events`` (same dict objects, no copies),
    # kept in seq order so message pagination is O(log m + page) via bisect
    # instead of re-scanning every event on each request.
    self._messages: dict[str, list[dict]] = {}      # thread_id -> seq 排序的 message 子集
    # Run-keyed projections of the two lists above (same dict objects, no
    # copies), kept in seq order. Per-run reads then cost O(events-in-run)
    # instead of O(events-in-thread): without these, ``list_events`` and
    # ``list_messages_by_run`` re-scan the whole thread's event log on every
    # request even though one run holds only a handful of events.
    self._events_by_run: dict[str, dict[str, list[dict]]] = {}      # thread_id -> run_id -> 该 run 的全部 event
    self._messages_by_run: dict[str, dict[str, list[dict]]] = {}    # thread_id -> run_id -> 该 run 的 message
    self._seq_counters: dict[str, int] = {}         # thread_id -> last assigned seq
```

逐行讲这四张投影：

- `self._events`——**全部 event**，按 thread_id 分组，每个 thread 内按 seq 排好序。这是"全集"。
- `self._messages`——**只有 message**（`category="message"`），同样按 thread 分组、按 seq 排序。注释强调"**same dict objects, no copies**"——它存的是 `_events` 里那些 message 类型 event 的**同一个 dict 对象的引用**，不是复制一份。所以加一条 message 时，`_events` 和 `_messages` 同时挂上同一个对象，不占双倍内存。
- `self._events_by_run`——**按 run 再分一层**。`_events` 是 thread 级的（一个 thread 下所有 run 混在一起），但有些查询是"**查某一次 run 的事件**"（比如 `GET /threads/{tid}/runs/{rid}/events`）。如果只有 thread 级的 `_events`，查某 run 就得**遍历这个 thread 全部 event 过滤 run_id**（O(整个 thread 的 event 数)）；有了这个按 run 的索引，直接按 `(thread_id, run_id)` 取（O(这个 run 的 event 数)）。注释把这点说得很清楚："Per-run reads then cost O(events-in-run) instead of O(events-in-thread)"。
- `self._messages_by_run`——**按 run 分的 message 子集**。同理，查"某 run 的消息"用这个。

**为什么要四张？** 因为有两种维度的查询交叉：**{全部 vs 仅 message} × {thread 级 vs run 级}** = 4 种组合，每种都对应一张投影。这样无论哪种查询，都能直接命中一张已经排好序、分好组的表，不用现场过滤。

写入时这四张怎么同步更新？看 `_put_one`（`memory.py:50-66`）：

```python
def _put_one(self, *, thread_id, run_id, event_type, category, content="", metadata=None, created_at=None) -> dict:
    seq = self._next_seq(thread_id)
    record = {... "seq": seq, ...}
    self._events.setdefault(thread_id, []).append(record)                          # ① 全集 +1
    self._events_by_run.setdefault(thread_id, {}).setdefault(run_id, []).append(record)  # ② run 级全集 +1
    if category == "message":
        self._messages.setdefault(thread_id, []).append(record)                    # ③ thread 级 message +1
        self._messages_by_run.setdefault(thread_id, {}).setdefault(run_id, []).append(record)  # ④ run 级 message +1
    return record
```

每写一条 event，往对应的投影里 append 同一个 record 对象。注意 `if category == "message"`——只有 message 类型的才进 `_messages` 和 `_messages_by_run`，trace 只进 `_events` 和 `_events_by_run`。这就是"按 category 分流"的体现。

#### 查询时——bisect 二分定位窗口

有了排好序的投影，查询就能用 `bisect` 二分快速定位。看 `list_messages`（`memory.py:96-111`）：

```python
async def list_messages(self, thread_id, *, limit=50, before_seq=None, after_seq=None, user_id=AUTO):
    # ``messages`` is messages-only and seq-sorted, so the seq window is a
    # contiguous slice located with bisect (O(log m)) rather than a full scan.
    messages = self._messages.get(thread_id, [])

    if before_seq is not None:
        # Records with seq < before_seq, then the last `limit` of them.
        hi = bisect.bisect_left(messages, before_seq, key=lambda e: e["seq"])
        return messages[max(0, hi - limit) : hi]                # ★ 切片，O(log m + limit)
    elif after_seq is not None:
        lo = bisect.bisect_right(messages, after_seq, key=lambda e: e["seq"])
        return messages[lo : lo + limit]
    else:
        return messages[-limit:]                                # 默认取最新 limit 条
```

逐行讲关键的：

- `messages = self._messages.get(thread_id, [])` 直接拿这个 thread 的 message 投影——**已经是 message-only、且 seq 排好序的**，不用现场过滤。
- `bisect.bisect_left(messages, before_seq, key=lambda e: e["seq"])` **二分查找**——在有序列表里找 `before_seq` 该插的位置。`bisect_left` 返回第一个 `>= before_seq` 的位置 `hi`，那么 `messages[:hi]` 就是所有 `seq < before_seq` 的（往回翻页用）。二分是 O(log m)，m 是这个 thread 的 message 数。
- `messages[max(0, hi - limit) : hi]` 从 `hi` 往前切 `limit` 条——这就是"比 before_seq 早的最新 limit 条"，对应"上一页"。
- `messages[-limit:]` 默认（没传游标）取最新 limit 条——对应"第一页"。

效果：**每次查消息的成本是 O(log m + limit)**——二分定位 O(log m)，切片取 limit 条 O(limit)。相比朴素写法"扫全部 event 再过滤"的 O(全部 event)，快了几个数量级。这就是四张投影的价值：**用写入时的少量额外开销（多 append 几个引用），换来查询时的指数级提速。**

朴素写法**每次查询都现场过滤全表**，慢且浪费。DeerFlow 用"预先分好的四张有序投影 + bisect"把查询做成了对数级。

---

### 麻烦 4：Db 后端怎么保证跨进程 seq 单调——双层锁

#### 场景

生产环境用 db 后端，开了多 worker（W1、W2、W3 都连同一个 Postgres）。现在 W1 和 W2 **几乎同时**往同一个 thread 写 event：

- W1 读到当前 max seq = 10，准备写 seq=11。
- W2 也几乎同时读到 max seq = 10（因为 W1 还没 INSERT 进去），也准备写 seq=11。
- 两个都 INSERT seq=11——**撞号了**。

这违背了接口契约第 2 条（seq 严格递增）。更糟的是，数据库表上还有个**唯一约束**（`persistence/models/run_event.py:32`）：

```python
UniqueConstraint("thread_id", "seq", name="uq_events_thread_seq"),
```

`(thread_id, seq)` 组合唯一——所以两个 worker 都写 seq=11 时，第二个 INSERT 会**撞唯一约束报错**，直接失败。这不只是序号乱，是写入直接挂掉。

#### 朴素写法为什么做不到

朴素写法（每个请求 new 个 store）连共享存储都没有，谈不上跨进程。就算用 db 后端，如果只是"先 SELECT max(seq)、再 INSERT"这种**先读后写**，中间必然有竞态窗口——麻烦 2 末尾提过的那个漏洞，在多进程下被放大成硬故障。

#### DeerFlow 怎么解决的：进程内 asyncio.Lock + 跨进程数据库锁（双层）

`DbRunEventStore` 用了**两层锁**来堵这个洞。先看构造函数（`db.py:26-43`）：

```python
class DbRunEventStore(RunEventStore):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, max_trace_content: int = 10240):
        self._sf = session_factory
        self._max_trace_content = max_trace_content
        # Per-thread asyncio locks serialize seq assignment for concurrent
        # in-process writers on the same thread. The DB-level FOR UPDATE /
        # advisory lock guards cross-process races; this guards the common
        # single-process case where two coroutines interleave between the
        # max(seq) read and the INSERT and would otherwise collide on seq.
        self._write_locks: dict[str, asyncio.Lock] = {}

    def _get_write_lock(self, thread_id: str) -> asyncio.Lock:
        """Return (creating if needed) the per-thread seq-assignment lock."""
        lock = self._write_locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._write_locks[thread_id] = lock
        return lock
```

逐行讲关键的：

- `self._write_locks: dict[str, asyncio.Lock] = {}` **第一层锁的容器**——按 thread_id 维护一把 `asyncio.Lock`。注意是**按 thread 分锁**，不是全局一把锁——这样不同 thread 的写互不阻塞（A thread 写不耽误 B thread 写），只有同一个 thread 的并发写才排队。
- 注释把两层锁的分工说得很清楚："Per-thread asyncio locks serialize seq assignment for concurrent **in-process** writers ... The DB-level FOR UPDATE / advisory lock guards **cross-process** races." —— **进程内的协程竞争，由 asyncio.Lock 堵；跨进程的 worker 竞争，由数据库锁堵。** 两层各管一摊。

为什么需要进程内这层锁？注释继续解释："this guards the common single-process case where two coroutines interleave between the max(seq) read and the INSERT and would otherwise collide on seq." —— 同一个进程里两个协程，一个读完 max(seq)=10 让出 CPU，另一个进来也读到 max(seq)=10，两个都算出 seq=11。**单进程多协程的交错，是 asyncio.Lock 的主场。**

#### 跨进程那层——为什么用 advisory lock 而不是 FOR UPDATE

进程内锁堵不住跨进程——W1 和 W2 是两个进程，各自的 asyncio.Lock 互不可见。跨进程得靠**数据库**这个共享裁判。看 `_max_seq_for_thread`（`db.py:107-127`）：

```python
@staticmethod
async def _max_seq_for_thread(session: AsyncSession, thread_id: str) -> int | None:
    """Return the current max seq while serializing writers per thread.

    PostgreSQL rejects ``SELECT max(...) FOR UPDATE`` because aggregate
    results are not lockable rows. As a release-safe workaround, take a
    transaction-level advisory lock keyed by thread_id before reading the
    aggregate. Other dialects keep the existing row-locking statement.
    """
    stmt = select(func.max(RunEventRow.seq)).where(RunEventRow.thread_id == thread_id)
    bind = session.get_bind()
    dialect_name = bind.dialect.name if bind is not None else ""

    if dialect_name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(CAST(:thread_id AS text))::bigint)"),
            {"thread_id": thread_id},
        )
        return await session.scalar(stmt)

    return await session.scalar(stmt.with_for_update())
```

逐行讲这段最精妙的：

- `stmt = select(func.max(RunEventRow.seq)).where(...)` 准备一个"读 max seq"的查询。**但这一步还不够**——光读不行，读完到 INSERT 之间有窗口。
- `if dialect_name == "postgresql":` Postgres 走一条特殊的路。
  - `await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(...))"))` ★ **事务级咨询锁**。`pg_advisory_xact_lock` 是 Postgres 提供的一种锁——它锁的不是某一行数据，而是一个**应用程序自定义的整数 key**。这里 key 是 `hashtext(thread_id)`（把 thread_id 哈希成一个 bigint）。**效果：同一个 thread_id 的所有写入，在数据库这一层被串行化**——W1 拿到这个锁才能读 max(seq) 和 INSERT，W2 必须等 W1 的事务提交（锁自动释放）才能进来。
  - docstring 解释了为什么不用 `SELECT ... FOR UPDATE`：*"PostgreSQL rejects `SELECT max(...) FOR UPDATE` because aggregate results are not lockable rows."* —— Postgres **拒绝**对聚合结果（`max(...)`）加行锁，因为聚合结果不是真实的行，没法锁。所以用 advisory lock 绕过这个限制。注释称这是个 "release-safe workaround"（发布级安全的绕过法）。
- `return await session.scalar(stmt.with_for_update())` 非 Postgres（比如 SQLite）走老路——`with_for_update()` 给查询加行锁。SQLite 没有真正的行锁，但单机 SQLite 本来就是单进程，进程内 asyncio.Lock 已经够了，这里只是接口上对齐。

#### 把两层锁串起来看——`put` 方法

完整的写入流程在 `put`（`db.py:129-158`）：

```python
async def put(self, *, thread_id, run_id, event_type, category, content="", metadata=None, created_at=None):
    """Write a single event — low-frequency path only.

    This opens a dedicated transaction with a FOR UPDATE lock to
    assign a monotonic *seq*.  For high-throughput writes use
    :meth:`put_batch`, which acquires the lock once for the whole
    batch.
    """
    content, metadata = self._truncate_trace(category, content, metadata)
    db_content, metadata = self._content_to_db(content, metadata)
    user_id = self._user_id_from_context()
    async with self._get_write_lock(thread_id):              # ★ 第一层：进程内 asyncio.Lock
        async with self._sf() as session:
            async with session.begin():                      # ★ 第二层：数据库事务（含 advisory lock）
                max_seq = await self._max_seq_for_thread(session, thread_id)
                seq = (max_seq or 0) + 1
                row = RunEventRow(thread_id=thread_id, ..., seq=seq, ...)
                session.add(row)
            return self._row_to_dict(row)
```

逐行讲两层锁怎么套：

- `async with self._get_write_lock(thread_id):` **第一层（进程内）**——拿这个 thread 的 asyncio.Lock。持有这把锁期间，本进程内别的协程想写同一个 thread，得在外面排队。这就堵住了"同进程两协程交错撞 seq"。
- `async with self._sf() as session: async with session.begin():` **第二层（数据库）**——开一个数据库事务。事务里调 `_max_seq_for_thread`（它内部会取 advisory lock），读 max seq、算新 seq、INSERT。整个事务是原子的——提交前别的进程进不来（advisory lock 挡着）。
- `seq = (max_seq or 0) + 1` 算新 seq。因为两层锁都在持着，这个 max_seq 一定是"当前真实的最大值"，不会读到别人还没提交的中间态。

**两层锁的关系是嵌套的、各管一摊**：进程内 asyncio.Lock 解决"同进程协程交错"，数据库 advisory lock 解决"跨进程 worker 竞争"。缺了任何一层都会漏：

- 只有 asyncio.Lock → 跨进程的 W1、W2 各自的锁互不可见，照样撞。
- 只有数据库锁 → 同进程两协程在"读 max"和"INSERT"之间交错，照样在数据库事务层面撞（虽然 advisory lock 会兜底让第二个等待，但效率低；进程内先串一遍更高效）。

#### `put_batch`——一次锁、批量写

高频写入不走 `put`（每次一条太贵），走 `put_batch`（`db.py:160-195`）：

```python
async def put_batch(self, events):
    if not events:
        return []
    thread_ids = {e["thread_id"] for e in events}
    if len(thread_ids) > 1:
        raise ValueError(f"put_batch requires all events to belong to the same thread; got {thread_ids!r}")
    user_id = self._user_id_from_context()
    thread_id = events[0]["thread_id"]
    async with self._get_write_lock(thread_id):              # 锁一次
        async with self._sf() as session:
            async with session.begin():
                max_seq = await self._max_seq_for_thread(session, thread_id)
                seq = max_seq or 0
                rows = []
                for e in events:
                    seq += 1                                  # 批量内顺序递增
                    ... session.add(row) ...
                return [self._row_to_dict(r) for r in rows]
```

关键区别：**锁只取一次，在锁内连续分配一批 seq**。注释（`put` 的 docstring）明说："For high-throughput writes use `put_batch`, which acquires the lock once for the whole batch."——`RunJournal`（事件收集器）攒一批 event 后一次性 flush，就是走这条高效路径。

注意那条校验：`if len(thread_ids) > 1: raise ValueError(...)`——**一个 batch 必须全属于同一个 thread**。为什么？因为 seq 是按 thread 分配的，一批 event 跨多个 thread 的话，没法用一把锁、一个 max_seq 统一分配。这是"seq 按 thread 取值范围"在 API 层的体现。

朴素写法**先读后写有竞态窗口、跨进程必撞唯一约束**。DeerFlow 用"进程内 asyncio.Lock + 数据库 advisory lock"的双层锁，把跨进程 seq 单调这件事管死了。

---

### 麻烦 5：多租户隔离——`user_id` 怎么读、怎么过滤

#### 场景

§0.8 讲过：多用户环境下，A 用户的事件不能被 B 查到。这要求：

- **写入时**：每条 event 盖上"这是谁的事件"（`user_id`）。
- **查询时**：自动加 `WHERE user_id = <当前用户>`，只返回当前用户的。

但这里有个微妙问题：**写入路径有两类调用者**，对 user_id 的处理不同：

- **HTTP 请求触发的写入**（比如用户发消息触发的 `human_message` 事件）——这时请求里带了用户身份，应该盖上 user_id。
- **后台 worker 的写入**（agent 跑的过程中产生的事件）——worker 跑在后台任务里，**可能没有用户上下文**（contextvar 没设置），这时不该报错，应该允许"无 user_id"写入（事后由迁移补）。

怎么区分"有用户上下文"和"没有"？又怎么区分"调用者没传 user_id"和"调用者显式说不要过滤"？

#### 朴素写法为什么做不到

朴素写法没有 user_id 概念——所有用户的数据混在一起，A 能查到 B 的消息。或者写死一个 user_id（比如永远不过滤）——失去了多租户能力。

#### DeerFlow 怎么解决的：`AUTO` 哨兵 + 软读 + 三态语义

§0.8 提过，`user_id` 参数是**三态**的，靠 `AUTO` 哨兵区分。看 `user_context.py` 模块 docstring（开头那段）对三态的权威定义：

> *`_AUTO` (module-private sentinel, default): read from contextvar; raise `RuntimeError` if unset.*
> *Explicit `str`: use the provided value, overriding contextvar.*
> *Explicit `None`: no WHERE clause — used only by migration scripts and admin CLIs that intentionally bypass isolation.*

翻译：

- **`AUTO`（默认）** → 从 contextvar（请求级的用户上下文）读。读不到就抛 `RuntimeError`（说明在不该没有用户的地方调了）。
- **显式传 `str`** → 用这个值，覆盖 contextvar。
- **显式传 `None`** → **不加 WHERE 过滤**——只给迁移脚本和管理 CLI 这种"故意要看全部数据"的场景用。

`AUTO` 是个哨兵对象（不是 None、不是字符串），它的存在让"我没传"（默认 AUTO）和"我传了 None"（显式不过滤）能被区分开。

#### 写入路径——软读

db 后端的写入用**软读**（`_user_id_from_context`，`db.py:89-105`）：

```python
@staticmethod
def _user_id_from_context() -> str | None:
    """Soft read of user_id from contextvar for write paths.

    Returns ``None`` (no filter / no stamp) if contextvar is unset,
    which is the expected case for background worker writes. HTTP
    request writes will have the contextvar set by auth middleware
    and get their user_id stamped automatically.

    Coerces ``user.id`` to ``str`` at the boundary: ``User.id`` is
    typed as ``UUID`` by the auth layer, but ``run_events.user_id``
    is ``VARCHAR(64)`` and aiosqlite cannot bind a raw UUID object
    to a VARCHAR column ("type 'UUID' is not supported") — the
    INSERT would silently roll back and the worker would hang.
    """
    user = get_current_user()
    return str(user.id) if user is not None else None
```

逐行讲关键的：

- `user = get_current_user()` 从 contextvar 读当前用户。**软读**——读不到返回 `None`，不抛错。这和"三态里的 AUTO"不同——AUTO 是查询路径用的（读不到要抛 RuntimeError）；**写入路径用软读（读不到返回 None）**，因为后台 worker 没用户上下文是正常情况，不能因此报错。
- `return str(user.id) if user is not None else None` 有用户就返回 `str(user.id)`，没有就返回 `None`（这条 event 不盖 user_id）。
- docstring 那段关于 `str()` 强转的解释**非常值得逐字看**：`User.id` 在 auth 层被定义成 `UUID` 类型，但数据库的 `run_events.user_id` 列是 `VARCHAR(64)`。**aiosqlite 没法把一个 UUID 对象绑定到 VARCHAR 列**（会报 "type 'UUID' is not supported"）——更阴的是，**INSERT 会静默回滚，worker 会挂住**。所以在边界上必须 `str()` 强转一下。这是个真实的坑被 docstring 记下来防止后人再踩。

#### 查询路径——`resolve_user_id` 应用过滤

db 后端的查询方法用 `resolve_user_id` 把三态翻译成 SQL。看 `list_messages`（`db.py:197-227`）：

```python
async def list_messages(self, thread_id, *, limit=50, before_seq=None, after_seq=None, user_id=AUTO):
    resolved_user_id = resolve_user_id(user_id, method_name="DbRunEventStore.list_messages")
    stmt = select(RunEventRow).where(RunEventRow.thread_id == thread_id, RunEventRow.category == "message")
    if resolved_user_id is not None:
        stmt = stmt.where(RunEventRow.user_id == resolved_user_id)      # ★ 加 user_id 过滤
    if before_seq is not None:
        stmt = stmt.where(RunEventRow.seq < before_seq)
    ...
```

逐行：

- `resolved_user_id = resolve_user_id(user_id, ...)` 把三态的 `user_id` 参数解析成"实际该用的 user_id 值"：如果传的是 AUTO，从 contextvar 读（读不到按 AUTO 语义抛 RuntimeError）；如果传 str，用它；如果传 None，返回 None。
- `if resolved_user_id is not None: stmt = stmt.where(RunEventRow.user_id == resolved_user_id)` ★ **只有解析出来的 user_id 不是 None，才加 WHERE 过滤**。如果是 None（显式传 None，或老数据没盖 user_id），不加过滤——返回全部。这就是"显式 None = 绕过隔离"的体现。
- `RunEventRow.category == "message"` 顺带也做了 category 过滤（呼应麻烦 3）。

注意 db 后端和 memory 后端在 user_id 上的差异：

- **db 后端**：按 user_id 过滤（多租户隔离）。读路径靠 `resolve_user_id`，写路径靠 `_user_id_from_context` 软读。
- **memory 后端**：**不隔离**（看 `memory.py` 的 `list_messages`，`user_id` 参数收了但没用）。因为 memory 后端默认是单租户开发场景，不搞隔离。
- **jsonl 后端**：按 thread 分文件，靠"不同用户的 thread_id 不同"隐式隔离。

朴素写法**没有 user_id、所有用户数据混在一起**。DeerFlow 用"`AUTO` 哨兵 + 三态语义 + db 后端的软读/过滤"把多租户隔离做出来了，同时照顾了"后台 worker 无用户上下文"这个现实。

---

### 麻烦 6：Jsonl 单进程限制 + 路径安全 + 后端选择冻结

#### 场景

最后这个麻烦把几个散落的小问题收尾——它们都和"后端选择 + 配置冻结"有关：

1. jsonl 后端是个轻量的"文件存储"，但有**单进程硬限制**——多 worker 下 seq 会撞。这条限制在哪、为什么？
2. jsonl 后端把 thread_id/run_id 拼进文件路径，怎么防止**路径穿越攻击**（用户传个 `../../etc/passwd` 当 thread_id）？
3. **后端选择依赖配置，且配置和 store 必须成对冻结**——为什么不能让 store 用旧后端、配置却跟着热重载变了？

#### 朴素写法为什么做不到

朴素写法后端写死（永远用 memory），没有"换后端"的概念；也没有路径校验（直接拼字符串做路径，会被穿越）；更没有"配置和实例一致性"的考虑。

#### DeerFlow 怎么解决的

##### (1) Jsonl 的单进程硬限制

jsonl 后端的 docstring（`jsonl.py:9-12`）开篇就明说这是硬限制：

> *Single-process guarantee: the in-memory seq counter is process-local. Multi-process deployments sharing the same directory will produce duplicate or non-monotonic seq values. Use `DbRunEventStore` for multi-process or high-concurrency deployments.*

翻译：**单进程保证**——seq 计数器是进程局部的内存变量。多进程共享同一个目录会产生重复或非单调的 seq 值。多进程或高并发部署请用 `DbRunEventStore`。

为什么 jsonl 撞号而 db 不撞？看 jsonl 的 seq 分配（`jsonl.py:66-68`）：

```python
def _next_seq(self, thread_id: str) -> int:
    self._seq_counters[thread_id] = self._seq_counters.get(thread_id, 0) + 1
    return self._seq_counters[thread_id]
```

`self._seq_counters` 是**进程内存里的 dict**——W1 和 W2 各有各的 dict，互相看不到。W1 数到 10，W2 自己的 dict 里可能还是 0，于是 W2 也从 1 开始数——撞号。

而且 jsonl 后端**没有数据库那种 advisory lock 兜底**（它只是往文件 append），所以多进程下撞号是真撞、没人拦。这就是为什么 docstring 把它定位成"**lightweight single-node deployments**"——单机轻量部署，别上多 worker。

##### (2) Jsonl 怎么不堵事件循环 + 怎么防路径穿越

jsonl 后端有两个值得讲的小机制。

**不堵事件循环**——文件 I/O 是同步阻塞的，直接在 async 函数里 `open()` 会卡住整个事件循环。jsonl 后端用 `asyncio.to_thread` 把 I/O 丢到线程池。docstring（`jsonl.py:14-16`）：

> *File I/O is offloaded to a thread pool via `asyncio.to_thread` so the event loop is never blocked. Per-thread `asyncio.Lock` objects serialise writes within a single process to prevent interleaved JSONL lines.*

看 `put`（`jsonl.py:141-156`）：

```python
async def put(self, *, thread_id, run_id, event_type, category, content="", metadata=None, created_at=None):
    async with self._get_write_lock(thread_id):
        await self._ensure_seq_loaded(thread_id)
        seq = self._next_seq(thread_id)
        record = {...}
        await asyncio.to_thread(self._write_record, record)      # ★ I/O 丢线程池
        return record
```

`await asyncio.to_thread(self._write_record, record)` 把真正写文件的同步操作丢到线程池，事件循环不被阻塞。

**防路径穿越**——thread_id 和 run_id 会被拼进文件路径（`.deer-flow/threads/{thread_id}/runs/{run_id}.jsonl`）。如果用户恶意传个 `../../etc/passwd` 当 thread_id，路径就穿越了。jsonl 后端用正则白名单校验（`jsonl.py:38, 51-56`）：

```python
_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+$")

@staticmethod
def _validate_id(value: str, label: str) -> str:
    """Validate that an ID is safe for use in filesystem paths."""
    if not value or not _SAFE_ID_PATTERN.match(value):
        raise ValueError(f"Invalid {label}: must be alphanumeric/dash/underscore, got {value!r}")
    return value
```

逐行：

- `_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+$")` 一个正则——**只允许字母、数字、下划线、短横线**。`.` 和 `/` 都不在白名单里，所以 `../` 这种穿越片段直接被拒。
- `_validate_id` 把 ID 校验一遍，不合法就抛 `ValueError`。`_thread_dir` 和 `_run_file`（`jsonl.py:58-64`）在拼路径前都先调它校验。

这是个容易忽视但很重要的安全细节——只要 ID 被拼进文件系统路径，就得校验。

##### (3) `make_run_event_store` 三路分支 + db 后端 fallback

后端选择由 `config.yaml` 的 `run_events.backend` 决定（`run_events_config.py:21-33`，整个配置类）：

```python
class RunEventsConfig(BaseModel):
    backend: Literal["memory", "db", "jsonl"] = Field(
        default="memory",
        description="Storage backend for run events. 'memory' for development (no persistence), 'db' for production (SQL queries), 'jsonl' for lightweight single-node persistence.",
    )
    max_trace_content: int = Field(default=10240, ...)   # trace 内容截断阈值（db 专用）
    track_token_usage: bool = Field(default=True, ...)
```

工厂 `make_run_event_store`（`store/__init__.py:5-23`）按这个字段三路分支：

```python
def make_run_event_store(config=None) -> RunEventStore:
    """Create a RunEventStore based on run_events.backend configuration."""
    if config is None or config.backend == "memory":
        return MemoryRunEventStore()                              # ① memory 或没配 → 内存
    if config.backend == "db":
        from deerflow.persistence.engine import get_session_factory
        sf = get_session_factory()
        if sf is None:
            # database.backend=memory but run_events.backend=db -> fallback
            return MemoryRunEventStore()                          # ② 想用 db 但没引擎 → 退回内存
        from deerflow.runtime.events.store.db import DbRunEventStore
        return DbRunEventStore(sf, max_trace_content=config.max_trace_content)
    if config.backend == "jsonl":
        from deerflow.runtime.events.store.jsonl import JsonlRunEventStore
        return JsonlRunEventStore()                               # ③ jsonl → 文件
    raise ValueError(f"Unknown run_events backend: {config.backend!r}")   # ④ 别的 → 报错
```

逐行：

- `if config is None or config.backend == "memory":` ① 没配或显式 memory → `MemoryRunEventStore()`。这是默认/开发兜底。
- `if config.backend == "db":` ② 想用 db。但有个 fallback：`sf = get_session_factory()`，如果 `sf is None`（即 `database.backend` 是 memory，根本没建 SQL 引擎），**退回 `MemoryRunEventStore()`**。注释说得很清楚："database.backend=memory but run_events.backend=db -> fallback"——你让事件存 db，但持久化引擎本身是 memory（没数据库），那事件也只能退回内存。这是个优雅的降级。
- `if config.backend == "jsonl":` ③ jsonl → `JsonlRunEventStore()`。
- `raise ValueError(...)` ④ 配了个不认识的后端名 → 直接报错（fail fast，不让进程带着错误配置起来）。

注意三路最终都返回一个 `RunEventStore`（同一个抽象接口），**调用方根本不用关心后端是啥**。`get_run_event_store(request)` 拿到的类型是 `RunEventStore`，它可能是三个具体类之一——但路由代码只调 `list_messages`/`list_events` 等接口方法，后端透明。这就是抽象接口（`base.py`）的价值。

##### (4) 配置和 store 成对冻结——为什么不能让配置热重载

最后讲一个微妙但重要的设计。回到 §0.5 提过的 lifespan 安装代码（`deps.py:299-305`），那里**同时冻结了 store 和 config**：

```python
run_events_config = getattr(config, "run_events", None)
app.state.run_events_config = run_events_config                # ★ config 冻结
app.state.run_event_store = make_run_event_store(run_events_config)  # ★ store 冻结（绑定到这个 config）
```

为什么两个都要冻结？想象一种坏情况：

- 启动时 `run_events.backend=db`，建了个 `DbRunEventStore`，绑到 Postgres。
- 运行中，运维改了 `config.yaml`，把 `run_events.backend` 改成 `memory`，触发热重载。
- 如果 `get_run_context`（`deps.py:406-424`）读的是**热重载后的新 config**（说 memory），但 store 还是**启动时建的那个 DbRunEventStore**（绑在 Postgres）——**config 和 store 对不上**。代码以为"现在用 memory"，实际数据在 Postgres 里，行为错乱。

`deps.py:299-302` 那段注释专门讲了这个不变量：

> *The store and the matching `run_events_config` are both frozen at startup so `get_run_context` does not combine a freshly-reloaded `AppConfig.run_events` with a store still bound to the previous backend.*

翻译：**store 和配套的 `run_events_config` 都在启动时冻结，这样 `get_run_context` 就不会把"刚热重载的 `AppConfig.run_events`"和"还绑着旧后端的 store"凑到一起。**

看 `get_run_context`（`deps.py:406-424`）怎么用这对冻结的值：

```python
def get_run_context(request: Request) -> RunContext:
    """Build a RunContext from app.state singletons.

    Returns a *base* context with infrastructure dependencies. The
    ``app_config`` field is resolved live so per-run fields (e.g.
    ``models[*].max_tokens``) follow ``config.yaml`` edits; the
    ``event_store`` / ``run_events_config`` pair stays frozen to the snapshot
    captured in :func:`langgraph_runtime` so callers never see a store bound
    to one backend paired with a config pointing at another.
    """
    return RunContext(
        checkpointer=get_checkpointer(request),
        store=get_store(request),
        event_store=get_run_event_store(request),                              # ★ 冻结的 store
        run_events_config=getattr(request.app.state, "run_events_config", None),  # ★ 冻结的 config（配套）
        ...
        app_config=get_config(),                                              # ← 这个才是 live 读的
        ...
    )
```

逐行讲关键的对比：

- `event_store=get_run_event_store(request)` 取**冻结的 store**（启动时建的那个）。
- `run_events_config=getattr(request.app.state, "run_events_config", None)` 取**冻结的 config**（启动时和 store 一起冻结的）。这两个是**配套**的——永远指向同一个后端。
- `app_config=get_config()` 这一行才是**live 读**当前 config 的——但只取"每次 run 会变的字段"（比如 `models[*].max_tokens`）。
- docstring 把这个区分讲透了：*"`app_config` field is resolved live so per-run fields follow config.yaml edits; the `event_store` / `run_events_config` pair stays frozen"* —— **per-run 的字段 live 读，基础设施（store）冻结。** 因为 per-run 字段改了不影响已建的 store（max_tokens 改了，store 还是那个 store）；但 backend 改了会影响（backend 变了，旧 store 就对不上新 config 了），所以 backend 类的字段必须冻结，改了要重启。

这个"哪些字段 live、哪些字段冻结"的边界，在 `reload_boundary.py:48` 有正式记录：

> *`run_events`: `make_run_event_store()` picks the memory- vs SQL-backed implementation at startup and is frozen onto `app.state.run_events_config` to stay paired with the underlying event store.*

翻译：`run_events` 字段——`make_run_event_store()` 在启动时选定 memory 或 SQL 后端实现，并冻结到 `app.state.run_events_config` 上，以保持和底层 event store 配对。属于"改了要重启"的字段。

朴素写法**后端写死、无路径校验、无配置一致性**。DeerFlow 用"jsonl 单进程硬限制的明确文档 + 路径白名单 + 工厂三路分支 + 配置/store 成对冻结"把这些收尾都做扎实了。

---

## 第 3 步：把 6 个麻烦的本质串起来

朴素写法（只往 SSE 流塞事件 / 每次请求 new 个空 store）的根本问题是：

> **把 event 当成了"一过性的实时流"，而没有给它准备一个能持久化、能按 thread 单调排序、能分页查询、能多租户隔离的历史存储。**

而现实里，event 的"实时消费"和"历史查询"是**完全不同的两种访问模式**（§0.2 讲过）：

| 维度 | 朴素写法（只 SSE / 每 request new） | 真实写法（RunEventStore 进程级单例 + 后端） |
|---|---|---|
| **寿命** | 流跑完就清 / 函数返回就 GC | 整个进程生命周期；db/jsonl 后端**跨重启存活** |
| **seq 单调** | 不分配，或按 run 分配（撞号） | thread 级单调（接口契约第 2 条）+ 双层锁保证（db） |
| **消息 vs 轨迹** | 混在一起，查消息要扫全表 | category 分流 + 四张投影，查消息 O(log m + page) |
| **跨进程** | 各进程各跑各的，互不可见 | db 后端共享 Postgres，advisory lock 保证全局单调 |
| **多租户** | 没有，所有用户数据混在一起 | db 后端按 user_id 过滤；AUTO 哨兵支持三态语义 |
| **后端可换** | 写死，换后端要发版 | 看 `config.yaml`，工厂三路分支 + 统一接口 |
| **配置一致性** | 不考虑 | store 和 config 成对冻结，防热重载错配 |

把 event 和"可查询的历史"割裂（只走 SSE），就会出现 6 个麻烦——因为现实里 event **就是需要被反复查询、分页、审计**：

- ① 刷新页面要看到历史，进程重启历史不能丢 → 进程级单例 + 启动时建一次（`make_run_event_store` + `app.state`）
- ② seq 必须按 thread 全局单调 → 接口契约钉死 + thread 级计数器
- ③ 消息和轨迹混在一起要分别查 → category 字段 + 四张有序投影 + bisect
- ④ 多进程下 seq 不能撞 → db 后端的 asyncio.Lock + 数据库 advisory lock 双层锁
- ⑤ 多用户要隔离 → `AUTO` 哨兵三态语义 + db 后端软读/过滤
- ⑥ jsonl 单进程限制 + 路径安全 + 后端选择冻结 → docstring 明确限制 + 路径白名单 + 工厂三路 + 配置/store 成对冻结

`get_run_event_store(request)` + `RunEventStore` 抽象 + 三后端实现，就是**把"event 的历史"从"event 的实时流"里彻底分离出来**的那个中间层。它具体做的事：

- **进程级单例**：让 store 活过单次请求，所有请求共用一个实例（麻烦 1）。
- **thread 级 seq**：让 event 有一条统一的时间轴，可排序可分页（麻烦 2、3）。
- **双后端高效查询**：memory 用四张投影做到 O(log) 查询；db 用 SQL 索引（`ix_events_thread_cat_seq`、`ix_events_run`，见 `run_event.py:33-34`）。
- **跨进程安全**：db 后端用双层锁保证多 worker 下 seq 单调（麻烦 4）。
- **多租户隔离**：db 后端按 user_id 过滤，三态参数照顾后台 worker（麻烦 5）。
- **可配置 + 一致性**：三后端可换，配置和 store 成对冻结（麻烦 6）。

代码上，这一切都从那一行开始：

```python
get_run_event_store: Callable[[Request], RunEventStore] = _require("run_event_store", "Run event store")
```

这一行本身只是"注册一个取依赖的函数"；但它背后牵着一整条链——`app.py` 的 lifespan → `deps.py:222` 的 `langgraph_runtime` → `deps.py:299-305` 把 `make_run_event_store(config)` 的产出冻结到 `app.state.run_event_store`（连同 `run_events_config` 成对冻结）→ 每个请求里 `get_run_event_store(request)` 取出来 → 下游路由 `thread_runs.py:718` 的 `list_messages` / `list_events` / `list_messages_by_run` 用它查历史。一张全景图：

```
                    ┌─────────────────────────────────────────────────────────────┐
                    │            进程启动（lifespan，app.py）                      │
                    │                                                             │
                    │  langgraph_runtime（deps.py:222）                           │
                    │    │                                                        │
                    │    │  init_engine_from_config()  ← 共享 SQL 引擎（db 后端用）│
                    │    │                                                        │
                    │    │  run_events_config = config.run_events  ← 冻结快照     │
                    │    │  app.state.run_events_config = run_events_config        │
                    │    │  app.state.run_event_store =                           │
                    │    │       make_run_event_store(run_events_config)          │
                    │    │                  │                                     │
                    │    │                  ▼                                      │
                    │    │       [MemoryRunEventStore /                           │
                    │    │        DbRunEventStore(sf) /                           │
                    │    │        JsonlRunEventStore()]  ← 三后端之一             │
                    │    │                                                        │
                    │    │  yield  ← 应用开始接请求                                │
                    │    └─────────────────────────────────────────────────────── │
                    └─────────────────────────────────────────────────────────────┘
                                          │ app.state.run_event_store（进程级工具箱）
                                          ▼
                    ┌─────────────────────────────────────────────────────────────┐
                    │                  每次 HTTP 请求                              │
                    │                                                             │
                    │  event_store = get_run_event_store(request)                 │
                    │       │  ← _require 工厂：getattr app.state，取不到抛 503    │
                    │       │     （麻烦1）                                        │
                    │       ▼                                                     │
                    │  await event_store.list_messages(thread_id, ...)  ── 查历史 │
                    │  await event_store.list_events(thread_id, run_id) ── 查轨迹 │
                    │  await event_store.list_messages_by_run(...)      ── 按run  │
                    │  （thread_runs.py:718 等）                                  │
                    │                                                             │
                    │  另外：get_run_context(request) 把同一个 store 包进          │
                    │  RunContext.event_store，交给后台 worker 的 RunJournal 写    │
                    └─────────────────────────────────────────────────────────────┘
                                          │
                            （写入路径，不在本篇路由里）
                                          ▼
                    ┌─────────────────────────────────────────────────────────────┐
                    │       后台 worker（agent 跑的过程）                         │
                    │                                                             │
                    │  RunJournal 攒一批 event → event_store.put_batch(...)       │
                    │       │                                                     │
                    │       ├─ memory: 直接 append 进四张投影                     │
                    │       ├─ db: 双层锁 + advisory lock 分配 seq + INSERT       │
                    │       └─ jsonl: asyncio.to_thread 写 .jsonl 文件            │
                    │                                                             │
                    │  （同一批 event 同时也推给 StreamBridge 给实时订阅者）       │
                    └─────────────────────────────────────────────────────────────┘
```

所以它不是"代码好看"或"封装一下"——是这 6 个麻烦里**任意一个**单拿出来，朴素写法都过不去。

它和姊妹篇的 `get_stream_bridge` / `get_run_manager` / `get_checkpointer` 是同一类东西的四面：

- **`get_stream_bridge`**（`deps.py:364`）解耦**实时事件流**的生命周期——让 run 的事件能被任意数量、任意时刻、跨进程的订阅者实时消费（活流）。
- **`get_run_manager`**（`deps.py:365`）解耦**执行 + 状态**的生命周期——让 run 能后台跑、可查可取消、可恢复、崩溃能收尸。
- **`get_checkpointer`**（`deps.py:366`）解耦**存档（持久化）**的生命周期——让 agent 的 checkpoint 有正确的数据库后端、正确的开关机顺序、正确的多进程语义。
- **`get_run_event_store`**（`deps.py:367`，本篇）解耦**事件历史（可查询）**的生命周期——让 run 产出的 event 有一个能持久化、按 thread 单调排序、可分页、可隔离、可审计的历史存储（死档）。

四个紧挨着写在 `deps.py:364-367`，是因为它们是同一类东西：**"启动时建一次、放进 `app.state`、每个请求取一次、取不到就 503"的进程级运行时单例**。理解了其中一个，另几个就是同一套机制的换皮——但每一个都解决了一类独特的、不可替代的生命周期问题。

特别要记住 `get_stream_bridge` 和 `get_run_event_store` 这一对的区分（最容易搞混）：

- **两者存的是同一批 event 的两份副本**——写入时 RunJournal 同时往两边送。
- **StreamBridge 服务"实时访问"**——事件还在跑时怎么被多方订阅（push，活流，不持久）。
- **RunEventStore 服务"历史访问"**——事件跑完后怎么被查询、分页、审计（pull，死档，db/jsonl 持久）。

一个管"现在正在发生什么"，一个管"曾经发生过什么"。前者是订阅，后者是查询。这就是 `event_store = get_run_event_store(request)` 存在的全部理由。
