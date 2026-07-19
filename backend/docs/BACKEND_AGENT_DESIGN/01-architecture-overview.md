# 第 1 章：架构总览（极致详细版）

> **本章目标**：让你建立"一个用户请求从进来到出去，在 DeerFlow 后端经历了什么"的**完整、无遗漏**的心智模型。本章不假设你看过任何其他资料，从零开始，每个函数贴完整代码逐行讲解，配输入输出样例，深挖设计动机。
>
> 读完本章，你不看源码也能回答：请求怎么进来的？怎么认证的？Run 怎么创建的？Agent 怎么被造出来又怎么执行的？结果怎么流式推回去的？

---

## 1.1 DeerFlow 是什么：从"研究框架"到"Agent 运行时"

### 一句话定位

> DeerFlow 2.0 是一个 **super agent harness（超级 Agent 运行时）**——不是一个你拼装的 SDK，而是一个**自带全部基础设施、开箱即用**的 Agent 执行环境。

**关键词是 harness（挽具/运行时框架）**。理解这个词很重要：

| 类型 | 例子 | 你做什么 |
|------|------|----------|
| **SDK/框架** | LangChain、LlamaIndex | 你用它的零件**拼装**一个 Agent |
| **Harness/运行时** | DeerFlow 2.0、Claude Code | 它**已经是一个完整 Agent**，你直接用或扩展 |

DeerFlow 1.x 时代是前者（Deep Research 框架，你拼装研究流程）。2.0 是**完全重写**（和 v1 无共享代码），变成了后者——文件系统、记忆、技能、沙箱、子 Agent 调度，全部内置。

### 本次演进（e418d729 → b3a0dac8）强化了什么

这次代码更新（984 文件、14 万行改动）给 harness 加了三大能力，让 Agent 从"问答工具"走向"自主完成目标"：

1. **目标自动延续**：Agent 在回答用户后，自己判断"目标达成了吗"，没达成就继续干。这是迈向 Agentic 的关键。
2. **多 worker 高可用**：生产环境可以跑多个 Gateway 进程，通过租约（lease）+ 心跳（heartbeat）协调，一个崩了另一个接管。
3. **精细可观测性**：每次 run 消耗多少 token、改了哪些文件、为什么终止（循环？超预算？），全部记录。

---

## 1.2 Harness / App 分层：最重要的架构边界

### 分层结构

整个后端被**严格地**切成两层，依赖方向**单向**：

```
┌─────────────────────────────────────────────────────────────┐
│  App 层 (backend/app/)                  导入前缀: app.*      │
│  HTTP 壳：FastAPI Gateway、IM 渠道、认证                     │
│              ↓ 可以 import deerflow (允许)                   │
├─────────────────────────────────────────────────────────────┤
│  Harness 层 (backend/packages/harness/deerflow/)             │
│  导入前缀: deerflow.*                                        │
│  核心智能：Agent 编排、工具、沙箱、子Agent、运行时            │
│              ✗ 绝不 import app (禁止)                        │
└─────────────────────────────────────────────────────────────┘
```

### 铁律及其强制执行

**铁律**：`app` 可以 import `deerflow`，但 `deerflow` **绝不** import `app`。

```python
# 引用位置：backend/tests/test_harness_boundary.py
# 这条边界由测试强制守护，每个 PR 的 CI 都会跑
```

### 为什么这么分层？（深挖设计动机）

**核心目的：让 harness 成为可独立发布的 PyPI 包（`deerflow-harness`）。**

想象一个反例：如果 `deerflow`（核心）反向 import 了 `app`（FastAPI 网关），会发生什么？

```
你想把 deerflow-harness 装进一个非 Web 应用（比如定时任务脚本、Jupyter notebook）
  → pip install deerflow-harness
  → import deerflow
  → deerflow 内部 import app.gateway  ← 炸了：app 依赖 FastAPI，脚本里没有
  → 你被迫装一堆 Web 依赖，只为了用 Agent 核心
```

所以分层把"Agent 智能"和"HTTP 壳"彻底隔离。harness 里的 `DeerFlowClient` 和新增的 `tui/`（终端 UI）证明了这一点——它们都不依赖 FastAPI，直接在进程内调用 Agent。

**对比其他项目的类似设计**：这和 LangChain 自己把 `langchain`（核心）与 `langserve`（HTTP 服务）分开是同一个思路。DeerFlow 更激进——它把整个运行时（RunManager、worker、StreamBridge）都放进了 harness，这样嵌入式调用和 HTTP 调用走**同一套执行引擎**。

---

## 1.3 四端口拓扑

### 拓扑图

```
浏览器/IM客户端/Webhook
     │
     ▼
┌──────────────────────────────────────────────────────┐
│  Nginx  :2026  ◄── 统一入口（用户只访问这个端口）       │
│  路由规则：                                            │
│   /api/langgraph/*  → Gateway :8001 (LangGraph兼容层) │
│   /api/*            → Gateway :8001 (REST API)        │
│   /*                → Frontend :3000 (Next.js 页面)   │
└──────────────────────────────────────────────────────┘
     │                              │
     ▼ (api 请求)                    ▼ (页面请求)
┌─────────────────┐           ┌──────────────────┐
│  Gateway :8001  │           │  Frontend :3000  │
│  FastAPI 应用    │           │  Next.js Web界面 │
│  • 内嵌 Agent    │           └──────────────────┘
│  • 多 worker 租约│
└─────────────────┘
     │ (K8s沙箱时)
     ▼
┌──────────────────────┐
│  Provisioner :8002   │
└──────────────────────┘
```

### 三个关键设计决策（深挖"为什么"）

**决策 1：为什么用 Nginx 做统一入口？**

浏览器有**同源策略**：前端（:3000）用 JavaScript 调 API（:8001），属于跨域，默认被浏览器拦截。两种解法：
- (A) 在 API 上配 CORS 头——允许跨域。但这暴露了 API 端口，有安全风险。
- (B) 用反向代理把前后端统一到一个端口——同源，CORS 自动消失。

DeerFlow 选 (B)。Nginx :2026 把 `/api/*` 转发给 Gateway、`/*` 转发给 Frontend，浏览器看来都是 :2026，同源。只在"分离部署"（前端和 API 在不同机器）时才需要配 `GATEWAY_CORS_ORIGINS`。

**决策 2：为什么把 Agent 运行时内嵌在 Gateway 里？**

Gateway **不是**单纯的 HTTP 代理——它自己就跑着 Agent。看 RunManager、run_agent、StreamBridge 全在 harness 里就知道了。

如果 Agent 在独立进程，流式响应（SSE）就需要跨进程传 token——每生成一个字都要 IPC 一次，延迟和开销都大。内嵌在 Gateway 里，Agent 输出直接通过进程内的 StreamBridge 桥接到 HTTP 响应，零跨进程开销。

**决策 3：多 worker 怎么协调？**

生产环境可以跑多个 Gateway worker（比如 gunicorn 多进程）。但"哪个 worker 负责哪个 run"是个问题——如果用户在 worker A 创建了 run，但重连时连到了 worker B，B 怎么知道这个 run？

DeerFlow 用 **lease（租约）+ heartbeat（心跳）**：每个 run 创建时带一个 `lease_expires_at`（租约过期时间），负责它的 worker 定期发心跳续租。如果 worker 崩溃，心跳停止，租约过期后其他 worker 可以接管（takeover）。详见第 7 章。

---

## 1.4 一个请求的完整生命周期（核心，极致详细）

这是全篇最重要的一节。我们跟踪一个**用户在聊天框发消息**的完整旅程。

### 场景设定

假设用户在前端发送了一条消息：

```json
{
  "input": {
    "messages": [
      {
        "type": "human",
        "content": "帮我分析 uploads/sales.csv 这个文件，画出月度趋势图"
      }
    ]
  },
  "context": {
    "model_name": "doubao-seed-2-0-code",
    "thinking_enabled": true,
    "is_plan_mode": true
  },
  "stream_mode": ["values", "messages-tuple"]
}
```

这条消息 POST 到 `http://localhost:2026/api/threads/thread-abc-123/runs/stream`。

下面我们逐阶段跟踪它。

---

### 阶段 ①：HTTP 入口与认证

#### 1.4.1 Nginx 路由

Nginx 收到 `POST /api/threads/thread-abc-123/runs/stream`，匹配 `/api/*` 规则，转发给 Gateway :8001。

#### 1.4.2 FastAPI 路由匹配

```python
# 引用位置：backend/app/gateway/routers/thread_runs.py:496-498
@router.post("/{thread_id}/runs/stream")
@require_permission("runs", "create", owner_check=True, require_existing=True)
async def stream_run(thread_id: str, body: RunCreateRequest, request: Request) -> StreamingResponse:
```

**► 逐行注解**：
- **`@router.post("/{thread_id}/runs/stream")`**：FastAPI 路由装饰器。`thread_id` 从 URL 路径提取（这里是 `thread-abc-123`）。路由器前缀是 `/api/threads`（`thread_runs.py:34`），所以完整路径是 `/api/threads/{thread_id}/runs/stream`。
- **`@require_permission("runs", "create", owner_check=True, require_existing=True)`**：权限装饰器，**在进入函数前**就检查：
  - `"runs", "create"`：需要"创建 run"的权限。
  - `owner_check=True`：只允许**线程所有者**操作——防止用户 A 在用户 B 的线程上创建 run。
  - `require_existing=True`：线程必须已存在。
- **`body: RunCreateRequest`**：Pydantic 请求体模型（`thread_runs.py:71-91`，21 个字段：`assistant_id`、`input`、`command`、`metadata`、`config`、`context`、`multitask_strategy`、`stream_mode`、`on_disconnect` 等）。FastAPI 自动做校验，非法 JSON 直接 422。
- **返回类型 `StreamingResponse`**：函数体返回 `StreamingResponse(sse_consumer(...), media_type="text/event-stream", headers={"Content-Location": ...})`（`thread_runs.py:505-521`）。注意 `Content-Location` 只在这里设一次——`join_run` 和 `stream_existing_run` 都不设它，因为这个 header 是 LangGraph SDK 提取 `run_id` 的入口，只在"首次创建 run 的请求"上需要。

#### 1.4.3 认证中间件（请求穿过两层）

在到达路由函数前，请求已经穿过两层中间件。按注册顺序（`app.py:441-445`）：

```python
# 引用位置：backend/app/gateway/app.py:441-445 (create_app 里的中间件注册，简化展示)
# Auth: reject unauthenticated requests to non-public paths (fail-closed safety net)
app.add_middleware(AuthMiddleware)      # 行 442：认证
# CSRF: Double Submit Cookie pattern for state-changing requests
app.add_middleware(CSRFMiddleware)      # 行 445：CSRF 防护
# 可选：CORSMiddleware（在 split-origin 时，行 451-458）
```

**► ASGI 执行顺序提醒**：源码里按"先 Auth 后 CSRF"注册，但 Starlette/ASGI 的语义是**后注册的在外层先执行**——所以运行时实际是 CSRF 先于 Auth 进入。文档此处按"源码注册顺序"叙述，区分这一点即可。

**AuthMiddleware 的工作**（类在 `auth_middleware.py:65`，`dispatch` 在 `88`）：

模块 docstring 自述为 **"fail-closed safety net"**（默认拒绝，只有明确验证通过才放行；`app.py:441` 注释同此词）。`dispatch()` 三阶段调度：

```
请求进来（auth_middleware.py:88 dispatch）
  │
  ├─ 阶段1（行 92-104）：有 X-DeerFlow-Internal-Token 头？（IM 渠道 worker 用的）
  │   └─ 是 → 用 secrets.compare_digest 验证（internal_auth.py:39）
  │       └─ 通过 → 构造一个携带 owner 身份的合成"内部用户"，放行
  │
  ├─ 阶段2（行 107、113-128）：有 access_token cookie？
  │   └─ 是 → 调 get_current_user_from_request 做严格 JWT 验证
  │       ├─ 通过 → set_current_user(user)（行 155）写入 ContextVar
  │       └─ 失败 且 is_auth_disabled()=True（行 130-133）→ 回退到 auth_disabled 用户
  │
  ├─ 阶段3（行 134-136）：无 access_token cookie 但 is_auth_disabled()=True？（本地开发免认证）
  │   └─ 是 → 放行（user_id 回退为 "default"）
  │
  └─ 都不满足（行 137-146）→ 401 Unauthorized "Authentication required"
```

**► 设计动机深挖**：
- **为什么阶段 2 要"严格验证"？** 早期版本可能接受任何 cookie，导致"垃圾 cookie 绕过"漏洞——攻击者塞一个格式错误的 cookie，中间件异常被吞，默认放行。严格验证关闭了这个漏洞：JWT 签名不对、过期了，一律拒绝。
- **内部 token 头的精确名字**：`X-DeerFlow-Internal-Token`（`internal_auth.py:13`，常量 `INTERNAL_AUTH_HEADER_NAME`）；与之配对的归属头是 `X-DeerFlow-Owner-User-Id`（`internal_auth.py:14`，`INTERNAL_OWNER_USER_ID_HEADER_NAME`）。本文后续提到的 `X-DeerFlow-*` 是这两个的统称。
- **`set_current_user(user)` 写入 ContextVar**（行 155）：这个用户身份会贯穿整个请求。后面所有 per-user 隔离（文件路径、记忆、技能）都依赖它。ContextVar 在 asyncio 下是**任务本地**的（类似线程本地存储），不同请求互不干扰。`finally` 块里会 `reset_current_user(token)` 复位（行 159）。

**CSRFMiddleware 的工作**（类在 `csrf_middleware.py:200`，校验逻辑在 `214-228`）：

双重提交 Cookie 模式——防跨站请求伪造：
- 仅作用于状态变更方法（POST/PUT/DELETE/PATCH；`should_check_csrf()` 行 36-56）。
- 浏览器发 POST 时，必须同时带 `csrf_token` cookie 和 `X-CSRF-Token` header（名字定义在行 21-22）。
- 任一缺失 → 403 `"CSRF token missing"`（行 218-222）。
- 中间件用 `secrets.compare_digest(cookie_token, header_token)`（**行 224**）比较两者是否一致。
- 不一致 → 403 `"CSRF token mismatch."`（行 225-228）。
- 登录/注册/登出/初始化等 auth 端点豁免 token 校验，但走单独的 Origin 检查（`is_allowed_auth_origin`，行 164-182）。

我们的示例请求是前端发的（前端会自动带 CSRF token），所以通过。

---

### 阶段 ②：创建运行（start_run 逐行剖析）

路由函数把工作委托给 `services.py:start_run()`。这是连接 HTTP 层和运行时核心的桥梁。**这个函数极其重要，我们完整贴出并逐段注解。**

```python
# 引用位置：backend/app/gateway/services.py:608-767
async def start_run(
    body: Any,
    thread_id: str,
    request: Request,
) -> RunRecord:
    """Create a RunRecord and launch the background agent task."""
```

**► 关于 `body: Any` 的注解**：静态类型是 `Any`，但运行时一定是 `RunCreateRequest`。函数自己的 docstring 解释了这个"看起来怪"的注解——**为了避免与定义 `RunCreateRequest` 的 router 模块产生循环 import**（`services.py:615-619`）。如果你在自己代码里直接调 `start_run`，请传 `RunCreateRequest`。

**输入样例**（对应我们的场景）：
```python
body = RunCreateRequest(
    input={"messages": [{"type": "human", "content": "帮我分析 uploads/sales.csv..."}]},
    context={"model_name": "doubao-seed-2-0-code", "thinking_enabled": True, "is_plan_mode": True},
    stream_mode=["values", "messages-tuple"],
    assistant_id="lead_agent",
    on_disconnect="cancel",
    multitask_strategy="reject",
    metadata={},
    config={},
)
thread_id = "thread-abc-123"
```

#### 步骤 1：取出三个单例

```python
# 引用位置：backend/app/gateway/services.py:625-629
    bridge = get_stream_bridge(request)
    run_mgr = get_run_manager(request)
    run_ctx = get_run_context(request)

    disconnect = DisconnectMode.cancel if body.on_disconnect == "cancel" else DisconnectMode.continue_
```

**► 逐行注解**：
- **`bridge`**（StreamBridge）：SSE 事件桥接器，负责把 Agent 的流式输出转成 SSE 推给前端。在应用启动时由 `lifespan()` 创建，存在 `app.state` 上。
- **`run_mgr`**（RunManager）：运行注册表，管理所有活跃 run 的状态。
- **`run_ctx`**（RunContext）：基础设施依赖包，包含 checkpointer、store、event_store 等（详见阶段 ④ 的 RunContext 字段表）。
- **`disconnect`**：断开语义。`DisconnectMode` 是从 `deerflow.runtime` 导入的枚举（不是 `get_*` 单例），`cancel`（默认）= 用户关浏览器就取消 run；`continue_` = 继续在后台跑。我们的场景是 `cancel`。

#### 步骤 2：模型白名单验证

```python
# 引用位置：backend/app/gateway/services.py:631-646
    body_context = getattr(body, "context", None) or {}
    model_name = body_context.get("model_name")
    if model_name is not None and not isinstance(model_name, str):
        model_name = str(model_name)
    if model_name:
        app_config = get_app_config()
        resolved = app_config.get_model_config(model_name)
        if resolved is None:
            raise HTTPException(
                status_code=400,
                detail=f"Model {model_name!r} is not in the configured model allowlist",
            )
```

**► 注解**：
- 用户传的 `model_name` 在 `body.context` 里取（不是顶层字段），且会做类型规范化（非字符串转字符串）。
- `"doubao-seed-2-0-code"` 必须在 `config.yaml` 的 `models[]` 列表里。否则 400 拒绝。这防止用户指定未授权的模型。

**设计动机**：为什么不放行任意模型名？因为 (1) 模型可能有不同的 API key 和计费，不能让用户随便选；(2) 安全考虑，防止注入未审核的模型。

#### 步骤 3：线程所有权检查（双轨）

```python
# 引用位置：backend/app/gateway/services.py:648-670
    owner_user_id = get_trusted_internal_owner_user_id(request)
    # ... 大段注释解释为什么需要二次检查 ...
    user = getattr(request.state, "user", None)
    if user is not None:
        allowed = await run_ctx.thread_store.check_access(thread_id, str(user.id))
        if not allowed and owner_user_id and getattr(user, "system_role", None) == INTERNAL_SYSTEM_ROLE:
            # Channel workers may also act for the connection owner named in
            # the trusted header (e.g. claiming a legacy default-owned channel
            # thread for its real owner).
            allowed = await run_ctx.thread_store.check_access(thread_id, owner_user_id)
        if not allowed:
            raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
```

**► 逐行注解**：
- **`owner_user_id = get_trusted_internal_owner_user_id(request)`**（行 648）：从 `X-DeerFlow-Owner-User-Id` header 取出可信的 owner（IM 渠道 worker 代用户操作时带这个 header）。普通 HTTP 请求没有这个 header，返回 None。
- **`run_ctx.thread_store.check_access(thread_id, str(user.id))`**（行 663）：检查当前用户是否有权访问这个线程。
- **双轨回退**（行 664-668）：如果当前用户没权限，但它是内部系统角色（IM 渠道 worker，`INTERNAL_SYSTEM_ROLE`）且带了 owner header，再用 `owner_user_id` 查一次。这让 IM worker 能代真实用户操作。
- **404 而非 403**（行 669-670）：**反枚举设计**——返回 404（"不存在"）而非 403（"无权"），不暴露"这个线程存在但你没权限"的信息。

**设计动机深挖**：
> **安全设计原则：在执行不可逆操作前，做最后一道防线检查。**
> 虽然 `@require_permission` 已经查过一次，但这里的 `thread_id` 来自请求 body（stateless run 端点），path param 检查保护不到。**永远不要假设上游检查够了，关键操作前自己再查一次。**

#### 步骤 4：创建 Run（create_or_reject）

```python
# 引用位置：backend/app/gateway/services.py:672-693
    owner_context_token = set_current_user(SimpleNamespace(id=owner_user_id)) if owner_user_id else None
    try:
        try:
            async with goal_thread_lock(thread_id):
                record = await run_mgr.create_or_reject(
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

**► 逐行注解**：
- **`owner_context_token = set_current_user(...)`**（行 672）：如果有 `owner_user_id`（IM 场景），临时把 ContextVar 切换成 owner 的身份，这样后续的 per-user 操作（文件路径等）用 owner 的身份。最外层 `finally`（行 768-770）里会 `reset_current_user` 恢复——这一步把整段步骤 4-7 包了起来，文档里必须理解"先切身份 → 干活 → 复位"的边界。
- **`async with goal_thread_lock(thread_id)`**（行 675）：**目标锁**。同一个 thread 的 run 创建要串行化——防止两个并发请求同时创建 run，破坏目标延续循环的状态一致性。
- **`run_mgr.create_or_reject(...)`**（行 676-689）：原子性的"检查并插入"。核心参数：
  - `multitask_strategy=body.multitask_strategy`：多任务策略。我们的场景是 `"reject"`。
  - `kwargs={"input": body.input, "config": redact_config_secrets(body.config)}`（行 685）：**注意 `redact_config_secrets`**——持久化前脱敏！run 记录会写到数据库并被 API 回显，不能让 secret 混进去（issue #3861）。
  - `user_id=owner_user_id`：归属用户。
- **异常处理**：`ConflictError`（409，线程已有活跃 run）→ HTTP 409；`UnsupportedStrategyError`（501）。

**`multitask_strategy` 的取值**（设计动机深挖，两套值要对齐看）：

| 策略 | API 是否接受 | RunManager 是否支持 | 行为 / 适用场景 |
|------|------|------|------|
| `reject` | ✓ | ✓ | 线程已有活跃 run → 抛 `ConflictError` (409)。默认值，防止并发冲突 |
| `interrupt` | ✓ | ✓ | 先取消正在跑的 run（标记 `interrupted`），再创建新的。用户发新消息想中断当前任务 |
| `rollback` | ✓ | ✓ | 先取消并回滚旧的（恢复 pre-run checkpoint），再创建。用户想完全重来 |
| `enqueue` | ✓（`thread_runs.py:88` 的 Literal 列了它） | ✗（`manager.py:949` 的 `_supported_strategies` 没列） | API 层放行、运行时抛 `UnsupportedStrategyError` → **HTTP 501**。LangGraph 协议保留位，DeerFlow 尚未实现 |

> 即"客户端能传 4 个，但实际能跑的只有 3 个"——这种 API 与 runtime 的策略不对齐是有意的，便于将来加 `enqueue` 时只改 `RunManager` 一处。

#### 步骤 5：线程元数据 upsert

```python
# 引用位置：backend/app/gateway/services.py:695-715
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
            await run_ctx.thread_store.create(thread_id, assistant_id=body.assistant_id, metadata=body.metadata)
        else:
            await run_ctx.thread_store.update_status(thread_id, "running")
    except Exception:
        logger.warning("Failed to upsert thread_meta for %s (non-fatal)", sanitize_log_param(thread_id))
```

**► 逐行注解**：
- **为什么需要 upsert**：有些线程是"隐式创建"的（比如 stateless run 端点直接传 thread_id，没先 POST /threads 创建）。这里确保线程元数据存在，这样它才能出现在 `/threads/search` 列表里。
- **unscoped 回查 + `update_owner`**（行 700-705）：处理"遗留数据认领"——有些老线程是 `user_id=None`（pre-auth 时代创建的），现在有 owner 了，就把它认领过来。
- **`except Exception: logger.warning(...)`**（行 714-715）：**非致命**——线程元数据写失败不影响 run 执行。`sanitize_log_param` 防止 thread_id（可能含敏感信息）污染日志。

#### 步骤 6：构建输入和配置

```python
# 引用位置：backend/app/gateway/services.py:717-742
        agent_factory = resolve_agent_factory(body.assistant_id)
        is_internal_caller = getattr(getattr(request, "state", None), "auth_source", None) == AUTH_SOURCE_INTERNAL
        command = getattr(body, "command", None)
        if command and command.get("resume") is not None:
            graph_input = Command(resume=command["resume"])
        else:
            graph_input = normalize_input(body.input, trusted_internal=is_internal_caller)
        config = build_run_config(thread_id, body.config, body.metadata, assistant_id=body.assistant_id)
        await apply_checkpoint_to_run_config(config, body=body, thread_id=thread_id, request=request)

        merge_run_context_overrides(config, getattr(body, "context", None), internal=is_internal_caller)
        if not is_internal_caller:
            strip_internal_context_keys(config)
        internal_owner_user = await resolve_trusted_internal_owner_for_attribution(request, owner_user_id)
        inject_authenticated_user_context(config, request, internal_owner_user=internal_owner_user, request_context=getattr(body, "context", None))
        stream_modes = normalize_stream_modes(body.stream_mode)
```

**► 逐行注解**：
- **`resolve_agent_factory(body.assistant_id)`**（行 717）：**别被函数名骗了**——这个函数不看 `assistant_id`，无论传什么（`"lead_agent"`、自定义 agent 名、甚至 `None`），**一律返回 `make_lead_agent`**（`services.py:380-391`）。所有 agent 都共用同一个工厂，区别在配置：自定义 agent 通过 `build_run_config`（`services.py:534-537`）把 `agent_name` 写进 `configurable` 和 `context`，`_make_lead_agent` 后续读到 `agent_name` 时才走自定义 agent 配置（加载 `agents/<name>/` 下的 SOUL.md/config）。所以"路由"实际发生在工厂内部读 cfg 那一刻，不是 `resolve_agent_factory` 里。
- **`is_internal_caller`**（行 718）：是否内部调用（IM worker）。影响输入信任度——内部调用可信，外部调用要 scrub。
- **`graph_input = normalize_input(body.input, trusted_internal=is_internal_caller)`**（行 723）：标准化输入。我们的场景输出就是 `{"messages": [HumanMessage("帮我分析...")]}`。
- **`config = build_run_config(...)`**（行 724）：构建 LangGraph 配置，包含 `thread_id`、`checkpoint_ns`、`recursion_limit` 等（详见 1.6 框架注入表）。
- **`merge_run_context_overrides(config, body.context, ...)`**（行 731）：把用户传的 `context`（model_name、thinking_enabled 等）合并进 config。
- **`strip_internal_context_keys(config)`**（行 732-735）：外部调用时，**清除**内部专用的 context key——防止外部用户伪造内部参数（如 `is_internal`、`channel_user_id`）。
- **`inject_authenticated_user_context(config, request, ...)`**（行 736-742）：把认证用户身份（`user_id`、`is_internal`、`user_role` 等）注入 config 的 `context`——注意是写到 `context` 而非 `configurable`，详见 1.6。
- **`stream_modes = normalize_stream_modes(body.stream_mode)`**（行 744）：标准化流模式（`["values", "messages-tuple"]` 之类）。

#### 步骤 7：启动后台任务（关键！）

```python
# 引用位置：backend/app/gateway/services.py:746-770
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
        return record
    finally:
        if owner_context_token is not None:
            reset_current_user(owner_context_token)
```

**► 逐行注解**：
- **`asyncio.create_task(run_agent(...))`**（行 746-760）：**把真正的 Agent 执行丢到后台 task，立即返回！** 这是 SSE 流式的基础——HTTP 响应不能阻塞等 Agent 完成（可能要几分钟），必须立即开始流式推送。
- **`record.task = task`**（行 761）：把 asyncio Task 引用存到 record 里，这样后续可以 cancel 它。
- **`return record`**（行 767）：路由函数拿到 record，立即开始返回 SSE 流（阶段 ③）。
- **`finally: reset_current_user(...)`**（行 768-770）：恢复 ContextVar，对应步骤 4 开头的 `set_current_user`。**注意**：后台 task 里用的是 `copy_context()`（ContextVar 会复制），所以重置不影响后台 task。

**输出样例**：
```python
record = RunRecord(
    run_id="run-xyz-789",
    thread_id="thread-abc-123",
    status="pending",               # 刚创建，还没开始跑
    assistant_id="lead_agent",
    model_name="doubao-seed-2-0-code",
    owner_worker_id="worker-1",
    lease_expires_at="2026-07-16T12:01:00Z",  # 租约30秒后过期
    task=<asyncio.Task>,            # 后台任务引用
    ...
)
```

---

### 阶段 ③：立即返回 SSE 流

路由函数返回 record 后，FastAPI 把它转成 `StreamingResponse`：

```python
# 引用位置：backend/app/gateway/routers/thread_runs.py:505-521 (简化)
# record = await start_run(body, thread_id, request)
# return StreamingResponse(
#     sse_consumer(bridge, record, request, run_mgr),
#     media_type="text/event-stream",
#     headers={
#         "Cache-Control": "no-cache",
#         "Connection": "keep-alive",
#         "X-Accel-Buffering": "no",                 # 关闭 Nginx 缓冲，SSE 必须
#         "Content-Location": f"/api/threads/{thread_id}/runs/{record.run_id}",
#     },
# )
```

**► 注解**：
- **`Content-Location` header** 指向 run 的资源 URL（`/api/threads/{thread_id}/runs/{run_id}`），前端 LangGraph SDK 用一个**贪婪正则**从这个 path 里提取 `run_id`——所以路径必须是规范的 run 资源 URL，不能加额外后缀（注释在 `thread_runs.py:515-518`）。
- **`sse_consumer`** 是异步生成器，持续从 `StreamBridge` 订阅事件，转成 SSE 格式推给浏览器，直到收到 `END_SENTINEL` 或客户端断开。
- **`X-Accel-Buffering: no`**：Nginx 专用头，关闭响应缓冲——SSE 必须实时推送，不能攒批。

**此时 HTTP 层面的工作完成了。请求阶段 ①②③ 在毫秒级完成，用户已经开始看到 SSE 流的 metadata 事件。真正的 Agent 执行在后台 task（阶段 ④及以后）。**

---

### 阶段 ④：后台 worker 执行（run_agent 极致详解）

这是整个系统的心脏。`run_agent` 函数体本身从旧版约 320 行扩展到约 474 行（行 246-719；**注意是函数体，不是整个文件**——`worker.py` 整个文件 1576 行，后面还有 ~850 行的辅助函数 `_prepare_goal_continuation_input`、`_rollback_to_pre_run_checkpoint`、`_ensure_interrupted_title` 等）。我们先看它的完整签名和依赖，再逐阶段剖析。

#### 4.1 RunContext：基础设施依赖包

```python
# 引用位置：backend/packages/harness/deerflow/runtime/runs/worker.py:130-145
@dataclass(frozen=True)
class RunContext:
    """Infrastructure dependencies for a single agent run.

    Groups checkpointer, store, and persistence-related singletons so that
    ``run_agent`` (and any future callers) receive one object instead of a
    growing list of keyword arguments.
    """

    checkpointer: Any                        # 行 139：状态持久化（checkpoint）
    store: Any | None = field(default=None)  # 行 140：长期记忆存储
    event_store: Any | None = field(default=None)  # 行 141：事件日志存储
    run_events_config: Any | None = field(default=None)  # 行 142
    thread_store: Any | None = field(default=None)  # 行 143：线程元数据
    app_config: AppConfig | None = field(default=None)  # 行 144
    on_run_completed: Any | None = field(default=None)  # 行 145：run完成回调
```

**► 设计动机**：**依赖注入模式**。worker 不自己 new 这些对象，而是从外部接收。好处：(1) 易测试（可以传 mock）；(2) 所有 run 共享同一套基础设施；(3) worker 不关心这些对象怎么来的。注意类标了 `frozen=True`——构造后不可变，避免运行中误改依赖。

#### 4.2 run_agent 完整签名

```python
# 引用位置：backend/packages/harness/deerflow/runtime/runs/worker.py:246-259
async def run_agent(
    bridge: StreamBridge,
    run_manager: RunManager,
    record: RunRecord,
    *,
    ctx: RunContext,
    agent_factory: Any,
    graph_input: dict,
    config: dict,
    stream_modes: list[str] | None = None,
    stream_subgraphs: bool = False,
    interrupt_before: list[str] | Literal["*"] | None = None,
    interrupt_after: list[str] | Literal["*"] | None = None,
) -> None:
    """Execute an agent in the background, publishing events to *bridge*."""
```

**► 注解**：**函数签名在本次演进中完全没变**——这是个好的工程信号：内部剧烈演进（目标延续、RunJournal、workspace changes），但对外接口稳定。调用方（services.py）不需要改。

#### 4.3 _SubagentEventBuffer：子 Agent 事件批量持久化（新增）

这是一个本次新增的辅助类，值得单独讲，因为它体现了一个重要的性能优化模式：

```python
# 引用位置：backend/packages/harness/deerflow/runtime/runs/worker.py:184-243
class _SubagentEventBuffer:
    """Buffer subagent ``task_*`` step events and flush them in one locked batch (#3779).

    The live SSE bridge already forwards these events for real-time display; this
    additionally writes them so the subtask card's step history survives a reload.

    ``RunEventStore.put`` is documented as a low-frequency path — on Postgres each
    call opens its own transaction and takes a per-thread advisory lock. A deep
    subagent (``general-purpose`` runs up to ``max_turns=150``) emits hundreds of
    ``task_running`` steps on the hot stream loop, so persisting each with
    ``put()`` would serialize against the run's own message-batch writer. This
    accumulates recognized subagent events and writes them with ``put_batch``,
    which acquires the lock once per batch, honoring the store's contract.
    """

    FLUSH_THRESHOLD = 25  # 行 206：攒够25条刷一次

    def __init__(self, event_store, thread_id, run_id):  # 行 208
        self._event_store = event_store
        self._thread_id = thread_id
        self._run_id = run_id
        self._pending: list[dict[str, Any]] = []

    async def add(self, chunk: Any) -> None:  # 行 214
        """Buffer one custom stream chunk; flush on a terminal event or threshold."""
        if self._event_store is None:
            return
        from deerflow.subagents.step_events import subagent_run_event  # 行 222：懒导入
        record = subagent_run_event(chunk)
        if record is None:
            return
        self._pending.append({"thread_id": self._thread_id, "run_id": self._run_id, **record})
        if record["event_type"] == "subagent.end" or len(self._pending) >= self.FLUSH_THRESHOLD:
            await self.flush()

    async def flush(self) -> None:  # 行 231
        """Persist buffered events in one ``put_batch`` call; swallow store errors."""
        if self._event_store is None or not self._pending:
            return
        batch = self._pending
        self._pending = []
        try:
            await self._event_store.put_batch(batch)
        except Exception:
            # Rebuffer the failed batch (ahead of any events queued since)  行 242
            self._pending = batch + self._pending
            logger.warning(...)
```

**► 逐行注解 + 设计动机深挖**：

- **问题背景**（注释说得很详细）：子 Agent（`general-purpose`）最多跑 150 轮（`max_turns=150`），每轮都发 `task_running` 事件。如果每个事件都调 `event_store.put()` 持久化，在 Postgres 上每次都要开事务 + 获取线程级 advisory lock。几百个事件串行化获取锁，会**阻塞 run 自己的消息批写入**——性能灾难。

- **解决方案**：**批量缓冲**。攒够 25 条（`FLUSH_THRESHOLD`）或遇到终态事件（`subagent.end`）时，一次性 `put_batch`——只获取一次锁。

- **第 214-217 行的懒导入注释**："importing deerflow.subagents at module load triggers its package __init__ (executor → agents → tools → task_tool), which imports back from deerflow.subagents and deadlocks at gateway startup"。这是**循环导入**的经典坑——在模块顶层 import 会导致死锁，所以延迟到调用时 import。

- **第 236-238 行的 rebuffer**：`put_batch` 失败时，把失败的 batch **重新放回队列头部**（`batch + self._pending`），不丢弃。瞬态错误不丢数据。

**设计模式总结**：这是"**写聚合（write coalescing）**"模式——高频小写入合并成低频批写入。在数据库性能优化里极其常见（比如日志系统、监控指标上报）。

---

#### 4.4 run_agent 执行流程（逐阶段，带数据样例）

由于 `run_agent` 函数体有 470 行，我按阶段拆解。每个阶段给出"此时 state 长什么样"。

##### 步骤 0：等待前序 run 收尾（新增）

```python
# 引用位置：backend/packages/harness/deerflow/runtime/runs/worker.py:297-298
    try:
        await run_manager.wait_for_prior_finalizing(thread_id, run_id)
```

**► 注解**：同一个线程可能有多个 run 在交接（用户连发消息，第二条 interrupt 了第一条）。`wait_for_prior_finalizing` 确保前一个 run 的 `finalizing` 状态收尾完毕后，当前 run 才开始——避免两个 run 同时写 checkpoint 造成状态损坏。

**数据流样例**：
```
t=0: run-A 开始执行
t=1: 用户发第二条消息 → run-B 创建（interrupt 策略）
t=1: run-A 被取消，进入 finalizing 状态
t=1: run-B 的 worker 调 wait_for_prior_finalizing
t=2: run-A finalizing 完成（checkpoint 清理、title 同步等）
t=2: run-B 的 wait返回，开始执行  ← 确保不冲突
```

##### 步骤 1：初始化 RunJournal（新增）+ 标记 running

```python
# 引用位置：backend/packages/harness/deerflow/runtime/runs/worker.py:306-318
        if event_store is not None:
            from deerflow.runtime.journal import RunJournal
            journal = RunJournal(
                run_id=run_id,
                thread_id=thread_id,
                event_store=event_store,
                track_token_usage=getattr(run_events_config, "track_token_usage", True),
                progress_reporter=lambda snapshot: run_manager.update_run_progress(run_id, **snapshot),
            )

        # 1. Mark running
        await run_manager.set_status(run_id, RunStatus.running)
```

**► 注解**：
- **RunJournal**（新增）：本次 run 的"记账本"。它作为 LangChain callback handler 挂到 graph 上，在 `on_llm_end` 时捕获 token 用量，在 `on_chain_start/end` 时捕获生命周期事件。run 结束后，这些数据刷到 event_store，用于可观测性。
- **`progress_reporter=lambda snapshot: run_manager.update_run_progress(run_id, **snapshot)`**：RunJournal 统计到进度变化时，回调更新 RunManager 的 record。这让前端能实时看到 token 消耗。
- **`set_status(running)`**：状态从 `pending` → `running`。

**为什么 RunJournal 初始化在 try 块里？**（注释 296-301 行解释）：如果初始化抛异常（比如 DB 连接失败），要能走到 except/finally 发 `end` 事件——否则 SSE 流会永远挂起没有终止符。

##### 步骤 2：工作区快照（新增）

```python
# 引用位置：backend/packages/harness/deerflow/runtime/runs/worker.py:320-328
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

**► 注解**：**workspace changes 子系统的基础**。运行前对工作区拍快照，结束后再拍一个，diff 得到"这次 run 改了哪些文件"。

**数据流样例**：
```python
# 运行前快照
pre_run_workspace_snapshot = {
    "uploads/sales.csv": {"size": 15234, "mtime": "2026-07-16T10:00:00Z"},
    "workspace/": {"files": []},  # 工作区空
    "outputs/": {"files": []},    # 产出目录空
}
# 运行后快照
post_run_workspace_snapshot = {
    "uploads/sales.csv": {"size": 15234, "mtime": "2026-07-16T10:00:00Z"},
    "workspace/analysis.py": {"size": 2048, "mtime": "..."},  # 新增
    "outputs/trend.png": {"size": 45678, "mtime": "..."},     # 新增
}
# diff 结果：created workspace/analysis.py, outputs/trend.png
```

这让用户能看到"Agent 具体创建了什么"，而不只是看聊天文字。`try/except` 降级——快照失败不阻塞执行。

##### 步骤 3：快照 pre-run checkpoint（用于回滚）

```python
# 引用位置：backend/packages/harness/deerflow/runtime/runs/worker.py:330-347
        # Snapshot the latest pre-run checkpoint so rollback can restore it.
        if checkpointer is not None:
            try:
                config_for_check = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
                ckpt_tuple = await checkpointer.aget_tuple(config_for_check)
                if ckpt_tuple is not None:
                    ckpt_config = getattr(ckpt_tuple, "config", {}).get("configurable", {})
                    pre_run_checkpoint_id = ckpt_config.get("checkpoint_id")
                    pre_run_snapshot = {
                        "checkpoint_ns": ckpt_config.get("checkpoint_ns", ""),
                        "checkpoint": copy.deepcopy(getattr(ckpt_tuple, "checkpoint", {})),
                        "metadata": copy.deepcopy(getattr(ckpt_tuple, "metadata", {})),
                        "pending_writes": copy.deepcopy(getattr(ckpt_tuple, "pending_writes", []) or []),
                    }
                    pre_existing_message_ids = _collect_pre_existing_message_ids(pre_run_snapshot)
            except Exception:
                snapshot_capture_failed = True
                logger.warning(...)
```

**► 逐行注解**：
- **目的**：保存"run 开始前的 checkpoint 快照"。如果用户后来选 `rollback` 取消，就恢复到这个快照——仿佛 run 从未发生。
- **`deepcopy`**：**深拷贝**！checkpoint 是可变对象，如果不拷贝，后续 run 修改 checkpoint 会反向污染这个"快照"。
- **`_collect_pre_existing_message_ids`**（新增）：收集"run 开始前已存在的消息 ID"。用于后续屏蔽历史遗留的 error fallback 标记——不把旧消息的错误归因到本次 run。

##### 步骤 4：发布 metadata 事件

```python
# 引用位置：backend/packages/harness/deerflow/runtime/runs/worker.py:349-357
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

**► 注解**：**第一个 SSE 事件总是 `metadata`**。前端的 LangGraph `useStream` hook 必须先收到 `run_id` 和 `thread_id` 才能关联后续事件。这个约定和官方 LangGraph Platform 完全一致。

**SSE 输出样例**：
```
event: metadata
data: {"run_id": "run-xyz-789", "thread_id": "thread-abc-123"}

```

##### 步骤 5：构建 Agent（调用工厂）

```python
# 引用位置：backend/packages/harness/deerflow/runtime/runs/worker.py:359-446 (核心部分)
        # 3. Build the agent
        from langchain_core.runnables import RunnableConfig
        from langgraph.runtime import Runtime

        # Inject runtime context so middlewares and tools (via ToolRuntime.context) can
        # access thread-level data. langgraph-cli does this automatically; we must do it
        # manually here because we drive the graph through ``agent.astream(config=...)``
        # without passing the official ``context=`` parameter.
        runtime_ctx = _build_runtime_context(thread_id, run_id, config.get("context"), ctx.app_config)
        runtime_ctx[CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY] = frozenset(pre_existing_message_ids)
        # ... 解析并注入 deerflow_trace_id（行 369-376）...
        if journal is not None:
            runtime_ctx["__run_journal"] = journal              # 行 382
        _install_runtime_context(config, runtime_ctx)            # 行 383
        runtime = Runtime(context=cast(Any, runtime_ctx), store=store)  # 行 384
        config.setdefault("configurable", {})["__pregel_runtime"] = runtime  # 行 385

        # 把 RunJournal 也作为 LangChain callback 挂上（行 389-390）
        if journal is not None:
            config.setdefault("callbacks", []).append(journal)

        # 注入 Langfuse trace-attribute metadata（行 396-404）
        inject_langfuse_metadata(config, thread_id=thread_id, user_id=resolve_runtime_user_id(runtime), ...)

        config.setdefault("run_name", resolve_root_run_name(config, record.assistant_id))  # 行 408
        initial_runnable_config = RunnableConfig(**config)       # 行 409

        # 工厂调用（行 420-423）
        if ctx.app_config is not None and _agent_factory_supports_app_config(agent_factory):
            agent = agent_factory(config=initial_runnable_config, app_config=ctx.app_config)
        else:
            agent = agent_factory(config=initial_runnable_config)

        # 捕获实际生效的 model_name 写回 record（行 429-434）
        # _resolve_model_name 可能回退到默认模型，这里同步真实模型名
```

**► 逐行注解**：
- **`_build_runtime_context`**（行 367）：构建运行时上下文，包含 thread_id、run_id、app_config、trace_id 等。中间件和工具通过 `ToolRuntime.context` 读取这些值。
- **`runtime_ctx["__run_journal"] = journal`**（行 382）：把 RunJournal 暴露给中间件（比如 `SafetyFinishReasonMiddleware` 写审计事件）。`__` 前缀标记为运行时内部 channel，用户代码不应依赖此 key 名。
- **`_install_runtime_context(config, runtime_ctx)`**（行 383）：把 runtime_ctx 注入 config，让 LangGraph 能传给图节点。
- **`runtime = Runtime(context=..., store=store)`**（行 384）+ **`config["configurable"]["__pregel_runtime"] = runtime`**（行 385）：创建 LangGraph Runtime 对象并手动注入——因为 worker 用 `agent.astream(config=...)` 驱动图，没走 LangGraph Server 的自动注入。
- **`agent = agent_factory(config=initial_runnable_config)`**（行 423）：**调用 Agent 工厂！** 这里 `agent_factory` 就是 `make_lead_agent`。一个配置字典变成了编译好的 LangGraph。**这一步的细节是第 2 章的主题。**
- **写回 effective model_name**（行 429-434）：如果工厂内部把请求的模型名回退成了默认模型，要把这个"真实跑的模型名"同步回 `RunRecord`——否则前端/计费看到的模型名会不对。

**设计动机深挖**——`_agent_factory_supports_app_config` 的优雅降级：
```python
# worker.py:164-181（含 lru_cache 缓存的版本）
def _compute_agent_factory_supports_app_config(agent_factory: Any) -> bool:
    try:
        return "app_config" in inspect.signature(agent_factory).parameters
    except (TypeError, ValueError):
        return False

@lru_cache(maxsize=128)
def _cached_agent_factory_supports_app_config(agent_factory: Any) -> bool: ...

def _agent_factory_supports_app_config(agent_factory: Any) -> bool:
    return _cached_agent_factory_supports_app_config(agent_factory)
```
这个辅助函数用 `inspect.signature` 检查工厂函数是否接受 `app_config` 参数。如果工厂是新版（支持注入 app_config），就传；如果是旧版（不支持），就不传。**这让新旧工厂函数都能工作**——向后兼容的优雅设计，并加了 `lru_cache` 避免每次 run 都反射一次。

> ⚠️ 注意：默认的 `make_lead_agent` 公开签名是 `make_lead_agent(config: RunnableConfig)`——**没有** `app_config` 参数，所以 `_agent_factory_supports_app_config` 对它返回 `False`，永远走 `agent_factory(config=...)` 分支。`app_config` 参数存在于内部的 `_make_lead_agent(config, *, app_config)`，由 `make_lead_agent` 自己通过 `get_app_config()` 解析。这个 app_config 分支是为**自定义工厂**预留的兼容口子。

##### 步骤 5b：挂 checkpointer / store（注释 #4，行 436-440）

```python
# 引用位置：backend/packages/harness/deerflow/runtime/runs/worker.py:436-440
        # 4. Attach checkpointer and store
        if checkpointer is not None:
            agent.checkpointer = checkpointer
        if store is not None:
            agent.store = store
```

**► 注解**：工厂返回的 agent 默认不知道用哪个 checkpointer/store——这俩是 worker 自己注入的。挂上之后，`agent.astream(...)` 才能在每步自动写 checkpoint、读历史消息。

##### 步骤 5c：设 interrupt 节点（注释 #5，行 442-446）

```python
# 引用位置：backend/packages/harness/deerflow/runtime/runs/worker.py:442-446
        # 5. Set interrupt nodes
        if interrupt_before:
            agent.interrupt_before_nodes = interrupt_before
        if interrupt_after:
            agent.interrupt_after_nodes = interrupt_after
```

**► 注解**：支持 `interrupt_before`/`interrupt_after`（来自 `RunCreateRequest`）。比如 plan mode 想在某节点前停下来等人审核，就把节点名传进来。这里设置到编译后的 graph 上。

##### 步骤 5d：构造 LangGraph stream_mode 列表（注释 #6，行 448-472）

```python
# 引用位置：backend/packages/harness/deerflow/runtime/runs/worker.py:448-472
        # 6. Build LangGraph stream_mode list
        #    "events" is NOT a valid astream mode — skip it
        #    "messages-tuple" maps to LangGraph's "messages" mode
        lg_modes: list[str] = []
        for m in requested_modes:
            if m == "messages-tuple":
                lg_modes.append("messages")    # 协议名 → LangGraph 内部名
            elif m == "events":
                continue                        # events 不是合法 astream mode，跳过
            elif m in _VALID_LG_MODES:
                lg_modes.append(m)
        if not lg_modes:
            lg_modes = ["values"]               # 兜底
        # 去重保序（行 463-470）
        ...
        subagent_events = _SubagentEventBuffer(event_store, thread_id, run_id)  # 行 477
```

**► 注解**：把前端用的协议名（`messages-tuple`、`values`、`custom` 等）翻译成 LangGraph 的 `astream` 模式名。`_SubagentEventBuffer` 在这里实例化（前面 4.3 讲过），稍后被 `_stream_once` 用到。

##### 步骤 6：定义 `_stream_once` 闭包（行 490-525）

真正的"流式跑一轮"被封装在闭包里，因为目标延续循环要复用它：

```python
# 引用位置：backend/packages/harness/deerflow/runtime/runs/worker.py:490-525 (简化)
        async def _stream_once(input_payload: Any, stream_config: RunnableConfig) -> None:
            nonlocal llm_error_fallback_message
            async with _checkpoint_thread_lock(thread_id):       # 同 thread 的 checkpoint 写入串行化
                if len(lg_modes) == 1 and not stream_subgraphs:
                    # 单模式：astream 直接 yield chunk
                    async for chunk in agent.astream(input_payload, config=stream_config, stream_mode=single_mode):
                        if record.abort_event.is_set(): break    # 用户取消
                        llm_error_fallback_message = ... or _extract_llm_error_fallback_message(chunk, pre_existing_message_ids)
                        await bridge.publish(run_id, sse_event, serialize(chunk, mode=single_mode))
                        if single_mode == "custom":
                            await subagent_events.add(chunk)     # 子 Agent 事件入批
                else:
                    # 多模式：astream yield (mode, chunk) 元组
                    async for item in agent.astream(input_payload, config=stream_config, stream_mode=lg_modes, subgraphs=stream_subgraphs):
                        ...
```

**► 注解**：
- **`_checkpoint_thread_lock(thread_id)`**：把同一 thread 的 checkpoint 写入串行化——避免两个并发的 `_stream_once`（理论上不会同时发生，但防御性加锁）。
- **`_extract_llm_error_fallback_message`**：从流里识别 LLM 错误回退消息（带 `deerflow_error_fallback` 标记的 AIMessage），如果撞到了，记下来供步骤 8 判定终态。`pre_existing_message_ids` 用于**屏蔽 run 开始前就存在的历史错误消息**——不把旧错误归因到本次 run。
- **`bridge.publish(run_id, sse_event, ...)`**：把每个 chunk 实时推给前端。

##### 步骤 7：流式执行 + 目标延续循环（最重要的新增！）

```python
# 引用位置：backend/packages/harness/deerflow/runtime/runs/worker.py:527-550
        # 7. Stream the requested turn, then optionally continue hidden goal turns.
        # Clear any stale stop_reason before the first (user-visible) turn only.
        # Continuation turns preserve a cap reason from the user turn: a run that
        # hits a cap during the user turn IS capped even if hidden goal-evaluator
        # turns complete cleanly afterward (#4176 review).
        if isinstance(runtime.context, dict):
            runtime.context.pop("stop_reason", None)
        await _stream_once(graph_input, initial_runnable_config)
        while not record.abort_event.is_set() and not llm_error_fallback_message and (journal is None or not journal.had_llm_error_fallback):
            continuation_input = await _prepare_goal_continuation_input(
                bridge=bridge,
                checkpointer=checkpointer,
                thread_id=thread_id,
                run_id=run_id,
                model_name=record.model_name,
                app_config=ctx.app_config,
                evaluator_model_factory=_get_goal_evaluator_model,
                abort_event=record.abort_event,
                user_id=resolve_runtime_user_id(runtime),
                deerflow_trace_id=deerflow_trace_id,
            )
            if continuation_input is None or record.abort_event.is_set():
                break
            await _stream_once(continuation_input, _continuation_runnable_config())
```

**► 逐行注解（这是全篇最重要的新机制）**：

- **第 527 行注释**："Stream the requested turn, then optionally continue hidden goal turns."——两阶段执行。

- **第 532-533 行 `stop_reason` 清理**（注释很重要）：
  > "Clear any stale stop_reason before the first (user-visible) turn only. Continuation turns preserve a cap reason from the user turn."
  
  只在**第一个用户可见 turn 前**清理 `stop_reason`。续轮**保留**它。为什么？如果一个 run 在用户 turn 撞了 token 上限（`token_capped`），即使后续续轮干净完成，整个 run 也算被 cap 了。**防止 Agent 通过续轮"绕过"用户 turn 的限制**（issue #4176 review）。

- **第 534 行 `await _stream_once(graph_input, initial_runnable_config)`**：**第一阶段——用户可见 turn**。跑用户发来的消息，结果流式推给前端。这和旧版一样。

- **第 535-550 行 `while` 循环**：**第二阶段——目标延续循环**。用户 turn 结束后，循环判断"目标达成了吗"，没达成就跑隐藏续轮。

- **`while` 的三个终止条件**：
  1. `record.abort_event.is_set()`：用户点了停止。
  2. `llm_error_fallback_message`：LLM 调用失败（被 `_stream_once` 检测到）。
  3. `journal.had_llm_error_fallback`：RunJournal 记录到错误。

- **`_prepare_goal_continuation_input(...)`**（定义在 `worker.py:866-1055`，是个 ~190 行的大函数）：这是延续循环的核心。它调用一个 **goal-evaluator 模型**（通常是小模型，非 thinking 模式），传入当前对话状态和目标（`state.goal`），让模型判断"目标是否达成"：
  - 已达成 → 返回 `None`，循环结束。
  - 未达成 → 返回 `continuation_input`（一个隐藏 `HumanMessage`），worker 用它再跑一轮。
  
  还内置了**无进展熔断**：连续 2 次续轮没有产生新的可见 assistant 输出就停（`max_no_progress_continuations=2`），以及硬上限 `max_continuations`（默认 8，`0-8` 范围）。

- **`await _stream_once(continuation_input, _continuation_runnable_config())`**：跑隐藏续轮。`_continuation_runnable_config()`（定义在行 411-418）会克隆 config，强制 `checkpoint_ns=""`、弹出 `checkpoint_id`/`checkpoint_map`——确保续轮写到主时间线而不是分支。

**数据流样例（目标延续的完整旅程）**：

```
用户消息: "帮我分析 sales.csv，画出月度趋势图，写一份分析报告"

【第一阶段：用户可见 turn】
  _stream_once(graph_input)
  → Agent 读 CSV、分析数据、画图、写报告
  → 前端看到所有步骤（流式）
  → Agent 回复："我已完成分析，趋势图和分析报告已生成"
  → 前端显示最终回复

【第二阶段：目标延续循环，对用户隐藏】
  第1次 _prepare_goal_continuation_input:
    goal-evaluator 检查 state.goal = "分析CSV+画图+写报告"
    判断: 画图完成✓ 报告完成✓ 但"分析"够深入吗?
    返回: continuation_input（继续深化分析）

  _stream_once(continuation_input)  ← 隐藏turn，前端看不到
  → Agent 补充更深入的数据洞察

  第2次 _prepare_goal_continuation_input:
    goal-evaluator 判断: 所有子目标达成✓
    返回: None
  
  循环结束 → 进入收尾
```

**设计意义**：这让 Agent 从"问一句答一句"变成"接到目标后持续工作直到完成"。用户不需要反复追问"做完了吗？"，Agent 自己判断。

##### 步骤 8：判定最终状态 + stop_reason 归因

```python
# 引用位置：backend/packages/harness/deerflow/runtime/runs/worker.py:552-594
        # 8. Final status
        if record.abort_event.is_set():
            await run_manager.set_finalizing(run_id, True)
            action = record.abort_action
            if action == "rollback":
                await run_manager.set_status(run_id, RunStatus.error, error="Rolled back by user")
                try:
                    await _rollback_to_pre_run_checkpoint(...)  # 回滚到步骤3的快照
                except Exception:
                    logger.warning(...)
            else:
                await run_manager.set_status(run_id, RunStatus.interrupted)
        elif llm_error_fallback_message or (journal is not None and journal.had_llm_error_fallback):
            error_msg = llm_error_fallback_message or journal.llm_error_fallback_message or "LLM provider failed after retries"
            await run_manager.set_status(run_id, RunStatus.error, error=error_msg)
        else:
            runtime_context = runtime.context if isinstance(runtime.context, dict) else None
            # Guard middlewares stamp stop_reason into runtime.context:
            #   loop_detection      -> "loop_capped"
            #   token_budget        -> "token_capped"
            #   safety_finish_reason -> "safety_capped"
            #   subagent_limit       -> "subagent_limit_capped"
            stop_reason = runtime_context.get("stop_reason") if runtime_context is not None else None
            await run_manager.set_status(run_id, RunStatus.success, stop_reason=stop_reason)
```

##### 补：两条 except 分支（文档常漏掉，行 596-628）

`try` 块还有两个 `except`，处理 `run_agent` 被异常打断的情况：

```python
# 引用位置：backend/packages/harness/deerflow/runtime/runs/worker.py:596-628
    except asyncio.CancelledError:
        # 后台 task 被取消（比如 worker 关停）→ 走和 abort 一样的 finalizing/rollback/interrupt 路径
        await run_manager.set_finalizing(run_id, True)
        ...
        raise   # 必须重新抛出 CancelledError，否则会破坏 asyncio 取消协议

    except Exception as exc:
        # 未预期的异常 → 标 error，发 "error" SSE 事件给前端
        error_msg = f"{type(exc).__name__}: {exc}"
        await run_manager.set_status(run_id, RunStatus.error, error=error_msg)
        await bridge.publish(run_id, "error", {"message": error_msg, "name": type(exc).__name__})
```

**► 注解**：
- **`except asyncio.CancelledError`**：后台 task 被 cancel 时进入。逻辑和 abort 分支一样（rollback 或 interrupt），但**必须 `raise`** 重新抛出——否则会吞掉取消信号，破坏 asyncio 取消协议。
- **`except Exception`**：兜底所有未预期异常（如 graph 内部 bug）。标记 `error` 并通过 bridge 发 `"error"` SSE 事件，前端会显示错误提示。
- 这两条 + `finally` 共同保证：**无论 run 怎么结束（正常/取消/异常），`finally` 块的记账和清理都一定会跑**。

**► 逐行注解**：
- **三种最终状态**：
  1. **`abort`（用户取消）**：如果策略是 `rollback`，回滚到步骤 3 保存的 checkpoint 快照；否则标记 `interrupted`（保留进度）。
  2. **`LLM 错误`**：标记 `error`，带上错误消息。
  3. **`成功`**：标记 `success`，带 `stop_reason`。

- **`stop_reason` 归因**（第 571-586 行，注释很关键）：安全中间件（LoopDetection、TokenBudget 等）强制终止 run 时，会把原因写到 `runtime.context["stop_reason"]`。worker 读取它，记录到 run 记录。这让前端能显示"因为循环检测被终止"而非笼统的"已结束"。

- **注释里的前瞻设计**（第 580-584 行）："如果更多 guard 需要 stop_reason，考虑改成 publish/collect 模式"——目前每个 guard 直接写同一个 key（可能覆盖），未来可能改成各写各的 channel 再聚合。

##### 步骤 9：finally 收尾（大幅扩展，记账密集）

`finally` 块（**行 630-719**）现在包含一系列记账操作：

```python
# 引用位置：backend/packages/harness/deerflow/runtime/runs/worker.py:630-719 (关键部分)
    finally:
        # 1. 刷出缓冲的子 Agent 事件（行 633-634）
        if subagent_events is not None:
            await subagent_events.flush()

        # 2. 对比运行前后工作区快照，持久化文件变更（行 636-646）
        if event_store is not None and pre_run_workspace_snapshot is not None:
            await record_workspace_changes(event_store, thread_id, run_id, pre_run_workspace_snapshot, ...)

        # 3. 刷出 RunJournal（token统计），更新 run 完成数据（行 649-660）
        if journal is not None:
            await journal.flush()
            completion = journal.get_completion_data()
            await run_manager.update_run_completion(run_id, status=record.status.value, **completion)

        # 4. interrupted 状态也要生成标题（行 662-668）—— 防止后续 run 已启动覆盖 checkpoint
        if checkpointer is not None and record.status == RunStatus.interrupted:
            await run_manager.wait_for_prior_finalizing(...)
            if not await run_manager.has_later_started_run(...):
                await _ensure_interrupted_title(checkpointer=..., thread_id=..., app_config=..., graph_input=...)

        # 5. 同步 title 到 thread_meta（行 671-681）
        if checkpointer is not None and thread_store is not None:
            # 重读 checkpoint 取 title 字段
            await thread_store.update_display_name(thread_id, title)

        # 6. 把 turn 时长写进 checkpoint metadata（行 685-700，#4118）—— 仅 success
        if checkpointer is not None and record.status == RunStatus.success:
            await _persist_run_duration(checkpointer=..., thread_id=..., run_id=..., duration_seconds=duration)

        # 7. 更新线程状态（行 703-708）—— success→idle，其他→对应 status
        if thread_store is not None:
            final_status = "idle" if record.status == RunStatus.success else record.status.value
            await thread_store.update_status(thread_id, final_status)

        # 8. on_run_completed 回调（行 710-714）
        if ctx.on_run_completed is not None:
            await ctx.on_run_completed(record)

        # 9. 标记 finalizing 结束（行 715-716）
        if record.finalizing:
            await run_manager.set_finalizing(run_id, False)

        # 10. 发布 end 事件 + 清理 bridge（行 718-719）
        await bridge.publish_end(run_id)
        asyncio.create_task(bridge.cleanup(run_id, delay=60))  # 60秒后清理，支持重连
```

**► 注解**：收尾从旧版的"简单清理"变成了**多步记账**，且每步都有条件守卫（`if checkpointer is not None` / `if journal is not None` / `if thread_store is not None`），允许某些基础设施缺失时降级运行：
1. **子 Agent 事件刷盘**（行 633-634）
2. **workspace changes diff**（行 636-646）
3. **RunJournal token 统计 + completion 数据**（行 649-660）
4. **interrupted run 的标题兜底**（行 662-668）—— 只有在没有更晚的 run 启动时才写，避免覆盖
5. **title 同步到 thread_meta**（行 671-681）
6. **run duration 持久化**（行 685-700，仅 success）
7. **线程状态更新**（行 703-708）
8. **on_run_completed 回调**（行 710-714）
9. **finalizing 标志复位**（行 715-716）
10. **bridge 清理**（行 718-719，**延迟 60 秒**且用 `asyncio.create_task` 异步执行，支持断线重连的客户端补拉事件）

---

## 1.5 核心数据结构：ThreadState（12 个通道，极致详解）

`ThreadState` 定义了"在一次对话线程里，Agent 需要记住哪些状态"。现在有 **12 个自有字段**（加上继承自 `AgentState` 的 `messages`，状态通道共 13 个）。

### 1.5.1 完整类定义

```python
# 引用位置：backend/packages/harness/deerflow/agents/thread_state.py:239-251
class ThreadState(AgentState):
    sandbox: SandboxStateField                                                       # 行 240
    thread_data: NotRequired[ThreadDataState | None]                                 # 行 241
    title: NotRequired[str | None]                                                   # 行 242
    artifacts: Annotated[list[str], merge_artifacts]                                 # 行 243
    todos: Annotated[list | None, merge_todos]                                       # 行 244
    goal: Annotated[GoalState | None, merge_goal]                                    # 行 245 ★ 新增
    uploaded_files: NotRequired[list[dict] | None]                                   # 行 246
    viewed_images: Annotated[dict[str, ViewedImageData], merge_viewed_images]        # 行 247
    promoted: Annotated[PromotedTools | None, merge_promoted]                        # 行 248
    delegations: Annotated[list[DelegationEntry], merge_delegations]                 # 行 249 ★ 新增
    skill_context: Annotated[list[SkillEntry], merge_skill_context]                  # 行 250 ★ 新增
    summary_text: NotRequired[str | None]                                            # 行 251 ★ 新增
```

**► 字段总览**：
- **继承自 `AgentState`**：后者定义了 `messages: list[BaseMessage]`（对话历史）——这是 Agent 的核心，**不在上面的 12 个里**。把 `messages` 算进去总共 13 个状态通道。
- **4 个本次新增字段**（标 ★）：`goal`、`delegations`、`skill_context`、`summary_text`。

### 1.5.2 Annotated + Reducer 机制（首次讲解，不跳过）

你会注意到有些字段用了 `Annotated[类型, reducer函数]`。这是 LangGraph 的 **reducer（归约器）** 机制，极其重要，首次接触必须讲透。

**问题背景**：LangGraph 是状态图，多个节点可能**并发**向同一个 state 字段写入。比如两个并行工具执行时，都想更新 `sandbox`。默认行为是"后者覆盖前者"，但这往往不对。`Annotated[类型, reducer]` 让你自定义"多次写入时怎么合并"。

**具体例子**：

```python
# 没有 reducer（默认行为是覆盖）
some_field: str
# 两个节点同时写 "A" 和 "B" → 结果是 "B"（最后写的赢）

# 有 reducer
some_field: Annotated[list[str], merge_artifacts]
# 两个节点同时写 ["A"] 和 ["B"] → merge_artifacts 合并成 ["A", "B"]
```

### 1.5.3 reducer 逐一详解（完整代码 + 数据样例）

#### merge_sandbox：幂等写入，冲突报错（fail-closed）

```python
# 引用位置：backend/packages/harness/deerflow/agents/thread_state.py:34-52
def merge_sandbox(existing: SandboxState | None, new: SandboxState | None) -> SandboxState | None:
    """Reducer for sandbox state - accepts idempotent writes only.

    Multiple sandbox tools can initialize lazily in the same graph step and
    emit the same sandbox_id via Command(update=...). LangGraph needs an
    explicit reducer for that shared state key. Different sandbox ids in the
    same thread indicate a lifecycle/isolation bug, so fail closed instead of
    choosing one silently.
    """
    if new is None:
        return existing
    if existing is None:
        return new

    existing_id = existing.get("sandbox_id")
    new_id = new.get("sandbox_id")
    if existing_id == new_id:
        return existing
    raise ValueError(f"Conflicting sandbox state updates: {existing_id!r} != {new_id!r}")
```

**► 逐行注解**：
- **第 43-44 行**：`new is None` → 保留 existing（这次没更新）。
- **第 45-46 行**：`existing is None` → 用 new（第一次设置）。
- **第 50-51 行**：两个 sandbox_id **相同** → 幂等返回 existing（重复设置同一个，正常）。
- **第 52 行**：两个 sandbox_id **不同** → **直接抛异常！**

**数据流样例**：
```python
# 正常情况：两个工具并发懒初始化同一个沙箱
existing = {"sandbox_id": "local:thread-abc"}
new = {"sandbox_id": "local:thread-abc"}
merge_sandbox(existing, new)  # → {"sandbox_id": "local:thread-abc"} (幂等)

# 异常情况：一个线程出现了两个不同沙箱（bug！）
existing = {"sandbox_id": "local:thread-abc"}
new = {"sandbox_id": "docker:xyz789"}
merge_sandbox(existing, new)  # → raise ValueError! (fail-closed)
```

**设计动机深挖**：为什么 fail-closed 而不是"选一个"？
- 沙箱是**安全隔离边界**。一个线程出现两个不同沙箱，说明出了严重 bug（比如隔离被打破）。
- 如果"选一个"，可能选到错误的沙箱——用户 A 的数据写到了用户 B 的沙箱，或者代码在错误的容器里执行。
- **fail-closed**（直接崩溃）暴露问题，比静默用错数据安全得多。
- **对比**：LangGraph 默认 reducer 是"后者覆盖"——DeerFlow 在安全相关字段刻意覆盖这个默认值。

#### merge_viewed_images：空字典是"清空信号"

```python
# 引用位置：backend/packages/harness/deerflow/agents/thread_state.py:68-82
def merge_viewed_images(existing, new):
    """Reducer for viewed_images dict - merges image dictionaries.

    Special case: If new is an empty dict {}, it clears the existing images.
    This allows middlewares to clear the viewed_images state after processing.
    """
    if existing is None:
        return new or {}
    if new is None:
        return existing
    if len(new) == 0:      # 空字典 = 清空信号
        return {}
    return {**existing, **new}  # 否则合并
```

**► 注解**：
- 默认行为是合并字典：`{**existing, **new}`（new 覆盖 existing 的同 key）。
- **特殊约定**：`new = {}`（空字典）是"清空"信号。

**为什么需要这个约定？** `ViewImageMiddleware` 在注入图片后需要清空 `viewed_images`，防止下一轮重复注入。但 reducer 默认"new 是 None 才保留 existing"——传空字典会被当"有值"覆盖。作者**利用**这个特性，显式约定空字典=清空。

**数据流样例**：
```python
# 模型调用了 view_image("chart.png")
existing = {}
new = {"chart.png": {"mime_type": "image/png", "size": 45678, "actual_path": "..."}}
merge_viewed_images(existing, new)
# → {"chart.png": {"mime_type": "image/png", "size": 45678, "actual_path": "..."}}

# ViewImageMiddleware 注入图片到消息后，清空
existing = {"chart.png": {...}}
new = {}  # 清空信号
merge_viewed_images(existing, new)
# → {} (清空了)
```

**注意**：`ViewedImageData` 现在只存元数据（mime_type/size/actual_path），**不存 base64**！issue #4138 的性能优化——旧版每个 checkpoint 都带 base64，极其浪费空间。

#### merge_delegations：委派账本（新增，终态粘性）

```python
# 引用位置：backend/packages/harness/deerflow/agents/thread_state.py:151-179
def merge_delegations(existing: list[DelegationEntry] | None, new: list[DelegationEntry] | None) -> list[DelegationEntry]:
    """Reducer for the delegation ledger.

    - new None/empty -> preserve existing.
    - append entries, replacing same id with the latest version while preserving
      first-seen order.
    - terminal status is never overwritten by a non-terminal status.
    """
    if not new:
        return existing or []

    by_id: dict[str, DelegationEntry] = {}
    order: list[str] = []
    for entry in [*(existing or []), *new]:
        entry_id = entry["id"]
        previous = by_id.get(entry_id)
        if previous is not None and previous["status"] in TERMINAL_STATUSES and entry["status"] not in TERMINAL_STATUSES:
            continue  # 终态粘性：不覆盖
        if entry_id not in by_id:
            order.append(entry_id)
        elif previous.get("created_at"):
            entry = {**entry, "created_at": previous["created_at"]}
            if previous.get("run_id") and not entry.get("run_id"):
                entry["run_id"] = previous["run_id"]
        by_id[entry_id] = entry
    merged = [by_id[entry_id] for entry_id in order]
    if len(merged) > _DELEGATION_LEDGER_MAX_ENTRIES:
        merged = merged[-_DELEGATION_LEDGER_MAX_ENTRIES:]
    return merged
```

**► 逐行注解**：
- **第 167-168 行"终态粘性"**：如果一个委派已经是终态（completed/failed/cancelled），后来的非终态更新**不会覆盖**它。防止"子 Agent 完成了，但一个迟到的'running'消息把它覆盖回 running"的竞态。
- **第 171-174 行"保留首次元数据"**：更新时保留首次创建的 `created_at` 和 `run_id`——这些是"这个委派什么时候发起的"的历史事实，不应被后续更新改写。
- **第 177-178 行"50 条上限"**：超过 `_DELEGATION_LEDGER_MAX_ENTRIES=50` 则保留最新的 50 条。

**数据流样例**：
```python
# 主 Agent 委派子 Agent 做研究
existing = []
new = [{"id": "task-1", "description": "研究X", "subagent_type": "general-purpose", "status": "running", "created_at": "10:00"}]
merge_delegations(existing, new)
# → [{"id": "task-1", ..., "status": "running", "created_at": "10:00"}]

# 子 Agent 完成
existing = [{"id": "task-1", "status": "running", ...}]
new = [{"id": "task-1", "status": "completed", "result_brief": "X是..."}]
merge_delegations(existing, new)
# → [{"id": "task-1", "status": "completed", "result_brief": "X是...", "created_at": "10:00"}]
#    注意 created_at 保留为首次的 10:00

# 迟到的 running 消息（竞态）→ 被终态粘性阻止
existing = [{"id": "task-1", "status": "completed", ...}]
new = [{"id": "task-1", "status": "running"}]  # 迟到了
merge_delegations(existing, new)
# → [{"id": "task-1", "status": "completed", ...}]  ← 终态不被覆盖!
```

#### merge_skill_context：持久技能引用（新增）

```python
# 引用位置：backend/packages/harness/deerflow/agents/thread_state.py:205-236
def merge_skill_context(existing: list[SkillEntry] | None, new: list[SkillEntry] | None) -> list[SkillEntry]:
    """Reducer for the skill-context channel.

    - new None/empty -> preserve existing.
    - legacy entries are normalized to references; verbatim body keys are dropped.
    - dedup by ``path``; later reads refresh recency and replace the reference.
    - cap by keeping the most recently read entries.
    """
    normalized_existing = [_normalize_skill_entry(entry) for entry in existing or []]
    if not new:
        return normalized_existing

    by_path: dict[str, SkillEntry] = {}
    order: list[str] = []
    for entry in normalized_existing:
        path = entry["path"]
        if path not in by_path:
            order.append(path)
        by_path[path] = entry

    for entry in (_normalize_skill_entry(entry) for entry in new):
        path = entry["path"]
        if path in by_path:
            order.remove(path)  # 移到末尾（最近使用）
        order.append(path)
        by_path[path] = entry

    merged = [by_path[path] for path in order]
    if len(merged) > _SKILL_CONTEXT_MAX_ENTRIES:  # 8条上限
        merged = merged[-_SKILL_CONTEXT_MAX_ENTRIES:]
    return merged
```

**► 逐行注解**：
- **按 path 去重**：同一个技能被多次读取，只保留一条引用，更新 `loaded_at`。
- **LRU 语义**：重新读取的技能移到 `order` 列表末尾（最近使用）。超过 8 条时淘汰最前面的（最久未用）。
- **`_normalize_skill_entry`**：把 legacy 的完整技能内容（body keys）归一化成轻量引用（只留 name/path/description），丢弃全文。

**设计动机**：旧版每次轮次都可能重新加载技能全文，浪费 token。新版 `skill_context` 只存引用，配合 `DurableContextMiddleware`（第 3 章）跨轮持久化。

#### merge_goal：last-write-wins，None 不动（新增）

```python
# 引用位置：backend/packages/harness/deerflow/agents/thread_state.py:98-102
def merge_goal(existing: GoalState | None, new: GoalState | None) -> GoalState | None:
    """Reducer for goal state - preserves existing when a node does not touch it."""
    if new is None:
        return existing
    return new
```

**► 注解**：极简——**只有当节点显式传 `None` 才保留旧值**；传新值就覆盖。这保证普通状态更新（不碰 goal）不会误清空它；而 `set_goal`/`clear_goal` 想改时直接覆盖即可。

#### merge_promoted：catalog_hash 防漂移（新增）

```python
# 引用位置：backend/packages/harness/deerflow/agents/thread_state.py:110-128
def merge_promoted(existing: PromotedTools | None, new: PromotedTools | None) -> PromotedTools | None:
    """Reducer for deferred-tool promotions, scoped by catalog hash.

    - new None/empty -> preserve existing (node didn't touch promotions).
    - catalog_hash changed -> replace wholesale, dropping stale names (prevents a
      persisted bare name from exposing a different tool after catalog drift).
    - same catalog_hash -> union names, dedupe, preserve order.
    """
    if not new:
        return existing
    if existing is None or existing.get("catalog_hash") != new["catalog_hash"]:
        return {"catalog_hash": new["catalog_hash"], "names": list(dict.fromkeys(new["names"]))}
    return {"catalog_hash": existing["catalog_hash"], "names": list(dict.fromkeys(existing["names"] + new["names"]))}
```

**► 注解**：
- `PromotedTools` 只有 `catalog_hash` 和 `names: list[str]`（行 105-107）。
- **catalog_hash 变了 → 整个替换**：工具目录（MCP 注册的工具集）变化后，旧 hash 下持久化的工具名可能指向**完全不同的工具**。直接清掉，防止"名字还在但指向变了"的安全风险。
- **catalog_hash 不变 → 取并集**：用 `dict.fromkeys` 做保序去重，多次 promote 同一工具不会重复。
- 配合 `McpRoutingMiddleware`（自动 promote）和 `DeferredToolFilterMiddleware`（隐藏未 promote 的）。

---

## 1.6 运行时配置：config.configurable + config.context

LangGraph 的运行时配置有两个字典：`config["configurable"]` 和 `config["context"]`。前者是 LangGraph 一直就有的（checkpoint 定位、节点参数）；后者是 LangGraph ≥0.6 引入的"运行时上下文"，**不进 checkpoint、每次请求重算**。DeerFlow 的工厂用一个 merge 把两者打通：

```python
# 引用位置：backend/packages/harness/deerflow/agents/lead_agent/agent.py:84-90
def _get_runtime_config(config: RunnableConfig) -> dict:
    cfg = dict(config.get("configurable", {}) or {})
    context = config.get("context", {}) or {}
    if isinstance(context, dict):
        cfg.update(context)   # context 覆盖 configurable
    return cfg
```

所以下面"用户可控"的 key，无论存在 `configurable` 还是 `context`，agent 代码都能用同一个 `cfg.get(...)` 读到。但**写入位置是有讲究的**——下面标注了每个 key 实际写在哪。

### 1.6.1 用户/客户端可控的开关（11 个）

| 配置项 | 默认值 | 写入位置 | 作用 | 新增? |
|--------|--------|--------|------|------|
| `thinking_enabled` | `True` | `context` | 开启思维链（模型不支持时会被强制改 False） | |
| `reasoning_effort` | `None` | `context` | 推理强度 | |
| `model_name` / `model` | 三级解析 | `context` | 指定模型（`agent.py:469`，两个 key 都接受） | |
| `is_plan_mode` | `False` | `context` | 启用计划模式（TodoList middleware） | |
| `subagent_enabled` | `False` | `context` | 启用子 Agent（task 工具） | |
| `max_concurrent_subagents` | `3`（夹紧到 [2,4]） | `context` | 单次响应最大并发子 Agent | |
| `max_total_subagents` | 默认 6（来自 `subagents.max_total_per_run`，夹紧到 [1,50]） | `context` | **单次 run 最大总子 Agent 数** | ★ |
| `is_bootstrap` | `False` | `context` | 创建自定义 Agent 引导 | |
| `non_interactive` | `False` | `context` | **非交互模式（lead-agent toolset 排除 `ask_clarification`）** | ★ |
| `agent_name` | `None` | `configurable` + `context` 都写 | 自定义 Agent 名 | |
| `user_id` | 回退 `get_effective_user_id()` | `context` | **显式用户 ID（覆盖 ContextVar）** | ★ |

**► 三级模型解析**（`agent.py:469-484`）：
1. 请求级 `requested_model_name`（`cfg["model_name"]` 或 `cfg["model"]`）
2. agent 级 `agent_model_name`（自定义 agent 配置 `agents/<name>/.config` 里的 `model`）
3. 全局默认（`app_config.models[0].name`）

**► 两个夹紧区间**（`subagents_config.py:11-15`）：
- `max_concurrent_subagents` ∈ **[2, 4]**（`MIN_CONCURRENT_SUBAGENT_CALLS=2`、`MAX_CONCURRENT_SUBAGENT_CALLS=4`）
- `max_total_subagents` ∈ **[1, 50]**（`MIN_TOTAL_SUBAGENTS_PER_RUN=1`、`MAX_TOTAL_SUBAGENTS_PER_RUN=50`），默认 6

**新增项的设计动机**：
- **`max_total_subagents`**：目标延续循环的安全阀——防止 Agent 在续轮里无限委派。
- **`non_interactive`**：IM/Webhook 场景无法提问，过滤 `ask_clarification`。Gateway 仅对**内部认证**的调用方（scheduler 启动路径）保留此 key，客户端传的会被丢弃。
- **`user_id`**：Gateway 注入认证用户，支持 per-user 技能白名单、文件路径隔离。

### 1.6.2 框架注入的 key（用户传了也会被覆盖/清除）

这些 key 不是给用户设的，由 Gateway 或 worker 在 run 生命周期里注入。**客户端传的同名 key 会被 strip 或覆盖**（`build_run_config` 行 483 清掉所有 `__`-前缀的客户端 key）：

| key | 写入位置 | 注入方 / 行号 | 作用 |
|-----|--------|--------------|------|
| `thread_id` | `configurable` | `build_run_config` `services.py:596` | 线程 ID（checkpoint 定位） |
| `checkpoint_ns` | `configurable` | `services.py:597` | checkpoint 命名空间（默认 `""`） |
| `checkpoint_id` | `configurable` | `services.py:598` | 分支/历史 checkpoint 定位 |
| `checkpoint_map` | `configurable` | `services.py:600`（可选） | checkpoint 父子映射 |
| `__pregel_runtime` | `configurable` | `worker.py:385` | LangGraph Runtime 对象（手动注入，因为不走 langgraph-cli 自动注入） |
| `__run_journal` | `context` | `worker.py:382` | RunJournal 引用，给中间件写审计事件 |
| `is_internal` / `user_role` / `oauth_provider` / `oauth_id` / `channel_user_id` | `context` | `inject_authenticated_user_context` `services.py:337-377` | 认证上下文（`is_internal` 严格由 `request.state.auth_source` 派生，客户端传的不算） |

**► 安全提示**：`__`-前缀的 key（`__pregel_runtime`、`__run_journal`）是运行时内部 channel，**用户代码不应依赖其名字**（worker 注释明说 `Double-underscore prefix marks it as a runtime-internal channel; user code must not depend on the key name`）。认证上下文 key 由 `_SERVER_OWNED_AUTHZ_CONTEXT_KEYS` 守护，Gateway 会移除客户端提供的同名 key（`services.py:337`）。

---

## 1.7 本章小结

读完本章，你应该建立了这些**完整、无遗漏**的认知：

1. **架构**：Harness（核心）+ App（HTTP 壳）两层分层，单向依赖，由 `tests/test_harness_boundary.py` 强制。
2. **拓扑**：4 端口（Nginx:2026 / Gateway:8001 / Frontend:3000 / Provisioner:8002）；多 worker 通过租约/心跳协调。
3. **请求生命周期**：认证（fail-closed，三阶段 + `X-DeerFlow-Internal-Token`/`access_token`/auth_disabled）→ 创建 Run（模型白名单 / 所有权双轨检查 / `create_or_reject` / 线程元数据 upsert）→ 立即返回 SSE（`Content-Location` 给 SDK 提取 `run_id`）→ 后台 worker（等待前序 / RunJournal / 工作区快照 / checkpoint 快照 / 组装 Agent / 挂 checkpointer / 设 interrupt / 构造 stream_mode / 用户 turn / **目标延续循环** / 终态判定 / 收尾记账）。
4. **目标自动延续**：两阶段执行——用户可见 turn + 隐藏续轮循环（`max_continuations≤8`、无进展 2 次熔断），让 Agent 自主判断目标达成。
5. **ThreadState 12 自有字段**（+ 继承的 `messages` 共 13 通道）：4 个新增（goal/delegations/skill_context/summary_text）；7 个 reducer 详解——merge_sandbox（fail-closed）、merge_viewed_images（空字典清空）、merge_delegations（终态粘性）、merge_skill_context（LRU）、merge_goal（last-write-wins）、merge_promoted（catalog_hash 防漂移）、merge_artifacts/merge_todos（去重合并）。
6. **配置体系**：用户可控 11 个开关 + 框架注入（thread_id/checkpoint_ns/checkpoint_id/__pregel_runtime 等）；通过 `_get_runtime_config` 把 `configurable` 和 `context` merge 起来统一读。
7. **可观测性**：RunJournal token 记账、workspace changes diff、stop_reason 归因（loop_capped/token_capped/safety_capped/subagent_limit_capped）、run duration 持久化。
8. **异常路径**：`except asyncio.CancelledError`（重新 raise）和 `except Exception`（标 error + 发 SSE 错误事件）——无论 run 怎么结束，`finally` 的 10 步记账都一定跑。

**下一章**：放大"步骤 5 构建 Agent"——`make_lead_agent` 怎么把模型/工具/提示词/**配置驱动的中间件链**拼成可执行 Agent。

> **关于中间件数量**：lead-agent 的中间件链长度是**配置驱动**的，不是固定数字。常驻中间件约 22 个（`build_lead_runtime_middlewares` 提供的 10 个 runtime 基座 + lead-only 的 ~12 个），加上按条件追加的（plan_mode/subagent_enabled/vision/summarization/loop_detection/token_budget/safety_finish_reason/guardrails/read_before_write/tool_progress/MCP routing/memory/...）。一个全功能 run 通常在 28-32 之间浮动。具体的清单和顺序见第 3 章。
