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

- 用户在标签页 A 发了消息，agent 开跑
- 用户又开了标签页 B 看同一线程——B 要 join 已经在跑的 run
- IM 通道（飞书）也触发了这个 run，飞书端也要看事件
- 调度器 / 内部测试也想旁观

这些场景在 DeerFlow 里真实存在。看 `thread_runs.py:645-698` 的 `stream_existing_run` 端点（GET 和 POST 都注册）：

```python
@router.get("/{thread_id}/runs/{run_id}/stream", response_model=None)
@router.post("/{thread_id}/runs/{run_id}/stream", response_model=None)
@require_permission("runs", "read", owner_check=True)
async def stream_existing_run(thread_id, run_id, request, ...):
    ...
    return StreamingResponse(
        sse_consumer(bridge, record, request, run_mgr),
        ...
    )
```

这个端点的作用是：**第二个、第三个客户端加入一个已经在跑的 run 的事件流**。它也调用 `sse_consumer`，也订阅 bridge。

#### 朴素写法为什么做不到

朴素 generator 是 **1:1 管道**：一个生产者（agent）配一个消费者（HTTP 响应）。第二个消费者来 join 时：

- generator 已经被第一个消费者迭代掉了——没法"再生成一遍"
- 重跑 agent？同样不行（副作用、成本、不确定性）

#### Bridge 怎么解决的：Condition 广播

看 publish 端（`memory.py:95-104`）：

```python
async def publish(self, run_id, event, data) -> None:
    stream = self._get_or_create_stream(run_id)
    entry = StreamEvent(id=self._next_id(run_id), event=event, data=data)
    async with stream.condition:
        stream.events.append(entry)
        if len(stream.events) > self._maxsize:
            overflow = len(stream.events) - self._maxsize
            del stream.events[:overflow]
            stream.start_offset += overflow
        stream.condition.notify_all()
```

逐行：

- `async def publish(self, run_id, event, data) -> None:`
  生产者调用，往 run 的事件流里塞一条。
- `stream = self._get_or_create_stream(run_id)`
  根据 run_id 找到（或新建）对应的 `_RunStream` 对象。
- `entry = StreamEvent(id=self._next_id(run_id), event=event, data=data)`
  构造一个事件对象：id 用 `_next_id` 生成（麻烦 2 讲过），event 和 data 是调用者传进来的。
- `async with stream.condition:`
  **加锁**。`async with` 是异步上下文管理器，进入时自动 `acquire`（拿锁），退出时自动 `release`（放锁）。`Condition` 本身也是一把锁，所以这里相当于"获取 condition 锁"。这一段代码（到 `notify_all`）是临界区——同一时刻只有一个协程能执行。
  为什么要锁？因为可能有多个协程同时往里塞事件（虽然实际 publish 一般是 agent worker 一个，但锁是健壮的写法），同时还有别的协程在 `wait`，必须保证数据一致。
- `stream.events.append(entry)`
  把事件加到列表末尾。
- `if len(stream.events) > self._maxsize:` 滑动窗口判断（麻烦 6 细讲），列表超长就淘汰旧的。
- `stream.condition.notify_all()`
  **按铃**——叫醒所有在这个 condition 上 `wait` 的订阅者协程。这是 fan-out 的核心：一个 publish，所有订阅者都被通知。

订阅端（`memory.py:112-150`）：

```python
async def subscribe(self, run_id, *, last_event_id=None, heartbeat_interval=15.0):
    stream = self._get_or_create_stream(run_id)
    async with stream.condition:
        next_offset = self._resolve_start_offset(stream, last_event_id)

    while True:
        async with stream.condition:
            if next_offset < stream.start_offset:
                next_offset = stream.start_offset
            local_index = next_offset - stream.start_offset
            if 0 <= local_index < len(stream.events):
                entry = stream.events[local_index]
                next_offset += 1
            elif stream.ended:
                entry = END_SENTINEL
            else:
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

- `async with stream.condition: next_offset = self._resolve_start_offset(stream, last_event_id)`
  加锁，调用麻烦 2 里讲过的函数算出起始位置。锁是为了保证算 `start_offset` 时数据不被改。
- `while True:` 死循环——持续订阅，直到 run 结束。
- `async with stream.condition:` 每轮迭代加锁。
- `local_index = next_offset - stream.start_offset`
  把绝对偏移换算成列表下标。
- `if 0 <= local_index < len(stream.events): entry = stream.events[local_index]; next_offset += 1`
  如果列表里有下一条要给的事件，拿出来，下标前进。
- `elif stream.ended: entry = END_SENTINEL`
  列表里没有新事件，但 run 已经结束了——返回结束标记。
- `else: try: await asyncio.wait_for(stream.condition.wait(), timeout=heartbeat_interval)`
  没有新事件，run 也没结束——**等铃响**（publish 进来新事件会 notify_all），最多等 `heartbeat_interval` 秒。
  - `stream.condition.wait()` 是"释放锁 + 暂停协程 + 等通知 + 被叫醒后重新拿锁"。
  - `asyncio.wait_for(..., timeout=...)` 包一层超时——超过时间没被叫醒就抛 `TimeoutError`。这是心跳机制（麻烦 5）。
- `except TimeoutError: entry = HEARTBEAT_SENTINEL`
  超时了（没新事件），给个心跳标记。
- `else: continue`
  `try-except-else` 的 else 分支：正常被叫醒（没超时），说明可能有新事件了，**重新进入循环**检查（`continue`）。

**多订阅者是怎么工作的**：

每个调用 `subscribe` 的客户端都在自己的 `while True` 循环里 `wait`。当 publish 来时，`notify_all()` 把它们**全部叫醒**，每个都重新检查 `events` 列表，各自从自己的 `next_offset` 往前读。所以一个事件能被多个订阅者各自读到一次——这就是 fan-out（扇出，一对多广播）。

---

### 麻烦 4：跨进程 / 多 worker

#### 场景

生产部署时，Gateway（API 服务）往往不止一个进程：

- **横向扩展**：一台机器扛不住，跑 3 台机器，每台一个 Gateway 进程
- **负载均衡**：nginx 把客户端请求分发到不同进程

这时会出现一个问题：客户端的 SSE 连接落在进程 A，但 agent run 跑在进程 B（因为某些调度原因）。进程 A 要怎么把进程 B 里产生的事件流给客户端？

#### 朴素写法为什么做不到

朴素写法依赖**进程内的 async generator**。Python 进程之间内存是隔离的——进程 A 没法迭代进程 B 里的 generator 对象，那个对象在进程 B 的内存里。

代码里能直接看到这个真实约束，`thread_runs.py:667-668`：

```python
if record.store_only and action is None and not bridge.supports_cross_process:
    raise HTTPException(409, "Run ... is not active on this worker and cannot be streamed")
```

逐行：

- `if record.store_only and action is None and not bridge.supports_cross_process:`
  三个条件全满足才报错：
  - `record.store_only`：这个 run 记录是从存储里恢复出来的（不是这个 worker 正在跑的）。`store_only` 的含义是"这个 worker 只从数据库读了记录，没有实际的运行任务"。
  - `action is None`：用户没有要取消（cancel 时走另一条路径）。
  - `not bridge.supports_cross_process`：当前的 bridge 实现不支持跨进程。
- `raise HTTPException(409, "Run ... is not active on this worker and cannot be streamed")`
  返回 HTTP 409（Conflict）错误，意思是"这个 run 不在当前 worker 上跑，没法给你流"。

#### Bridge 怎么解决的：抽象 + 两种实现

`StreamBridge` 是个**抽象基类**（abstract base class）——只定义接口（有哪些方法），不规定具体实现。具体实现由子类提供。

抽象基类的代码（`backend/packages/harness/deerflow/runtime/stream_bridge/base.py:37-44`）：

```python
class StreamBridge(abc.ABC):
    supports_cross_process: bool = False

    @abc.abstractmethod
    async def publish(self, run_id, event, data) -> None:
        ...

    @abc.abstractmethod
    async def publish_end(self, run_id) -> None:
        ...
```

逐行：

- `class StreamBridge(abc.ABC):`
  继承自 `abc.ABC`（Abstract Base Class），表示这是个抽象类，**不能直接实例化**（不能 `StreamBridge()`），必须由子类实现所有抽象方法。
- `supports_cross_process: bool = False`
  类属性，标记这个实现**是否支持跨进程**。默认 False，子类可以覆盖为 True。
- `@abc.abstractmethod`
  装饰器，标记下面的方法是抽象方法——子类必须实现，否则子类也不能实例化。
- `async def publish(self, run_id, event, data) -> None: ...`
  方法定义，`...`（Ellipsis）只是占位，表示"这里没有实现，由子类填"。

两种实现：

**`MemoryStreamBridge`**（`memory.py:25`）：进程内实现。事件存在 Python 进程的内存里（就是之前看的 `_RunStream.events` 列表）。`supports_cross_process` 不覆盖，保持默认的 `False`。

**`RedisStreamBridge`**（`redis.py`）：用 Redis（一个独立的内存数据库服务，进程之间通过它交换数据）。事件不存本地内存，而是塞进 Redis 的 stream 数据结构。任何进程都能 publish、任何进程都能 subscribe——通过 Redis 这个"中间人"交换。`supports_cross_process = True`。

怎么选？看配置 `config.yaml -> stream_bridge.type`：

- `type: memory`（默认）→ 单进程部署够用
- `type: redis` → 多进程部署必需

应用启动时，`backend/app/gateway/deps.py:261` 这一行根据配置选一个：

```python
app.state.stream_bridge = await stack.enter_async_context(make_stream_bridge(config))
```

- `make_stream_bridge(config)` 根据 config 返回 Memory 或 Redis 实现
- `stack.enter_async_context(...)` 把它注册到应用的"启动栈"里，应用关闭时自动清理
- `app.state.stream_bridge = ...` 把它挂在 app 的全局状态上

之后路由里 `get_stream_bridge(request)` 就是从 `app.state.stream_bridge` 取出来。整个应用共用一个 bridge 实例。

#### 配合"恢复"场景

`deps.py:326` 还有一段：

```python
await _publish_recovered_run_stream_end(app.state.stream_bridge, recovered_runs, cleanup_delay=cleanup_delay)
```

进程重启后，有些 run 在数据库里标记为"还在跑"，但实际上 agent 已经不会产事件了（进程死了）。如果客户端重连过来订阅，会傻等。所以启动时**主动给这些"孤儿 run"补发 END_SENTINEL**，让客户端能正常收到"结束"。

朴素写法根本做不到——agent 都没了，谁去发结束事件？

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
