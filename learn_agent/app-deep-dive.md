# app.py 深度讲解：为什么"装一个 Gateway 并让它跑起来"不是 `FastAPI()` 加一串 `include_router` 就完事

本文是 `learn_agent` 系列的"总入口"篇。前面的姊妹篇各自认领了 Gateway 的一块器官：

- `lifespan-deep-dive.md`——开机/关机的总闸（`app.py` 里的 `lifespan` 函数）
- `langgraph-runtime-deep-dive.md`——`langgraph_runtime`：StreamBridge / RunManager / checkpointer / store 四件套怎么被建出来
- `stream-bridge-deep-dive.md` / `run-manager-deep-dive.md` / `run-event-store-deep-dive.md` / `get-checkpointer-deep-dive.md`——四件套各自的内部
- `start-run-deep-dive.md` / `run-agent-deep-dive.md`——一次 run 从路由到 agent 执行的全程
- `mcp-task-service-deep-dive.md` / `subagent-batch-service-deep-dive.md`——两个后台持久任务服务（它们在 `lifespan` 里的接线分别见其 §8.1 / §5）

但有两个问题没有任何一篇正面回答过：**① 这些零件所在的那个 FastAPI 应用本身，是怎么被装配出来的？** 中间件为什么是这个顺序、鉴权为什么拦得住所有路由、扩展（plugin）什么时候被加载进来、二十多个 router 为什么是这个挂载顺序、文件末尾那行孤零零的 `app = create_app()` 又是被谁执行的。**② 开机那几十秒里，Gateway 按什么顺序做了哪些事、每一站为什么在那里、为什么"提前"做？** 为什么要赶在服务前重建记忆检索索引、预热 tiktoken；为什么清理上传暂存文件必须挑开机那一刻；为什么关机要先停 IM channel、再 flush 记忆缓冲——这些是本篇的内容。

具体到源头，本文讲的是这一个文件的全貌：`backend/app/gateway/app.py`（910 行），分两大部分——**import-time 的"总装配车间" `create_app`（`app.py:577-896`）**（第 2 步的麻烦 1-5），和 **startup/shutdown-time 的开机/关机序列 `lifespan`（`app.py:193-574`）逐站讲解**（第 3 步）。

和 `lifespan-deep-dive.md` 的分工：那篇写于 lifespan 还比较短的时候（当时是 `app.py:170-341`），讲的是 lifespan 的**机制**——`yield` 怎么把函数劈成两半、`langgraph_runtime` 怎么嵌套、`AsyncExitStack` 的 LIFO、bounded shutdown、issue #3373——这些机制讲解至今成立。本篇第 3 步补的是它没来得及覆盖的**序列本身**：后来长出来的每一站（skill 投影、记忆索引预热、tiktoken 预热、上传清理、四个后台服务、关机段整段）各自"做什么、为什么存在、为什么在这个位置、为什么提前做"。建议两篇对照读。

本文面向第一次接触这套架构的同学。先把前置概念铺出来，再讲 6 个装配层的麻烦、第 3 步开机/关机序列逐站讲解，每段代码逐行解释。

---

## 第 0 步：先把要用的术语讲清楚

### 0.1 ASGI / uvicorn / FastAPI——三层各自管什么

这三个词经常被混用，先把边界画清：

- **ASGI** 是一个 Python 的异步 Web 协议规范。它的核心是一个极简的约定：一个 Web 应用就是一个可调用对象 `app`，服务器用 `app(scope, receive, send)` 调它——`scope` 是请求的元信息（方法、路径、headers……），`receive` 用来收请求体，`send` 用来发响应。任何满足这个约定的对象都叫 **ASGI 应用**。
- **uvicorn** 是一个 **ASGI 服务器**：它监听 TCP 端口、解析 HTTP 字节流、把它变成 `scope`/`receive`/`send`，然后调用你给它的那个 ASGI 应用。它只负责"接电话"，不负责"说什么"。
- **FastAPI** 是一个 **ASGI 框架**：它帮你把 `scope` 里的路径分发到对应的处理函数（路由）、做参数校验、管理中间件。它本身也是个 ASGI 应用，所以能被 uvicorn 拿去调。

三者接起来的形状就是启动命令（`backend/Makefile:19-20`、`backend/Dockerfile:92`）：

```bash
uvicorn app.gateway.app:app --host 0.0.0.0 --port 8001
#          └───────┬──────┘ └┬┘
#          模块路径：import app.gateway.app   模块里叫 `app` 的那个变量
```

冒号左边是模块的 import 路径，右边是模块里那个 ASGI 应用变量的名字。uvicorn 做的第一件事就是去 **import 这个模块**，然后从里面取出 `app` 这个变量——所以"import 这个模块"会触发什么副作用，是本篇麻烦 2/6 的主题。`app.py:909-910` 那两行就是为它准备的：

```python
# Create app instance for uvicorn
app = create_app()
```

打个比方：ASGI 是"插座标准"，uvicorn 是"墙上的插座"（提供电，即事件循环和网络），FastAPI 应用是"你造出来的电器"。`app.gateway.app:app` 就是告诉插座：电器在哪个房间、插头叫什么名字。

### 0.2 import-time vs startup-time——在本文件里的具体形状

※ 概念本身 `lifespan-deep-dive.md` §0.2 已经讲透（import 一个 `.py` 时模块顶层代码从上到下跑一遍 = import-time；uvicorn 起服务、跑 lifespan 时 = startup-time），这里只把它落到本文件上。

`app.py` 是整个仓库里 **import-time 代码占比最高**的一个文件，因为它一共只有四块，其中两块整个都是 import-time 的：

| 块 | 行号 | 执行时刻 |
|---|---|---|
| 模块头：import、别名、`logging.basicConfig`、两个常量 | `app.py:1-69` | import-time |
| 辅助函数：`_ensure_admin_user` / `_iter_store_items` / `_migrate_orphaned_threads` / `_warm_memory_retrieval` | `app.py:72-190` | 定义在 import-time，**调用**在 startup-time（被 `lifespan` 调） |
| `lifespan` | `app.py:193-574` | startup-time / shutdown-time（由 uvicorn 触发） |
| `create_app` + `_resolve_trace_enabled_for_app_construction` + `app = create_app()` | `app.py:577-910` | **import-time**（`app = create_app()` 在模块顶层） |

也就是说：**`import app.gateway.app` 这一句 = 造出整个应用**（装配中间件、挂全部路由、加载扩展）。谁 import 它，谁就触发装配——uvicorn 会 import 它，测试会 import 它，任何想拿 FastAPI app 对象的脚本也会 import 它。这个"谁 import 谁触发"的性质，决定了 `create_app` 里每一行代码的容错纪律（麻烦 2）。

而 `lifespan` 是 uvicorn 明确说"我要开始服务了/我要关闭了"才跑的，它才允许做那些重活——读 config.yaml、开数据库连接、起后台服务（姊妹篇的主场）。

### 0.3 中间件洋葱 + `add_middleware` 的"后加的在外层"规则——★本篇的核心概念

FastAPI/Starlette 的中间件模型叫**洋葱模型**：每个中间件把下一个 ASGI 应用包一层，请求从最外层往里穿，响应从最里层往外穿。`create_app` 里连续四次 `app.add_middleware(...)`（`app.py:675, 720, 731, 746`），就是在往洋葱上包四层皮。

关键问题是：**先 `add` 的皮在洋葱的外面还是里面？** Starlette 的答案是反直觉的——**后 add 的在外层**。看 Starlette 源码（本仓库 `.venv` 里的 `starlette/applications.py:98-101`）：

```python
def add_middleware(self, middleware_class, *args, **kwargs):
    if self.middleware_stack is not None:
        raise RuntimeError("Cannot add middleware after an application has started")
    self.user_middleware.insert(0, Middleware(middleware_class, *args, **kwargs))
    #             ↑ 每次都插到列表头部
```

构建中间件栈时从内往外逐层包裹，`user_middleware` 列表头部的（也就是**最后 add 的那个**）被包在**最外层**。所以 `create_app` 里四次 `add_middleware` 的书写顺序是 Auth → CSRF → CORS → Trace，实际请求的穿越顺序恰好**完全倒过来**：

```
请求 → TraceMiddleware → CORSMiddleware → CSRFMiddleware → AuthMiddleware → 路由处理函数
响应 ← TraceMiddleware ← CORSMiddleware ← CSRFMiddleware ← AuthMiddleware ← 路由返回值
```

这个顺序不是随手写的，每一层的位置都有安全语义——为什么 Trace 必须最外、为什么 CORS 必须在 Auth 外面、为什么 Auth 贴着路由——是麻烦 1 的全部内容。

打个比方：穿防护服。先穿贴身的隔离衣（Auth 最后 add → 最贴身），再穿防化服（CSRF），再穿外层罩衣（CORS），最后戴面罩（Trace 最先 add → 最外面）。穿衣顺序倒着读就是脱衣顺序，也倒着读就是请求穿越的顺序。**代码里"写"的顺序和运行时"穿"的顺序是反的**——读 `create_app` 时要始终在心里做这个翻转，否则会把每一层的意图读反。

### 0.4 `request.state`——每个请求自带的"便签本"

※ `app.state`（进程级工具箱）姊妹篇讲过；本篇反复出现的是它的"每请求版"亲戚 `request.state`。

Starlette 给每个进来的 HTTP 请求都挂了一个 `request.state` 对象，它就是一个空白的命名空间（随便往上贴属性），**只活这一个请求**。它的典型用途是让"前面的层"给"后面的层"传话，而不用改函数签名：

- `AuthMiddleware` 鉴权成功后，把解析出来的 `User` 对象盖到 `request.state.user` 上（`auth_middleware.py:1-12` 的模块 docstring 写明了这件事），后面的每个路由处理函数都能从 `request.state.user` 读到"调用者是谁"，而不需要自己再查一遍 JWT。
- `request.state.auth_source` 记录这次鉴权走的是哪条通道（session cookie / PAT / 内部调用 / auth-disabled），本篇麻烦 4 里 PAT 的处理就靠它。
- `create_app` 里塞到 `app.state` 上的扩展鉴权投影函数（麻烦 4），读的也正是 `request.state.user`。

打个比方：`app.state` 是前台桌子底下那口公共工具箱（整个进程共用）；`request.state` 是每个访客进门时发的一张访客牌（这个请求专用）——鉴权处在牌上盖个章（"张三，管理员"），后面所有部门看章办事，不用每个部门都重新查一遍访客登记本。

### 0.5 fail-closed vs fail-open——两种失败姿势

本篇会反复出现这一对词，先把定义钉死：

- **fail-closed（失败即关闭）**：出问题时，把功能**整个关掉/拒掉**，宁可服务不可用，也不带着错误的状态继续跑。典型形状：`raise`、返回 401/404。
- **fail-open（失败即放行）**：出问题时，**跳过这个功能、其余照常**。典型形状：`try/except Exception: logger.exception(...)` 然后继续。

判断标准是一条线：**这个失败的默认状态是"安全"还是"危险"？**

- 鉴权中间件失败了还放行请求 → 危险 → fail-closed（`auth_middleware.py` 模块 docstring 第一句就是 "fail-closed safety net"）。
- 可观测性 tracing 挂了还硬要启动 → 纯属自残 → fail-open（`app.py:241` 的注释原话："observability must never break startup"）。
- GitHub webhook 没配验签密钥还挂出路由 → 任何人都能伪造投递 → fail-closed 到"路由根本不存在"的程度（麻烦 5）。

本篇的麻烦 2/3/5 都是在同一条线上做切割：哪段代码 fail-closed、哪段 fail-open、为什么。

### 0.6 路由匹配按注册顺序——"先到先得"

FastAPI/Starlette 收到一个请求后，按 **router 里路由的注册顺序**逐个尝试匹配，**第一个匹配上的赢**。这意味着两件事：

1. 同一个路径如果被注册两次，**先注册的那个**永远生效，后注册的是死代码。
2. 如果想让某一批路由"永远优先"，就应该让它们**先注册**；想让某一批"只兜底、不抢戏"，就让它们**最后注册**。

`create_app` 挂了二十多个 router（`app.py:787-859`），而扩展贡献的 router 被故意排在整个函数的**最后一步**（`app.py:888-894`），并且挂载前还要做"确定性遮蔽检测"——就是利用这条匹配规则：扩展路由排后面，就永远不可能抢走宿主路由的流量；如果它声称的路径和宿主重叠，直接拒绝挂载并记账（麻烦 5）。

### 0.7 GatewayConfig（环境变量）vs AppConfig（config.yaml）——两套配置、两个时刻

※ AppConfig 的读取顺序和热重载契约，`lifespan-deep-dive.md` 麻烦 1 和 `langgraph-runtime-deep-dive.md` §0.6 讲过；这里只讲本文件新出现的那一半。

本文件里出现了两个名字很像的配置入口，它们是**两套体系**：

| | `get_gateway_config()`（`app/gateway/config.py:17-26`） | `get_app_config()`（harness 层） |
|---|---|---|
| 读什么 | 三个环境变量：`GATEWAY_HOST` / `GATEWAY_PORT` / `GATEWAY_ENABLE_DOCS` | 仓库根的 `config.yaml`（业务总配置） |
| 默认值 | `0.0.0.0:8001`、docs 开 | （没有 config.yaml 就 `FileNotFoundError`） |
| 缓存 | 模块级单例 `_gateway_config`，进程内只读一次 env | 每次调用重新解析（这是"热重载"契约的根基） |
| 什么时候读 | **import-time**（`create_app:583`）和 lifespan 各读一次 | lifespan 里严格读一次（`app.py:205`）；import-time 只允许"缺了容忍"的读法（麻烦 2） |
| 热重载 | 不需要（env 改了本来就得重启进程） | 大部分字段改了即时生效；`log_level` / `logging` / `plugins` 等少数字段注册为 startup-only（`reload_boundary.py`） |

为什么 GatewayConfig 敢在 import-time 读？因为它只依赖**环境变量**——进程能被启动，env 就一定在，不存在"config.yaml 还没生成"的问题。而 AppConfig 背后是那个 gitignored 的 `config.yaml`，测试和 CI 环境（甚至 `make config` 之前的新装机器）可能根本没有它。这就是麻烦 2 要讲的纪律。

### 0.8 扩展（plugin / extension）——什么时候进来、能贡献什么

DeerFlow 的第三方 Python 扩展由 `config.yaml` 顶层的 `plugins:` 列表声明（operator 手控——这一栏会导致 import 别人的代码，所以刻意不放进 API 可写的 `extensions_config.json`）。扩展能贡献五种东西：中间件、任务生命周期钩子、系统消息观察者、Gateway 后台服务、FastAPI 路由（详见 `backend/packages/harness/deerflow/extensions/AGENTS.md` 和参考实现 `examples/deerflow-extension-example/`）。

本文件是扩展进入进程的**唯一入口**：`create_app` 中段那一大段（`app.py:751-785`）负责"加载"，函数末尾（`app.py:888-894`）负责"挂路由"，`lifespan` 里由扩展运行时负责"起服务"。麻烦 3 讲加载的容错分界，麻烦 4 讲加载后扩展拿到的鉴权视图，麻烦 5 讲路由挂载的顺序纪律。

---

## 第 1 步：朴素写法长什么样

如果只看"把一个 API 服务跑起来"这个目标，一个 Gateway 模块大概会写成这样（**这是反面教材，真实代码不是这样**）：

```python
# app.py 模块顶层 —— 反面教材

from fastapi import FastAPI
from my_routers import models, threads, runs          # ① 所有路由一股脑挂上

app = FastAPI()                                        # ② 裸应用，没有中间件
app.include_router(models.router)
app.include_router(threads.router)
app.include_router(runs.router)
# ... 二十几个 include_router

@app.get("/health")
async def health():
    return {"status": "healthy"}

config = load_yaml("config.yaml")                      # ③ import 时读配置文件
plugins = config["plugins"]
for p in plugins:                                      # ④ import 时执行第三方代码
    load_extension(p)

app.include_router(any_extension_router)               # ⑤ 扩展路由和宿主路由混着挂
```

这段代码能跑，但它有六个层次的坑，每个都对应真实代码里一段煞费苦心的设计：

1. **② 没有中间件**：任何人、不带任何凭据，就能调用所有接口。没有鉴权、没有 CSRF、没有 CORS、没有请求级 trace id。而且中间件之间还有顺序语义——不是"加上就行"。
2. **③ import 时读 config.yaml**：测试、CI、`make config` 之前的新机器，import 这个模块就会崩。**import 应该总是成功的**——连"起来失败"的资格都没有的东西，不该连累别人 import 失败。
3. **④ import 时执行第三方代码**：`plugins:` 列表意味着执行不受信任面的代码。import 时执行 = 连 `python -c "import app.gateway.app"` 这种最无害的动作都会触发它，而且此时连日志、错误报告都还没就位。第三方代码挂了怎么办？`required: true` 的扩展挂了怎么办？
4. **⑤ 扩展路由混挂**：扩展声明了一个 `/api/threads/{thread_id}/runs` 呢？注册顺序决定匹配顺序，扩展可能整段接管宿主的核心路由。
5. **没有 `/setup` 引导路径**：第一个管理员从哪来？没有鉴权体系时创建的旧数据（孤儿 thread）归谁？
6. **uvicorn 拿什么**：`uvicorn app.gateway.app:app` 要求模块顶层就有一个组装完毕的 `app` 变量——朴素写法里它和"import 时副作用"纠缠在一起，测试没法构造隔离的 app 实例。

下面逐个看真实代码怎么解。

---

## 第 2 步：6 个麻烦逐一展开

### 麻烦 1：中间件的洋葱顺序是安全语义，不是装饰

`create_app` 的前半段连续包了四层中间件（书写顺序）：

```python
# Auth: reject unauthenticated requests to non-public paths (fail-closed safety net)
app.add_middleware(AuthMiddleware)                          # app.py:675  ← 第 1 个 add

# CSRF: Double Submit Cookie pattern for state-changing requests
app.add_middleware(CSRFMiddleware)                          # app.py:720  ← 第 2 个

cors_origins = sorted(get_configured_cors_origins())
if cors_origins:                                            # app.py:729-731 ← 第 3 个（有条件）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=list(CORS_EXPOSED_HEADERS),
    )

app.add_middleware(TraceMiddleware, enabled=_resolve_trace_enabled_for_app_construction())
                                                            # app.py:746  ← 第 4 个（最后一个）
```

按 0.3 讲的"后 add 的在外层"翻转过来，实际洋葱是：

```
           ┌────────────────────────────────────────────────────────┐
           │ TraceMiddleware（最外层）                                │
           │   ┌────────────────────────────────────────────────┐   │
           │   │ CORSMiddleware                                  │   │
           │   │   ┌────────────────────────────────────────┐   │   │
           │   │   │ CSRFMiddleware                          │   │   │
           │   │   │   ┌────────────────────────────────┐   │   │   │
           │   │   │   │ AuthMiddleware（最内层，贴路由） │   │   │   │
           │   │   │   │   ┌──────────────────────┐     │   │   │   │
           │   │   │   │   │  全部业务路由          │     │   │   │   │
           │   │   │   │   └──────────────────────┘     │   │   │   │
           │   │   │   └────────────────────────────────┘   │   │   │
           │   │   └────────────────────────────────────────┘   │   │
           │   └────────────────────────────────────────────────┘   │
           └────────────────────────────────────────────────────────┘
```

现在逐层回答"为什么它必须在这一层"。

**TraceMiddleware 为什么最外？** 因为它是唯一一个"无论请求成败都该工作"的层。它的职责是给每个 HTTP 请求绑一个 trace id、写进响应头 `X-Trace-Id`（`trace_middleware.py:23-24`），并放进日志上下文。如果它不是最外层，401 响应（Auth 拒的）、403 响应（CSRF 拒的）、CORS preflight 响应就都没有 trace id——而排查"为什么我被 401 了"恰恰是最需要 trace id 的场景。它还是纯 ASGI 中间件（直接操作 `scope`/`send`；四层里 Trace 和 CORSMiddleware 是纯 ASGI，CSRF 和 Auth 是 `BaseHTTPMiddleware`），包在最外面开销最小。

还有一层精妙：它的 `enabled` 开关是 **startup 快照**而不是每次请求现读（`trace_middleware.py:26-34` 的 docstring 专门解释了这件事）。`logging.enhance.enabled` 开启后，`configure_logging()` 在 lifespan 启动时才往 root handler 上装 trace 过滤器和增强格式器；如果中间件改为每请求热读这个开关，就会出现"响应头和 Langfuse 已经带上 trace id、日志格式器却还没装"的半同步状态——所以 `logging` 整节在 `reload_boundary.py` 里被注册为 startup-only，改了就要重启。

**CORSMiddleware 为什么在 Auth 外面？** 这是四层里最硬的一条顺序约束。浏览器在发出任何"非简单请求"（带自定义头、跨域 POST JSON……）之前，会先发一个 **OPTIONS preflight 预检**——这个预检**不带 cookie、不带 Authorization**，它问的不是"我是谁"，而是"服务器允不允许你们这个 origin 来访问"。如果 CORS 在 Auth 里面，预检会先撞上 Auth：没 cookie → 401 → 浏览器判定跨域不可用 → 前端全挂。所以 CORS 必须站在 Auth 前面，把 preflight 截下来直接答掉。

另一个细节藏在 `expose_headers=list(CORS_EXPOSED_HEADERS)`（`app.py:737`）：`CORS_EXPOSED_HEADERS = ("Content-Location",)`（`csrf_middleware.py:133`）。`app.py:722-728` 的注释解释了为什么——默认情况下，跨域 JS 只能读到 7 个 safelist 响应头；Gateway 新建 run 时把 run id 放在 `Content-Location` 头里返回，分体部署（前端和 Gateway 不同源）的浏览器客户端必须被显式授权读到它，否则 SDK 永远不知道 run 建好了，新建的 thread 停在占位路由上，所有"基于已建立 thread"的入口在刷新页面前全部隐藏。**CORS 允许列表和 CSRF 的 origin 校验共享同一份来源**（`get_configured_cors_origins()`，`app.py:729`），两套检查不会漂移。

还要注意它是**有条件的**：`if cors_origins:` ——统一从 nginx 单一入口访问（默认部署形态，同源）时允许列表为空，这层皮根本不穿。CORS 是给"分体部署"显式 opt-in 的。

**CSRFMiddleware 为什么在 Auth 里面、CORS 里面？** CSRF 防的是"攻击者借受害者浏览器里的 cookie 发请求"（Double Submit Cookie 模式：cookie 里放一个 token，请求头里必须带同一个 token，攻击者的跨站请求拿不到这个值）。它检查的是**请求的形状**（method、origin、cookie 配对），而不是调用者身份，所以它不需要在 Auth 之后；但它的豁免逻辑需要知道鉴权状态——比如 `csrf_middleware.py:226` 附近，带 `Authorization` 头的请求（PAT，#4849）豁免 cookie 配对检查（PAT 请求天然没有 CSRF 语义），而 auth-disabled 模式下整个 CSRF 检查跳过（`csrf_middleware.py:48-49`）。这些判断都建立在"请求会带着什么凭据"的模型上，放在 Auth 内侧、路由外侧正合适：安全方法（GET/HEAD/OPTIONS）不检查（`should_check_csrf`，`csrf_middleware.py:39-44`），状态变更方法检查。它同样是 `BaseHTTPMiddleware`。

**AuthMiddleware 为什么贴着路由（最内）？** 它的产物要被所有路由消费：鉴权成功后把 `User` 盖到 `request.state.user` 和 `user_context` contextvar 上（`auth_middleware.py:4-8`），repository 层的多租户过滤靠 contextvar 自动生效。它必须包住**所有**业务路由——但它不需要包 CORS preflight（预检没有身份可言）。它是 fail-closed 的：所有非公开路径，没有有效凭据一律 401（模块 docstring 原话 "fail-closed safety net"）。公开路径是白名单（`auth_middleware.py:34-58`）：`/health`、`/docs`、`/redoc`、`/openapi.json`、OAuth 回调前缀、`/api/webhooks/`（webhook 靠提供方签名自证，如 GitHub 的 `X-Hub-Signature-256` HMAC，不靠 cookie），加上登录/注册/登出/初始化等少数精确路径。细粒度的权限检查不在这层做——留给 `authz.py` 的装饰器，这层只做"没带票的一律进不来"的总闸。

**书写顺序和运行顺序相反**这个翻转规则，是读这段代码最容易犯的错。`app.py:675` 的 Auth 在源码里排第一，运行时却最深；`app.py:746` 的 Trace 在源码里排最后，运行时却最先碰到请求。

### 麻烦 2：`create_app` 跑在 import-time——对 config.yaml 只能"缺了容忍、坏了失败"

0.2 说过，`create_app` 在 import-time 执行。那么它里面的每一次 `get_app_config()` 都要回答一个问题：**此刻 config.yaml 不存在怎么办？**

`create_app` 里有两处碰 AppConfig，两处用了同一种宽容策略——但只宽容一种失败：

```python
    # Resolving the configured plugin list is deliberately outside the
    # fail-open guard below: a config.yaml that exists but cannot be parsed or
    # validated is a configuration failure, not an extension failure. ...
    try:
        configured_plugins = get_app_config().plugins
    except FileNotFoundError:
        logger.debug("config.yaml not found while constructing Gateway app; loading no extensions for this app instance")
        configured_plugins = []
```

（`app.py:768-772`；`_resolve_trace_enabled_for_app_construction` 的 `app.py:899-906` 一模一样。）

逐行读这段注释，它把一条纪律讲得很清楚：

- **只捕获 `FileNotFoundError`**——"文件不存在"被降级为 debug 日志，然后继续（扩展列表为空 / trace 中间件关闭）。
- **解析失败、校验失败照常抛**——config.yaml 存在但内容是坏的，`get_app_config()` 抛的就不是 `FileNotFoundError`，会直接炸出 `create_app`，import 失败，服务起不来。

为什么这两种失败要区别对待？看注释的论证："a config.yaml that **exists** but cannot be parsed or validated is a **configuration failure**, not an extension failure"。翻译成 0.5 的语言：

- **文件不存在**：说明这台机器本来就还没部署（没跑过 `make config`），或者这是个只想拿 app 对象的测试。此时"没有配置"是一个**已知的、合法的状态**，降级继续——注意 `app.py:766` 注释的最后一句话："create_app() runs at import time, and lifespan still performs strict config loading before serving"——**import 时的宽容不是终点，lifespan 启动时还会严格读一次**（`app.py:204-224`，失败直接 `raise RuntimeError` 拒绝启动）。所以宽容不会漏接：真要起服务，坏配置在 lifespan 那一关还是死；只是"import"这个动作本身永远是安全的。
- **文件存在但坏了**：操作者明确表达了配置意图，但意图无法理解。这种失败如果被静默降级（比如当成"没有插件"），`required: true` 的扩展就会被无声丢弃——操作者以为它加载了，实际没有，**静默改变生产行为**比崩溃危险得多。所以炸出来，让 uvicorn 起不来，让失败可见。

打个比方：邮局清晨开门。门牌还没挂（文件不存在）→ 先按"无地址营业"模式开张，等正式营业检查（lifespan）再说；挂了门牌但里面的营业执照是涂改过的（文件存在但解析失败）→ 当场停业整顿，绝不带着假执照营业。

这里还有一个对比值得点出：同一行 `get_gateway_config()`（`app.py:583`）却在 import-time 读得理直气壮，因为它走的是 0.7 讲的环境变量体系，不存在"文件还没生成"的问题。**"import-time 能读什么"取决于读的东西缺不缺都合法**——env 缺了有默认值，config.yaml 缺了是合法状态、坏了才是事故。

### 麻烦 3：扩展加载的 fail-open / fail-closed 分界，以及"为什么必须在装配时加载"

`create_app` 中段是整个扩展体系的引水渠（`app.py:751-785`）：

```python
    from deerflow.extensions import (
        EMPTY_EXTENSIONS,
        ExtensionLoadError,
        initialize_runtime_diagnostics,
        load_extensions,
        record_runtime_diagnostics,
        set_loaded_extensions,
    )

    try:
        configured_plugins = get_app_config().plugins      # ← 麻烦 2 讲过的宽容读
    except FileNotFoundError:
        ...
        configured_plugins = []

    try:
        loaded_extensions, extension_diagnostics = load_extensions(configured_plugins)
    except ExtensionLoadError:
        # `required: true` makes the extension part of the startup contract.
        # Booting without it would silently change configured behaviour.
        raise
    except Exception:
        logger.exception("Extension loading failed; continuing with no extensions")
        loaded_extensions, extension_diagnostics = EMPTY_EXTENSIONS, []
    set_loaded_extensions(loaded_extensions)               # ← 进程级单例
    app.state.extensions = loaded_extensions
    app.state.extension_diagnostics = initialize_runtime_diagnostics(extension_diagnostics)
```

（`app.py:774-785`，有删节省略号；`plugins` 读取段在 `app.py:768-772`。）

这段的精妙全在**两层 try 的三种结局**上：

- **结局一（fail-closed）**：`ExtensionLoadError` → `raise`。`ExtensionLoadError` 是 `load_extensions` 对 **`required: true` 扩展**加载失败的专用异常。`app.py:777-778` 的注释给出了理由："required 的扩展是 startup contract 的一部分，不带着它启动会 silently change configured behaviour"——和麻烦 2 的"坏配置必须炸"是同一条哲学：**操作者声明了"没它不行"，那就真的没它不行，宁可拒绝启动。**
- **结局二（fail-open）**：其它任何异常 → 记 exception 日志、空扩展继续。普通扩展（没标 required）的代码在 import/初始化时崩了，Gateway 依然起服务——扩展是增强，不是地基（0.5 的判断标准：缺它不危险，硬拖死整个 Gateway 才危险）。
- **结局三（正常）**：`set_loaded_extensions(loaded_extensions)` 把结果写进**进程级单例**，同时挂到 `app.state.extensions`，诊断信息初始化后也挂上。

为什么加载必须发生在 `create_app`（import-time），而不是 `lifespan`（startup-time）？两个原因：

1. **消费方在进程级单例里读它**。`reload_boundary.py` 的 `STARTUP_ONLY_FIELDS["plugins"]` 写明了运行时形状："load_extensions() runs once during create_app() and the process-wide middleware registry is not rebuilt on config.yaml edits; adding, removing or reconfiguring a plugin requires a restart."（`reload_boundary.py:36-37`）agent 的中间件构建器在每次 run 时从 `set_loaded_extensions` 设下的进程单例里拿这份**不可变集合**；如果加载推迟到 lifespan，那"import 完但还没起服务"的窗口里，任何读单例的代码拿到的都是空集，行为就分叉了。**装配时刻加载一次、此后冻结**，是"startup-only"契约能被守住的结构前提。
2. **`import` 副作用的确定性**。扩展加载 = import 第三方代码 = 可能有 import 副作用（比如 GitHub channel 的 `ChannelRunPolicy` 就是靠 import 副作用注册的——见麻烦 5 里 webhooks 路由的注释 `app.py:866-867`）。这些副作用必须发生在任何请求、任何 run 之前，也就是 `create_app` 里，而不是服务已经开始接受流量之后。

注意 `app.py:762-765` 注释里那个"deliberately outside the fail-open guard"的设计：`get_app_config().plugins` 这一句**故意放在**失败降级 `try` 的**外面**一层（自己的 `try` 只捕 `FileNotFoundError`）。如果把"读 plugins"和"加载扩展"塞进同一个 fail-open 块，那么**坏配置**就会被伪装成**扩展加载失败**——静默丢弃一个 `required: true` 扩展而不是拒绝启动。**错误分类错了，容错策略就全错了**：配置错误按扩展失败降级，是这段代码最容易犯、也特意防住的错。

### 麻烦 4：鉴权投影——给扩展"能问的问题"，不给"能抓的把手"

`create_app` 里有一个看起来不起眼、安全上却极讲究的函数（`app.py:686-717`）：

```python
    def _resolve_extension_principal(request):
        """Project the host's auth context into the neutral extension shape.

        Deliberately a projection, not a handle: an extension gets the
        questions it may ask (who, is that an admin, and what role they
        hold), not the host's AuthContext, which would pin every extension to
        its internals.
        """
        user = getattr(request.state, "user", None)
        if user is None:
            return None
        system_role = getattr(user, "system_role", None)
        # PAT credentials never carry admin capability (#5041): suppress every
        # admin signal — both ``is_admin`` and the ``admin`` role — so an
        # admin-owned PAT cannot regain admin through extension-side
        # require_admin, mirroring deps.is_admin_user's PAT guard.
        auth_source = getattr(request.state, "auth_source", None)
        is_pat = auth_source == AUTH_SOURCE_PAT
        is_admin = system_role == "admin" and not is_pat
        roles = () if is_pat and system_role == "admin" else (system_role,) if isinstance(system_role, str) and system_role else ()
        return ExtensionPrincipal(
            user_id=str(user.id),
            is_admin=is_admin,
            is_internal=auth_source == AUTH_SOURCE_INTERNAL,
            roles=roles,
        )

    setattr(app.state, EXTENSION_PRINCIPAL_RESOLVER_KEY, _resolve_extension_principal)
```

先看它解决的结构问题。扩展是**独立发行的包**（`deerflow_extension_api` 是它们唯一被允许的公共接口），但扩展 inevitably 会问"调用我的人是不是管理员"。如果宿主直接把内部的 `AuthContext` 对象递过去：

1. 扩展就 pin 死在宿主内部类的字段结构上——宿主重构 `AuthContext`，所有已发行的扩展全炸；
2. 扩展拿到的是**把手**（可变对象、可继续深挖的引用），而不是**答案**。

所以注释说 "Deliberately a projection, not a handle"——宿主把内部上下文**投影**成一个中立的值对象 `ExtensionPrincipal`（谁、是不是管理员、是不是内部调用、持什么角色），四个答案全是不可变的事实。扩展拿到的是"能问的问题"，不是"能抓的把手"。这就是 `EXTENSION_PRINCIPAL_RESOLVER_KEY` 这个 API 层常量存在的意义：`app.state` 上以它为键存放投影函数，扩展通过公共 API 取用，宿主内部怎么变都不影响这个契约。

再看那两行"看起来啰嗦"的 PAT 逻辑（#5041）。PAT（Personal Access Token）是一种长期凭据，通常签给自动化脚本。宿主侧的安全规则（`deps.py` 的 `is_admin_user`）是：**PAT 永远不携带管理员能力**——哪怕这个 PAT 属于管理员本人。为什么？因为 PAT 会进 CI 配置、脚本文件、聊天记录，泄漏面远大于会话 cookie；管理员把它签出去的那一天，就不该再有管理员权限。问题在于：扩展侧也有自己的 `require_admin`。如果投影函数如实报告"system_role == admin"，一个持有管理员 PAT 的调用者就能在**扩展贡献的路由**上重新拿回管理员——宿主路由上被拦死的，从扩展路由绕回去了。所以投影函数在出口处再拦一次：

- `is_admin = system_role == "admin" and not is_pat`——`is_admin` 信号被压制；
- `roles` 那一行——管理员 PAT 连 `"admin"` 这个角色名都从 `roles` 元组里抹掉（返回空元组），防止扩展绕开 `is_admin`、自己去查 `roles` 里有没有 `"admin"`。

**两条通道都得堵**，只堵一条等于没堵。这就是"mirroring deps.is_admin_user's PAT guard"的含义：同一条安全规则必须在宿主侧和投影侧各实现一次，因为这是两个不同的出口。

最后一个细节：为什么这个函数是**同步**的（不是 `async def`）？`app.py:679-684` 的注释说明了——它镜像 `require_admin_user` 的主路径，只读 `request.state.user`（AuthMiddleware 在任何路由跑之前就盖好了章，同步读就够），不需要异步查库；保持同步，扩展的 `resolve_principal` / `require_admin` 才能在同步和异步路由处理函数里都被调用。**把"必须异步"压缩到最小集合**，扩展作者的调用面才不会被无谓地限制。

### 麻烦 5：路由注册顺序 = 匹配顺序；条件挂载 fail-closed；扩展路由最后

`create_app` 的后半段（`app.py:787-894`）挂全部路由。0.6 说过，注册顺序就是匹配顺序——这段代码把这个性质用出了三层花样。

**第一层：宿主路由全部先挂，包括条件路由和内联路由。** `app.py:787-859` 顺序挂了二十多个 router（models → features → console → mcp → mcp_tasks → subagent_batches → memory → skills → integrations → artifacts → browser → uploads → threads → scheduled_tasks → agents → subagents → suggestions → input_polish → channel_connections → channels → assistants_compat → auth → feedback → thread_runs → runs），`app.py:879-886` 再内联一个 `/health`。次序本身没有抢路径的冲突（每家前缀不同），重要的是它们**全部**排在扩展路由之前——宿主对自家路径有绝对优先权。

**第二层：GitHub webhooks 的条件挂载——fail-closed 到"404"的程度。** `app.py:861-877`：

```python
    # Fail-closed: only mount the route when a webhook secret is configured
    # (or when the explicit DEER_FLOW_ALLOW_UNVERIFIED_GITHUB_WEBHOOKS=1
    # dev opt-in is set). A misconfigured deployment without a secret cannot
    # serve forged deliveries because the URL responds 404 — there is no
    # handler to reach.
    if github_webhooks.is_route_enabled():
        app.include_router(github_webhooks.router)
        logger.info("GitHub webhooks route mounted at /api/webhooks/github")
    else:
        logger.warning("GitHub webhooks route NOT mounted: GITHUB_WEBHOOK_SECRET unset and ...")
```

这段有两层设计值得逐行看。第一层是**没有路由比没有校验安全**：webhook 路由被 auth 和 CSRF 中间件双双豁免（它靠提供方 HMAC 签名自证身份，见麻烦 1 里 Auth 白名单的注释），那么"密钥没配"时如果照常挂路由，签名校验就没有输入、任何人 POST 一个假投递进来都能触发内部逻辑。对策不是"挂上路由、签名失败时拒绝"——那要赌校验代码永远无 bug——而是**路由根本不存在**：URL 回 404，攻击者没有 handler 可以够到。**fail-closed 的极致不是"拒绝执行"，是"没有东西可执行"。** 未配密钥时打一条 warning（而不是 error / raise），因为 webhook 是可选功能，没配它不构成"带病运行"。

第二层藏在注释里（`app.py:865-867`）：include 这个 router 会**传递性 import** `app.gateway.github`，而后者靠 **import 副作用**注册 GitHub channel 的 `ChannelRunPolicy`。这呼应麻烦 3 的第 2 点：import 副作用是扩展/channel 体系的一个真实机制，因此"谁 import 谁、什么时候 import"必须被当回事——条件挂载连 import 一起跳过，未启用 GitHub 时这个模块根本不进内存。

**第三层：扩展路由最后 + 确定性遮蔽检测 + 原子回滚。** `app.py:888-894`：

```python
    from deerflow.extensions.gateway import include_contributed_routers

    record_runtime_diagnostics(include_contributed_routers(app, loaded_extensions))
```

`include_contributed_routers`（`packages/harness/deerflow/extensions/gateway.py:526` 起）做三件事：

1. **先登记宿主既有路由的所有权**：扫描 `app.routes`，把每个路径模式记成一条 "owner = host" 的主张。
2. **挂载前做遮蔽检测**：对每个扩展 router 的每条路由，问一个问题——"它声称的路径，是不是已经被前面某个 owner **覆盖**了？"（`_find_route_clash` + `_matcher_covers`，连 `{thread_id:int}` 和 `{thread_id:str}` 这种转换器宽度的差异都算）。如果答案是否——比如扩展声明 `/api/threads/{thread_id}/runs`，而宿主已经注册了这个模式——这条路由会**必然**在匹配顺序上被宿主吃掉，扩展等于白挂还自我感觉良好。这叫 **definite shadow（确定性遮蔽）**，直接拒绝：记一条 `Diagnostic.error`（"router path ... is already served by host; this router was not mounted"），log 一条 error，**continue**。注意分寸：只有**确定性**遮蔽才拒——如果只是"可能重叠"（比如宿主有 `/{something:path}` 这种通配），不拒，因为 FastAPI 的匹配还受 method、转换器等影响，误杀比放过更糟。没有被遮蔽的无关路由**照常挂载**——一个扩展的坏路由不连累其它扩展（fail-open 粒度在"单条路由"，不在"整个扩展"）。
3. **挂载本身是原子的**：`include_router` 内部是逐条复制路由，如果复制到一半抛异常，`gateway.py:552-560` 用 `route_mark` 记录的长度把 `app_routes[route_mark:]` 整段删掉——**半挂载状态不允许存在**。否则宿主的路由表里会留下一个"挂了一半"的扩展路由集合，行为不可预测。

为什么这一切必须建立在"扩展路由最后挂"之上？因为 0.6 的规则：先注册先匹配。宿主路由先挂，就已经在任何扩展路由之前占住了所有匹配优先级；遮蔽检测是在这之上再补的"结构保证"——就算某个扩展的路由能匹配，宿主也永远先匹配。**顺序是安全属性**，和麻烦 1 的中间件顺序是同一个主题在路由层的重演。

### 麻烦 6：模块尾部 `app = create_app()`——import 即装配，以及文件里其余的拼图

**`app.py:909-910`：**

```python
# Create app instance for uvicorn
app = create_app()
```

这两行是 0.1/0.2 两条线索的汇合点。`uvicorn app.gateway.app:app`（`backend/Makefile:19-20`、`backend/Dockerfile:92,127`）会 import `app.gateway.app` 这个模块——import 执行到模块最后一行，`create_app()` 被调用，**整个装配（四层中间件、扩展加载、二十多个路由、投影函数）在这一刻完成**，产出的 FastAPI 实例赋给模块变量 `app`，uvicorn 从这里取走它。

这个安排带来一个必须内化的推论：**任何 import 这个模块的代码，都会完整触发一次装配。** pytest 里 `from app.gateway.app import app`、写脚本拿路由表、IDE 的类型检查 import……全都跑一遍 `create_app`。这就是麻烦 2 那条"import 永远安全"纪律的物理来源——装配里最危险的动作（读 config.yaml、执行扩展代码）都已经分别被"缺失容忍"和"fail-open/fail-closed 分界"驯化了。也正因为装配是纯函数式的（`create_app() -> FastAPI`，不碰全局状态除了显式设计的进程单例），测试可以自己调 `create_app()` 造隔离实例，而不是共享那个模块级单例。

顺带把 `create_app` 的 `FastAPI(...)` 构造参数过一眼（`app.py:588-672`）：`title` / `description`（OpenAPI 文档首页的大段描述）/ `version` / `lifespan=lifespan`（把姊妹篇的主角挂进来——**lifespan 不是隐式按名字找的，是显式传进去的**，这是"这个文件里哪个函数是生命周期钩子"的唯一声明点）/ `docs_url` / `redoc_url` / `openapi_url`——后三个由 `config.enable_docs` 决定，关掉时传 `None`，路由就不存在（和 GitHub webhooks 的条件挂载是同一个模式：**不存在**优于**存在但拒绝**）；`openapi_tags` 给文档里的接口分组。

**`lifespan` 摘要**（`app.py:193-574`）：它的**机制**——`yield` 把函数劈成两半、嵌套 `langgraph_runtime`、`AsyncExitStack` 的 LIFO、关机每步有界、#3373 的 drain 顺序——`lifespan-deep-dive.md` 逐行讲透了；它的**开机/关机序列**——每一站做什么、为什么提前做、致命还是非致命——本篇第 3 步逐站讲。这里只需要记住它在哪个位置被声明：`FastAPI(lifespan=lifespan)`。

**四个模块级辅助函数**（`app.py:72-190`）也都值得点名，因为它们各自守着一个小设计：

- `_ensure_admin_user`（`app.py:72-141`）：首次启动（没有管理员）不自动建号，只打横幅让操作者去 `/setup` 手工建第一个 admin——**第一个管理员的诞生必须有人格担保**，自动建号等于开随后门；管理员已存在时，做一次性的"无鉴权时代 → 有鉴权时代"迁移：把 LangGraph store 里没有 `user_id` 的孤儿 thread 划给 admin。整个函数是尽力而为的：`get_local_provider()` 抛 `RuntimeError`（测试/启动早期鉴权层未就绪）就跳过，不炸启动（`app.py:99-105`）。
- `_iter_store_items`（`app.py:144-161`）：把旧的 `limit=1000` 一次拉齐改成游标分页（每页 500，短页即终），否则孤儿超过一页就**静默丢数据**——迁移代码里最阴的坑不是迁移失败，是迁移"成功"但只迁了一半。
- `_migrate_orphaned_threads`（`app.py:164-178`）：消费上面的游标，逐条给 `metadata.user_id` 补 admin id 后 `aput` 回写，返回迁移条数。
- `_warm_memory_retrieval`（`app.py:181-190`）：包一层 `try/except` 和 `asyncio.to_thread`，让检索索引重建在后台线程跑、失败只 warning——索引是**派生数据**（canonical 数据在内存文件里），重建失败时 DeerMem 会在请求期懒重建兜底，所以它天生就是非致命步骤。

最后是文件头那两块容易被跳过的东西。**两个常量**（`app.py:62-69`）：`_SHUTDOWN_HOOK_TIMEOUT_SECONDS = 5.0` 给每个关机钩子封顶（注释说明了动机：uvicorn 的 reload supervisor 会往卡在清理里的 worker 反复发信号，所以 worker 退出时间必须有界），`_RETRIEVAL_WARM_SHUTDOWN_TIMEOUT_SECONDS = 1.0` 给"派生索引"单独一个更小的预算——canonical 的内存 flush 保留完整预算，派生数据不值这个价。**`logging.basicConfig`**（`app.py:53-58`）：import-time 就把根日志的格式和 INFO 级别装好——因为从 import 到 lifespan 执行之间有一段真空（uvicorn 自己的启动日志、config 加载失败的 exception 日志都发生在 configure_logging 之前），没有它，这段真空期的日志要么没格式要么丢失；注释也写明了"lifespan overrides from config.yaml log_level"——正式级别由 lifespan 阶段的 `configure_logging(startup_config)` 接管。

---

## 第 3 步：开机/关机序列逐站讲解——每一站"做什么、为什么现在做、为什么在这里"

`create_app` 只是造好了机器，还没通电。uvicorn 拿到 `app` 后才执行 `lifespan`（`app.py:193-574`）——那里面是一条**严格排序的流水线**：开机段十几站，`yield` 交付服务期，关机段再按依赖关系逐站收尾。

本节按代码顺序逐站走。每一站回答四个问题：**做什么？为什么必须在这一刻做（而不是开机更早/更晚、而不是请求时）？为什么排在这个位置？失败了为什么是致命或非致命？** 先上总览表，再展开重点站。

### 3.0 流水线总览表

| # | 站点 | 行号 | 失败后果 | 一句话理由 |
|---|---|---|---|---|
| 1 | 读 `startup_config` + 子代理容量闸门 + 正式日志 + 浏览器运行时检查 + auth-disabled 警告 | `197-226` | **致命** | 后面所有步骤的输入和"规矩" |
| 2 | public skill 投影 | `228-232` | 非致命 | 沙箱挂载路径在第一次 acquire 前必须就位 |
| 3 | Monocle tracing | `234-242` | 非致命 | 可观测性不能绊倒业务（lifespan 篇麻烦 2） |
| 4 | 记忆检索索引**后台**重建 | `244-262` | 非致命 | 全量重建慢且是派生数据 → 后台跑、懒重建兜底 |
| 5 | tiktoken 预热（**同步等，最多 5 秒**） | `264-290` | 非致命 | 第一个用户不该付 BPE 下载的账单（#3402/#3429） |
| 6 | 上传暂存文件清理 | `292-297` | 非致命 | 开机是唯一"没人正在上传"的无竞态窗口 |
| 7 | `langgraph_runtime` 四件套 | `300` | **致命** | StreamBridge/RunManager/checkpointer/store（姊妹篇） |
| 8 | admin 引导 + 孤儿 thread 迁移 | `305` | 内部分级 | store 建好才能迁（lifespan 篇麻烦 5） |
| 9 | IM channel 服务 | `308-326` | 非致命 | 入站流量，尽早恢复接单 |
| 10 | 定时任务服务 | `328-348` | 非致命 | 后台批处理入口 |
| 11 | MCP 任务服务 | `350-408` | 配置矛盾 = **致命** | 校验失败拒绝启动 |
| 12 | 子代理批量服务 | `410-428` | enabled 无库 = **致命** | 承诺了持久就不能静默不持久 |
| — | `yield` —— 服务期 | `430` | — | 流水线交付，uvicorn 开始接流量 |
| 关1 | OIDC → channel → scheduler → mcp → batch → browser 会话 | `432-499` | 各自非致命 | 先停所有"生产者" |
| 关2 | retrieval 等待 → 停扩展观察 → 记忆 flush → 条件 close | `523-572` | 非致命 | 水龙头全关了才冲水 |

一个总原则先立住：**开机顺序是依赖图的拓扑序**（lifespan 篇麻烦 5 的结论），本表就是那张依赖图的具体节点——1 是所有节点的输入，2-6 不依赖 runtime 可以先行，7 是基础设施，8-12 依赖 7 的产物；关机顺序的原则则相反：**先停生产者，再 flush 消费者**（关段细讲）。

### 3.1 第 1 站：读 config + 立规矩（`app.py:197-226`，致命）

```python
    try:
        startup_config = get_app_config()
        ...
        configure_subagent_execution_capacity(subagent_runtime_config)
        configure_logging(startup_config)
        ensure_browser_runtime_available(startup_config)
        logger.info("Configuration loaded successfully")
        warn_if_auth_disabled_enabled()
    except Exception as e:
        error_msg = f"Failed to load configuration during gateway startup: {e}"
        logger.exception(error_msg)
        raise RuntimeError(error_msg) from e
```

config 是第一站，理由不言自明：**后面每一站"做不做、怎么做"都由 config 说了算**——memory 开不开（决定第 4/5 站）、scheduler 开不开（第 10 站）、技能投影读哪些目录（第 2 站）。输入没就位，流水线无从谈起。为什么致命也是显然的：读不到 config 的 Gateway 连"自己该是什么样"都不知道。

有意思的是 config 之后紧跟的四个"立规矩"动作，每个都有讲究：

- **`configure_subagent_execution_capacity`**（`app.py:216`）装的是**子代理执行的准入闸门**（进程级 FIFO 容量控制器：`max_running` / `max_queued` / 拒绝或排队策略，见 `deerflow/subagents/capacity.py`）。为什么它必须出现在**所有服务启动之前**？看后面的站：第 9-12 站的服务一起来就可能 launch run（IM 来消息、定时任务到点、MCP 通知、批量条目执行），run 里的 agent 会 spawn 子代理——**闸门必须在第一个可能的 run 之前就位**，否则最初的几个子代理就是不受控的。这是"先立规矩、再放人进场"：任何"消费方/参与者可能出现"的机制，其约束要先于参与者存在。
- **`configure_logging`**（`app.py:217`）：模块头的 `logging.basicConfig`（麻烦 6 讲过）只是真空期兜底；这一站才把正式级别和增强格式器装上。为什么排在第 2 站之前？**后面每一站都要打日志**——技能投影失败、预热超时，这些日志必须以正式格式出现，排障时才认得出。
- **`ensure_browser_runtime_available`**（`app.py:218`，`browser_capability.py:67-72`）：检查"配置了浏览器控制，但运行环境跑不起来"（依赖没装、多 worker 约束不满足等），**configured 但不可用 → 直接 `RuntimeError` 拒绝启动**。为什么这么狠？因为这是**操作者显式配置了的能力**——他要的浏览器自动化坏了，等到第一次 run 真去调 `browser_navigate` 时才炸，炸在用户的对话里、藏在 run 的错误事件里；开机时炸，uvicorn 起不来，部署流水线立刻红。和 0.5 的 fail-closed 判断标准一致：**显式承诺的功能坏了 = 危险，要 fail-closed**。对比第 3 站的 Monocle（没人显式承诺要 tracing，off by default）就非致命——同一条标准，两种配置姿态，两种后果。
- **`warn_if_auth_disabled_enabled`**（`app.py:220`）：`DEER_FLOW_AUTH_DISABLED` 是个开发后门，开着时所有请求免鉴权。它不能阻止你启动（开发场景需要），但必须在开机日志里**大声警告**——后门可以留，但不能静默留。

### 3.2 第 2 站：public skill 投影——为什么公共的提前做、用户的懒修复（`app.py:228-232`）

```python
    from deerflow.skills.projection import ensure_public_skill_projection

    public_projection_ready = await asyncio.to_thread(ensure_public_skill_projection, app_config=startup_config)
    if public_projection_ready:
        logger.info("Ensured the public skill projection; user projections repair lazily on sandbox acquire")
```

先讲**投影是什么**。agent 在沙箱里看到的技能目录，不是 `skills/` 源目录本身，而是一份**物化副本（投影）**：只包含"已启用"的技能、按 public / custom / legacy / integrations 四类稳定根目录组织（`SkillProjectionPaths`，`projection.py:44-49`）。为什么要投影而不是直接挂源目录？因为"agent 能看到什么"必须是**白名单**（只有启用的技能），且挂载进沙箱的路径结构要稳定——隔离与安全都住在投影里（`projection.py:100` 注释原话："Isolation must live in the projection itself"）。

然后是这一站最值得学的对比——**公共投影开机做，用户投影懒修复**。`ensure_public_skill_projection` 的 docstring（`projection.py:585-591`）把理由写得很直白：

> User projections are repaired lazily by sandbox acquire. **Eagerly rebuilding every historical user would make gateway readiness scale with tenant count**, while providing no additional safety before that user's next acquire.

拆开说：公共技能是**部署级**的，全系统就一份，开机建一次的代价是常数；用户投影是**每用户一份**的，历史用户可能成千上万，开机全部重建会让"Gateway 多久能接流量"随租户数**线性恶化**——而且用户下次 acquire 沙箱之前根本用不到他的投影，提前建好不提供任何额外安全。所以：公共的（常数代价、人人马上要用）开机同步做；用户级的（代价随规模涨、单用户用到才需要）懒修复。**"启动时间不许随数据规模增长"**是一条硬约束，凡是"每 X 一份"的东西都不许在开机路径上全量物化。

失败分级是**非致命**的——但注意它和第 4/5 站的非致命不一样：`app.py:230` 这一行**外面没有 try/except**，非致命性是函数**内部**保证的（`projection.py:596-608`：自己 catch、把坏投影清空、返回 False，等下一次 sandbox acquire 自愈）。对外不炸、对内自清。

### 3.3 第 3 站：Monocle tracing（`app.py:234-242`）

※ 逐行讲解见 `lifespan-deep-dive.md` 麻烦 2。一句话：进程全局 tracer 必须在 startup 装（import 时装会污染所有 importer），且"observability must never break startup"（`app.py:241` 注释）——非致命。

### 3.4 第 4 站：记忆检索索引——为什么要"提前"重建、又为什么不等它（`app.py:244-262`）★

这是你问的"为什么要提前加载记忆"的第一半。先补背景，再回答"为什么提前"和"为什么不等"。

**背景：canonical 和派生。** DeerMem 的**权威数据（canonical）**是磁盘上的记忆文件（markdown/JSON，agent 和用户写进去的事实）。而"检索"（`scoped search`，供每条消息的记忆注入用）走的不是直接读文件，是从 canonical 建出来的**派生索引**。派生的定义就是：可以随时从源头重建、重建期间源头无损、重建错了扔掉重来即可。

**为什么开机要重建（"提前"的第一层）。** 索引是上个进程时代建出来的；上个进程运行期间，agent 一直在写记忆文件（通过 debounce 队列异步落盘）。所以新进程开机时，**索引和 canonical 大概率已经不一致**——开机重建就是一次"派生状态的开机对账"。不重建的话，前几个请求的检索结果是基于过期索引的，注入的记忆可能是漏的。

**为什么放后台 task、不等它（"提前"的第二层）。** 全量重建要扫**所有**用户/agent 的记忆文件、可能还要算 embedding——几秒到几分钟都有可能。如果 `await` 它，`lifespan` 的 `yield` 就迟迟不执行，uvicorn 一直认为应用没就绪，容器的健康检查过不去，编排层可能直接判死重启。所以：

```python
    retrieval_warm_task: asyncio.Task[None] | None = None       # app.py:247
    ...
                retrieval_warm_task = asyncio.create_task(
                    _warm_memory_retrieval(manager),
                    name="memory-retrieval-warm-up",            # app.py:255-258
                )
```

`asyncio.create_task` 把重建扔到事件循环里并发跑，流水线**立刻**继续走第 5、6 站。task 起了名字（`name="memory-retrieval-warm-up"`）——后台任务可诊断，日志/监控里能认出它；这个 task 引用被存下来，关机段（3.12 节）还要用它。

**为什么重建期间的请求仍然正确。** 这是 warm-up 设计的第一原则，值得单独加粗：**预热是优化，正确性从不依赖预热。** DeerMem 在后端里维护 `_retrieval_fully_warmed` 和 `_retrieval_warmed_scopes`（`deer_mem.py:518-539`）：全量预热没完成时，任何请求到达的 scope 都**现场懒重建**，建完打标记。所以 warm-up 失败的唯一后果是"每个 scope 第一次查询慢一点"，功能照常。`app.py:244-246` 的注释就是这个意思："Scoped searches remain correct while this runs because DeerMem lazily rebuilds the requested scope when the full warm-up has not completed yet."

**失败分级**：非致命（`app.py:261-262` 只 warning）。理由 = 派生数据 + 懒兜底，两个条件缺一不可——没有懒兜底的派生数据失败就不能这么轻飘飘。

**为什么两次 `asyncio.to_thread`**（`app.py:252` 取 manager、`app.py:184` 跑 warm_retrieval）：`get_memory_manager` 首次调用会**同步构建**整个 memory 后端（重活，`manager.py:846-861` 双检锁单例），`warm_retrieval` 本身是同步扫描——直接在事件循环里调会卡住所有并发请求，扔工作线程跑。第 5 站再取一次 manager 时拿到的是同一个缓存单例，代价趋近于零。

### 3.5 第 5 站：tiktoken 预热——同样是"提前加载记忆"，为什么这次**同步等**（`app.py:264-290`）★

这是"为什么要提前加载记忆"的第二半，而且和第 4 站构成一组绝佳对照。

**为什么要预热。** tiktoken（OpenAI 的 BPE 分词器）**第一次使用时要下载编码数据，而这个下载会打 OpenAI/Azure 的公网 URL**（issue #3402）。DeerFlow 里每条消息的记忆注入都要做 token 计数（决定注入多少记忆），也就是 tiktoken 几乎每个请求都会用到。如果等第一个请求来了才下载：受限网络里这个下载可能打不通、可能卡很久——**第一个用户的第一个请求，就替全系统付了这笔下载账单**，还可能永远付不完。开机预热把这笔账单挪到"没有任何人在等"的时刻付。

**为什么这次 `await` + 5 秒超时，而第 4 站放后台？** 两个 warm-up，两种调度策略，选择标准值得背下来：

| | 第 4 站：检索索引重建 | 第 5 站：tiktoken 下载 |
|---|---|---|
| 代价 | 大（全量扫描 + embedding），秒~分钟级 | 小（一次 HTTP 下载），秒级 |
| 有没有懒兜底 | **有**（scope 级懒重建，功能不受影响） | **有**（字符计数 fallback，`app.py:286-288`） |
| 懒兜底的体验 | 慢但正确，且可以随时自愈 | token 计数退化为 char 计数，**精度降级**直到 tiktoken 某次懒重试成功（`app.py:286-288` 的 warning 原话："until tiktoken loads successfully"） |
| 结论 | 后台跑，不等 | 同步等，但最多 5 秒 |

tiktoken 是"**小而关键**"：值得开机花最多 5 秒等它；索引重建是"**大而可懒**"：懒兜底代价小，不值得开机等。`asyncio.wait_for(asyncio.to_thread(manager.warm), timeout=5)`（`app.py:277-280`）——工作线程里下载（不卡事件循环）+ 5 秒有界（不拖就绪）。

**#3429 的补丁**：`token_counting == "char"` 的部署**根本不用 tiktoken**——`warm()` 里第一行就 early return（`deer_mem.py:503-516`："token_counting='char'; tiktoken not used, skipping warm-up"）。为什么值得专门修？因为在受限网络里，连"探测一下 tiktoken 要不要下载"这件事本身都可能挂满 5 秒——**对注定用不到的东西，最优成本是零，而不是一次便宜的尝试**。

**三态返回值的日志语义**（`app.py:281-286`）：`warm()` 返回 `None` = 这个后端没东西可预热（基类默认，`manager.py:370-386`）→ 日志说 "skipping"；`True` → "warmed successfully"；`False` → "failed; will use character-based fallback"。注释特别解释了为什么要把三态区分开："a backend with nothing to warm (e.g. noop) returns None from the base default -- log 'skipping' instead of the misleading 'warmed successfully' so the log reflects what actually happened"——**noop 后端打"预热成功"是撒谎**，排障的人会被它带偏。日志必须反映真实发生的事。

### 3.6 第 6 站：上传暂存清理——为什么必须挑"开机这一刻"（`app.py:292-297`）

```python
    try:
        removed_upload_staging_files = await asyncio.to_thread(cleanup_stale_upload_staging_files)
        if removed_upload_staging_files:
            logger.info("Removed %d stale upload staging file(s)", removed_upload_staging_files)
    except Exception:
        logger.warning("Upload staging file cleanup skipped", exc_info=True)
```

**staging 文件是什么**：用户上传大文件时的中间产物（按命名模式识别，`is_upload_staging_file`），传完改名为正式文件。**孤儿从哪来**：上传传到一半，进程崩了/被 kill 了/重启了——staging 文件留在磁盘上，没人删，日积月累占满磁盘。

**为什么清理必须放在开机这一刻，而不能定时清？** 这是本站最精彩的设计判断。清理的任务是"删掉孤儿"，难点是**怎么区分孤儿和正在上传的分片**——运行期你无法区分：看到一个两小时前的 staging 文件，它可能是没人要的垃圾，也可能是一个正在慢慢传的大文件。任何"按年龄判孤儿"的运行期规则都会误删正在进行的上传（竞态）。

而**开机这一刻有个独特性质：上一个进程必然已经死了**。所以磁盘上所有 staging 文件**必然**是孤儿——没有"正在上传"这回事，删除零竞态。`cleanup_stale_upload_staging_files` 的 docstring（`uploads/manager.py:166`）原话就是 "Remove orphaned Gateway upload staging files left by a hard crash"。

这个模式有个可复用的名字：**"开机即独占"**——刚启动的进程，天然拥有对上一个进程遗留状态的独占裁判权，平时做不了的无竞态清扫（清暂存、对账孤儿 run（run-manager 篇麻烦 5 的 lease 恢复）、迁移孤儿 thread（本篇 3.8））都要抓住这个窗口做。

**失败分级**：非致命——删不掉最多留垃圾文件，warning 继续。判断依据又是 0.5 那条线：垃圾文件占磁盘是慢性病，不是急症。

### 3.7 第 7 站：`langgraph_runtime` 四件套（`app.py:300`）

※ 全部细节见 `langgraph-runtime-deep-dive.md`。本节只需要知道它在流水线里的位置逻辑：它是**基础设施层**（数据库引擎、连接池、StreamBridge、RunManager、checkpointer、store），第 8-12 站全都依赖它的产物，所以它必须夹在"轻量预热（2-6，不依赖它）"和"依赖它的服务（8-12）"之间。

### 3.8 第 8 站：admin 引导 + 孤儿 thread 迁移（`app.py:305`）

※ 位置依赖（为什么必须在 store 建好之后）见 `lifespan-deep-dive.md` 麻烦 5；首启不自动建号、迁移的游标分页细节见本篇第 2 步麻烦 6。流水线视角补一句：它是唯一一个"做一次就可能永远不再需要"的站（首次引导 + 一次性迁移），但每次开机都要**检查**——检查便宜（数一下 admin 行数），跳过才危险。

### 3.9 第 9-12 站：四个后台服务（`app.py:308-428`）

四站的共性：都是 `async with langgraph_runtime(...)` **块内**启动的（依赖 7 的产物——stream_bridge、run_manager、各类 repo），都带 `try/except`（大多非致命），都 `await service.start()` 起后台轮询循环。个性在排序和分级上：

**为什么 channel 排第一（`308-326`）？** 它是四者中唯一的**入站流量**入口——飞书/Slack/Telegram/Discord 的用户消息从它进来。停机窗口里积压的 IM 消息，要靠它在开机后尽快恢复接单；scheduler / MCP 任务 / 批量子代理都是**后台批处理**，晚几秒接单无所谓。**入口先于后台**——和超市开门先开大门、再开后台仓库一个道理。这一站还有个值得注意的写法（`app.py:311-322` 的注释，#4121 Slice 2）：`get_stream_bridge` 传的是**闭包** `lambda: getattr(app.state, "stream_bridge", None)` 而不是当场取值——channel 服务的后续 drain 监视器要用它时才取，和所有其它 run 消费者走同一条 `get_stream_bridge` 路径。

**为什么 fatal 分级不一样？** 这是四站里最值得对比的：

- channel / scheduler 起不来 → `logger.exception` 后**继续启动**（`app.py:325-326, 347-348`）。Web UI 是主产品，IM 集成是可选增强——没配 channel 本来就是合法状态。
- MCP 任务服务的 `validate_mcp_task_runtime_configuration`（`app.py:373-377`）抛错 = **致命**。它校验的是**配置自相矛盾**（比如 enabled 但没有任何 task toolset、或 repo 缺失）——不是"没配"，是"配错了"。
- 子代理批量服务（`app.py:416-417`）：`subagent_batches.enabled` 但数据库 backend 是 memory → `RuntimeError` 拒绝启动。

分界线清晰可背：**"没配"是合法状态（非致命，跳过）；"配错了"是事故（致命，拒绝启动）；"显式承诺了持久却给不出持久的前提"是谎言（致命）**。批量服务的核心卖点就是活过 Gateway 重启（`subagent-batch-service-deep-dive.md`），没有数据库就承诺不了——与其起一个注定违约的服务，不如当场拒绝。

四站的接线细节各自有姊妹篇：MCP 任务 → `mcp-task-service-deep-dive.md` §8.1；批量 → `subagent-batch-service-deep-dive.md` §5；scheduler → 根 `AGENTS.md` 的 Scheduled-task note。

### 3.10 服务期（`app.py:430`）

`yield`。流水线交付完毕，uvicorn 开始接流量。前面 1-12 站的所有产物——工具箱里的四件套、五个后台服务的轮询任务、两个后台预热——从这一刻起开始服役。

### 3.11 关机段前半：先停所有"生产者"（`app.py:432-499`）

关机段的总原则一句话：**先停生产者，再 flush 消费者**。

谁是"消费者"？memory 后端的 pending 更新缓冲（debounce 队列，3.12 详讲）。谁是"生产者"？所有会往里塞新东西、或会 launch 新 run 的东西——IM channel（外部消息随时进来）、scheduler（定时器随时到点）、MCP 任务服务（会 launch 通知 run）、批量服务（会执行条目产生记忆更新）、浏览器会话（agent 回调可能触发记忆更新）、以及 `langgraph_runtime` 里还在跑的 in-flight run。

所以关机的前半段（`432-499`）是按这个逻辑逐个关水龙头：

1. **OIDC**（`432-435`）：认证服务先关，不再发新会话。
2. **channel**（`437-451`）：**最大的生产者**——外部 IM 消息不受本进程控制，必须最先停。5 秒有界（`_SHUTDOWN_HOOK_TIMEOUT_SECONDS`），超时放行（lifespan 篇 0.6/麻烦 6 讲过信号风暴的机制）。
3. **scheduler**（`453-457`）：第二个 run 入口。
4. **MCP 任务服务**（`459-471`）：stop 之后把进程级 `set_mcp_task_submitter(None)` 和 `set_mcp_task_config_snapshot(None)` **归零**（`459-471`）。为什么关机还要花两步归零全局单例？因为在 yield 之后的残余窗口里（HTTP 还没完全断、后台协程还没全退），任何代码还可能调 submitter——撞上一个已 stop 的服务不如显式拿到 None 走降级路径；同时把进程态恢复干净（测试里同进程反复建/关 app 不受上次污染）。
5. **批量服务**（`473-482`）：同样归零 submitter。注意它故意**不 finalize 条目**（`subagent-batch-service-deep-dive.md` §7.5）——批次的设计目标就是活过重启，关机不收尾是特性不是缺陷。
6. **浏览器会话**（`484-499`）：5 秒有界关全部会话。它排在 flush 之前，归入"先关生产者"的同一段：browser 自动化的回调链同样会喂养 agent 的观察与记忆更新，先关掉能压缩 flush 窗口内的新增量（代码注释没有明说这一层，这是从 flush 段"无新更新"原则推出的对称解释；代码明说的理由见下一段注释里的"channel stop + browser session close"都在 grace 预算清单里）。

这一段的顺序不是 3.0-3.9 开机顺序的严格镜像（开机是 channel→scheduler→mcp→batch，关机也是这个方向），但**原则是同一个**：按"对外界的暴露面"从大到小关——越接近外部流量的越先关，越接近内部数据的越后关。

### 3.12 关机段后半：冲水（`app.py:501-572`）

生产者全停了，现在处理消费者——memory 的 pending 缓冲。这一段四步，每步都有独立的"为什么"。

**第一步：等 retrieval 重建收尾（`523-535`）。** 第 4 站留下的后台重建任务可能还在跑（开机到现在还没扫完）。马上要 flush + close 后端了，close 会把它正用的连接关掉。所以给它**最多 1 秒**的等待：

```python
        retrieval_warm_finished = True
        if retrieval_warm_task is not None and not retrieval_warm_task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(retrieval_warm_task),
                    timeout=min(_RETRIEVAL_WARM_SHUTDOWN_TIMEOUT_SECONDS, ...),   # 1 秒
                )
            except TimeoutError:
                retrieval_warm_finished = False
                logger.warning("Memory retrieval index rebuild is still running; leaving its connection open during shutdown")
```

两个细节都是故意的：

- **`asyncio.shield`**：`wait_for` 超时的默认行为是 **cancel** 里面的任务——这里如果让它 cancel，重建就死于半路。`shield` 把"不再等待"和"取消它"分开：超时后我们只是不等了，任务本身继续活着，能在剩余的关机时间里跑多远跑多远。**有界放弃 ≠ 中途破坏**。
- **为什么只给 1 秒**（`_RETRIEVAL_WARM_SHUTDOWN_TIMEOUT_SECONDS = 1.0`，`app.py:67-69` 注释）："The retrieval index is **derived** state, so shutdown only waits briefly for its startup rebuild. The canonical memory flush keeps its full configured budget."——派生数据的重建没跑完就让它跑，不占关机预算；canonical 数据的 flush 才配拿完整预算。**关机预算的分配权重 = 数据的不可再生程度**。

**第二步：停扩展的系统观察回调（`537-547`）。** `suspend_extension_system_observations()`（`extensions/notify.py:207-211`）把"fire-and-forget 的系统观察"关掉，但**保留** notify loop 给 awaited 的 task 钩子用。为什么要分两种？flush 马上要在工作线程里跑（下一步），DeerMem 的更新路径会顺带触发扩展的系统模型观察——这些回调是"发了就不管"的，flush 期间还在触发，就是在"边冲水边进水"（又一种生产者）。而 awaited 钩子（`langgraph_runtime` 还在 drain 的 in-flight run 挂着的）还得活到 drain 结束。**只关单向阀，不拆管道**——这是 `app.py:539-542` 注释的原意："Stop accepting those callbacks before flushing, while keeping the registered loop alive for awaited task hooks until langgraph_runtime drains runs and subagents."

**第三步：flush 记忆缓冲（`549-565`）——整段关机的存在理由。**

```python
        try:
            app_cfg = get_app_config()
            if app_cfg.memory.enabled:
                manager = await asyncio.to_thread(get_memory_manager)
                flush_timeout = app_cfg.memory.shutdown_flush_timeout_seconds
                completed = await asyncio.to_thread(manager.shutdown_flush, flush_timeout)
```

**为什么必须 flush**：memory 的更新不是实时写盘的——它们先进**内存里的 debounce 缓冲**，由一个 Timer 线程攒批后异步落盘。`manager.py:338-354` 的 docstring 把后果写透了："any update enqueued since the last timer fire is lost on restart / rolling deploy / SIGTERM, because **the buffer is pure in-memory and the debounce worker is a daemon thread killed on process exit**"——不 flush，从最后一次 timer 到进程死之间的**所有记忆更新全部丢失**，而"记忆"恰恰是用户以为已经存下来的东西。这是 3.11"先停生产者"的终点：水龙头全关了，现在把管道里剩的水冲干净。

**为什么无条件调用**（`app.state` 上不判 `pending_count > 0`）：`app.py:509-514` 的注释给过论证（review #6）——"truly idle 的缓冲短路极快（立即返回 True），所以无条件调用很便宜"；而 host 层如果只判 `pending_count > 0`，就会漏掉"还有 in-flight worker 正在处理缓冲外的事"的情形——**那个状态只有 buffer 所在的 backend 自己知道，host 层判了就会忘**。守门判据放在离真相最近的一层。

**K8s 部署约束**（`app.py:516-522` 注释）：`shutdown_flush_timeout_seconds` 必须装进 pod 的 `terminationGracePeriodSeconds`（channel stop + browser close + retrieval 等待 + flush 之和），否则 K8s 在 flush 半路 `SIGKILL`，flush 修复的丢失问题被静默重新引入——**代码有界不够，编排层的预算也要对齐**，这是"有界关机"作为系统工程的完整含义。

**第四步：条件 close（`566-572`）。**

```python
        finally:
            close = getattr(manager, "close", None)
            if callable(close) and retrieval_warm_finished:
                try:
                    await asyncio.to_thread(close)
```

只有 `retrieval_warm_finished` 为 True 才 close 后端。为什么？如果重建任务还在跑（第一步没等完），close 会抽掉它脚下的连接——索引重建中途死掉。不 close 的代价是：进程退出时这条连接被 OS 硬回收（连接泄漏到进程死为止）——但进程马上就没了，这个"泄漏"有进程生命周期兜底。**宁可带着一条活连接退场，也不把正在重建的派生数据杀在半路**——和第一步的 shield 是同一个思想的收尾：放弃等待，但不搞破坏。

最后 `logger.info("Shutting down API Gateway")`（`app.py:574`）在 `async with langgraph_runtime` **块外**——此刻四件套已被 runtime 的 finally 收完（drain runs → close engine → LIFO），这句日志是整条关机流水线的终点界碑。

### 3.13 序列小结：两条排序原则

把 3.1-3.12 压缩成两条可迁移的原则：

- **开机序列 = 依赖图拓扑序 + 预热预算分配**。输入（config）最先；规矩（容量闸门、日志、能力检查）先于参与者；公共的/常数的开机做，随规模涨的懒修复（skill 投影）；大而可懒的后台跑、小而关键的同步等（两个 memory 预热的对照）；无竞态清扫抓"开机即独占"窗口（上传暂存）；基础设施先于依赖它的服务；入口先于后台（channel 第一个起）。
- **关机序列 = 先停生产者再冲水 + 有界放弃不破坏 + 归零全局**。暴露面大的先关；派生数据少等、canonical 数据多等（预算 = 不可再生程度）；放弃等待用 shield，不搞破坏（条件 close）；服务停了把进程级单例归零。

---

## 第 4 步：把 6 个麻烦的本质串起来

### 一张全景图

从 `uvicorn` 敲下命令到服务可用的完整时间线——前半段（装配）是第 2 步的麻烦 1-6，后半段（开机/关机序列）是第 3 步逐站讲解的内容：

```
 $ uvicorn app.gateway.app:app --host 0.0.0.0 --port 8001      (backend/Makefile:20)
        │
        ▼
 ┌─ import-time ────────────────────────────────────────────────────────┐
 │  import app.gateway.app                                              │
 │      │  模块顶层从上到下执行（0.2 的四块地图）                          │
 │      ├─ logging.basicConfig：给"真空期"日志兜底        app.py:54-58   │
 │      ├─ 定义辅助函数 / lifespan / create_app（只是定义，不执行）        │
 │      └─ app = create_app()                            app.py:910     │
 │              │                                                       │
 │              ├─ FastAPI(lifespan=lifespan, docs=…)     :588-672      │
 │              ├─ add_middleware × 4（书写序 Auth→CSRF→CORS→Trace        │
 │              │      ⇒ 运行序反过来，麻烦 1）            :675,720,731,746│
 │              ├─ 塞扩展鉴权投影函数（麻烦 4）             :686-717      │
 │              ├─ 读 plugins：缺 config.yaml 容忍、坏则炸（麻烦 2）        │
 │              ├─ load_extensions：required 炸 / 普通降级（麻烦 3）        │
 │              ├─ include_router × 25 + /health          :787-886      │
 │              ├─ GitHub webhooks 条件挂载：没密钥就不存在（麻烦 5）        │
 │              └─ include_contributed_routers 最后 + 遮蔽检测（麻烦 5）    │
 └──────────────────────────────────────────────────────────────────────┘
        │  uvicorn 拿到模块变量 app
        ▼
 ┌─ startup-time（lifespan 开机段 → 本篇 3.1-3.9 逐站）─────────────────┐
 │  严格读 startup_config（致命）→ 容量/日志/浏览器（致命）→ 投影/预热/清理  │
 │  （全部非致命）→ async with langgraph_runtime（四件套）                │
 │      → admin 引导 + 孤儿迁移 → channel / scheduler / mcp_tasks /       │
 │         subagent_batches 五个后台服务 → yield（服务期，3.10）           │
 └──────────────────────────────────────────────────────────────────────┘
        ▼
 ┌─ shutdown-time（3.11-3.12：先停生产者→冲水；每步有界）────────────────┐
 │  OIDC → channel → scheduler → mcp → batch → browser 会话              │
 │      → retrieval 等待 → 停扩展观察 → 记忆 flush → 条件 close            │
 │      → 退出 langgraph_runtime：drain run + close engine + LIFO         │
 └──────────────────────────────────────────────────────────────────────┘
```

### 六个麻烦的一条主线

把六个麻烦排成一排，会发现它们都在回答同一个问题——**"装配"这个动作发生在错误的时刻会怎样？**

| 麻烦 | 错误时刻的代价 | 真实代码的对策 |
|---|---|---|
| 1 中间件顺序 | 书写顺序 ≠ 运行顺序，凭直觉写会把 CORS 包进 Auth 里，preflight 全灭 | Starlette 后加在外层；Trace→CORS→CSRF→Auth 每层位置都有安全理由 |
| 2 import-time 读配置 | 测试/新机器 import 即崩；坏配置静默降级则静默改行为 | 只容忍 `FileNotFoundError` 且 lifespan 兜底严查；env 体系才准进 import-time |
| 3 扩展加载 | required 扩展静默丢失；单例在"装配完但没开机"窗口里是空集 | fail-closed（required raise）/ fail-open（其余）分界；`create_app` 里加载一次后冻结 |
| 4 鉴权投影 | 内部 AuthContext 泄给扩展 = pin 死结构 + 泄漏把手；PAT 从扩展路由绕回 admin | 投影而非引用；PAT 双信号压制（is_admin + roles）；保持同步可调 |
| 5 路由顺序 | 扩展路由先挂能整段接管宿主；webhook 无密钥仍挂 = 有 handler 可伪造 | 先宿主后扩展；遮蔽检测 + 原子回滚；无密钥 → 路由不存在（404） |
| 6 `app = create_app()` | import 即装配 → 装配里任何不安全动作都会被"无辜的 import"引爆 | 装配纯函数化、import 永远安全；重活全部推给 lifespan |

一句话收束：**`app.py` 是两种时刻的合订本——import-time 的"装配图"和 startup-time 的"开机程序"。装配图的纪律是"永远可安全执行"（不依赖可选文件、第三方代码挂了不连累宿主、路由和中间件的次序即安全语义）；开机程序的纪律是"致命/非致命分级 + 有界关闭"（那是 `lifespan-deep-dive.md` 的主题）。两套纪律在文件里交错出现，靠"哪个函数在模块顶层被调用"这条线分开：被 `app = create_app()` 调到的，守装配纪律；被 uvicorn 在启动时调到的，守开机纪律。** 下次读这个文件，先问每一行"你在哪个时刻被执行"，答案就都顺了。

### 和姊妹篇的接线

- `lifespan`（`app.py:193-574`）→ **机制**（yield 劈两半、`langgraph_runtime` 嵌套、LIFO、bounded shutdown、#3373）看 `lifespan-deep-dive.md`；**序列**（每站做什么、为什么提前、致命/非致命）看本篇第 3 步
- `startup_config` 快照 vs 请求期热重载 → `lifespan-deep-dive.md` 麻烦 1 + `reload_boundary.py`
- 记忆的两个预热（检索索引后台重建、tiktoken 同步预热）与关机 flush → 本篇 3.4 / 3.5 / 3.12；memory 后端的 tier-3 钩子契约（`warm` / `warm_retrieval` / `shutdown_flush`）见 `deerflow/agents/memory/manager.py`
- channel 服务接线（`app.py:308-326` 的 `get_stream_bridge` 闭包，#4121 Slice 2）→ `stream-bridge-deep-dive.md`
- 定时任务服务 → 根 `AGENTS.md` "Scheduled-task note"；MCP 任务服务 → `mcp-task-service-deep-dive.md` §8.1（其引用的 `app.py:350-408` 即本文 3.9 的同一段）；子代理批量服务 → `subagent-batch-service-deep-dive.md` §5（统一执行容量 #4998 见其 §8 与本篇 3.1）
- runs 路由背后的世界 → `start-run-deep-dive.md` / `run-agent-deep-dive.md` / `run-event-store-deep-dive.md` / `get-checkpointer-deep-dive.md` / `get-feedback-repo-deep-dive.md`
