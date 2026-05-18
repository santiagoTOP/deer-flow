# 分离启动下的流式输出问题总结

## 背景

官方推荐启动方式是通过 nginx 统一入口访问前端和后端。nginx 会把浏览器请求转发到对应服务，浏览器看到的是同一个 origin。

这次没有使用 nginx，而是分别启动：

```text
前端：Next.js
后端：DeerFlow Gateway
```

配置主要来自根目录 `.env` 和 `config.yaml`，前端配置来自 `frontend/.env`。

目标是：不使用 nginx，保持前后端分离启动，同时让前端正常显示 LangGraph 的流式输出。

## 解决了什么

这次主要解决了三个问题：

1. 前端看起来不是流式输出，而是等后端结束后一次性显示。
2. 改成前端直连后端流式接口后，浏览器报 CORS 或 CSRF 问题。
3. 明确当前 DeerFlow 不需要启动 `localhost:2024` 的 `langgraph dev`。

最终方案是：

```text
普通 REST API：继续走 Next.js rewrite
LangGraph 流式 API：浏览器直接访问 DeerFlow Gateway
```

## 原始现象

浏览器里看到输出像是一次性返回，但后端实际已经在发送 SSE 增量事件。

能看到类似事件：

```text
event: messages
data: {"content":"..."}

event: updates
data: {...}
```

这说明后端并不是没有流式能力，问题主要发生在前端请求链路或浏览器接收链路。

## 根因

分离启动时，如果 LangGraph 请求仍然走 Next.js rewrite，链路会变成：

```text
浏览器
  -> Next.js /api/langgraph
  -> Next rewrite
  -> DeerFlow Gateway :8001
  -> SSE 响应
```

SSE 对代理链路比较敏感。中间代理如果没有正确处理流式响应，就可能把后端持续返回的数据缓存起来，最后一次性吐给前端。

所以后端是流式的，但前端 UI 看起来不像流式。

## 为什么不用 localhost:2024

`localhost:2024` 通常是 `langgraph dev` 启动后的 LangGraph Server 地址。

但当前 DeerFlow 后端已经自己运行 agent，不依赖单独的 `langgraph dev` 服务。

实际链路是：

```text
前端 useStream
  -> DeerFlow Gateway :8001/api
  -> Python run_agent(...)
  -> agent.astream(...)
  -> StreamBridge
  -> SSE 事件
```

也就是说，DeerFlow Gateway 自己包装了一套 LangGraph SDK 能识别的 HTTP/SSE 接口。

因此：

```env
NEXT_PUBLIC_LANGGRAPH_BASE_URL="http://localhost:2024"
```

不适合当前启动方式。

应该指向 DeerFlow Gateway：

```env
NEXT_PUBLIC_LANGGRAPH_BASE_URL="http://localhost:8001/api"
```

## 为什么 NEXT_PUBLIC_BACKEND_BASE_URL 不打开

`NEXT_PUBLIC_BACKEND_BASE_URL` 控制普通后端 API 是否从浏览器直连后端。

例如：

```env
# NEXT_PUBLIC_BACKEND_BASE_URL="http://localhost:8001"
```

保持注释时，普通 REST API 继续走 Next.js rewrite：

```text
浏览器 -> Next.js /api/models -> Next rewrite -> DeerFlow Gateway
```

这样普通 API 仍然是同源请求，少处理一部分 CORS 和 cookie 问题。

这次真正需要绕开 rewrite 的只有 LangGraph SSE 流式接口，所以只打开：

```env
NEXT_PUBLIC_LANGGRAPH_BASE_URL="http://localhost:8001/api"
```

## 为什么要配置 CORS

分离启动后，浏览器看到的是两个不同 origin：

```text
前端：http://localhost:3000 或 http://localhost:2026
后端：http://localhost:8001
```

浏览器从前端页面直接请求 `localhost:8001` 时，就是跨域请求。

所以后端必须允许这个前端来源：

```env
GATEWAY_CORS_ORIGINS=http://localhost:3000,http://localhost:2026
```

只需要保留你实际打开的前端地址。例如前端实际是 `http://localhost:2026`，就必须包含它。

如果不配置，浏览器会在请求前或响应阶段拦截跨域访问。

## 为什么 CORS 代码之前没有起作用

后端代码类似：

```py
cors_origins = sorted(get_configured_cors_origins())
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
```

这段代码只有在 `get_configured_cors_origins()` 读到配置时才会启用。

如果 `.env` 里没有配置实际前端 origin，或者配置的是 `localhost:3000`，但浏览器实际打开的是 `localhost:2026`，那么 CORS 仍然不会匹配。

另外，CORS 只负责允许跨域，不负责自动携带 CSRF token。

## CSRF 403 的原因

直连后端后，浏览器报错：

```text
HTTP 403: {"detail":"CSRF token missing. Include X-CSRF-Token header."}
```

原因是后端对状态变更请求有 CSRF 校验，要求同时具备：

```text
Cookie: csrf_token=...
X-CSRF-Token: ...
```

LangGraph SDK 发起请求时，如果没有带 cookie，前端就算想补 header，也拿不到对应 token，后端会拒绝。

所以需要在 SDK 请求里设置：

```ts
credentials: "include"
```

并在状态变更请求里补充：

```text
X-CSRF-Token
```

## 代码修改

修改位置：

```text
frontend/src/core/api/api-client.ts
```

核心作用：

1. LangGraph SDK 请求会携带 cookie。
2. 对 POST 等状态变更请求，从 `csrf_token` cookie 里读取 token。
3. 自动设置 `X-CSRF-Token` header。

关键逻辑：

```ts
const nextInit: RequestInit = { ...init, credentials: "include" };
```

这样直连后端的 LangGraph 请求既能跨域带 cookie，也能通过后端 CSRF 校验。

## 推荐配置

`frontend/.env`:

```env
# 普通 REST API 继续走 Next rewrite。
# NEXT_PUBLIC_BACKEND_BASE_URL="http://localhost:8001"

# LangGraph SSE 流式接口直连后端。
NEXT_PUBLIC_LANGGRAPH_BASE_URL="http://localhost:8001/api"
```

根目录 `.env`:

```env
GATEWAY_CORS_ORIGINS=http://localhost:3000,http://localhost:2026
```

如果只使用一个前端端口，可以只保留一个：

```env
GATEWAY_CORS_ORIGINS=http://localhost:2026
```

## 最终请求链路

普通 REST API：

```text
浏览器
  -> Next.js /api/models
  -> Next rewrite
  -> DeerFlow Gateway :8001
```

LangGraph 流式 API：

```text
浏览器
  -> DeerFlow Gateway :8001/api/threads/.../runs/stream
  -> SSE 增量事件
```

这个方案把容易被代理缓冲的 SSE 请求从 Next rewrite 中拿出来，普通 API 仍然保持原来的访问方式。

## 修改后步骤

1. 确认 `frontend/.env` 中配置了 `NEXT_PUBLIC_LANGGRAPH_BASE_URL="http://localhost:8001/api"`。
2. 确认根目录 `.env` 中 `GATEWAY_CORS_ORIGINS` 包含当前前端地址。
3. 重启后端。
4. 重启前端。
5. 必要时清理浏览器中的 `localhost` cookies。
6. 重新登录或重新执行 setup。
7. 在浏览器 Network 中检查 `/runs/stream`。

验证标准：

```text
/runs/stream 请求保持 pending
Response 中持续出现 event: messages
前端 UI 逐步显示内容
不再出现 CSRF token missing
```
