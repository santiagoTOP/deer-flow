# 第 3 章（续）：安全限流层中间件 #24-#31

> 本文是第 3 章的第三部分，讲解排在上下文层之后的 **8 个安全/限流中间件 + ClarificationMiddleware**。它们是**防护性中间件**——防止 Agent 失控（死循环、超预算、并发爆炸、执行被安全拦截的操作、空回复）。
>
> 阅读前请先读 [03a-middlewares-base.md](./03a-middlewares-base.md) 和 [03b-middlewares-context.md](./03b-middlewares-context.md)。

---

## #24 DeferredToolFilterMiddleware —— 隐藏延迟工具（条件性）

**职责**：把 MCP 工具的 schema 从模型绑定中隐藏，直到 `tool_search` 或 McpRoutingMiddleware 提升它们。

### 两个 hook 配合

- **`wrap_model_call`**：**隐藏 schema**——从传给模型的工具列表里移除未 promote 的延迟工具，模型根本看不到它们。
- **`wrap_tool_call`**：**拦截调用**——万一模型通过某种方式猜到一个延迟工具名并调用，返回错误提示"先用 tool_search 发现它"。

### 提升状态

提升状态按 `catalog_hash` 从 `state["promoted"]` 读（第 1 章讲的 reducer）。catalog_hash 变了说明工具目录变了，旧的提升记录失效（防漂移）。

### 与 McpRoutingMiddleware 的配合

routing 先 promote（自动提升相关工具），filter 再隐藏（隐藏未提升的）。`assert_mcp_routing_before_deferred_filter` 是构建时检查保证顺序。

---

## #25 SystemMessageCoalescingMiddleware ★新增 —— SystemMessage 合并

**职责**：把所有 SystemMessage 合并成一条放在最前面，兼容严格后端。

### 问题背景（文件头注释极其详细）

```python
# 引用位置：backend/packages/harness/deerflow/agents/middlewares/system_message_coalescing_middleware.py:1-28
"""Middleware to coalesce multiple SystemMessages into a single leading one.

Strict OpenAI-compatible backends (vLLM, SGLang, Qwen) and Anthropic reject
non-leading SystemMessages with errors like "System message must be at the
beginning" or "Received multiple non-consecutive system messages". The
official OpenAI API tolerates mid-conversation system messages, so the issue
only surfaces on strict backends.

DeerFlow's lead agent accumulates multiple SystemMessages because
DynamicContextMiddleware uses the ID-swap technique to replace the first or
last HumanMessage with a triplet whose first element is a SystemMessage
reminder (framework-owned date/metadata must not masquerade as user input,
per OWASP LLM01). On midnight crossings a second SystemMessage (date update)
is injected. ...
"""
```

**► 注解**：问题根源是**DynamicContextMiddleware 的 ID-swap 技术产生了 SystemMessage**（日期提醒）。加上跨午夜可能注入第二个。多个中间件（DurableContext 的 authority contract 也是 SystemMessage）累积，最终消息列表里有多个 SystemMessage。

**OpenAI 官方 API 容忍**消息列表中间的 SystemMessage，但**严格后端**（vLLM、SGLang、Qwen、Anthropic）拒绝——报错"System message must be at the beginning"。这让 DeerFlow 在这些后端上崩溃。

### 解决方案

```python
# 引用位置：backend/packages/harness/deerflow/agents/middlewares/system_message_coalescing_middleware.py:63-90 (核心逻辑)
def _coalesce_request(request: ModelRequest) -> ModelRequest | None:
    """Merge ``request.system_message`` and in-``messages`` SystemMessages into one."""
    in_msg_systems = [m for m in request.messages if isinstance(m, SystemMessage)]
    if not in_msg_systems:
        return None  # 没有需要合并的，零改动（保 prefix-cache）

    # Merge system_message (if any) + all in-messages SystemMessages.
    parts: list[SystemMessage] = []
    if request.system_message is not None:
        parts.append(request.system_message)
    parts.extend(in_msg_systems)

    # Deduplicate dynamic_context_reminder SystemMessages: only keep the last
    # one (most recent date), drop earlier reminders. On midnight crossings
    # the merged content would otherwise contain two adjacent contradictory
    # ...
```

**► 逐行注解**：
- **第 78-80 行**：扫描 `request.messages` 里的所有 SystemMessage。如果没有，返回 `None`——**零改动，保 prefix-cache**。
- **第 83-86 行**：合并 `request.system_message`（静态系统提示词，langchain 1.2.15+ 放在独立字段）+ messages 里的 SystemMessage。
- **第 88-90 行**：**去重 dynamic_context_reminder**——只保留最后一个（最新日期）。跨午夜时旧日期 + 新日期都在，合并后只留新的。

### 只改请求，不改 state

注释强调："It only touches the request payload; the persistent conversation state (checkpoint) is unchanged"——**只改发给 provider 的请求**，不改 checkpoint。这样其他靠 marker 扫描历史的中间件（如 `is_dynamic_context_reminder`）还能正常工作。

### 设计动机：provider-agnostic 层

注释最后一句："Mirrors the per-request coalescing already done for Claude in `claude_provider._coalesce_system_messages` but at a provider-agnostic layer so every backend benefits from a single fix instead of per-provider patches."

**► 注解**：以前只在 Claude provider 里做这个合并。现在提到中间件层——**所有后端都受益**，不用每个 provider 单独 patch。这是"在正确的抽象层解决问题"的好实践。

---

## #26 SubagentLimitMiddleware —— 子 Agent 限流（条件性，subagent_enabled）

**职责**：模型一次响应里发起的 `task` 调用超过上限时，截断多余的。

### 双闸控制的硬闸

这是第 2 章讲的"并发控制双闸"的**硬闸**。prompt 软闸（告诉模型"最多 N 个"）不可靠，这个中间件是代码层面的强制截断。

### 两个限制参数（演进）

现在接收两个参数：
- **`max_concurrent`**（单次响应并发上限）：clamp 到 [2,4]，防止配置错误。
- **`max_total`**（单次 run 累计上限）：配合目标延续循环的新增安全阀。

### "保留前 N 个，丢弃多余"

不是平均分配，而是模型列出几个就执行前 N 个——简单且可预测。被丢弃的不会执行，模型下一轮可以重新发起。

---

## #27 LoopDetectionMiddleware —— 循环检测（条件性，loop_detection.enabled）

**职责**：检测 Agent 陷入"反复调用相同工具"的死循环，强制终止。

### 双层检测

**层 1：hash-based**。判断"两次调用是不是重复的"。难点是参数不同但本质重复的情况（`read_file("f", start=1, end=100)` vs `read_file("f", start=1, end=101)`）。`_stable_tool_key` 做了**语义归一化**：
- `read_file`：按**行范围桶**归一化（相近的行范围算同一次）。
- `write_file`/`str_replace`：全 args hash（内容不同就不算重复）。

**层 2：按工具类型频率**。

### 两级阈值

- **warn_threshold**：达到就提醒模型"你在重复"，给它自救机会。
- **hard_limit**：达到就**强制剥离 tool_calls**，让模型只能输出文本——彻底打断循环。同时写 `stop_reason="loop_capped"` 到 `runtime.context`，worker 收尾读取。

### 警告注入的精妙

警告作为 `name="loop_warning"` 的 HumanMessage **追加在消息列表末尾**（在所有 ToolMessage 之后）。为什么末尾？因为 OpenAI/Anthropic 要求 tool_calls 后面必须紧跟对应的 ToolMessage——如果警告插在中间，会破坏配对完整性。

---

## #28 TokenBudgetMiddleware —— 单次 run token 预算（条件性，token_budget.enabled）

**职责**：限制单次 run 消耗的总 token，超预算则强制终止。

### 关键：历史消息不计入本次预算

`before_agent` 把历史消息（从 checkpoint 恢复的）标记为 seen——它们的 token **不计入**本次预算。只算"本次 run 新产生的 token"。否则多轮对话很快触发上限。写 `stop_reason="token_capped"`。

### 两级阈值 + BoundedDict

和 LoopDetection 类似：hard_stop 强制结束，warn 提醒收尾。用 `BoundedDict`（有容量上限）存 run 状态，防长期运行泄漏。

---

## #29 TerminalResponseMiddleware ★新增 —— 终止响应保障

**职责**：确保工具调用轮次最终以一个可见的 assistant 响应结束，防止"工具跑完了但模型没回复"的静默成功。

### 问题背景（极其重要的用户体验问题）

```python
# 引用位置：backend/packages/harness/deerflow/agents/middlewares/terminal_response_middleware.py:1 (文件头)
"""Ensure tool-using lead-agent turns end with a visible assistant response."""
```

有时模型在执行完工具后，返回一个**空的 AIMessage**（没有文本、没有 tool_calls）。LangChain 的默认路由看到"没有 tool_calls"就认为 turn 结束了——结果是**用户看到一个"成功的空回复"**。

这极其糟糕：Agent 做了一堆工具调用（读了文件、跑了代码），却什么都没告诉用户。用户以为 Agent 坏了。

### 两步处理

```python
# 引用位置：backend/packages/harness/deerflow/agents/middlewares/terminal_response_middleware.py:108-149
    def _apply(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        messages = list(state.get("messages") or [])
        if not messages or not isinstance(messages[-1], AIMessage):
            return None

        last = messages[-1]
        if _has_visible_content(last) or _has_tool_call_intent_or_error(last):
            return None  # 有内容或有工具调用意图 → 不干预
        if not _tool_result_in_current_turn(messages):
            return None  # 当前轮没有工具结果 → 不干预

        key = self._key(runtime)
        with self._lock:
            # The recovery budget is once per run, not once per empty message.
            # A retry that calls another tool must not refresh the budget and
            # create an unbounded empty -> retry -> tool loop.
            retry_count = self._retry_counts.get(key, 0)
            if retry_count == 0:
                self._retry_counts[key] = 1
                self._pending_prompts[key] = True

        if retry_count == 0:
            # 第一次空回复 → 删掉空消息 + 跳回 model 重试
            message_updates = [RemoveMessage(id=last.id)] if last.id else []
            return {"messages": message_updates, "jump_to": "model"}

        # 第二次还是空 → 返回 fallback 错误消息
        additional_kwargs = dict(last.additional_kwargs or {})
        additional_kwargs.update(
            {
                "deerflow_error_fallback": True,
                "error_reason": "Model returned an empty terminal response after one retry",
            }
        )
        fallback = last.model_copy(
            update={
                "content": _FALLBACK_CONTENT,
                "additional_kwargs": additional_kwargs,
            }
        )
        return {"messages": [fallback]}
```

**► 逐行注解**：
- **第 114-115 行**：如果有可见内容或有工具调用意图 → 不干预。
- **第 121-127 行注释**："The recovery budget is **once per run**, not once per empty message"——**重试预算是每次 run 一次**，不是每次空消息一次。防止"空回复 → 重试 → 调工具 → 又空回复 → 再重试"的死循环。
- **第 129-134 行**：第一次空回复 → 用 `RemoveMessage` 删掉空消息 + `jump_to: "model"` 跳回模型重试。**删掉空消息**是因为下次模型调用会获得新 message id，成功的恢复不应该在 checkpoint 里留下空消息。
- **第 136-149 行**：第二次还是空 → 返回带 `deerflow_error_fallback` 标记的 fallback 消息。

### 恢复提示

```python
# 引用位置：backend/packages/harness/deerflow/agents/middlewares/terminal_response_middleware.py:151-163
    def _augment_request(self, request: ModelRequest) -> ModelRequest:
        # ...
        reminder = HumanMessage(
            content=_RECOVERY_PROMPT,
            name="terminal_response_recovery",
            additional_kwargs={"hide_from_ui": True},
        )
        return request.override(messages=[*request.messages, reminder])
```

`_RECOVERY_PROMPT`（文件顶部定义）："Your previous response after the tool execution was empty. Review the tool results already present in the conversation and provide a concise, user-visible final response."

**► 注解**：重试时注入一条隐藏的 HumanMessage，告诉模型"你刚才的回复是空的，请根据工具结果给出最终回复"。这条消息 `hide_from_ui=True`，用户看不到。

---

## #30 SafetyFinishReasonMiddleware —— Provider 安全终止（条件性）

**职责**：当 LLM provider 因安全原因（content_filter、refusal）终止响应，且响应里还带着 tool_calls 时，剥离这些 tool_calls。

### 问题背景

有些 provider（如 OpenAI）在检测到不安全内容时，返回 `finish_reason=content_filter` 并**截断响应**。但诡异的是，截断前的响应可能已经包含了 `tool_calls`——如果直接执行这些 tool_calls，等于执行了"被安全拦截的操作"。

### 关键判断

```python
# 引用位置：backend/packages/harness/deerflow/agents/middlewares/safety_finish_reason_middleware.py:278-280
    tool_calls = last.tool_calls
    if not tool_calls:
        return None  # content_filter 但没有 tool_calls → 放行
```

`content_filter` 但**没有** tool_calls 时，放行（让部分文本响应自然到达用户）。只在"安全终止 + 有 tool_calls"时干预。

### 审计不记录工具参数

安全审计只记工具名，不记参数——因为参数可能就是敏感内容，记下来等于二次泄露。

### 写 stop_reason

写 `stop_reason="safety_capped"`，worker 收尾读取。

### 为什么注册在 Loop 之后

利用 reverse dispatch，Safety 的 `after_model` 先执行（注册在后 = after_model 先跑），剥离后的干净消息再流经 Loop/Subagent 统计，不触发误报。

---

## #31 ClarificationMiddleware —— 澄清中断（永远最后）

**职责**：拦截 `ask_clarification` 工具调用，格式化为用户友好的问题，中断执行等待用户回答。

### 永远在最后

这是**永远在最后**的中间件（`build_middlewares` 最后一行 append）。因为它用 `wrap_tool_call` 拦截 `ask_clarification`，必须在所有其他工具处理之后才能判断"这是不是要中断"。

### 拦截不执行

```python
# 引用位置：backend/packages/harness/deerflow/agents/middlewares/clarification_middleware.py:158-178
    @override
    def wrap_tool_call(self, request, handler):
        if request.tool_call.get("name") != "ask_clarification":
            return handler(request)  # 不是澄清调用 → 正常执行
        return self._handle_clarification(request)  # 是澄清 → 不执行原handler
```

检查工具名，不是 `ask_clarification` 就正常执行；是的话走 `_handle_clarification`，**不执行原 handler**。

### Command(goto=END) 中断

```python
# 引用位置：backend/packages/harness/deerflow/agents/middlewares/clarification_middleware.py:148-156
        return Command(
            update={"messages": [tool_message]},
            goto=END,  # 跳到END节点，中断整个Agent循环
        )
```

返回 `Command(goto=END)`——LangGraph 的控制流指令，直接跳到 END 节点，中断整个 Agent 循环。Agent 暂停，把问题呈现给用户，等待用户回答后开启新 run 继续。

### 稳定 message id

用 `_stable_message_id(tool_call_id, formatted_message)` 生成稳定 ID——重试时**替换而非追加**。如果模型多次调用 ask_clarification，用相同 ID 让它们替换，不堆积。

### non_interactive 模式

在 `non_interactive` 模式下，`ask_clarification` 工具已经被工厂过滤掉了（第 2 章），所以这个中间件虽然存在但不会触发。

---

## 第 3 章完整总结（三个文件合起来）

31 个中间件是 DeerFlow Agent 的"神经系统"，分四层：

| 层 | 数量 | 解决的问题 | 代表中间件 |
|----|------|-----------|-----------|
| **基础层** (03a) | 13 | 输入安全、上下文管理、错误兜底、文件质量 | InputSanitization、Sandbox、ReadBeforeWrite |
| **上下文层** (03b) | 10 | 动态内容注入、任务跟踪、记忆 | DynamicContext(ID-swap)、DurableContext、Summarization |
| **安全层** (03c) | 7 | 防止 Agent 失控 | LoopDetection、TokenBudget、TerminalResponse、SystemMessageCoalescing |
| **收尾** (03c) | 1 | 与用户交互中断 | Clarification |

### 本次新增的 7 个中间件填补的能力缺口

| 新中间件 | 解决的问题 | 设计亮点 |
|---------|-----------|---------|
| **ToolResultSanitization** | 远程工具结果间接注入 | "两个不可信入口"对称防御 |
| **ReadBeforeWrite** | 盲写覆盖/重复追加 | 版本门 + 5 个不变量 + fail-open |
| **ToolProgress** | 工具进度不可见 | 构建时顺序断言 |
| **DurableContext** | 摘要丢失委派/技能信息 | Capture+Injection 两阶段，信任边界 |
| **McpRouting** | MCP 工具发现不够智能 | 自动 promote，优雅降级 |
| **SystemMessageCoalescing** | 多 SystemMessage 被严格后端拒绝 | provider-agnostic 层合并 |
| **TerminalResponse** | 工具跑完但模型空回复 | once-per-run 重试预算，防死循环 |

### 四个贯穿全层的设计模式

1. **双闸控制**：prompt 软约束 + middleware 硬截断（子 Agent 限流）。
2. **两级阈值**：warn（提醒）+ hard（强制终止），终止时写 `stop_reason` 归因。
3. **fail-closed vs fail-open**：安全相关 fail-closed（Guardrail、SandboxAudit），可用性相关 fail-open（InputSanitization、ReadBeforeWrite）。
4. **构建时断言**：用代码强制顺序不变量（ToolProgress/ToolError、McpRouting/DeferredFilter），而非依赖注释。

### 核心思想

中间件链是**洋葱模型**——请求从外层（InputSanitization）逐层穿入到模型，响应从模型逐层穿出到用户。每层各司其职，通过 reverse dispatch 保证顺序正确。这个设计让功能**可组合、可插拔**（条件性实例化）、**可扩展**（`custom_middlewares`）。

**下一章（04）**：Agent 手里的工具是怎么来的——`get_available_tools` 的四源组装。
