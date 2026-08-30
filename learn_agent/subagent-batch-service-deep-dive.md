# SubagentBatchService 深度讲解：为什么"派一千个子代理去干活"不能只是一次 for 循环调 task——让批量工作活过本次 run、活过 Gateway 重启

本文是 `run-manager-deep-dive.md` 和 `run-agent-deep-dive.md` 的姊妹篇。

`run-manager` 那篇回答的问题是：**一次 HTTP 请求里启动的 agent run，凭什么能活得比这次请求久？** 本篇回答的是紧挨着它的下一层问题：**一个 run 里通过 `task` 工具派出去的子代理工作，凭什么能活得比这个 run 本身还久？** run 会结束、Gateway 会重启、进程会崩溃——但派出去的一千个独立子任务不能跟着陪葬。

这就是 2026 年 8 月合入的 **durable subagent batch（持久子代理批量执行）** 功能（PR #4998）。它带来一个新工具 `batch_task`、一个后台服务 `SubagentBatchService`、两张 SQL 表、一套租约恢复机制，以及一个此前不存在的概念——**统一执行容量**。

本文面向第一次接触这套架构的同学。先把所有前置概念一个一个铺出来，然后逐段讲代码。

---

## 第 0 步：先把要用的术语讲清楚

### 0.1 普通 `task` 工具的生命周期绑定问题

读过 `run-agent-deep-dive.md` 的同学知道，lead agent（主代理）有一个 `task` 工具可以委派子代理。它的工作方式是：lead 发一个 `task` 工具调用 → 子代理在隔离的执行环境里跑 → **lead 挂起等待** → 子代理跑完，结果作为 `ToolMessage` 回到 lead 的上下文里。

注意"挂起等待"这四个字。普通 `task` 是**同步语义**：`task` 调用不返回，lead 的这一轮推理就不结束；整个 run 不结束，这些子代理就都活着。这带来三个天然约束：

1. **子代理的命和 run 的命绑死。** run 结束（成功、出错、被用户点停止），所有还在跑的子代理全部终止。run 活多久，子代理最多活多久。
2. **结果全部涌进 lead 的上下文。** 每个子代理的返回都是一个 `ToolMessage`，要占 lead 的上下文窗口。派 10 个还行，派 1000 个，lead 自己先被撑死。
3. **并发数受每次 run 的总量约束。** `max_total_per_run`（默认 6）限制一个 run 能委派多少次；单次响应里并行 `task` 的数量也被硬性限制（`prompt.py` 里的 HARD LIMITS）。这是故意设计的——防止 lead 疯狂委派。

对"帮我审查这个目录里所有文件的命名规范"这类**量大、每项独立、结果不需要逐条汇报**的任务，这三个约束全是反的：量大但不想占上下文、跑得久但 run 可能先结束、量太大但总量上限卡死。

### 0.2 durable（持久）是什么意思

本篇标题里的 durable 是分布式系统的标准词汇：**状态记在进程外面的持久存储里（这里是 SQL 数据库），而不是进程内存里**。进程死了，状态还在；进程重新起来，能从持久存储里把没干完的活接回去。

对比一下：

- 普通 `task` 的子代理状态活在**进程内存**（executor 的注册表）+ **当前 run 的图状态**里。run 结束、进程重启，状态就没了。
- durable batch 的每一条子任务是一行 SQL 记录。Gateway 重启 100 次，那一行记录还在表里躺着，等下一个活着的 worker 来认领。

打个比方：普通 `task` 是你在白板边上贴的便利贴——会议室（进程）一收拾，便利贴就进垃圾桶了；durable batch 是写入公司 ERP 系统的工单——公司搬不搬家，工单都在数据库里，谁接班谁查系统。

### 0.3 lease（租约）——本篇的续篇

※ lease 的基础概念（共享单车比喻）`run-manager-deep-dive.md` §0.6 讲过，本篇复用结论：谁要处理一条记录，先"租"走——记下 `lease_owner`（谁租的）和 `lease_expires_at`（租到什么时候）；租约没到期别人不能碰；租的人崩了，租约过期后别人可以收尸接管。

本篇要用到 lease 的两个**新语义**，是 run-manager 篇没有的：

**语义一：attempt（尝试次数）与重试预算。** 一条子任务被认领一次，`attempt` 加一。如果 worker 崩了、租约过期，别人接手时先看 `attempt` 有没有耗尽配置的重试上限（`max_attempts`，默认 3）：没耗尽就重新排队再试一次，耗尽了就标 `failed`。这回答了"崩溃恢复会不会无限循环"的问题——不会，每条任务最多正式执行 `max_attempts` 次。

**语义二：at-least-once（至少一次）执行语义。** 租约过期收尸的前提是"租的人真的死了"，但系统没法区分"死了"和"活得很难受但还没来得及续租"。于是存在这样的窗口：worker A 其实还活着，只是续租慢了一步，worker B 已经把任务重新认领并开始执行——**同一个任务被执行了两次**。这就是 at-least-once：不保证只执行一次，但保证至少成功执行一次。代价是业务侧必须幂等。DeerFlow 的应对是把这件事**显式写进给子代理的提示词里**（§7.3 会看到那段 preamble）。

### 0.4 幂等键（idempotency key）与 `submission_key`

at-least-once 的世界里，幂等是保命的。本篇有两个幂等设计：

- **批次级**：`submission_key = f"{run_id or thread_id}:{tool_call_id}"`。同一个工具调用重复提交（比如网络重试、图状态回放），落到数据库的唯一约束 `(user_id, submission_key)` 上，只会创建一个批次。
- **条目级**：每个 item 必须带一个 `key`（1–128 字符，批次内唯一）。重试、接管、跨 worker 恢复，都用这个 key 定位"是同一条任务"，并且把它作为幂等身份写进子代理的提示词。

### 0.5 提交器桥（submitter bridge）——harness 层和 app 层的分工

看代码前必须先理解这个仓库的分层（见 `backend/AGENTS.md`）：`backend/packages/harness/` 是框架层（`deerflow.*`），`backend/app/` 是 Gateway 应用层（`app.*`）。`SubagentBatchService` 这种"要连数据库、要长在 Gateway 进程里"的服务住在 harness 层，但它**不是任何时候都存在**——只有 Gateway 配置了 SQL 数据库并打开了开关，lifespan 才会创建它。

那 harness 层的 `batch_task` 工具怎么找到一个"可能存在也可能不存在"的服务？答案是一个**进程全局的提交器单例**（`backend/packages/harness/deerflow/subagents/batch_runtime.py:33-49`）：

```python
_submitter: SubagentBatchSubmitter | None = None
_lock = threading.Lock()


def set_subagent_batch_submitter(submitter: SubagentBatchSubmitter | None) -> None:
    global _submitter
    with _lock:
        _submitter = submitter


def get_subagent_batch_submitter() -> SubagentBatchSubmitter | None:
    with _lock:
        return _submitter


def is_subagent_batch_runtime_available() -> bool:
    return get_subagent_batch_submitter() is not None
```

逐行：

- `SubagentBatchSubmitter` 是一个 `Protocol`（`batch_runtime.py:25-30`）——三个方法：`submit`（交一批）、`get_batch`（查进度）、`cancel_batch`（取消）。Protocol 是 Python 的"鸭子类型接口"：不要求继承，只要长这样就能传入。
- Gateway 启动时 `set_subagent_batch_submitter(batch_service)` 把服务实例挂上去；关闭时 `set_subagent_batch_submitter(None)` 摘下来。工具侧永远只跟这个单例打交道，不 import 任何 app 层的东西——依赖方向保持"harness 不认识 app"。
- `_lock` 是 `threading.Lock` 而不是 `asyncio.Lock`，因为这个单例会被不同 OS 线程访问（IM 渠道线程有自己的事件循环，见 lifespan 篇的讨论）。锁只保护引用的读写，几十纳秒的事。

这个模式和 `run-manager` 篇讲的 `get_run_manager(request)` 从 `app.state` 取单例是同一个思想的两兄弟：一个挂在 FastAPI 的 `app.state` 上（HTTP 请求侧拿），一个挂在模块级全局变量上（agent 工具侧拿，因为工具代码在 harness 层，够不着 `app.state`）。

---

## 第 1 部分：总体架构——一张图看懂

```
 lead agent (一个正在跑的 run)
   │  发出 batch_task 工具调用
   ▼
 batch_task / batch_status / cancel_batch   ← tools/builtins/batch_task_tool.py
   │  校验（submitter 在不在、key 唯一、subagent 范围、thread_id…）
   │  打包 BatchSubmitRequest（含 execution_spec 快照）
   ▼
 进程全局提交器（batch_runtime.py 的单例）      ← harness 与 Gateway 之间的桥
   │
   ▼
 SubagentBatchService.submit()               ← subagents/batch_service.py
   │
   ▼
 SubagentBatchRepository.create_batch()      ← persistence/subagent_batches/sql.py
   │
   ▼
 ┌────────────── SQL（sqlite/postgres）──────────────┐
 │ subagent_batches (批次)   subagent_batch_items (条目) │
 └───────────────────────┬───────────────────────────┘
                         │ 轮询认领（lease）
                         ▼
 SubagentBatchService 后台 worker（asyncio 任务，lifespan 启动）
   │  每条 item:
   │    claim（租约 + attempt++）
   │    从 execution_spec 重建 SubagentConfig + 工具
   │    过统一容量闸门 SubagentExecutionCapacity.slot()
   │    跑 SubagentExecutor.execute_async()
   │    续租循环（lease_seconds/3）
   │    finalize（结果截断入库）
   ▼
 结果的两条出口：
   ① lead agent 用 batch_status 查进度（紧凑 JSON）
   ② HTTP API /api/threads/{id}/subagent-batches/... + results.jsonl 导出（前端面板）
```

一句话：**工具负责"接单和验单"，SQL 负责"记账"，worker 负责"干活和收尸"，HTTP 负责"对外看板"。** 四个角色通过两张表和两个单例解耦，谁也不依赖谁的进程内状态。

---

## 第 2 部分：`batch_task` 工具——接单和验单的每一道闸

文件：`backend/packages/harness/deerflow/tools/builtins/batch_task_tool.py`。

### 2.1 入参模型与工具绑定

```python
class BatchTaskItem(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=100_000)
```

（`batch_task_tool.py:27-29`）

每个 item 就两个字段：稳定的 `key`（幂等身份）和自包含的 `prompt`。注意 `max_length=100_000`——每条提示词上限 10 万字符，这是防呆：批量模式适合"每条都很小"的任务，一条提示词就顶一篇长文的场景不该走批量。

### 2.2 工具怎么找到提交器——显式绑定优先于全局单例

§0.5 说工具从进程全局单例拿提交器，但实际代码里多了一层（`batch_task_tool.py:32-47`）：

```python
_NO_EXPLICIT_BATCH_SUBMITTER = object()
_explicit_batch_submitter: ContextVar[SubagentBatchSubmitter | None | object] = ContextVar(
    "deerflow_explicit_subagent_batch_submitter",
    default=_NO_EXPLICIT_BATCH_SUBMITTER,
)


def _batch_submitter() -> SubagentBatchSubmitter | None:
    explicit = _explicit_batch_submitter.get()
    if explicit is not _NO_EXPLICIT_BATCH_SUBMITTER:
        return cast(SubagentBatchSubmitter | None, explicit)
    return get_subagent_batch_submitter()
```

逐行：

- `_explicit_batch_submitter` 是一个 `ContextVar`——协程版的"线程局部变量"，每个 asyncio 上下文有自己的值。
- 哨兵 `_NO_EXPLICIT_BATCH_SUBMITTER` 用一个独立的 `object()` 而不是 `None`，因为 `None` 本身是合法的显式值，含义是"这里**明确没有**提交器"。哨兵的含义则是"没人显式设置过，去看全局单例"。三个值、两种含义，必须分清。
- 为什么需要这层？`bind_batch_tools`（`:79-96`）的 docstring 说得很直白：直接用 SDK 造 agent 的调用方（不经过 Gateway）可以给工具绑一个**显式的提交器或提供器**。关键在 provider 是个函数——worker 停了之后，provider 返回 `None`，工具立刻报告"不可用"，**绝不会穿透到另一个应用的进程全局提交器**。如果只有全局单例，一个嵌入式 runtime 停服后工具会悄悄把任务交给宿主进程里恰好存在的提交器——任务跑在错误的账号、错误的配置下，这是很糟的事故。

这个"显式绑定 + provider 延迟解析"的模式，和 `run-manager` 篇讲过的 ContextVar 传 `execution_capacity` 是同一招：**生命周期敏感的资源，宁可每个调用现场解析一次，也不缓存在工具闭包里。**

### 2.3 `batch_task` 的校验链

`batch_task`（`:133-227`）按顺序过五道闸，任何一道不过就返回 `status="error"` 的 `ToolMessage`（不抛异常——工具错误要走模型可见的错误消息，让 lead 有机会改参数重试，这是全仓库工具的统一约定）：

**闸 1：提交器在不在**（`:157-163`）

```python
submitter = _batch_submitter()
if submitter is None:
    return _result(
        tool_call_id,
        content="Durable subagent batches are unavailable. Enable subagent_batches with a SQL database and restart Gateway.",
        error=True,
    )
```

错误消息把修复动作写全了：开配置、配 SQL、重启 Gateway。模型读到就能转告用户怎么做。

**闸 2：items 非空、key 唯一**（`:164-168`）。`len(set(keys)) != len(keys)` 是 O(n) 的重复检测。

**闸 3：subagent 类型在委托范围内**（`:173-182`）

```python
allowed_subagents = metadata.get("allowed_subagents")
available = get_available_subagent_names(app_config=app_config, allowed_subagents=allowed_subagents)
config = get_subagent_config(subagent_type, app_config=app_config)
if config is None or subagent_type not in available:
```

`allowed_subagents` 是"委托范围"功能（PR #4887）的核心：每个 custom agent 可以配置一个子代理白名单（`None`=全部、`[]`=全禁、列表=白名单），在**图组装时**快照进 run 的 metadata。工具侧每次都从 metadata 读快照、而不是去读可能已被管理员改掉的实时配置——**一次 run 内的权限边界必须恒定**，不能因为配置热更新而中途变宽。`task` 工具里的同一套检查在 `task_tool.py:315-338`，连 `"none permitted by caller policy"` 这个空白名单的报错文案都一致。

**闸 4：skill 白名单取交集**（`:184-186`）。lead 有自己的可用 skills 集合时，子代理的 skills 必须与父级取交集（`_merge_skill_allowlists`，`:124-130`）——子代理的权限永远不能比父级宽，这一条对批量模式尤其重要，因为一次提交就是 N 个执行体。

**闸 5：必须有线程**（`:188-190`）。批量结果要通过 `/api/threads/{thread_id}/subagent-batches` 查看，没有 `thread_id` 的批次无处归属。

### 2.4 `submission_key` 与 `execution_spec`——快照哲学

过了五道闸，构造提交请求（`:193-204`）：

```python
submission_key = f"{run_id or thread_id}:{tool_call_id}"
execution_spec = {
    "subagent_config": asdict(config),
    "parent_model": metadata.get("model_name"),
    "tool_groups": metadata.get("tool_groups"),
    "user_role": context.get("user_role"),
    "oauth_provider": context.get("oauth_provider"),
    "oauth_id": context.get("oauth_id"),
    "channel_user_id": context.get("channel_user_id"),
    "is_internal": context.get("is_internal") is True,
    "authz_attributes": normalize_authz_attributes(context.get("authz_attributes")),
}
```

`submission_key` 前面讲过——批次级幂等。真正值得停下来的是 `execution_spec`：**它是"这个子代理该怎么被重建"的完整快照**，序列化成 JSON 存进批次行。

为什么必须快照？因为**执行这些 item 的时刻，离提交可能隔着几小时、几次配置热更新、甚至一次 Gateway 重启**。如果 worker 执行时再去实时解析"这个 subagent 现在的配置是什么"，会遇到一串无法回答的问题：管理员刚把 timeout 从 900 改成 60，已提交的批次按哪个算？lead 的模型热切换了，子代理继承谁？用户权限变了，`authz_attributes` 用新的还是旧的？

快照的答案粗暴而正确：**提交那一刻是什么样，执行就按什么样。** 连用户的鉴权属性都冻结进 spec——batch worker 执行时以快照里的身份过沙箱/工具鉴权，而不是以"当前数据库里这个用户是什么角色"。

### 2.5 成功消息——一次教育模型的广播

提交成功后返回给 lead 的消息（`:223-227`）值得全文读一遍：

> Batch {id} accepted with N items. It is running independently and survives Gateway restarts. Use batch_status for progress; do not launch ordinary task calls for these items.

三个信息点：批次的**独立生命**（不随本 run 结束）、进度的**查询方式**（`batch_status`）、以及一条**负向禁令**（别再为这些 item 发普通 `task`）。为什么需要禁令？因为普通 `task` 的结果会回到 lead 上下文——lead 如果"好心"地把批量任务再用 `task` 逐个跑一遍，批量模式的全部意义（结果不占上下文）就毁于一旦。第 9 部分会看到，system prompt 里还有一整段更正式的规则来固化这个约定。

---

## 第 3 部分：两张表——持久化的最小充分设计

文件：`backend/packages/harness/deerflow/persistence/subagent_batches/model.py`；建表迁移 `migrations/versions/0016_subagent_batches.py`。

### 3.1 批次行 `subagent_batches`（`model.py:12-36`）

值得逐列看的字段：

- `submission_key` + 唯一约束 `uq_subagent_batches_user_submission (user_id, submission_key)`（`:34`）——§2.4 的批次幂等在数据库层的落点。重复提交会在 insert 时撞唯一约束，仓库层把它翻译成"返回已存在的批次"。
- `status`——批次状态机（§4），带索引。
- `max_live_items` / `max_running_items` / `max_attempts`——**提交时冻结**的限量参数（不是执行时读配置）。和 `execution_spec` 同一个快照哲学。
- `execution_spec`(JSON)——§2.4 的重建快照。

### 3.2 条目行 `subagent_batch_items`（`model.py:39-71`）

- `item_key` / `position` 各有一个唯一约束（`:68-69`）——key 管幂等，position 管顺序稳定（claim 和导出都按 position 排）。
- `lease_owner` / `lease_expires_at`——租约两件套。
- `attempt`——重试预算计数器。
- `cancel_requested_at`——**取消是"请求"不是"命令"**。取消一个正在别的 worker 手里跑的 item，本 worker 无法直接杀掉它（进程都不在一起），只能盖一个"请求取消"的时间戳，由持有者或租约收尸逻辑观察到后执行。这是分布式取消的标准姿势。
- `result` / `result_preview` / `result_truncated` / `error` / `stop_reason` / `token_usage`——终态产物。注意 `result` 有 `max_result_chars`（默认 10 万字符）的入库截断、`result_preview`（默认 2 千字符）给列表页用——**数据库不是垃圾桶，大结果要有界**。
- 索引 `ix_subagent_batch_items_claim (status, lease_expires_at, batch_id)`（`:70`）——专为 worker 的认领查询设计：按状态 + 租约过期时间扫可认领条目。

---

## 第 4 部分：状态机——两层各转各的

### 4.1 状态词表

仓库层集中定义（`persistence/subagent_batches/sql.py:15-18`）：

```python
BATCH_ACTIVE_STATUSES = ("queued", "running", "paused")
BATCH_TERMINAL = ("completed", "failed", "cancelled")
ITEM_ACTIVE = ("queued", "leased", "running")
ITEM_TERMINAL = ("succeeded", "failed", "cancelled")
```

### 4.2 批次状态机

```
queued ──▶ running ──▶ completed（全部条目终态且无失败）
   │          │    └──▶ failed（有失败且零成功，_refresh_batch_status 的判定）
   │          └──▶ cancelled（cancel_batch）
   └──▶ paused（暂停，不认领）──▶ running（resume）
```

细节：`failed` 只在"有失败**且**零成功"时判（`sql.py` 的 `_refresh_batch_status`）——990 条成功、10 条失败的批次是 `completed`（带着 10 条 failed 的 item），不是 `failed`。批次状态描述的是"这批活儿还等不等得起"，不是"有没有任何瑕疵"。

### 4.3 条目状态机

```
pending ──▶ queued ──▶ leased ──▶ running ──▶ succeeded
               ▲                    │   └──▶ failed / cancelled
               │                    │
               └── 租约过期收尸（attempt 未耗尽）◀──┘
                    （attempt 耗尽则 → failed；有取消请求则 → cancelled）
```

三个不显眼但重要的设计：

1. **`pending` 和 `queued` 分开**。提交时 N 个条目全是 `pending`（"已入库但未放行"）。worker 每轮按批次的 `max_live_items`（活跃窗口，默认 100）把最早的一批 `pending` 晋升为 `queued`（"可以认领了"）。**总量 5000 ≠ 同时活跃 5000**——live window 是防止一万个条目同时抢租约、把数据库和模型配额打爆的节流阀。
2. **`leased` 和 `running` 分开**。`leased` = 已被某个 worker 认领、正在初始化（重建配置、装工具）；`running` = 真正开始执行（executor 报告 RUNNING 后由 `mark_item_running` 落库）。分开的意义：租约过期收尸时，一个卡在"leased 三小时"的条目明显是 worker 死了；而 running 的条目如果还在续租就是活的。
3. **`failed` 的条目可以 `retry_item`**（仓库层 `retry_item`，REST 暴露为 `POST .../items/{item_id}/retry`）——手工重试走独立的计数通道，不与自动重试混用。

---

## 第 5 部分：SubagentBatchService——worker 的骨架

文件：`backend/packages/harness/deerflow/subagents/batch_service.py`。app 层的 `backend/app/subagent_batches/service.py` 只是一行兼容再导出（`from deerflow.subagents.batch_service import SubagentBatchService`），真正的类在 harness 层。

### 5.1 构造与身份

```python
class SubagentBatchService:
    """Lease, execute, and recover durable native-subagent batch items."""

    def __init__(self, *, repository, config, runtime_config, app_config=None,
                 execution_capacity=None) -> None:
        ...
        self._lease_owner = f"{socket.gethostname()}:{uuid.uuid4().hex}"
        self._executions: dict[str, asyncio.Task[None]] = {}
        self._execution_ids: dict[str, str] = {}
        self._item_batches: dict[str, str] = {}
```

（`batch_service.py:37-59`）

`_lease_owner` 是 worker 的身份：主机名 + 随机 hex。**每次进程启动都是新身份**——即使同一台机器重启，旧租约的 owner 也对不上了，旧租约只能靠过期收尸，不能靠"身份相同"续。这是刻意的：进程重启后内存里的 executor 状态已丢，"续"是不可能的，只有"重新认领"。

三个内存字典是本进程正在执行的 item 的登记簿（`_executions`：asyncio 任务；`_execution_ids`：item → executor 执行 ID；`_item_batches`：item → 批次 ID，取消时反查用）。**注意它们只是"加速索引"，不是真相之源**——真相在 SQL。进程死了，字典没了，SQL 里的租约过期后一切照旧。这就是 durable 的分层：内存只做快，SQL 做对。

### 5.2 启停（`batch_service.py:61-84`）

`start()` 起一个名为 `subagent-batch-poller` 的 asyncio 任务跑 `_run` 循环。`stop()` 做三件事，顺序有讲究：

1. 停 poller（先止血：不再认领新 item）；
2. 对每个还在跑的执行 `request_cancel_background_task(execution_id)` + cancel 掉 asyncio 任务；
3. 清空登记簿。

被 cancel 的 `_execute_item` 会走到 CancelledError 分支——那里**故意不 finalize**（§7.5），租约留在数据库里等过期，另一个 worker（或重启后的自己）收尸重跑。

### 5.3 轮询循环（`batch_service.py:86-100`）

```python
async def _run(self) -> None:
    while not self._stop.is_set():
        try:
            await self.run_once(now=datetime.now(UTC))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Subagent batch scheduler pass failed")
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self._config.poll_interval_seconds)
        except TimeoutError:
            pass
```

和 `run-manager` 的心跳循环、调度器循环同款写法：一轮的异常**必须**被吞掉并记日志（一轮失败不能杀死整个 worker），睡眠用 `wait_for(stop.wait(), timeout=...)` 而不是 `asyncio.sleep`——这样 `stop()` 一按铃，睡眠立刻中断，关机不用等下一个 poll 间隔。

### 5.4 `run_once`——一轮认领（`batch_service.py:102-126`）

```python
async def run_once(self, *, now: datetime) -> None:
    available = max(0, self._runtime_config.max_running - len(self._executions))
    if available <= 0:
        return
    items = await self._repository.claim_items(
        now=now, lease_owner=self._lease_owner,
        lease_seconds=self._config.lease_seconds, limit=available,
    )
    for item in items:
        ...
        task = asyncio.create_task(self._execute_item(item), name=f"subagent-batch-item-{item_id}")
        self._executions[item_id] = task
        task.add_done_callback(...)
```

第一个数字值得停一下：`available = max_running - len(self._executions)`。这个 `max_running` 来自 `SubagentRuntimeConfig`——就是普通 `task` 子代理用的那个**进程统一容量**的上限（§8）。worker 只认领"本进程还有空位"数量的 item，认领多少跑多少，绝不超卖。每认领一条就 `create_task` 一条独立协程 + 登记进 `_executions`，`add_done_callback` 保证任务结束（无论成败）自动从登记簿除名。

### 5.5 `submit` 与 `cancel_batch`（`batch_service.py:128-154, 164-179`）

`submit` 把工具层传来的请求做最后一道上限校验（条目总数、live window、并发数的默认值与上限关系），然后 `create_batch` 落库，批次 ID 形如 `subagent-batch-{uuid}`。

`cancel_batch` 的两半结构是本篇最重要的设计之一：

```python
batch = await self._repository.cancel_batch(batch_id, user_id=user_id)
if batch is None:
    return None
for item_id, execution_id in list(self._execution_ids.items()):
    if self._item_batches.get(item_id) == batch_id:
        request_cancel_background_task(execution_id)
```

注释原文说得很清楚："Normal ids are not prefixed; the renew loop observes the durable cancellation within lease_seconds/3. Keeping cancellation durable is what lets another worker own the HTTP control request safely."（`batch_service.py:176-178`）

拆开讲：取消请求可能从**任何一个 Gateway 实例**的 HTTP 接口进来，而 item 正在**另一个实例**的内存里跑。所以取消必须拆成两半：

- **持久半**：`cancel_batch` 在 SQL 里把 `cancel_requested_at` 盖章。这一半跨进程可见。
- **本地半**：本进程恰好持有这个批次的执行时，顺手直接取消（快路径）。

如果 item 在别的进程：那个进程的 `_execute_item` 续租循环（每 `lease_seconds/3`，默认 40 秒内）下一轮就会从数据库读到取消请求并自杀；如果持有者已经死了，租约收尸逻辑看到 `cancel_requested_at` 非空直接判 `cancelled`（§6 的三分支之一）。**"请求-观察"代替"命令-执行"，是跨进程控制的标准解。**

---

## 第 6 部分：`claim_items`——数据库里最复杂的一个函数

文件：`persistence/subagent_batches/sql.py:230-335`。它一个事务里干四件事，顺序敏感：

### 6.1 锁批次

```python
batches = list((await session.execute(
    select(SubagentBatchRow)
    .where(SubagentBatchRow.status.in_(("queued", "running")))
    .order_by(SubagentBatchRow.created_at, SubagentBatchRow.id)
    .with_for_update(skip_locked=True)
)).scalars())
```

（`sql.py:243`）

`FOR UPDATE SKIP LOCKED` 是所有"多 worker 抢任务"实现的基石：加行锁，但**锁不上的行直接跳过**（别的 worker 正在处理），绝不阻塞等待。没有 SKIP LOCKED，N 个 worker 轮询同一批行会互相排队甚至死锁；有了它，每个 worker 拿到的是互不相交的子集。`run-manager` 的接管、调度器的 `claim_due_tasks` 用的都是同一个原语。

### 6.2 收尸：租约过期的条目（`sql.py:248-274`）

对本批次每个 `status ∈ (leased, running)` 且 `lease_expires_at < now` 的条目，三分支处置：

```python
if item.cancel_requested_at is not None:
    item.status = "cancelled"
elif item.attempt >= batch.max_attempts:
    item.status = "failed"
    item.error = item.error or "Execution lease expired after the maximum retry count"
else:
    item.status = "queued"
    item.error = "Previous worker lease expired; retrying"
```

优先级依次是：取消请求 > 重试预算耗尽 > 重新排队。注意重试不重置 `attempt`（认领时才 `+1`），所以第 4 次过期必然落到 `failed`——重试预算是防无限循环的硬顶。

### 6.3 放行：live window 晋升（`sql.py:276-296`）

数出当前活跃数（queued + leased + running），`promote_count = max_live_items - live`，从 `pending` 里按 `position` 顺序晋升这么多个到 `queued`。这就是 §4.3 说的节流阀，实现在认领的同事务里，天然一致。

### 6.4 认领（`sql.py:298-333`）

```python
counts = await self._counts(session, batch.id)
batch_available = max(0, batch.max_running_items - counts["leased"] - counts["running"])
take = min(limit - len(claimed), batch_available)
...
for item in runnable:
    item.status = "leased"
    item.attempt += 1
    item.lease_owner = lease_owner
    item.lease_expires_at = expires_at
```

三层预算取最小：本进程剩余容量（`limit - len(claimed)`）× 批次自身并发上限（`max_running_items`，**按批次隔离**——一个大批次不能吃光 worker 的全部算力，别的批次也要有得跑）× 实际排队的条目数。认领动作就是 §0.3 的租约三件套 + `attempt += 1`。最后有认领发生时把批次置 `running`。

整个函数一个事务提交——中途任何一步失败，全部回滚，不会有"收了尸但没认领"的中间态。

---

## 第 7 部分：`_execute_item`——一条 item 的一生

`batch_service.py:181-320`，本服务的心脏。

### 7.1 从快照重建执行体（`:184-219`）

```python
spec = batch["execution_spec"]
config = SubagentConfig(**spec["subagent_config"])
...
tools = get_available_tools(
    groups=spec.get("tool_groups"),
    model_name=effective_model,
    subagent_enabled=False,
    include_upload_tool=False,
    app_config=app_config,
)
executor = SubagentExecutor(config=config, tools=tools, ..., authz_attributes=spec.get("authz_attributes"), execution_capacity=self._execution_capacity)
```

§2.4 存的快照在这里被反序列化：子代理配置、工具组、模型解析、用户身份、鉴权属性，全部按提交时刻重建。两个布尔参数体现批量 worker 的身份定位：`subagent_enabled=False`（批量 item 是叶子执行体，**不允许再委派**，否则一个批次能指数裂变）、`include_upload_tool=False`（叶子不需要给用户传文件）。最后传入本服务的 `execution_capacity`——和普通 `task` 共用同一个容量闸门（§8），这是"统一容量"的落点。

### 7.2 幂等 preamble——把 at-least-once 说给模型听（`:220`）

```python
prompt = (f"Durable batch item key: {item['item_key']}\n"
          "This item may be retried after a worker crash. Keep side effects idempotent "
          "and use the item key as the idempotency identity.\n\n" + item["prompt"])
```

§0.3 说 at-least-once 要求业务幂等，谁来保证？**把责任显式写进子代理的任务描述**：你的 key 是什么、你可能被重试、副作用必须幂等、用 key 做幂等身份。模型不是可靠的幂等执行器，但一个被明确告知"你可能重跑"的模型，写文件会想到先查、发请求会带上 key——比完全不告知强一个数量级。

### 7.3 执行 + 续租循环（`:221-265`）

```python
execution_id = executor.execute_async(prompt, task_id=item_id)
self._execution_ids[item_id] = execution_id
marked_running = False
renew_every = max(1.0, self._config.lease_seconds / 3)
...
while True:
    result = get_background_task_result(execution_id)
    ...
    if result.status is SubagentStatus.RUNNING and not marked_running:
        marked_running = await self._repository.mark_item_running(item_id, lease_owner=self._lease_owner, now=datetime.now(UTC))
        if not marked_running:
            request_cancel_background_task(execution_id)
    if result.status.is_terminal:
        break
    if now_monotonic >= next_renew_at:
        lease = await self._repository.renew_item_lease(item_id, lease_owner=self._lease_owner, lease_seconds=self._config.lease_seconds, now=datetime.now(UTC))
        next_renew_at = loop.time() + renew_every
        if not lease["valid"]:
            request_cancel_background_task(execution_id)
    ... 睡 min(status_poll_every, until_renew) ...
```

四个要点：

1. **`execute_async` 是提交到隔离执行循环，不阻塞**——和普通 `task` 走的是同一个 executor（`run-agent-deep-dive.md` 讲过它的隔离循环设计）。worker 协程自己进入一个**观察循环**：查结果、更新租约、睡觉。
2. **`renew_every = lease_seconds / 3`**——租约 120 秒，40 秒续一次。为什么是 1/3？一次续租失败还有两次机会；就算进程假死 40 秒，租约也不会被误判过期。这和调度器、run 心跳的续租比例是同一个经验值。
3. **`mark_item_running` 的返回值被检查**：`marked_running=False` 意味着数据库侧不认这次状态推进（比如租约已被收尸、条目已归别人）——本地执行立刻取消。**数据库说了算，本地执行体是租约的租客，不是主人。**
4. **续租返回 `valid=False` 同理**：租约失效（过期被收尸/条目被取消），立即 `request_cancel_background_task`。宁可白跑，不可两跑——其实还是会有短暂的_double-run 窗口（§0.3），但主动自堕能把它压到最小。

### 7.4 终态处理：admission 失败不烧重试预算（`:267-291`）

```python
if getattr(result, "admission_failure", False):
    await self._repository.requeue_item_after_admission_failure(item_id, lease_owner=self._lease_owner, error=result.error, now=datetime.now(UTC))
    return
truncated = len(raw_result) > self._config.max_result_chars
...
await self._repository.finalize_item(item_id, lease_owner=self._lease_owner, succeeded=result.status is SubagentStatus.COMPLETED, result=stored_result, result_preview=preview, result_truncated=truncated, error=result.error, stop_reason=result.stop_reason, token_usage=_usage(result.token_usage_records), model_name=effective_model, completed_at=datetime.now(UTC))
```

`admission_failure` 是 §8 的伏笔：item 在容量闸门那里被拒了（池满被拒/排队超时），**一个 slot 都没占到、模型一个 token 都没花**——这不算执行失败，不该烧 `attempt`。所以走专门的 `requeue_item_after_admission_failure` 回到 `queued`，等下一轮窗口。终态才 `finalize_item`：成功与否、结果（截到 10 万字符）、预览（2 千字符）、错误、停止原因、token 用量、实际用的模型，一次写全。

### 7.5 进程关闭时：故意不 finalize（`:292-297`）

```python
except asyncio.CancelledError:
    if execution_id is not None:
        request_cancel_background_task(execution_id)
    # Do not finalize on process shutdown. The durable lease expires and
    # another worker reclaims the same stable item key.
    raise
```

这是全文最反直觉的一段，注释值得逐字读：**进程关闭（stop/重启/崩溃）时不把 item 标成失败或中断，而是什么都不写，让租约静静过期。**

为什么不写"中断"？因为"进程在关闭"≠"这批工作该作废"。用户提交的批次期望它跑完——Gateway 重启后，worker 回来看到过期租约，收尸、重新排队、接着跑。这就是 durable 的完整闭环：**关机只是暂停，租约过期是恢复的交接棒**（这条设计与调度器"重启恢复"一脉相承）。本地执行体倒是立刻取消了——反正这个进程马上就没了，别浪费电。

与之对照，普通异常（`:298-315`）才是真正的失败：记日志、finalize 成 `failed`，错误消息截 4 千字符。

---

## 第 8 部分：统一执行容量——#4998 的另一半

PR #4998 的标题是 "unified capacity **and** durable batch execution"——前半部分（容量）是后半部分（批量）的地基，也是普通 `task` 和批量 item 能公平共处的前提。

### 8.1 为什么要"统一"

改造前，子代理并发有**三个各自为政的数字**：prompt 里告诉模型的"最多并行 N 个"、`SubagentLimitMiddleware` 强制的上限、executor 里 `ThreadPoolExecutor(max_workers=3)` 的线程池大小。三个数字来源不同、默认不同、改配置时未必同步——模型被告知 5，中间件放行 5，线程池只有 3，多出来的两个在等线程时白占内存。

#4998 把三处收敛成**一个进程级异步 FIFO 控制器**：`SubagentExecutionCapacity`（`backend/packages/harness/deerflow/subagents/capacity.py:36-107`），配置源是 `SubagentRuntimeConfig`（`config/subagent_runtime_config.py:8-32`）：

```python
max_running: int = Field(default=3, ge=1, le=64, ...)
max_queued: int = Field(default=64, ge=0, le=10_000, ...)
admission_policy: Literal["queue", "reject"] = Field(default="queue", ...)
queue_timeout_seconds: int = Field(default=300, ge=1, le=86_400, ...)
```

**普通 `task` 的子代理和批量 item 过的是同一个闸门**——批量模式开 100 个 item，worker 也只按 `max_running` 认领（§5.4），不会把交互式 `task` 饿死。

### 8.2 容量控制器的三个精妙点（`capacity.py:54-107`）

**点一：等待者不占线程。** `_acquire` 拿不到 slot 时创建一个 `Future` 排进 FIFO 队列然后 `await`——等待的协程不占任何线程或 slot，这正是老线程池方案"每个等待任务占一个线程"的反面（对照 `run-manager` 篇讲的"主动检查比被动占坑好"）。

**点二：超时与放行的竞态处理**（`:71-83`）：

```python
except (TimeoutError, asyncio.CancelledError) as exc:
    async with self._lock:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            # A release transferred the slot just as the timeout fired.
            # If the future completed, this caller owns that transfer and
            # must release it before reporting the timeout.
            if waiter.done() and not waiter.cancelled():
                self._release_locked()
```

排队超时的瞬间，恰好有人释放并把 slot **直转**给了这个 waiter——`remove` 抛 `ValueError`（已不在队列，因为已被弹出），此时 future 已完成，说明 slot 所有权已经到了自己手上。直接报超时会造成 slot 泄漏（计数永远少一），所以必须先 `_release_locked()` 归还再报超时。这种"超时与让渡赛跑"的竞态在异步系统里极常见，这里是教科书级处理。

**点三：释放时槽位直转**（`_release_locked`，`:85-95`）：

```python
def _release_locked(self) -> None:
    while self._waiters:
        waiter = self._waiters.popleft()
        if waiter.done():
            continue
        # Transfer the existing slot; _running intentionally stays flat.
        waiter.set_result(None)
        return
    ...
    self._running -= 1
```

有等待者时，释放不做"计数减一、唤醒、再竞争"三步，而是把**现有 slot 的所有权直接移交**给队首 waiter（`_running` 保持不变）。省掉一次唤醒-再抢的往返，且绝对公平（严格 FIFO）。

### 8.3 一个值三处用

`effective_subagent_concurrency`（`config/subagents_config.py:26-36`）把 `subagent_runtime.max_running` 解析成一个数，同时喂给：lead 的 system prompt（模型看到的并行上限）、`SubagentLimitMiddleware`（硬性截断）、`SubagentExecutionCapacity`（真实闸门）。**说的、管的、真的一比一。** 容量在启动时冻结（`configure_subagent_execution_capacity`，`capacity.py:116-132`，有执行体活跃时拒绝改配置——运行中改闸门等于把人关在门外）。

### 8.4 admission_failure 如何回到工具层

executor 的入口（`subagents/executor.py:899-922`）：

```python
try:
    capacity = self.execution_capacity or get_subagent_execution_capacity()
    async with capacity.slot():
        ... result.status = SubagentStatus.RUNNING ...
        return await self._aexecute_admitted(task, result)
except SubagentCapacityError as exc:
    result.try_set_terminal(SubagentStatus.FAILED, error=str(exc), admission_failure=True)
    return result
```

注意状态翻转的位置：**进了 slot 才标 RUNNING**。排队中的子代理状态一直是 PENDING——"排队"和"在跑"在状态上就分开了。容量错误打上 `admission_failure=True` 标记，批量 worker 据此走"不烧 attempt"的回队路径（§7.4）。普通 `task` 路径则把这个失败作为普通失败返回给 lead。

---

## 第 9 部分：怎么用——配置、prompt 规则、HTTP 看板

### 9.1 打开开关

`config.yaml`（模板 `config.example.yaml` 的 `subagent_batches:` 段）：

```yaml
subagent_batches:
  enabled: true          # 默认 false：开它会显著增加模型用量，且要求 SQL
poll_interval_seconds: 1.0
lease_seconds: 120
max_items_per_batch: 5000
...
```

两个前置条件在 lifespan 里强制（`backend/app/gateway/app.py:410-428`）：`enabled=true` 但没有 SQL 仓库直接 `RuntimeError`（fail-fast，启动即报错而不是第一个 batch 提交时才炸）；服务创建后 `set_subagent_batch_submitter` 挂全局提交器、`app.state.subagent_batches_available = True`。关机时对称地停止服务、摘提交器（`app.py:473-482`）。配置类 `SubagentBatchesConfig`（`config/subagent_batches_config.py:6-31`）带一个 model_validator 保证 default ≤ max 的四组关系不会配反。

### 9.2 lead 的 system prompt 规则

批量开启时，prompt 组装器注入一整段"显式批量模式"说明（`agents/lead_agent/prompt.py:432-449`），核心四条：

> - Every item must be self-contained and must not depend on another item's output.
> - Give every item a stable unique key; retries reuse that key as idempotency identity.
> - Set total, live-window, and running concurrency separately. A high total never implies that all items become live or run at once.
> - Do not wait for or paste all item results into this run. The Web UI and results export API own progress and result inspection.

外加一句总纲："**Never infer batch mode from item count and never emulate it by repeatedly calling `task`**"（不要因为条目多就自作主张走批量，也不要用循环调 `task` 模拟批量）。这段 prompt 和 §2.5 的成功消息一唱一和，把批量模式的使用边界写进模型的世界观。

### 9.3 HTTP 看板与前端

路由挂在 `/api/threads/{thread_id}/subagent-batches`（`backend/app/gateway/routers/subagent_batches.py:19`）：列表（`:39-43`）、详情（`:46-50`）、条目分页、`pause`/`resume`/`cancel`、条目级 `retry`、以及 `results.jsonl` 流式导出（NDJSON，逐条 item 一行 JSON）。所有路由 `require_permission(..., owner_check=True)` + `_owned_batch` 双重校验（`:30-36`）——batch 必须属于这个 user 且属于这个 thread，防止越权枚举。

`/api/features` 里有一个 `subagent_batches` 特性对象（`routers/features.py:39, 54, 66, 74`），把 `worker_running`、仓库可用性、`configured_subagent_max_running()` 暴露给前端——前端工作区面板（`frontend/src/components/workspace/thread-subagent-batches.tsx`）据此决定渲染批量进度卡片还是隐藏入口。**结果不进 lead 上下文，全靠这个看板消费**——这是"批量的结果归 UI 管"约定的人类侧。

### 9.4 SDK 直连的护栏

不用 Gateway、直接 `create_deerflow_agent` 造 agent 的调用方，如果想用批量工具，必须显式提供一个跑着的 `SubagentRuntime`——工厂里的护栏（`agents/factory.py:148`）：

> The explicit durable batch worker is not running; await subagent_runtime.start() or enter it with 'async with' before calling create_deerflow_agent

宁可在组装时报错，不让工具在运行时才发现提交器是 `None`。

---

## 第 10 部分：普通 `task` vs `batch_task`——一张表说清

| | 普通 `task` | `batch_task` |
|---|---|---|
| 提交后 | lead 挂起等结果 | 立即返回批次 ID |
| 生命周期 | 随 run 而死 | 独立，活过 run、活过重启 |
| 结果去向 | ToolMessage 进 lead 上下文 | SQL + 前端看板 + JSONL 导出，**不进**上下文 |
| 数量约束 | `max_total_per_run`（默认 6）+ 单响应并行上限 | 单批最多 5000，live window 节流 |
| 失败语义 | 一次执行 | at-least-once，`max_attempts` 次重试 |
| 崩溃恢复 | 无（陪 run 一起没） | 租约过期 → 重新排队 → 接管 |
| 执行并发 | 统一容量闸门 | **同一个**统一容量闸门 |
| 适合 | 少量、需要结果继续推理 | 大量、独立、幂等/只读、结果后置查看 |

---

## 配置项速查

| 配置 | 默认 | 约束 | 定义处 |
|---|---|---|---|
| `subagent_batches.enabled` | `false` | — | `subagent_batches_config.py:9` |
| `.poll_interval_seconds` | `1.0` | 0.1–60 | `:10` |
| `.lease_seconds` | `120` | 10–3600 | `:11` |
| `.max_items_per_batch` | `5000` | 1–100000 | `:12` |
| `.default_max_live_items` | `100` | 1–10000 | `:13` |
| `.max_live_items_per_batch` | `1000` | 1–100000 | `:14` |
| `.default_max_running_items` | `3` | 1–64 | `:15` |
| `.max_running_items_per_batch` | `64` | 1–1000 | `:16` |
| `.max_attempts` | `3` | 1–10 | `:17` |
| `.max_result_chars` | `100000` | 1000–1000000 | `:18` |
| `.result_preview_max_chars` | `2000` | 64–100000 | `:19` |
| `subagent_runtime.max_running` | `3` | 1–64 | `subagent_runtime_config.py:11-16` |
| `subagent_runtime.max_queued` | `64` | 0–10000 | `:17-22` |
| `subagent_runtime.admission_policy` | `queue` | queue/reject | `:23-26` |
| `subagent_runtime.queue_timeout_seconds` | `300` | 1–86400 | `:27-32` |

前 11 个是批量 worker 专属（重启生效）；后 4 个是统一容量，普通 `task` 同样受它约束。

---

## 测试在哪

- `backend/tests/test_batch_task_tool.py` —— 工具校验链、幂等 key、execution_spec 快照、不可用降级
- `backend/tests/test_subagent_batch_service.py` —— worker 提交/认领/续租/取消/关机不 finalize
- `backend/tests/test_subagent_batch_repository.py` —— claim_items 三分支收尸、live window、批次状态刷新
- `backend/tests/test_subagent_batches_router.py` —— owner 校验、pause/resume/retry、JSONL 导出
- `backend/tests/test_subagent_execution_capacity.py` / `test_subagent_capacity_config.py` —— FIFO、超时竞态、配置冻结
- `backend/tests/test_subagent_runtime.py` / `test_subagent_executor.py` / `test_subagent_limit_middleware.py` —— Runtime 装配、admission_failure、三处一值
- 前端：`frontend/tests/unit/core/subagent-batches/`、批量面板 DOM 测试

---

## 本篇小结

回到标题的问题：为什么"派一千个子代理"不能只是一次 for 循环调 `task`？

- **for 循环的结果会淹死 lead 的上下文** → 批量结果只进 SQL 和看板（§2.5、§9.3）
- **for 循环随 run 一起死** → 状态进 SQL，`SubagentBatchService` 按租约认领、按 `execution_spec` 快照重建（§2.4、§6、§7.1）
- **for 循环没有崩溃恢复** → 租约过期收尸 + `attempt` 重试预算 + at-least-once + 幂等键写进提示词（§0.3、§6.2、§7.2）
- **for 循环没有公平性** → 一个进程一个容量闸门，`task` 和批量 item 同门进出；批次间还有 per-batch 并发上限（§8、§6.4）
- **for 循环不知道"关机不是作废"** → shutdown 故意不 finalize，租约过期是恢复的交接棒（§7.5）

`SubagentBatchService` 与姊妹篇的关系可以一句话收束：`RunManager` 让**一个 run** 活过一次 HTTP 请求；`SubagentBatchService` 让**run 里派出去的一批工作**活过 run 本身。两层解耦用的是同一套词汇——注册表、租约、状态机、收尸——只是把守护对象从 run 换成了批量条目。

下一篇姊妹篇 `mcp-task-service-deep-dive.md` 讲同一问题的第三个实例：MCP 长时工具调用的持久任务运行时。
