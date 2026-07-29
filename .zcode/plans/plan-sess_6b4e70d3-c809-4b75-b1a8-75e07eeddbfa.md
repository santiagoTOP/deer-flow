# 计划：新建 `langgraph-runtime-deep-dive.md`（装配总闸篇）

## 定位
在 `backend/docs/BACKEND_AGENT_DESIGN/` 下新建 `langgraph-runtime-deep-dive.md`，与 `stream-bridge-deep-dive.md` / `run-manager-deep-dive.md` / `get-checkpointer-deep-dive.md` 并列。

它**不是第四个并列的 getter 篇**，而是这三姊妹的**源头/总闸篇**：那三个 getter（`deps.py:364-366` 的 `get_stream_bridge`/`get_run_manager`/`get_checkpointer`）每个请求取出来的对象，**就是在 `langgraph_runtime`（`deps.py:222-343`）里被建出来、按特定顺序装配、在关机时按特定顺序回收的**。本篇回答：**为什么需要专门一个生命周期函数，而不是在 `app.py` 顶部随手 `checkpointer = AsyncPostgresSaver(...)` 就完事？**

## 已核实的事实基础（全部带行号，确保逐行讲解不出错）
- 函数本体：`deps.py:222-343`，唯一生产调用点 `app.py:240`（在 `lifespan` 内）。
- 5 个同文件 helper：`_enforce_postgres_for_multi_worker`(49)、`_drain_inflight_runs`(88)、`_publish_recovered_run_stream_end`(114)、`_log_recovered_stream_cleanup_result`(143)、`_mark_latest_recovered_threads_error`(162)；常量 `_RUN_DRAIN_TIMEOUT_SECONDS=5.0`(46)。
- 工厂性质：`make_stream_bridge` / `make_checkpointer` / `make_store` **都是 `@asynccontextmanager`**，故用 `stack.enter_async_context(...)`；`make_run_event_store` 是**普通同步工厂**（不持有需回收资源），故直接赋值、不入栈。
- `init_engine_from_config`(engine.py:174) 必须先于 `make_checkpointer` 的根因：postgres checkpointer 会 `async with pool:` 并 `saver.setup()` 跑 DDL，**都要求目标库已存在**；而"建库"逻辑（`_auto_create_postgres_db`）在 `init_engine` 里。
- `reconcile_orphaned_inflight_runs` 返回 `list[RunRecord]`；`start_heartbeat()` 在未启用时是 no-op；`shutdown(timeout=5.0)` 是有界 drain。
- 热重载边界 `reload_boundary.py:45-75`：`database/checkpointer/run_events/stream_bridge/run_ownership` 等均为 `startup-only`，其 reason 字面点名 `langgraph_runtime()` —— 正是本篇要讲的那个函数。
- `get-checkpointer-deep-dive.md` 结尾已有一张引用 `langgraph_runtime` 的全景图、`run-manager-deep-dive.md` 麻烦6 讲过 shutdown ordering —— 这些标 ※"姊妹篇讲过"，本篇只复用结论、不重复展开。

## 文档结构（严格沿用姊妹篇的"第 0 步术语 → 第 1 步朴素写法 → 第 2 步 6 个麻烦 → 第 3 步串起来+全景图"骨架，中文、逐行、打比方）

### 标题
`langgraph_runtime 深度讲解：为什么不能在 app.py 顶部随手构造运行时单例就完事`

### 开篇
点明它是三姊妹 getter 篇的**上游**：getter 每请求取的是"活物"，本篇讲这些活物**哪里来、何时建、按什么顺序装配、关机怎么收**。

### 第 0 步：前置术语（带 ※ 标记复用姊妹篇结论的）
- 0.1 `app.state`——进程级工具箱（※ 复用 get-checkpointer §0.3）
- 0.2 FastAPI `lifespan`——开机/关机总闸（※ 复用 get-checkpointer §0.4，补"langgraph_runtime 是被它套住的子总闸"）
- 0.3 `AsyncExitStack`——一摞"用完要按倒序还"的资源
- 0.4 `@asynccontextmanager` + `enter_async_context`——把"建好还要能关"的工厂登记进栈
- 0.5 "活资源" vs 纯数据结构——为什么有的进栈、有的不进栈（`make_run_event_store` 对比）
- 0.6 启动期配置快照（`startup_config`）vs 请求期热重载（`get_config`）——`startup-only` 边界（引 `reload_boundary.py`）
- 0.7 多 worker / `GATEWAY_WORKERS`（※ 复用 run-manager §0.8）

### 第 1 步：朴素写法长什么样
反面教材：在 `app.py` 模块顶层直接 `checkpointer = AsyncPostgresSaver(...)` / `run_manager = RunManager(...)`。逐行点出三个致命点：import 时跑异步会崩（无事件循环/无 await）、连接池随 import 打开且永不关、关机时无顺序可言。

### 第 2 步：6 个麻烦逐一展开（每个都落到 `langgraph_runtime` 的具体行）
1. **异步构造 + 连接池，import 时跑不了** → 整个生命周期必须是 `async def`、在 `lifespan` 里 `await`（对应把构造搬进 `langgraph_runtime` 而非 `app.py` 顶部）。
2. **多个活资源要"用完按倒序还"** → `async with AsyncExitStack() as stack:` + `enter_async_context(make_*)`；并讲清 `make_run_event_store` 为何**不**入栈（纯数据、无 `close`/连接池）。
3. **构造顺序：engine 必须先于 checkpointer** → `init_engine_from_config` 在前（postgres auto-create db），`make_checkpointer` 才能在"库已存在"前提下 `pool`+`setup()`；顺带讲 `sf is not None` 的 db vs memory 分支选择。
4. **进程重启后系统状态要自愈** → `reconcile_orphaned_inflight_runs`（孤儿 run 标 error）+ `_publish_recovered_run_stream_end`（给还等着的订阅者发 END）+ `_mark_latest_recovered_threads_error`（thread 层标 error）+ `start_heartbeat`（重启心跳）。点明这里复用 run-manager 篇的孤儿恢复结论。
5. **多 worker 下 SQLite 互相踩** → 函数第一行 `_enforce_postgres_for_multi_worker`，不满足直接 `SystemExit`（※ run-manager §0.8 结论，本篇讲它在装配链里的"位置为何在最早"）。
6. **优雅关机：run 不能随进程暴毙（issue #3373）** → `try: yield finally:` 里**先** `_drain_inflight_runs`（含 `asyncio.shield` 抗二次取消）**再** `close_engine()`，讲清"先排干 run、后关连接池"的顺序为何是 #3373 的修复要点（※ run-manager §麻烦6 已讲 shutdown 内部，本篇聚焦它在 `langgraph_runtime` 里的调用点与相对 `close_engine` 的次序）。

### 第 3 步：串起来 + 全景图
- 一张图：`lifespan(app.py:171)` → `async with langgraph_runtime(app, startup_config)`（deps.py:222）→ 内部按序建出 `stream_bridge/checkpointer/store/run_store/.../run_manager` 并挂 `app.state` → `yield`（开始接请求）→ `finally` 先 drain 后 close → 每个请求经 `deps.py:364-366` 三个 getter 取出。
- 收束句：本篇是三姊妹的"装配/生命周期"上游；getter 是"取"，本篇是"建与还"。

## 风格要求（严格对齐姊妹篇）
- 全中文、面向第一次接触的同学；每段代码逐行解释；大量用餐厅/工具箱/存档机等比方。
- 所有代码块带真实文件:行号；反面教材明确标注"反面教材，真实代码不是这样"。
- 复用姊妹篇结论处用 ※ 标注"姊妹篇讲过，本篇只复用"，避免重复展开。

## 不做的事
- 不改任何源码（仅新增一个 .md 文档）。
- 不动三篇姊妹篇正文（它们已正确引用 `langgraph_runtime`）。
- 不提交 git（除非你要求）。

## 验证
写完后通读一遍核对：①每一处引用的 `deps.py` 行号与现网一致；②6 个麻烦与代码行的对应关系无遗漏；③全景图与 `get-checkpointer-deep-dive.md` 结尾那张图互洽、不矛盾。