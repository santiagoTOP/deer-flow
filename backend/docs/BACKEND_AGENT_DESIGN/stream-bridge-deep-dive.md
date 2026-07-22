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

#### 3.4.1 补充：定时任务怎么把 run 派发给 agent 执行（完整链路逐行讲）

既然 3.4 说了"调度器不订阅 bridge，只靠完成回调钩子关心 run 跑完了没"，那自然引出下一个问题：**那定时任务是怎么把一个 run 启起来、交给 agent 执行的？** 这一段把从"到期任务被认领"到"agent 真正开始跑"的整条派发链路一次讲透。它和 bridge 没有直接关系（调度器确实不碰 bridge），但和"run 的生命周期怎么被启动、怎么被回写"紧密相关，正好补全 3.4 的另一面。

整条链路分 5 个阶段。

##### 阶段 1：后台轮询认领（scheduler 进程内）

入口在 `ScheduledTaskService._run_loop`（`backend/app/scheduler/service.py:332-346`）：

```python
async def _run_loop(self) -> None:
    while not self._stop.is_set():
        try:
            await self.run_once(now=datetime.now(UTC))
        except Exception:
            logger.exception("Scheduled task poll failed; retrying next interval")
        try:
            await asyncio.wait_for(
                self._stop.wait(),
                timeout=self._poll_interval_seconds,
            )
        except TimeoutError:
            continue
```

逐行：

- `async def _run_loop(self) -> None:` 定义调度器的核心循环。这是个**协程**，会被 `asyncio.create_task` 放到事件循环里长期跑（见阶段 1 启动处的 `start` 方法）。
- `while not self._stop.is_set():` 只要 `_stop` 这个 `asyncio.Event` 没被 set，就一直循环。`stop()` 方法会 `set()` 它，让循环退出——这是优雅停机的入口。
- `try: await self.run_once(now=datetime.now(UTC))` 每个周期执行一次 `run_once`（下面讲），传当前 UTC 时间。**关键点**：包在 `try` 里是为了**容错**——一次轮询失败（比如数据库暂时锁了）不能把整个调度器协程杀掉，记个日志下一周期继续。
- `except Exception: logger.exception(...)` 捕获所有异常，打日志，**不退出循环**。注释（`service.py:337-339`）特意说明：SQLite 的 "database is locked" 这种瞬时错误绝不能杀死 poller。
- `await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval_seconds)` **这一行是"周期性睡眠"的优雅写法**。`self._stop.wait()` 会阻塞到有人 `set()` 这个 Event；`asyncio.wait_for` 给它套一个 `timeout`（轮询间隔，比如 30 秒）。两种情况会从这行返回：
  - 有人调了 `stop()` → `wait()` 立刻返回 → `wait_for` 不超时 → 走 `else`（这里没写 else，直接落到循环条件检查，发现 `_stop.is_set()` 为真，退出）。
  - 超时到了还没人 stop → 抛 `TimeoutError` → 走下面的 `except`。
- `except TimeoutError: continue` 超时了就进入下一轮循环。**这就是"每 N 秒轮一次"的实现**——不是 `time.sleep`（那是同步阻塞，会卡住整个事件循环），而是 `wait_for` + `Event`（异步等待，期间让出 CPU）。

`run_once` 本体（`service.py:39-54`）：

```python
async def run_once(self, *, now: datetime) -> None:
    active = await self._task_run_repo.count_active_runs()
    budget = self._max_concurrent_runs - active
    if budget <= 0:
        return
    claimed = await self._task_repo.claim_due_tasks(
        now=now,
        lease_owner=self._lease_owner,
        lease_seconds=self._lease_seconds,
        limit=budget,
    )
    for task in claimed:
        await self.dispatch_task(task, now=now, trigger="scheduled")
```

逐行：

- `active = await self._task_run_repo.count_active_runs()` 查 `scheduled_task_runs` 表里状态为 `queued` 或 `running` 的行数（见 `scheduled_task_runs/sql.py:68-73` 的 `ACTIVE_RUN_STATUSES = ("queued", "running")`）。这是"当前有多少个定时任务正在跑"的全局计数。
- `budget = self._max_concurrent_runs - active` 算出这一轮**还能再启动多少个**。`_max_concurrent_runs` 是配置项（`scheduler.max_concurrent_runs`）。注释（`service.py:40-42`）特意强调：这是个**全局上限**，不是"每次轮询最多认领多少"——因为长 run 会跨多个轮询周期累积，所以每轮都要重新算剩余预算。
- `if budget <= 0: return` 没预算了就直接返回，这一轮什么都不认领。这保证定时任务不会把 agent runtime 挤爆。
- `claimed = await self._task_repo.claim_due_tasks(...)` **这是分布式安全的核心**。`claim_due_tasks` 是个原子操作（在数据库层用 `UPDATE ... WHERE lease_expires_at < now RETURNING ...` 或等价手段实现），把到期的、且没被别人认领的任务"租"给当前调度器。参数：
  - `now=now`：当前时间，用来判断哪些任务到期（`next_run_at <= now`）且租约过期（`lease_expires_at < now`）。
  - `lease_owner=self._lease_owner`：租约持有者标识。`_lease_owner` 在构造函数里生成（`service.py:35`）：`f"{socket.gethostname()}:{uuid.uuid4().hex}"`——主机名加一个随机 uuid，保证同一台机器上即使重启也会换新 owner（防止旧实例的幽灵租约干扰新实例）。
  - `lease_seconds=self._lease_seconds`：租约时长。认领后这一行会写上 `lease_expires_at = now + lease_seconds`，期间别的调度器实例不会动它。
  - `limit=budget`：最多认领这么多，正好填满剩余并发预算。
- `for task in claimed: await self.dispatch_task(task, now=now, trigger="scheduled")` 对每个认领到的任务，调用 `dispatch_task` 派发。`trigger="scheduled"` 标记这是定时触发（区别于 `manual` 手动触发，影响失败时的状态流转，见 `_task_status_for_failure`）。

> **为什么用租约而不是直接 `SELECT ... UPDATE`？** 因为调度器可能部署多实例（高可用）。如果两个实例同时 `SELECT` 到同一个到期任务，然后各自 `UPDATE` 并派发，同一个任务会被执行两次。租约机制让"认领"变成原子的：第一个 `UPDATE` 成功的实例拿到 lease，第二个 `UPDATE` 的 `WHERE lease_expires_at < now` 条件就不成立了（lease 被刷新过了），所以拿不到。这和 0.9 讲的 run lease 是同一套思想。

##### 阶段 2：派发准备（`dispatch_task` 的前半段）

`dispatch_task` 是整个派发逻辑的核心（`service.py:81-210`），这里先讲它"启动 run 之前"的准备部分：

```python
async def dispatch_task(self, task, *, now, trigger):
    execution_thread_id = task.get("thread_id")
    if task.get("context_mode") == "fresh_thread_per_run" or not execution_thread_id:
        execution_thread_id = str(uuid.uuid4())
    skip_error: str | None = None
    if task.get("overlap_policy", "skip") == "skip" and await self._task_run_repo.has_active_runs(task["id"]):
        if trigger == "manual":
            return {"outcome": "conflict", ...}
        skip_error = "skipped: a previous run of this task is still active"
    task_run_id = f"task-run-{uuid.uuid4().hex}"
    await self._task_run_repo.create(
        run_record_id=task_run_id,
        task_id=task["id"],
        thread_id=execution_thread_id,
        scheduled_for=now,
        trigger=trigger,
        status="queued",
    )
    if skip_error is not None:
        return await self._finalize_skip(...)
```

逐行：

- `execution_thread_id = task.get("thread_id")` 先取任务配置的默认 thread。
- `if task.get("context_mode") == "fresh_thread_per_run" or not execution_thread_id:` **两种情况要换新 thread**：①任务配置了"每次跑都开新会话"（`fresh_thread_per_run`）；②任务压根没配 thread。`context_mode` 的默认值在模型里（`scheduled_tasks/model.py:17`）就是 `"fresh_thread_per_run"`，所以**定时任务默认每次都在全新会话里跑**——上一轮的对话上下文不会污染这一轮。
- `execution_thread_id = str(uuid.uuid4())` 生成一个新 thread id。这个 thread 在 agent runtime 那边会被自动创建（`start_run` 路径负责）。
- `skip_error: str | None = None` 初始化"跳过原因"。如果这一轮因为重叠策略被跳过，这里会被填上。
- `if task.get("overlap_policy", "skip") == "skip" and await self._task_run_repo.has_active_runs(task["id"]):` **重叠检查**。`overlap_policy` 默认 `"skip"`（`scheduled_tasks/model.py:25`），意思是"如果上一次还没跑完，就跳过这一次"。`has_active_runs` 查 `scheduled_task_runs` 表里这个 task 还有没有 `queued`/`running` 的行（`sql.py:109-120`）。
- `if trigger == "manual": return {"outcome": "conflict", ...}` 手动触发遇到重叠，直接返回 conflict——因为手动触发一般是 API 请求，得立刻给调用方一个明确答复（HTTP 层面会映射成 409）。
- `skip_error = "skipped: ..."` 定时触发遇到重叠，**不立刻返回**，而是先记下跳过原因，下面照常创建一行 task_run（状态会是 `skipped`）。这样历史记录里能看到"这次本来该跑，但被跳过了"。
- `task_run_id = f"task-run-{uuid.uuid4().hex}"` 生成这次执行的唯一 id。`uuid4().hex` 是 32 位无连字符的十六进制。
- `await self._task_run_repo.create(...)` 在 `scheduled_task_runs` 表插一行，状态 `queued`。**注意时机**：这一行在"真正启动 run 之前"就插了，所以 `count_active_runs` 会立刻把它算进去——即使 run 启动失败，这行记录也在，能追溯到"这次认领过但没启动成功"。
- `if skip_error is not None: return await self._finalize_skip(...)` 如果是重叠跳过，走 `_finalize_skip` 把这行更新成 `skipped` 并算下一次 `next_run_at`，然后返回。

> **为什么"先查重叠、再插 task_run 行"这个顺序很关键？** 注释（`service.py:91-96`）特意说明：必须在**插自己这行之前**查 `has_active_runs`，否则这行 `queued` 会把自己算成"活跃 run"，导致永远查到有活跃 run、永远跳过。这是经典的"自指陷阱"。

##### 阶段 3：桥接进入 agent runtime（关键转折）

这是整条链路最关键的一段：调度器怎么从一个"数据库里的任务定义"变成一个"真正在跑的 agent run"。入口是 `dispatch_task` 里的这一行（`service.py:120-130`）：

```python
result = await self._launch_run(
    thread_id=execution_thread_id,
    assistant_id=task.get("assistant_id"),
    prompt=task["prompt"],
    owner_user_id=task.get("user_id"),
    metadata={
        "scheduled_task_id": task["id"],
        "scheduled_task_run_id": task_run_id,
        "scheduled_trigger": trigger,
    },
)
```

逐行：

- `result = await self._launch_run(...)` `self._launch_run` 是构造函数里注入的回调（`service.py:24, 31`）。调度器**不知道**它具体是什么——它只规定了一个接口："给我 thread_id、assistant_id、prompt，你给我启动一个 run，返回 `{"run_id": ..., "thread_id": ...}`"。这种依赖注入让调度器逻辑和具体的 run 启动实现解耦，测试时可以塞个假函数。
- `thread_id=execution_thread_id` 传阶段 2 决定好的 thread。
- `assistant_id=task.get("assistant_id")` 用任务配置的 assistant（决定用哪个 agent、哪些工具）。可能为 None，走默认。
- `prompt=task["prompt"]` 把任务的 prompt 当作用户输入。
- `owner_user_id=task.get("user_id")` **多租户的关键**。定时任务是某个用户创建的，run 必须归属到这个用户，否则后续的权限检查、工具访问、记忆隔离全会错乱。
- `metadata={...}` **这是反向回写的钥匙**。把 `scheduled_task_id` 和 `scheduled_task_run_id` 塞进 run 的 metadata，run 跑完时 `handle_run_completion` 能从 `record.metadata` 把它们读出来，反向定位到是哪个定时任务的哪一次执行。没有这两个字段，run 跑完了调度器也无从得知。

那 `_launch_run` 具体是什么？看 Gateway 启动时的接线（`backend/app/gateway/app.py:261-267`）：

```python
scheduled_task_service = ScheduledTaskService(
    task_repo=app.state.scheduled_task_repo,
    task_run_repo=app.state.scheduled_task_run_repo,
    launch_run=lambda **kwargs: launch_scheduled_thread_run(app=app, **kwargs),
    poll_interval_seconds=startup_config.scheduler.poll_interval_seconds,
    lease_seconds=startup_config.scheduler.lease_seconds,
    max_concurrent_runs=startup_config.scheduler.max_concurrent_runs,
)
```

逐行：

- `scheduled_task_service = ScheduledTaskService(...)` 在 Gateway 应用启动时构造调度器服务。
- `task_repo=app.state.scheduled_task_repo` / `task_run_repo=...` 注入两个仓储（操作 `scheduled_tasks` 和 `scheduled_task_runs` 表）。
- `launch_run=lambda **kwargs: launch_scheduled_thread_run(app=app, **kwargs)` **关键接线**：把 `launch_scheduled_thread_run` 这个函数包成 lambda 塞进去。lambda 在这里的作用是**柯里化/绑定**——把 `app` 参数预先绑死，剩下的 `thread_id`/`prompt` 等由调度器在调用时传。这样调度器调 `self._launch_run(thread_id=..., prompt=...)` 时，实际执行的是 `launch_scheduled_thread_run(app=app, thread_id=..., prompt=...)`。
- `poll_interval_seconds=...` / `lease_seconds=...` / `max_concurrent_runs=...` 三个调度参数，都来自 `config.yaml -> scheduler` 配置。

##### 阶段 4：伪装成 HTTP 请求，复用 run 启动管道

这是最巧妙的设计。`launch_scheduled_thread_run`（`backend/app/gateway/services.py:773-825`）没有重新实现一套"调度专用的 run 启动逻辑"，而是**把定时任务伪装成一个 HTTP 请求**，塞进正常的 `start_run` 路径：

```python
async def launch_scheduled_thread_run(
    *,
    thread_id: str,
    assistant_id: str | None,
    prompt: str,
    request: Request | None = None,
    app: Any | None = None,
    owner_user_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if request is None:
        if app is None:
            raise ValueError("launch_scheduled_thread_run requires request or app")
        request = SimpleNamespace(
            app=app,
            headers=({INTERNAL_OWNER_USER_ID_HEADER_NAME: owner_user_id} if owner_user_id else {}),
            state=SimpleNamespace(
                user=get_internal_user(),
                auth_source=AUTH_SOURCE_INTERNAL,
            ),
            cookies={},
        )
    body = SimpleNamespace(
        assistant_id=assistant_id,
        input={"messages": [{"role": "user", "content": prompt}]},
        command=None,
        metadata=metadata or {},
        config=None,
        context=({"non_interactive": True, "user_id": owner_user_id} if owner_user_id else {"non_interactive": True}),
        webhook=None,
        checkpoint_id=None,
        checkpoint=None,
        interrupt_before=None,
        interrupt_after=None,
        stream_mode=None,
        stream_subgraphs=False,
        stream_resumable=None,
        on_disconnect="continue",
        on_completion="keep",
        multitask_strategy="reject",
        after_seconds=None,
        if_not_exists="reject",
        feedback_keys=None,
    )
    record = await start_run(body, thread_id, request)
    return {"run_id": record.run_id, "thread_id": record.thread_id}
```

逐行讲关键的几段：

**构造伪 Request**（`services.py:783-794`）：

- `if request is None:` 调度器路径不会传 `request`（它没有 HTTP 请求），只传 `app`。所以走这个分支。
- `request = SimpleNamespace(...)` **`SimpleNamespace` 是个万能的"假对象"**——它把任意属性挂在一个对象上。这里用它来伪造一个"长得像 FastAPI Request"的对象，骗过下游代码里所有 `request.app` / `request.headers` / `request.state` 的访问。
- `app=app` 把 Gateway 应用实例挂上，下游能拿到 `app.state` 里的各种仓储、bridge、配置。
- `headers={INTERNAL_OWNER_USER_ID_HEADER_NAME: owner_user_id}` **伪造内部鉴权头**。这个 header 是 Gateway 内部用来传递"谁拥有这次 run"的（见 `deps.py` 的鉴权链路）。定时任务没有外部 token，但必须标明归属用户，所以直接塞这个内部头。
- `state=SimpleNamespace(user=get_internal_user(), auth_source=AUTH_SOURCE_INTERNAL)` 伪造请求状态。`get_internal_user()` 返回一个代表"系统内部"的特殊用户；`AUTH_SOURCE_INTERNAL` 标记认证来源是内部的（不是 OAuth/SSO）。下游所有依赖 `request.state.user` 的代码（权限检查、日志、审计）都能正常工作。
- `cookies={}` 空字典占位。

注释（`services.py:795-797`）特意说明：这个 `SimpleNamespace` 模拟的是 Pydantic run-request body。如果 `start_run` 新增了对 `body.*` 某个字段的直接读取，这里得同步加上，否则调度器路径会坏。

**构造伪 body**（`services.py:798-823`）：

这是另一个 `SimpleNamespace`，模拟 HTTP 路径里 Pydantic 解析出来的请求体。每一行都值得说一下：

- `assistant_id=assistant_id` 用任务配置的 assistant。
- `input={"messages": [{"role": "user", "content": prompt}]}` **这是把 prompt 包装成标准消息格式**。agent runtime 期望的输入是 `{"messages": [...]}`，和浏览器发消息的格式完全一样。定时任务的 `prompt` 字符串被包成一条 `user` 角色的消息——对 agent 来说，和用户在聊天框打字没有任何区别。
- `command=None` 没有特殊命令（比如 `/reset` 之类）。
- `metadata=metadata or {}` 把阶段 3 传进来的 `scheduled_task_id` / `scheduled_task_run_id` 原样透传——这就是反向回写的钥匙。
- `config=None` 没有 LangGraph 运行时配置覆盖。
- `context={"non_interactive": True, "user_id": owner_user_id}` **这一行是最关键的差异点**，下面专门讲。
- `webhook=None` 没有 webhook 回调。
- `checkpoint_id=None` / `checkpoint=None` 不从历史 checkpoint 恢复，从头开始。
- `interrupt_before=None` / `interrupt_after=None` 没有人机协作的打断点。
- `stream_mode=None` / `stream_subgraphs=False` / `stream_resumable=None` 流式相关，定时任务不关心（它根本不订阅流）。
- `on_disconnect="continue"` **关键**：断线策略设为"继续跑"。定时任务没有客户端连接，所以"断线"对它没有意义——但 run 启动时会读这个字段决定行为，设成 `continue` 保证 run 不会因为"没人订阅"就被取消。对比聊天场景的默认值，这里是特意配的。
- `on_completion="keep"` run 结束后保留记录（不自动删除），方便事后查历史。
- `multitask_strategy="reject"` 同一 thread 已有 run 时拒绝（返回 409）。这和阶段 2 的重叠检查是两层防护。
- `after_seconds=None` 没有超时限制（定时任务可能跑很久）。
- `if_not_exists="reject"` thread 不存在时拒绝（而不是自动创建）——因为这里已经传了明确的 `thread_id`。
- `feedback_keys=None` 没有反馈路由配置。

**`non_interactive: True` 是什么意思**（对应 `services.py:808` 和 AGENTS.md 的说明）：

定时任务执行时，agent 的工具集会**排除 `ask_clarification`**——也就是 agent 不能反过来问用户问题。因为定时任务没有客户端在线回答，如果 agent 卡在"请问您是要 A 还是 B？"这种澄清问题上，run 会永远卡死。所以调度路径在 `context` 里塞 `non_interactive: True`，agent 框架读到这个标记后会从 lead-agent 的工具集里拿掉 `ask_clarification`。

但这里有个**安全要点**（AGENTS.md 明确强调）：客户端**伪造**的 `context.non_interactive` 会被丢弃——只有内部认证来源（`AUTH_SOURCE_INTERNAL`）的调用才允许设这个标记。这就是为什么上面伪 Request 里要特意把 `auth_source` 设成 `AUTH_SOURCE_INTERNAL`：它是 `non_interactive` 生效的前提。调度器是内部可信调用方，所以可以设；外部用户就算在请求里塞了 `non_interactive: True` 也会被框架忽略。

**进入 `start_run`**（`services.py:824-825`）：

- `record = await start_run(body, thread_id, request)` **这是统一入口**。`start_run`（`services.py:608`）是所有 run 的启动函数——无论来自浏览器 HTTP、IM 通道 HTTP、还是调度器内部调用，最终都汇聚到这里。它做的事：创建 `RunRecord`、在后台 `asyncio.create_task` 启动 agent worker（LangGraph 执行图）、返回 record。
- `return {"run_id": record.run_id, "thread_id": record.thread_id}` 把 run_id 和 thread_id 返给调度器。调度器拿到后会写进 `scheduled_task_runs` 表的对应行（阶段 5）。

> **为什么要伪装成 HTTP 请求而不是直接调 agent？** 因为 HTTP 路径上有大量横切逻辑：鉴权、context 注入、权限检查、审计、run 状态机管理。重新实现一套调度专用的就会和 HTTP 路径分叉，维护成本翻倍。用 `SimpleNamespace` 伪装请求，**复用全部 HTTP 路径代码**，保证行为一致性——定时任务和用户在聊天框发消息走的是完全相同的 agent 执行管道。

##### 阶段 5：启动后回写 + 终态回调

`dispatch_task` 启动 run 之后，还有两段回写（`service.py:131-175` 和 `service.py:252-301`）：

**启动成功后的即时回写**（`service.py:147-168`）：

```python
await self._task_run_repo.update_status(
    task_run_id,
    status="running",
    run_id=result["run_id"],
    started_at=now,
    protect_terminal=True,
)
await self._task_repo.update_after_launch(
    task["id"],
    status=task_status,
    next_run_at=next_at,
    last_run_at=now,
    last_run_id=result["run_id"],
    last_thread_id=result["thread_id"],
    last_error=None,
    increment_run_count=True,
    protect_terminal=True,
)
```

逐行：

- `update_status(task_run_id, status="running", run_id=..., started_at=now, protect_terminal=True)` 把阶段 2 插的 `queued` 行更新成 `running`，记下真正的 `run_id` 和开始时间。
- `protect_terminal=True` **这个参数非常关键**。注释（`service.py:152-154`）解释：一个快速失败的 run 可能在 `update_status` 这行代码**还没执行**的时候，就已经跑完并触发了 `handle_run_completion`（见下面），把状态写成了 `failed`。如果这行不带 `protect_terminal`，就会把 `failed` **覆盖**回 `running`——状态永久错乱。仓储层（`scheduled_task_runs/sql.py:90-99`）看到 `protect_terminal=True` 且当前状态是终态（`success`/`failed`/`skipped`/`interrupted`），就**只补全 bookkeeping 字段**（run_id、started_at），不覆盖 status 和 error。
- `update_after_launch(...)` 更新 `scheduled_tasks` 表（任务定义表）的"最近一次执行"摘要：`last_run_at`、`last_run_id`、`last_thread_id`、`next_run_at`（下一次到期时间）、`run_count += 1`。这就是"任务列表"页面上显示的那些字段。
- `task_status` 的取值（`service.py:137-146`）有讲究：
  - `once` 类型任务：启动后设成 `"running"`，**不立刻设成 completed**。注释（`service.py:137-142`）说明：要等到 `handle_run_completion` 看到真正的终态才落定。如果启动时就写 completed，万一 run 失败或进程崩了，任务会永远卡在 completed。
  - 周期任务：设成 `"enabled"`（继续按计划跑）。
  - 手动触发的暂停任务：设回 `"paused"`（手动跑一次不影响暂停状态）。

**run 结束时的终态回调**（`handle_run_completion`，`service.py:252-301`）：

这就是 3.4 开头提到的"完成回调钩子"。当 agent worker 的 `finally` 块（`worker.py:710-712`）调用 `ctx.on_run_completed(record)` 时，这个钩子被触发（接线在 `deps.py:423`）：

```python
async def handle_run_completion(self, record: RunRecord) -> None:
    metadata = record.metadata or {}
    task_id = metadata.get("scheduled_task_id")
    task_run_id = metadata.get("scheduled_task_run_id")
    user_id = record.user_id
    if not isinstance(task_id, str) or not isinstance(task_run_id, str) or not user_id:
        return
    ...
```

逐行：

- `metadata = record.metadata or {}` 取 run 的 metadata。这就是阶段 3 `dispatch_task` 塞进去的那个字典（`scheduled_task_id` / `scheduled_task_run_id`）。
- `task_id = metadata.get("scheduled_task_id")` / `task_run_id = metadata.get("scheduled_task_run_id")` 把钥匙读出来。
- `user_id = record.user_id` run 的归属用户。
- `if not isinstance(task_id, str) or ... or not user_id: return` **过滤非定时任务的 run**。如果这个 run 不是定时任务触发的（比如是用户在聊天框发的消息），metadata 里就没有这两个字段，直接 return——钩子是全局接的，但只对定时任务的 run 起作用。

之后（`service.py:260-301`）根据 `record.status` 把 run 的终态映射成 task_run 的终态：

- `record.status.value == "success"` → task_run 状态 `success`
- `record.status.value == "interrupted"` → task_run 状态 `interrupted`（用户取消或同 thread 被新 run 抢占，**区别于 failed**，不带 error）
- `record.status.value in {"error", "timeout"}` → task_run 状态 `failed`，记 error

然后更新 `scheduled_task_runs` 行（`service.py:278-284`）和 `scheduled_tasks` 行（`service.py:286-301`）。对于 `once` 类型任务，终态会落到 `completed` / `cancelled` / `failed`——一次性任务的"一生"在这里结束。

##### 整条链路的全景图

把 5 个阶段拼起来：

```
[后台轮询 _run_loop]                         ← 阶段 1：每 N 秒一次
    ↓
[run_once: 算预算 + claim_due_tasks]        ← 阶段 1：原子认领到期任务（租约）
    ↓
[dispatch_task: 决定 thread / 查重叠]        ← 阶段 2：派发准备
    ↓
[插 scheduled_task_runs 行 (queued)]         ← 阶段 2：记账
    ↓
[_launch_run → launch_scheduled_thread_run]  ← 阶段 3：桥接（依赖注入）
    ↓
[构造伪 Request + 伪 body (non_interactive)] ← 阶段 4：伪装 HTTP 请求
    ↓
[start_run → RunRecord + asyncio.create_task]← 阶段 4：进入统一 run 启动入口
    ↓
[agent worker 跑 LangGraph 图]               ← 和聊天消息走完全相同的执行路径
    ↓ (run 结束)
[worker finally: on_run_completed(record)]   ← 阶段 5：触发回调钩子
    ↓
[handle_run_completion: 读 metadata 回写]    ← 阶段 5：终态落库
```

**三个核心设计点回顾**：

1. **依赖注入解耦**：调度器只定义 `_launch_run` 接口，具体实现在 Gateway 启动时注入。这让调度器逻辑可独立测试（塞假函数），也让它不绑死在 Gateway 上。
2. **伪装复用**：用 `SimpleNamespace` 伪造 HTTP 请求，复用 `start_run` 全部代码（鉴权、context、状态机）。定时任务和聊天消息走同一条 agent 执行管道，保证行为一致。
3. **metadata 双向绑定**：启动时塞 `scheduled_task_id` / `scheduled_task_run_id` 进 run metadata，结束时 `handle_run_completion` 读出来反向回写。这是"调度账本"和"agent run"之间唯一的粘合点——**调度器全程不碰 bridge**（呼应 3.4 的结论）。

> ⚠️ 注意：定时任务的 agent 输出（消息、工具调用、流式 token）**不存在 `scheduled_task_runs` 表里**。那张表只存调度元数据（状态、时间、错误、run_id 外键）。真正的 agent 输出走的是 DeerFlow 正常的 run/thread 持久化（thread store），靠 `scheduled_task_runs.run_id` 和 `.thread_id` 关联——要看某次执行的对话内容，拿这两个 id 去 thread store 取消息。

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
