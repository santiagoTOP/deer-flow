# StreamBridge 深度讲解：为什么不能直接把 `agent.astream()` 接到 `StreamingResponse`

本文面向第一次接触这套流式架构的同学。先把所有需要的前置概念一个一个铺出来，然后再讲 6 个麻烦，每段代码逐行解释。

---

## 第 0 步：先把要用的术语讲清楚

在讲任何代码之前，先把后面会反复出现的概念讲明白。这些是理解"为什么朴素写法会出问题"的地基。

### 0.1 HTTP 请求/响应是一条"电话通话"

把 HTTP 想象成一次电话通话：

- **客户端**（浏览器、手机 App）拨号过来 = 发 HTTP 请求
- **服务器**接电话 = FastAPI 路由函数被调用
- 通话挂断 = HTTP 响应发完，或者连接断开

普通 HTTP 是"问一句答一句"：客户端问"今天天气？"，服务器立刻答"晴，25度"，通话结束。

但聊天场景不一样：你发一句"帮我写个排序算法"，agent 要**一边想一边往外吐字**，可能吐 30 秒。如果用普通 HTTP，服务器要么憋 30 秒一次性返回（用户看到 30 秒空白），要么每秒轮询一次（浪费流量）。

所以聊天用一种特殊模式：**SSE（Server-Sent Events）**。

### 0.2 SSE（Server-Sent Events）——服务器单向往客户端推消息

SSE 是 HTTP 的一种用法，本质就是：**HTTP 响应不结束，服务器一直往里写数据**。每个数据块叫一个"事件"，长这样：

```
id: 1690000000-1
event: messages
data: {"content": "你"}

id: 1690000000-2
event: messages
data: {"content": "好"}

```

格式说明：
- `id:` 这一行是这条事件的编号，客户端会记住它
- `event:` 是事件类型（比如"有新 token"、"有 tool 调用"）
- `data:` 是这条事件携带的数据（一般是 JSON）
- 每个事件之间用空行隔开

客户端的 `EventSource` API 或 LangGraph 的 `useStream` hook 会**持续监听**这个响应，每收到一个事件就处理一个。对用户来说就是看到字一个一个蹦出来。

**关键点**：这条 HTTP 连接是**长连接**，一直开着，直到服务器主动写完"结束事件"或者连接断开。

### 0.3 Python 的 `async` / `await`——能"暂停"的函数

普通函数（同步）从头跑到尾，中间谁叫它停它都不停。`async def` 定义的函数叫**协程**（coroutine），它可以在某些点"暂停"，把 CPU 让给别人。

```python
async def foo():
    result = await some_slow_operation()   # 在这里暂停，等操作完成
    return result
```

- `async def` 声明一个协程函数
- `await xxx` 的意思是"等 xxx 完成，期间我先歇着，让别的协程跑"

为什么需要这个？因为一个 Python 服务器要同时处理几百个用户。如果某个用户在等数据库返回（要 100 毫秒），同步函数会让 CPU 干等 100 毫秒，其他用户全卡住；async 协程会在 `await` 点**主动让出 CPU**，让别的协程用这 100 毫秒。

### 0.4 `async for` + `yield`——异步生成器（async generator）

这是 SSE 流式响应的核心机制。先看普通生成器：

```python
def counter():
    yield 1
    yield 2
    yield 3

for x in counter():
    print(x)   # 依次打印 1, 2, 3
```

`yield` 的意思是"先吐一个值出来，函数暂停在这里；调用者来要下一个值时，再从暂停点继续"。生成器是一种**懒序列**——你不取，它就不往下算。

异步生成器就是 async 版本：

```python
async def counter():
    yield 1
    await asyncio.sleep(1)   # 暂停 1 秒
    yield 2
```

`async for` 来消费它：

```python
async for x in counter():
    print(x)   # 先打 1，等 1 秒后打 2
```

**SSE 响应就是用异步生成器实现的**：Starlette 的 `StreamingResponse` 接受一个异步生成器，每 `yield` 一个字符串就往 HTTP 响应里写一段。所以"边算边吐"靠的就是 `yield`。

### 0.5 `CancelledError`——协程被强行打断

当一个协程在 `await` 点暂停时，调用者（更准确说是事件循环）可以**取消它**。取消的方式是从协程内部抛出一个叫 `CancelledError` 的异常。

打个比方：你正在做饭（一个协程），切到一半（await 点），室友突然喊"别做了我们点外卖"，于是一个 `CancelledError` 异常从你切菜的地方炸出来，你的做饭流程被中断——但**菜板上的菜还切了一半**，没收拾。

这正是 DeerFlow 踩过的坑：agent 正在写 checkpoint（保存对话状态），`CancelledError` 来了，写了一半的状态留在硬盘上，后面读出来当正常状态用。

### 0.6 `asyncio.Condition`——"通知"机制

`asyncio.Condition` 是 Python 异步里的一个"通知铃"。多个协程可以**等**这个铃（`await condition.wait()`），另一个协程按铃（`condition.notify_all()`），所有等的协程就被叫醒。

打个比方：餐厅等位。一群客人坐下等（`wait()`），前台叫号（`notify_all()`），所有等位的客人都被叫醒去看是不是轮到自己。

StreamBridge 用这个机制实现"事件来了，叫醒所有订阅者"。

### 0.7 checkpoint——agent 的"存档"

游戏里打 boss 前先存档，死了从存档点重来。agent 也一样：每执行一步（一个图节点），就把当前状态（消息列表、变量、工具调用结果）写到 checkpoint 里。这样：

- 进程崩了重启，能从最近的 checkpoint 继续
- 用户点"重新生成"，能回到某个 checkpoint 重跑

所以 agent 执行过程中**会持续写 checkpoint**——这是个有副作用的操作，不能被打断到一半。

### 0.8 worker / 进程——agent 在哪儿跑

一个 Python 进程是一个独立运行的程序实例。服务器可能跑多个进程（多 worker）来分摊负载——就像一家餐厅有多个厨师，每个厨师在自己的厨房（进程）里干活。

进程之间内存是隔离的：厨师 A 锅里的菜，厨师 B 看不到。要共享信息得通过外部渠道（Redis、数据库、消息队列）。

### 0.9 lease——"我正在处理这个 run"的租约

"租约"是一种分布式系统里的常见机制。想象 run 是一辆共享单车，谁要处理它先**扫码租走**（拿到 lease），别人就不能动了；处理完释放租约。如果租的人摔了（进程崩了），租约到期后别人能接手。

DeerFlow 的 run 有 lease 概念，是为了避免两个 worker 同时跑同一个 run。

---

好，前置概念讲完了。现在我们开始讲代码。

## 第 1 步：朴素写法长什么样

```python
async def stream_run_naive(thread_id, body, request):
    agent = build_agent(...)
    async def gen():
        async for chunk in agent.astream(...):
            yield format_sse(...)
    return StreamingResponse(gen(), media_type="text/event-stream")
```

逐行解释：

- **第 1 行** `async def stream_run_naive(thread_id, body, request):`
  定义一个异步函数（协程），它就是 FastAPI 的路由处理函数。三个参数：
  - `thread_id`：会话 ID（哪段对话）
  - `body`：请求体（用户发的消息）
  - `request`：FastAPI 给的请求对象，能查请求头、检测连接状态等

- **第 2 行** `agent = build_agent(...)`
  构造一个 agent 对象。可以把它想象成一个"能聊天、能调用工具、能思考"的机器人实例。

- **第 3 行** `async def gen():`
  在函数里面再定义一个异步函数 `gen`。注意这个 `gen` 是个**异步生成器**（因为它里面有 `yield`），它负责生产 SSE 数据流。

- **第 4 行** `async for chunk in agent.astream(...):`
  `agent.astream(...)` 返回一个异步迭代器，每产出一个 `chunk`（一个数据块，比如"模型刚吐了几个字"或"工具调用完成"），就执行一次循环体。`async for` 表示"异步地循环"——每次等下一个 chunk 来，期间让出 CPU。

- **第 5 行** `yield format_sse(...)`
  把这个 chunk 格式化成 SSE 文本（就是那种 `id: ... \nevent: ... \ndata: ... \n\n` 的格式），然后 `yield` 出去。`yield` 暂停 `gen`，把这段文本交给 `StreamingResponse`，后者把它写进 HTTP 响应。

- **第 6 行** `return StreamingResponse(gen(), media_type="text/event-stream")`
  返回一个 Starlette 的 `StreamingResponse` 对象。它的第一个参数是异步生成器 `gen()`（注意有括号，是调用，返回生成器对象），第二个参数说明这是 SSE 流。Starlette 会持续从生成器取值，每取到一个就往 HTTP 响应里写，直到生成器结束。

**这 6 行代码的根本问题**：`gen`、`agent.astream`、`StreamingResponse` 是**绑在同一个调用链**上的。换句话说，agent 的执行和 HTTP 响应是**同一条命**——HTTP 一断，agent 也跟着死。下面 6 个麻烦全是从这一条派生的。

---

## 第 2 步：6 个麻烦逐一展开

### 麻烦 1：客户端断线，agent 被 `CancelledError` 杀掉

#### 场景

用户在地铁里用 DeerFlow 聊天，agent 正在跑（比如正在调用某个工具装包），网络信号一差，HTTP 连接断了。你希望发生什么？

**理想**：agent 该跑还跑完（用户重连还能看到结果），或者按策略显式停下来并清理状态。

**朴素写法的实际**：协程链被取消，`CancelledError` 从 `astream` 内部冒出来，agent 正在做的事被硬生生打断，可能留下半成品。

#### 朴素写法为什么会这样

回到第 1 步的代码。客户端断开连接时，Starlette 会做一件事：**取消 `gen` 这个协程**。取消的方式是在 `gen` 当前暂停的地方（也就是 `async for chunk in agent.astream(...)` 这一行）注入一个 `CancelledError` 异常。

但 `agent.astream(...)` 不是孤立的——它内部正在执行 agent 的图，跑各种节点，写 checkpoint。`CancelledError` 会一路冒泡进去，破坏内部状态。

历史 bug（issue #3265，注释在 `backend/app/gateway/services.py:880-887`）：

> The non-streaming `/wait` endpoints used to `await record.task` directly with no disconnect handling. When the client (or an intermediate HTTP proxy) timed out during a long tool call such as `pip install`, the handler would swallow `CancelledError` and serialize whatever checkpoint happened to exist — masking a half-finished run as a normal completion.

翻译：以前的 `/wait` 端点直接 `await` agent task，客户端超时（比如 `pip install` 跑很久），handler 把 `CancelledError` 吞掉，然后把当时碰巧存在的 checkpoint 当成最终结果序列化返回——**一个跑了一半的 run 被伪装成"正常完成"返回给用户**。用户看到的是个错的、半截的结果，但以为是对的。

#### Bridge 怎么解决的

先把代码再贴一遍，逐行解释：

```python
async def sse_consumer(bridge, record, request, run_mgr):
    last_event_id = request.headers.get("Last-Event-ID")
    if await _terminal_record_stream_missing(bridge, record):
        yield format_sse("end", None)
        return

    try:
        async for entry in bridge.subscribe(record.run_id, last_event_id=last_event_id):
            if await request.is_disconnected():
                break
            if entry is HEARTBEAT_SENTINEL:
                if await _terminal_record_stream_missing(bridge, record):
                    yield format_sse("end", None)
                    return
                yield ": heartbeat\n\n"
                continue
            if entry is END_SENTINEL:
                yield format_sse("end", None, event_id=entry.id or None)
                return
            yield format_sse(entry.event, entry.data, event_id=entry.id or None)
    finally:
        if not record.store_only and record.status in (RunStatus.pending, RunStatus.running):
            if record.on_disconnect == DisconnectMode.cancel:
                await run_mgr.cancel(record.run_id)
```

逐行：

- **第 1 行** `async def sse_consumer(bridge, record, request, run_mgr):`
  定义一个异步生成器（注意函数体里有 `yield`，所以是生成器）。参数：
  - `bridge`：StreamBridge 实例，事件总线
  - `record`：这次 run 的记录对象（含 run_id、状态等）
  - `request`：HTTP 请求对象
  - `run_mgr`：run 管理器，能取消 run

- **第 2 行** `last_event_id = request.headers.get("Last-Event-ID")`
  从 HTTP 请求头里取 `Last-Event-ID`。这是 SSE 协议规定的字段——客户端重连时会带上它，告诉服务器"我上次收到的最后一条事件 id 是这个，从下一条开始给我"。如果是首次连接，这个值是 `None`。

- **第 3-4 行** `if await _terminal_record_stream_missing(bridge, record): yield format_sse("end", None); return`
  检查这个 run 是不是已经结束了（terminal = 终态，比如 success/error/cancelled）**并且** bridge 里没存事件了。如果是，直接发个 `end` 事件然后 return——没必要订阅一个早就结束的 run。

- **第 6 行** `try:` 开始一个 try 块。try-finally 是 Python 的异常处理结构：try 里的代码无论正常结束还是出错，finally 里的代码都会执行。这里用 try-finally 是为了**无论怎么退出循环，最后都走断线清理逻辑**。

- **第 7 行** `async for entry in bridge.subscribe(record.run_id, last_event_id=last_event_id):`
  调用 bridge 的 `subscribe` 方法，订阅这个 run 的事件流。它返回一个异步迭代器，每次产出一条 `entry`（一个事件对象）。`last_event_id` 透传进去，让 bridge 知道从哪里开始给。

- **第 8-9 行** `if await request.is_disconnected(): break`
  **这是关键的一行**。每收到一条事件前，先主动问一句："客户端是不是已经断开了？" 如果是，跳出循环。注意这是**主动检查**，不是被动接 `CancelledError`——区别巨大，下面会展开。

- **第 10-13 行** 心跳处理（先跳过，麻烦 5 里细讲）

- **第 14-16 行** `if entry is END_SENTINEL: yield format_sse("end", None, ...); return`
  如果收到的是"结束标记"（agent 已经跑完了），给客户端发个 `end` 事件，然后 return（结束整个生成器）。

- **第 17 行** `yield format_sse(entry.event, entry.data, event_id=entry.id or None)`
  正常事件，格式化成 SSE 文本吐出去。

- **第 19-21 行** finally 块，断线清理：
  - `if not record.store_only and record.status in (RunStatus.pending, RunStatus.running):`
    只有当这个 run 是这个 worker 真正在跑的（不是从别处恢复的 `store_only` 记录），并且状态还是 pending/running，才有意义去取消。
  - `if record.on_disconnect == DisconnectMode.cancel:`
    用户配置了"断线时取消"的策略。
  - `await run_mgr.cancel(record.run_id)`
    **显式调用** run 管理器的 cancel 方法。这是关键——它走的是 run 的状态机正规取消流程（释放 lease、迁移状态、清理资源），不是粗暴的 `CancelledError`。

#### 为什么"主动检查 + 显式 cancel"比"被动接异常"好

被动接 `CancelledError`（朴素写法）相当于：你在做饭，室友直接把电源拔了——锅里的菜半生不熟，灶台一片狼藉，没人收拾。

主动检查 + 显式 cancel（Bridge 写法）相当于：你做完当前一道菜，主动问"还要继续吗？"，听到"不用了"就**关火、洗碗、归位调料**——厨房干干净净。

代码上具体差别：
- 朴素写法：`CancelledError` 在任意 `await` 点冒泡，**不可预测**会在哪一行中断，连 try-finally 都未必能干净地清理（因为 finally 里如果再 await 还会再次被取消）。
- Bridge 写法：循环每次迭代都在**可控的位置**（`async for` 之间）检查断开状态，cancel 是**显式的方法调用**，run 管理器内部能按部就班走完取消流程。

---

### 麻烦 2：断线重连（`Last-Event-ID`）

#### 场景

agent 跑 30 秒，第 10 秒用户网络抖了一下，HTTP 断开。第 12 秒网络恢复，客户端重连。这时：

- agent 已经产出了 50 条事件（比如前 50 个 token）
- 客户端在断线前收到了第 40 条
- 客户端带着 `Last-Event-ID: {ts}-40` 重连

期望：从第 41 条继续给客户端，**不漏不重**。

#### 朴素写法为什么做不到

朴素 generator 是**一次性的、无记忆的**。`agent.astream()` 吐出来的 chunk，`yield` 完就没了——generator 内部不留底。所以重连时：

- 没法从第 41 条继续——前 40 条早被消费掉了，generator 不知道
- 重跑 agent？不行——LLM 调用要钱、有副作用、每次结果不一样（温度采样）
- 从头重放？客户端会看到前 40 条 token 重复出现——UI 错乱

#### Bridge 怎么解决的：每个 run 一个事件日志

代码在 `backend/packages/harness/deerflow/runtime/stream_bridge/memory.py`。先看数据结构：

```python
@dataclass
class _RunStream:
    events: list[StreamEvent] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    ended: bool = False
    start_offset: int = 0
```

逐行：

- `@dataclass` 这是个装饰器，告诉 Python"这是一个数据类"，自动生成构造函数等样板代码。
- `class _RunStream:` 每个 run 内部对应一个 `_RunStream` 对象。
- `events: list[StreamEvent] = field(default_factory=list)` 一个列表，存这个 run 的所有事件。`field(default_factory=list)` 的意思是"每个 `_RunStream` 实例都有自己的一个新空列表"（不能用 `[]`，否则所有实例会共享同一个列表，这是个 Python 陷阱）。
- `condition: asyncio.Condition = field(default_factory=asyncio.Condition)` 一个"通知铃"，用于麻烦 3（多订阅者）。每个实例一个新 Condition。
- `ended: bool = False` 这个 run 是不是已经结束了（agent 调过 `publish_end`）。
- `start_offset: int = 0` 起始偏移量。这个字段是为"滑动窗口"服务的（麻烦 6）：当旧事件被淘汰时，`start_offset` 往前走，配合算术定位。

再看事件 ID 怎么生成（`memory.py:45-49`）：

```python
def _next_id(self, run_id: str) -> str:
    self._counters[run_id] = self._counters.get(run_id, 0) + 1
    ts = int(time.time() * 1000)
    seq = self._counters[run_id] - 1
    return f"{ts}-{seq}"
```

逐行：

- `self._counters[run_id] = self._counters.get(run_id, 0) + 1`
  每个 run 有自己的计数器。`get(run_id, 0)` 是"取计数器，如果没有就当 0"，然后 +1。第一次调用：0+1=1，第二次：1+1=2，依此类推。所以 `_counters[run_id]` 是"这是第几次 publish"。
- `ts = int(time.time() * 1000)`
  当前时间的毫秒数（`time.time()` 是秒，乘 1000 转毫秒，`int` 取整）。这是给 ID 加个时间戳前缀，让它看起来更"全局唯一"（多个 run 之间不会撞 ID）。
- `seq = self._counters[run_id] - 1`
  序号 = 当前 publish 次数 - 1。第一次 publish：seq=0；第二次：seq=1。**seq 从 0 开始，每次 +1**。
- `return f"{ts}-{seq}"`
  返回字符串，形如 `1690000000123-0`、`1690000000124-1`。中间用 `-` 连接。

**关键设计**：`seq` 是从 0 开始每次 +1 的连续整数，正好等于这个事件在 `events` 列表里的**绝对偏移量**。这让重连定位变成 O(1) 的算术题。

重连定位代码（`memory.py:67-87`）：

```python
def _resolve_start_offset(self, stream, last_event_id):
    if last_event_id is None:
        return stream.start_offset
    seq = self._parse_event_seq(last_event_id)
    if seq is not None:
        local_index = seq - stream.start_offset
        if 0 <= local_index < len(stream.events) and stream.events[local_index].id == last_event_id:
            return stream.start_offset + local_index + 1
    if stream.events:
        logger.warning("last_event_id=%s not found ... replaying from earliest retained event", last_event_id)
    return stream.start_offset
```

逐行：

- `def _resolve_start_offset(self, stream, last_event_id):`
  给定客户端传来的 `last_event_id`，算出"应该从哪个位置开始给事件"。返回一个数字（绝对偏移量）。
- `if last_event_id is None: return stream.start_offset`
  首次连接（没传 Last-Event-ID），从最早的留存事件开始。
- `seq = self._parse_event_seq(last_event_id)`
  从 `"{ts}-{seq}"` 字符串里提取出 seq 数字（下面有这个函数的代码）。比如 `"1690000000123-40"` → 40。
- `if seq is not None:`
  提取成功（字符串格式对）才进入这个分支。
- `local_index = seq - stream.start_offset`
  把绝对 seq 换算成"在当前 `events` 列表里的下标"。比如 seq=40，但 `start_offset=10`（前面 10 条被淘汰了），那 `local_index=30`。
- `if 0 <= local_index < len(stream.events) and stream.events[local_index].id == last_event_id:`
  **双重校验**：①下标合法（在列表范围内）；②列表那个位置的事件 id 确实就是 `last_event_id`（防止伪造或串台的 id 误命中）。两个条件都满足，才算定位成功。
- `return stream.start_offset + local_index + 1`
  返回"下一条要给的事件的绝对偏移量"。`+1` 是因为要从"上次收到的"的下一条开始。
- 后面三行：定位失败（id 找不到），日志告警，然后从最早的留存事件开始回放（兜底，宁可重复也不能漏）。

`_parse_event_seq` 函数（`memory.py:51-65`）：

```python
@staticmethod
def _parse_event_seq(event_id: str) -> int | None:
    _, sep, seq_text = event_id.rpartition("-")
    if not sep:
        return None
    try:
        return int(seq_text)
    except ValueError:
        return None
```

逐行：

- `@staticmethod` 装饰器，表示这是个静态方法（不依赖 self）。
- `_, sep, seq_text = event_id.rpartition("-")`
  `rpartition("-")` 从右往左找 `-`，把字符串切成三段：前部分、分隔符、后部分。比如 `"1690000000123-40"` → `("1690000000123", "-", "40")`。`_` 是 Python 惯例，表示"这部分我不用，扔了"。`sep` 是分隔符（`"-"` 或空字符串）。`seq_text` 是后部分（`"40"`）。
  为什么用 `rpartition` 而不是 `partition`？因为时间戳里万一有 `-`（其实不会，但稳健起见），从右切更安全。
- `if not sep: return None`
  如果没找到 `-`（sep 是空字符串），返回 None 表示解析失败。
- `try: return int(seq_text)`
  尝试把 `"40"` 转成整数 40。
- `except ValueError: return None`
  转换失败（比如 seq_text 是 `"abc"`），返回 None。

**这套设计的好处**：重连定位是 O(1)——直接算术算出下标，不用遍历整个事件列表。如果事件有几千条，遍历是 O(n)，算术是 O(1)。

---

### 麻烦 3：多个订阅者看同一个 run（fan-out）

#### 场景

agent 正在跑，**多个客户端想同时看**：

- 用户在浏览器标签页 A 发了消息，agent 开跑，A 在看流
- 用户又开了标签页 B 看同一线程——B 要 join 已经在跑的 run
- 一个流式 IM 通道（飞书 / Telegram / 企业微信）也触发了同一个 run，它也要看事件流
- 客户端网络抖断，带 `Last-Event-ID` 重连——相当于"第二个订阅者"加入

这些场景在 DeerFlow 里真实存在。下面把"谁在订阅、在哪订阅、怎么订阅"一次讲清楚。

#### 3.1 先看清全貌：一个 run 到底有几条订阅链路

先记住一个关键事实：**所有 HTTP 订阅最终都汇聚到两个 helper 函数**，它们是 `bridge.subscribe(...)` 的唯二调用点（在 `backend/app/gateway/services.py`）：

```python
async def sse_consumer(bridge, record, request, run_mgr):   # services.py:828
    ...
    async for entry in bridge.subscribe(record.run_id, last_event_id=last_event_id):  # services.py:846
        ...  # 把 entry 格式化成 SSE 文本 yield 出去

async def wait_for_run_completion(bridge, record, request, run_mgr):   # services.py:874
    ...
    async for entry in bridge.subscribe(record.run_id, last_event_id=last_event_id):  # services.py:908
        ...  # 不往 HTTP 吐 SSE，只是"阻塞到 run 结束"
```

区别只有一点：

- `sse_consumer`：**流式**——每来一个 entry 就 `yield` 一段 SSE 文本给浏览器（字一个一个蹦）。
- `wait_for_run_completion`：**阻塞**——把流"喝干"直到 `END_SENTINEL`，用于 `/runs/wait` 这种"跑完一次性返回最终状态"的端点。它内部消费同一个 `subscribe`，但不往外吐事件。

所以"谁在订阅"这个问题，等价于"谁调用了这两个 helper"。下面把所有调用点列全。

#### 3.2 所有订阅入口（端点）一张表

`bridge.subscribe` 不直接暴露给外部，外部通过 HTTP 端点间接触发。每个端点进来 → 拿到 `bridge` → 调 `sse_consumer` 或 `wait_for_run_completion` → 内部 `bridge.subscribe`。下面是全部入口（`thread_runs.py` 和 `runs.py`）：

| 端点 | 文件:行 | 触发场景 | 调的 helper |
|---|---|---|---|
| `POST /{tid}/runs/stream` | `thread_runs.py:496` | **创建 run 并流式看**（浏览器发消息的主路径） | `sse_consumer` (:510) |
| `POST /{tid}/runs/wait` | `thread_runs.py:524` | 创建 run，阻塞等它跑完，返回最终状态 | `wait_for_run_completion` (:534) |
| `GET /{tid}/runs/{rid}/join` | `thread_runs.py:618` | **第二个标签页 join 一个已经在跑的 run** | `sse_consumer` (:631) |
| `GET\|POST /{tid}/runs/{rid}/stream` | `thread_runs.py:645` | 重新挂回一个 run（重连 / SDK 的 stop 按钮） | `sse_consumer` (:691) |
| `POST /runs/stream`（无状态） | `runs.py:44` | 无 thread 的单次流式 run | `sse_consumer` |
| `POST /runs/wait`（无状态） | `runs.py:69` | 无 thread 的单次阻塞 run | `wait_for_run_completion` |

注意一个关键点：**每个 HTTP 请求进来，都会新开一个 `bridge.subscribe(...)` 迭代器**。两个标签页看同一个 run = 两次 HTTP = 两个独立的 `subscribe` 迭代器，但它们读的是**同一个** `_RunStream`（memory）或**同一个 Redis key**（redis）。这就是 fan-out 的物理基础。

举个最典型的 fan-out 场景，看 `join_run` 端点（`thread_runs.py:618-638`）：

```python
@router.get("/{thread_id}/runs/{run_id}/join")
@require_permission("runs", "read", owner_check=True)
async def join_run(thread_id, run_id, request):
    run_mgr = get_run_manager(request)
    record = await run_mgr.get(run_id)              # 找到那个正在跑的 run
    if record is None or record.thread_id != thread_id:
        raise HTTPException(status_code=404, ...)
    bridge = get_stream_bridge(request)
    if record.store_only and not bridge.supports_cross_process:   # 麻烦 4 会讲这个守卫
        raise HTTPException(status_code=409, "Run ... is not active on this worker ...")

    return StreamingResponse(
        sse_consumer(bridge, record, request, run_mgr),   # 第 N 个订阅者，从这里加入
        media_type="text/event-stream", ...)
```

逐行：

- `record = await run_mgr.get(run_id)`——根据 run_id 拿到这个 run 的记录。**注意它不需要 run 是"我创建的"**，任何知道 run_id 的合法请求都能 join。这就是"多订阅者"能成立的前提。
- `if record.store_only and not bridge.supports_cross_process:`——跨进程守卫，麻烦 4 展开。这里先理解为"内存模式下，run 不在这个 worker 上就拒绝"。
- `sse_consumer(bridge, record, request, run_mgr)`——开一个新的订阅。里面会调 `bridge.subscribe(record.run_id, ...)`，加入这个 run 的事件流。

#### 3.3 IM 通道是不是订阅者？（容易搞混的点）

很多人（包括这篇文档的旧版）会以为"飞书触发的 run，飞书内部直接订阅 bridge"。**这是错的**，务必厘清：

- IM 通道（`backend/app/channels/feishu.py` 等）**从不导入 `StreamBridge`，也从不调用 `bridge.subscribe`**。它们和 bridge 之间没有任何直接联系。
- IM 通道走的是**普通 HTTP 路径**：通道管理器（`app/channels/manager.py:1081`）用 `langgraph_sdk` 的 HTTP 客户端，向 Gateway 自己的 `POST /api/threads/{tid}/runs/stream` 发请求。

看飞书这类**流式通道**（`manager.py:1744`）：

```python
async for chunk in client.runs.stream(thread_id, assistant_id, **stream_kwargs):
    ...
```

这一行 `client.runs.stream(...)` 本质就是一次 HTTP POST 到 `/runs/stream`。它在**服务端**会命中上面表里的第一个端点 → 调 `sse_consumer` → 调 `bridge.subscribe`。

**所以 IM 通道是订阅者，但是"远程的、经 HTTP 进来的"订阅者**，和浏览器标签页没有本质区别。只有飞书 / Telegram / 企业微信 这三种流式通道会这么干（`manager.py:87-95` 的 `CHANNEL_CAPABILITIES` 里 `supports_streaming: True`）；Slack/Discord/钉钉 用 `runs.wait`（阻塞等结果），GitHub 用 `runs.create`（发完就走，根本不看流）。

> ⚠️ 注意：通道里那些 `bus.subscribe_outbound(...)` 调用是**另一个东西**——`MessageBus`（进程内的消息总线），和 `StreamBridge` 完全不是一回事，别因为名字里有 subscribe 就搞混。

#### 3.4 调度器（scheduler）是不是订阅者？

**不是**。调度器（`backend/app/scheduler/service.py`）从不订阅 bridge 的事件流。它关心的是"run 跑完了没"，而这件事用的是**完成回调钩子**，不是订阅：

- agent worker 跑完时（`worker.py:710-712` 的 `finally` 块）会调用 `ctx.on_run_completed(record)`。
- 这个钩子在 Gateway 启动时被接到调度器的 `handle_run_completion` 上（`deps.py:423`）。
- 调度器收到回调，更新任务表，**全程没碰 bridge**。

所以 fan-out 的订阅者只有两类：**浏览器/客户端**（通过 `/runs/stream`、`/runs/{id}/join`、`/runs/{id}/stream`）和**流式 IM 通道**（本质上也是上一类的 HTTP 客户端）。

#### 3.5 朴素写法为什么做不到

朴素 generator 是 **1:1 管道**：一个生产者（`agent.astream()`）配一个消费者（HTTP 响应）。第二个消费者来 join 时：

- generator 已经被第一个消费者迭代掉了——没法"再生成一遍"（generator 是一次性的）。
- 重跑 agent？不行——LLM 调用要钱、有副作用、每次结果不一样（温度采样）。
- 把 chunk 复制一份给第二个消费者？朴素写法里 agent 和 HTTP 响应是**同一条命**（见第 1 步），根本没有"中间层"可以做复制。

#### 3.6 Bridge 怎么解决的：每个 run 一份共享事件日志 + Condition 广播

核心思路：把"agent 产出事件"和"N 个客户端消费事件"中间塞一个**每个 run 一份的事件日志**（`_RunStream`）。agent 往里写一次，N 个订阅者各自从自己的进度读——读的是同一份数据，互不影响。

**生产端**——`publish`（`memory.py:101-111`）：

```python
async def publish(self, run_id, event, data) -> None:
    stream = self._get_or_create_stream(run_id)                 # 每个 run 一份
    entry = StreamEvent(id=self._next_id(run_id), event=event, data=data)
    async with stream.condition:                                # 加锁
        stream.events.append(entry)                             # 写一次
        if len(stream.events) > self._maxsize:                  # 滑动窗口（麻烦 6）
            overflow = len(stream.events) - self._maxsize
            del stream.events[:overflow]
            stream.start_offset += overflow
        stream.condition.notify_all()                           # ← fan-out 的核心：叫醒所有订阅者
```

逐行：

- `stream = self._get_or_create_stream(run_id)`：按 run_id 拿到（或新建）那个 run 的 `_RunStream`。**同一个 run_id 永远拿到同一个 `_RunStream` 对象**——这是"共享"的物理基础。
- `entry = StreamEvent(...)`：构造一条事件（id 由麻烦 2 讲的 `_next_id` 生成）。
- `async with stream.condition:`：加锁。`Condition` 本身是一把锁，`async with` 进入时拿锁、退出时放锁。从这行到 `notify_all` 是临界区，同一时刻只有一个协程能进来。锁是为了保证"写 + 通知"是个原子动作，不会出现"事件写一半、订阅者读到半截"的情况。
- `stream.events.append(entry)`：把事件追加到列表末尾。**只 append 一次**，不管有几个订阅者——事件在内存里只有一份。
- `if len(stream.events) > self._maxsize:`：滑动窗口淘汰（麻烦 6 细讲）。
- `stream.condition.notify_all()`：**按铃，叫醒所有在这个 condition 上 `wait` 的订阅者协程**。这是 fan-out 的扳机：一次 publish，所有订阅者全被唤醒。

**消费端**——`subscribe`（`memory.py:120-161`）：

```python
async def subscribe(self, run_id, *, last_event_id=None, heartbeat_interval=15.0):
    stream = self._get_or_create_stream(run_id)                 # 和 publish 读的是同一个对象
    async with stream.condition:
        next_offset = self._resolve_start_offset(stream, last_event_id)  # 各算各的起点

    while True:
        async with stream.condition:
            if next_offset < stream.start_offset:               # 我落后太多，被淘汰了
                next_offset = stream.start_offset
            local_index = next_offset - stream.start_offset
            if 0 <= local_index < len(stream.events):           # 有新事件
                entry = stream.events[local_index]
                next_offset += 1                                # 我自己往前走一格
            elif stream.ended:                                  # 没新事件但 run 结束了
                entry = END_SENTINEL
            else:                                               # 没新事件，等铃
                try:
                    await asyncio.wait_for(stream.condition.wait(), timeout=heartbeat_interval)
                except TimeoutError:
                    entry = HEARTBEAT_SENTINEL
                else:
                    continue
        if entry is END_SENTINEL:
            yield END_SENTINEL
            return
        yield entry
```

逐行讲关键的几个：

- `stream = self._get_or_create_stream(run_id)`：**和 publish 拿到的是同一个 `_RunStream` 对象**。这是"共享"的另一面——生产者和所有消费者都指向同一块数据。
- `next_offset = self._resolve_start_offset(stream, last_event_id)`：每个订阅者**各自**算自己的起点（麻烦 2 讲过）。新来的从 0 开始，重连的从 `Last-Event-ID` 之后开始——**互不影响**。
- `async with stream.condition:`（循环里）：每轮迭代加锁。注意锁是"进入临界区 → 读一个事件 → 退出临界区"，所以多个订阅者能**交错**进入（一个在 `wait` 时让出锁，另一个能进来读）。
- `local_index = next_offset - stream.start_offset`：把"我自己的绝对进度"换算成"列表下标"。
- `if 0 <= local_index < len(stream.events):`：有我还没读的事件，拿出来，`next_offset += 1`（**我的进度前进一格，不影响别的订阅者的 next_offset**）。
- `elif stream.ended:`：列表里没新事件，但 run 已经结束 → 给结束标记。
- `else: await asyncio.wait_for(stream.condition.wait(), ...)`：没新事件也没结束 → **等铃响**（`wait` 会释放锁、暂停自己；被 `notify_all` 叫醒后重新拿锁）。外面包一层超时就是心跳（麻烦 5）。

#### 3.7 fan-out 到底是怎么发生的（把流程走一遍）

把上面拼起来，看两个订阅者同时看一个 run 时，一次 `publish` 的完整流程：

```
时刻 T0：run R 正在跑，订阅者 A 和 B 都在 subscribe 的 wait() 上睡着（各自持 next_offset=10）
         events 列表长度 = 10（e0..e9）

时刻 T1：agent worker 调 bridge.publish(R, "messages", {"content":"你"})
   publish 内部：
     1. async with condition:   拿到锁
     2. events.append(e10)      列表变成 11 条
     3. notify_all()            ← 按铃
     4. 退出 async with         释放锁
        此时 A 和 B 的 wait() 被同时唤醒（这就是"广播"）

时刻 T2：A 被唤醒，重新拿锁
     local_index = 10 - 0 = 10
     0 <= 10 < 11 成立 → entry = events[10] = e10
     next_offset = 11（A 的进度前进）
     yield entry → sse_consumer 把它格式化成 SSE 发给浏览器 A

时刻 T3：B 被唤醒，重新拿锁（A 已经放锁）
     同样：local_index = 10 - 0 = 10，拿到 e10，next_offset = 11
     yield entry → 发给浏览器 B

结果：e10 这一条事件，被 A 和 B 各读了一次，但它在 events 列表里只存了一份。
```

**关键点**：

- **事件只存一份**（`events.append` 一次），不管几个订阅者——内存不会因为订阅者变多而膨胀。
- **每个订阅者有自己的 `next_offset`**（是 `subscribe` 函数的局部变量，每个调用一份）——所以 A 和 B 互不干扰，各自按自己的进度读。
- **`notify_all` 是广播**——一次唤醒所有 `wait` 的订阅者，每个醒来后自己检查列表。
- **迟到者也能补看**：订阅者 C 在 T5 才 join，它的 `next_offset` 从 `_resolve_start_offset` 算出来（比如从 0），会先把 e0..e10 这 11 条**历史事件**快速读出来（循环每轮读一条，不等 `wait`），追上进度后才进入 `wait`。这就是"迟到订阅者回放"。

> 这也是为什么麻烦 2 的"事件日志 + ID"和麻烦 3 的"fan-out"是同一套机制的两面：因为有可重放的事件日志，才能让任意数量、任意时刻到来的订阅者各自回放/追赶。

**朴素写法为什么做不到**：朴素 generator 没有"事件日志"这个中间层，`yield` 出去就没了；也没有"共享对象 + notify_all"这个广播机制。第二个订阅者要 join 时，generator 早被第一个迭代光了，无处可读。

---

### 麻烦 4：跨进程 / 多 worker

#### 场景

生产部署时，Gateway（API 服务）往往不止一个进程：

- **横向扩展**：一台机器扛不住，跑 3 台机器，每台一个 Gateway 进程
- **负载均衡**：nginx 把客户端请求分发到不同进程（轮询 / 最少连接）

这时会出现一个核心问题：**客户端的 SSE 连接落在进程 A，但 agent run 跑在进程 B**（因为 run 是进程 B 创建并启动的，它持有那个 agent task）。进程 A 要怎么把进程 B 里产生的事件流给客户端？

麻烦 4 就是专门解决"事件流怎么跨越进程边界"这件事。下面把"为什么朴素写法做不到、两种 bridge 实现怎么对应两种部署、Redis 模式下具体的工作流水"一次讲透。

#### 4.1 先理解一个关键概念：`store_only` 记录

要讲清跨进程，先得理解 `record.store_only` 这个标志。它是"这个 worker 不拥有这个 run"的信号：

- 每个 worker 进程内部有个**内存表** `_runs`（`RunManager` 维护），记录"本进程正在跑的 run"。只有创建并 `asyncio.create_task` 启动了 agent 的那个 worker，才把 run 放进自己的 `_runs`。
- 除了内存表，run 的元信息（run_id、状态、thread_id 等）还会落到**共享存储**（数据库，`RunStore`），所有 worker 都能读。
- 当一个 worker 查一个**不是自己启动的** run 时（`RunManager.get()`，`manager.py:527-557`），内存表里没有，就回退到从共享存储读，读出来的记录 `store_only=True`（`manager.py:399`）。

`store_only` 的字面意思就是："我这个 worker 只从存储里读了这条记录，**没有**对应的活 agent task 在跑"。agent task 在**另一个** worker 手里。

#### 4.2 朴素写法为什么做不到

朴素写法依赖**进程内的 async generator**。Python 进程之间内存是隔离的——进程 A 没法迭代进程 B 里的 generator 对象，那个对象（连同 agent、连同产到一半的 chunk）全在进程 B 的内存里。

代码里能直接看到这个真实约束。看 `join_run` 和 `stream_existing_run` 里这道守卫（`thread_runs.py:627` 和 `:667`）：

```python
if record.store_only and not bridge.supports_cross_process:
    raise HTTPException(409, "Run ... is not active on this worker and cannot be streamed")
```

逐行：

- `if record.store_only and not bridge.supports_cross_process:`
  两个条件**同时**满足才报错：
  - `record.store_only`：这个 run 不在当前 worker 手里（agent task 在别的进程）。
  - `not bridge.supports_cross_process`：当前的 bridge 实现不支持跨进程（内存模式就是如此）。
- `raise HTTPException(409, ...)`
  返回 HTTP 409（Conflict），意思是"这个 run 不在我手上，我又没法跨进程读它的事件，没法给你流"。

（`stream_existing_run` 里多了个 `action is None` 条件，是因为带 `action` 时走的是"跨 worker 取消"的另一条路径，麻烦 4 末尾会讲。）

这道守卫就是朴素写法约束的**直接代码化**：内存模式下，跨 worker 的事件流根本拿不到，与其让客户端傻等，不如直接 409 告诉它"你得连到那个真正在跑的 worker"。

#### 4.3 Bridge 怎么解决的：抽象基类 + 两种实现，对应两种部署

`StreamBridge` 是个**抽象基类**（abstract base class）——只定义接口（有哪些方法），不规定具体实现。具体实现由子类提供，不同的子类对应不同的部署规模。

抽象基类（`backend/packages/harness/deerflow/runtime/stream_bridge/base.py:37-44`）：

```python
class StreamBridge(abc.ABC):
    supports_cross_process: bool = False            # ← 关键开关：是否支持跨进程

    @abc.abstractmethod
    async def publish(self, run_id, event, data) -> None: ...

    @abc.abstractmethod
    async def publish_end(self, run_id) -> None: ...

    @abc.abstractmethod
    def subscribe(self, run_id, *, last_event_id=None, heartbeat_interval=15.0) -> AsyncIterator[StreamEvent]: ...
```

逐行：

- `class StreamBridge(abc.ABC):` 继承 `abc.ABC`，是抽象类，**不能直接实例化**，必须由子类实现所有抽象方法。
- `supports_cross_process: bool = False` 类属性，标记这个实现**是否支持跨进程**。默认 False——这正是 4.2 那道守卫检查的字段。
- `@abc.abstractmethod` 装饰器，标记下面的方法是抽象方法——子类必须实现。`publish` / `publish_end` / `subscribe` 的签名对所有实现都一样，所以**调用方（端点、worker）的代码不用改**，换 bridge 实现就行。

两种实现，对应两种部署：

| 实现 | 文件 | `supports_cross_process` | 事件存在哪 | 适用部署 |
|---|---|---|---|---|
| `MemoryStreamBridge` | `memory.py:27` | `False`（不覆盖） | 本进程内存（`_RunStream.events` 列表） | 单 worker |
| `RedisStreamBridge` | `redis.py:51` | `True`（`redis.py:59`） | Redis 服务（Redis Stream 数据结构） | 多 worker |

**`MemoryStreamBridge`**：麻烦 2、3 看的就是它。事件存在本进程内存里，`_RunStream.events` 是个 Python list。进程一死，列表就没了。所以它天然只能服务"在本进程创建的 run"，跨进程读不到——`supports_cross_process` 保持默认 `False`，于是 4.2 的守卫会对 `store_only` 记录报 409。

**`RedisStreamBridge`**：事件不存本地内存，而是塞进一个**独立的 Redis 服务**。所有 worker 进程都连同一个 Redis，都往同一个 key 读写。进程 A 和进程 B 通过 Redis 这个"中间人"交换事件——谁的内存里都没有事件，事件只在 Redis 里。`supports_cross_process = True`，于是守卫的 `not bridge.supports_cross_process` 为假，**409 不会触发**，任何 worker 都能服务任何 run 的流。

怎么选？看配置 `config.yaml -> stream_bridge.type`。工厂函数 `make_stream_bridge`（`async_provider.py:48-91`）根据它二选一：

```python
@contextlib.asynccontextmanager
async def make_stream_bridge(app_config=None) -> AsyncIterator[StreamBridge]:
    config = _resolve_config(app_config)
    if config is None or config.type == "memory":       # 默认 / memory
        from deerflow.runtime.stream_bridge.memory import MemoryStreamBridge
        bridge = MemoryStreamBridge(queue_maxsize=maxsize)
        ...
        yield bridge
        return
    if config.type == "redis":                          # 多 worker 必需
        from deerflow.runtime.stream_bridge.redis import RedisStreamBridge
        bridge = RedisStreamBridge(redis_url=..., queue_maxsize=..., ...)
        ...
        yield bridge
        return
    raise ValueError(f"Unknown stream bridge type: {config.type!r}")
```

注意两点：

- `type: memory`（默认）→ 单进程够用；`type: redis` → 多进程必需。
- `RedisStreamBridge` 是**懒导入**的（`import` 写在 `if config.type == "redis"` 分支里），因为 `redis` 是个可选依赖（optional extra）。如果选了 memory，没装 redis 包也不报错；只有选了 redis 才需要装（`uv sync --extra redis`）。

应用启动时，`deps.py:261` 这一行根据配置选一个，挂到全局状态上：

```python
app.state.stream_bridge = await stack.enter_async_context(make_stream_bridge(config))
```

- `make_stream_bridge(config)` 根据 config 返回 Memory 或 Redis 实现。
- `stack.enter_async_context(...)` 把它注册到应用的"启动栈"，应用关闭时自动 `close()`。
- `app.state.stream_bridge = ...` 挂在 app 的全局状态上。**每个 worker 进程各自启动时各自建一个 bridge 实例**——Memory 模式下各进程的 bridge 互相独立（所以跨不了进程）；Redis 模式下各进程的 bridge 都连同一个 Redis（所以能跨进程）。

之后路由里 `get_stream_bridge(request)` 就是从 `app.state.stream_bridge` 取出来用。

#### 4.4 Redis 模式下，具体的工作流水是怎么走的（重点）

这是"跨进程具体怎么用"的核心。假设部署了两个 worker（W1、W2），都连同一个 Redis。用户在浏览器发消息，请求被 nginx 分到了 **W1**；但之前同一个 run 是 **W2** 创建的（agent task 在 W2 手里）。看事件怎么从 W2 流到 W1 再到浏览器。

先看 Redis 这边用了什么数据结构。`RedisStreamBridge` 用的是 **Redis Stream**（不是 pub/sub，这是个关键区别）。每个 run 对应一个 Redis Stream，key 是 `deerflow:stream_bridge:{run_id}`（`redis.py:84-85`）：

```python
def _stream_key(self, run_id: str) -> str:
    return f"{self._key_prefix}:{run_id}"      # 例如 "deerflow:stream_bridge:run-abc123"
```

为什么用 Stream 不用 pub/sub？因为 **pub/sub 是"发完就丢"**——后连上的订阅者收不到历史消息，重连也没法补。而 Stream 是**持久化的、可回放的事件日志**（和内存模式的 `events` 列表对应），正好支撑麻烦 2 的 `Last-Event-ID` 重连。

**生产端**（agent task 所在的 W2）——`publish`（`redis.py:142-152`）：

```python
async def publish(self, run_id, event, data) -> None:
    key = self._stream_key(run_id)
    await self._xadd_retained(                          # 底层调 Redis 的 XADD
        key,
        {"kind": "event", "event": event, "data": self._encode_data(data)},
        maxlen=self._maxsize,                           # 滑动窗口：最多留 256 条
    )
```

`_xadd_retained`（`redis.py:87-105`）底层用的是 Redis 的 **`XADD`** 命令：

```python
await self._redis.xadd(key, fields, maxlen=maxlen, approximate=False)
# 或者（设了 TTL 时）用 pipeline 事务：
async with self._redis.pipeline(transaction=True) as pipe:
    pipe.xadd(key, fields, maxlen=maxlen, approximate=False)
    pipe.expire(key, self._stream_ttl_seconds)          # 默认 86400 秒后自动清理这个 key
    await pipe.execute()
```

逐行：

- `xadd(key, fields, maxlen=maxlen)`：往 Redis Stream 里追加一条。`maxlen` 保证 Stream 最多留 `maxsize`（默认 256）条——**这就是麻烦 6 的"有界滑动窗口"在 Redis 上的实现**，对应内存模式的 `del events[:overflow]`。
- `pipe.expire(key, ttl)`：给这个 key 设个过期时间（默认 1 天），run 结束很久后 Redis 自动回收，不用人工清。

`XADD` 的返回值是 Redis 自动生成的 stream entry id（形如 `1690000000123-0`），它就被直接拿来当 SSE 的 `id:` 字段——这就是麻烦 2 里 `Last-Event-ID` 重连在 Redis 模式下的依据。

**消费端**（浏览器连着的 W1）——`subscribe`（`redis.py:181-235`）：

```python
async def subscribe(self, run_id, *, last_event_id=None, heartbeat_interval=15.0):
    key = self._stream_key(run_id)
    stream_id = await self._resolve_start_stream_id(key, last_event_id)   # 从哪条开始读
    block_ms = max(1, int(heartbeat_interval * 1000))                     # 心跳毫秒数
    while True:
        response = await self._redis.xread({key: stream_id}, count=_XREAD_COUNT, block=block_ms)  # ← 核心一行
        if not response:                          # block 超时还没数据
            yield HEARTBEAT_SENTINEL              # → 心跳（麻烦 5）
            continue
        for _stream_name, entries in response:
            for event_id, fields in entries:
                stream_id = event_id             # 我的进度前进
                entry = self._entry_from_redis(event_id, fields)
                if entry is END_SENTINEL:
                    yield END_SENTINEL
                    return
                yield entry
```

逐行讲关键的：

- `stream_id = await self._resolve_start_stream_id(key, last_event_id)`：算出从哪条 entry 开始读。首次连接（`last_event_id is None`）从 `"0-0"`（Stream 开头）开始；重连就把客户端传来的 `Last-Event-ID` 直接当起点——**因为 Redis Stream 的 entry id 就是发布时返回的那个 id**，天然对得上。
- `await self._redis.xread({key: stream_id}, count=_XREAD_COUNT, block=block_ms)`：**核心**。`XREAD` 是 Redis 的"读 Stream"命令，参数含义：
  - `{key: stream_id}`：从 `key` 这个 Stream 的 `stream_id` **之后**开始读。
  - `count=64`（`_XREAD_COUNT`）：最多读 64 条——把一次大批量回放压缩成少数几次往返。
  - `block=block_ms`：**阻塞读**。如果当前没有新数据，Redis 不会立刻返回空，而是**最多阻塞 15 秒**（心跳间隔），期间一旦有新数据就立刻返回。这正好实现了"等新事件 + 超时发心跳"，和内存模式的 `asyncio.wait_for(condition.wait(), timeout=...)` 对应。
- `if not response: yield HEARTBEAT_SENTINEL`：`block` 超时还没数据 → 产心跳哨兵。这就是麻烦 5 的心跳在 Redis 模式下的实现。
- `stream_id = event_id`：读完一条，把进度推进到这条的 id。**这个 `stream_id` 是 `subscribe` 函数的局部变量，每个订阅者一份**——和内存模式的 `next_offset` 一样，多订阅者互不干扰。

**注意**：Redis 模式下没有 `notify_all`。fan-out 是怎么实现的？答案是 **Redis Stream 天生支持多消费者各自 `XREAD` 同一个 key**——W1 和 W2 上的两个订阅者各自发 `XREAD`，各自从自己的 `stream_id` 往后读，互不影响。Redis 就是那个共享的"事件日志"，对应内存模式里的 `_RunStream.events` 列表。

#### 4.5 把跨进程的完整工作流串一遍

现在把 4.4 的零件拼起来，看一个完整的跨进程场景。部署：nginx → W1、W2 两个 worker，共用一个 Redis。

```
初始状态：
  - W2 之前创建了 run R（agent task 在 W2 的事件循环里跑）
  - W2 的 bridge 把 R 的事件 publish 到 Redis 的 key "deerflow:stream_bridge:R"
  - W1 没有跑 R 的 agent task；它的 RunManager 内存表里没有 R

步骤 1：浏览器想看 run R 的流，请求被 nginx 分到 W1
   → W1 命中 GET /runs/R/join（或 /runs/R/stream）
   → record = run_mgr.get(R)
     W1 的内存表没有 R → 回退到共享存储读 → 得到 record，store_only=True
   → 检查守卫：record.store_only and not bridge.supports_cross_process
     Redis 模式下 supports_cross_process=True → 守卫不触发 ✅（内存模式下这里就 409 了）
   → 进入 sse_consumer → bridge.subscribe(R, last_event_id=...)

步骤 2：W1 的 subscribe 开始从 Redis 读
   → _resolve_start_stream_id：浏览器带了 Last-Event-ID → 从那条之后开始
   → xread(key, stream_id, count=64, block=15000)
     Redis 把历史 entry（重连回放）立刻返回，W1 yield 出去 → SSE 发给浏览器
   → 追上进度后，再次 xread 没有新数据 → block 阻塞等待

步骤 3：与此同时，W2 上的 agent 产出新事件
   → W2 的 worker.py 调 bridge.publish(R, "messages", {"content":"你"})
   → publish → XADD 到 Redis 的 key
   → Redis 的 key 多了一条 entry

步骤 4：W1 上阻塞中的 xread 被唤醒（block 期间有新数据就立刻返回）
   → 返回那条新 entry → W1 yield → SSE 发给浏览器
   → stream_id 前进，继续 xread block 等下一条

结果：agent 在 W2 跑、事件经 Redis 中转、SSE 从 W1 发出——三者解耦，浏览器毫不知情。
```

对比内存模式会发生什么：同样的场景（R 在 W2，请求到 W1），W1 会在步骤 1 的守卫处直接返回 **409**。客户端（浏览器或 SDK）拿到 409 后，要么重试到命中 W2，要么部署上用 **sticky session**（nginx 按 thread_id/run_id hash，保证同一 run 的请求总落同一个 worker）。这就是"内存模式只能单 worker，或必须粘性会话"的根因。

#### 4.6 补充：跨进程时的"取消"和"孤儿 run 恢复"

跨进程不只"读事件"一件事，还有两个相关问题，这里顺带说清（它们不是 bridge 的核心职责，但和 `supports_cross_process` 这套机制配套）：

**跨 worker 取消**：如果浏览器在 W1 上对（W2 拥有的）run R 点了"停止"按钮，会带 `action=interrupt` 打到 `stream_existing_run`（`thread_runs.py:671-688`）。W1 调 `run_mgr.cancel(R, action=...)`——这走的是 **lease（租约）机制**，不是 bridge：每个 run 在共享存储里记了 `owner_worker_id` 和 `lease_expires_at`（`manager.py:493-507`）。W1 发现 R 的 lease 还在 W2 手里且没过期 → 返回 `lease_valid_elsewhere` → W1 回 409 + `Retry-After`；如果 W2 的 lease 已过期（W2 可能挂了）→ W1 能"接管"（take over）。这和 bridge 无关，是另一套所有权系统。

**孤儿 run 恢复**：进程重启后，有些 run 在存储里还标着"running"，但实际 agent task 已经随进程死了——再也不会有 `publish`。如果客户端重连过来订阅，会**傻等 END_SENTINEL 永远等不到**。所以 Gateway 启动时会扫这些孤儿 run，主动补发结束信号（`deps.py:326`）：

```python
await _publish_recovered_run_stream_end(app.state.stream_bridge, recovered_runs, cleanup_delay=cleanup_delay)
```

它对每个孤儿 run 调一次 `bridge.publish_end(run_id)`（`deps.py:131`），让订阅者能正常收到 `END_SENTINEL` 然后退出。这件事内存模式和 Redis 模式都得做——因为进程重启后内存表空了，原来的 `_RunStream` 也没了，得重建"已结束"的信号。

朴素写法根本做不到这两件事——agent 都没了，谁去发结束事件？谁去协调跨 worker 的所有权？bridge + lease 这套机制就是为了把这些"生命周期错位"的问题都接住。

---

### 麻烦 5：长静默期保活（心跳）

#### 场景

agent 在执行中会**长时间不产事件**：

- 在跑 `pip install`，可能 60 秒没动静
- 在等一个慢工具返回
- 在等 LLM 推理（大模型可能 10-30 秒才出第一个 token）

这期间 SSE 连接上没有任何字节流动。问题来了：互联网链路上有一堆"中间设备"（nginx、CDN、NAT 路由器、浏览器自身），它们看到一条连接长时间没数据，会判定它"死了"，主动掐掉：

- nginx 默认 60 秒没数据就掐（`proxy_read_timeout`）
- 浏览器 EventSource 默认 45 秒超时
- NAT 设备会清理"空闲"的连接表项

被掐之后，客户端看到的就是连接断开，得重新连接——重连时如果 bridge 里的事件已经被淘汰（麻烦 6），还会丢事件。

#### 朴素写法为什么做不到

朴素 generator 在 `astream()` 卡住等 LLM 时，**真的什么字节都发不出来**——它要么侵入 agent 内部塞心跳（破坏封装），要么干等被掐。

#### Bridge 怎么解决的：心跳哨兵

回到 `subscribe` 里那行（前面麻烦 3 贴过）：

```python
try:
    await asyncio.wait_for(stream.condition.wait(), timeout=heartbeat_interval)
except TimeoutError:
    entry = HEARTBEAT_SENTINEL
else:
    continue
```

意思是：等事件来，但最多等 `heartbeat_interval`（默认 15 秒）。超时了就产出一个 `HEARTBEAT_SENTINEL`（哨兵对象，就是个特殊标记）。

`sse_consumer` 收到哨兵后（`services.py:850-855`）：

```python
if entry is HEARTBEAT_SENTINEL:
    if await _terminal_record_stream_missing(bridge, record):
        yield format_sse("end", None)
        return
    yield ": heartbeat\n\n"
    continue
```

逐行：

- `if entry is HEARTBEAT_SENTINEL:` 是不是心跳哨兵？
- `if await _terminal_record_stream_missing(bridge, record):`
  **顺带做健康检查**——run 是不是悄悄结束了？如果是，发个 end 然后退出（避免客户端傻等）。
- `yield ": heartbeat\n\n"`
  发 SSE 注释帧。SSE 协议规定，以 `:` 开头的行是注释，客户端会**忽略**它（不影响业务），但它**仍然是字节**，会让中间设备看到"连接还活着"。这就是保活。
- `continue`
  继续等下一条事件。

注释帧的样子：

```
: heartbeat

```

为什么用注释帧而不是发个普通事件？因为注释帧不会触发客户端的回调，业务无感；如果发个 `event: ping` 之类的，客户端还得专门处理。

**巧妙之处**：心跳每 15 秒发一次，**完全解决了中间设备掐连接**的问题（15 < 45 < 60，远早于它们超时）。同时心跳间隙还能做健康检查——一举两得。

---

### 麻烦 6：背压 / 内存边界

#### 场景

设想极端情况：

- agent 跑得飞快（比如返回大量短 token，每秒 1000 条事件）
- 客户端网速慢（地铁 3G，每秒只能收 10 条）

朴素写法会怎么样？

#### 朴素写法会怎么样

朴素 generator 是**拉模式（pull-based）**：生产者 `yield` 之后必须等消费者来取下一个才继续。所以 agent 只能**慢下来**等客户端——一个慢用户拖慢整个 worker 上其他 run 的执行。

反过来如果有人"聪明地"用无界队列：

```python
queue = asyncio.Queue()  # 没设 maxsize
async def producer():
    async for chunk in agent.astream(...):
        await queue.put(chunk)   # 永远不阻塞
async def consumer():
    while True:
        chunk = await queue.get()
        yield format_sse(chunk)
```

队列没上限，生产者疯跑，消费者跟不上，队列里堆了几百万条事件——**内存爆掉（OOM, Out Of Memory）**，进程被操作系统杀掉。

#### Bridge 怎么解决的：有界滑动窗口

回到 publish 里的这段（`memory.py:100-103`）：

```python
if len(stream.events) > self._maxsize:
    overflow = len(stream.events) - self._maxsize
    del stream.events[:overflow]
    stream.start_offset += overflow
```

逐行：

- `if len(stream.events) > self._maxsize:`
  列表长度超过上限（默认 256）吗？
- `overflow = len(stream.events) - self._maxsize`
  超了多少。比如列表有 257 条，maxsize 256，overflow=1。
- `del stream.events[:overflow]`
  删掉最早的 overflow 条。`events[:overflow]` 是切片，表示"前 overflow 个元素"，`del` 删掉它们。比如 overflow=1，删掉 events[0]。
- `stream.start_offset += overflow`
  起始偏移量前进 overflow。**这一步非常关键**——它保证重连算术仍然成立。

举个具体例子说明 `start_offset` 的作用：

- 初始：`start_offset=0`，events = [e0, e1, e2, ...]（e0 的 seq=0）
- e0 的 id 是 `{ts}-0`，重连客户端传 `Last-Event-ID: {ts}-0`，期望从 e1 开始
- 现在列表满了，淘汰 e0：`start_offset=1`，events = [e1, e2, ...]
- 如果有个客户端传 `Last-Event-ID: {ts}-0` 来重连，但 e0 已经被淘汰了
- `_resolve_start_offset` 算 `local_index = 0 - 1 = -1`，不满足 `0 <= local_index < len(events)`，定位失败
- 兜底返回 `start_offset=1`，从 e1 开始回放——**宁可重复也不能漏**

#### 这个设计的取舍

非常明确：

- **生产者永远不阻塞**：agent 该跑多快跑多快，worker 不会被慢客户端拖累
- **慢消费者最多丢"窗口外"的历史事件**：256 条之外的就没了
- **不丢"当前在发生"的事件**：只要客户端跟得上最近 256 条，就能看到完整流
- **配合麻烦 2 的重连兜底**：丢失的部分由"回放从最早留存"补上
- **内存有上限**：每个 run 最多存 256 条，再多就淘汰——永不 OOM

这是一种典型的**有界缓冲 + 允许丢弃旧数据**的策略，适合"实时性 > 完整性"的场景。聊天流正好符合——用户更关心"现在发生了什么"，不太关心"3 分钟前的 token"。

---

## 第 3 步：把 6 个麻烦的本质串起来

朴素写法 `agent.astream() → StreamingResponse` 的根本问题是：

> **它把"agent 的执行生命周期"和"HTTP 连接的生命周期"绑定成了同一个东西。**

这两者本来是完全独立的：

| 维度 | HTTP 连接 | agent run |
|---|---|---|
| 寿命 | 几秒到几分钟，随时可能断 | 一次完整的执行，有状态机 |
| 数量 | 1 个客户端 1 条 | 可能被多个客户端同时看 |
| 位置 | 在某个 worker 进程里 | 可能在同进程，也可能跨进程 |
| 可恢复 | 断了就断了，重连是新的 | 有 checkpoint，能恢复 |

把两者绑死，就会出现 6 个麻烦——因为现实里它们的生命周期**就是会错位**：

- ① HTTP 断了，agent 不该跟着死
- ② HTTP 断了重连，agent 不能重跑，得有"事件回放"
- ③ 多条 HTTP 想看同一个 agent
- ④ HTTP 和 agent 可能在不同进程
- ⑤ HTTP 长时间没数据，但 agent 还在跑，得保活
- ⑥ agent 跑得快，HTTP 收得慢，得有缓冲和淘汰

`StreamBridge` 就是**把这两条生命周期解耦**的那个中间层：

- agent 只管"这次 run 发生了哪些事件"，往 bridge 里 publish
- bridge 把事件**可靠地、可重放地、可扇出地、可跨进程地**递交给任意数量、任意时刻到来的 HTTP 订阅者

代码上对应 `client.py:703-706` 那段注释：

> `StreamBridge` is an asyncio-queue decoupling producers from consumers across an HTTP boundary (`Last-Event-ID` replay, heartbeats, multi-subscriber fan-out).

翻译：StreamBridge 是一个异步队列，**跨 HTTP 边界把生产者和消费者解耦**（支持 Last-Event-ID 重放、心跳、多订阅者扇出）。

所以它不是"性能优化"或"代码好看"——是这 6 个麻烦里**任意一个**单拿出来，朴素写法都过不去。它是在解决一类真实工程问题，而不是过度设计。
