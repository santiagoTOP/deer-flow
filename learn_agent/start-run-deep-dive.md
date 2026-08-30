# `start_run` 深度讲解：为什么"创建一个 run"不是路由里一句话，而要一个编排函数

本文是 `run-manager-deep-dive.md` 和 `stream-bridge-deep-dive.md` 的姊妹篇。那两篇讲的是 `start_run` 里面用到的两个单例——`run_mgr = get_run_manager(request)`（执行 + 状态）和 `bridge = get_stream_bridge(request)`（事件流）——为什么存在；本篇讲的是紧挨着它们的那一行：

```python
record = await start_run(body, thread_id, request)
```

——这行在 **6 个地方**被调用（三个 thread-scoped 路由、两个 stateless 路由、一个调度器入口），是整个 Gateway 里"把一次 HTTP 请求变成一次后台 agent run"的唯一闸门。

三篇文章回答的是同一个大问题的三个面：**一次 HTTP 请求里启动的 agent run，凭什么能活得比这次请求久？**

- `stream-bridge` 负责"事件流"这一半——agent 产出的 token 要能被任意数量、任意时刻、跨进程的订阅者消费。
- `run-manager` 负责"执行 + 状态"这一半——run 要活过创建请求、跨重启可查、跨 worker 可协调、能被干净取消和收尸。
- **本文负责"启动"这一半**——从"路由收到请求"到"`asyncio.create_task(run_agent(...))` 把协程派出去"之间，到底发生了什么、为什么不能少。

本文面向第一次接触这套架构的同学。先把所有需要的前置概念一个一个铺出来，然后再讲 6 个麻烦，每段代码逐行解释。已经读过姊妹篇的同学可以快速跳过带 ※ 标记的小节。

---

## 第 0 步：先把要用的术语讲清楚

### 0.1 三段架构：路由（薄）→ 服务（编排）→ runtime（执行）

DeerFlow 的 Gateway 后端分三层，理解这个分层是理解"为什么有 `start_run`"的前提：

| 层 | 在哪 | 职责 | 厚薄 |
|---|---|---|---|
| **路由层** | `backend/app/gateway/routers/thread_runs.py`、`routers/runs.py` | 接 HTTP 请求、鉴权装饰器、决定"响应长什么样"（立即 JSON / 内联 SSE / 阻塞） | **薄**——每个 handler 往往就三四行 |
| **服务层（编排）** | `backend/app/gateway/services.py` | **横切关注点集中处理**：鉴权归属、模型白名单、并发策略、thread 元信息、config 加固、派发后台 task | **厚**——`start_run` 就在这 |
| **runtime 层（执行）** | `backend/packages/harness/deerflow/runtime/runs/` | 真正驱动 LangGraph 图、产出事件、写 checkpoint | `run_agent`（`worker.py:246`）在这 |

打个比方：路由层是**前台**（接单、决定怎么上菜），服务层是**领班**（核对菜单、记客户信息、排好单子、把单子递进厨房），runtime 层是**厨师**（真正做菜）。

`start_run` 就是那个"领班"——它**不是**做菜的，也不是接单的，它负责"接单到递进厨房之间"的所有杂事。这些杂事朴素写法（直接 `asyncio.create_task(agent.astream(...))`）一件都不做，所以朴素写法在下面 6 个麻烦上全会翻车。

关键结论先放在这：**正因为这些杂事和"响应长什么样"无关**（立即返回 JSON 也要做、流式返回 SSE 也要做、阻塞返回也要做），它们才被从路由层抽出来，集中到一个共享的 `start_run` 里。这就是为什么 6 个 call site 能复用同一行 `record = await start_run(body, thread_id, request)`。

### 0.2 `contextvar`——异步里的"隐式上下文"

Python 的 `contextvars` 模块提供一种**协程级的隐式变量**：设一个值后，**同一个协程链里的所有 `await` 调用**都能读到它，不用一层层传参数。

DeerFlow 用它记"当前 run 归属哪个用户"（`packages/harness/deerflow/runtime/user_context.py`）。`set_current_user(user)`（`user_context.py:55`）设值，`reset_current_user(token)`（`user_context.py:65`）还原，中间任何 `await` 调用的函数都能读到当前 user。

为什么要"隐式"而不是显式传参？因为 agent 的调用链**非常深**（agent → 子 agent → 工具 → 子工具……），如果每层都加一个 `user` 参数，签名会爆炸；而且 LangGraph 的工具协议本身不允许工具自带任意参数。contextvar 让 user 信息像"空气"一样，需要的地方随手能取，不需要的地方完全无感。

但它有个**必须配对**的纪律：`set_current_user` 设了值，**必须在同一个作用域 `reset`**。否则——`start_run` 给这次 run 设了 owner user，如果忘了 reset，下一个请求（复用同一个 worker）会**继承上一个请求的 owner**，用户 A 的请求在"以为自己是用户 B 的环境"里跑，串号了。第 6 个麻烦会专门讲这个 `try/finally` 配对。

### 0.3 `owner_user_id` / 内部调用者 / `auth_source`——多租户归属

DeerFlow 是多租户的：每个 thread 归属一个用户，用户 A 不能在用户 B 的 thread 上开 run、也不能读 B 的 checkpoint。那"这次调用代表哪个用户"怎么确定？分两种调用者：

- **外部调用者**（浏览器、SDK）：`request.state.user` 是认证中间件验过的真实登录用户，`auth_source != "internal"`。
- **内部调用者**（IM 通道 worker、调度器）：它们持有一个内部 token，`auth_source == "internal"`（`app/gateway/auth_disabled.py:16`），`request.state.user` 是个**合成的内部身份**（`system_role == "internal"`，`app/gateway/internal_auth.py:16`），**不是真实用户**。真实用户通过一个受信任的 HTTP 头 `X-DeerFlow-Owner-User-Id` 传进来（`internal_auth.py:81`）。

`get_trusted_internal_owner_user_id(request)`（`internal_auth.py:70`）就干这件事：**只有当调用者是内部调用者时**，才从那个头里读出真实 owner user id；外部调用者一律返回 `None`（不能让外部调用者通过伪造 HTTP 头冒充别人）。

`owner_user_id` 是后面一整串操作的核心：它决定鉴权范围、决定 contextvar 设成谁、决定 run record 上记谁的归属、决定 thread_meta 的 owner。朴素写法不区分这层，等于所有内部调用都"裸奔"。

### 0.4 `goal_thread_lock`——跨协程串行化同一 thread 的创建

※ 概念基础（asyncio 锁）姊妹篇 §0.4 讲过（"单人电话亭"）。这里补 `goal_thread_lock` 这个**特定**的锁。

`run_mgr.create_or_reject` 内部**已经有**一把锁（姊妹篇麻烦 3 讲的 `RunManager._lock`），它保证"检查 inflight + 创建 run"是原子的。那为什么 `start_run` 还要在它**外面**再套一层 `goal_thread_lock`？

因为 `create_or_reject` 的锁**管不住**它锁外的事。看 `start_run` 的结构（精简）：

```python
async with goal_thread_lock(thread_id):           # ← 这层锁
    record = await run_mgr.create_or_reject(...)   # ← 里面也有锁，但只管 create
# ... 锁外：thread_meta upsert ...
```

设想没有外层 `goal_thread_lock`：协程 A 调 `create_or_reject` 建了 run R1（释放了 RunManager 的锁），**正准备**去 `thread_store.create(thread_id)`（第一次给这个 thread 注册元信息）；与此同时协程 B 也调 `create_or_reject`——如果策略是 `reject`，B 会先查 inflight，但 R1 这时还是 pending……更糟的是，两个协程同时去 `thread_store.create(thread_id)`，第二次会撞"thread 已存在"。所以需要一把**范围更大**的锁，把"创建 run + upsert thread_meta"这组跨 store 的读改写**整体串行化**。

`goal_thread_lock` 的实现在 `packages/harness/deerflow/runtime/goal.py:68`，是个 async context manager。它有个细节值得注意——它**按 event loop 隔离**（`goal.py:60` 的 `weakref.WeakKeyDictionary[loop, ...]`）：

```python
_goal_locks_by_loop: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = ...
```

也就是说，每个 thread_id 在**每个 event loop**里有一把独立的锁。为什么？因为 `asyncio.Lock` **不能跨 loop 用**——在 loop A 里 `acquire` 的锁，到 loop B 里 `release` 会炸。按 loop 隔离就避开了这个坑。`weakref` 让 loop 一死、它那批锁自动被 GC 掉，不漏内存。

> 注意：`goal_thread_lock` 是**单进程内**的串行化（它锁不住别的 worker）。跨进程的"同一 thread 不能并发跑两个 run"靠的是 `RunManager` + 数据库唯一索引（姊妹篇麻烦 3）。两把锁分工不同，缺一不可。

### 0.5 `RunnableConfig` / `configurable` / `context`——LangGraph 的运行配置

LangGraph 跑一个图，需要一个 **`config` 字典**（类型 `RunnableConfig`），里面塞这次运行的所有配置。DeerFlow 往里塞两组东西：

- **`configurable`**：LangGraph 原生的、**会被持久化进 checkpoint** 的配置。最关键的是 `configurable["thread_id"]`——checkpointer 靠它区分"这是哪段对话的状态"。
- **`context`**：LangGraph ≥0.6 引入的、**线程级但不进 checkpoint** 的配置（`ToolRuntime.context`）。放些运行期才需要、但不能持久化的东西（比如一次性 token、运行时开关）。

`start_run` 花了**将近一半篇幅**在组装这个 `config`（第 5 个麻烦会逐层讲）。为什么这么费劲？因为这个字典会被原样传进 `agent.astream(config=...)`、被工具读、被中间件读、被写进 checkpoint——**它是 agent 运行的"配方 + 身份证"**。配方错一个字段，agent 行为就跑偏；身份证漏一个字段，归属就乱了。

有个 LangGraph ≥0.6 的硬约束要记一下：**`configurable` 和 `context` 不能同时传**（`build_run_config` 在 `services.py:464-470` 专门处理这个）。DeerFlow 的策略是：如果客户端传了 `context`，就尊重它、不再塞自己的 `configurable`；否则默认走 `configurable`。

### 0.6 外部 vs 内部调用者——安全边界

这是贯穿 `start_run` 的一个二分判断：`is_internal_caller`（`services.py:718`）。

```python
is_internal_caller = getattr(getattr(request, "state", None), "auth_source", None) == AUTH_SOURCE_INTERNAL
```

它决定三件事：

1. **input 怎么 normalize**：内部调用者（IM 通道）可信，保留它们加的元信息；外部调用者（浏览器）不可信，server-owned 的字段（如 `original_user_content`）一律剥离（`normalize_input` 的 docstring，`services.py:156-158`）。
2. **context key 能否带**：有些 context key（`_CONTEXT_INTERNAL_CALLER_KEYS`）是内部专属的；外部调用者就算在 `body.config` 里偷塞了这些 key，也会被 `strip_internal_context_keys`（`services.py:240`）剥掉。
3. **`context` 段里 `__` 前缀的私钥**：`build_run_config`（`services.py:475-483`）会剥掉所有 `__` 开头的 key——那是 harness 内部的私有通道（skill 密钥绑定源、活跃密钥集、run journal），任何调用者都不能伪造。

朴素写法没有这层判断，外部调用者能往 context 里塞 `__slash_skill_secret_source` 伪造内部状态（issue #3938 的坑），等于绕过了 skill 的启用/白名单/声明闸门。

---

好，前置概念讲完了。现在我们开始讲代码。

## 第 1 步：朴素写法长什么样

如果没有 `start_run`，一个"创建 run 并流式返回"的路由大概会写成这样（**这是反面教材，真实代码不是这样**）：

```python
async def create_run_naive(thread_id, body, request):
    agent = make_lead_agent(config={"configurable": {"thread_id": thread_id}})
    record = RunRecord(run_id=str(uuid.uuid4()), thread_id=thread_id, status=RunStatus.pending)
    record.task = asyncio.create_task(agent.astream(body.input, config=agent.config))   # 直接派出去
    return _record_to_response(record)
```

逐行解释：

- **第 1 行** `async def create_run_naive(thread_id, body, request):` 路由处理函数。三个参数：会话 ID、请求体、请求对象。
- **第 2 行** `agent = make_lead_agent(config=...)` 直接构造一个 agent。
- **第 3 行** `record = RunRecord(...)` 手搓一个 run record，pending 状态。
- **第 4 行** `record.task = asyncio.create_task(agent.astream(...))` **直接派出去后台跑**，task 钉在 record 上。
- **第 5 行** `return _record_to_response(record)` 立刻返回。

这 5 行代码看起来"能用"，但它在下面 **6 个横切关注点**上**一件都没做**：

1. **没鉴权**——谁都能在别人 thread 上开 run。
2. **没验模型**——用户能指定任意 model_name 绕过白名单/计费。
3. **没并发控制**——两个请求同时建 run，撞车（姊妹篇麻烦 3 的病根）。
4. **没管 thread 元信息**——thread 在 `/threads/search` 里查不到。
5. **没加固 config**——外部调用者能伪造内部状态、密钥泄漏进 checkpoint。
6. **task 没注册 + 上下文没还原**——task 是野任务（姊妹篇麻烦 1），owner contextvar 泄漏到下个请求。

下面 6 个麻烦，就是 `start_run` 一一解决这 6 件事的全过程。它的全部源码在 `backend/app/gateway/services.py:608-770`，约 160 行。我们分段来看。

---

## 第 2 步：6 个麻烦逐一展开

### 麻烦 1：鉴权与归属——谁能在谁的 thread 上开 run

#### 场景

用户 A 登录了，想在一个 thread 上开 run。但那个 thread 归属用户 B（比如 A 手搓了一个 B 的 thread_id 直接 POST）。后端必须拦下：**404**（不是 403，避免 thread_id 枚举攻击——告诉攻击者"这个 thread 存在但不是你的"反而泄露了信息）。

更复杂的是**内部调用者**：IM 通道（比如飞书 bot）代表真实用户来开 run，它持有内部 token，`request.state.user` 是合成的内部身份，真实用户在 `X-DeerFlow-Owner-User-Id` 头里。这时鉴权要**按那个 owner** 而不是按内部身份。

#### 朴素写法为什么做不到

第 1 步的朴素写法**压根没调 `check_access`**。任何人拿到一个 thread_id 就能在上面开 run、跑 agent、读 checkpoint——把别人对话的历史全读出来、还能往里写脏数据。多租户隔离形同虚设。

#### `start_run` 怎么解决：`check_access` + 内部调用者二次鉴权

看 `services.py:648-670`：

```python
owner_user_id = get_trusted_internal_owner_user_id(request)        # 0.3 讲过
user = getattr(request.state, "user", None)
if user is not None:
    allowed = await run_ctx.thread_store.check_access(thread_id, str(user.id))
    if not allowed and owner_user_id and getattr(user, "system_role", None) == INTERNAL_SYSTEM_ROLE:
        # 内部调用者：再按 owner 鉴一次
        allowed = await run_ctx.thread_store.check_access(thread_id, owner_user_id)
    if not allowed:
        raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
```

逐行：

- `owner_user_id = get_trusted_internal_owner_user_id(request)` 0.3 讲过。外部调用者这里是 `None`；内部调用者这里是头里的真实 owner。
- `user = getattr(request.state, "user", None)` 取认证中间件盖在 `request.state` 上的用户对象。没有（未认证路径）就 `None`，跳过整个鉴权块（那条路由靠别的装饰器守）。
- `allowed = await run_ctx.thread_store.check_access(thread_id, str(user.id))` **第一次鉴权**：按"当前请求用户"查 thread 访问权。`check_access` 的语义是"这个 thread 要么归属这个 user、要么是共享/无主的（NULL owner），都算可访问；只有归属**另一个** user 才拒绝"（docstring `services.py:652-656` 说得很清楚）。这样自动创建的临时 thread、共享 thread 都不会被误拦。
- `if not allowed and owner_user_id and ... == INTERNAL_SYSTEM_ROLE:` **第二次鉴权**——这是给内部调用者的兜底。如果第一次（按合成内部身份）查不通过，但这是个内部调用者、且带了 owner，就**再按 owner 查一次**。为什么？因为内部身份是合成的（`system_role == internal`），它名下不可能有 thread；真正的归属在 owner 头里。docstring（`services.py:664-668`）给的例子是"IM 通道替真实 owner 认领一个历史遗留的默认归属 thread"。
- `if not allowed: raise HTTPException(404, ...)` 两次都查不过 → 404。注意是 **404 不是 403**——anti-enumeration，不给攻击者"这个 thread 存在"的信号。

注意这段鉴权**在创建 run 之前**（`create_or_reject` 在 `services.py:676`，更靠后）。docstring（`services.py:652-653`）明说："before any run is created"。这很重要——得先确认你有权，才能建 run；不能建完 run 再发现无权，那时 run 已经落库了。

朴素写法不鉴权，等于把多租户的命门敞着。`start_run` 这 23 行（`648-670`）把它守死了。

---

### 麻烦 2：模型白名单——用户不能指定任意模型

#### 场景

DeerFlow 的模型是受控的——管理员在 `config.yaml` 里配一个**模型白名单**（allowlist），规定这个部署只能用哪些模型。用户在 `body.context.model_name` 里指定一个模型，后端得检查它**在不在白名单里**。如果不在 → 400。

为什么必须查？因为模型直接关联**成本**（token 计费）和**安全**（有些模型可能没经过内容审核）。让用户随便指定模型，等于让用户绕过成本控制、绕过安全策略。

#### 朴素写法为什么做不到

朴素写法直接 `agent.astream(...)`，`model_name` 要么用默认、要么从客户端原样吃下去。用户传一个超贵的模型名，agent 照跑，账单爆炸；或者传一个根本没配 API key 的模型名，跑到一半才崩。

#### `start_run` 怎么解决：白名单校验

看 `services.py:631-646`：

```python
body_context = getattr(body, "context", None) or {}
model_name = body_context.get("model_name")

# Coerce non-string model_name values to str before truncation.
if model_name is not None and not isinstance(model_name, str):
    model_name = str(model_name)

# Validate model against the allowlist when a model_name is provided.
if model_name:
    app_config = get_app_config()
    resolved = app_config.get_model_config(model_name)
    if resolved is None:
        raise HTTPException(
            status_code=400,
            detail=f"Model {model_name!r} is not in the configured model allowlist",
        )
```

逐行：

- `body_context = getattr(body, "context", None) or {}` 从请求体取 `context` 字段。`or {}` 兜底：客户端没传 context 就当空 dict（避免后面 `.get` 炸）。
- `model_name = body_context.get("model_name")` 取模型名。`get`（不是 `[]`）取不到返回 `None`，不报错。
- `if model_name is not None and not isinstance(model_name, str): model_name = str(model_name)` **类型规整**。为什么？因为客户端可能传了个数字、布尔之类的非字符串（JSON 反序列化出来的）。后面 `get_model_config` 和日志展示都期望字符串，先 `str()` 兜一道。
- `if model_name:` 有模型名才查（没传就用默认模型，跳过校验）。注意是 truthy 检查——空串、`None`、`0` 都跳过。
- `app_config = get_app_config()`（`packages/harness/deerflow/config/app_config.py:565`）取全局应用配置单例。
- `resolved = app_config.get_model_config(model_name)`（`app_config.py:479`）查白名单：**返回 `ModelConfig`（在白名单里）或 `None`（不在）**。
- `if resolved is None: raise HTTPException(400, ...)` 不在白名单 → 400，明确告诉前端"这个模型没配"。

这个 `model_name` 校验过后，会被原样传给 `create_or_reject`（`services.py:687` 的 `model_name=model_name`），记进 run record，后续 agent 执行时用它。也就是说：**校验和使用的同一个变量**，不会出现"校验的是 A、用的是 B"的漏洞。

朴素写法不校验，用户能拿模型白名单当摆设。`start_run` 这 16 行（`631-646`）把成本/安全闸门焊死了。

---

### 麻烦 3：多任务策略下的创建——同一 thread 不能并发跑两个 run

#### 场景

用户在一个 thread 里已经发了一条消息，agent 正在跑。这时用户又发了一条（手抖连点 / 故意打断）。后端怎么办？DeerFlow 给三种策略（`multitask_strategy`）：

- **`reject`（默认）**：拒绝新 run，返回 409。
- **`interrupt`**：打断旧 run（保留 checkpoint），启新的。
- **`rollback`**：回滚旧 run（撤销 checkpoint），启新的。

这三种策略的**底层实现**（本地锁 + 数据库唯一索引 + lease）姊妹篇麻烦 3 已经讲透了，本文不重复。本文要讲的是：`start_run` 在调用 `create_or_reject` 之外，**为什么还要多套一层 `goal_thread_lock`**。

#### 朴素写法为什么做不到

朴素写法连 `create_or_reject` 都不调，直接 `create_task`。两个请求同时到达，各自建 run、各自启动 agent，两个 agent 同时往同一个 checkpoint 写——撞车，对话历史损坏。这是姊妹篇 0.7 讲的并发竞争。

#### `start_run` 怎么解决：`goal_thread_lock` + `create_or_reject` + 异常翻译

看 `services.py:674-693`：

```python
async with goal_thread_lock(thread_id):                              # ① 外层锁（0.4 讲过）
    record = await run_mgr.create_or_reject(                         # ② 内层：RunManager 的原子创建
        thread_id,
        body.assistant_id,
        on_disconnect=disconnect,
        metadata=body.metadata or {},
        kwargs={"input": body.input, "config": redact_config_secrets(body.config)},
        multitask_strategy=body.multitask_strategy,
        model_name=model_name,
        user_id=owner_user_id,
    )
except ConflictError as exc:
    raise HTTPException(status_code=409, detail=str(exc)) from exc
except UnsupportedStrategyError as exc:
    raise HTTPException(status_code=501, detail=str(exc)) from exc
```

逐行：

**① `async with goal_thread_lock(thread_id):`** 0.4 讲过——这是 `runtime/goal.py:68` 的 async context manager，把"同一 thread 的创建"在**单进程内**串行化。为什么 `create_or_reject` 已经有锁了还要它？因为 `create_or_reject` 的锁**只覆盖它自己内部**（检查 inflight + 落库 + 登记内存），它**管不住**紧随其后的 thread_meta upsert（麻烦 4）。设想没有外层锁：

```
协程 A: create_or_reject 建了 R1（释放 RunManager 锁）
        ↓ 此时协程 B 插进来
协程 B: create_or_reject —— 如果策略是 reject，查 inflight 会看到 R1，拒绝（OK）
协程 A: thread_store.create(thread_id)  —— 第一次注册 thread 元信息
协程 B: (已被拒绝，不会到这)
```

这种 reject 情况其实没事。但**麻烦在于 thread_meta 的读改写**（`services.py:699-713`）：两个协程同时 `thread_store.get(thread_id)` 都得到 `None`，然后都 `thread_store.create(thread_id)`，第二次撞主键冲突。`goal_thread_lock` 把"创建 run + upsert thread_meta"**整体**包进同一把锁，这个竞态就没了。

所以这两把锁的分工是：

- **`goal_thread_lock`**（外层）：单进程内串行化"创建 run + upsert thread_meta"这组**跨 store 的读改写**。
- **`RunManager._lock`**（内层，在 `create_or_reject` 里）：保证"检查 inflight + 落库 + 登记内存"原子。

外层管的是 `start_run` 这个**编排函数**层面的跨 store 一致性，内层管的是 `RunManager` 这个**组件**内部的原子性。两者粒度不同，都不可省。

**② `record = await run_mgr.create_or_reject(...)`** 这才是真正创建 run 的地方（姊妹篇麻烦 3 的主角）。注意它传进去的参数里有几个关键点：

- `on_disconnect=disconnect`：断线语义（`disconnect` 在 `services.py:629` 算：`cancel` 还是 `continue`），决定客户端断开时 run 是取消还是继续跑。
- `kwargs={"input": body.input, "config": redact_config_secrets(body.config)}`：**这是要持久化进 runs 表的 `kwargs_json` 的副本**。注意 `redact_config_secrets(body.config)`（`packages/harness/deerflow/runtime/secret_context.py:130`）——它把 config 里的密钥**剥掉**再存。为什么？因为 run record 会被 run API echo 回客户端（`secret_context.py:134-136`），一个 request-scoped 的密钥（issue #3861）绝不能跟着 record 泄漏出去。**注意区分**：这里存的是脱敏副本，而下面第 5 个麻烦里"活的 config"（驱动真实 run 的）保留密钥。两条路，一份脱敏给存储/展示，一份完整给执行。
- `multitask_strategy=body.multitask_strategy`：并发策略，传给 `create_or_reject` 决定 reject/interrupt/rollback。
- `user_id=owner_user_id`：归属，记进 record（多租户隔离）。

**异常翻译**（`690-693`）：

- `ConflictError → HTTPException(409)`：`create_or_reject` 在 reject 策略下撞 inflight 会抛 `ConflictError`（姊妹篇麻烦 3 讲过），这里翻译成 **409 Conflict**——告诉前端"上一条还没跑完"。
- `UnsupportedStrategyError → HTTPException(501)`：传了个不认识的 `multitask_strategy` 值，`create_or_reject` 抛 `UnsupportedStrategyError`，翻译成 **501 Not Implemented**。

注意这两个 `except` 在 `goal_thread_lock` 的 `async with` **外面**（看缩进——`services.py:690` 的 `except` 对应的是 `674` 的 `try`，不是 `675` 的 `async with`）。为什么？因为我们要在锁释放后才抛 HTTP 异常——异常抛出时如果还持着锁，会拖累别的协程等锁。`async with` 正常退出或异常退出都会释放锁，所以把 `except` 放外面既安全又干净。

朴素写法没有任何并发控制——两个请求各跑各的 agent，checkpoint 必撞。`start_run` 用"外层 goal_thread_lock + 内层 create_or_reject + 数据库唯一索引"三层保障把这件事管死。

---

### 麻烦 4：thread_meta 的尽力而为 upsert——为什么"建 run"要顺带管"thread 注册"

#### 场景

用户在一个**从来没显式创建过的 thread** 上开 run（比如 stateless 路由自动生成了一个 thread_id 就直接开跑）。这时 thread 在 `thread_meta` 表里**没有记录**。后果：这个 thread 在 `/threads/search` 列表里**查不到**——用户刷新页面，发现自己的对话凭空消失了。

所以"开 run"的时候，得顺带保证"这个 thread 在 thread_meta 里有记录"。但又有个微妙要求：**这件事失败了不能拖垮主流程**——run 该跑还得跑，thread_meta 没更新顶多列表里少一条，不能因为它让整个 run 失败。

#### 朴素写法为什么做不到

朴素写法压根没碰 thread_meta——它只管建 run、派 task。结果就是 stateless 路由跑出来的 thread 在前端列表里查不到，用户体验割裂。

#### `start_run` 怎么解决：尽力而为的 upsert + 整段兜底

看 `services.py:695-715`：

```python
# Upsert thread metadata so the thread appears in /threads/search,
# even for threads that were never explicitly created via POST /threads
# (e.g. stateless runs).
try:
    existing = await run_ctx.thread_store.get(thread_id)
    if existing is None and owner_user_id:
        unscoped_existing = await run_ctx.thread_store.get(thread_id, user_id=None)
        if unscoped_existing is not None:
            if unscoped_existing.get("user_id") != owner_user_id:
                await run_ctx.thread_store.update_owner(thread_id, owner_user_id, user_id=None)
            existing = await run_ctx.thread_store.get(thread_id)
    if existing is None:
        await run_ctx.thread_store.create(
            thread_id,
            assistant_id=body.assistant_id,
            metadata=body.metadata,
        )
    else:
        await run_ctx.thread_store.update_status(thread_id, "running")
except Exception:
    logger.warning("Failed to upsert thread_meta for %s (non-fatal)", sanitize_log_param(thread_id))
```

逐行：

- `existing = await run_ctx.thread_store.get(thread_id)` 按**当前 owner** 查 thread_meta。有 → `existing` 是那条记录；没有 → `None`。
- `if existing is None and owner_user_id:` 这块是**认领历史遗留 thread**的逻辑。设想：一个 thread 以前是"无主"（NULL owner，共享数据）创建的，现在内部调用者（带 owner）来开 run。第一次 `get(thread_id)` 按当前 owner 查不到（因为 thread 不归他），但 thread 其实存在（只是 owner 是 NULL）。
  - `unscoped_existing = await run_ctx.thread_store.get(thread_id, user_id=None)` **不限 owner** 再查一次。
  - `if unscoped_existing.get("user_id") != owner_user_id: await ...update_owner(...)` 如果这个无主 thread 的 owner 不是当前 owner，就把它**过户**给当前 owner。这是 IM 通道"替真实 owner 认领遗留 thread"的实现。
  - `existing = await run_ctx.thread_store.get(thread_id)` 过户后再按当前 owner 查一次，这次应该查到了。
- `if existing is None: await run_ctx.thread_store.create(...)` 真的从来没有这个 thread → **新建**一条 thread_meta，带上 assistant_id 和 metadata。
- `else: await run_ctx.thread_store.update_status(thread_id, "running")` thread 已存在 → 把状态标成 `running`（前端能显示"正在跑"）。
- **`except Exception: logger.warning(... "(non-fatal)", ...)`** 这是整段的灵魂。**任何异常都吞掉，只记 warning**。为什么？

  因为 thread_meta 的 upsert 是**辅助副作用**，不是 run 的核心。run 能不能跑，不取决于 thread_meta 有没有更新。如果这里因为数据库抖动抛异常，把它冒泡上去 → 整个 `start_run` 失败 → 用户明明能跑的 run 跑不了。所以宁可吞掉（记个 warning 便于排查），也要让主流程继续往下走（派 task、返回 record）。

  docstring 的 `"(non-fatal)"` 标签就是这个意思——运维看到这条 warning 知道"thread_meta 没更新成功，但 run 照跑了"，是个可容忍的次要故障。

  顺带注意 `sanitize_log_param(thread_id)`——日志里的 thread_id 也做了脱敏（防注入/防信息泄漏的日志规范）。

朴素写法不管 thread_meta，stateless run 的 thread 在列表里凭空消失。`start_run` 这 21 行（`695-715`）用"认领 + 新建 + 尽力而为"三段把 thread 注册兜住了，且绝不让它拖垮主流程。

---

### 麻烦 5：config 的层层加固——agent 的"配方 + 身份证"不能被伪造

#### 场景

`config` 字典是 agent 运行的"配方 + 身份证"：它决定 agent 用什么递归上限、归哪个 thread、归哪个 user、带哪些工具开关、带哪些一次性密钥。这个字典会被传进 `agent.astream(config=...)`、被工具读、被中间件读、**还会被写进 checkpoint**。

正因为如此，它是个**高价值攻击面**：

- 外部调用者往 `body.config.context` 里塞个 `__slash_skill_secret_source`，伪造 skill 密钥绑定源 → 绕过 skill 启用闸门（issue #3938）。
- 外部调用者塞个 `user_id` → 在 agent 里冒充别人。
- 一次性 token（github_token）进了 `configurable` → 被 checkpoint 持久化，永久泄漏。

`start_run` 花了**最多的篇幅**（`services.py:717-744`，近 30 行）在加固这个 config。它是一**层层**的流水线，每一层有明确的安全/脱敏/归属目的。

#### 朴素写法为什么做不到

朴素写法 `make_lead_agent(config={"configurable": {"thread_id": thread_id}})` 手搓一个最小 config——客户端传的任何东西都不进、server 该盖的字段也不盖。结果：

- 安全闸门全开（伪造内部状态、冒充用户）。
- 归属丢失（checkpoint 里没有 user_id）。
- 密钥可能进 checkpoint。

#### `start_run` 怎么解决：6 层加固流水线

逐层来看 `services.py:717-744`：

```python
agent_factory = resolve_agent_factory(body.assistant_id)            # ① 选 agent 工厂
is_internal_caller = getattr(getattr(request, "state", None), "auth_source", None) == AUTH_SOURCE_INTERNAL
command = getattr(body, "command", None)
if command and command.get("resume") is not None:
    graph_input = Command(resume=command["resume"])                 # ②a resume 分支
else:
    graph_input = normalize_input(body.input, trusted_internal=is_internal_caller)  # ②b 正常 input
config = build_run_config(thread_id, body.config, body.metadata, assistant_id=body.assistant_id)  # ③ 基础 config
await apply_checkpoint_to_run_config(config, body=body, thread_id=thread_id, request=request)      # ④ checkpoint 配置

merge_run_context_overrides(config, getattr(body, "context", None), internal=is_internal_caller)    # ⑤ 合并 context
if not is_internal_caller:
    strip_internal_context_keys(config)                             # ⑥ 剥内部 key（仅外部）
internal_owner_user = await resolve_trusted_internal_owner_for_attribution(request, owner_user_id)
inject_authenticated_user_context(                                  # ⑦ 盖 server-owned 身份
    config, request, internal_owner_user=internal_owner_user,
    request_context=getattr(body, "context", None),
)
```

**① `resolve_agent_factory(body.assistant_id)`**（`services.py:380`）选 agent 工厂。它看起来按 `assistant_id` 分发，但其实**所有 assistant_id 都映射到同一个 `make_lead_agent`**（`resolve_agent_factory` 的 docstring `383-388` 明说）。真正的"用哪个 agent"是在 `make_lead_agent` 内部读 `configurable["agent_name"]` 决定的——而那个 `agent_name` 是 `build_run_config`（③）根据 `assistant_id` 塞进去的。这里先拿到工厂，后面 `run_agent` 用它造 agent 实例。

**② input 规整——resume 分支 vs 正常 input**：

- `command = getattr(body, "command", None)` 取 LangGraph 的 `Command`（用于 resume、中断后恢复等场景）。
- `if command and command.get("resume") is not None: graph_input = Command(resume=command["resume"])` 如果是 resume 请求，把 input 包装成 `Command(resume=...)`——LangGraph 协议要求 resume 信号走 `Command`，不能当普通 input。
- `else: graph_input = normalize_input(body.input, trusted_internal=is_internal_caller)` **正常分支**——把客户端的 input 规整成 LangChain state dict。`normalize_input`（`services.py:142`）干两件事：
  - 用 `convert_to_messages` 把 dict 形式的消息转成 `BaseMessage` 对象（保留 `additional_kwargs` 如上传文件元数据，issue #3132；保留 role/id/name）。
  - **按是否内部调用者剥离 server-owned 字段**：`original_user_content` 和动态提醒标记是 server 拥有的，外部调用者不能带；内部调用者（IM 通道自己加的）可以保留（docstring `156-158`）。`trusted_internal` 这个 flag 就是 0.6 讲的 `is_internal_caller`。
  - 格式错误的消息（缺 role/type/content）→ 400，不是 500（系统边界要做 per-entry 校验，docstring `151-154`）。

**③ `build_run_config(...)`**（`services.py:429`）组装基础 config。它干的事（看 `453-488`）：

- `config = {"recursion_limit": _DEFAULT_RECURSION_LIMIT}` 起手——lead graph 的 super-step 上限（默认 100，注释 `454-457` 说清了它和 subagent 的 `max_turns` 是两码事）。
- 如果 `assistant_id` 是自定义 agent，往 `configurable` 和 `context` 都塞 `agent_name`（让 `make_lead_agent` 能读到）。
- 处理 `configurable` vs `context` 的二选一（0.5 讲的 LangGraph ≥0.6 约束）：客户端传了 `context` 就尊重它。
- **关键安全动作**（`475-483`）：剥掉 `context` 里所有 `__` 前缀的 key。那是 harness 的私有通道（skill 密钥绑定源、活跃密钥集、run journal），任何调用者都不能塞。注释明说"一个伪造的 `__slash_skill_secret_source` 会绕过 skill 的启用/白名单/声明闸门（#3938）"。合法的客户端 key（`secrets`、`user_id`、模型覆盖）从不用 `__` 前缀。

**④ `apply_checkpoint_to_run_config(config, body=..., thread_id=..., request=...)`**（`services.py:544`）把 checkpoint 相关的配置盖到 config 上（比如 checkpoint_id、checkpoint 模式）。这层负责让 agent 知道"从哪个 checkpoint 恢复 / 怎么写新 checkpoint"。

**⑤ `merge_run_context_overrides(config, body.context, internal=is_internal_caller)`**（`services.py:255`）把 `body.context` 里**白名单**的 key 合并进 `config["configurable"]` 和 `config["context"]`。它的规则很细（docstring `256-275`）：

- 只转发白名单 key（`_CONTEXT_CONFIGURABLE_KEYS`），未知 key（如 thread_id）忽略。
- 内部调用者额外能带一组内部专属 key（`_CONTEXT_INTERNAL_CALLER_KEYS`）。
- **`_CONTEXT_RUNTIME_ONLY_KEYS`**（如 `github_token`、`disable_clarification`）只进 `context`、**绝不进 `configurable`**——因为 `configurable` 会被 checkpoint 持久化，一次性 token 不能进 checkpoint。这个区分极其关键：短命 token 进 `context`（运行期用完即弃），不进 `configurable`（持久化）。
- `user_id` 用 `setdefault` 进 `context`——让后面 ⑦ 的 server 端认证身份**永远盖过**客户端传的。

**⑥ `if not is_internal_caller: strip_internal_context_keys(config)`**（`services.py:240`）**仅对外部调用者**，剥掉他们偷塞进 `config` 的内部专属 key。为什么 ⑤ 已经 gate 了还要这步？因为 `build_run_config`（③）会把客户端的 `body.config["context"]` / `body.config["configurable"]` **原样拷贝**进来（`strip_internal_context_keys` 的 docstring `244-246` 明说），所以同一个 key 要从**两个段**都剥。③ 剥的是 `__` 前缀，⑥ 剥的是内部专属 key——两道不同的闸。

**⑦ `inject_authenticated_user_context(config, request, internal_owner_user=..., request_context=...)`**（`services.py:312`）**最后一道**：把 server 端认证过的身份**盖**进 config。它的 docstring（`319-327`）点明要害：

> Tool execution may happen after the request handler has returned, so tools that persist user-scoped files should not rely only on ambient ContextVars. The value comes from server-side auth state, never from client context.

翻译：**工具执行可能在请求 handler 返回之后才发生**（agent 在后台跑，请求早结束了），所以工具不能只靠 contextvar（0.2 讲的"隐式上下文"）拿用户——得把用户**写进 config**，config 会跟着 agent 一路传下去。而且这个值**只来自 server 端认证状态，绝不来自客户端 context**。

具体动作（`330-341`）：

- 先把 `_SERVER_OWNED_AUTHZ_CONTEXT_KEYS`（server 拥有的鉴权身份字段）从 `context` 和 `configurable` **都清掉**——清掉客户端伪造的值。
- 再写 server 端权威的 `is_internal` 等字段。**先清后写**，保证客户端伪造的身份被抹掉。

前面的 `internal_owner_user = await resolve_trusted_internal_owner_for_attribution(request, owner_user_id)`（`services.py:297`）是为了**归属归因**：如果是内部调用者，把 owner 头里的真实用户对象取出来（`get_local_provider().get_user(owner_user_id)`），传给 ⑦ 用于归因。取不到（异常）就记日志返回 None，不阻断（`307-309`）。

**为什么要这么多层？** 因为 config 是个**自由格式的字典**，客户端能往里塞任意东西，而它又被 agent/工具/中间件/checkpoint 消费。每一层防御针对一种攻击/泄漏面：

| 层 | 防什么 |
|---|---|
| ② normalize_input | 外部带 server-owned 字段 |
| ③ build_run_config 剥 `__` | 伪造 harness 私有通道（#3938） |
| ⑤ merge（RUNTIME_ONLY 只进 context） | 一次性 token 进 checkpoint 泄漏 |
| ⑥ strip_internal_context_keys | 外部带内部专属 key |
| ⑦ inject（先清后写） | 伪造 user_id / 鉴权字段 |

朴素写法一层都不做，config 是个敞口的攻击面。`start_run` 这 6 层流水线把"配方 + 身份证"焊死了。

---

### 麻烦 6：record.task 延迟绑定 + contextvar 还原

#### 场景

`start_run` 的最后两件事，看似不起眼，却是"run 不失联 + 不串号"的关键：

1. **task 的延迟绑定**：`record.task` 一开始是 `None`，`create_or_reject` 返回时还没 task，要等 `asyncio.create_task(run_agent(...))` 之后才把 task 赋上去。这个"延迟"有什么讲究？
2. **contextvar 还原**：开头设的 `owner_context_token`，必须在 `finally` 里 `reset`。忘了会怎样？

#### 朴素写法为什么做不到

朴素写法是 `record.task = asyncio.create_task(...)` 一行——record 和 task 同时出现，没有"先注册 record、再绑 task"的两步；更没有 contextvar 的 set/reset 配对（它压根没设 contextvar）。后果：

- task 是"野"的（姊妹篇麻烦 1）——record 虽然有 task 字段，但 record 本身没注册进任何全局表，run 还是失联。
- owner 信息没进 contextvar，后台 agent 跑起来时取不到 owner（或者更糟，取到上个请求残留的 owner）。

#### `start_run` 怎么解决：先注册后绑 task + try/finally 配对

先看**注册**这件事——它其实发生在 `create_or_reject` **内部**（麻烦 3 那一步）。姊妹篇麻烦 1 讲过：`create_or_reject` 在创建 record 的**同时**就把它登记进了 `RunManager._runs` 字典。也就是说，`services.py:676` 这行 `record = await run_mgr.create_or_reject(...)` **返回时，record 已经在进程级注册表里了**——`record.task` 此时是 `None`，但 record 本身已经"有处可查"。

然后看 task 的派发与绑定（`services.py:744-761`）：

```python
stream_modes = normalize_stream_modes(body.stream_mode)

task = asyncio.create_task(
    run_agent(
        bridge,
        run_mgr,
        record,
        ctx=run_ctx,
        agent_factory=agent_factory,
        graph_input=graph_input,
        config=config,
        stream_modes=stream_modes,
        stream_subgraphs=body.stream_subgraphs,
        interrupt_before=body.interrupt_before,
        interrupt_after=body.interrupt_after,
    )
)
record.task = task
```

逐行：

- `stream_modes = normalize_stream_modes(body.stream_mode)`（`services.py:117`）把客户端的 `stream_mode`（可能是字符串、列表、None）规整成统一的 `list[str]`，传给 `run_agent` 决定 agent 产哪些模式的事件（values/updates/messages/...）。
- `task = asyncio.create_task(run_agent(...))` **这一行是"启动"的真正发生地**。`asyncio.create_task` 把 `run_agent` 协程塞进后台事件循环，立刻返回一个 task 对象。**注意：到这里 `start_run` 还没返回，但 agent 已经在后台跑起来了**——这就是"run 活得比请求久"的物理起点。
  - 传给 `run_agent` 的参数全是前面几步准备好的：`bridge`（事件流出口）、`run_mgr`（状态机，`run_agent` 会调它的 `set_status`）、`record`（run 身份，`run_agent` 往它的字段写进度）、`ctx`（runtime 依赖：checkpointer/store/event_store）、`agent_factory`（① 选的工厂）、`graph_input`（② 规整的输入）、`config`（③-⑦ 加固的配置）、`stream_modes`、`interrupt_before/after`（中断点）。
  - **本篇不深入 `run_agent` 内部**（它怎么调 `agent.astream`、怎么 `bridge.publish`、怎么 `set_status`、怎么 `publish_end`）——那是 runtime 层的事。本篇只交代：`start_run` 把这个协程**派出去**，并把它的 task 钉在 `record.task` 上。
- `record.task = task` **延迟绑定**——task 创建后立刻赋给 record。为什么强调"延迟"？因为 record 在 `create_or_reject` 返回时就已经注册进 `_runs` 了（task 是 `None`），而 task 是**后来**才绑的。这个顺序保证了一个重要性质：**只要 record 存在，它就一定在注册表里**（哪怕短暂地 task=None）。设想反过来——先 `create_task` 再注册 record：task 派出去后、注册前，如果有别的协程来查这个 run，会发现注册表里没有，以为 run 不存在；而 agent 其实已经在跑了。先注册后绑 task 关闭了这个窗口。

  而且这个 task **不是野任务**——它被钉在 `record.task` 上，record 钉在 `_runs` 字典里，`_runs` 钉在进程级单例上（姊妹篇麻烦 1 的"三级钉住"）。所以后续任何请求都能按 `run_id` 找到 record、拿到 `record.task`、观察/取消它。

再看 **contextvar 还原**。开头的设置在 `services.py:672`：

```python
owner_context_token = set_current_user(SimpleNamespace(id=owner_user_id)) if owner_user_id else None
try:
    ...  # 整个 run 创建 + task 派发 ...
    return record
finally:
    if owner_context_token is not None:
        reset_current_user(owner_context_token)
```

逐行：

- `owner_context_token = set_current_user(...) if owner_user_id else None` 0.2 讲过——如果有 owner，设 contextvar，拿到 token（用于后面还原）；没 owner 就 `None`，不设。设这个 contextvar 的目的：让 `try` 块里 `create_or_reject`、`thread_store` 等**同步/异步调用链**都能通过 contextvar 读到当前 owner（归属判定、落库 user_id 等）。`SimpleNamespace(id=owner_user_id)` 是个轻量 user 替身——只需要个 `id`。
- `try: ... return record` 整个 run 创建逻辑包在 try 里。
- `finally: if owner_context_token is not None: reset_current_user(owner_context_token)` **无论 `start_run` 正常返回还是抛异常，finally 都执行**——把 contextvar **还原**到设之前的状态。

  **为什么必须还原？** 0.2 讲过纪律。FastAPI 的 worker 是**长跑的**，一个请求处理完，worker 继续处理下一个请求，**复用同一个协程上下文**。如果 `start_run` 设了 owner user 不还原：

  ```
  请求 1（用户 A）: set_current_user(A) → 跑完 → 忘了 reset
  请求 2（用户 B）: 进来时 contextvar 还是 A → agent 在"以为自己是 A"的环境里跑 B 的 run
  ```

  串号了。而且这种 bug 极难排查——时有时无（取决于请求落到哪个 worker、上一个请求是谁）。`try/finally` 配对是杜绝它的标准写法：**set 必有 reset，异常路径也 reset**。

  注意 `if owner_context_token is not None`——只有设过才 reset（没设过 reset 会出错）。这和开头的 `if owner_user_id else None` 严格对称。

朴素写法既不注册 record（野任务）、也不管 contextvar（串号）。`start_run` 用"先注册后绑 task + try/finally 配对"把这两件事都兜住了。

---

## 第 3 步：一个 record，三种命——create / stream / wait

讲到这里，`start_run` 做的事已经清楚了：**它把所有和"响应形状无关"的横切关注点处理完，吐出一个干净的 `RunRecord`**（task 已绑、config 已加固、归属已记、thread_meta 已 upsert）。这个 record 的 `task` 字段已经指向后台跑着的 `run_agent` 协程。

现在的问题是：**拿到这个 record 之后，路由怎么把它变成 HTTP 响应？** 这正是路由层的职责。同一个 `record = await start_run(...)`，在三个路由里被包成**三种完全不同的响应**。这是理解"为什么 `start_run` 要从路由层抽出来"的最后一块拼图——**正因为启动逻辑共享，路由才能各自只操心响应形状**。

### 命运 1：`POST /{thread_id}/runs`——立即返回 JSON

看 `backend/app/gateway/routers/thread_runs.py:488-493`：

```python
@router.post("/{thread_id}/runs", response_model=RunResponse)
@require_permission("runs", "create", owner_check=True, require_existing=True)
async def create_run(thread_id: str, body: RunCreateRequest, request: Request) -> RunResponse:
    """Create a background run (returns immediately)."""
    record = await start_run(body, thread_id, request)
    return _record_to_response(record)
```

逐行：

- `@router.post(...)` 注册路由。
- `@require_permission("runs", "create", owner_check=True, require_existing=True)` **鉴权装饰器**——注意这层鉴权和 `start_run` 里面的 `check_access`（麻烦 1）是**两道不同的闸**：装饰器基于 path param 做粗粒度权限 + owner 检查，`start_run` 里面的 `check_access` 是给 stateless 路由（thread_id 在 body 里、装饰器够不着）兜底的细粒度检查。两道闸都过才放行。
- `record = await start_run(body, thread_id, request)` **共享的启动逻辑**。
- `return _record_to_response(record)` 立刻把 record 转成 `RunResponse`（JSON）返回。**注意：此时 `record.task` 指向的后台 agent 还在跑**，但 HTTP 响应已经返回了。客户端拿到 `run_id`，稍后通过 `GET .../join` 或 `GET .../stream` **另发请求**去订阅这个 run 的事件流。

这是 **"create-then-join"** 模式（LangGraph Platform 协议）：创建和订阅是两次独立的 HTTP 请求。适合客户端想"提交后干别的、过会儿再来看"的场景。

### 命运 2：`POST /{thread_id}/runs/stream`——内联 SSE

看 `thread_runs.py:496-521`：

```python
@router.post("/{thread_id}/runs/stream")
@require_permission("runs", "create", owner_check=True, require_existing=True)
async def stream_run(thread_id: str, body: RunCreateRequest, request: Request) -> StreamingResponse:
    bridge = get_stream_bridge(request)
    run_mgr = get_run_manager(request)
    record = await start_run(body, thread_id, request)

    return StreamingResponse(
        sse_consumer(bridge, record, request, run_mgr),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Content-Location": f"/api/threads/{thread_id}/runs/{record.run_id}",
        },
    )
```

逐行：

- `bridge = get_stream_bridge(request); run_mgr = get_run_manager(request)` 路由层自己也要拿这两个单例——因为 `sse_consumer`（消费事件流的生成器）需要它们。注意 `start_run` 内部也拿了同样的单例，但那是 `start_run` 的局部变量，路由拿不到，所以路由得自己再拿一次。
- `record = await start_run(body, thread_id, request)` **同一个共享调用**。`start_run` 返回时，后台 task 已经在往 bridge 里 `publish` 事件了。
- `return StreamingResponse(sse_consumer(...), ...)` 返回一个 **SSE 流式响应**。`sse_consumer`（`services.py:828`）是个 async generator，它 `bridge.subscribe(record.run_id)` 订阅这个 run 的事件流，把每个事件格式化成 SSE frame（`data: ...\n\n`）yield 出去。**HTTP 连接保持打开**，客户端实时收到 agent 产出的 token——这是聊天界面"打字机效果"的实现。

  关键点：**这个响应和"命运 1"用的是同一个 run、同一个 task、同一个事件流**。区别只在"HTTP 连接是立刻关还是保持开"。`start_run` 不关心这个区别，它只管把 run 启动好；路由决定连接形态。

- `headers` 里几个值得说：
  - `"X-Accel-Buffering": "no"` 告诉 nginx"别缓冲这个响应"——SSE 要实时推送，被 nginx 缓冲就变成一坨一坨的了。
  - `"Content-Location": f"/api/threads/{thread_id}/runs/{record.run_id}"` **这是 LangGraph Platform 协议要求的头**。注释（`thread_runs.py:516-519`）解释：前端的 `useStream` React hook 用一个**贪婪正则**从这个路径里提取 run_id，所以这个路径必须指向"规范的 run 资源 URL"、不能带额外后缀。客户端拿这个 run_id 后，万一连接断了，能凭它重连到 `join` 端点继续订阅。

这是 **"fire-and-stream"** 模式：创建和流式订阅在**同一次** HTTP 请求里。适合聊天界面实时看 token 流的场景。

### 命运 3：`POST /{thread_id}/runs/wait`——阻塞到完成

看 `thread_runs.py:524-548`：

```python
@router.post("/{thread_id}/runs/wait", response_model=dict)
@require_permission("runs", "create", owner_check=True, require_existing=True)
async def wait_run(thread_id: str, body: RunCreateRequest, request: Request) -> dict:
    bridge = get_stream_bridge(request)
    run_mgr = get_run_manager(request)
    record = await start_run(body, thread_id, request)

    completed = True
    if record.task is not None:
        completed = await wait_for_run_completion(bridge, record, request, run_mgr)

    if completed:
        checkpointer = get_checkpointer(request)
        config = {"configurable": {"thread_id": thread_id}}
        try:
            checkpoint_tuple = await checkpointer.aget_tuple(config)
            if checkpoint_tuple is not None:
                checkpoint = getattr(checkpoint_tuple, "checkpoint", {}) or {}
                channel_values = checkpoint.get("channel_values", {})
                return serialize_channel_values_for_api(channel_values)
        except Exception:
            logger.exception("Failed to fetch final state for run %s", record.run_id)

    return {"status": record.status.value, "error": record.error}
```

逐行：

- `record = await start_run(body, thread_id, request)` 还是**同一个共享调用**。
- `if record.task is not None:` **检查 task 是否存在**。为什么查？因为 `start_run` 可能在某些边缘情况返回 task=None 的 record（比如 run 已经被并发策略干掉了）。正常情况下 task 一定在，这个检查是防御性的。
- `completed = await wait_for_run_completion(bridge, record, request, run_mgr)`（`services.py:874`）**阻塞等待 run 跑完**。它内部消费 bridge 的事件流直到 `END_SENTINEL`（尊重 `on_disconnect` 语义）。`completed` 表示是否真的等到自然结束（vs 客户端断开了）。
- `if completed:` 如果跑完了——**去 checkpointer 读最终的 channel_values**（agent 跑完后写进 checkpoint 的最终状态），序列化返回。这是 `/wait` 区别于 `/stream` 的地方：`/stream` 返回的是事件流，`/wait` 返回的是**最终状态快照**。
- `return {"status": record.status.value, "error": record.error}` 兜底——没跑完或读 checkpoint 失败，至少返回状态和错误。

这是 **"fire-and-wait"** 模式：同步阻塞，run 跑完才返回。适合不需要实时流、只想拿最终结果的脚本/批处理调用。

### 三个命运共享同一个 `start_run` 的根本原因

把三个路由并排看，`start_run` 那一行**一模一样**：

```python
record = await start_run(body, thread_id, request)
```

三个路由**只在这行之后分道扬镳**：

| 路由 | 拿到 record 后做什么 | 响应形态 | 客户端何时拿到结果 |
|---|---|---|---|
| `create_run` | `_record_to_response(record)` | 立即 JSON | 立刻（只给 run_id），结果要另发请求订阅 |
| `stream_run` | `StreamingResponse(sse_consumer(...))` | 内联 SSE | 实时（token 流） |
| `wait_run` | `await wait_for_run_completion(...)` + 读 checkpoint | 阻塞 JSON | run 跑完后（最终状态） |

**这就是 `start_run` 存在的最强论据**：鉴权、模型校验、并发控制、thread_meta、config 加固、task 注册——这 6 件事**和响应形状无关**，三种响应都要做。如果把这 6 件事写进每个路由，就是三份重复的 ~150 行代码，改一处要改三处（而且容易漏改）。抽成 `start_run` 后，路由只剩三四行，各自只管"响应怎么包"。

> **顺带一提：还有两个 stateless 路由和一个调度器入口也调 `start_run`**，合计 **6 个 call site**：
> - `routers/runs.py:46`（`stateless_stream`，POST `/api/runs/stream`）和 `routers/runs.py:71`（`stateless_wait`，POST `/api/runs/wait`）——thread_id 在 body 里自动生成或复用，鉴权靠 `start_run` 里的 `check_access`（装饰器够不着 body 里的 thread_id，docstring `services.py:649-652` 专门说了这个）。
> - `services.py:773` 的 `launch_scheduled_thread_run`——调度器入口，它的 `body` 不是 Pydantic 的 `RunCreateRequest`，而是个**手搓的 `SimpleNamespace`**（`services.py:798-802`），模拟请求体的字段。注释（`services.py:795-797`）特意提醒："如果 `start_run` 新读了一个 `body.*` 属性，记得在这里补上对应字段，保持调度器路径同步"——这是 `start_run` 作为共享契约的一个维护纪律。

---

## 第 4 步：把本质串起来

朴素写法 `record.task = asyncio.create_task(agent.astream(...))` 的根本问题是：

> **它把"启动一个 run"等同于"派一个协程"——而真实世界里，安全地启动一个 run 要处理 6 件和"派协程"无关、但又绝不能省的横切关注点。**

这 6 件事的共同特点是：**它们和"HTTP 响应长什么样"完全无关**。立即返回 JSON 要做、流式返回 SSE 要做、阻塞返回也要做。正是这个特点决定了它们必须从路由层抽出来、集中到一个共享函数里——这就是 `start_run`。

| 横切关注点 | 朴素写法 | `start_run` 处理点 |
|---|---|---|
| 鉴权与归属 | 不做 | `check_access` + 内部调用者二次鉴权（`648-670`） |
| 模型白名单 | 不做 | `get_model_config` → 400（`631-646`） |
| 多任务策略创建 | 不做（野任务） | `goal_thread_lock` + `create_or_reject`（`674-693`） |
| thread_meta upsert | 不做 | 尽力而为 + non-fatal 兜底（`695-715`） |
| config 加固 | 不做（敞口） | 6 层流水线（`717-744`） |
| task 注册 + contextvar 还原 | 野任务 + 串号 | 先注册后绑 task + try/finally（`672-770`） |

`start_run` 就是**把"安全地启动一个 run"这件事集中化的那个编排阀门**。它吐出的 `RunRecord` 是一个"干净的、可直接消费的"对象：

- **归属干净**：鉴权过了，owner 记进 record 和 contextvar。
- **配置干净**：config 加固过，无伪造、无泄漏。
- **状态干净**：record 已注册进 `RunManager._runs`，task 已绑定，run 不会失联。
- **副作用干净**：thread_meta 已 upsert，脱敏副本已落库。

路由层拿到这个干净的 record，只需决定"怎么把它包成响应"——立即 JSON、内联 SSE、还是阻塞等待。**启动的复杂性被 `start_run` 吸收了，路由保持薄。**

### 三姊妹篇的关系

至此，三篇文章覆盖了"一次 agent run 的完整生命周期"的三个关键面：

- **`stream-bridge-deep-dive`**（`bridge`）——解耦**事件流**的生命周期：agent 产出的 token 要能被任意数量、任意时刻、跨进程的订阅者消费。
- **`run-manager-deep-dive`**（`run_mgr`）——解耦**执行 + 状态**的生命周期：run 要活过创建请求、跨重启可查、跨 worker 可协调、能被干净取消和收尸。
- **本文 `start-run-deep-dive`**（`start_run`）——是**安全启动的编排阀门**：把鉴权、模型、并发、thread_meta、config、task 注册这 6 件横切关注点集中处理，吐出一个干净的 record，让路由只管响应形状。

三者是**同一个启动流程**的三层：

```
HTTP 请求
   │
   ├─ 路由层（薄）：鉴权装饰器 + 决定响应形状
   │
   ▼
start_run（编排阀门）─────────────┐
   │  ├─ 鉴权归属（麻烦 1）         │
   │  ├─ 模型白名单（麻烦 2）        │  这 6 件事和响应形状无关
   │  ├─ goal_thread_lock +         │  所以能被 3 个路由共享
   │  │  create_or_reject（麻烦 3） │
   │  ├─ thread_meta upsert（麻烦 4）│
   │  ├─ config 6 层加固（麻烦 5）   │
   │  └─ task 绑定 + contextvar（麻烦 6）│
   │                                │
   │  吐出干净的 RunRecord ──────────┘
   │
   ▼
路由层用 record 包成响应（JSON / SSE / 阻塞）
   │
   meanwhile，后台 task = run_agent(...) 在跑：
      ├─ 通过 bridge 发布事件  ← stream-bridge 管这一半
      └─ 通过 run_mgr 更新状态  ← run-manager 管这一半
```

所以 `start_run` 不是"代码好看"或"封装一下"——是这 6 个横切关注点里**任意一个**单拿出来，朴素写法都过不去（要么安全洞、要么数据损坏、要么串号、要么失联）。它和另外两篇的 `bridge`、`run_mgr` 是同一类东西：

- `bridge` 管"事件流出去"。
- `run_mgr` 管"执行和状态活下去"。
- `start_run` 管"run 被安全、正确地启动起来"。

三者一起，把"一次 HTTP 请求里启动的 agent run"从请求的短暂生命周期里彻底解放出来——`start_run` 负责干净地把它派出去，`run_mgr` 负责让它活下来并被协调，`bridge` 负责把它产出的事件送到任意订阅者手上。

这就是 `record = await start_run(body, thread_id, request)` 存在的全部理由。
