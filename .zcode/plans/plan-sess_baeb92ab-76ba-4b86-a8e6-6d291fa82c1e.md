# 计划：新建 `get-checkpointer-deep-dive.md`

## 目标
在 `D:\deer-flow\backend\docs\BACKEND_AGENT_DESIGN\` 下新建 `get-checkpointer-deep-dive.md`，用与 `run-manager-deep-dive.md` / `stream-bridge-deep-dive.md` **完全一致的体裁**讲清这一行：

```python
get_checkpointer: Callable[[Request], Checkpointer] = _require("checkpointer", "Checkpointer")
```

（位于 `backend/app/gateway/deps.py:366`）

## 体裁对齐（来自现有姊妹篇的固定模板）
- **标题公式**：`# get_checkpointer 深度讲解：为什么不能直接在路由里 InMemorySaver() 就完事`
- **四幕结构**：第 0 步术语铺垫 → 第 1 步朴素写法（反面教材）→ 第 2 步 6 个麻烦 → 第 3 步本质串联
- **生活比喻驱动**、**每段代码后 `逐行：` 列表**、`# ←` / `# ★` 行内注释、ASCII 全景图（不用 Mermaid）、⚠️ 仅用于真正的坑、`file:line` 引用贯穿全文
- **英文 docstring/issue 原文 + `翻译：` 中文** 的引用惯例
- **结尾 coda**：复用 *"所以它不是…任意一个…都过不去"* 的收尾，并加姊妹篇配对句
- **全篇简体中文**（代码、标识符、路径、引用原文保留英文）

## 文件位置
`D:\deer-flow\backend\docs\BACKEND_AGENT_DESIGN\get-checkpointer-deep-dive.md`（与两篇姊妹篇同目录）

## 内容大纲（全部用真实代码核实行号）

### 第 0 步：术语铺垫（8 个小节，每个结尾带比喻，与姊妹篇重叠处标 ※）
- **0.1 Checkpointer——agent 的"存档"** ※（复用姊妹篇 §0.7 结论）
- **0.2 `BaseCheckpointSaver` 的三件套 `aget_tuple`/`aput`/`aput_writes`——存档机的按钮**
- **0.3 `app.state`——进程级"工具箱"**（Starlette.State）
- **0.4 FastAPI 的 lifespan——开机/关机的总闸** ※
- **0.5 `AsyncExitStack`——一摞"用完要还"的资源** ※
- **0.6 `@asynccontextmanager`——能"开/关"的工厂**
- **0.7 HTTP 503——"还没准备好"的信号**
- **0.8 worker / `GATEWAY_WORKERS`——进程数** ※

### 第 1 步：朴素写法（反面教材）
路由里直接 `from langgraph.checkpoint.memory import InMemorySaver; checkpointer = InMemorySaver()`。
逐行解释 → 点出**根本病根**：*checkpointer 被当成"一个随手 new 的对象"，而不是"一个有生命周期的进程级资源"*。

### 第 2 步：6 个麻烦（每个都走 场景 → 朴素写法为什么做不到 → 怎么解决的）
1. **真正的后端要开数据库连接，不能每次 new** → `make_checkpointer` 工厂 + 启动时建一次（`async_provider.py:167-202`，三种后端 memory/sqlite/postgres，postgres 走 `AsyncConnectionPool`，`async_provider.py:50-67`）
2. **异步构造需要事件循环，import 时跑不了** → 必须在 lifespan 里 `await`（`deps.py:267` 的 `stack.enter_async_context(make_checkpointer(config))`）
3. **后端选择依赖 config，不能写死** → `make_checkpointer` 的优先级逻辑（legacy `checkpointer:` → 统一 `database:` → 默认 InMemorySaver，`async_provider.py:186-202`）
4. **关机要按顺序回收，否则 PoolClosed** → 历史 bug issue #3373；`deps.py:88-111` 的 `_drain_inflight_runs` + `deps.py:334-343` 的 `finally:` 先排干 in-flight run 再让 `AsyncExitStack` 关连接池
5. **多 worker 下 SQLite 会互相踩** → `_enforce_postgres_for_multi_worker`（`deps.py:49-80`，在 `deps.py:256` 启动闸调用，`GATEWAY_WORKERS>1` 强制 postgres + heartbeat）
6. **缺失时要给干净的 503，而不是深层 NoneType 崩** → `_require` 工厂（`deps.py:351-361`）的 `dep` 闭包：`getattr` → None 则 `raise HTTPException(503)`；并讲清 6 个兄弟 getter 共用同一套（`deps.py:364-369`，配一张表）

### 第 3 步：本质串联
- 用 blockquote 重申病根
- 对比表：维度 | 朴素写法（每次 new） | 真实写法（app.state 单例 + _require）—— 寿命 / 资源占用 / 后端可换 / 关机回收 / 多worker / 缺失表现 共 6 行
- ①–⑥ 一句话列表（每个麻烦 → 对应机制）
- 一张 ASCII 全景图：`lifespan → langgraph_runtime → app.state.checkpointer → get_checkpointer(request) → aget_tuple/aput`
- 配对姊妹篇收尾句

## 贯穿全文的真实代码引用（均已核实行号）
- `backend/app/gateway/deps.py:366`（主角这一行）
- `backend/app/gateway/deps.py:351-361`（`_require` 工厂全文）
- `backend/app/gateway/deps.py:222-343`（`langgraph_runtime` lifespan，含 267 注册、256 多worker闸、334-343 排干）
- `backend/app/gateway/deps.py:49-80`（`_enforce_postgres_for_multi_worker`）
- `backend/app/gateway/deps.py:88-111`（`_drain_inflight_runs`，issue #3373）
- `backend/app/gateway/deps.py:364-369`（6 个兄弟 getter）
- `backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py:50-67, 90-123, 131-164, 167-202`（后端选择 + 连接池）
- `backend/app/gateway/routers/threads.py:836, 845-864`（下游消费：`checkpointer = get_checkpointer(request)` → `read_thread_goal`/`write_thread_goal` → `aget_tuple`/`aput`）
- `backend/app/gateway/deps.py:406-424`（`get_run_context` 聚合路径）
- issue #3373（PoolClosed 历史 bug）

## 重要说明：使用方式澄清（文档里作为一个 ⚠️ 讲清）
这些 getter 虽然有 FastAPI 依赖的形状（`Request -> T`）并被 docstring 称为 "FastAPI dependency"，但本仓库**实际用法是直接调用**（`checkpointer = get_checkpointer(request)`），而非 `Depends(get_checkpointer)`（全 `backend/app` 搜不到一处 `Depends(get_*)`）。这个反差点会明确讲清楚，避免读者误解。

## 不做的事
- 不修改任何现有文件（仅新增这一个 md）
- 不动 `run-manager-deep-dive.md` / `stream-bridge-deep-dive.md`
- 不写代码、不跑测试、不改配置

## 篇幅预期
约 900–1100 行，与 `run-manager-deep-dive.md`（1063 行）量级一致，保持"每个麻烦 ~120 行"的均匀节奏。