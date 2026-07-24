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

#### 3.3 IM 通道完整接入链路（IM 是怎么接入 agent runtime 的）

很多人（包括这篇文档的旧版）会以为"飞书触发的 run，飞书内部直接订阅 bridge"。这个直觉**半对半错**，坑就坑在"半对"上——它对了一半（IM 确实最终消费了 bridge 的事件流），但走的路完全不是想象中那条。这一节把 IM 从"用户在飞书里发一句话"到"agent 在飞书里回一句话"的**完整往返**逐阶段拆开讲，顺手把那个最容易搞混的点（两条名字像但毫不相干的总线）彻底厘清。

##### 先把概念用大白话讲一遍：在看那张满屏术语的全景图之前

下面这张图（"##### 0"那张）一眼看过去全是 `MessageBus` / `StreamBridge` / `ChannelManager` / `langgraph_sdk 自回环` 这种词，第一次看肯定懵。所以在展开它之前，先把这一节会反复出现的几个概念，用生活里的事打比方讲一遍——就像本文档开头"第 0 步"铺垫 SSE、async 那些概念一样。读完这段，再看图就能把每个框对上号了。

**① IM 机器人 / 通道（Channel）是什么**

你在飞书/Slack/Telegram 里 @ 一个机器人发消息，这个机器人就是 DeerFlow 的"通道"。打个比方：它像银行大堂的**迎宾机器人**——你把需求告诉它，它不会自己思考，而是转身去后台找专家（agent）帮忙，专家给出答案后，它再把答案转达给你。所以**机器人本身只是个"传话筒"**，真正的智能在 agent 那边。代码里每个平台（飞书、Slack、Telegram、GitHub…）都有一个对应的 `Channel` 子类（比如 `feishu.py` 里的 `FeishuChannel`），负责"怎么收这个平台的消息、怎么往这个平台发消息"这两件脏活。

**② 长连接 vs webhook：平台的消息怎么送到我们服务器**

这俩是"收快递"的两种姿势：

- **长连接**（飞书、Slack、Telegram、Discord、钉钉用这个）：像你**主动去快递柜取件**——程序主动连上平台，平台一有消息就顺着这条连接推过来。好处是**不需要公网 IP / 域名**，部署在公司内网也能收到消息。
- **webhook**（只有 GitHub 用这个）：像**快递员直接按你家门铃**——平台主动往你的服务器发 HTTP 请求。好处是简单，但**必须有公网能访问的地址**，还得验签防伪造。

记住这个区别，后面阶段 A 会再讲一次，但现在你已经知道"DeerFlow 大部分 IM 走长连接，所以内网也能用"。

**③ MessageBus —— 进程内的"取号机"**

这是第一条总线。把它想象成**医院的取号机 + 叫号广播**：

- 病人（用户发的消息，叫 `InboundMessage`）来了，先在取号机取号排队——代码里就是一个 `asyncio.Queue`（`message_bus.py:148` 的 `publish_inbound` 就是"把号塞进队列"）。
- 护士（`ChannelManager`）按号叫人处理（`get_inbound` 从队列里取）。
- 处理完，医生开的处方（agent 要回的消息，叫 `OutboundMessage`）通过**广播**喊给对应科室——代码里是遍历所有出站监听器（`message_bus.py:177` 的 `publish_outbound`）。

关键点：**它只活在 Gateway 这一个进程的内存里**，不跨网络、不跨机器。就像取号机的号只在一家医院有效。

**④ StreamBridge —— 跨网络边界的"事件日志"（前面讲过的主角）**

第二条总线。这个本文档前面（麻烦 2、麻烦 3）已经详细讲过——就是"每个 agent run 一份的事件存档 + 多人订阅"。这里只补一句最关键的：**它和 MessageBus 是两套完全独立的系统**，名字都带 "Bus/Bridge"、都有个 `subscribe` 方法，但它们**互不 import、互不知道对方存在**。后面 90% 的混淆都来自把这两条总线当成一个东西——所以现在先把这个雷踩明白：**两个 `subscribe`，两个不同的总线。**

**⑤ ChannelManager —— 身兼两职的"翻译员"**

这是把两条总线串起来的关键角色。把它想象成一个**会两种语言的翻译员**：

- 它的**左耳**听 IM 侧的 `MessageBus`（从取号机取消息）；
- 它的**右嘴**对 agent 说话——但注意，它对 agent 说话的方式不是直接调函数，而是**发 HTTP 请求**。

这个"翻译员"就是后面反复出现的 `ChannelManager`（`manager.py`）。整个 3.3 节本质上就是在讲："这个翻译员怎么从左边收消息、翻译成什么、再从右边递给 agent，agent 的回答又怎么原路返回。"

**⑥ HTTP 自回环（self-loopback）—— 为什么翻译员不直接调函数**

这是最反直觉的一个设计，单独拎出来讲。`ChannelManager` 明明和 agent 跑在**同一个进程**里，为什么发消息给 agent 非要走一圈 HTTP（`POST http://localhost:8001/api/...`，请求目标就是它自己）？

打个比方：**餐厅经理明明就站在餐厅里，点菜却偏要用手机在外卖 App 下单**。听着多此一举？但他这么干是有道理的——这样后厨的接单系统、记账系统、排队叫号系统全都**一视同仁地跑**，不用为"老板亲自点的单"单独开一套特殊流程。

DeerFlow 同理：IM 发起的 run 走一圈 HTTP 自回环，就能**复用浏览器的整套 run 管道**——鉴权、CSRF 校验、context 注入、`start_run` 状态机、"这个会话正在忙就拒绝"的保护，**浏览器能享受的，IM 自动也享受**。agent 根本不知道也不关心这次 run 是飞书来的还是浏览器来的。这就是后面阶段 F 要讲的核心。

---

> 小结一句话：**通道（传话筒）→ MessageBus（取号机）→ ChannelManager（翻译员）→ 走 HTTP 自回环 → 复用浏览器那套 run 管道 → StreamBridge（事件存档）→ 原路返回**。现在带着这条主线，再看下面那张全景图，每个框就能对上号了。

##### 装配篇：这套系统是怎么被启动、被接线起来的

上面那段主线讲的是"一次消息往返怎么走"。但在看全景图之前，还有一组同样重要的问题没回答：**这一大堆零件（ChannelManager、MessageBus、飞书通道）到底是谁、在什么时候、按什么顺序把它们造出来并连到一起的？** 这一节专门把"装配过程"讲透——尤其是两个贯穿全程、但前面概念铺垫里没法展开的架构事实：① `MessageBus` 是**全进程共享的单实例**，所有通道和 manager 拿的是同一个对象；② Gateway 那 `http://localhost:8001/api` 里的 `8001` 和 `/api` 到底指向哪。理解了装配，再看下面全景图里那些箭头，就会明白"它们为什么能连得上"。

---

###### A. 启动入口：谁第一次把 ChannelService 拉起来

先把"从敲下 `make dev` 到飞书通道真正连上飞书服务器"的整条启动链一次走完。这条链路上只有**一个顶层触发点**，记住它就够了。

###### A.1 顶层触发：Gateway 进程的 FastAPI lifespan

IM 子系统**不是一个独立进程**，它内嵌在 Gateway 进程里（监听 8001 端口那个）。Gateway 一启动，IM 就跟着启动。整个 channels 体系的总开关，是 Gateway 应用 lifespan 里的一行代码（`backend/app/gateway/app.py:251`）：

```python
# app.py:170-171  lifespan 是个 @asynccontextmanager
@asynccontextmanager
async def lifespan(app):
    ...
    # app.py:248-254  Start IM channel service if any channels are configured
    try:
        from app.channels.service import start_channel_service

        channel_service = await start_channel_service(startup_config)   # ★ 顶层触发
        logger.info("Channel service started: %s", channel_service.get_status())
    except Exception:
        logger.exception("No IM channels configured or channel service failed to start")
```

逐行：

- `@asynccontextmanager async def lifespan(app):` —— FastAPI 的"生命周期"钩子。它在**应用启动时执行一次**（在开始接请求之前）、**应用关闭时再执行一次收尾**。所有"进程级单例的初始化"都放这里。`lifespan` 通过 `FastAPI(..., lifespan=lifespan)`（`app.py:377`）注册，应用对象在模块底部 `app = create_app()`（`app.py:570`）创建。
- `from app.channels.service import start_channel_service` —— 延迟 import：只有真要起 channels 时才把模块加载进来，不起 channels 的进程（比如某些纯 agent worker 配置）就不背这个依赖。
- `channel_service = await start_channel_service(startup_config)` —— **★ 这就是整个 channels 体系的顶层入口**。`await` 说明它会同步等待所有启用通道的 `start()` 都返回，lifespan 才继续往下走。但 lifespan 本身跑在 uvicorn 的事件循环里，不会阻塞其它协程。
- `except Exception: logger.exception("No IM channels configured or channel service failed to start")` —— **关键设计：channels 起不来不拖垮 Gateway**。整个启动包在 try/except 里，即使飞书配置错了 / 连不上，Gateway 的 API 和前端照常工作，只是 IM 收消息功能不可用。

进程级入口（真正拉起 uvicorn 的命令）在三个地方，写法一致：

| 文件 | 命令 | 场景 |
|---|---|---|
| `backend/Makefile:5, 8` | `uvicorn app.gateway.app:app --host 0.0.0.0 --port 8001 [--reload]` | 本地 `make dev` / `make start` |
| `backend/Dockerfile:84, 116` | 同上（生产镜像去掉 `--reload`） | Docker 部署 |
| `docker/dev-entrypoint.sh:97` | 同上 | Docker 开发容器 |

`make dev` / `make start` / `make up` 最终都走这条 `uvicorn ... app.gateway.app:app`，进而触发 lifespan。

###### A.2 单例工厂：`start_channel_service`

`start_channel_service`（`backend/app/channels/service.py:410-419`）是个**模块级单例工厂**：

```python
_channel_service: ChannelService | None = None        # 402：模块级缓存变量

def get_channel_service() -> ChannelService | None:   # 405：拿单例（已起才非空）
    return _channel_service

async def start_channel_service(app_config: AppConfig | None = None) -> ChannelService:
    """Create and start the global ChannelService from app config."""
    global _channel_service
    if _channel_service is not None:                  # 已存在就直接返回（幂等）
        return _channel_service
    # from_app_config reads the JSON channel store and runtime config files;
    # keep that disk IO off the event loop.
    _channel_service = await asyncio.to_thread(       # ★ 第一次 new 在这里
        ChannelService.from_app_config,                #   通过 classmethod 构造
        app_config,
    )
    await _channel_service.start()
    return _channel_service
```

逐行：

- `_channel_service: ChannelService | None = None` —— 模块级全局变量，缓存那个唯一的 `ChannelService` 实例。
- `if _channel_service is not None: return _channel_service` —— **幂等保护**：第二次调 `start_channel_service`（比如运行时热重载）直接返回已有实例，不会重复构造。整个进程内只有一个 `ChannelService`。
- `_channel_service = await asyncio.to_thread(ChannelService.from_app_config, app_config)` —— **真正第一次实例化发生在这里**，但走的是 classmethod `from_app_config`（不是直接 `ChannelService(...)`）。注释特意说明为什么包 `asyncio.to_thread`：`from_app_config` 要读磁盘（JSON channel store、运行时配置文件），把这种 IO 丢到工作线程，不阻塞主事件循环。
- `await _channel_service.start()` —— 构造完后立刻 `start()`（见 A.4）。

###### A.3 classmethod 读配置 + 触发 `__init__`

`from_app_config`（`service.py:124-144`）负责从 `config.yaml` 的 `channels` 块读出配置，然后调 `cls(...)` 触发真正的 `__init__`：

```python
@classmethod
def from_app_config(cls, app_config: AppConfig | None = None) -> ChannelService:
    """Create a ChannelService from the application config."""
    if app_config is None:
        from deerflow.config.app_config import get_app_config
        app_config = get_app_config()
    channels_config = {}
    extra = app_config.model_extra or {}
    if "channels" in extra:
        channels_config = dict(extra["channels"] or {})        # 从 config.yaml 的 channels 块读
    ...
    return cls(                                                 # cls = ChannelService，触发 __init__
        channels_config=channels_config,
        connection_repo=_make_connection_repo(connection_config),
        require_bound_identity=require_bound_identity,
    )
```

`cls(...)` 这一行就是 `ChannelService(...)`，真正执行下面 A.4 的 `__init__`。

###### A.4 `ChannelService.__init__`：MessageBus 诞生 + Manager 装配

这是整个装配的**核心现场**（`service.py:94-122`）：

```python
def __init__(
    self,
    channels_config: dict[str, Any] | None = None,
    *,
    connection_repo: Any | None = None,
    require_bound_identity: bool = False,
) -> None:
    self.bus = MessageBus()                                     # 101 ★ MessageBus 唯一创建点
    self.store = ChannelStore()                                 # 102
    ...
    self.manager = ChannelManager(                              # 109 ★ 把 bus 注入给 manager
        bus=self.bus,
        store=self.store,
        langgraph_url=langgraph_url,
        gateway_url=gateway_url,
        default_session=...,
        channel_sessions=...,
        connection_repo=connection_repo,
        require_bound_identity=require_bound_identity,
    )
    self._channels: dict[str, Any] = {}                         # 119：name -> Channel 实例
    self._config = config
    self._running = False
    self._readiness_locks: dict[str, asyncio.Lock] = {}
```

注意装配顺序：**bus 先生 → 立刻给 manager →（后续启动时）再给各个 channel**。manager 先于任何 channel 拿到 bus，因为 manager 的 `_dispatch_loop` 要先就位，才能消费 channel 后续 publish 进来的消息。这两行（101、109）是后面 B 节"MessageBus 全局共享"的物理基础。

###### A.5 `start()`：起 dispatcher + 逐个启动通道

构造完，`start_channel_service` 立刻调 `start()`（`service.py:146-156`）：

```python
async def start(self) -> None:
    """Start the manager and all enabled channels."""
    if self._running:
        return
    await self.manager.start()                          # 151：起 ChannelManager._dispatch_loop 后台任务
    self._running = True
    ready_status = await self.ensure_ready_channels(attempts=2)   # 154：逐个启动 enabled 通道
    ready_count = sum(1 for ready in ready_status.values() if ready)
    logger.info("ChannelService started with %d/%d ready channels", ready_count, len(ready_status))
```

- `await self.manager.start()` —— 在 `ChannelManager` 里 `asyncio.create_task(self._dispatch_loop())`（`manager.py:1109`）起一个常驻后台协程，死循环地从 bus 取消息。
- `await self.ensure_ready_channels(attempts=2)` —— 遍历 `config.yaml` 里所有通道，逐个启动 `enabled: true` 的。

###### A.6 单通道启动：`_start_channel` + 飞书长连接

`ensure_ready_channels` → `ensure_channel_ready` → `_start_channel`（`service.py:311-343`），**这就是每个通道拿到 bus 的地方**，也是飞书 WS 长连接真正起飞的地方：

```python
async def _start_channel(self, name: str, config: dict[str, Any]) -> bool:
    """Instantiate and start a single channel."""
    import_path = _CHANNEL_REGISTRY.get(name)          # 313：查表，"feishu" → "app.channels.feishu:FeishuChannel"
    if not import_path:
        logger.warning("Unknown channel type")
        return False
    try:
        from deerflow.reflection import resolve_class
        channel_cls = resolve_class(import_path, base_class=None)   # 321：延迟导入类
    except Exception:
        logger.exception("Failed to import channel class")
        return False

    try:
        config = dict(config)
        config["channel_store"] = self.store
        if self._connection_repo is not None:
            config["connection_repo"] = self._connection_repo
        channel = channel_cls(bus=self.bus, config=config)   # ★ 331：实例化 channel，把共享 bus 注入
        self._channels[name] = channel                       # 332：注册到 service 的通道表
        await channel.start()                                # 333：启动（飞书这里会起 WS 线程）
        if not channel.is_running:
            self._channels.pop(name, None)
            logger.error("Channel did not enter a running state after start()")
            return False
        logger.info("Channel started")
        return True
```

逐行讲关键的：

- `_CHANNEL_REGISTRY.get(name)` —— **用"名字 → import 路径"的注册表**（`service.py:23-32`，形如 `{"feishu": "app.channels.feishu:FeishuChannel", "slack": ..., ...}`）做查表，而不是 `if name == "feishu"`。好处：加新通道只改注册表一行，不动 `_start_channel` 逻辑。
- `channel_cls = resolve_class(import_path)` —— **延迟导入**：只有真正要启动的通道才会 import 它的模块。不起飞书就不 import `lark_oapi`，启动更快、依赖更轻。
- `channel = channel_cls(bus=self.bus, config=config)` —— **★ 这一行的两个关键动作**：①通过 registry + 反射动态实例化通道类；②把 `ChannelService` 持有的那个唯一共享 `MessageBus`（连同 config）传进去。对每个启用的通道各执行一次，但每次传的 `self.bus` 是**同一个对象的引用**。这一行是 B 节"MessageBus 全局共享"的另一半。
- `await channel.start()` —— 通道真正启动。对飞书来说（`feishu.py:149` 的 `start()`），这里会 `self._main_loop = asyncio.get_event_loop()` 记下主循环，然后开一个 daemon 线程跑 `_run_ws`，里面 `lark.ws.Client(...).start()` 建立到飞书服务器的 WebSocket 长连接（详见 9 步拆解的"第 1 步"）。

###### A.7 启动的前置条件：满足才会真正起

`ensure_ready_channels`（`service.py:158-174`）对每个通道检查，飞书要真正起飞需要**同时满足**：

| 条件 | 检查点 | 不满足时 |
|---|---|---|
| `channels.feishu.enabled: true` | `service.py:164` | 跳过，打日志（默认就是 `false`，所以开箱即用时根本不起） |
| 提供了 `app_id` 和 `app_secret` | `feishu.py:191-197` | 报错并 return，不起线程 |
| 装了 `lark-oapi` 包 | `feishu.py:153-173` | `ImportError`，直接 return |

任意一个不满足，飞书通道就**静默不启动**（但不会阻断 Gateway）。配置示例（`config.example.yaml:1831-1836`）：

```yaml
channels:
  feishu:
    enabled: false                                       # 默认未启用
    app_id: $FEISHU_APP_ID
    app_secret: $FEISHU_APP_SECRET
    # domain: https://open.feishu.cn      # 中国版（默认）
    # domain: https://open.larksuite.com   # 国际版 Lark
```

###### A.8 关闭：lifespan 退出时自动收尾

Gateway 进程退出时，lifespan 的收尾阶段自动调 `stop_channel_service()`（`app.py:284-289`，带超时保护），它会对每个通道 `channel.stop()`（飞书：`join(timeout=5)` 等 WS 线程结束）+ `manager.stop()`（取消 dispatcher 后台任务）。**channels 和 Gateway 同生共死**——这正是"内嵌在 Gateway 进程里"的体现。

###### A.9 启动链路全景图

```
make dev / make start / make up
        ↓
uvicorn app.gateway.app:app --port 8001
        ↓
FastAPI lifespan(app.py:170)
        ↓
★ app.py:251  await start_channel_service(startup_config)        ← 顶层总开关
        ↓
service.py:410  start_channel_service()  （单例工厂 + asyncio.to_thread）
        ↓
service.py:124  ChannelService.from_app_config()  读 config.yaml
        ↓
service.py:94   ChannelService.__init__()
                  • self.bus = MessageBus()            (101) ← bus 诞生
                  • self.manager = ChannelManager(bus=self.bus, ...)  (109)
        ↓
service.py:146   ChannelService.start()
                  • manager.start() → _dispatch_loop 后台任务  (151)
                  • ensure_ready_channels()                   (154)
        ↓
service.py:311   _start_channel("feishu", config)
                  • channel = FeishuChannel(bus=self.bus, config)  (331) ← bus 注入给 channel
                  • await channel.start()                          (333)
        ↓
feishu.py:149   FeishuChannel.start()
                  • self._main_loop = asyncio.get_event_loop()
                  • threading.Thread(_run_ws).start()  (215) ← 飞书 WS 长连接起飞
        ↓
飞书服务器 ↔ DeerFlow（内网部署也能用，因为长连接是出站连接）
```

---

###### B. MessageBus 为什么是"全局共享单实例"

上面 A.4 和 A.6 两处注入，合起来构成一个贯穿整个 IM 子系统的核心架构事实：**`MessageBus` 在整个进程里只被 new 过一次，所有组件拿到的是同一个对象的引用。** 这一点前面概念铺垫里没法展开（那时还没讲装配），但它解释了为什么消息能在通道和 manager 之间流转。

###### B.1 唯一创建点 + 两个注入点

```
① 唯一创建（全进程只这一次）
   service.py:101     self.bus = MessageBus()
                          ↓ 同一个实例，传引用（不复制、不重新 new）
② 注入给 manager
   service.py:109     ChannelManager(bus=self.bus, ...)
   manager.py:825     self.bus = bus
                          ↓ 同一个实例，传引用
③ 注入给每个 channel（每启动一个 channel 执行一次）
   service.py:331     channel = channel_cls(bus=self.bus, config=config)
                          ↓
   feishu.py:58       super().__init__(name="feishu", bus=bus, config=config)
                          ↓
   base.py:33         self.bus = bus      ← 基类统一存成 self.bus
```

> 已经验证过：`MessageBus()` 在整个 `backend/app/` 里**只出现一次**（`service.py:101`），manager 和 channel 都从不自己 new，全部靠构造器参数接收。没有依赖注入容器、没有服务定位器——就是最朴素的"构造时传引用"。

###### B.2 所有权与共享关系图

```
ChannelService  ← 唯一所有者（self.bus）
      │
      │  self.bus 这个引用，被三处"拿着"
      │
      ├──→ ChannelManager.bus   (service.py:109 注入)
      │       │
      │       └─ _dispatch_loop 在这里 await bus.get_inbound()
      │          调 bus.publish_outbound() 广播回复
      │
      ├──→ FeishuChannel.bus    (service.py:331 注入)
      ├──→ SlackChannel.bus     (service.py:331 注入)
      ├──→ DiscordChannel.bus   (service.py:331 注入)
      ├──→ TelegramChannel.bus  (service.py:331 注入)
      └──→ DingTalkChannel.bus  (service.py:331 注入)
                │
                └─ 各自 bus.publish_inbound() 投消息进来
                   各自 bus.subscribe_outbound(cb) 收回复

         所有箭头指向 ★同一个 MessageBus 对象★
```

**`ChannelService` 是 `MessageBus` 的唯一所有者**；`ChannelManager` 和每个 `Channel` 都只是引用者。

###### B.3 为什么必须共享同一个 bus —— manager 和 channel 是"对偶角色"

bus 是个**双向枢纽**，manager 和 channel 各占一头，互为生产者/消费者：

| 角色 | 对 inbound（入站消息） | 对 outbound（agent 回复） |
|---|---|---|
| **Channel**（飞书/Slack/...） | **生产者**：`bus.publish_inbound()` | **消费者**：`bus.subscribe_outbound()` 回写 IM |
| **ChannelManager** | **消费者**：`await bus.get_inbound()` | **生产者**：`bus.publish_outbound()` 广播 |

```
飞书/Slack/Discord  ──publish_inbound──→  ┌─────────────┐  ──get_inbound──→  ChannelManager
 (生产者)                                 │  同一个 bus  │                    (消费者)
                                          │             │
飞书/Slack/Discord  ←──subscribe_outbound─ │             │ ←──publish_outbound─ ChannelManager
 (消费者)                                 └─────────────┘                    (生产者)
```

如果每个 channel 有自己的 bus，manager 就得给每个 bus 各起一个 dispatcher → N 份并发、N 套 agent 调度、N 把锁。共享一个 bus → **一个 dispatcher 统一消费** → 天然的"多入口汇聚、统一处理"漏斗，简单且避免竞争。这就是为什么 bus 必须是全局共享单实例。

###### B.4 channel 拿到 bus 之后怎么用（双向）

基类 `base.py:33` 只做了 `self.bus = bus`，不自动订阅。各通道在 `start()`/`stop()` 里主动接入（飞书 `feishu.py:204/261`）：

```python
# start() —— 订阅"出站"方向
self.bus.subscribe_outbound(self._on_outbound)
# 效果：agent 产出回复 → bus 广播 → 飞书的 _on_outbound 被调 → 回写飞书卡片

# _prepare_inbound (feishu.py:917) —— 发布"入站"方向
await self.bus.publish_inbound(inbound)
# 效果：飞书收到消息 → 塞进 bus → ChannelManager._dispatch_loop 取走 → 送给 agent
```

所以 bus 对每个通道来说是**双向枢纽**：一端 `publish_inbound` 把 IM 消息送进去，另一端 `subscribe_outbound` 接住 agent 的回复。这两个方向的完整代码逐行拆解，见下面全景图之后的"第 4 步 / 第 9 步"以及阶段 B / 阶段 J–K。

###### B.5 "全局共享"的边界

虽然叫"全局共享"，但要精确理解范围：

| 范围 | 是否共享同一个 bus |
|---|---|
| 同一个 Gateway 进程内的所有 channel + manager | ✅ 是，同一个实例 |
| 多个 Gateway 进程（多 worker 水平扩展） | ❌ 否，每个进程有自己的 bus（bus 是内存对象，不跨进程） |
| HTTP API 请求处理（前端调 Gateway） | ❌ 无关，API 请求不走 bus，直接调 LangGraph runtime |
| 定时任务（scheduler） | ❌ 无关，scheduler 直接走 run 生命周期，不经 bus（见 §3.4） |

所以"全局"是**进程级**的，不是字面意义的"全宇宙"。这也意味着：如果水平扩展 Gateway（多副本），每个副本是独立的 channels 实例——但 deer-flow 默认是单进程单 bus 设计（`--workers 1`）。对比之下，**`StreamBridge` 才是那个"可以跨进程"的总线**（Redis 模式，见麻烦 4）——这是 MessageBus 和 StreamBridge 一个本质区别。

> 一句话：**`MessageBus` 是进程级全局共享的单实例**——`ChannelService` 在 `__init__` 里 new 一次（`service.py:101`），然后通过构造器注入把同一个引用分发给 `ChannelManager`（`service.py:109`）和所有启用的 channel（`service.py:331`）。这种"单 bus 汇聚"是整个 channels 架构的核心简化点。

---

###### C. `http://localhost:8001/api` 里的 8001 和 /api 到底指向哪

阶段 F 会用到 `DEFAULT_LANGGRAPH_URL = "http://localhost:8001/api"`，说"ChannelManager 发 HTTP 请求给自己"。但这里有两个常被追问的细节没展开：① `8001` 这个端口写在哪、谁能改；② `/api` 这个前缀到底路由到什么。这一节把这两点补全。

###### C.1 8001 的源头：`GatewayConfig.port` vs 启动命令写死

8001 的**权威默认值定义**在 `backend/app/gateway/config.py:10`：

```python
class GatewayConfig(BaseModel):
    """Configuration for the API Gateway."""
    host: str = Field(default="0.0.0.0", description="Host to bind the gateway server")
    port: int = Field(default=8001, description="Port to bind the gateway server")   # ★ 权威默认
    enable_docs: bool = Field(default=True, ...)

def get_gateway_config() -> GatewayConfig:
    ...
    _gateway_config = GatewayConfig(
        host=os.getenv("GATEWAY_HOST", "0.0.0.0"),
        port=int(os.getenv("GATEWAY_PORT", "8001")),       # 允许用环境变量覆盖
        ...
    )
```

**优先级**：环境变量 `GATEWAY_PORT` > 默认值 `8001`。

**但实际启动命令里 8001 是"硬编码"的，绕过了这个配置**。看真实启动命令：

```bash
# backend/Makefile:5,8 / backend/Dockerfile:84,116 / docker/dev-entrypoint.sh:97
uvicorn app.gateway.app:app --host 0.0.0.0 --port 8001
                                          ^^^^^^^^^^
                                          这里直接写死 8001
```

也就是说：**`--port 8001` 是在启动命令行里写死的**，uvicorn 直接按这个参数监听，根本没去读 `GatewayConfig.port`。`config.py` 里的 `port: int = 8001` 更像是一份"声明/记录"，告诉代码的其他部分"Gateway 跑在 8001"——但**真正决定监听端口的是 uvicorn 的命令行参数**。

8001 在代码库里出现的位置（按作用分类）：

| 类别 | 文件:行 | 作用 |
|---|---|---|
| ★ 源头定义（`GatewayConfig`） | `backend/app/gateway/config.py:10, 23` | 端口的权威默认值（可被 `GATEWAY_PORT` 覆盖） |
| ★ 实际监听（uvicorn 命令行，写死） | `backend/Makefile:5, 8` | 本地 `make dev` / `make start` |
| | `backend/Dockerfile:84, 116` | Docker 生产镜像 |
| | `docker/dev-entrypoint.sh:97` | Docker 开发容器 |
| channels 调 agent 的默认 URL | `backend/app/channels/manager.py:49, 50` | `DEFAULT_LANGGRAPH_URL` / `DEFAULT_GATEWAY_URL` |
| 容器端口暴露 | `backend/Dockerfile:82, 113` | `EXPOSE 8001` |
| 配置示例（文档/兜底） | `config.example.yaml:1806-1818` | 注释示例 |

> **改端口的代价**：如果想把 Gateway 改成 9000，必须同时改 ①启动命令的 `--port`（Makefile/Dockerfile/entrypoint）、②Nginx 配置里 `proxy_pass http://...:8001`、③`manager.py:49,50` 的 `DEFAULT_LANGGRAPH_URL`/`DEFAULT_GATEWAY_URL`、④`config.yaml` 里 channels 的 `langgraph_url`/`gateway_url`（如果显式配过）。漏一个就会出现"channels 找不到 agent"或"前端请求到不了 Gateway"。

###### C.2 `DEFAULT_LANGGRAPH_URL` 不是服务，是字符串默认值

`manager.py:49-50` 这两个常量**不是被启动的服务**，只是字符串默认值——意思是"如果用户没在 config 里指定 agent runtime 的地址，就用这个"：

```python
DEFAULT_LANGGRAPH_URL = "http://localhost:8001/api"   # channels 调 agent 用的默认 URL
DEFAULT_GATEWAY_URL = "http://localhost:8001"         # channels 调 Gateway REST API 用的默认 URL
```

它们通过一条短的解析链进入 `ChannelManager`（`service.py:105-106`）：

```python
langgraph_url = _resolve_service_url(
    config,                          # 1. 先看 config.yaml 里 channels.langgraph_url
    "langgraph_url",
    _CHANNELS_LANGGRAPH_URL_ENV,     # 2. 再看环境变量 DEER_FLOW_CHANNELS_LANGGRAPH_URL
    DEFAULT_LANGGRAPH_URL,           # 3. 都没有 → 用默认值 "http://localhost:8001/api"
)
```

解析优先级（高到低）：

```
1. config.yaml 的 channels.langgraph_url
       ↓ (没有)
2. 环境变量 DEER_FLOW_CHANNELS_LANGGRAPH_URL
       ↓ (没有)
3. DEFAULT_LANGGRAPH_URL = "http://localhost:8001/api"   ← 兜底默认
```

**为什么默认指向 `localhost:8001`**：因为 channels 服务内嵌在 Gateway 进程里（8001 端口那个进程）。所以 manager 调 LangGraph runtime，本质是**进程自己 HTTP 调自己**——"localhost:8001" 就是自己监听的端口。

**什么时候会不是 localhost:8001 —— Docker 部署**（`config.example.yaml:1805-1818`）：

```yaml
# channels:
#   langgraph_url: http://gateway:8001/api    # Docker 里用容器名寻址
#   gateway_url: http://gateway:8001
```

在 Docker 网络里，channels 和 Gateway 可能逻辑上分属不同容器（或至少要用容器名寻址），这时就得在 config 里覆盖默认值，改成 `http://gateway:8001/api`（用 docker-compose 的服务名 `gateway`）。这就是为什么要有 `_resolve_service_url` 这套优先级解析——**本地开发用 localhost，Docker 部署用容器名**。

> 注意一个有意思的细节：既然 channels 和 LangGraph runtime 在同一个进程，manager 本可以直接 `from ... import lead_agent; await lead_agent(...)` 调函数，为什么绕一圈 HTTP？因为 manager 用的是 `langgraph_sdk` 客户端（标准 LangGraph 协议），走 HTTP 调 `/threads`、`/runs` 这些标准端点。好处是：①协议一致（前端、定时任务、channels 都用同一套 LangGraph API）；②复用 Gateway 的所有中间件（鉴权、限流、日志、CSRF）。代价就是 localhost 的一次 HTTP 往返开销（可忽略）。详见阶段 F。

###### C.3 `/api` 指向哪里 —— Gateway 的 REST API 路由命名空间

`http://localhost:8001/api` 本身不是一个具体端点，而是**所有 REST API 路由的统一前缀**。它由约 20 个 `APIRouter(prefix="/api/...")` 在 `app.py:469-543` 通过 `app.include_router(...)` 挂载拼出来：

```python
# 每个 router 文件里都这样声明前缀
router = APIRouter(prefix="/api/threads", tags=["threads"])    # threads.py:54
router = APIRouter(prefix="/api/runs", tags=["runs"])          # runs.py:24
router = APIRouter(prefix="/api", tags=["memory"])             # memory.py:14

# app.py 里逐个 include（469-543）
app.include_router(threads.router)         # → /api/threads
app.include_router(thread_runs.router)     # → /api/threads/{id}/runs  ★ channels 调的就是这个
app.include_router(runs.router)            # → /api/runs
app.include_router(memory.router)          # → /api/memory
app.include_router(skills.router)          # → /api/skills
... (约 20 个)
```

`/api` 下的完整路由地图（节选，与 IM 链路相关的标 ★）：

| 完整路径 | 模块 | 作用 |
|---|---|---|
| `/api/threads` | threads.py | 会话（thread）的 CRUD |
| ★ `/api/threads/{id}/runs/stream` | thread_runs.py | **★ 在某个会话里触发流式 agent run（channels 流式通道调的就是这个，见阶段 F）** |
| ★ `/api/threads/{id}/runs/wait` | thread_runs.py | **★ 触发阻塞 run（Slack/Discord 等 channels 调这个）** |
| `/api/runs` | runs.py | LangGraph 兼容的 runs 端点 |
| `/api/memory` | memory.py | 持久记忆查询 |
| `/api/skills` | skills.py | 技能管理 |
| `/api/agents` | agents.py | agent 配置 |
| `/api/models` | models.py | 可用 LLM 模型列表 |
| `/api/channels` | channels.py | IM 通道状态/重启 |
| `/api/channels` | channel_connections.py | 浏览器绑定 IM 账号 |
| `/api/scheduled-tasks` | scheduled_tasks.py | 定时任务 |
| `/api/v1/auth` | auth.py | 鉴权（OIDC/Keycloak） |
| `/api/webhooks/github` | github_webhooks.py | GitHub webhook 接收（需启用） |

**所以 `DEFAULT_LANGGRAPH_URL = ".../8001/api"` 就是 channels 找到 agent 运行入口的"路标"**：它加上 `/threads/{id}/runs/stream` 后命中的正是 `thread_runs.py:496` 的 `stream_run` 路由——和浏览器发消息走的是**字面意义上同一个路由**。这就是阶段 F 要讲的"复用同一套 run 管道"的物理基础。

###### C.4 配套的三个端口（别混淆）

整个服务拓扑里有三个端口，各司其职：

| 服务 | 端口 | 角色 |
|---|---|---|
| **Nginx** | `2026` | **统一公网入口**——浏览器打开的就是这个；它反向代理到前端和 Gateway |
| **Gateway API** | `8001` | FastAPI + LangGraph runtime + channels 服务（就是上面讲的那一坨） |
| **Frontend** | `3000` | Next.js Web UI |

- ❌ 浏览器**不是**开 `8001`（那是 Gateway 的 API 端口，直接访问返回 JSON，不是给浏览器用的）
- ❌ 也不是 `3000`（那是前端开发服务器，绕过了 Nginx 的路由重写）
- ✅ 浏览器开 **`2026`**（Nginx 统一入口，正确处理前端静态资源 + `/api/*` 代理）

Nginx 把浏览器请求转到 `/api` 的两条规则（2026 端口）：

```
浏览器请求 http://localhost:2026/api/langgraph/xxx
                ↓ Nginx 重写
            → http://localhost:8001/api/xxx   (Gateway)

浏览器请求 http://localhost:2026/api/xxx（非 langgraph）
                ↓ Nginx 直传
            → http://localhost:8001/api/xxx   (Gateway)
```

所以**前端通过 2026 → Nginx → 8001/api**；**channels 直接 8001/api**（因为同进程，不用绕 Nginx）。

---

###### D. 出站链路收尾：publish_outbound 之后的下一步

阶段 J/K 会详细讲"MessageBus 扇出 + 通道回写平台"。但在看全景图之前，先用一节把"manager 发出 `OutboundMessage` 之后到底发生了什么"快速串一遍——这条出站链路是装配篇里 bus 共享性的最直接体现（B 节讲了 bus 为什么共享，这一节讲共享之后消息怎么流）。

典型的一段 manager 代码长这样（节选自 `manager.py:907`，类似的 publish 点在 manager.py 里有 7 处）：

```python
await self.bus.publish_outbound(
    OutboundMessage(
        channel_name=msg.channel_name,
        chat_id=msg.chat_id,
        thread_id=thread_id,
        text=response_text,
        artifacts=artifacts,
        attachments=attachments,
        is_final=True,
        thread_ts=msg.thread_ts,
        connection_id=msg.connection_id,
        owner_user_id=msg.owner_user_id,
        metadata=_response_metadata(msg.metadata, pending_clarification=pending_clarification),
    )
)
```

**这一行不是终点，而是出站广播的起点**。它触发的是一条"广播 → 过滤 → 发送"链。

###### D.1 bus 广播：遍历所有订阅者

`MessageBus.publish_outbound`（`message_bus.py:177-190`）做的事很简单——遍历所有出站监听器，挨个 `await`：

```python
async def publish_outbound(self, msg: OutboundMessage) -> None:
    """Dispatch an outbound message to all registered listeners."""
    logger.info(
        "[Bus] outbound dispatching: channel=%s, chat_id=%s, listeners=%d, text_len=%d",
        msg.channel_name, msg.chat_id, len(self._outbound_listeners), len(msg.text),
    )
    for callback in self._outbound_listeners:        # 186 ★ 所有 channel 的回调都被调一次（广播）
        try:
            await callback(msg)
        except Exception:
            logger.exception("Error in outbound callback for channel=%s", msg.channel_name)   # 190
```

每个通道在 `start()` 时调过 `self.bus.subscribe_outbound(self._on_outbound)`（飞书 `feishu.py:204`），把自己注册进 `_outbound_listeners`。所以这里会**逐个 await 每个通道的 `_on_outbound`**——飞书、Slack、Discord……都被调一次。一个通道挂了（抛异常）只记日志、不影响别的通道被调（190 行的 `except`）。

> ⚠️ 注意是 `await` **串行**，不是并发。所以如果某个通道的回调很慢，会拖慢后续通道的回调。不过通常很快（就一个 `if` 过滤就返回）。

###### D.2 名字门过滤：`_on_outbound` 按 `channel_name` 路由

每个通道注册的回调其实是基类共享的 `_on_outbound`（`backend/app/channels/base.py:158-179`），靠**名字门**决定要不要处理：

```python
async def _on_outbound(self, msg: OutboundMessage) -> None:
    """Outbound callback registered with the bus.
    Only forwards messages targeted at this channel. ..."""
    if msg.channel_name == self.name:                # 166 ★ 只有飞书实例会处理飞书消息
        try:
            await self.send(msg)                     # 168，调子类的 send
        except Exception:
            logger.exception("Failed to send outbound message on channel %s", self.name)
            return                                   # 文本发送失败就不传附件，避免半截投递
        for attachment in msg.attachments:
            try:
                success = await self.send_file(msg, attachment)   # 175，附件单独传
                ...
```

上面 `OutboundMessage` 里带了 `channel_name=msg.channel_name`（从原始入站消息继承，比如 `"feishu"`）。所以：

- 飞书的回调看到 `msg.channel_name == "feishu"` ✅ → 发送
- Slack 的回调看到 `msg.channel_name == "feishu"` ❌ → 直接返回，啥也不干

**这就是"广播给所有通道，但只有命中的那个真发"的精准路由机制**——`MessageBus` 不知道也不关心这条消息该发给哪个平台，它只是无差别广播；每个通道自己用 `channel_name` 字符串匹配判断"是不是我的"。

###### D.3 实际发送：`send` + 附件

`base.py:168-179` 先发文本/卡片，再传附件：

```python
await self.send(msg)                       # ① 发主消息（飞书是更新卡片）
# 文本发送失败就不传附件，避免"光秃秃的文件"
for attachment in msg.attachments:
    await self.send_file(msg, attachment)  # ② 逐个传附件
```

`send` 是各通道子类自己的实现。对飞书（`feishu.py:273` 的 `send` → `_send_card_message` `feishu.py:607`），它会把之前阶段 A 建的那张"running"卡片用 `im.v1.message.patch` 更新成最终结果；收到 `is_final=True` 时还会 pop 掉 running 卡片缓存、给原消息加 "DONE" reaction。Slack / Telegram / Discord 各自的 `send` 类似，调各自平台 SDK 的发消息接口。

###### D.4 套到 OutboundMessage 的各字段

逐个看上面 `OutboundMessage` 的字段在下游怎么用：

| 字段 | 在哪用 | 作用 |
|---|---|---|
| `channel_name` | `base.py:166` 过滤 | **路由依据**：决定哪个通道处理 |
| `chat_id` | channel.send 内部 | 飞书：发到哪个会话 |
| `thread_id` | send 内部 / metadata | 关联 LangGraph 会话 ID |
| `text` | send 内部 | **回复正文** → 写进卡片 |
| `artifacts` | send 内部 | 产物引用（代码块/文件等） |
| `attachments` | `base.py:173-179` | **要上传的文件列表** |
| `is_final=True` | send 内部 | 标记这是最终回复（非中间状态） |
| `thread_ts` | send 内部 | Slack 等用的消息时间戳（回帖） |
| `connection_id` | send 内部 | 浏览器绑定身份相关 |
| `owner_user_id` | send 内部 | 归属用户（权限/隔离） |
| `metadata` | send 内部 | 透传上下文（含 `pending_clarification` 等） |

特别留意 `metadata=_response_metadata(msg.metadata, pending_clarification=pending_clarification)` 这一行——它把"是否需要追问澄清"的状态塞进 metadata，通道在 send 时可以据此调整 UI（比如飞书卡片可能显示一个"等待用户输入"的样式）。

###### D.5 这是"消息闭环"的最后一环

整个消息生命周期（**装配篇 → 全景图 → 9 步拆解 → 阶段 A–K** 讲的是同一条链的不同粒度）：

```
飞书用户发消息
   ↓ (lark WS 长连接，见第 1 步)
_on_message → publish_inbound  ──→  同一个 MessageBus  ──→  manager 取出（第 4-5 步）
   （B 节：bus 全局共享，所以能流转）                            ↓
                                            调 8001/api 跑 lead_agent（阶段 F，C 节：8001/api 指向哪）
                                                                 ↓
                                            得到 response_text
                                                                 ↓
                  ★ D 节：publish_outbound(OutboundMessage(...))（manager.py:907）
                                                                 ↓
                                bus 广播 → 飞书的 _on_outbound 命中（base.py:166 过滤）
                                                                 ↓
                                          更新卡片 + DONE 表情（feishu.py:607）
                                                                 ↓
                                          用户看到回复 ✅
```

> **装配篇的四节合起来回答了四个问题**：A. 谁启动这套系统（lifespan → start_channel_service → __init__ → start → _start_channel → 飞书 WS 线程）；B. MessageBus 为什么能流转消息（全进程共享同一个实例，manager 和 channel 是对偶角色）；C. `localhost:8001/api` 指向哪（Gateway 进程 + REST 路由命名空间前缀）；D. 出站广播怎么落到对应通道（`channel_name` 名字门过滤）。带着这四点认知，再看下面那张全景图，每个箭头"为什么连得上"就都有答案了。

##### 0. 先看全景：一次 IM 往返到底经过了哪些零件

```
  ┌──────────────┐  用户发消息    ┌─────────────────────┐
  │  飞书/Slack  │ ────────────►  │  Channel (feishu.py)│  平台长连接(WS/Socket/轮询)
  │  /TG/GitHub  │                │  _on_message        │  ★ GitHub 走 HTTP webhook
  └──────────────┘                └──────────┬──────────┘
        ▲                                    │ bus.publish_inbound
        │                                    ▼
        │                          ┌─────────────────────┐
        │  平台 API 回帖            │   MessageBus        │  ① 进程内 IM 总线
        │  (im.v1.message.patch)    │  (message_bus.py)   │     InboundMessage / OutboundMessage
        │                           └────┬───────────┬───┘
        │                                │           │ publish_outbound(扇出)
        │                                ▼           ▼
        │   ┌───────────────────┐  ┌─────────────────────────────┐
        │   │  ChannelManager   │  │  Channel._on_outbound       │
        │   │  _dispatch_loop   │  │  (if channel_name==self)    │
        │   │  → _handle_chat   │  │   → send() → 平台 API       │
        │   └────────┬──────────┘  └─────────────────────────────┘
        │            │ langgraph_sdk HTTP 自回环
        │            │ client.runs.stream/wait/create
        │            ▼
        │   ═══════════════════════════════════════════════  HTTP 边界
        │            │ POST /api/threads/{tid}/runs/stream
        │            ▼
        │   ┌─────────────────────────────────────────────┐
        │   │  Gateway: stream_run → start_run            │
        │   │   → asyncio.create_task(run_agent(...))     │
        │   └────────────────────┬────────────────────────┘
        │                        │ bridge.publish(run_id, ...)
        │                        ▼
        │                ┌─────────────────────┐
        │                │   StreamBridge      │  ② 跨 HTTP 边界的 run 事件总线
        │                │  (memory/redis.py)  │     StreamEvent, 按 run_id 路由
        │                └─────────┬───────────┘
        │                          │ bridge.subscribe (SSE)
        │                          ▼
        │          sse_consumer 把事件格式化成 SSE 帧发回去
        │                          │
        └──────────────────────────┘  SDK 在 manager 侧把 SSE 解析回 chunk
                                      （manager 再转成 OutboundMessage 发到 ①）
```

记住这张图里**两个带圈编号的总线**：①`MessageBus`（IM↔manager，进程内）和 ②`StreamBridge`（agent worker↔HTTP 订阅者，跨 HTTP 边界）。整节都在讲它们怎么通过 `ChannelManager` 这个"既是 IM 协调者、又是 HTTP 客户端"的角色串起来。

##### 0.1 逐步拆解：图里每一步对应哪段代码，逐行讲

上面那张图一共 9 步。下面把每一步单独拎出来：先说"这一步在干什么"，再贴真实代码，再逐行解释。全程以飞书为例（流式通道，链路最完整）。

---

###### 第 1 步：飞书把消息推给 `Channel._on_message`

飞书的 WebSocket 连接跑在一个**独立线程**里（不是主的 asyncio 事件循环线程）——这是因为 lark-oapi 这个 SDK 在建立连接时会把"当前的事件循环"缓存下来，如果放在主循环里建，后面回调时容易连错循环。所以 DeerFlow 专门开一个线程只跑这个 WebSocket。

飞书那边一来新消息，就同步调用（`_on_message`，`feishu.py:954`）：

```python
def _on_message(self, event) -> None:
    """Called by lark-oapi when a message is received (runs in lark thread)."""
    try:
        logger.info("[Feishu] raw event received: type=%s", type(event).__name__)
        message = event.event.message
        chat_id = message.chat_id
        msg_id = message.message_id
        sender_id = event.event.sender.sender_id.open_id
        ...
        content = json.loads(message.content)
        if "text" in content:
            text = content["text"]
        ...
```

逐行：

- `def _on_message(self, event) -> None:` —— 这是一个**普通同步函数**（没有 `async def`），因为 lark-oapi 库是用普通线程回调机制通知我们的，不是 `await` 出来的。函数开头的注释直接写明"runs in lark thread"——提醒读代码的人：这段代码运行在那个独立的 WebSocket 线程里，不是主事件循环。
- `message = event.event.message` —— `event` 是 lark-oapi 包装好的事件对象，`event.event.message` 才是真正的消息体（这是 lark SDK 自己的嵌套结构，不是 DeerFlow 定义的）。
- `chat_id = message.chat_id` —— 取出这条消息所在的会话 ID（对应飞书里的一个聊天窗口/群）。
- `msg_id = message.message_id` —— 这条消息在飞书那边的唯一 ID，后面用于"给这条消息加个 ok 表情""引用回帖"等操作。
- `sender_id = event.event.sender.sender_id.open_id` —— 发消息的人是谁（飞书用户的 open_id）。
- `content = json.loads(message.content)` —— 飞书把消息内容以 JSON 字符串的形式传过来（比如 `'{"text": "帮我写个排序算法"}'`），这里用 `json.loads` 解析成 Python 字典。
- `if "text" in content: text = content["text"]` —— 如果这个字典里有 `"text"` 键，说明是纯文本消息，直接取出文本内容。（后面还有分支处理文件、图片等类型，此处从略。）

这个函数最后会把解析结果打包成 DeerFlow 自己定义的 `InboundMessage`（对应图里"平台长连接"箭头指向的目标），然后调用下一步要讲的 `_schedule_prepare_inbound`。

---

###### 第 2 步：跨线程"传球"回主事件循环

这是最容易被忽略、但概念上很关键的一步。第 1 步的 `_on_message` 跑在**独立线程**里，但 `MessageBus` 是一个 `asyncio.Queue`——**`asyncio.Queue` 只能在它所属的那个事件循环里安全地读写**，不能跨线程直接操作。所以需要一个"把工作从线程 A 扔回事件循环 B"的机制。

`_schedule_prepare_inbound`（`feishu.py:802`）：

```python
def _schedule_prepare_inbound(
    self,
    msg_id: str,
    inbound: InboundMessage,
    *,
    source_message_ids: list[str] | None = None,
) -> None:
    if self._main_loop and self._main_loop.is_running():
        logger.info("[Feishu] publishing inbound message to bus (type=%s, msg_id=%s)", inbound.msg_type.value, msg_id)
        fut = asyncio.run_coroutine_threadsafe(
            self._prepare_inbound(msg_id, inbound, source_message_ids=source_message_ids),
            self._main_loop,
        )
        fut.add_done_callback(lambda f, mid=msg_id: self._log_future_error(f, "prepare_inbound", mid))
    else:
        logger.warning("[Feishu] main loop not running, cannot publish inbound message")
```

逐行：

- `def _schedule_prepare_inbound(...)` —— 注意这也是个**普通同步函数**，因为它是被第 1 步那个同步的 `_on_message` 直接调用的（同一个线程里，不能中途 `await`）。
- `if self._main_loop and self._main_loop.is_running():` —— `self._main_loop` 是通道启动时记下来的"主事件循环"引用（`ChannelManager` 和 `MessageBus` 都跑在这个循环里）。先检查它存在且还在跑，避免往一个已经关闭的循环里扔任务导致崩溃。
- `fut = asyncio.run_coroutine_threadsafe(coroutine, self._main_loop)` —— **这就是"跨线程传球"的关键 API**。`asyncio.run_coroutine_threadsafe` 的意思是：我现在在线程 A（WebSocket 线程），但我想让 `self._prepare_inbound(...)` 这个协程在线程 B 的事件循环（`self._main_loop`）里安全地跑起来。它会把这个协程安全地"提交"给目标循环去调度，返回一个可以在线程 A 里查询结果的 `concurrent.futures.Future` 对象（`fut`）。
- `self._prepare_inbound(msg_id, inbound, source_message_ids=source_message_ids)` —— 这是接下来真正要在主循环里执行的协程，即第 3 步。
- `fut.add_done_callback(lambda f, mid=msg_id: self._log_future_error(f, "prepare_inbound", mid))` —— 给这个 `Future` 挂一个"完成时回调"。因为 `run_coroutine_threadsafe` 提交之后，线程 A 并不会等它跑完（不会阻塞），所以如果协程内部抛了异常，没人会自动报错——必须显式挂一个回调去检查、打日志，否则异常会静默消失。

**为什么要绕这一圈？** 因为 `MessageBus`（`asyncio.Queue`）和 `ChannelManager` 的整个调度逻辑，都假设自己运行在同一个事件循环里。如果直接从 WebSocket 线程里调用 `await self.bus.publish_inbound(...)`，Python 会报错（协程只能在它所属的事件循环里跑），所以必须先用 `run_coroutine_threadsafe` 把执行权"传球"过去，再在目标循环里正常 `await`。

---

###### 第 3 步：`_prepare_inbound` 把消息交给 `MessageBus`

`_prepare_inbound`（`feishu.py:909`）现在已经安全地跑在主事件循环里了。它先做一些收尾工作（给这条消息加个"收到了"的表情、建一张"处理中"的占位卡片），然后：

```python
await self.bus.publish_inbound(inbound)      # feishu.py:917
```

逐行：

- `await self.bus.publish_inbound(inbound)` —— 现在是在正确的事件循环里，可以正常 `await` 了。把打包好的 `InboundMessage` 对象交给 `MessageBus`。

**到这一步为止，agent runtime 还完全没有被碰**——消息只是从"飞书的线程"安全转移到了"主事件循环"，还没有进入 agent 的任何处理逻辑。

---

###### 第 4 步：`MessageBus.publish_inbound` —— 消息进队列

看总线这一侧的代码（`message_bus.py:148`）：

```python
async def publish_inbound(self, msg: InboundMessage) -> None:
    """Enqueue an inbound message from a channel."""
    await self._inbound_queue.put(msg)
    logger.info(
        "[Bus] inbound enqueued: channel=%s, chat_id=%s, type=%s, queue_size=%d",
        msg.channel_name,
        msg.chat_id,
        msg.msg_type.value,
        self._inbound_queue.qsize(),
    )
```

逐行：

- `async def publish_inbound(self, msg: InboundMessage) -> None:` —— 一个协程方法，`msg` 就是上一步打包好的 `InboundMessage`。
- `await self._inbound_queue.put(msg)` —— `self._inbound_queue` 是 `MessageBus.__init__` 里创建的 `asyncio.Queue()`（对照 `message_bus.py:134` 那个类定义）。`put` 把消息塞进队列尾部。这是个**无界队列**——没有设置容量上限，所以永远不会阻塞在这里（这也是文档前面表格里"重放/心跳：无（无界 `asyncio.Queue`，取走即消失）"这句话的来源：队列只负责"排队"，不负责"留存历史"）。
- 后面的 `logger.info(...)` 只是打日志，记录一下现在队列里堆了多少条消息（`qsize()`），方便排查"消息是不是堆积了"这种问题。

到这里，`MessageBus` 的"入队"这一半就完成了。消息现在安静地躺在队列里，等着被下一步的"消费者"取走。

---

###### 第 5 步：`ChannelManager._dispatch_loop` 把消息取出来

`ChannelManager` 启动时会起一个**常驻的后台协程**，专门死循环地从队列里取消息（`manager.py:1126`）：

```python
async def _dispatch_loop(self) -> None:
    logger.info("[Manager] dispatch loop started, waiting for inbound messages")
    while self._running:
        try:
            msg = await asyncio.wait_for(self.bus.get_inbound(), timeout=1.0)
        except TimeoutError:
            continue
        except asyncio.CancelledError:
            break

        if self._is_duplicate_inbound(msg):
            continue
        logger.info(
            "[Manager] received inbound: channel=%s, chat_id=%s, type=%s, text_len=%d, files=%d",
            msg.channel_name,
            msg.chat_id,
            msg.msg_type.value,
            len(msg.text or ""),
            len(msg.files),
        )
        task = asyncio.create_task(self._handle_message(msg))
        task.add_done_callback(self._log_task_error)
```

逐行：

- `while self._running:` —— 只要通道管理器没被要求停止，就一直循环。这是个典型的"后台常驻任务"写法。
- `msg = await asyncio.wait_for(self.bus.get_inbound(), timeout=1.0)` —— `self.bus.get_inbound()`（对应 `message_bus.py:159` 的 `get_inbound`，内部就是 `await self._inbound_queue.get()`）会**阻塞等待**，直到队列里有消息可取。外面包一层 `asyncio.wait_for(..., timeout=1.0)` 的意思是"最多等 1 秒，等不到就算了，不要一直死等"——这样即使一直没有新消息，循环也能每秒醒一次，去检查 `self._running` 有没有变成 `False`（否则等 `stop()` 调用时，这个协程可能永远卡在 `get_inbound()` 里退不出去）。
- `except TimeoutError: continue` —— 1 秒内没等到消息，正常现象，直接进入下一轮循环（再等 1 秒）。
- `except asyncio.CancelledError: break` —— 如果这个协程被取消了（比如程序正常关闭），跳出循环，让函数正常结束。
- `if self._is_duplicate_inbound(msg): continue` —— **去重**。IM 平台经常会把同一条消息重复推送（网络抖动、平台自己重试），这里按"通道 × 会话 × 去重元数据 × 归属用户"算一个 key，10 分钟内重复的直接丢弃，不进入后面的处理。
- `task = asyncio.create_task(self._handle_message(msg))` —— **关键设计**：不是直接 `await self._handle_message(msg)`，而是用 `asyncio.create_task` 把处理这条消息的工作包成一个**独立的后台任务**扔出去，`_dispatch_loop` 自己立刻回到循环顶部去取下一条消息。这样即使第一条消息的 agent 要跑 30 秒，`_dispatch_loop` 也不会被卡住，可以马上去处理第二条消息（真正的并发数由后面 `_handle_message` 内部的信号量控制，见下一步）。
- `task.add_done_callback(self._log_task_error)` —— 和第 2 步同理：`create_task` 创建的任务如果内部抛异常，没人主动 `await` 它就不会被发现，所以也要挂一个回调专门记录错误。

`_handle_message` 内部（`manager.py:1241`）会先做鉴权/去重，再用一个**最大并发数为 5 的信号量**（`async with self._semaphore`）限制"同时处理中的消息数"，防止 5 个以上用户同时发消息把 agent runtime 一下子打爆。之后会解析出 LangGraph 的 `thread_id`（一个飞书话题对应一个 thread），最终走到 `_handle_chat_on_thread` 的三路分流（正文"阶段 E"已经讲过分流逻辑），飞书命中**流式**这一路，也就是下面第 6 步的 `_handle_streaming_chat`。

---

###### 第 6 步：`_handle_streaming_chat` 发起 HTTP 自回环

这是全图**从"IM 世界"跨进"agent runtime 世界"**的临界点（`manager.py:1712`，节选）：

```python
async def _handle_streaming_chat(
    self,
    client,
    msg: InboundMessage,
    thread_id: str,
    assistant_id: str,
    run_config: dict[str, Any],
    run_context: dict[str, Any],
    human_message: dict[str, Any],
    storage_user_id: str | None = None,
) -> None:
    logger.info("[Manager] invoking runs.stream(thread_id=%s, text_len=%d)", thread_id, len(msg.text or ""))

    stream_kwargs: dict[str, Any] = {
        "input": {"messages": [human_message]},
        "config": run_config,
        "context": run_context,
        "stream_mode": list(STREAM_MODES),
        "multitask_strategy": "reject",
    }

    async for chunk in client.runs.stream(
        thread_id,
        assistant_id,
        **stream_kwargs,
    ):
        ...
```

逐行：

- 函数签名里的 `client` —— 这是 `langgraph_sdk` 的 HTTP 客户端实例，本质上是一个对 `httpx` 的封装，它发出的每一次调用最终都是一次真正的 HTTP 请求。
- `stream_kwargs = {"input": ..., "config": run_config, "context": run_context, ...}` —— 把这次对话要传给 agent 的所有参数（用户说的话、运行配置、上下文）打包成一个字典，等下要作为 HTTP 请求体发出去。
- `"multitask_strategy": "reject"` —— 前面正文提到过，这个参数的意思是"这个 thread 上如果已经有 run 在跑，就直接拒绝"，用来保证同一个会话的消息严格排队处理。
- `async for chunk in client.runs.stream(thread_id, assistant_id, **stream_kwargs):` —— **这一行就是图上"HTTP 自回环"发生的地方**。`client.runs.stream(...)` 在 SDK 内部做的事情，本质上等价于：

  ```
  POST http://localhost:8001/api/threads/{thread_id}/runs/stream
  Body: {"input": {...}, "config": {...}, "context": {...}, "multitask_strategy": "reject", ...}
  ```

  也就是说，`ChannelManager` 虽然和 agent 跑在**同一个 Python 进程**里，但它没有直接 `import` agent 的代码去调用函数，而是发了一个真正的、完整的 HTTP 请求，目标地址是它自己所在的服务（`localhost:8001`）。`client.runs.stream(...)` 返回的是一个**异步迭代器**——请求发出后，SDK 会持续从 HTTP 响应里读 SSE 事件，每读到一个就产出（`yield`）一个 `chunk` 对象，`async for` 逐个接住它们。这一行也正是本文档第 0.6 节讲过的"异步生成器"用法的又一个例子：只不过这次的生成器藏在 SDK 内部，我们只看到调用方在 `async for` 里消费。

**为什么明明在同一个进程里，非要多此一举发 HTTP？** 正文前面用"经理自己在餐厅里，点菜却要用外卖 App 下单"打过比方——这样做能让飞书发起的这次 run，和浏览器发起的 run 走**一模一样的入口**：同一个 `POST /{tid}/runs/stream` 路由、同一套鉴权/CSRF/并发保护/状态机。agent 完全不知道、也不需要知道这次请求是飞书发来的还是浏览器发来的。

---

###### 第 7 步：请求命中 Gateway 路由，`start_run` 把 agent 丢进后台任务

上一步发出的 HTTP 请求，命中的正是 `thread_runs.py:496` 这个路由（`thread_runs.py:496-521`）：

```python
@router.post("/{thread_id}/runs/stream")
@require_permission("runs", "create", owner_check=True, require_existing=True)
async def stream_run(thread_id: str, body: RunCreateRequest, request: Request) -> StreamingResponse:
    bridge = get_stream_bridge(request)
    run_mgr = get_run_manager(request)
    record = await start_run(body, thread_id, request)

    return StreamingResponse(
        sse_consumer(bridge, record, request, run_mgr),
        media_type="text/event-stream",
        headers={...},
    )
```

逐行：

- `@router.post("/{thread_id}/runs/stream")` —— FastAPI 装饰器，声明这个函数处理 `POST /api/threads/{thread_id}/runs/stream`。浏览器发消息走的也是这同一个路由——这就是"复用同一套管道"字面意义上的体现。
- `bridge = get_stream_bridge(request)` / `run_mgr = get_run_manager(request)` —— 从 FastAPI 应用状态里取出全局唯一的 `StreamBridge` 实例和 `RunManager` 实例（这两个是进程级单例，所有请求共享）。
- `record = await start_run(body, thread_id, request)` —— **这一行是关键**，下面单独展开。它创建一次"运行记录"（`RunRecord`），并且**在内部把 agent 的实际执行包装成一个独立的后台任务**——注意此时这个 HTTP 请求处理函数本身还没有返回响应。
- `return StreamingResponse(sse_consumer(bridge, record, request, run_mgr), ...)` —— 返回一个 SSE 流式响应，响应体的内容由 `sse_consumer`（本文档"麻烦 1"整节逐行讲过的那个函数）产出——它会去订阅 `StreamBridge`，把收到的事件格式化成 SSE 文本吐出去。

`start_run` 内部（`services.py:608`）做了很多事（鉴权、幂等、构造 config），其中和这张图直接相关的核心两行在 `services.py:746-761`：

```python
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
```

逐行：

- `task = asyncio.create_task(run_agent(...))` —— 和第 5 步 `_dispatch_loop` 里那次 `create_task` 是同一种手法：把 `run_agent(...)` 这个协程（真正驱动 LangGraph 图执行、一步步跑 agent 的那个函数）包装成一个**独立的后台任务**，立刻扔出去执行，不在这里 `await` 它跑完。
- `record.task = task` —— 把这个任务对象保存到 `RunRecord` 上，方便后续（例如客户端断线时）可以找到它、取消它。

**这就是文档最开头强调的解耦点**：`stream_run` 这个 HTTP 处理函数，在 `start_run` 返回之后就可以立刻把 `StreamingResponse` 返回给调用方了（也就是第 6 步那个自回环 HTTP 请求），而 `run_agent` 在另一个独立的后台任务里继续跑——两者的生死不再绑在一起。

---

###### 第 8 步：`run_agent` 一边跑 LangGraph 图，一边往 `StreamBridge` 里发事件

`run_agent`（`packages/harness/deerflow/runtime/runs/worker.py:246`）内部会调用 LangGraph 图的 `agent.astream(...)`（就是本文档"第 1 步：朴素写法"里出现过的那个方法），每产出一个 chunk 就发布一次事件（`worker.py:496-502`，节选）：

```python
async for chunk in agent.astream(input_payload, config=stream_config, stream_mode=single_mode):
    if record.abort_event.is_set():
        logger.info("Run %s abort requested — stopping", run_id)
        break
    sse_event = _lg_mode_to_sse_event(single_mode)
    await bridge.publish(run_id, sse_event, serialize(chunk, mode=single_mode))
```

逐行：

- `async for chunk in agent.astream(...):` —— 和文档"第 1 步"讲过的用法一样：LangGraph 图一步步执行，每产出一点新内容（模型吐的字、工具调用结果等）就交出一个 `chunk`。
- `if record.abort_event.is_set(): break` —— 每收到一个 chunk 前，先检查这个 run 有没有被显式要求中止（比如用户点了停止按钮）。这是本文档"麻烦 1"里讲的"主动检查"模式在生产者这一侧的对应实现。
- `sse_event = _lg_mode_to_sse_event(single_mode)` —— 把 LangGraph 的内部模式名（比如 `"messages"`）转换成 SSE 事件类型名。
- `await bridge.publish(run_id, sse_event, serialize(chunk, mode=single_mode))` —— **这就是图上"② StreamBridge"收到事件的地方**。`bridge.publish` 把这个事件追加写进 `run_id` 对应的那份事件日志里（对照本文档"麻烦 2"讲过的 `_RunStream.events` 列表和 `_next_id` 生成 ID 的逻辑），并且唤醒所有正在订阅这个 `run_id` 的人（`asyncio.Condition.notify_all()`，对照"第 0.6 节"）。

订阅者是谁？就是第 7 步 `stream_run` 里那个 `sse_consumer(bridge, record, request, run_mgr)`——它正 `async for entry in bridge.subscribe(record.run_id, ...)` 地等在那儿（这部分逐行解释见本文档"麻烦 1"一节，此处不重复），一收到新事件就格式化成 SSE 文本，写回给第 6 步发起的那个 HTTP 请求的响应体里。

---

###### 第 9 步：SDK 在 manager 侧把 SSE 解析回 chunk，再发回 IM

回到第 6 步的 `_handle_streaming_chat`——它的 `async for chunk in client.runs.stream(...)` 循环，此刻正持续收到第 8 步发出的 SSE 事件（SDK 内部已经帮它把 SSE 文本解析回结构化的 `chunk` 对象）。循环体把文本攒起来，攒够一定量就发布一次 outbound（`manager.py:1765-1788`，节选）：

```python
if not latest_text or latest_text == last_published_text:
    continue

display_text = latest_text + " ▉"
await self.bus.publish_outbound(
    OutboundMessage(
        channel_name=msg.channel_name,
        chat_id=msg.chat_id,
        thread_id=thread_id,
        text=display_text,
        is_final=False,
        ...
    )
)
last_published_text = latest_text
```

逐行：

- `if not latest_text or latest_text == last_published_text: continue` —— 如果这次没有新文本，或者文本和上次发过的一模一样，就跳过，不重复发送（避免刷屏）。
- `display_text = latest_text + " ▉"` —— 给当前已经生成的文本加一个"光标"符号 `▉`，让用户在 IM 里看到"正在输入"的效果。
- `await self.bus.publish_outbound(OutboundMessage(...))` —— **注意这里又把消息发回了 `MessageBus`**（图上"①"那个总线），而不是直接调用发送平台消息的函数。`is_final=False` 表示"这是中途更新，还没完"。
- `last_published_text = latest_text` —— 记住这次发过的文本，供下一轮循环比较用。

`MessageBus.publish_outbound`（`message_bus.py:177`）会把这条 `OutboundMessage` **广播**给所有注册过的"出站监听器"。每个通道在启动时都注册了自己的监听器（对应 `base.py:158` 的 `_on_outbound`）：

```python
async def _on_outbound(self, msg: OutboundMessage) -> None:
    if msg.channel_name == self.name:
        try:
            await self.send(msg)
        except Exception:
            logger.exception("Failed to send outbound message on channel %s", self.name)
            return

        for attachment in msg.attachments:
            ...
```

逐行：

- `if msg.channel_name == self.name:` —— **这就是"广播"的落地逻辑**：`MessageBus` 不知道、也不关心这条 outbound 消息该发给哪个平台，它只是把消息推给**所有**注册过的通道；每个通道自己判断"这条消息的 `channel_name` 是不是我"，不是自己的就直接忽略（函数体其余部分不执行）。飞书通道只处理 `channel_name == "feishu"` 的消息，Slack 通道只处理 `channel_name == "slack"` 的消息，以此类推。
- `await self.send(msg)` —— 是自己的消息，调用这个通道具体实现的 `send` 方法（飞书这里就是调用飞书开放平台的"更新卡片"API，即图最左边那条 `▲ 平台 API 回帖 (im.v1.message.patch)` 箭头），把文本真正发回用户所在的 IM 界面。
- 后面的 `for attachment in msg.attachments:` —— 如果这条消息还带了文件附件，再挨个尝试上传。

**至此整个闭环走完**：用户在飞书发的一句话，经过"通道 → ①MessageBus → ChannelManager → HTTP 自回环 → Gateway → 后台 task 跑 LangGraph → ②StreamBridge → SSE 订阅 → manager 解析 chunk → ①MessageBus 广播 → 通道发平台 API"，最终又变成飞书卡片上跳动的文字，回到了用户眼前。

---

##### 1. 先厘清最容易搞混的点：两条总线，各管各的

代码里有两套名字相近、但**完全不互相 import** 的消息系统。90% 的混淆来自把它们当成一个东西。

**`MessageBus`**（`backend/app/channels/message_bus.py:134`）——进程内的异步 pub/sub，连接 **IM 通道 ↔ ChannelManager**。一个 `asyncio.Queue` 装入站消息，一个回调列表指出站消息。它只活在 Gateway 进程里，不跨进程、不跨 HTTP。

**`StreamBridge`**（`backend/packages/harness/deerflow/runtime/stream_bridge/base.py:37`）——本文档主角，连接 **agent worker ↔ HTTP SSE 订阅者**。每个 run 一份事件日志，支持 `Last-Event-ID` 重放、心跳、多订阅者 fan-out、跨进程（Redis 模式）。

| 维度 | `MessageBus` | `StreamBridge` |
|---|---|---|
| 定义位置 | `app/channels/message_bus.py:134` | `runtime/stream_bridge/base.py:37`（抽象） |
| 数据单元 | `InboundMessage` / `OutboundMessage`（整条 IM 消息） | `StreamEvent(id, event, data)`（一个 run 的一帧） |
| 按 what 路由 | `channel_name`（字符串匹配，飞书/Slack/…） | `run_id`（每个 agent run 一份日志） |
| 生产者 | Channel（入站）/ ChannelManager（出站） | agent worker（`run_agent`） |
| 消费者 | ChannelManager（入站）/ Channel._on_outbound（出站） | gateway SSE 端点（`sse_consumer` / `wait_for_run_completion`） |
| 重放/心跳 | 无（无界 `asyncio.Queue`，取走即消失） | 有（有界滑动窗口 + `Last-Event-ID` 重放 + 心跳） |
| 跨进程 | 否 | 取决于实现（Memory 否 / Redis 是） |

> ⚠️ 这就是为什么通道代码里那些 `bus.subscribe_outbound(...)`（`bus` 是 `MessageBus`）看着像在"订阅流"，其实和 `bridge.subscribe`（`bridge` 是 `StreamBridge`）八竿子打不着——两个 `subscribe`，两个不同的总线。下面凡是说"订阅者"，除非明确带 bridge，否则都指 `MessageBus` 的出站回调。

**关键结论**（呼应旧版的那句话）：IM 通道**从不导入 `StreamBridge`，从不调 `bridge.subscribe`**。它和浏览器一样，是 StreamBridge 的**远程 HTTP 订阅者**——只不过这个"HTTP 请求"是 ChannelManager 在 Gateway 同进程里发出的自回环。下面把这条自回环怎么走、在哪儿接到 agent runtime，一步步拆开。

##### 2. 能力分流：IM 接入 agent 的三条路径

不是所有 IM 通道都用同一种方式接 agent。Gateway 按通道能力分三条路。先看能力开关（`backend/app/channels/manager.py:87-96`）：

```python
CHANNEL_CAPABILITIES = {
    "dingtalk": {"supports_streaming": False},
    "discord":  {"supports_streaming": False},
    "feishu":   {"supports_streaming": True},
    "github":   {"supports_streaming": False},
    "slack":    {"supports_streaming": False},
    "telegram": {"supports_streaming": True},
    "wechat":   {"supports_streaming": False},
    "wecom":    {"supports_streaming": True},
}
```

`supports_streaming: True` 的通道（飞书 / Telegram / 企业微信）会走流式路径，边跑边把字吐回 IM；其余走阻塞或发后即忘。⚠️ 有个动态例外：钉钉的 `supports_streaming` 在运行时被覆盖成 `bool(self._card_template_id)`（`dingtalk.py:142`）——配了 AI 卡片模板才流式，否则阻塞。

这三条路最终都汇聚到同一个 `start_run`，区别只在"怎么调、调哪个端点、run 跑完怎么把结果送回 IM"：

| 路径 | SDK 调用（manager 侧） | 命中的 HTTP 端点 | 用哪些通道 | 出站行为（结果怎么回 IM） |
|---|---|---|---|---|
| **流式** | `client.runs.stream` (`manager.py:1744`) | `POST /{tid}/runs/stream` (`thread_runs.py:496`) | feishu / telegram / wecom /（dingtalk 条件） | 边跑边发多条 `OutboundMessage(is_final=False)`，最后一条 `is_final=True` |
| **阻塞** | `client.runs.wait` (`manager.py:1659`) | `POST /{tid}/runs/wait` (`thread_runs.py:524`) | slack / discord / wechat 等 | run 跑完发**一条** `OutboundMessage(is_final=True)` |
| **发后即忘** | `client.runs.create` (`manager.py:1644`) | `POST /{tid}/runs` (`thread_runs.py:488`) | github | **不发 outbound**——agent 在 sandbox 里自己用 `gh` 回帖 |

**为什么 GitHub 走发后即忘？** 注释（`manager.py:1626-1637`）说得很直白：GitHub agent 是自主长任务（改代码、提 PR），用 `runs.wait` 会被 SDK 的 `httpx.ReadTimeout`（300 秒）截断，然后 manager 还会误发一条"内部错误"出站。改用 `runs.create`——一个 run 一进 `pending` 就返回的短 POST——就避开了超时；而 agent 的回复本来就是在 sandbox 里直接调 `gh` 写到 issue/PR 上的，不需要 manager 再 ferry 回去。`ConflictError`（同 thread 已有 run）仍然由 `start_run` 同步抛出，所以"线程忙"的提示不受影响。

这三条路的分流代码就在 `_handle_chat_on_thread`（`manager.py:1604-1663`），下面阶段 E 会贴出来逐行讲。

##### 3. 入站链路：IM → agent，逐阶段拆解

以飞书为例（它是流式通道，链路最完整；GitHub/Slack 的差异会在对应阶段点出）。

###### 阶段 A：平台消息到达通道

**先纠正一个常见误解**：DeerFlow 的 IM 入口**不全是 HTTP webhook**。只有 GitHub 用 FastAPI webhook 路由（`POST /api/webhooks/github`，`backend/app/gateway/routers/github_webhooks.py:172`），进去先做 HMAC-SHA256 验签（`_verify_signature:101`，常量时间比较 `X-Hub-Signature-256`），验过才 `fanout_event` 把事件转成 `InboundMessage`。

飞书 / Slack / Telegram / Discord / 钉钉 都**不走自家 HTTP webhook**，而是用各平台提供的**长连接**（飞书 lark-oapi WebSocket、Slack Socket Mode、Telegram long-polling、Discord gateway、钉钉 stream）。好处是不需要公网 IP/域名，部署在内网也能收消息。

飞书的具体入口：lark WebSocket 在独立线程里跑（`feishu.py:218` 的 `_run_ws`，之所以单独开线程是因为 lark-oapi 在构造时会缓存当前事件循环），收到消息触发注册的回调 `_on_message`（`feishu.py:954`）：

```python
def _on_message(self, ...):
    # ... 解析 event.event.message，json.loads(message.content)
    inbound = self._make_inbound(...)        # base.py:135，构造 InboundMessage
    self._schedule_prepare_inbound(inbound)  # 跨线程 marshal 回主循环
```

`_schedule_prepare_inbound`（`feishu.py:802`）用 `asyncio.run_coroutine_threadsafe(..., self._main_loop)` 把后续工作从 WebSocket 线程扔回 ChannelManager 所在的主事件循环——因为 `MessageBus` 和 `langgraph_sdk` 客户端都必须在主循环里用。

###### 阶段 B：通道把入站消息丢进 MessageBus

`_prepare_inbound`（`feishu.py:909`）在主循环里跑，做完收尾（加"OK" reaction、建"running"卡片占位）后：

```python
await self.bus.publish_inbound(inbound)      # feishu.py:917
```

这一行就把消息交给了 `MessageBus`。看总线这头（`message_bus.py:148`）：

```python
async def publish_inbound(self, msg: InboundMessage) -> None:
    await self._inbound_queue.put(msg)       # 进 asyncio.Queue
```

`InboundMessage`（`message_bus.py:32-70`）的关键字段：`channel_name`（"feishu"，后续出站按它路由）、`chat_id` / `topic_id`（一个飞书话题 ↔ 一个 DeerFlow thread）、`text`、`owner_user_id`（连接归属用户，多租户用）、`files`、`metadata`（可带 `assistant_id` 指定用哪个 agent）。

**到这里为止，agent runtime 还完全没被碰——消息只是在 IM 侧的 `MessageBus` 里排队。**

###### 阶段 C：ChannelManager 消费入站

`ChannelManager.start()`（`manager.py:1103`）起一个后台任务跑 `_dispatch_loop`（`manager.py:1126`）：

```python
async def _dispatch_loop(self):
    while not self._stop.is_set():
        msg = await self.bus.get_inbound()          # manager.py:1130，阻塞等下一条
        if self._is_duplicate_inbound(msg): ...     # 去重（channel×chat×metadata×owner，TTL 10 分钟）
        async with self._semaphore:                 # 并发上限 max_concurrency=5
            await self._handle_message(msg)
```

逐行：

- `await self.bus.get_inbound()` 从 `MessageBus` 的队列里取一条（对应阶段 B 的 `put`）。
- `_is_duplicate_inbound`（`manager.py:1156`）做幂等去重——IM 平台常常重投递同一条消息（网络抖动/重试），这里用 `channel_name × chat_id × 去重 metadata × owner` 做键，TTL 10 分钟内重复的直接丢。
- `async with self._semaphore`（`manager.py:1255`，大小 5）限并发，防止 5 个飞书用户同时发消息就把 agent runtime 撞爆。每条入站起一个 `_handle_message` 任务串行走完。

###### 阶段 D：解析线程 + 运行参数

`_handle_message`（`manager.py:1241`）做完身份/权限校验，分流 COMMAND（`/reset` 这类）和 CHAT，CHAT 走 `_handle_chat`（`manager.py:1504`）：

- get-or-create LangGraph thread：`_get_or_create_thread`（`manager.py:1455`），没有就 `client.threads.create(...)`（`manager.py:1406`）——这又是一次 HTTP 自回环到 `POST /api/threads`。飞书按 `topic_id` 映射，保证一个话题对应一个 thread；GitHub 用确定性的 `preferred_thread_id`，让 `(repo, number)` 永远落到同一个 thread。
- 调 `_handle_chat_on_thread`（`manager.py:1563`），里面先用 `_resolve_run_params`（`manager.py:921`）算出三元组：`assistant_id`（默认 `"lead_agent"`）、`run_config`（含 `configurable.thread_id`）、`run_context`（带 `channel_name`、`user_id`、`channel_user_id`）。

###### 阶段 E：三路分流（本节的核心转折）

`_handle_chat_on_thread` 在准备好输入后，按通道能力三路分流（`manager.py:1604-1663`）：

```python
if self._channel_supports_streaming(msg.channel_name):           # 1604
    await self._handle_streaming_chat(...)                        # 流式：client.runs.stream
    return

run_kwargs = {"input": {"messages": [human_message]},
              "config": run_config, "context": run_context,
              "multitask_strategy": "reject"}                     # 1617-1622

if policy is not None and policy.fire_and_forget:                 # 1626  发后即忘（GitHub）
    await client.runs.create(thread_id, assistant_id, **run_kwargs)  # 1644
    return

result = await client.runs.wait(thread_id, assistant_id, **run_kwargs)  # 1659  阻塞
```

逐行讲三路的差别：

- `self._channel_supports_streaming(...)`（`manager.py:853`）先看通道实例的 `supports_streaming` 属性（钉钉的动态值就在这生效），查不到再回退 `CHANNEL_CAPILITIES`。为真就走 `_handle_streaming_chat`（阶段 H–K 详讲）。
- `multitask_strategy="reject"`——**所有三路都带这个**。意思是"这个 thread 上如果已经有 run 在跑，就拒绝"。这个 kwarg 会一路传到 `start_run`，在那里变成 `ConflictError` → HTTP 409，manager 侧 `_is_thread_busy_error`（`manager.py:198`）识别后给用户回一句"这个会话正在处理上一条消息"（`THREAD_BUSY_MESSAGE`，`manager.py:72`）。这就是 IM 侧的"一个会话串行处理"保障。
- `policy.fire_and_forget`（GitHub）→ `client.runs.create`：一个短 POST，run 一进 `pending` 就返回，manager 不等它跑完。
- 默认（Slack/Discord 等）→ `client.runs.wait`：阻塞到 run 跑完，拿到最终状态。

###### 阶段 F：langgraph_sdk 内部 = 一次 HTTP 自回环（IM 接入 agent runtime 的物理接合点）

**这是整条入站链路最关键的一步**：上面那些 `client.runs.stream/wait/create` 不是什么特殊协议，它们就是 `langgraph_sdk` 发的 HTTP POST。看客户端怎么构造（`_get_client`，`manager.py:1081-1094`）：

```python
self._client = get_client(
    url=self._langgraph_url,                         # http://localhost:8001/api
    headers={
        **create_internal_auth_headers(),            # 内部鉴权头（标记可信内部调用）
        CSRF_HEADER_NAME: self._csrf_token,          # CSRF 双提交：header + cookie
        "Cookie": f"{CSRF_COOKIE_NAME}={self._csrf_token}",
    },
)
```

逐行：

- `url=self._langgraph_url`——默认 `http://localhost:8001/api`（`manager.py:50` 的 `DEFAULT_LANGGRAPH_URL`）。**注意这个 URL 指向 Gateway 自己**。也就是说 ChannelManager 虽然跑在 Gateway 同一个进程里，但它对 agent runtime 的访问方式和一个外部浏览器**完全一样**——发 HTTP 请求到自己。
- `create_internal_auth_headers()`（`app/gateway/internal_auth.py`）塞内部鉴权头，让请求被中间件当成"可信内部调用"放行（和调度器路径的 `AUTH_SOURCE_INTERNAL` 是同一套思想）。
- CSRF 双提交（header 里带 token，cookie 里也带同一个 token）——Gateway 开了 CSRF 防护，自回环请求得带上才能过。

于是 `client.runs.stream(thread_id, assistant_id, ...)` 这一行的**实际网络效果**就是：

```
POST /api/threads/{tid}/runs/stream  →  命中 stream_run (thread_runs.py:496)
                                        →  start_run(body, tid, request)  (services.py:608)
                                        →  asyncio.create_task(run_agent(bridge, run_mgr, record, ...))
                                        →  sse_consumer(bridge, record, ...)  (services.py:828)
                                            →  bridge.subscribe(run_id, ...)
```

**到 `start_run` 这一步，IM 发起的 run 和浏览器发的 run、调度器发起的 run 汇聚到同一条 agent 执行管道**——`start_run`（`services.py:608`）是所有 run 的统一入口：建 `RunRecord`、鉴权、注入 context、后台起 `run_agent` 任务。从这往后，agent 根本不知道也不关心这次 run 是飞书来的还是浏览器来的。

> 这也正是 3.1 表格里"流式 IM 通道"那行的物理基础：`client.runs.stream` 命中的就是 `POST /{tid}/runs/stream`，服务端走的是和浏览器一模一样的 `sse_consumer → bridge.subscribe`。**IM 是订阅者，但是"远程的、经 HTTP 自回环进来的"订阅者**——和浏览器标签页没有本质区别。

##### 4. 出站链路：agent → IM，逐阶段拆解

agent 跑起来后，怎么把输出送回飞书？这条链路横跨两条总线，是整节最容易绕晕的地方，慢慢看。

###### 阶段 G：agent worker 只往 StreamBridge publish

`run_agent`（`backend/packages/harness/deerflow/runtime/runs/worker.py:246`）由阶段 F 的 `asyncio.create_task` 启动。它跑 LangGraph 图，每产出一点东西就往 StreamBridge 写一帧（`worker.py:350 / 502 / 523` 等）：

```python
await bridge.publish(run_id, sse_event, serialize(chunk, ...))   # 例如 worker.py:502
```

**关键**：`run_agent` 全程只认识 `bridge`，**从不 import `MessageBus`，从不调 `bus.publish_outbound`**。它就是个"往 StreamBridge 吐事件的工人"，根本不知道有 IM 这回事。agent 输出到 IM 的整个桥接，完全发生在 agent 之外。

###### 阶段 H：ChannelManager 作为 HTTP 客户端，从 StreamBridge 把事件读回来

回到阶段 E 的流式分支。`_handle_streaming_chat`（`manager.py:1712`）里那行 `async for chunk in client.runs.stream(...)`（`manager.py:1744`）：

- `client.runs.stream` 在 SDK 内部发的是阶段 F 那次 `POST /runs/stream` 的 HTTP 请求，然后**解析服务端返回的 SSE 流**。
- 服务端的 SSE 流是谁产生的？正是 3.1/3.6 讲的 `sse_consumer` → `bridge.subscribe(run_id)`。也就是说，**阶段 G 里 worker `bridge.publish` 的事件，在这里被 SDK 解析回 `chunk`，吐给这个 `async for`**。

这一步就是 StreamBridge 的消费端在 IM 链路上的体现：ChannelManager 是 StreamBridge 的订阅者，只不过它订阅的方式是"用 SDK 发 HTTP 请求、收 SSE 流"，而不是直接调 `bridge.subscribe`（那个是 gateway 进程内部用的）。

###### 阶段 I：manager 把 chunk 翻译成 OutboundMessage，发到 MessageBus

这是**两条总线的衔接点**——但衔接发生在 manager 代码里，不是两条总线自己连。看循环体（`manager.py:1744-1791`）：

```python
async for chunk in client.runs.stream(thread_id, assistant_id, **stream_kwargs):  # 1744
    event = getattr(chunk, "event", "")
    data = getattr(chunk, "data", None)
    if event in MESSAGE_STREAM_EVENTS:                       # ("messages-tuple","messages")
        accumulated_text, current_message_id = _accumulate_stream_text(...)   # 1753，累加文本
        ...
    # ...节流判断...
    display_text = latest_text + " ▉"                        # 1775，加"打字中"游标
    await self.bus.publish_outbound(                          # 1776 ← 翻译并丢给 MessageBus
        OutboundMessage(
            channel_name=msg.channel_name, chat_id=msg.chat_id, thread_id=thread_id,
            text=display_text, is_final=False, ...
        )
    )
```

逐行讲关键的：

- `event in MESSAGE_STREAM_EVENTS`——只关心"有新文本"这类事件，别的事件（工具调用等）在 IM 里不直接显示。
- `_accumulate_stream_text`（`manager.py:1753`）把流式 token 累积成一段逐渐变长的文本。
- **节流**（`manager.py:1771-1773`）：不是每个 token 都发回飞书（那会把飞书 API 打爆），而是"距上次发送 ≥1 秒（`STREAM_UPDATE_MIN_INTERVAL_SECONDS=1.0`）**或**新累积 ≥60 字（`STREAM_UPDATE_MIN_CHARS=60`）"才发一次。这是流式通道的背压适配——agent 秒出 1000 token，但飞书消息更新有频率限制。
- `text=latest_text + " ▉"`——末尾加个"▉"游标，提示用户"还在打字"。最后一条（`is_final=True`）会去掉游标。
- `await self.bus.publish_outbound(...)`——**这一行完成了从 StreamBridge 到 MessageBus 的语义转换**：`StreamEvent`（一帧 run 事件）→ `OutboundMessage`（一条要发去飞书的 IM 消息）。从此数据离开 StreamBridge 的领地，进入 MessageBus。

finally 块（`manager.py:1826`）发最后一条 `OutboundMessage(is_final=True)`——去掉游标、带上最终文本和附件。

> 阻塞通道（Slack 等）更简单：阶段 H/I 直接被 `client.runs.wait`（一次拿最终结果）替代，只发一条 `is_final=True` 的 `OutboundMessage`（`manager.py:1710`）。发后即忘通道（GitHub）则**根本不发 outbound**。

###### 阶段 J：MessageBus 扇出到对应通道（按 channel_name 名字门）

`bus.publish_outbound`（`message_bus.py:177`）做的事很简单——遍历所有出站监听器，挨个 `await`：

```python
async def publish_outbound(self, msg: OutboundMessage) -> None:
    for callback in self._outbound_listeners:        # 186
        try:
            await callback(msg)
        except Exception:
            logger.exception(...)                    # 190，一个通道挂了不影响别的
```

每个通道在 `start()` 时都注册过 `self.bus.subscribe_outbound(self._on_outbound)`（飞书在 `feishu.py:204`）。`_on_outbound` 是所有通道共享的基类逻辑（`backend/app/channels/base.py:158`），靠**名字门**决定要不要处理：

```python
async def _on_outbound(self, msg: OutboundMessage) -> None:
    if msg.channel_name == self.name:                # 166 ← 只有飞书实例会处理飞书消息
        try:
            await self.send(msg)                     # 168，调子类的 send
        except Exception:
            return                                   # 文本发送失败就不传附件，避免半截投递
        for attachment in msg.attachments:
            await self.send_file(msg, attachment)    # 175，附件单独传
```

所以一条 `channel_name="feishu"` 的 `OutboundMessage` 会被所有通道实例的 `_on_outbound` 收到，但只有 `FeishuChannel`（`self.name == "feishu"`）会真正 `send`，别的通道直接 return。这就是 MessageBus 的"路由"——按字符串名字匹配，不跑配。

###### 阶段 K：通道调平台 API，把消息送回用户

以飞书为例。`FeishuChannel.send`（`feishu.py:273`）带重试地调 `_send_card_message`（`feishu.py:607`），里面会找之前阶段 A 建的那张"running"卡片，用 `im.v1.message.patch` 更新内容（`feishu.py:526`）：

```python
await asyncio.to_thread(self._api_client.im.v1.message.patch, request)   # feishu.py:526
```

- `asyncio.to_thread` 把同步的 lark-oapi SDK 调用扔到线程池，避免阻塞主事件循环。
- 收到 `is_final=True` 的消息时，`_send_card_message`（`feishu.py:658-660`）会 pop 掉 running 卡片缓存、加上 "DONE" reaction——标志着这次回复结束。

Slack / Telegram / Discord 各自的 `send` 类似，调各自平台 SDK 的发消息接口。**到这一步，用户终于看到 agent 的回复出现在 IM 里**——一次完整往返结束。

##### 5. 回到 3.3 的老问题：IM 到底是不是订阅者？

**是**，但要分清是"谁的订阅者"：

- 对 **StreamBridge** 而言，流式 IM 通道（经 ChannelManager 的 `client.runs.stream`）是订阅者——和浏览器标签页本质相同，都是"远程的、经 HTTP 进来的"StreamBridge 消费者。阻塞通道（`client.runs.wait`）也消费 StreamBridge，只是把流"喝干"拿最终结果（对应 3.1 表里的 `wait_for_run_completion`）。
- 对 **MessageBus** 而言，IM 通道既是入站生产者（`publish_inbound`），也是出站订阅者（`subscribe_outbound`）——但这是**进程内的另一套总线**，和 StreamBridge 毫无关系。

这两层"订阅"叠在一起，就是混淆的根源。记住一句话：**StreamBridge 管 run 的事件流怎么跨 HTTP 边界递给任意订阅者；MessageBus 管 IM↔manager 之间整条消息怎么在进程内往返。** 把 ChannelManager 想象成一座桥：它一头连着 MessageBus（收 IM、发 IM），另一头用 HTTP 自回环连着 StreamBridge（驱动 agent、收 agent 输出）。

##### 6. 三个核心设计点回顾

1. **两条总线职责分离，唯一的粘合点是 ChannelManager 本身**。StreamBridge 从不 import MessageBus，反之亦然；它们靠 ChannelManager"一边消费 StreamBridge 的 chunk、一边往 MessageBus 发 OutboundMessage"在代码层衔接（阶段 I）。这让 IM 子系统（`app/channels/`）和 agent runtime（`packages/harness/`）可以独立演进、独立测试。
2. **HTTP 自回环复用整套 run 管道**。ChannelManager 不绕开 HTTP 直接调 agent，而是老老实实用 `langgraph_sdk` POST 自家 `/runs/*`。这样鉴权、CSRF、context 注入、`start_run` 状态机、`multitask_strategy="reject"` 的线程忙保护——浏览器能享受的，IM 自动也享受。IM 发起的 run 和浏览器发的 run 走的是**字面意义上同一条 agent 执行管道**（阶段 F）。
3. **能力分流按通道交互范式**。流式 / 阻塞 / 发后即忘三条路对应三种 IM 交互：实时打字（飞书卡片边跑边更新）、跑完一次性回帖（Slack）、agent 自主长任务自己回帖（GitHub 改代码提 PR）。分流逻辑集中在 `_handle_chat_on_thread` 一处，新增通道只要在 `CHANNEL_CAPABILITIES` 里标好 `supports_streaming`，或注册一个 `ChannelRunPolicy` 设 `fire_and_forget`，就能自动落到对应路径。

#### 3.4 调度器（scheduler）是不是订阅者？

**不是**。这一节专门把这个结论讲透——不光说"不是"，还要说清"它靠什么拿到 run 跑完的信号"。

先说为什么会有人以为调度器是订阅者：§3.3 刚把"流式 IM 通道是订阅者"讲完，紧接着 §3.4.1 又要讲"定时任务怎么把 run 派发给 agent 执行"。把这两件事连起来读，很容易顺着惯性得出"调度器既然能发起 run，那它八成也订阅了那个 run 的事件流，好盯着它跑完"。这个直觉**错了**。调度器确实发起 run（生产者），但它盯 run 跑完用的不是订阅，而是一个叫**完成回调钩子**的东西。

下面把这个钩子从 worker 到调度器的完整链路拆成四段讲，最后用一张对比表说清"回调"和"订阅"为什么是两套机制。

##### 先看清"完成回调钩子"长什么样：从 worker 到调度器的三跳链路

钩子的触发点在 agent worker 的收尾代码里。`run_agent`（`backend/packages/harness/deerflow/runtime/runs/worker.py:246`）的函数体最外层包了一个 `try ... finally:`（`finally` 从 `worker.py:630` 开始），finally 里做一堆收尾（flush 日志、同步标题、记 token 用量……），其中就有调钩子这几行（`worker.py:710-714`）：

```python
        if ctx.on_run_completed is not None:
            try:
                await ctx.on_run_completed(record)
            except Exception:
                logger.warning("Run completion hook failed for %s (non-fatal)", run_id, exc_info=True)
```

逐行：

- **第 1 行** `if ctx.on_run_completed is not None:` `ctx` 是这次 run 的 `RunContext`（下面讲它怎么来的），`on_run_completed` 是它上面的一个可选字段。`is not None` 是个**守卫**——不是所有 run 都接了钩子（比如调度器没配置时这个字段就是 `None`，见下一段），没接就跳过整个调用。
- **第 2-3 行** `try: await ctx.on_run_completed(record)` 真正调用钩子，传入刚跑完的 `record`（一个 `RunRecord`，含 run_id、status、metadata、error 等）。`await` 说明钩子本身是个**协程**——它可以做异步操作（比如写数据库）。注意它是在 `finally` 块里、`bridge.publish_end(run_id)`（`worker.py:718`）**之前**调的，语义是"run 已经定稿了，把'定稿'这件事通知出去"。
- **第 4 行** `except Exception: logger.warning(... "non-fatal" ...)` **钩子失败不影响 run 收尾**。任何异常都被吞掉、只记一条 warning 日志，然后继续往下走 `publish_end`。这是个刻意的容错设计：钩子是"通知"性质的旁路逻辑，它挂了不能反过来把 agent 的正常收尾搞坏。

钩子触发后数据怎么流到调度器？看这张流向图——注意它是**函数调用链**，不是消息总线：

```
  run_agent 的 finally 块（worker.py:710）        ← agent worker 进程内
        │
        │  await ctx.on_run_completed(record)
        ▼
  ctx.on_run_completed                             ← 一个协程引用（绑定方法，见下一段）
        │  = scheduled_task_service.handle_run_completion   （deps.py:423 接的线）
        ▼
  ScheduledTaskService.handle_run_completion(record)   （scheduler/service.py:252）
        │
        │  读 record.metadata → 定位是哪个定时任务 → 更新 task_run / task 表
        ▼
  scheduled_task_runs 行落终态、once 任务收尾
```

整条链路全程**没有 bridge**——没有 `subscribe`、没有事件流、没有 `asyncio.Condition`。它就是一次普通的异步函数调用：worker 调一个挂在 `ctx` 上的协程，那个协程恰好是调度器的 `handle_run_completion`。下面把"恰好是"这件事怎么接上的讲清楚。

##### 钩子是怎么接上去的：依赖注入 + 单一赋值点

`ctx.on_run_completed` 这个协程引用是哪来的？答案在 `get_run_context`（`backend/app/gateway/deps.py:406-424`）——它是一个 FastAPI 依赖，每次 run 启动时（`start_run` 在 `services.py:627` 调一次）从 `app.state` 的单例里拼一个 `RunContext`：

```python
def get_run_context(request: Request) -> RunContext:
    """Build a :class:`RunContext` from ``app.state`` singletons. ..."""
    return RunContext(
        checkpointer=get_checkpointer(request),
        store=get_store(request),
        event_store=get_run_event_store(request),
        run_events_config=getattr(request.app.state, "run_events_config", None),
        thread_store=get_thread_store(request),
        app_config=get_config(),
        on_run_completed=getattr(request.app.state, "scheduled_task_service", None).handle_run_completion if getattr(request.app.state, "scheduled_task_service", None) is not None else None,
    )
```

逐行讲最后一行那条密集的表达式（`deps.py:423`）——这是整条接线的核心：

- `getattr(request.app.state, "scheduled_task_service", None)` 读 `app.state` 上挂的调度器服务单例。用 `getattr(..., None)` 而不是 `request.app.state.scheduled_task_service`，是因为**调度器是可选的**——只有当 `scheduled_task_repo` 和 `scheduled_task_run_repo` 都配了，Gateway 启动时（`app.py:256-269`）才会构造 `ScheduledTaskService` 并 `app.state.scheduled_task_service = ...`；没配就没有这个属性，直接访问会抛 `AttributeError`。
- `... .handle_run_completion if ... is not None else None` 这个三元表达式取的是调度器服务的 **bound method（绑定方法）**——注意不是 lambda、不是包装函数，就是 `scheduled_task_service.handle_run_completion` 这个方法引用本身。Python 的 bound method 自带 `self`，所以之后 worker `await ctx.on_run_completed(record)` 时，实际执行的是 `scheduled_task_service.handle_run_completion(record)`，`self` 已经绑死。
- `if getattr(request.app.state, "scheduled_task_service", None) is not None else None` **这里 `getattr` 写了两遍**，看着冗余，其实是必要的：第一次取出服务对象用来判空，第二次（在最前面那行）再取一次访问它的 `handle_run_completion`。Python 没有 `?.` 安全调用，所以只能这样两段式。调度器没配 → 整个表达式落 `None` → worker 那头的 `is not None` 守卫把它跳过。

几个要点补一下，呼应 §3.4.1：

- **`RunContext` 是 `frozen=True` 的 dataclass**（`worker.py:130-145`）。`frozen` 意味着对象构造完字段就**不可重新赋值**——所以钩子没法在 run 跑到一半时再接上去，必须在 `get_run_context` 构造 `RunContext` 那一刻就定死。这和 §3.4.1 阶段 3 讲的"依赖注入在启动时接线"是同一个思想：钩子是个基础设施依赖，和 checkpointer、store 同级，都走构造时注入。
- **`on_run_completed` 是单个回调，不是回调列表**。它的字段类型是 `Any | None`（`worker.py:145`），调用点是单次 `await ctx.on_run_completed(record)`（不是 `for cb in ...`）。全代码库对它的赋值**只有 `deps.py:423` 这一处**。所以"钩子全局接在每个 run 上"这句话要精确理解：每个 run 的 `RunContext` 上都挂了同一个绑定方法，但这个方法内部会判断这次 run 到底是不是定时任务触发的（见下一段）。

##### 调度器收到回调干了什么：`handle_run_completion` 逐行讲

钩子落到调度器这边就是 `handle_run_completion`（`backend/app/scheduler/service.py:252-301`）。完整代码：

```python
async def handle_run_completion(self, record: RunRecord) -> None:
    metadata = record.metadata or {}
    task_id = metadata.get("scheduled_task_id")
    task_run_id = metadata.get("scheduled_task_run_id")
    user_id = record.user_id
    if not isinstance(task_id, str) or not isinstance(task_run_id, str) or not user_id:
        return

    terminal_status: Literal["success", "failed", "interrupted"] | None
    if record.status.value == "success":
        terminal_status = "success"
        error = None
    elif record.status.value == "interrupted":
        # Distinct from "failed": an interrupt (user cancel, same-thread
        # takeover) carries no error and is not an execution failure.
        terminal_status = "interrupted"
        error = record.error or "run was interrupted before completion"
    elif record.status.value in {"error", "timeout"}:
        terminal_status = "failed"
        error = record.error
    else:
        terminal_status = None
        error = record.error
    if terminal_status is None:
        return

    await self._task_run_repo.update_status(
        task_run_id,
        status=terminal_status,
        run_id=record.run_id,
        error=error,
        finished_at=datetime.now(UTC),
    )

    task = await self._task_repo.get(task_id, user_id=user_id)
    if task is None:
        return

    updates: dict[str, Any] = {"last_error": error}
    if task["schedule_type"] == "once":
        if terminal_status == "success":
            updates["status"] = "completed"
        elif terminal_status == "interrupted":
            updates["status"] = "cancelled"
        else:
            updates["status"] = "failed"
    await self._task_repo.update(task_id, user_id=user_id, updates=updates)
```

逐段讲：

**读钥匙 + 早退守卫**（前 6 行）：

- `metadata = record.metadata or {}` 取 run 的 metadata。这就是 §3.4.1 阶段 3 `dispatch_task` 调 `_launch_run` 时塞进去的那个字典（`scheduled_task_id` / `scheduled_task_run_id`）。`or {}` 是防 `None`。
- `task_id = metadata.get("scheduled_task_id")` / `task_run_id = metadata.get("scheduled_task_run_id")` 把"反向回写的钥匙"读出来——靠它们才能把"这个 agent run"关联回"调度账本里哪条 task_run"。
- `user_id = record.user_id` run 的归属用户。
- `if not isinstance(task_id, str) or not isinstance(task_run_id, str) or not user_id: return` **这就是"钩子全局接、但只对定时任务生效"的机制**。非定时任务的 run（用户在浏览器聊天框发的消息、IM 通道触发的 run）metadata 里根本没有这两个字段，`task_id` / `task_run_id` 是 `None`，`isinstance` 判不过，直接 return——钩子对它们是个 no-op。所以 worker 那头"每个 run 都调一次 `on_run_completed`"听着很重，实际上非定时任务的调用一进来就 return，开销极小。

**状态映射**（中间一大段）：

- 把 agent worker 的 `RunRecord.status` 映射成调度账本的终态。注意三种终态的区分：
  - `success` → `"success"`，`error=None`。
  - `interrupted` → `"interrupted"`。**这是"被中断"，不是"失败"**——注释特意写清楚：用户主动取消、或同 thread 被新 run 抢占，都算 interrupted，它不带执行失败的语义。这一点很关键：它影响下面 `once` 任务收尾时落到 `cancelled` 还是 `failed`。
  - `error` / `timeout` → `"failed"`，带上 `record.error`。
  - 其它（比如还在跑的中间状态）→ `terminal_status = None`，紧接着的 `if terminal_status is None: return` 直接退出——钩子只处理**终态**的 run。
- `if terminal_status is None: return` 二次守卫，防止把非终态 run 当成跑完了。

**写库 + 父任务收尾**（最后一段）：

- `self._task_run_repo.update_status(task_run_id, status=terminal_status, run_id=..., error=..., finished_at=...)` 把 §3.4.1 阶段 2 插的那条 `scheduled_task_runs` 行（启动时是 `queued` → `running`）更新成终态，记下 `finished_at` 时间戳。这就是调度账本上"这一次执行"的最终落点。
- `task = await self._task_repo.get(task_id, user_id=user_id)` 重新读父任务定义（`scheduled_tasks` 表）。
- `if task is None: return` 父任务可能已经被删了（用户删了任务但 run 还在跑），直接退出。
- `updates = {"last_error": error}` 所有任务都更新 `last_error`（成功时是 `None`）。
- `if task["schedule_type"] == "once":` **一次性任务的一生在这里结束**。注释解释：run 既然启动了，这一次"发生"就算消耗掉了（再触发一次会有重复副作用），所以无论成功/中断/失败都把它收尾掉——成功→`completed`，中断→`cancelled`，失败→`failed`。
- **周期任务（`cron` / `interval`）只更新 `last_error`，不碰 `status`**——它继续是 `enabled`，按 `next_run_at`（§3.4.1 阶段 5 的 `update_after_launch` 已经算好下一次）继续跑。一次失败不影响后面的调度。

##### 为什么"回调"而不是"订阅"：两种机制的本质对比

到这里可能会问：调度器要的不就是"run 跑完了"这个信号吗？订阅 bridge 不也能拿到吗（看到 `END_SENTINEL` 就当跑完了）？为什么非得另搞一套回调？因为这两套机制解决的是**完全不同的需求**：

| 维度 | `bridge.subscribe`（订阅） | `on_run_completed`（回调） |
|---|---|---|
| 触发时机 | 每来一条事件都触发 | run 进终态时**触发一次** |
| 拿到的数据 | 事件流（token、tool 调用、error 帧…逐条） | 一个 `RunRecord`（终态快照） |
| 模式 | 拉模式（订阅者主动 `async for` 读） | 推模式（worker 主动 `await` 调） |
| 跨进程 | 是（Redis 模式跨 worker，麻烦 4） | 否（进程内函数调用，钩子和 worker 在同一进程） |
| 典型消费者 | 浏览器（要逐字显示）、IM（要边跑边回帖） | 调度器（只要"跑完了 + 结果如何"） |
| 历史回放 | 要（`Last-Event-ID` 重连，麻烦 2） | 不要（终态就一个，不需要回放） |

打个比方把两套机制的区别落地：

- **订阅像订报纸**。你每天收到一份，要自己看；出差几天回来还能补订历史那几天的（重放）。流式 UI 和 IM 通道需要这个——它们要把每个 token、每次工具调用实时展现给用户。
- **回调像快递签收回执**。快递员（worker）把货送到、拿到签收，只回一句"货到了，签收状态是 X"。他不关心沿途、不送报纸，就一个单次通知。调度器需要的就是这个——它只关心"这一次定时任务跑完了没、成功还是失败"，对中间那几百个 token 毫无兴趣。

所以"调度器不订阅"不是图省事，而是**它根本没有订阅能解决的需求**。硬要订阅反而浪费：为拿到一个终态信号，去消费几百条它根本不看的事件帧，还得自己从 `END_SENTINEL` 里推断"跑完了"——而 worker 那头本来就有一个精确的 `RunRecord` 可以直接传过来。回调把这个现成的终态快照用一次函数调用递过去，干净利落。

##### 回到 fan-out 的结论：订阅者到底有谁

把上面四段合起来，§3.3 那句"fan-out 的订阅者有两类"可以精确成：

- **订阅者（消费 bridge 事件流）只有两类**：浏览器/客户端（通过 `/runs/stream`、`/runs/{id}/join`、`/runs/{id}/stream`）和流式 IM 通道（本质上也是上一类的 HTTP 客户端，§3.3 详讲）。它们要的是**逐条事件**。
- **调度器不是订阅者**，它是 run 的**生产者之一**（经 §3.4.1 的派发链路把 run 启起来），同时是 run 终态的**回调消费者**（靠 `on_run_completed` 拿一个 `RunRecord` 快照）。这两个角色要分清——"发起 run"和"消费 run 的事件流"是两件事，调度器只做前者加一个"收尾通知"，不做后者。

一句话收尾：**定时任务的完整生命周期 = §3.4.1 的派发链路（启动）+ §3.4 的回调钩子（收尾）**。启动时 `dispatch_task` 把 `scheduled_task_id` / `scheduled_task_run_id` 塞进 run 的 metadata（§3.4.1 阶段 3）；收尾时 worker 的 `finally` 块调 `on_run_completed`，调度器从 metadata 把这两个 key 读出来反向定位、把终态写回调度账本。这就是"调度账本 ↔ agent run"之间唯一的粘合点——**全程不碰 bridge**。

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
