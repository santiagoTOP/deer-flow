# DeerFlow：从前端输入框到 `make_lead_agent` 的完整调用链路

> 本文档梳理用户在前端聊天框发送第一条消息后，系统内部从前端到后端 LangGraph 图工厂函数 `make_lead_agent` 的完整流程。

---

## 目录

1. [第一层：前端输入触发](#第一层前端输入触发)
2. [第二层：useThreadStream Hook](#第二层usethreadstream-hook)
3. [第三层：LangGraph SDK → HTTP 请求](#第三层langgraph-sdk--http-请求)
4. [第四层：后端 Gateway 接收请求](#第四层后端-gateway-接收请求)
5. [第五层：resolve_agent_factory 直接调度](#第五层resolve_agent_factory-直接调度)
6. [第六层：make_lead_agent 内部构建图](#第六层make_lead_agent-内部构建图)
7. [完整调用链总览](#完整调用链总览)
8. [关键文件速查表](#关键文件速查表)

---

## 第一层：前端输入触发

**关键文件：**
- `frontend/src/components/workspace/input-box.tsx`（第 255–297 行）
- `frontend/src/app/workspace/chats/[thread_id]/page.tsx`（第 110–119 行）

用户在 `InputBox` 组件中按下发送按钮，触发 `handleSubmit`，校验后调用父组件传入的 `onSubmit` prop。

聊天页面（`[thread_id]/page.tsx`）的 `handleSubmit` 直接调用来自 `useThreadStream` hook 的 `sendMessage`：

```ts
// [thread_id]/page.tsx
const { sendMessage } = useThreadStream({ threadId, ... });

function handleSubmit(message) {
  sendMessage(threadId, message);
}
```

---

## 第二层：useThreadStream Hook

**关键文件：** `frontend/src/core/threads/hooks.ts`（第 222–622 行）

这是前端流程的核心 Hook，内部使用 `@langchain/langgraph-sdk/react` 的 `useStream`。

### useStream 初始化（第 222–360 行）

```ts
useStream({
  client: getAPIClient(isMock),   // LangGraph SDK 客户端
  assistantId: "lead_agent",      // 对应后端 agent factory 名称
  threadId: onStreamThreadId,
  reconnectOnMount: true,
  // 事件回调
  onCreated: ...,
  onLangChainEvent: ...,
  onUpdateEvent: ...,
})
```

### sendMessage 函数（第 428–622 行）

执行顺序：

1. **上传附件**（如有）：先通过 Upload API 上传文件，获取文件引用
2. **提交消息**：调用 `thread.submit()`，携带以下数据：

```ts
thread.submit({
  messages: [{
    type: "human",
    content: [
      { type: "text", text: userInput },
      // 附件（图片/文件）...
    ],
    additional_kwargs: { ... },
  }],
  threadId: threadId,
  streamSubgraphs: true,
  config: {
    recursion_limit: 1000,
  },
  context: {
    model_name,          // 用户选择的模型
    mode,                // agent 模式
    thinking_enabled,    // 是否启用思考链
    is_plan_mode,        // 是否计划模式
    subagent_enabled,    // 是否启用子 agent
    reasoning_effort,    // 推理深度
    thread_id,           // 当前线程 ID
    // ...其他上下文
  },
})
```

`context` 字段会被透传到后端 `RunnableConfig`，最终注入 `make_lead_agent` 的 `config` 参数。

---

## 第三层：LangGraph SDK → HTTP 请求

**关键文件：** `frontend/src/core/api/api-client.ts`（第 35–73 行）

`LangGraphClient` 的创建：

```ts
new LangGraphClient({
  apiUrl: getLangGraphBaseURL(isMock),
  onRequest: (req) => {
    // 自动从 cookie 读取 CSRF token 并注入请求头
    req.headers["X-CSRFToken"] = getCsrfToken();
    return req;
  },
})
```

`thread.submit()` 内部发出的 HTTP 请求：

```
POST /threads/{thread_id}/runs/stream
Content-Type: application/json
X-CSRFToken: <from cookie>

{
  "assistant_id": "lead_agent",
  "input": { "messages": [...] },
  "config": { "recursion_limit": 1000 },
  "context": { "model_name": "...", "thinking_enabled": true, ... },
  "stream_mode": ["events", "updates", ...],
  "stream_subgraphs": true
}
```

响应为 SSE（Server-Sent Events）流，前端 Hook 持续消费事件并更新 UI。

---

## 第四层：后端 Gateway 接收请求

### 4.1 路由层

**关键文件：** `backend/app/gateway/routers/thread_runs.py`（第 116–149 行）

```python
@router.post("/threads/{thread_id}/runs/stream")
async def stream_run(thread_id: str, body: RunCreateRequest, request: Request):
    return await start_run(body, thread_id, request)
    # 响应头包含 Content-Location: /api/threads/{thread_id}/runs/{run_id}
```

### 4.2 服务层：start_run

**关键文件：** `backend/app/gateway/services.py`（第 248–353 行）

`start_run()` 是核心调度函数，按顺序执行：

| 步骤 | 代码行 | 说明 |
|------|--------|------|
| 获取基础依赖 | 265 | 取 bridge、run manager、run context |
| 校验 model_name | 271–286 | 确认请求的模型合法 |
| 创建 run 记录 | 288–301 | `run_mgr.create_or_reject()` 写入持久化 |
| Upsert thread 元数据 | 306–317 | 确保线程出现在搜索列表中 |
| **解析 agent factory** | **319** | `resolve_agent_factory(body.assistant_id)` |
| 构建 run config | 320 | `build_run_config(thread_id, body.config, ...)` |
| 合并 context overrides | 323–327 | 将 model_name、thinking_enabled 等注入 config |
| 注入认证用户上下文 | 328–329 | 为后台异步任务注入当前用户信息 |
| 标准化 stream modes | 330 | 处理 SSE 事件类型 |
| **异步启动 run** | **332–346** | `asyncio.create_task(run_agent(...))` |

---

## 第五层：resolve_agent_factory 直接调度

**关键文件：** `backend/app/gateway/services.py`（第 158–170 行）

```python
def resolve_agent_factory(assistant_id: str | None):
    """Resolve the agent factory callable from config.

    Custom agents are implemented as ``lead_agent`` + an ``agent_name``
    injected into ``configurable`` or ``context`` — see
    :func:`build_run_config`.  All ``assistant_id`` values therefore map to the
    same factory; the routing happens inside ``make_lead_agent`` when it reads
    ``cfg["agent_name"]``.
    """
    from deerflow.agents.lead_agent.agent import make_lead_agent

    return make_lead_agent
```

**关键点：**

- `resolve_agent_factory` **直接 import 并返回** `make_lead_agent` 函数引用，不经过 LangGraph Server 的图注册机制
- 所有 `assistant_id` 值（包括自定义 agent）都映射到同一个 factory
- 自定义 agent 的路由通过 `cfg["agent_name"]` 在 `make_lead_agent` 内部完成

> **注意：** 项目根目录有 `langgraph.json`，其中注册了 `"lead_agent": "deerflow.agents:make_lead_agent"`，这是给**直接使用 LangGraph Server 托管**的部署模式用的，在当前 Gateway 驱动的链路中不参与调度。

拿到 factory 函数后，`run_agent()` 直接调用：

```python
graph = agent_factory(config)   # 即 make_lead_agent(config)
graph.stream(graph_input, config)
```

---

## 第六层：make_lead_agent 内部构建图

**关键文件：** `backend/packages/harness/deerflow/agents/lead_agent/agent.py`（第 343–446 行）

### 入口函数（第 343–348 行）

```python
def make_lead_agent(config: RunnableConfig):
    """LangGraph graph factory; keep the signature compatible with LangGraph Server."""
    runtime_config = _get_runtime_config(config)
    runtime_app_config = runtime_config.get("app_config")
    return _make_lead_agent(config, app_config=runtime_app_config or get_app_config())
```

`_get_runtime_config(config)`（第 29–35 行）将 `config["configurable"]` 与 `config["context"]` 合并，返回所有运行时选项（model_name、thinking_enabled 等）。

### 实现函数 `_make_lead_agent`（第 350–446 行）

**步骤 1：提取运行时参数（第 355–365 行）**

```python
thinking_enabled   = runtime_config.get("thinking_enabled", True)
reasoning_effort   = runtime_config.get("reasoning_effort")
requested_model    = runtime_config.get("model_name")
is_plan_mode       = runtime_config.get("is_plan_mode", False)
subagent_enabled   = runtime_config.get("subagent_enabled", True)
max_concurrent_subagents = runtime_config.get("max_concurrent_subagents")
is_bootstrap       = runtime_config.get("is_bootstrap", False)
agent_name         = runtime_config.get("agent_name")  # 自定义 agent 路由
```

**步骤 2：解析模型（第 367–382 行）**

三级 fallback 链：

```
请求中的 model_name
    → agent 配置中的默认模型
        → 全局默认模型
```

**步骤 3：Bootstrap 模式（第 411–427 行）**

若 `is_bootstrap=True`，返回精简版 agent（仅含 `setup_agent` tool），用于初始化流程。

**步骤 4：标准 lead agent 构建（第 432–446 行）**

```python
tools = get_available_tools()                          # 加载所有可用工具
tools = filter_tools_by_skill_allowed_tools(tools)     # 按 agent 配置过滤

graph = create_agent(
    model=create_chat_model(
        name=model_name,
        thinking_enabled=thinking_enabled,
        reasoning_effort=reasoning_effort,
    ),
    tools=tools,
    middleware=_build_middlewares(...),    # 13 层 middleware 链
    system_prompt=apply_prompt_template(...),
    state_schema=ThreadState,
)

return graph   # CompiledGraph，可直接调用 .stream()
```

### Middleware 链（第 240–318 行）

按顺序应用，共 13 层：

| 顺序 | Middleware | 说明 |
|------|-----------|------|
| 1 | Base runtime middlewares | 错误处理、沙箱审计 |
| 2 | DynamicContextMiddleware | 动态上下文注入 |
| 3 | SummarizationMiddleware | 长对话摘要（可选） |
| 4 | TodoMiddleware | 计划模式 todo 管理 |
| 5 | TokenUsageMiddleware | Token 用量统计（可选） |
| 6 | TitleMiddleware | 自动生成对话标题 |
| 7 | MemoryMiddleware | 持久记忆管理 |
| 8 | ViewImageMiddleware | 图片视觉处理（vision 模型） |
| 9 | DeferredToolFilterMiddleware | 延迟工具过滤（tool_search 启用时） |
| 10 | SubagentLimitMiddleware | 子 agent 并发限制 |
| 11 | LoopDetectionMiddleware | 循环检测（可选） |
| 12 | Custom middlewares | 自定义扩展 |
| 13 | ClarificationMiddleware | 澄清提问（始终最后） |

---

## 完整调用链总览

```
用户在 InputBox 按下发送
    ↓
InputBox.handleSubmit(message)
    ↓
[thread_id]/page.tsx → sendMessage(threadId, message)
    ↓
useThreadStream (hooks.ts)
    → thread.submit({
        messages: [{ type: "human", content: [...] }],
        context: { model_name, thinking_enabled, is_plan_mode, ... },
        config: { recursion_limit: 1000 },
      })
    ↓
LangGraph SDK (api-client.ts)
    → POST /threads/{thread_id}/runs/stream
      Header: X-CSRFToken: <from cookie>
      Body: { assistant_id: "lead_agent", input, config, context, ... }
    ↓
Backend Gateway Router (thread_runs.py)
    → stream_run(thread_id, body, request)
    ↓
services.start_run(body, thread_id, request)
    → run_mgr.create_or_reject()           # 创建 run 记录
    → build_run_config()                   # 构建 RunnableConfig
    → merge_run_context_overrides()        # 注入 model_name 等到 config
    → resolve_agent_factory("lead_agent")  # 直接返回 make_lead_agent 函数引用
    → asyncio.create_task(
          run_agent(
              agent_factory=make_lead_agent,
              graph_input={ messages: [...] },
              config=RunnableConfig({
                  configurable: { thread_id, ... },
                  context: { model_name, thinking_enabled, is_plan_mode, ... },
              }),
          )
      )
    ↓
run_agent() 内部
    → graph = make_lead_agent(config)      # ← 目标函数被调用
    ↓
make_lead_agent(config: RunnableConfig)
    → _get_runtime_config(config)          # 合并 configurable + context
    → _make_lead_agent(config, app_config)
        → 提取运行时参数
        → 解析最终 model name（三级 fallback）
        → 构建 13 层 middleware 链
        → 加载并过滤 tools
        → create_agent(model, tools, middlewares, system_prompt, ThreadState)
        → return CompiledGraph
    ↓
graph.stream(graph_input, config)          # 图开始执行
    ↓
StreamBridge → SSE events → 前端 useStream hook → UI 实时更新
```

---

## 关键文件速查表

| 层级 | 文件 | 关键行 |
|------|------|--------|
| 前端输入组件 | `frontend/src/components/workspace/input-box.tsx` | 255–297（handleSubmit） |
| 聊天页面 | `frontend/src/app/workspace/chats/[thread_id]/page.tsx` | 76–119 |
| 流式 Hook | `frontend/src/core/threads/hooks.ts` | 222–360（useStream）、428–622（sendMessage） |
| API 客户端 | `frontend/src/core/api/api-client.ts` | 35–73 |
| 路由层 | `backend/app/gateway/routers/thread_runs.py` | 116–149 |
| 服务层 | `backend/app/gateway/services.py` | 158–170（resolve_agent_factory）、248–353（start_run） |
| 图工厂函数 | `backend/packages/harness/deerflow/agents/lead_agent/agent.py` | 343–348（入口）、350–446（实现） |
| LangGraph 部署配置 | `backend/langgraph.json` | 全文（仅供 LangGraph Server 托管模式使用） |
