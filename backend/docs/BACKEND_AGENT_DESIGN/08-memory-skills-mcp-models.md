# 第 8 章：记忆 / 技能 / MCP / 模型工厂

> **本章目标**：讲透 Agent 的四个周边子系统。读完本章，你会理解 Agent 怎么"记住"用户（记忆系统）、怎么"学会新技能"（技能系统）、怎么"接入外部工具"（MCP）、怎么"选择和配置模型"（模型工厂）。

---

## 8.1 记忆系统 —— Agent 怎么记住用户

### 为什么需要记忆？（设计动机）

大多数 Agent 在对话结束后就**忘记一切**。DeerFlow 不同——它跨会话构建用户的持久记忆（偏好、技术栈、工作习惯），让 Agent"越用越懂你"。

记忆存储在**本地文件**，用户完全可控。

### 架构：四组件协作

```mermaid
flowchart LR
    A[MemoryMiddleware<br/>after_agent] -->|过滤消息+捕获user_id| B[MemoryQueue<br/>去抖动批处理]
    B -->|Timer触发| C[MemoryUpdater<br/>LLM提取事实]
    C -->|原子写入| D[MemoryStorage<br/>per-user memory.json]
    D -->|注入| E[DynamicContextMiddleware<br/>系统提示词]
```

### MemoryMiddleware —— 入口（第 3 章 #17 已讲）

`after_agent` 时：过滤消息（只保留用户输入 + 最终 AI 回复）、检测纠正/强化信号、**提前捕获 user_id**（跨线程 ContextVar 陷阱）、入 MemoryQueue。

### MemoryQueue —— 去抖动批处理

```python
# 引用位置：backend/packages/harness/deerflow/agents/memory/queue.py (核心设计)
class MemoryUpdateQueue:
    # 按 (thread_id, user_id, agent_name) 去重
    # debounce_seconds（默认30s）：最后一条消息后等30s再处理
    # 批处理：同一线程的快速连续消息折叠为最新一条
```

**设计动机深挖——为什么去抖动 30 秒？**

用户可能连续发 5 条消息（"帮我..."、"还有..."、"对了..."）。如果每条都触发一次 LLM 记忆提取，会：(1) 浪费 token；(2) 产生 5 个碎片化的记忆更新。去抖动 30 秒让这 5 条消息"折叠"成一次处理——等用户停下来再批量提取记忆。

**入队时捕获 user_id 的跨线程陷阱**（第 3 章 #17 已详述）：Timer 在另一个线程触发，ContextVar 跨线程不传播，必须提前捕获。

### MemoryUpdater —— LLM 提取事实

```python
# 引用位置：backend/packages/harness/deerflow/agents/memory/updater.py:380+
class MemoryUpdater:
    # _prepare_update_prompt：加载当前记忆 + 格式化对话 + 构建提示
    # _do_update_memory_sync：用同步 model.invoke()（避免创建第二个事件循环 #2615）
    # _apply_updates：更新 user/history 部分，移除/添加 facts（置信度过滤）
```

**LLM 提取流程**：
1. 加载当前 memory.json。
2. 格式化对话 + 当前记忆 → 构建 LLM 提示。
3. LLM 分析"这段对话揭示了用户的什么新信息"。
4. 返回结构化 JSON：`{user: {...}, history: {...}, newFacts: [...], factsToRemove: [...]}`。
5. `_apply_updates` 原子写入（tmp + rename）。

### memory.json 数据结构

```json
{
  "version": "1.0",
  "lastUpdated": "2026-07-16T10:00:00Z",
  "user": {
    "workContext": {"summary": "后端开发者，主要用Python", "updatedAt": "..."},
    "personalContext": {"summary": "..."},
    "topOfMind": {"summary": "..."}
  },
  "history": {
    "recentMonths": {"summary": "..."},
    "earlierContext": {"summary": "..."},
    "longTermBackground": {"summary": "..."}
  },
  "facts": [
    {
      "id": "fact-001",
      "content": "偏好用中文回复",
      "category": "preference",
      "confidence": 0.9,
      "createdAt": "...",
      "source": "conversation"
    }
  ]
}
```

**事实类别**：preference / knowledge / context / behavior / goal / correction。
**置信度带**：0.9-1.0 显式、0.7-0.8 隐式、0.5-0.6 推断。只有 ≥ `fact_confidence_threshold`（默认 0.7）的事实才存储。

### 本次演进：per-fact expected_valid_days（新增 #4143）

LLM 现在可以为每个事实分配 `expected_valid_days`（预期有效天数）。短期事实（如"正在调试某个 bug"）几天后自动过期，长期事实（如"喜欢 Python"）永久保留。这让记忆系统不会无限积累过时事实。

### 注入机制

`DynamicContextMiddleware` 的 `_build_full_reminder` 调用 `_get_memory_context`：
1. 读取当前用户的 memory.json。
2. `format_memory_for_injection` 构建 `<memory>` 块。
3. 按 `max_injection_tokens`（默认 2000）预算限制——事实按置信度排名，增量添加。
4. 作为 HumanMessage 注入（`hide_from_ui=True`，防 OWASP LLM01 提权——第 3 章 #14 详述）。

---

## 8.2 技能系统 —— Agent 怎么学会新技能

### 技能是什么？

技能（Skill）是结构化的能力模块——一个 `SKILL.md` 文件定义了某个工作流、最佳实践、参考资源。DeerFlow 内置了研究、报告生成、幻灯片创建、网页生成、图像生成等技能。

### SKILL.md 格式

```markdown
---
name: report-generation
description: Generate structured reports from research findings
license: MIT
allowed-tools:
  - write_file
  - read_file
---

# Report Generation Skill

## Workflow
1. Organize findings into sections
2. ...
```

**YAML frontmatter** 必填字段：`name`（hyphen-case）、`description`。可选：`license`、`allowed-tools`（技能白名单工具）。

### 加载流程

`load_skills()`（`skills/storage/skill_storage.py:212-246`）：
1. 递归扫描 `skills/{public,custom}/` 找所有 SKILL.md。
2. `parse_skill_file` 解析 frontmatter。
3. 按 name 去重。
4. 合并 enabled 状态（从 `extensions_config.json` 读，每次重新读盘保证 Gateway API 的改动即时生效）。

### 两种注入方式

| 方式 | 触发 | 内容 |
|------|------|------|
| **自动（系统提示词）** | 每次 Agent 创建 | 只列元信息（name + description + location）——`<skill_system>` 块 |
| **斜杠激活** | 用户输入 `/skill-name` | 加载完整 SKILL.md 全文——`SkillActivationMiddleware`（第 3 章 #15） |
| **延迟发现**（新增） | `skills.deferred_discovery` | 紧凑的 `<skill_index>`，模型用 `describe_skill` 按需查看 |

**为什么默认只列元信息？** 技能全文很长（可能几千 token），全塞进系统提示词会撑爆上下文 + 破坏 prefix-cache。只列元信息，模型需要时自己 `read_file` 读全文——**按需加载**。

### 技能与工具白名单

技能的 `allowed-tools` 限制"用这个技能时只能用哪些工具"。`filter_tools_by_skill_allowed_tools`（第 2 章步骤 6）强制执行。

---

## 8.3 MCP 系统 —— 接入外部工具

### MCP 是什么？

MCP（Model Context Protocol）是 Anthropic 提出的**工具协议标准**。一个 MCP 服务器可以提供任意数量的工具，任何 MCP 客户端都能使用。DeerFlow 作为 MCP 客户端，能接入生态里的所有 MCP 服务器。

### 架构

```python
# 引用位置：backend/packages/harness/deerflow/mcp/tools.py:541-653 (get_mcp_tools)
# 使用 langchain-mcp-adapters 的 MultiServerMCPClient
client = MultiServerMCPClient(servers_config, tool_interceptors=..., tool_name_prefix=True)
tools = await client.get_tools()
```

### 懒加载 + mtime 缓存失效

```python
# 引用位置：backend/packages/harness/deerflow/mcp/cache.py
# 模块级缓存：_mcp_tools_cache, _config_mtime
# get_cached_mcp_tools：未初始化时自动初始化
# _is_cache_stale：比较 ExtensionsConfig 的 mtime，变了就 reset
```

**设计动机**：MCP 工具发现是昂贵的操作（要连每个 MCP 服务器握手）。缓存避免每次 Agent 创建都重新发现。mtime 检测让"Gateway API 修改了 MCP 配置"后，下次自动重新发现。

### 传输协议

- **stdio**：命令式启动子进程（如 `npx -y some-mcp-server`）。
- **sse**：Server-Sent Events 连接。
- **http**：HTTP 请求。

### OAuth 支持

HTTP/SSE MCP 服务器支持 OAuth：
- `client_credentials`：需 client_id/client_secret。
- `refresh_token`：需 refresh_token。
- `OAuthTokenManager` 管理 token 获取和自动刷新（提前 `refresh_skew_seconds` 刷新）。

### 路径翻译

stdio MCP server 的 cwd 钉在 thread 的 user-data 树内，产生的文件在可服务的位置。`_local_uri_to_virtual_path` 把 host 路径翻译成 `/mnt/user-data/...` 虚拟路径——让 Agent 能通过统一的虚拟路径访问 MCP 产生的文件。

### 延迟工具机制（第 3 章 #24 已讲）

MCP 服务器可能提供几十个工具，全塞进模型上下文浪费 token。DeerFlow 把 MCP 工具标记为 "deferred"，模型用 `tool_search` 或 McpRoutingMiddleware 自动发现后才可见。

---

## 8.4 模型工厂 —— 怎么选择和配置模型

### create_chat_model 完整流程

```python
# 引用位置：backend/packages/harness/deerflow/models/factory.py:174-204
def create_chat_model(name: str | None = None, thinking_enabled: bool = False, *,
                      app_config: AppConfig | None = None, attach_tracing: bool = True, **kwargs) -> BaseChatModel:
    config = app_config or get_app_config()
    if name is None:
        name = config.models[0].name  # 默认用第一个
    model_config = config.get_model_config(name)
    model_class = resolve_class(model_config.use, BaseChatModel)  # 反射加载
    model_settings_from_config = model_config.model_dump(exclude_none=True, exclude={
        "use", "name", "display_name", "description",
        "supports_thinking", "supports_reasoning_effort",
        "when_thinking_enabled", "when_thinking_disabled", "thinking",
        ...
    })
    # ... thinking 处理 + stream 默认 + stream_chunk_timeout ...
    model_instance = model_class(**kwargs, **model_settings_from_config)
    if attach_tracing:
        model_instance.callbacks = build_tracing_callbacks()
    return model_instance
```

**► 逐行注解**：
- **`resolve_class(model_config.use, BaseChatModel)`**：反射加载。`config.yaml` 的 `use: langchain_openai:ChatOpenAI` 解析成实际的类。这让用户能用任何 LangChain 兼容的模型类。
- **`model_dump(exclude_none=True, exclude={...})`**：dump 配置时**剔除元字段**（use/name/display_name/supports_thinking/when_thinking_*）——这些是 DeerFlow 的配置元数据，不该传给模型构造函数。
- **`attach_tracing`**：第 2 章详述的不变量——graph 内的调用传 `False`，独立调用（MemoryUpdater）传 `True`。

### stream_chunk_timeout 默认（重要的性能调优）

```python
# 引用位置：backend/packages/harness/deerflow/models/factory.py:126-171
_DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS: float = 240.0

def _apply_stream_chunk_timeout_default(model_class, model_settings_from_config):
    """Inject a generous stream_chunk_timeout for OpenAI-compatible clients."""
    if not issubclass(model_class, BaseChatOpenAI):
        model_settings_from_config.pop("stream_chunk_timeout", None)  # 非 OpenAI 系列丢弃
        return
    if "stream_chunk_timeout" in model_settings_from_config:
        return  # 用户显式配置了，保留
    model_settings_from_config["stream_chunk_timeout"] = _DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS
```

**► 设计动机深挖（issue #3189）**：
- **问题**：langchain-openai 默认 `stream_chunk_timeout=120s`。但推理模型（DeepSeek-R1、Doubao-thinking、GPT-5）的第一个 chunk 可能要 90~150 秒（长思考暂停）——超过 120s 就被误判为超时。
- **解决**：DeerFlow 默认 240 秒。**只对 OpenAI-compatible 系列**（`issubclass(model_class, BaseChatOpenAI)`）——因为 `stream_chunk_timeout` 是 OpenAI 客户端的字段，其他客户端（如 ChatAnthropic）不认。
- **注释极其详细地解释了为什么用 `issubclass` 而非显式类名 allowlist**：issue #3189 报告于 `mimo-v2.5`（`PatchedChatMiMo`），原修复只匹配 `ChatOpenAI`/`PatchedChatOpenAI`，漏了其他子类。改用 `issubclass` 让所有 OpenAI-compatible 子类自动继承。

### thinking 启用/禁用

**启用**（`thinking_enabled=True`）：
- 检查 `supports_thinking`，False 则抛错。
- 合并 `when_thinking_enabled` 配置到 model settings。

**禁用**（`thinking_enabled=False`）——四种策略，按优先级：
1. 用户显式 `when_thinking_disabled`：全量覆盖。
2. OpenAI-compatible gateway：`extra_body.thinking.type="disabled"` + `reasoning_effort="minimal"`。
3. **vLLM**：`chat_template_kwargs.thinking/enable_thinking` 设 False。
4. 原生 anthropic：`thinking={"type": "disabled"}`。

### vLLM Provider 特殊处理

```python
# 引用位置：backend/packages/harness/deerflow/models/vllm_provider.py:159-258
# VllmChatModel(ChatOpenAI) — 继承 OpenAI 兼容
# 保留 vLLM 的非标准 reasoning 字段：
#   - 请求侧：_normalize_vllm_chat_template_kwargs（thinking→enable_thinking）
#   - 响应侧：additional_kwargs["reasoning"] + reasoning_content
#   - 流式 delta：_convert_delta_to_message_chunk_with_reasoning
```

**设计动机**：vLLM 0.19.0 通过 OpenAI-compatible API 暴露推理模型，但 LangChain 默认适配器会丢掉非标准的 `reasoning` 字段，破坏 thinking/tool-call 交错流。`VllmChatModel` 保留这个字段。

---

## 8.5 本章小结

四个周边子系统的核心设计：

### 记忆系统
1. **四组件**：MemoryMiddleware（入口）→ Queue（去抖动）→ Updater（LLM 提取）→ Storage（per-user 文件）。
2. **per-user 隔离**：每个用户独立的 memory.json。
3. **去抖动 30 秒**：连续消息折叠为一次处理，省 token。
4. **per-fact 有效期**（新增）：短期事实自动过期。
5. **注入预算**：按置信度排名，限制 2000 token。

### 技能系统
1. **SKILL.md** 格式（YAML frontmatter + markdown 正文）。
2. **按需加载**：系统提示词只列元信息，模型需要时读全文。
3. **斜杠激活 + 延迟发现**：用户显式或模型主动发现。
4. **工具白名单**：技能可限制可用工具。

### MCP 系统
1. **标准协议**：接入 MCP 生态的所有工具服务器。
2. **懒加载 + mtime 缓存**：避免重复发现。
3. **三种传输**：stdio/sse/http + OAuth。
4. **延迟工具**：MCP 工具默认隐藏，tool_search 或 McpRouting 发现后可见。

### 模型工厂
1. **反射加载**：config.yaml 的 `use` 字段解析到任意 LangChain 模型类。
2. **thinking 四种禁用策略**：按 provider 优先级。
3. **stream_chunk_timeout 240s**：适配推理模型的长思考暂停。
4. **vLLM provider**：保留非标准 reasoning 字段。
5. **tracing 不变量**：graph 内 `attach_tracing=False`。

**核心思想**：这四个子系统让 Agent 具备了"记忆过去、学会技能、接入外部、适配多模型"的能力。它们都是**可选的、可配置的、可扩展的**——通过 `config.yaml` 和 `extensions_config.json` 声明式配置，通过反射和标准协议（MCP）实现扩展性。这让 DeerFlow 作为一个 harness（运行时），既能开箱即用，又能深度定制。

---

## 全系列总结

八章节完整覆盖了 DeerFlow 后端 Agent 的全部设计：

| 章 | 主题 | 核心问题 |
|----|------|----------|
| 01 | 架构总览 | 一个请求从进来到出去经历了什么？ |
| 02 | Agent 工厂 | Agent 是怎么被造出来的？ |
| 03 | 31 个中间件 | 请求在 Agent 内部怎么被一步步处理？ |
| 04 | 工具系统 | Agent 手里有哪些工具？怎么来的？ |
| 05 | 沙箱系统 | Agent 怎么执行代码、读写文件？ |
| 06 | 子 Agent 系统 | 复杂任务怎么分解和委派？ |
| 07 | 运行时 + Gateway | Agent 怎么被调度执行？结果怎么传给前端？ |
| 08 | 记忆/技能/MCP/模型 | Agent 的"记忆"和"扩展能力"怎么实现？ |

读完这八章，你已经理解了 DeerFlow 后端的**每一个关键组件**。你现在可以带着这份理解去读源码——文档里的 `file:line` 引用会带你到具体的实现位置，而你已经知道"它在整体架构中的位置"和"为什么这么设计"。
