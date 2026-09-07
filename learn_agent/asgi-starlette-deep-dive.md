# ASGI / Starlette / uvicorn 深度讲解：一次请求从网卡到 FastAPI 路由中间发生了什么

本文是 `lifespan-deep-dive.md` 的前置姊妹篇。那篇讲的是 Gateway 应用"开机初始化 → 正常服务 → 关机清理"的生命周期；本文讲的是更底下的一层地基——**支撑这一切运转的协议与框架栈**：uvicorn、ASGI、Starlette、FastAPI 四者是什么关系，一次 HTTP 请求是怎么从网卡上的字节流变成你写的 `@router.get(...)` 处理函数的。

读完 `lifespan-deep-dive.md` 你会知道 `lifespan` 是"总电闸"；但那个总电闸本身是被谁、在什么时机、通过什么接口触发的？答案就是本文的主角 **ASGI 协议**。建议先读本文，再回去读 lifespan 那篇。

本文面向第一次接触这套技术栈的同学，不需要任何前置知识。所有概念都从零讲起。

---

## 第 1 节：全景图——四层结构一张图

先把最终结论放在最前面。DeerFlow 的 Gateway（`backend/app/`）跑在这样一条链路上：

```
浏览器 / 飞书 / Slack
   ↓  HTTP / SSE / WebSocket
Nginx (端口 2026)              ← 反向代理,统一入口
   ↓
uvicorn                        ← "ASGI 服务器":把网络字节流解析成结构化事件
   ↓  await app(scope, receive, send)   ←—— ★这条边界就是 ASGI 协议
Starlette                      ← "ASGI 框架":把裸协议封装成 Request/Response/路由/中间件
   ↓
FastAPI                        ← 在 Starlette 之上加 Pydantic 校验、依赖注入、OpenAPI 文档
   ↓
DeerFlow Gateway 应用 (backend/app/)
```

对应的启动命令在 `backend/Makefile:10`：

```bash
uv run --locked uvicorn app.gateway.app:app --host 0.0.0.0 --port 8001
```

这行命令里 `app.gateway.app:app` 就是那个 FastAPI 应用对象。**uvicorn 启动后做的唯一核心工作，就是对这个对象发起 ASGI 调用**（第 6 节详述）。

记住这张图，后面每一节都是对图中某一层的展开。

---

## 第 2 节：Starlette 是什么——FastAPI 的"Web 内核"

**Starlette 是一个轻量级的 Python ASGI 异步 Web 框架/工具库。FastAPI 直接构建在它之上。**

一个常见误解是"FastAPI 是一个完整的 Web 框架"。实际上 FastAPI 本身几乎不实现 HTTP 层的底层细节，它做的是"API 语义层"——Pydantic 数据校验、依赖注入、自动生成 OpenAPI 文档。而一个请求如何进来、一个响应如何出去、路由怎么匹配、中间件链怎么组织，这些"Web 内核"全部来自 Starlette：

| 能力 | Starlette 提供 | 在 DeerFlow 中的体现 |
|---|---|---|
| Request / Response 对象 | `starlette.requests` / `starlette.responses` | `fastapi/responses.py` 里的 `JSONResponse` 等类基本都是从 Starlette 原样重导出或继承的 |
| 路由 | `Route` / `Router` / `APIRouter` 的底层 | Gateway 的所有 REST 路由 |
| 中间件 | CORS、GZip、认证等中间件栈 | Nginx 背后的 CORS 等处理 |
| ASGI 协议实现 | 应用了 ASGI 调用约定（见第 6 节） | 整个 Gateway 的运行基础 |
| SSE / 流式响应 | `StreamingResponse` / `sse-starlette` 的基础 | agent 边跑边往前端推事件流 |
| WebSocket | 原生支持 | 前端与 Gateway 的实时通道 |
| TestClient | 包装 `httpx` 的测试客户端 | backend 测试 |

一句话总结分工：**FastAPI 负责"API 语义"，Starlette 负责"Web 内核"**。你在 backend 里写的每个 `@router.get(...)`、`Request`、`StreamingResponse`、`WebSocket`，运行时真正干活的都是 Starlette。

---

## 第 3 节：ASGI 是什么——一份协议，不是一个软件

**ASGI**（Asynchronous Server Gateway Interface，异步服务器网关接口）是 Python Web 生态中的一个**标准协议规范**，定义了"Web 服务器"和"Python 应用"之间如何通话。

一句话理解：**它是 Web 服务器（uvicorn）把 HTTP 请求"递给"你的 Python 应用（Starlette/FastAPI）的接口约定。服务器和应用都按这个约定写，就可以任意组合搭配。**

这里有一个必须首先建立的认知，也是最容易搞混的点：

> **ASGI 不是栈里的一个软件组件，而是两个软件之间的那条边界本身。**
>
> - uvicorn 是 ASGI 规范**服务器侧**的实现
> - Starlette 是 ASGI 规范**应用侧**的实现
> - ASGI 本身只是一份文档约定，规定"服务器必须这样调用应用、应用必须这样响应"

类比：电源插座标准。插头和插座的**形状**是标准化的（这就是 ASGI）；插头后面插的是什么电器（Starlette / Django async / 任何框架）、插座后面接的是什么电厂（uvicorn / hypercorn / daphne），标准不管。只要形状匹配，即插即用。

---

## 第 4 节：为什么要有 ASGI——从 WSGI 说起

ASGI 之前，Python Web 的统一标准是 WSGI（2003 年，PEP 333）。理解两者的差异，就理解了 ASGI 存在的意义：

| | WSGI（老标准） | ASGI（新标准） |
|---|---|---|
| 时代 | 2003，PEP 333 | 2015+ |
| 模型 | **同步**，一个请求占一个线程 | **异步**，基于 `async/await` |
| 协议 | 只支持 HTTP | HTTP + **WebSocket** + 长连接任务 |
| 长流响应 | 很别扭 | 原生支持（逐段 `await` 发送） |
| 典型组合 | uWSGI / Gunicorn + Django/Flask | uvicorn / hypercorn + FastAPI/Starlette |

WSGI 处理"一次请求-一次响应"没问题，但遇到三类场景就无能为力：

1. **推送事件流**——agent 边执行边往前端推 SSE 事件，连接要挂很久，响应是"一段一段"出来的
2. **WebSocket**——双向长连接，需要持续收发帧
3. **几千个并发挂着的连接**——同步模型下一个连接占一个线程，线程很快耗尽

而这正是 DeerFlow 这种 agent 系统的核心场景。所以整个 backend 从第一天就构建在 ASGI 栈上。

ASGI 的设计为了同时兼容这些场景，采用了**事件驱动、消息分片**的模型（见下节）：一切通信都拆成一条条离散的消息（`http.response.start`、`http.response.body`、`websocket.receive`……），通过 async 回调传递。流式响应就是"多次发 body 消息"，WebSocket 就是"换一种消息类型"，lifespan 就是"启动/关机时发一对特殊消息"——**一套协议覆盖三种场景**。

---

## 第 5 节：协议长什么样——scope、receive、send

ASGI 应用就是一个 async 可调用对象，服务器每次请求都按固定的三参数签名调用它：

```python
async def app(scope, receive, send):
    ...
```

三个参数各是什么：

### 5.1 `scope`——请求的"元数据快照"（一个普通字典）

请求开始时就确定、整个生命周期不变的信息：

```python
{
    "type": "http",            # "http" / "websocket" / "lifespan"
    "method": "GET",
    "path": "/api/threads",
    "headers": [(b"host", b"localhost:8001"), ...],
    "query_string": b"",
    "client": ("127.0.0.1", 54321),
    ...
}
```

### 5.2 `receive`——async 可调用，用来"收"消息

应用主动 `await receive()` 拿一条消息。HTTP 场景下主要是收请求 body（body 也可能分多帧到达）；WebSocket 场景下收客户端发来的每一帧。

### 5.3 `send`——async 可调用，用来"发"消息

应用每 `await send({...})` 一次，就向客户端发出一段内容。消息是规定格式的字典，**必须带 `type` 字段**：

```python
# 先发响应头（一次）
await send({
    "type": "http.response.start",
    "status": 200,
    "headers": [(b"content-type", b"text/plain")],
})
# 再发 body（可以发很多次 → 这就是流式响应）
await send({"type": "http.response.body", "body": b"hello"})
await send({"type": "http.response.body", "body": b" world"})
```

### 5.4 一个最小但完整的 ASGI 应用

不用任何框架，一个函数就是一个合法的 ASGI 应用：

```python
async def app(scope, receive, send):
    body = b"hello"
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})
```

把它交给 `uvicorn mymodule:app` 就能跑。Starlette 做的事情，本质上是**替你把这个裸协议封装成人性化的 API**——把 `scope` 包成 `Request` 对象、把 `send` 包成 `Response` 对象、把消息分发变成路由匹配和中间件链。

---

## 第 6 节：ASGI 起作用的位置——那条边界，以及一次请求的完整时序

回到第 1 节的图。**ASGI "存在"的地方，就是 uvicorn 和 Starlette 之间那条边界。** 具体到代码，`make dev` 启动后，每个请求都走一遍下面这个时序：

```
1. 网卡收到 TCP 字节流
2. uvicorn 解析出 HTTP 方法/路径/头          （网络层 → 协议对象）
3. uvicorn 组装 scope,构造 receive/send 回调,
   然后调用 await app(scope, receive, send)   ← ★ ASGI 边界,请求方向
4. Starlette 接管:scope 包成 Request → 路由匹配 → 中间件链
   → FastAPI 依赖注入 + Pydantic 校验
5. 你的处理函数（backend/app/ 里的 handler）执行,返回 Response
6. Starlette 逐条调 await send({...})          ← ★ ASGI 边界,响应方向
7. uvicorn 把消息转回字节流写进 socket
8. 浏览器收到响应
```

注意第 3 步和第 6 步是**同一个接口的两个相反方向**：

- 第 3 步：uvicorn 的代码调用 Starlette 写的 `__call__`
- 第 6 步：Starlette 的代码调用 uvicorn 写的 `send` 回调

**同一份协议，双向使用。** 双方谁也不 import 谁、谁也不"依赖"谁，只共同依赖那份规范文档——这就是能即插即用换服务器/换框架的原因。

对 DeerFlow 最有现实意义的一点：**SSE 流式推送就是第 6 步被反复执行很多次**。agent 每产生一段事件，Starlette 的 `StreamingResponse` 就 `await send()` 一次，前端就收到一段。`stream-bridge-deep-dive.md` 讲的事件流，最终就是从这个出口到达浏览器的。

---

## 第 7 节：精确辨析——"标准"与"实现"（`await app` 与 `await send` 到底是谁的代码）

一个容易似懂非懂的问题："`await app` 和 `await send` 都是 ASGI 的标准实现吗？"

**不严谨。ASGI 是标准（一份规范文档），没有"标准实现"；这两个调用点是符合 ASGI 标准的接口，但实现代码分属两家：**

| 调用 | 规范规定的部分（标准） | 实现由谁写 |
|---|---|---|
| `await app(scope, receive, send)` | "应用必须是接收这三个参数的 async 可调用对象" | **Starlette**（框架侧实现 `__call__`） |
| `await send({...})` | "send 收到的必须是 `{"type": "http.response.start", ...}` 这种格式的消息字典" | **uvicorn**（服务器侧实现；`send` 这个可调用对象本身是 uvicorn 创建并传进来的） |
| `scope` / `receive` | 字典结构、消息格式 | 由 **uvicorn** 构造，Starlette 只消费 |

更细一层看第 3 步和第 6 步的"调用方/被调用方"：

```
3. await app(scope, receive, send)
     调用方:uvicorn 的代码        被调用:Starlette 写的 __call__

6. await send({"type": "http.response.body", ...})
     调用方:Starlette 的代码      被调用:uvicorn 写的回调
```

记忆口诀：**被标准化的是"插座形状"（参数签名、消息字典格式）；插座两边插的是什么设备，规范不管。**

---

## 第 8 节：两个精妙的下钻——Response 对象本身也是 ASGI 应用；`send` 的消息发给了谁

### 8.1 `StreamingResponse` 自己就是一个微型 ASGI 应用

一个自然的追问："StreamingResponse 是应用端定义的返回结构，符合 ASGI 对吧？"

先纠正措辞：**ASGI 规范里没有任何 Response 类**。规范只认识消息字典。`StreamingResponse` 是 Starlette（应用侧）写的封装类，属于"插座上插的电器"。

但它"符合 ASGI"的程度比想象中更彻底——看 Starlette 真实源码（`starlette/responses.py:257`）：

```python
async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
```

**它实现了 ASGI 应用的标准三参数签名——响应对象本身就是一个微型 ASGI 应用。** 被调用时，它发出的正是规范定义的消息序列（`responses.py:248-255`）：

```python
async def stream_response(self, send: Send) -> None:
    await send({"type": "http.response.start", "status": ..., "headers": ...})   # 1. 先发一次响应头
    async for chunk in self.body_iterator:                                       # 2. 逐块拉取你给的 async 生成器
        await send({"type": "http.response.body", "body": chunk,
                    "more_body": True})                                           #    每块一条消息,声明"还有下文"
    await send({"type": "http.response.body", "body": b"",
                "more_body": False})                                             # 3. 最后发一个空块,声明"完了"
```

`more_body: True/False` 就是 ASGI 表达"流式"的方式：不发 Content-Length，靠消息序列一块块推，以 `more_body: False` 收尾。DeerFlow 前端收到的每一段 agent 事件，对应这里的一次 `await send()`。

于是完整调用链是一层**俄罗斯套娃**——同一个三参数约定在两个尺度上复用：

```
uvicorn ──await app(scope, receive, send)──→ Starlette 路由
                                                │  handler 返回后:
                                                │  await response(scope, receive, send)   ← 同一个约定,再套一层
                                                ↓
                                         StreamingResponse.__call__
                                                │  await send(...) × (1 + N + 1)
                                                ↓
                                            uvicorn 写 socket
```

顺带一个彩蛋：同文件里的 `listen_for_disconnect`（`responses.py:242`）用 `await receive()` 监听 `http.disconnect` 消息——也是 ASGI 规范定义的消息类型。用户关掉浏览器标签页时，SSE 流就是这样停下来的。

### 8.2 `await send(...)` 的消息发给谁？

答案：**发给 uvicorn（服务器）——但它不是网络发送，是同进程的函数调用。**

回看 §6 时序第 3 步：`await app(scope, receive, send)` 的三个参数都是 uvicorn 组装/创建后递进来的。`send` 这个可调用对象**从头到尾都是 uvicorn 写的**，Starlette 只是从参数里接住它、存下来、在需要时调用。所以每执行一次 `await send({...})`，控制权就切进 uvicorn 的代码。

消息的完整旅程：

```
StreamingResponse: await send({"type": "http.response.body", "body": chunk, ...})
   ↓  同进程函数调用,"消息"只是个作为参数的普通 dict,没有离开 Python 进程
uvicorn 的 send 实现（协议翻译层）
   ↓  把 dict 翻译成 HTTP/1.1 chunked 编码的字节,写进 TCP socket     ← 网络从这里才开始
Nginx (2026, SSE 直通不压缩)
   ↓
浏览器 EventSource 收到一段事件
```

三个关键点：

1. **uvicorn 和应用在同一个 Python 进程里**。"消息"是字典，作为函数参数在进程内流动，零网络开销
2. **真正的网络动作只发生在一处**：uvicorn 把字典翻译成 HTTP 字节写 socket。应用自己永远不碰 socket
3. 方向对照：`send` = 应用→客户端（经 uvicorn 翻译），`receive` = 客户端→应用（经 uvicorn 翻译）。**uvicorn 是应用与网络之间唯一的翻译官，双向都经过它**

### 8.3 验证这个模型的彩蛋：TestClient 为什么能不联网测试

`fastapi.testclient`（Starlette 包装 httpx）能不监听端口、不联网地测试整个应用，原理正是：**它把 `send`/`receive` 换成了自己写的内存版实现**。你的 `await send({...})` 照样执行，只是消息落进内存缓冲区，再从缓冲区读出来断言，应用代码完全无感知。

这反过来印证了本篇的核心模型：应用对"send 后面接的是什么"一无所知也不需要知道——接的是 uvicorn+socket 也好、内存缓冲区也好，**插座形状不变就能换**。

---

## 第 9 节：在 DeerFlow 代码里的落点

把前面的概念锚到仓库的具体位置：

| 概念 | 仓库落点 |
|---|---|
| ASGI 服务器启动 | `backend/Makefile:10` / `Makefile:20`：`uvicorn app.gateway.app:app --host 0.0.0.0 --port 8001` |
| ASGI 应用本体 | `backend/app/gateway/app.py` 的 `app` 对象（FastAPI 实例，继承自 Starlette 的 ASGI 调用能力） |
| lifespan（ASGI 三种 scope 类型之一） | `backend/app/gateway/app.py:170-341`，详见 `lifespan-deep-dive.md` |
| SSE 流式（多次 `await send`） | Gateway 的 agent 事件流接口，前端在 Nginx（2026）后面消费 |
| 网络拓扑 | 浏览器 → Nginx(2026) → Gateway(8001，仅容器内部，不对外发布)；见根 `AGENTS.md` 的 Service Topology |

补充两点拓扑细节（来自根 `AGENTS.md`）：Nginx 是唯一对外入口，`/api/langgraph/*` 被改写为 Gateway 的原生 `/api/*` 路由；Nginx 刻意不压缩 SSE——流式响应要保持低延迟到达前端，中间不能被缓冲。

另外，`lifespan` 之所以存在，正是因为 ASGI 协议把 `scope["type"] == "lifespan"` 定义为协议的一部分：服务器启动/关闭时会以 lifespan 类型的 scope 调用应用，`startup` / `shutdown` 事件就是 Gateway 初始化数据库、调度器的时机。**"开机总电闸"本身也是 ASGI 协议约定的产物。**

---

## 第 10 节：自测清单

用下面几个问题检验是否真的掌握了（都在本文范围内）：

1. FastAPI 和 Starlette 的分工是什么？（§2）
2. ASGI 是一个库吗？如果不是，它是什么？（§3）
3. WSGI 哪三个场景搞不定，ASGI 怎么解决的？（§4）
4. `scope`、`receive`、`send` 各是什么？哪个不变、哪个用来收、哪个用来发？（§5）
5. 流式响应（SSE）在协议层面发生了什么？（§5.3 + §6）
6. `await app(...)` 这行代码里，调用方是谁的代码、被调用的是谁写的？（§7）
7. `send` 这个可调用对象是谁创建的？（§7）
8. DeerFlow 的 Gateway 进程里，"浏览器发来一个请求"到"你的 handler 被执行"之间有哪 8 步？（§6）
9. `lifespan` 和 ASGI 是什么关系？（§9）
10. `StreamingResponse` 和 ASGI 是什么关系？它自己是个 ASGI 应用吗？流式响应在消息层面长什么样？（§8.1）
11. `await send(...)` 的消息发给了谁？经过网络吗？在哪个环节才真正变成网络字节？（§8.2）
12. TestClient 为什么能不监听端口、不联网地测试整个应用？（§8.3）

如果第 6、7 题能不假思索答对（"uvicorn 调 Starlette"、"send 是 uvicorn 写的"），说明"标准 vs 实现"这层辨析已经吃透——这是本篇最容易被似懂非懂带过去的地方。如果第 11 题也能答对（"发给 uvicorn，同进程函数调用，写 socket 时才上网"），那么"ASGI 消息 ≠ 网络包"这层也通了。

---

## 延伸阅读

- `lifespan-deep-dive.md` —— ASGI lifespan 事件如何变成 Gateway 的开机/关机总闸（本文的后篇）
- `stream-bridge-deep-dive.md` —— SSE 事件流的上半程：agent 事件如何汇聚到出口（本文 §6 第 6 步的"上游"）
- `app-deep-dive.md` —— Gateway 应用本身的结构
- ASGI 规范原文：<https://asgi.readthedocs.io/>
- Starlette 文档：<https://www.starlette.io/>
