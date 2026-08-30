# McpTaskService 深度讲解：run 都结束了，那个还没跑完的 MCP 工具调用谁来管——持久任务运行时的提交、轮询与通知

本文是 `subagent-batch-service-deep-dive.md` 的姊妹篇，也是 `run-manager-deep-dive.md` 的隔代亲戚。

这个家族的文章都在回答同一个母题的变体：**"某件工作"和"承载它的 HTTP 请求 / agent run"寿命不一致，怎么办？**

- `RunManager`：run 要活得比创建它的 HTTP 请求久。
- `SubagentBatchService`：run 里派出去的一批子代理工作，要活得比 run 本身久、活过 Gateway 重启。
- 本篇的 `McpTaskService`：**agent 调用了一个长耗时的 MCP 工具（比如远程服务器上要跑半小时的数据任务），agent 循环等不起，run 也大概率等不到它跑完——run 结束之后，谁来盯这个远程任务的状态？谁把它完成的消息送回对话？**

这就是 2026 年 8 月合入的 MCP 持久任务运行时：地基是 PR #4665（durable task runtime foundation，8 月 8 日），随后 #4690（ordinary driver 与 agent 侧工具改装）、#4833（通知运行时）补齐，8 月下旬又有三个韧性修复（#4933、#4963、#5027）。

建议先读 `subagent-batch-service-deep-dive.md` 的第 0 步（durable、lease、at-least-once 三个概念那里讲得更细）。本篇复用结论，把篇幅留给这套运行时**独有的东西**：三套互相独立的租约、版本化的事件流、以及"用一个真正的 agent run 来当通知通道"这个别处看不到的设计。

---

## 第 0 步：先把要用的术语讲清楚

### 0.1 问题的形状：一个等不起的工具调用

MCP（Model Context Protocol）让 agent 能调用外部服务器上的工具。绝大多数工具是"问一句答一句"：查个天气、读个文件，秒回。但有一类服务器暴露的是**任务型工具**：你提交一个请求，它回你一个"任务已开始"的凭证，真正的工作在远程服务器上跑几分钟到几小时，状态要用另一个工具去查。

对 agent 循环来说，这类工具是个陷阱：

- **同步等它**：lead agent 挂在一个 `ToolMessage` 上等半小时，期间用户不能交互、run 的每一秒都在烧 recursion_limit、Gateway 重启一切归零。
- **提交完就不管**：agent 拿到"任务已开始"的凭证，run 顺理成章地结束了——然后没有任何东西记得去查那个任务。用户过一小时问"跑完了吗"，谁也答不上来。

所以需要一个**不属于任何 run 的中间人**：agent 在 run 内把远程任务登记给它，run 结束；中间人后台轮询远程状态，任务到了关键节点（完成、失败、需要用户输入）就把消息**投递回对话线程**。这个中间人就是 `McpTaskService`。

### 0.2 普通 MCP 工具 vs task toolset（三件套）

MCP 服务器的配置在 `extensions_config.json` 里。持久任务运行时引入了一个新配置块——**task toolset**（`McpTaskToolsetConfig`，`backend/packages/harness/deerflow/config/extensions_config.py:75-95`）：

```python
name: str          # 本地任务名（1..255，稳定标识）
submit_tool: str   # 原始"提交任务"工具名
status_tool: str   # 原始"查状态"工具名
cancel_tool: str   # 原始"取消任务"工具名
```

一个 task toolset 就是把远端 MCP 服务器上**本来就存在的三个工具**（提交/查状态/取消）登记为一组。登记之后，agent 看到的工具面会发生外科手术式的改变（§2 逐行讲）：`submit` 被替换成持久包装版（立即返回本地任务 ID），`status` 和 `cancel` **对 agent 隐藏**——轮询是运行时的事，不再劳烦 agent。

没有登记 task toolset 的服务器，工具照旧同步调用，与持久运行时无关。

### 0.3 协议中立的 driver——运行时不认识任何具体 MCP 服务器

持久运行时的核心原则是**协议中立**：`McpTaskService` 不知道"MCP 服务器长什么样"，它只认识一个三方法接口（`backend/packages/harness/deerflow/mcp/tasks/driver.py:8-15`）：

```python
class McpTaskDriver(Protocol):
    """Transport/protocol adapter used by the protocol-neutral task runtime."""

    async def submit(self, request: TaskSubmitRequest) -> TaskSubmission: ...

    async def get_status(self, task: TaskReference) -> TaskSnapshot: ...

    async def cancel(self, task: TaskReference) -> TaskSnapshot: ...
```

和 `subagent-batch` 篇的 `SubagentBatchSubmitter` 一样，`Protocol` 是鸭子类型接口。目前只有一个实现——**ordinary driver**（`tasks/ordinary.py:148-218`，注册名 `"ordinary-tools"`），它把三个方法翻译成对原始 MCP 工具的调用。将来出现"原生支持任务协议"的 MCP 服务器，只需再写一个 driver，运行时零改动。

driver 之间靠 `McpTaskDriverRegistry`（`driver.py:18-36`）按名字注册/查找，重复注册同名 driver 直接拒绝——防止两个实现静默覆盖。

ordinary driver 的线上契约值得单独一提（`ordinary.py:39-78`）：它要求远端工具把机器可读的载荷放在 MCP 响应的 **`structuredContent`** 字段里（提交必须返回 `task_id` + `status: "running"`），**永远不去解析文本内容**（`:112-115`）。文本是给人看的，结构化字段才是给程序看的——靠解析自然语言提取任务 ID，迟早被一句措辞变化弄崩。

### 0.4 三套互相独立的租约

lease 的基础概念（共享单车比喻）见 `run-manager-deep-dive.md` §0.6 和 `subagent-batch` 篇 §0.3。本篇的新东西是：`mcp_tasks` 一张表上同时跑着**三种后台工作**——轮询状态、执行取消、投递通知——每种工作有**自己独立的一组租约列和调度列**（`persistence/mcp_tasks/model.py:50-60`）：

- 轮询：`lease_owner` / `lease_expires_at` + 调度列 `next_poll_at`
- 取消：`cancel_requested_at` / `next_cancel_at` / `cancel_attempt_count`
- 通知：`notification_lease_owner` / `notification_lease_expires_at` + `next_notification_at`

为什么不像 batch 那样一条租约管到底？因为三种工作的节奏完全不同：轮询要按远端建议的间隔反复来（`next_poll_at` 一直变）；取消通常一辈子只发生一次；通知只在关键节点发生。**一种工作一套租约，一种工作一个状态列**，谁也不阻塞谁——轮询卡死不影响取消执行，通知重试不占用轮询预算。

### 0.5 版本化事件——通知系统的记账基础

任务状态会一路变化：`working → working → input_required → working → completed`。哪些变化值得打扰用户？同一个节点会不会被通知两次？远端在通知投递期间又变了状态怎么办？

这套运行时的答案是**事件版本化**：每次"值得注意"的状态变化，用 `event_fingerprint`（状态快照的 SHA-256 指纹，`sql.py:53-55`）去重后把行上的单调计数器 `event_version` 加一（`_record_event_if_changed`，`sql.py:58-76`）。通知系统的全部职责收敛成一句话：**确保 `event_version` 里每一个版本都被投递过恰好一次（at-least-once + 版本对账）**——行上的 `notified_version` 记"已确认投递到哪个版本"，`dispatch_version` 记"正在投递哪个版本的快照"。§7 会看到这个三方对账怎么玩。

---

## 第 1 部分：总体架构——一张图看懂

```
 agent run 内
   │ 调用被改装过的 submit 工具（tools.py:629 _make_background_submit_tool）
   ▼
 get_mcp_task_submitter()                ← deerflow/mcp/tasks/runtime.py:93 进程全局提交器
   ▼
 McpTaskService.submit (app/mcp_tasks/service.py:89)
   │ driver.submit ──► 远端 MCP 服务器（真正的任务在这里启动）
   │   ▲ request_scoped_headers=True（只有这一次带请求级凭据，§8.3）
   ▼
 repository.create ──► mcp_tasks 行（status=submitted, next_poll_at）
   │ 持久先于返回；落库失败 → 补偿取消远程任务（#4933，§3.3）
   ▼
 工具立即返回 {task_id, task_name, status, message}    ← agent 循环解锁，run 可以正常结束
                          │
   ┌──────────────────────┘  此刻起，一切都发生在 run 之外
   ▼
 McpTaskService._run_loop（asyncio 任务 "deerflow-mcp-task-poller"，service.py:662）
   └─ 每 poll_interval_seconds 一轮 run_once（service.py:217）
        ├─ ① _run_cancellations：认领取消请求 → driver.cancel → 落终态
        ├─ ② claim_due_tasks（FOR UPDATE SKIP LOCKED + 租约，sql.py:192）
        │      └─ _poll_one → driver.get_status → apply_snapshot
        │           （状态有实质变化 → event_version++，§0.5）
        └─ ③ _run_notifications：认领通知工作（event_version > notified_version）
               └─ _notify_one → launch_mcp_task_notification_run
                    └─ start_run：在同一线程里发起一次真正的 agent run（§7）
                         └─ run 成功 → finish_notification_run(delivered)
```

四个平面各司其职：**工具面**（agent 看到的 submit）→ **运行时面**（协议中立的 driver/registry/提交器）→ **持久面**（一张表、三套租约）→ **服务面**（Gateway 里的后台循环 + REST + 通知 run）。

---

## 第 2 部分：agent 侧——task toolset 的外科手术

文件：`backend/packages/harness/deerflow/mcp/tools.py`。

### 2.1 三刀改装（`_configure_task_tools_for_server`，`tools.py:683-740`）

服务器配置了 task toolset 时，工具发现阶段动三刀：

1. **隐藏 `status_tool` 和 `cancel_tool`**（`:715, 724-725`）——它们不再出现在 agent 的工具列表里。原因：轮询有专门的后台 worker（限流、退避、租约一应俱全），如果 agent 也能随手调 status，一来上下文被轮询结果灌满，二来和后台轮询互相踩（两个消费者抢一个远端状态接口）。
2. **缺刀即错**（`:711-713`）——toolset 声明的三个原始工具在服务器上找不到任何一个，启动即报错。配置里的名字是契约，契约不完整宁可不起来。
3. **替换 `submit_tool`**——换成 `_make_background_submit_tool` 包出来的持久版。

### 2.2 持久版 submit——立即返回的艺术（`_make_background_submit_tool`，`tools.py:629-680`）

```python
async def submit_in_background(...):
    ...
    submitter = get_mcp_task_submitter()
    result = await submitter.submit(
        driver_name=ORDINARY_MCP_TASK_DRIVER,
        request=TaskSubmitRequest(..., driver_data={"submit_tool", "status_tool", "cancel_tool"}),
    )
    return {"task_id": ..., "task_name": ..., "status": ..., "message": "Task is running in the background."}
```

（骨架见 `tools.py:640-672`）

对 agent 来说，这个工具调用**在 run 内完成**——所以它带着完整的请求上下文（用户身份、请求级凭据，§8.3），但它在远程启动任务、本地落库之后**立即返回**，返回值只是一个本地任务 ID 和一句"任务已在后台运行"。工具描述里也追加了契约说明（`:638, 676`）："returns a DeerFlow task ID immediately and status polling is handled automatically"。

注意这和 `subagent-batch` 篇 §2.5 的同构性：两者都在用返回消息**教育模型**"接下来该干嘛/不该干嘛"。这里没说的是"不要等"——因为根本没有可等的东西，status 工具已被藏起来了。

### 2.3 结果的三条出口

agent 拿到 task_id 之后，结果从三条通道回来：

1. **通知 run**（§7）——任务到达关键节点时，系统自动在同一线程里发起一次 agent run，把事件讲给用户听。这是主通道。
2. **agent 内置工具** `list_background_tasks` / `cancel_background_task`（`tools/builtins/background_tasks_tool.py:46-52, 55-84`）——lead 在**后续**对话轮次里可以主动列出/取消本线程的后台任务。
3. **REST + 前端面板**（§9）——`/api/threads/{thread_id}/mcp-tasks` 和聊天界面的后台任务卡片。

---

## 第 3 部分：submit——"持久先于返回"与提交补偿

文件：`backend/app/mcp_tasks/service.py`（类声明 `:45-46`，docstring："Persist and poll long-running MCP tasks outside the Agent loop."）。

### 3.1 提交主链（`submit`，`service.py:89-161`）

顺序是这个函数的灵魂：**先远程启动、再本地落库、然后才返回**。

```python
local_task_id = request.local_task_id or f"mcp-task-{uuid.uuid4().hex}"
driver_request = replace(request, local_task_id=local_task_id)
submission = await driver.submit(driver_request)          # ① 远程任务真正启动
...
snapshot = self._normalize_snapshot(submission.snapshot)
next_poll_at = self._next_poll_at(snapshot, now=submitted_at)
return await self._repository.create(...)                 # ② 落库，status=submitted
```

为什么是这个顺序？如果反过来（先落库再提交远程），落库成功但远程提交失败，数据库里就有一条"幽灵任务"永远等不到状态；先远程后落库，失败模式变成"远程跑起来了但本地没记上"——这才是能用补偿手段救的（§3.3）。**让可补偿的错误发生在可补偿的位置。**

`next_poll_at` 是远端快照建议的下一次轮询时间（`poll_after_seconds`），会被钳到一天以内（`MCP_TASK_POLL_AFTER_MAX_SECONDS`）。轮询节奏听远端的，但节奏上限本地说了算。

### 3.2 唯一约束兜底（`sql.py:158-162`）

`mcp_tasks` 表有唯一约束 `uq_mcp_tasks_user_server_remote (user_id, server_name, remote_task_id)`（`model.py:70-75`）：同一个用户在同一台服务器上的同一个远程任务，只能有一条本地记录。撞约束时仓库抛 `DuplicateMcpRemoteTaskError`，而 `submit` **故意不补偿**这个错误（`service.py:139-142`）——撞约束说明这个远程任务已经有主人了，补偿取消会把别人名下正在跟踪的任务杀掉。**补偿只救"无主的东西"，有主的东西交给它的主人。**

### 3.3 提交补偿 `_cancel_untracked_task`（#4933，`service.py:163-215`）

最阴险的失败窗口在 ① 和 ② 之间：远程任务已经启动，而调用方（正在被取消的 run、正在关闭的 Gateway）恰好在落库前取消了 `submit` 协程。此时远程任务**无人认领**——永远跑着、永远没人查。

#4933 的修复是提交补偿：

```python
except asyncio.CancelledError:
    await self._cancel_untracked_task(..., reason="caller cancellation during local persistence")
    raise
except Exception:
    await self._cancel_untracked_task(..., reason="local submission finalization failure")
    raise
```

（挂接点 `service.py:143-161`）

`_cancel_untracked_task` 的三个设计细节（`:163-215`）：

1. **后台执行**：补偿是一个独立的 `asyncio.Task`（名字 `mcp-submit-compensation-{id}`），登记进 `self._compensation_tasks`。补偿本身绝不阻塞原异常的抛出——用户看到的还是原始错误。
2. **防反复取消**：补偿内部用 shield 式的等待循环，外层协程再怎么被 cancel，补偿也要撑到 5 秒期限（`_UNTRACKED_TASK_COMPENSATION_WAIT_SECONDS = 5.0`，`:36`）。
3. **超时不强杀**：5 秒内没取消成功，补偿任务继续留在后台跑，只记 warning——尽力而为，但不撒谎说成功。

这个模式和数据库事务的"提交前失败 → 回滚"是同构的：远程提交是"不可回滚的外部副作用"，唯一的回滚手段就是再发一个反向副作用（取消）。

---

## 第 4 部分：轮询循环——一轮三件事

### 4.1 `run_once`（`service.py:217-239`）

```python
async def run_once(self, *, now: datetime) -> None:
    await self._run_cancellations(now=now)

    claimed = await self._repository.claim_due_tasks(
        now=now, lease_owner=self._lease_owner,
        lease_seconds=self._lease_seconds, limit=self._max_concurrent_polls,
    )
    if claimed:
        results = await asyncio.gather(
            *(self._poll_one(task, now=now) for task in claimed),
            return_exceptions=True,
        )
        for record, result in zip(claimed, results, strict=True):
            if isinstance(result, BaseException):
                logger.error("Unexpected MCP task poll failure (task_id=%s); the lease will expire for recovery", ...)

    await self._run_notifications(now=datetime.now(UTC))
```

三段的顺序有讲究：**先执行取消**（用户点了停止，越早生效越好，且取消后的行不该再被轮询），**再并发轮询**（一轮最多认领 `max_concurrent_polls`——默认 8——条，`gather` 并发发出，单条失败只记日志），**最后处理通知**（通知基于本轮刚刷新的状态，放在最后保证用的是最新 `event_version`）。

失败路径有个漂亮的兜底注释："the lease will expire for recovery"——单条轮询的意外异常不急着处置，租约过期机制（§5.2 的收尸分支）自然会在下轮把行重新放回池子。**能交给租约机制的事，就不要写专门的恢复代码**——这是全套持久化 worker 共享的哲学。

### 4.2 认领 SQL（`claim_due_tasks`，`sql.py:192-226`）

```python
stmt = (
    select(McpTaskRow)
    .where(
        McpTaskRow.status.in_(_POLLABLE_STATUS_VALUES),
        McpTaskRow.cancel_requested_at.is_(None),
        McpTaskRow.next_poll_at.is_not(None),
        McpTaskRow.next_poll_at <= now,
        or_(
            McpTaskRow.lease_expires_at.is_(None),
            McpTaskRow.lease_expires_at < now,
        ),
    )
    .order_by(McpTaskRow.next_poll_at.asc(), McpTaskRow.id.asc())
    .limit(limit)
    .with_for_update(skip_locked=True)
)
```

`FOR UPDATE SKIP LOCKED` 多 worker 互不阻塞（`subagent-batch` 篇 §6.1 讲过原语）。WHERE 里的五个条件各守一门：可轮询状态、没有未决取消请求、有调度时间、到点了、租约空闲或已过期。认领动作：盖上 `lease_owner` / `lease_expires_at`，`poll_attempt_count += 1`。

### 4.3 应用快照的四重守卫（`apply_snapshot`，`sql.py:228-276`）

轮询结果回来写库时，仓库层不是无脑 UPDATE，而是四重守卫：`lease_owner` 必须是自己（租约没被抢走）、`lease_expires_at >= polled_at`（租约没在轮询期间过期）、行不是终态（终态不可覆写）、`cancel_requested_at` 为空（取消路径独占终态裁决权，§6.1）。

任何一重不过，返回 `False`，服务层记一句 "Discarded MCP task poll result after lease ownership changed or expired"（`service.py:566-570`）然后**丢弃这份结果**。想想什么时候会发生：worker A 轮询花太久了，租约过期，worker B 认领并也在轮询——A 的迟到结果如果落库，就会和 B 的结果赛跑。丢弃 A 的，等 B 的。**迟到的一致性不如准时的一致性。**

成功应用时：清空租约（`row.lease_owner = None`）、清零连续错误计数、终态则记 `completed_at`、然后走 `_record_event_if_changed` 判断是否 `event_version++`。

### 4.4 失败降级（`release_claim`，`sql.py:278-305`）

轮询出错（远端超时、网络抖动）走 `release_claim`：清租约、`consecutive_poll_error_count += 1`、按退避设置 `next_poll_at`。连续错误达到 `tracking_degraded_after_errors`（默认 3）时，发出一个"跟踪降级"事件——**这也是一个 event_version**，会触发通知：告诉用户"这个任务的进度暂时看不清了，还在低频重试"。把系统的窘境也如实播报，而不是静默硬扛，是这个功能在用户体验上的一个讲究。

协议级失败（driver 抛 `McpTaskProtocolError`，比如远端返回了不合法的载荷）则直接强制一条 `FAILED` 快照（`service.py:519-531`）——契约破坏不是抖动，重试没有意义。

---

## 第 5 部分：一张表，三套账本

文件：`backend/packages/harness/deerflow/persistence/mcp_tasks/model.py`（`McpTaskRow`，表 `mcp_tasks`，`:17-80`）。地基是迁移 `0011_mcp_tasks`，#4833 的 `0013_mcp_task_notifications` 补上通知列组。

按功能分组看列（这比按定义顺序读有效得多）：

| 列组 | 列 | 作用 |
|---|---|---|
| 身份 | `user_id`, `server_name`, `remote_task_id`, `name` | 唯一约束 `(user_id, server_name, remote_task_id)` 防重复登记（§3.2） |
| 状态 | `status` | 协议中立状态机（§6.1） |
| 结果 | `result`, `result_preview`, `result_truncated`, `result_artifact` | 终态产物，入库截断（`max_result_bytes` 64KiB / preview 2000 字符） |
| 轮询账本 | `next_poll_at`, `last_polled_at`, `last_poll_error`, `poll_attempt_count`, `consecutive_poll_error_count` | 什么时候查、查了多少次、错了多少次 |
| 轮询租约 | `lease_owner`, `lease_expires_at` | §0.4 第一套租约 |
| 取消账本 | `cancel_requested_at`, `next_cancel_at`, `cancel_attempt_count`, `last_cancel_error` | §0.4 第二套账 |
| 事件账本 | `event_fingerprint`, `event_version`, `notified_version` | §0.5 版本化事件 |
| 通知账本 | `notification_status`, `dispatch_version`, `dispatch_attempt`, `dispatch_event`, `notification_run_id`, `notification_error`, `notification_attempt_count`, `next_notification_at` | §7 |
| 通知租约 | `notification_lease_owner`, `notification_lease_expires_at` | §0.4 第三套租约 |

辅助索引四个（`model.py:76-79`）：`(thread_id, created_at)` 给线程页列表；`(status, next_poll_at)` 给轮询认领；通知到期和取消到期各一个。**每个索引对应一个热查询**——这张表是三类 worker 的共同作业面，索引就是各自的取件口。

---

## 第 6 部分：状态机——两条独立的线

### 6.1 任务状态 `status`（`TaskStatus`，`tasks/models.py:21-29`）

```
create → submitted → working ⇄ input_required → completed / failed / cancelled
                       （pollable 三态：submitted / working / input_required）
```

远端 `running` 被 ordinary driver 映射为本地 `working`（`ordinary.py:72-78`）。合法转换只有三条路：

- **轮询应用**（`apply_snapshot`）：非终态、无取消请求的行 → driver 返回的任何状态。终态设 `completed_at`、不再接受新快照。
- **取消路径**（`claim_cancel_requests` → driver.cancel → `apply_cancel_snapshot`）：只有它能把行送进 `cancelled`，且 driver 契约要求取消必须返回终态快照（`service.py:328-329` 抛协议错误，`sql.py:387-388` 再验一遍——双重把关）。
- **协议违规**：强制 `FAILED`（§4.4）。

两条铁律藏在 `apply_snapshot` 的守卫里，值得单独说：**终态不可覆写**（已 completed 的行不能被迟到的快照改回 working）；**取消请求一旦存在，轮询结果一概拒收**（`cancel_requested_at IS NULL` 守卫）——用户说不要了，之后从四面八方赶来的状态更新一律作废，终态由取消路径独占裁决。两句话合起来：**每个终态都有唯一的裁决者。**

### 6.2 通知状态 `notification_status`（独立状态机）

```
none（create，sql.py:147）
   → pending（出现待投递事件：event_version > notified_version）
   → claimed（claim_notification_work 认领，sql.py:484）
   → dispatched（通知 run 已发起，sql.py:508）
   → delivered（run 成功且 event_version == dispatch_version，sql.py:579-588）
        └─ 若期间冒出更新的 event_version → 回 pending 投递新版
   claimed/dispatched → retry（投递失败）→ claimed …（重试预算 5 次，service.py:35）
   → dead_letter（重试耗尽或永久错误，sql.py:652-691）
```

两个精妙的细节：

1. **事件快照的不可变重建**：认领通知工作时，如果 `dispatch_version != event_version`（投递期间来了新事件），用最新事件快照**重建** `dispatch_event` 并重置尝试计数（`sql.py:473-477`）——永远投递"当前最新的事实"，不排队投递过时的中间态。用户要的是"任务完成了"，不是"任务在 14:02 完成过一次又被更新过三次"的流水账。
2. **死信不丢新消息**：进死信时如果发现 `event_version > dispatch_version`（僵在投递旧版本时来了新事件），新事件被重新放回 `pending`（`sql.py:673-689`）——**死信只埋葬那条投递失败的旧消息，不株连后来的消息**。

---

## 第 7 部分：通知 run——用一个真正的 agent run 当投递通道

这是整个功能里最"甚至"的设计：任务完成的消息，不是直接往对话里塞一条系统消息，而是**发起一次货真价实的 agent run**，让模型用自己的话把事件讲给用户。

### 7.1 触发（`_notify_one`，`service.py:391-497`）

三种情况产生待投递事件（`_notification_event`，`sql.py:35-50`）：状态到达**注意集**（`input_required` 或三个终态）、跟踪降级（§4.4）。行进入通知流水线后：

- 首次：调用 `launch_mcp_task_notification_run` 发起 run，标 `dispatched`；
- 已是 `dispatched`：改为**轮询那个通知 run 的状态**（通过 `run_manager.get`，`service.py:407-449`），run 到终态才算投递完成/失败。

### 7.2 通知 run 的六个参数（`launch_mcp_task_notification_run`，`backend/app/gateway/services.py:1505-1574`）

这个函数构造一次 `start_run`，每个参数都有明确的为什么：

1. **同一线程**：run 发起在任务的归属 thread 里——通知出现在用户原来的对话流中，上下文连贯。
2. **内部身份**：以 internal user（`get_internal_user()`）发起（`:1517-1522`）——通知不是用户本人发起的动作。
3. **隐藏的用户消息**：注入一条 `additional_kwargs={"hide_from_ui": True}` 的 user message（`:1525-1532`），内容是 `frame_untrusted_text` 包裹的事件 JSON——事件数据来自外部 MCP 服务器，是不可信输入，必须框起来防注入；隐藏是因为用户不需要看到原始 JSON，只需要看到模型对它的转述。提示词模板 `_mcp_task_notification_prompt`（`:1492-1502`）还专门叮嘱模型：不要暴露 remote task id、解释 `input_required` 为什么还不能恢复、解释跟踪降级是怎么回事。
4. **`non_interactive=True`**（`:1543`）：和定时任务同一机制（见根 `AGENTS.md` 的备注与 `stream-bridge` 篇 §3.4）——从 lead 工具集里摘掉 `ask_clarification`，通知 run 必须自己讲完，不能中途反问用户。
5. **`multitask_strategy="reject"` + `on_disconnect="continue"`**（`:1552-1554`）：同线程已有 run 在跑就拒绝（不排队不并发——通知不抢用户的交互窗口），断线继续跑。
6. **幂等键** `f"mcp-task:{task_id}:{dispatch_version}:{dispatch_attempt}"`（`:1559`）：落在 `runs.idempotency_key` 唯一索引上（迁移 0013）。投递重试时 attempt 递增、键不同；同一 attempt 重放时撞唯一索引，天然去重。

### 7.3 交付确认与 at-least-once 对账

run 成功 → `finish_notification_run(delivered=True)`：`notified_version = dispatch_version`（`sql.py:580`）——"这个版本确认送到了"。若发现期间来了更新的 `event_version`，当前投递算完成，新版本回 `pending` 等下一轮。run 失败/找不到 → `retry`，计数递增，下次 `next_notification_at` 再来（重试间隔由 `_notification_retry_seconds` 的退避决定）。五次耗尽 → 死信。

把 §0.5 的三方对账现在合上：`event_version`（事实发生了几件事）≥ `dispatch_version`（正在送第几件）≥ … 最终 `notified_version`（确认送到第几件）追平 `event_version`，账平。**一个用 run 状态做投递回执的、带死信的事件分发器。**

---

## 第 8 部分：网关接线、配置与凭据拆分

### 8.1 lifespan 接线（`backend/app/gateway/app.py:350-408`）

启动序列（读法同 `subagent-batch` 篇 §9.1）：构造扩展配置快照（`:367`）→ `validate_mcp_task_runtime_configuration` 校验（`:373-377`）→ 仅当配置了 task toolset 才注册 ordinary driver（`:380-384`，没有消费者就不注册，避免空转）→ 构造 `McpTaskService`，注入仓库、driver 注册表、全套配置项和两个回调（`launch_notification` 和 `get_run`，后者让服务能查询通知 run 的状态）→ `mcp_tasks_config.enabled` 时 `start()` + `set_mcp_task_submitter(...)` + `app.state.mcp_tasks_available = True`（`:405-408`）。关机对称（`:459-471`）：先摘可用标志，再停服务，再摘提交器和配置快照。

仓库只在 SQL 后端下创建（`deps.py:498-518`）——和 batch 一样，没有 SQL 就没有持久层，一切免谈。

### 8.2 配置项（`config/mcp_tasks_config.py:4-15`）

| 键 | 默认 | 约束 | 行 |
|---|---|---|---|
| `mcp_tasks.enabled` | `false` | — | `mcp_tasks_config.py:7` |
| `.poll_interval_seconds` | `5` | 1–300 | `:8` |
| `.lease_seconds` | `120` | 5–3600 | `:9` |
| `.max_concurrent_polls` | `8` | 1–64 | `:10` |
| `.max_poll_backoff_seconds` | `300` | 1–3600 | `:11` |
| `.input_required_poll_interval_seconds` | `60` | 5–3600 | `:12` |
| `.tracking_degraded_after_errors` | `3` | 1–100 | `:13` |
| `.max_result_bytes` | `65536` | 1024–10MiB | `:14` |
| `.result_preview_max_chars` | `2000` | 64–100000 | `:15` |

全部重启生效（`reload_boundary.py:75-78`——服务在 lifespan 构造时捕获这些值，YAML 热更新不重建服务实例）。工具集本身配在 `extensions_config.json` 的每服务器 `task_toolsets:` 列表里（§0.2），不配在 YAML——工具集是"扩展拓扑"，和 MCP 服务器定义放在一起才说得通。

### 8.3 凭据拆分——为什么只有 submit 带请求级凭据（#5010 的落点）

`McpTaskToolCaller` 的调用分两类时机（`backend/packages/harness/deerflow/mcp/task_tool_caller.py:63-85`）：

- **submit**：发生在 agent run 内部，LangGraph runtime 还活着，请求级 secrets（`config.context.secrets`）可解析 → 传 `request_scoped_headers=True`（`ordinary.py:162-166`），把 `headers_from_context` 拦截器接进拦截器链，per-run 的 API key 能随提交到达远端。
- **status / cancel**：发生在后台 worker 的轮询循环里——**此时 run 早已结束，运行时不存在，请求级 secrets 无处可取**。如果还挂着 fail-closed 的凭据拦截器，每一次轮询都会被拒。所以 status/cancel 走不带请求级拦截器的链，退回服务器级静态/OAuth 凭据。

如果一台服务器同时声明了 `headers_from_context` 和 `task_toolsets`，启动时会打警告（`mcp/context_headers.py:107-114`）——这个组合意味着"提交用每请求凭据、轮询用服务器凭据"，语义上必须想清楚再上。这个"同一工具链、按时机拆拦截器"的设计，是 MCP 凭据体系（详见后续 `mcp-credentials-deep-dive.md`）与持久任务运行时的接缝。

### 8.4 启动安全阀（`tasks/runtime.py:103-124`）

配置了 task toolset 却没开 `mcp_tasks.enabled`、或没用 SQL 后端 → 启动校验直接拒绝（`:112-115`）。task 相关服务器配置或 MCP 拦截器被热改 → 校验拒绝、提示重启（`:66-79`）——因为配置快照在启动时冻结（`app.py:372`），热改后的行为不可预期，宁可让管理员重启。**凡是会跨 run 存活的东西，都不参与热更新。**

---

## 第 9 部分：REST 看板与三个韧性修复

### 9.1 路由（`backend/app/gateway/routers/mcp_tasks.py`，前缀 `/api/threads/{thread_id}/mcp-tasks`，`:13`）

- **列表**（`:66-82`）：只给安全字段——task_id/name/status/时间/有界错误（500 字符）/`tracking_degraded`/`cancel_requested`。**`remote_task_id` 永不暴露**（`_list_item`，`:28-38`）：远程句柄是外部系统的钥匙，本地用户知道了它，就有可能绕过本系统直接操作远程任务。
- **详情**（`:85-101`）：追加结果、artifact、input_required 载荷和轮询/取消/通知三本账的诊断字段；跨用户/跨线程 404（`:96-97`）。
- **取消**（`:104-126`）：带一个重要的 503 闸门（§9.2）。

### 9.2 修复一：worker 没跑就不许承诺取消（#4963，`:113-118`）

```python
if not getattr(request.app.state, "mcp_tasks_available", False):
    # The service exists whenever SQL persistence is configured, but the
    # background loop that owns the remote cancel call only runs when
    # mcp_tasks.enabled=true. Recording cancel_requested_at without a
    # worker would acknowledge a cancellation nobody will ever perform.
    raise HTTPException(status_code=503, detail="MCP task cancellation worker is not running")
```

注释把因果讲尽了：服务对象在配了 SQL 时就存在，但真正执行远程取消的循环只在 `enabled=true` 时运转。以前的行为是照常盖 `cancel_requested_at` 章、返回"已取消"——而那个章永远没人来执行，远程任务跑到天荒地老。**没有执行者的承诺必须拒绝发出**，503 比虚假的 200 诚实得多。列表/详情保持可用（只读不受影响）。

### 9.3 修复二：超时不逐出会话（#5027，`mcp/session_pool.py:56-112`）

轮询用的 stdio 会话是池化复用的（每次轮询重新拉起子进程太重）。此前 any 异常都会把池里的会话关掉重建——一次普通的 `tool_call_timeout`（远端任务就是慢！）就毒化了健康会话，之后每轮轮询都要付出重启子进程的代价。修复后抽出的 `call_pooled_session_tool` 只在**明确的传输断连**（`anyio` 的 ClosedResourceError/BrokenResourceError/EndOfStream 或 MCP SDK 的 `Connection closed`，`_is_mcp_transport_disconnect`，`:56-59`）时才逐出会话；超时和拦截器错误一律保留会话。收尾用 `_finish_session_cleanup`（`:62-91`）屏蔽取消——清理半途被 cancel，也要把死会话拆干净恰好一次。

### 9.4 修复三：提交补偿（#4933）

§3.3 已展开，不赘述。三个修复的时间线（8/23、8/24、8/28）也说明这套运行时的成熟路径：先跑通（#4665/#4690/#4833），再在真实故障里补韧性。

---

## 第 10 部分：McpTaskService vs SubagentBatchService——一张表说清

两个服务解决同一个母题（工作活得比 run 久），但方向相反：

| | `SubagentBatchService` | `McpTaskService` |
|---|---|---|
| 工作发生在哪 | **本进程**（subagent 是本地跑的 agent） | **远端**（MCP 服务器上，本地只留凭证） |
| 谁执行 | worker 主动认领并执行 | worker 只**观察**（轮询），执行体在远端 |
| 提交者 | lead 的 `batch_task` 工具 | 被改装的 task toolset submit 工具 |
| 认领什么 | 待执行的条目（`leased`） | 该查状态的行（到 `next_poll_at`） |
| 租约管什么 | 执行权（谁在跑这条 item） | 轮询权（谁在查这行）/ 取消权 / 通知权，三套独立 |
| 失败重试 | 重跑整个 item（at-least-once 执行） | 重试的是**查询**，不重跑远端任务 |
| 完成的回报 | 结果落库 + 前端看板 + JSONL 导出 | **通知 run**：agent 把消息讲回对话线程 |
| 凭据 | 提交时快照进 `execution_spec` | submit 带请求级凭据，轮询用服务器凭据（时机拆分） |
| 配置 | `subagent_batches.*`（YAML） | `mcp_tasks.*`（YAML）+ `task_toolsets`（extensions_config.json） |

一句话：**batch 是"把远处的活搬回家干"，MCP task 是"活本来就在远处，我们只派观察员"**。执行语义的差异（重跑 vs 重查）就是这两句话在工程上的全部投影。

---

## 测试在哪

- `backend/tests/test_mcp_task_service.py` —— 提交持久先于返回、#4933 补偿三态、取消请求、通知全生命周期（成功/丢 run/失败隔离/冲突换新/死信）、无 LLM 的 run_once 轮询
- `backend/tests/test_mcp_task_repository.py` —— 唯一约束、租约认领/过期收尸、apply_snapshot 四重守卫、连续错误计数、通知版本重建与死信
- `backend/tests/test_mcp_task_ordinary_driver.py` / `test_mcp_task_ordinary_e2e.py` —— 线上契约（structuredContent、task_not_found→FAILED）与假 MCP 服务器端到端（提交→轮询→重启恢复→终态）
- `backend/tests/test_mcp_task_tool_wrapping.py` / `test_mcp_task_toolset_config.py` —— 工具改装三刀、toolset 配置校验
- `backend/tests/test_mcp_task_runtime_config.py` / `test_mcp_task_config.py` —— 启动校验（enabled/SQL/热改拒绝）、配置默认与边界
- `backend/tests/test_mcp_task_tool_caller.py` / `test_mcp_session_pool.py` —— #5027 的超时保会话/断连才逐出、凭据时机拆分
- `backend/tests/test_mcp_tasks_router.py` —— 安全字段、404、#4963 的 503 闸门
- 前端面板：`frontend/src/components/workspace/thread-background-tasks.tsx`（#4833 配套）

---

## 本篇小结

回到标题的问题：run 结束了，没跑完的 MCP 工具调用谁来管？

答案是一套四层分工：**工具层**把提交改成"立即返回本地凭证"（§2）；**运行时层**用协议中立的 driver 把"MCP 怎么提交/查/取消"封装起来（§0.3）；**持久层**用一张表、三套租约、两条状态机把事实记账（§5、§6）；**服务层**的 `McpTaskService` 在 run 之外循环三件事——执行取消、并发轮询、投递通知——并用一次真正的 agent run 把结果讲回对话（§7）。

支撑它的四根柱子，全部是这个文档家族里的老朋友：

- **持久先于返回 + 可补偿错误放在可补偿位置**（§3.1）
- **租约 + `FOR UPDATE SKIP LOCKED` + 过期收尸**——多 worker 安全的原话三件套（§4.2、§5）
- **"请求-观察"代替"命令-执行"**——取消是盖章，通知是账本，没有跨进程的命令（§6）
- **教育模型的返回消息 / 提示词**——agent 侧的一切行为契约都写在模型看得见的地方（§2.2、§7.2）

它和两个姊妹篇一起，把"工作与载体的寿命差"这个分布式系统的老问题，在 agent 系统里补齐了三种解法：`RunManager` 管 run 本身，`SubagentBatchService` 管搬回家干的工作，`McpTaskService` 管留在远端的工作。三套实现共享同一套词汇，读懂任何一篇，另外两篇的代码都会觉得似曾相识。
