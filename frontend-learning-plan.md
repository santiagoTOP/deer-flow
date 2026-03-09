# DeerFlow 前端从零学习方案

> **目标读者：** 零前端基础
> **学习载体：** DeerFlow 项目 (`frontend/src/`)
> **学习方法：** 先掌握概念 → 对照项目真实代码 → 理解为什么这样写

---

## 目录

1. [第一阶段：Web 基础概念](#第一阶段web-基础概念)
2. [第二阶段：JavaScript 核心语法](#第二阶段javascript-核心语法)
3. [第三阶段：TypeScript 入门](#第三阶段typescript-入门)
4. [第四阶段：React 基础](#第四阶段react-基础)
5. [第五阶段：Next.js App Router](#第五阶段nextjs-app-router)
6. [第六阶段：Tailwind CSS](#第六阶段tailwind-css)
7. [第七阶段：项目架构深入](#第七阶段项目架构深入)
8. [第八阶段：高级主题](#第八阶段高级主题)

---

## 第一阶段：Web 基础概念

### 1.1 浏览器如何工作？

你打开 `http://localhost:3000`，浏览器做了这些事：

1. 向服务器请求 HTML 文件
2. 解析 HTML，构建 **DOM 树**（一棵代表页面结构的树）
3. 加载 CSS，计算每个元素的样式
4. 执行 JavaScript，让页面"活起来"
5. 把所有内容画在屏幕上

### 1.2 HTML — 页面的骨架

HTML 是一种标记语言，用**标签**描述内容的结构：

```html
<!-- 这是一个段落 -->
<p>这是一段文字</p>

<!-- 这是一个按钮 -->
<button>点击我</button>

<!-- 这是一个链接，href 是"属性" -->
<a href="https://github.com">GitHub</a>

<!-- div 是一个没有语义的容器，用来分组元素 -->
<div>
  <h1>标题</h1>
  <p>段落</p>
</div>
```

**在项目中的体现** — 打开 `frontend/src/app/layout.tsx` 第 20-31 行：

```tsx
// 这就是一个完整 HTML 页面的骨架
<html lang={locale}>    // <html> 是根标签
  <body>                // <body> 包含所有可见内容
    <ThemeProvider>
      <I18nProvider>
        {children}      // children 是当前页面的内容
      </I18nProvider>
    </ThemeProvider>
  </body>
</html>
```

### 1.3 CSS — 页面的样式

CSS 负责控制颜色、大小、位置等视觉效果：

```css
/* 选中 <p> 标签，把字体颜色改成红色 */
p {
  color: red;
  font-size: 16px;
}

/* 选中 class="container" 的元素 */
.container {
  width: 100%;
  max-width: 1200px;
  margin: 0 auto;  /* 水平居中 */
}
```

**在项目中的体现** — 打开 `frontend/src/styles/globals.css`：

```css
/* DeerFlow 用 CSS 变量定义颜色主题 */
:root {
  --background: oklch(1 0 0);       /* 背景色 */
  --foreground: oklch(0.145 0 0);   /* 前景色（文字） */
  --primary: oklch(0.205 0 0);      /* 主色调 */
}
```

这些变量在整个项目中被 Tailwind 引用，改变 `--primary` 就能改变整个网站的主色。

### 1.4 JavaScript — 页面的行为

HTML/CSS 是静态的，JS 让页面响应用户操作：

```javascript
// 点击按钮时，把段落文字改掉
const button = document.querySelector('button')
const paragraph = document.querySelector('p')

button.addEventListener('click', function() {
  paragraph.textContent = '按钮被点了！'
})
```

**重要认知：** 现代前端（React/Next.js）不直接操作 DOM，而是通过**声明式**方式描述 UI 应该长什么样，框架自动处理 DOM 更新。

---

## 第二阶段：JavaScript 核心语法

### 2.1 变量声明

```javascript
// const：声明之后不能重新赋值（推荐默认用这个）
const name = "DeerFlow"
const count = 42
const isReady = true

// let：可以重新赋值
let score = 0
score = 10  // OK

// var：老语法，有坑，不要用
```

**在项目中** — 几乎所有变量都用 `const`，只有需要改变的才用 `let`。
例如 `frontend/src/core/settings/hooks.ts` 第 18-19 行：

```typescript
const [mounted, setMounted] = useState(false)  // useState 返回的是 const
const [state, setState] = useState<LocalSettings>(DEFAULT_LOCAL_SETTINGS)
```

### 2.2 函数

```javascript
// 普通函数声明
function add(a, b) {
  return a + b
}

// 箭头函数（现代 JS 更常用，更简洁）
const add = (a, b) => {
  return a + b
}

// 箭头函数：如果只有一行 return，可以省略花括号和 return
const add = (a, b) => a + b

// 箭头函数：如果只有一个参数，可以省略括号
const double = x => x * 2
```

**在项目中** — 打开 `frontend/src/lib/utils.ts`：

```typescript
// 这是一个普通函数导出
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
```

打开 `frontend/src/core/settings/hooks.ts` 第 26-44 行：

```typescript
// 这是箭头函数赋值给变量，传给 useCallback
const setter = useCallback(
  (key, value) => {                    // 箭头函数作为参数传入
    setState((prev) => {               // 另一个箭头函数作为参数
      const newState = { ...prev, [key]: { ...prev[key], ...value } }
      saveLocalSettings(newState)
      return newState
    })
  },
  [],
)
```

### 2.3 数组方法

```javascript
const fruits = ['苹果', '香蕉', '橙子']

// map：把每个元素转换成新值，返回新数组
const upper = fruits.map(fruit => fruit.toUpperCase())
// ['苹果', '香蕉', '橙子'] → 处理后返回新数组

// filter：筛选满足条件的元素
const numbers = [1, 2, 3, 4, 5]
const evens = numbers.filter(n => n % 2 === 0)
// [2, 4]

// find：找到第一个满足条件的元素
const first = numbers.find(n => n > 3)
// 4

// forEach：遍历每个元素（没有返回值）
fruits.forEach(fruit => console.log(fruit))
```

**在项目中** — `frontend/src/app/workspace/chats/[thread_id]/page.tsx` 第 105-115 行：

```typescript
// 用 state.messages 数组，找最后一条消息
const lastMessage = state.messages[state.messages.length - 1]
if (lastMessage) {
  const textContent = textOfMessage(lastMessage)
  // 如果文本超过 200 字符，截断并加省略号
  if (textContent.length > 200) {
    body = textContent.substring(0, 200) + "..."
  }
}
```

### 2.4 对象

```javascript
// 对象：键值对的集合
const user = {
  name: "张三",
  age: 25,
  isAdmin: false,
}

// 访问属性
console.log(user.name)   // "张三"
console.log(user["age"]) // 25（动态 key 时用这种写法）

// 解构：把对象的属性"拆"出来成变量
const { name, age } = user
console.log(name)  // "张三"

// 展开运算符：复制对象并修改部分属性
const updatedUser = { ...user, age: 26 }
// { name: "张三", age: 26, isAdmin: false }
```

**在项目中** — `frontend/src/core/settings/local.ts` 第 41-55 行：

```typescript
// 用展开运算符合并默认设置和已保存的设置
const mergedSettings = {
  ...DEFAULT_LOCAL_SETTINGS,          // 先铺开默认值
  context: {
    ...DEFAULT_LOCAL_SETTINGS.context, // 再铺开默认 context
    ...settings.context,               // 用已保存的 context 覆盖
  },
}
```

### 2.5 模块系统（import / export）

现代 JS 把代码拆分成多个文件，通过 `import`/`export` 相互引用：

```javascript
// utils.js — 导出一个函数
export function formatDate(date) {
  return date.toLocaleDateString()
}

// 也可以默认导出（一个文件只能有一个 default export）
export default function MyComponent() {
  return <div>Hello</div>
}
```

```javascript
// app.js — 导入使用
import { formatDate } from './utils'     // 命名导入（花括号）
import MyComponent from './MyComponent'  // 默认导入（无花括号）

// 导入时重命名
import { formatDate as fd } from './utils'

// 导入类型（TypeScript 专用）
import type { User } from './types'
```

**在项目中** — `frontend/src/app/page.tsx`：

```tsx
// 导入 6 个命名导出的组件
import { Footer } from "@/components/landing/footer"
import { Header } from "@/components/landing/header"
import { Hero } from "@/components/landing/hero"
// ...

// 默认导出这个页面组件
export default function LandingPage() {
  // ...
}
```

注意 `@/` 是路径别名，等价于 `src/`。所以 `@/components/landing/footer` 实际是 `src/components/landing/footer.tsx`。

### 2.6 异步编程（async / await）

JavaScript 是**单线程**的，处理网络请求时不能"卡住等待"，需要异步：

```javascript
// 不好的写法（回调地狱）
fetch('/api/user', function(response) {
  response.json(function(data) {
    console.log(data)
  })
})

// 好的写法：async/await
async function fetchUser() {
  const response = await fetch('/api/user')  // 等待请求完成
  const data = await response.json()          // 等待解析 JSON
  console.log(data)
  return data
}

// 错误处理
async function fetchUser() {
  try {
    const response = await fetch('/api/user')
    const data = await response.json()
    return data
  } catch (error) {
    console.error('请求失败:', error)
  }
}
```

**在项目中** — `frontend/src/components/landing/header.tsx` 第 42-67 行：

```typescript
// 这是一个异步的 React 组件（Server Component 特有）
async function StarCounter() {
  let stars = 10000

  try {
    // await 等待 GitHub API 返回数据
    const response = await fetch(
      "https://api.github.com/repos/bytedance/deer-flow",
      { next: { revalidate: 3600 } },  // Next.js 缓存控制
    )

    if (response.ok) {
      const data = await response.json()
      stars = data.stargazers_count ?? stars  // ?? 是空值合并运算符
    }
  } catch (error) {
    console.error("Error fetching GitHub stars:", error)
  }

  return <NumberTicker value={stars} />
}
```

### 2.7 可选链（`?.`）和空值合并（`??`）

```javascript
// 假设 user 可能是 null 或 undefined
const user = null

// 不安全：会报错 "Cannot read property 'name' of null"
console.log(user.name)

// 安全：可选链，如果 user 是 null/undefined，返回 undefined
console.log(user?.name)  // undefined，不报错

// 可选链链式
console.log(user?.address?.city)

// 空值合并：左边是 null 或 undefined 时，用右边的值
const name = user?.name ?? "匿名用户"
console.log(name)  // "匿名用户"

// 注意区分 ?? 和 ||
// || 对 false、0、"" 也会取右边的值
// ?? 只对 null 和 undefined 取右边的值
const count = 0 || 100   // 100（因为 0 是 falsy）
const count2 = 0 ?? 100  // 0（因为 0 不是 null/undefined）
```

**在项目中** — `frontend/src/app/workspace/chats/[thread_id]/page.tsx` 第 139-141 行：

```typescript
// 三层可选链：thread.values 可能没有 title 属性
thread.values?.title && thread.values.title !== "Untitled"
  ? thread.values.title
  : t.pages.untitled

// 第 162 行：数组可能不存在，用 ?? 提供默认空数组
thread?.values?.artifacts?.length > 0
```

---

## 第三阶段：TypeScript 入门

### 3.1 为什么需要 TypeScript？

JavaScript 是**动态类型**语言，变量可以存任何类型的值：

```javascript
// JavaScript：这完全合法，但运行时可能出错
let user = "张三"
user = 42          // 突然变成数字
user.toUpperCase() // 运行时报错！数字没有 toUpperCase 方法
```

TypeScript 在 JS 基础上加了**类型系统**，在编写代码时就发现错误：

```typescript
// TypeScript：编译器立即报错
let user: string = "张三"
user = 42  // ❌ 错误：不能把 number 赋给 string 类型
```

### 3.2 基本类型

```typescript
// 原始类型
const name: string = "DeerFlow"
const version: number = 2.0
const isReady: boolean = true
const nothing: null = null
const notDefined: undefined = undefined

// 数组类型
const names: string[] = ["Alice", "Bob"]
const scores: number[] = [90, 85, 92]
// 或者用泛型写法：
const names2: Array<string> = ["Alice", "Bob"]

// 联合类型：可以是多种类型之一
let id: string | number = "abc123"
id = 42  // OK，因为 number 也在联合类型里

// 字面量类型：只能是特定的值
type Mode = "flash" | "thinking" | "pro" | "ultra"
let mode: Mode = "flash"
mode = "fast"  // ❌ 错误："fast" 不在 Mode 类型中
```

**在项目中** — `frontend/src/core/settings/local.ts` 第 26-27 行：

```typescript
// 字面量联合类型，限定 mode 只能是这四个值
mode: "flash" | "thinking" | "pro" | "ultra" | undefined;
```

打开 `frontend/src/components/workspace/input-box.tsx` 第 72 行：

```typescript
// 这个 type 别名定义了 InputMode，和 local.ts 中的 mode 类型对应
type InputMode = "flash" | "thinking" | "pro" | "ultra"
```

### 3.3 接口（interface）和类型别名（type）

```typescript
// interface：描述对象的形状
interface User {
  id: number
  name: string
  email: string
  age?: number  // ? 表示可选属性
}

// 使用接口
const user: User = {
  id: 1,
  name: "张三",
  email: "zhang@example.com",
  // age 是可选的，可以不写
}

// type：更灵活，除了对象形状，还能描述联合类型等
type ID = string | number
type Mode = "dark" | "light"
```

**在项目中** — `frontend/src/core/settings/local.ts`：

```typescript
// 这个 interface 描述了"本地设置"的数据形状
export interface LocalSettings {
  notification: {
    enabled: boolean;       // 必填，布尔值
  };
  context: Omit<          // Omit 是工具类型，排除某些字段
    AgentThreadContext,
    "thread_id" | "is_plan_mode" | "thinking_enabled" | "subagent_enabled"
  > & {                   // & 是交叉类型，合并两个类型
    mode: "flash" | "thinking" | "pro" | "ultra" | undefined;
  };
  layout: {
    sidebar_collapsed: boolean;
  };
}
```

`frontend/src/core/i18n/context.tsx` 第 7-10 行：

```typescript
// 描述 Context 值的形状
export interface I18nContextType {
  locale: Locale;                    // 当前语言
  setLocale: (locale: Locale) => void; // 切换语言的函数
}
```

### 3.4 泛型

泛型是"类型参数"，让代码可以处理多种类型：

```typescript
// 不用泛型：要写两个函数
function getFirstString(arr: string[]): string {
  return arr[0]
}
function getFirstNumber(arr: number[]): number {
  return arr[0]
}

// 用泛型：一个函数搞定
function getFirst<T>(arr: T[]): T {
  return arr[0]
}

getFirst<string>(["a", "b"])  // 返回 string
getFirst<number>([1, 2, 3])   // 返回 number
// TypeScript 通常能自动推断，不需要手写 <string>
getFirst([1, 2, 3])  // 自动推断 T 是 number
```

**在项目中** — `frontend/src/core/i18n/context.tsx` 第 12 行：

```typescript
// createContext 是泛型函数
// <I18nContextType | null> 指定 Context 里存的数据类型
export const I18nContext = createContext<I18nContextType | null>(null)
```

`frontend/src/app/workspace/chats/[thread_id]/page.tsx` 第 87 行：

```typescript
// useState 是泛型 Hook
// <string | null> 说明这个 state 可以是 string 或 null
const [threadId, setThreadId] = useState<string | null>(null)
```

### 3.5 类型导入

TypeScript 允许只导入类型（不导入运行时代码），这对性能有好处：

```typescript
// 导入类型
import type { User } from './types'
import type { ReactNode } from 'react'

// 在同一个 import 里混合导入值和类型
import { useState, type ComponentProps } from 'react'
```

**在项目中** — `frontend/src/app/layout.tsx` 第 4 行：

```typescript
// 只导入 Metadata 类型，不导入任何运行时代码
import { type Metadata } from "next"
```

---

## 第四阶段：React 基础

### 4.1 什么是 React？

React 是一个 UI 库，核心思想是：**UI = f(state)**。

界面是状态的函数。给定相同的状态，总是渲染出相同的界面。当状态变化时，React 自动更新界面。

你不需要手动操作 DOM（`document.querySelector(...).style.color = 'red'`），只需要描述"当状态是 X 时，界面应该长什么样"。

### 4.2 JSX — 在 JavaScript 里写 HTML

JSX 是 React 的语法糖，让你在 JS/TS 代码里写类似 HTML 的东西：

```tsx
// 普通 HTML
<h1 class="title">Hello</h1>

// JSX（注意：class 变成了 className，因为 class 是 JS 保留关键字）
<h1 className="title">Hello</h1>

// JSX 里用 {} 插入 JavaScript 表达式
const name = "DeerFlow"
<h1>Hello, {name}!</h1>

// 条件渲染
const isLoggedIn = true
<div>
  {isLoggedIn ? <p>已登录</p> : <p>未登录</p>}
  {isLoggedIn && <button>退出</button>}  {/* && 短路：true 时才渲染 */}
</div>

// 列表渲染：用数组的 .map() 方法
const fruits = ['苹果', '香蕉', '橙子']
<ul>
  {fruits.map((fruit, index) => (
    <li key={index}>{fruit}</li>  // key 是必须的，帮助 React 识别元素
  ))}
</ul>
```

**在项目中** — `frontend/src/components/landing/hero.tsx` 第 38-79 行：

```tsx
<div className="container-md relative z-10 ...">
  {/* h1 标签里嵌入了 JSX 组件 */}
  <h1 className="flex items-center gap-2 text-4xl font-bold md:text-6xl">
    <WordRotate
      words={[
        "Deep Research",  // 数组 prop，用 {} 传递
        "Collect Data",
        // ...
      ]}
    />
    {" "}  {/* 空格字符 */}
    <div>with DeerFlow</div>
  </h1>

  {/* Link 组件：Next.js 的客户端导航链接 */}
  <Link href="/workspace">
    <Button size="lg">
      <span>Get Started with 2.0</span>
      <ChevronRightIcon className="size-4" />
    </Button>
  </Link>
</div>
```

### 4.3 组件（Component）

React 应用由**组件**构成。每个组件是一个函数，接收 Props（属性），返回 JSX（界面）。

```tsx
// 最简单的组件
function Greeting() {
  return <h1>Hello, World!</h1>
}

// 接收 props 的组件
function Greeting({ name }: { name: string }) {
  return <h1>Hello, {name}!</h1>
}

// 使用组件
function App() {
  return (
    <div>
      <Greeting name="Alice" />
      <Greeting name="Bob" />
    </div>
  )
}
```

**组件就是乐高积木**。小组件组装成大组件，大组件组装成页面。

**在项目中** — `frontend/src/app/page.tsx`：

```tsx
// LandingPage 组件由 7 个子组件拼装而成
export default function LandingPage() {
  return (
    <div className="min-h-screen w-full bg-[#0a0a0a]">
      <Header />           {/* 顶部导航栏 */}
      <main>
        <Hero />           {/* 英雄区（大标题区域） */}
        <CaseStudySection />
        <SkillsSection />
        <SandboxSection />
        <WhatsNewSection />
        <CommunitySection />
      </main>
      <Footer />           {/* 底部 */}
    </div>
  )
}
```

**在项目中** — `frontend/src/components/landing/hero.tsx`：

```tsx
// Hero 接收一个可选的 className prop
export function Hero({ className }: { className?: string }) {
  return (
    <div className={cn("flex size-full flex-col items-center", className)}>
      {/* ... */}
    </div>
  )
}
```

`className?:` 中的 `?` 表示这个 prop 是可选的，不传也没关系。

### 4.4 Props（属性）— 父传子的数据通道

Props 是父组件传给子组件的数据，**单向流动，不可修改**：

```tsx
// 父组件传数据
function Parent() {
  return (
    <Child
      title="标题"
      count={42}
      isActive={true}
      onClick={() => console.log('点击了')}
      items={['a', 'b', 'c']}
    />
  )
}

// 子组件接收数据
function Child({
  title,
  count,
  isActive,
  onClick,
  items,
}: {
  title: string
  count: number
  isActive: boolean
  onClick: () => void
  items: string[]
}) {
  return (
    <div>
      <h2>{title} ({count})</h2>
      {isActive && <button onClick={onClick}>点击</button>}
    </div>
  )
}
```

**特殊 Prop：`children`**

```tsx
// 可以在组件标签之间放内容，子组件用 children 接收
function Card({ children }: { children: React.ReactNode }) {
  return (
    <div className="card">
      {children}
    </div>
  )
}

// 使用
<Card>
  <h2>标题</h2>
  <p>内容</p>
</Card>
```

**在项目中** — `frontend/src/app/layout.tsx` 第 15-32 行：

```tsx
// children 是 Next.js 自动传入的，代表子页面的内容
export default async function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html>
      <body>
        <ThemeProvider>
          {/* children 被 ThemeProvider 和 I18nProvider 包裹 */}
          <I18nProvider initialLocale={locale}>{children}</I18nProvider>
        </ThemeProvider>
      </body>
    </html>
  )
}
```

### 4.5 useState — 组件的记忆

`useState` 让组件可以记住和改变数据。每次状态改变，组件**自动重新渲染**：

```tsx
import { useState } from 'react'

function Counter() {
  // useState(0)：初始值是 0
  // 返回 [当前值, 修改函数] 这个数组
  const [count, setCount] = useState(0)

  return (
    <div>
      <p>当前计数：{count}</p>
      <button onClick={() => setCount(count + 1)}>+1</button>
      <button onClick={() => setCount(0)}>重置</button>
    </div>
  )
}
```

**重要：** 永远用 `setCount(...)` 修改状态，不要直接 `count = count + 1`，直接修改不会触发重新渲染。

**在项目中** — `frontend/src/app/workspace/chats/[thread_id]/page.tsx`：

```typescript
// 记录对话线程的 ID
const [threadId, setThreadId] = useState<string | null>(null)

// 记录最终的对话状态
const [finalState, setFinalState] = useState<AgentThreadState | null>(null)

// 记录 Artifacts 面板是否折叠
const [todoListCollapsed, setTodoListCollapsed] = useState(true)

// 记录是否自动选择第一个文件
const [autoSelectFirstArtifact, setAutoSelectFirstArtifact] = useState(true)
```

这个页面有 4 个 `useState`，分别管理 4 个不同的状态。

### 4.6 useEffect — 副作用

`useEffect` 处理**副作用**：不属于渲染逻辑的操作，比如：
- 发送网络请求
- 设置定时器
- 订阅事件
- 直接操作 DOM

```tsx
import { useState, useEffect } from 'react'

function UserProfile({ userId }) {
  const [user, setUser] = useState(null)

  // 语法：useEffect(副作用函数, 依赖数组)
  useEffect(() => {
    // 这里的代码在渲染完成后执行
    fetch(`/api/user/${userId}`)
      .then(res => res.json())
      .then(data => setUser(data))
  }, [userId])  // 依赖数组：userId 变化时重新执行

  if (!user) return <p>加载中...</p>
  return <p>{user.name}</p>
}
```

**依赖数组的三种情况：**

```tsx
useEffect(() => {
  // 每次渲染后都执行（很少这样用）
})

useEffect(() => {
  // 只在组件挂载时执行一次
}, [])

useEffect(() => {
  // userId 变化时执行
}, [userId])
```

**在项目中** — `frontend/src/app/workspace/chats/[thread_id]/page.tsx` 第 88-94 行：

```typescript
// 当 URL 参数 threadIdFromPath 变化时，更新 threadId 状态
useEffect(() => {
  if (threadIdFromPath !== "new") {
    setThreadId(threadIdFromPath)     // 用 URL 里的 ID
  } else {
    setThreadId(uuid())              // 生成新的 UUID
  }
}, [threadIdFromPath])  // 依赖：threadIdFromPath 变化时执行
```

第 136-154 行：

```typescript
// 当标题或加载状态变化时，更新浏览器标签页标题
useEffect(() => {
  if (thread.isThreadLoading) {
    document.title = `Loading... - ${t.pages.appName}`
  } else {
    document.title = `${pageTitle} - ${t.pages.appName}`
  }
}, [
  isNewThread,
  t.pages.newChat,
  t.pages.untitled,
  t.pages.appName,
  thread.values.title,
  thread.isThreadLoading,
])
```

### 4.7 useCallback 和 useMemo — 性能优化

```tsx
import { useState, useCallback, useMemo } from 'react'

function SearchPage() {
  const [query, setQuery] = useState('')
  const [items] = useState(['苹果', '香蕉', '苹果手机', '橙子'])

  // useMemo：缓存计算结果
  // 只有 query 或 items 变化时，才重新计算
  // 避免每次渲染都重新过滤
  const filteredItems = useMemo(() => {
    return items.filter(item => item.includes(query))
  }, [query, items])

  // useCallback：缓存函数引用
  // 只有 query 变化时，才创建新函数
  // 防止子组件因为父组件重渲染而不必要地重渲染
  const handleSearch = useCallback((e) => {
    setQuery(e.target.value)
  }, [])  // 空依赖：函数永远不变

  return (
    <div>
      <input onChange={handleSearch} />
      <ul>
        {filteredItems.map(item => <li key={item}>{item}</li>)}
      </ul>
    </div>
  )
}
```

**在项目中** — `frontend/src/core/settings/hooks.ts` 第 26-44 行：

```typescript
// useCallback 缓存 setter 函数
// [] 空依赖：函数只创建一次，不会随组件重渲染而改变
const setter = useCallback(
  (key, value) => {
    setState(prev => {
      // ...
    })
  },
  [],  // 空依赖
)
```

`frontend/src/app/workspace/chats/[thread_id]/page.tsx` 第 83-85 行：

```typescript
// useMemo 计算 isNewThread（URL 参数是否是 "new"）
const isNewThread = useMemo(
  () => threadIdFromPath === "new",
  [threadIdFromPath],  // 只有 threadIdFromPath 变化时重新计算
)
```

### 4.8 自定义 Hook — 复用逻辑

**Hook** 是以 `use` 开头的函数，可以调用其他 Hook。自定义 Hook 让你把组件逻辑抽离成可复用的函数：

```tsx
// 自定义 Hook：封装本地存储逻辑
function useLocalStorage<T>(key: string, initialValue: T) {
  const [storedValue, setStoredValue] = useState<T>(() => {
    try {
      const item = window.localStorage.getItem(key)
      return item ? JSON.parse(item) : initialValue
    } catch {
      return initialValue
    }
  })

  const setValue = useCallback((value: T) => {
    setStoredValue(value)
    localStorage.setItem(key, JSON.stringify(value))
  }, [key])

  return [storedValue, setValue] as const
}

// 在任何组件里复用
function Settings() {
  const [theme, setTheme] = useLocalStorage('theme', 'dark')
  // ...
}
```

**在项目中** — `frontend/src/core/settings/hooks.ts` 完整文件就是一个自定义 Hook：

```typescript
// useLocalSettings：把本地设置的读取/保存逻辑封装起来
export function useLocalSettings(): [LocalSettings, SetterFunction] {
  const [mounted, setMounted] = useState(false)
  const [state, setState] = useState<LocalSettings>(DEFAULT_LOCAL_SETTINGS)

  // 组件挂载时，从 localStorage 读取设置
  useEffect(() => {
    if (!mounted) {
      setState(getLocalSettings())
    }
    setMounted(true)
  }, [mounted])

  // 修改设置时，同时更新 state 和 localStorage
  const setter = useCallback((key, value) => {
    setState(prev => {
      const newState = { ...prev, [key]: { ...prev[key], ...value } }
      saveLocalSettings(newState)  // 写入 localStorage
      return newState
    })
  }, [])

  return [state, setter]  // 返回 [当前设置, 修改函数]
}
```

在 ChatPage 里用起来非常简洁：

```typescript
// 一行代码搞定复杂的本地存储逻辑
const [settings, setSettings] = useLocalSettings()
```

### 4.9 Context — 跨层级传数据

当数据需要从祖先组件传到很深的后代组件时，一层层传 Props 太麻烦，用 Context：

```tsx
import { createContext, useContext, useState } from 'react'

// 第一步：创建 Context
const ThemeContext = createContext<'dark' | 'light'>('dark')

// 第二步：用 Provider 包裹，提供数据
function App() {
  const [theme, setTheme] = useState<'dark' | 'light'>('dark')

  return (
    <ThemeContext.Provider value={theme}>
      <Header />       {/* 这里的所有后代都能读取 theme */}
      <Main />
      <Footer />
    </ThemeContext.Provider>
  )
}

// 第三步：在任意后代组件中读取
function Button() {
  const theme = useContext(ThemeContext)  // 直接读，不需要逐层传 props
  return <button className={theme === 'dark' ? 'dark-btn' : 'light-btn'}>按钮</button>
}
```

**在项目中** — `frontend/src/core/i18n/context.tsx`：

```typescript
// 创建 Context
export const I18nContext = createContext<I18nContextType | null>(null)

// Provider 组件：包裹子树，提供 locale 状态
export function I18nProvider({ children, initialLocale }) {
  const [locale, setLocale] = useState<Locale>(initialLocale)

  const handleSetLocale = (newLocale: Locale) => {
    setLocale(newLocale)
    document.cookie = `locale=${newLocale}; ...`  // 同时保存 cookie
  }

  return (
    <I18nContext.Provider value={{ locale, setLocale: handleSetLocale }}>
      {children}
    </I18nContext.Provider>
  )
}

// 自定义 Hook：封装 useContext，更友好的 API
export function useI18nContext() {
  const context = useContext(I18nContext)
  if (!context) {
    throw new Error("useI18n must be used within I18nProvider")
  }
  return context
}
```

在 `frontend/src/app/layout.tsx` 中把整个应用包起来：

```tsx
<I18nProvider initialLocale={locale}>
  {children}   {/* 所有子页面都能访问语言设置 */}
</I18nProvider>
```

**在项目中** — `frontend/src/components/workspace/messages/context.ts`：

```typescript
// ThreadContext：让 MessageList 等深层组件能访问当前对话信息
export const ThreadContext = createContext<ThreadContextType | undefined>(undefined)

// 自定义 Hook：带错误提示
export function useThread() {
  const context = useContext(ThreadContext)
  if (context === undefined) {
    throw new Error("useThread must be used within a ThreadContext")
  }
  return context
}
```

---

## 第五阶段：Next.js App Router

### 5.1 什么是 Next.js？

Next.js 是基于 React 的**框架**，它在 React 之上提供了：
- **文件系统路由**：文件路径就是 URL 路径
- **服务端渲染**（SSR）：页面在服务器生成，更快更利于 SEO
- **服务端组件**：部分组件在服务器运行，不下载到浏览器
- **图片优化、字体优化**等内置功能

### 5.2 文件即路由

在 `app/` 目录下，文件路径直接对应 URL：

```
app/
├── page.tsx                      → /
├── layout.tsx                    → 所有页面的根布局
├── workspace/
│   ├── page.tsx                  → /workspace
│   ├── layout.tsx                → /workspace 及其子路由的布局
│   └── chats/
│       ├── page.tsx              → /workspace/chats
│       └── [thread_id]/          → 动态路由（方括号）
│           ├── page.tsx          → /workspace/chats/任意ID
│           └── layout.tsx        → 这个路由的布局
```

**在项目中** — 对照 `frontend/src/app/` 目录结构：

```
src/app/
├── layout.tsx       → 根布局（包含 ThemeProvider、I18nProvider）
├── page.tsx         → 首页（/）
└── workspace/
    ├── layout.tsx   → workspace 布局（包含侧边栏）
    ├── page.tsx     → /workspace（重定向到 chats）
    └── chats/
        ├── page.tsx → /workspace/chats（聊天列表）
        └── [thread_id]/
            ├── layout.tsx → 对话布局
            └── page.tsx   → /workspace/chats/abc123 （具体对话页）
```

### 5.3 Layout（布局）— 持久化界面

`layout.tsx` 包裹子页面，它**不会在路由切换时重新渲染**，适合放导航栏、侧边栏等：

```tsx
// app/layout.tsx — 根布局
export default function RootLayout({ children }) {
  return (
    <html>
      <body>
        <Navbar />      {/* 每个页面都有导航栏 */}
        {children}      {/* 当前页面的内容 */}
        <Footer />      {/* 每个页面都有页脚 */}
      </body>
    </html>
  )
}
```

**在项目中** — `frontend/src/app/layout.tsx`：

```tsx
// 根布局：为所有页面提供主题和国际化支持
export default async function RootLayout({ children }) {
  const locale = await detectLocaleServer()  // 服务端检测用户语言
  return (
    <html lang={locale} suppressHydrationWarning>
      <body>
        <ThemeProvider attribute="class" enableSystem>   {/* 主题（亮/暗模式） */}
          <I18nProvider initialLocale={locale}>           {/* 多语言 */}
            {children}                                    {/* 当前页面 */}
          </I18nProvider>
        </ThemeProvider>
      </body>
    </html>
  )
}
```

### 5.4 动态路由

文件夹名用 `[参数名]` 表示动态路由，匹配 URL 中的任意值：

```
[thread_id]/page.tsx
```

会匹配：
- `/workspace/chats/abc123`（thread_id = "abc123"）
- `/workspace/chats/new`（thread_id = "new"）
- `/workspace/chats/任意字符串`

**在项目中** — `frontend/src/app/workspace/chats/[thread_id]/page.tsx` 第 57 行：

```typescript
// useParams 读取 URL 中的动态参数
const { thread_id: threadIdFromPath } = useParams<{ thread_id: string }>()

// threadIdFromPath 可能是 "new"（新建对话）或具体的 ID（如 "abc123"）
const isNewThread = useMemo(
  () => threadIdFromPath === "new",
  [threadIdFromPath],
)
```

### 5.5 Server Component vs Client Component

这是 Next.js App Router 最重要的概念：

**Server Component（默认）：**
- 在服务器运行，不下载到浏览器
- 可以直接 `await` 异步操作（访问数据库、调用 API）
- **不能**用 `useState`、`useEffect`、事件处理器
- 更快（减少 JS 下载量）

```tsx
// Server Component（没有 "use client"，就是 Server Component）
async function UserList() {
  // 直接在服务端查询数据
  const users = await db.query('SELECT * FROM users')

  return (
    <ul>
      {users.map(user => <li key={user.id}>{user.name}</li>)}
    </ul>
  )
}
```

**Client Component（需要 `"use client"`）：**
- 在浏览器运行
- 可以用所有 React Hooks
- 可以处理用户交互

```tsx
"use client"  // 文件顶部加这一行

import { useState } from 'react'

// 现在可以用 useState 了
function LikeButton() {
  const [liked, setLiked] = useState(false)
  return (
    <button onClick={() => setLiked(!liked)}>
      {liked ? '❤️' : '🤍'}
    </button>
  )
}
```

**在项目中的区分：**

```tsx
// frontend/src/app/page.tsx — Server Component（无 "use client"）
// 可以直接组合其他服务端/客户端组件
export default function LandingPage() {
  return (
    <div>
      <Header />  {/* Server Component */}
      <Hero />    {/* Client Component（因为有动画、交互） */}
    </div>
  )
}
```

```tsx
// frontend/src/components/landing/hero.tsx
"use client"  // 有动画，需要在客户端运行

export function Hero({ className }) {
  return (
    <div>
      <Galaxy mouseRepulsion={false} ... />  {/* 粒子动画 */}
      <FlickeringGrid flickerChance={0.25} ... />  {/* 闪烁网格动画 */}
    </div>
  )
}
```

```tsx
// frontend/src/components/landing/header.tsx — Server Component
// 没有 "use client"，可以在服务端 fetch GitHub stars
export function Header() {
  return (
    <header>
      <h1>DeerFlow</h1>
      {/* StarCounter 是异步 Server Component，直接 await fetch */}
      {env.GITHUB_OAUTH_TOKEN && <StarCounter />}
    </header>
  )
}

async function StarCounter() {
  const response = await fetch('https://api.github.com/...')  // 服务端执行
  const data = await response.json()
  return <NumberTicker value={data.stargazers_count} />
}
```

### 5.6 Link 组件 — 客户端导航

Next.js 的 `<Link>` 替代 `<a>`，实现**无刷新页面跳转**：

```tsx
import Link from 'next/link'

// 普通 <a> 标签：会刷新整个页面
<a href="/workspace">进入工作区</a>

// Next.js Link：客户端路由，无刷新，快很多
<Link href="/workspace">进入工作区</Link>
```

**在项目中** — `frontend/src/components/landing/hero.tsx` 第 71-76 行：

```tsx
<Link href="/workspace">
  <Button size="lg">
    <span>Get Started with 2.0</span>
    <ChevronRightIcon className="size-4" />
  </Button>
</Link>
```

---

## 第六阶段：Tailwind CSS

### 6.1 什么是 Tailwind？

传统 CSS 的痛点：
- 需要给每个元素想命名（`.card-container`、`.user-profile-header`...）
- CSS 文件越来越大
- 样式冲突难以排查

Tailwind 的方案：**工具类（Utility Classes）**，每个 class 只做一件事，直接在 HTML 里写：

```html
<!-- 传统 CSS -->
<div class="user-card">张三</div>

<style>
.user-card {
  background: white;
  padding: 16px;
  border-radius: 8px;
  font-size: 14px;
  display: flex;
  align-items: center;
}
</style>

<!-- Tailwind：直接写 class -->
<div class="bg-white p-4 rounded-lg text-sm flex items-center">张三</div>
```

### 6.2 常用工具类

**间距：**

```html
p-4      → padding: 16px（4 * 4px）
p-2      → padding: 8px
px-4     → padding-left: 16px; padding-right: 16px
py-2     → padding-top: 8px; padding-bottom: 8px
m-4      → margin: 16px
mt-8     → margin-top: 32px
gap-2    → gap: 8px（用于 flex/grid 间距）
```

**尺寸：**

```html
w-full   → width: 100%
h-screen → height: 100vh
size-4   → width: 16px; height: 16px（正方形）
min-h-0  → min-height: 0
max-w-lg → max-width: 32rem
```

**布局：**

```html
flex               → display: flex
flex-col           → flex-direction: column（垂直排列）
items-center       → align-items: center（交叉轴居中）
justify-center     → justify-content: center（主轴居中）
justify-between    → justify-content: space-between
grid               → display: grid
grid-cols-3        → grid-template-columns: repeat(3, 1fr)
```

**文字：**

```html
text-sm    → font-size: 14px
text-lg    → font-size: 18px
text-4xl   → font-size: 36px
text-6xl   → font-size: 60px
font-bold  → font-weight: 700
font-medium → font-weight: 500
text-center → text-align: center
```

**颜色：**

```html
bg-black         → background-color: black
bg-white         → background-color: white
text-white       → color: white
bg-blue-500      → 蓝色背景
text-gray-400    → 灰色文字
border-gray-200  → 灰色边框
```

**定位：**

```html
relative    → position: relative
absolute    → position: absolute
fixed       → position: fixed
z-10        → z-index: 10
z-30        → z-index: 30
top-0       → top: 0
right-0     → right: 0
left-0      → left: 0
inset-0     → top: 0; right: 0; bottom: 0; left: 0（四个方向都是0）
```

### 6.3 响应式前缀

```html
<!-- 手机：text-xl，大屏（md 以上）：text-4xl -->
<h1 class="text-xl md:text-4xl">标题</h1>

<!-- 断点前缀：sm(640px) md(768px) lg(1024px) xl(1280px) -->
```

**在项目中** — `frontend/src/components/landing/hero.tsx` 第 39 行：

```tsx
<h1 className="flex items-center gap-2 text-4xl font-bold md:text-6xl">
{/* 手机上是 text-4xl（36px），md 屏幕以上是 text-6xl（60px） */}
```

### 6.4 状态修饰符

```html
hover:bg-blue-600    → 鼠标悬停时背景变深蓝
focus:ring-2         → 获得焦点时显示环形轮廓
disabled:opacity-50  → 禁用时半透明
group-hover:text-yellow-500  → 父元素悬停时改变子元素颜色
```

**在项目中** — `frontend/src/lib/utils.ts` 第 9-10 行：

```typescript
// externalLinkClass：
// 正常状态：有下划线（underline）
// 悬停时：无下划线（hover:no-underline）
export const externalLinkClass =
  "text-primary underline underline-offset-2 hover:no-underline"
```

`frontend/src/components/landing/header.tsx` 第 70 行：

```tsx
<StarFilledIcon
  // 正常：默认颜色
  // group-hover：父 <a> 悬停时变黄色
  className="size-4 transition-colors duration-300 group-hover:text-yellow-500"
/>
```

### 6.5 `cn()` 函数 — 条件拼接类名

当类名需要根据条件动态变化时，用 `cn()`：

```typescript
import { cn } from "@/lib/utils"

// cn 接收任意多个参数，合并成一个类名字符串
// 可以传字符串、对象（key 是类名，value 是 boolean）
cn("flex items-center", isActive && "bg-blue-500", "p-4")
// isActive=true  → "flex items-center bg-blue-500 p-4"
// isActive=false → "flex items-center p-4"
```

**在项目中** — `frontend/src/app/workspace/chats/[thread_id]/page.tsx` 第 217-222 行：

```typescript
<header
  className={cn(
    "absolute top-0 right-0 left-0 z-30 flex h-12 shrink-0 items-center px-4",
    // 新对话：透明背景，无模糊
    isNewThread
      ? "bg-background/0 backdrop-blur-none"
      // 已有对话：半透明背景，有模糊效果
      : "bg-background/80 shadow-xs backdrop-blur",
  )}
>
```

---

## 第七阶段：项目架构深入

### 7.1 项目目录结构全览

```
frontend/src/
├── app/                    # Next.js 路由（每个文件夹 = URL 段）
│   ├── layout.tsx          # 根布局（主题 + 国际化）
│   ├── page.tsx            # 首页（/）
│   └── workspace/
│       ├── layout.tsx      # 工作区布局（侧边栏）
│       └── chats/
│           └── [thread_id]/
│               └── page.tsx # 聊天页（核心页面）
│
├── components/             # 所有 UI 组件
│   ├── ui/                 # Shadcn UI 基础组件（自动生成，别手动改）
│   │   ├── button.tsx
│   │   ├── dialog.tsx
│   │   └── ...
│   ├── ai-elements/        # AI 专用组件（prompt-input, message 等）
│   ├── landing/            # 首页专用组件
│   └── workspace/          # 工作区专用组件
│       ├── messages/       # 消息列表组件
│       ├── artifacts.tsx   # 文件面板组件
│       ├── input-box.tsx   # 输入框组件
│       └── ...
│
├── core/                   # 业务逻辑（不含 UI）
│   ├── api/                # LangGraph 客户端
│   ├── threads/            # 对话线程（hooks + types）
│   ├── settings/           # 用户设置（localStorage）
│   ├── i18n/               # 国际化（中英文翻译）
│   ├── models/             # TypeScript 类型定义
│   └── ...
│
├── hooks/                  # 全局共享 Hooks
├── lib/                    # 工具函数（cn 等）
└── styles/                 # 全局样式
```

### 7.2 数据流：一条消息是如何发出的？

```
用户输入消息，点击发送
        ↓
InputBox 组件（components/workspace/input-box.tsx）
  调用 onSubmit(message)
        ↓
ChatPage 组件（app/workspace/chats/[thread_id]/page.tsx）
  调用 handleSubmit
        ↓
useSubmitThread hook（core/threads/hooks.ts）
  通过 LangGraph SDK 发送请求
        ↓
useThreadStream hook（core/threads/hooks.ts）
  接收流式响应，实时更新 thread.values
        ↓
ThreadContext.Provider（core/messages/context.ts）
  把 thread 数据传给所有子组件
        ↓
MessageList 组件（components/workspace/messages/）
  订阅 thread 数据，实时渲染消息
```

### 7.3 组件分层原则

```
app/         → 页面级别，组装所有子组件，管理页面状态
components/workspace/ → 业务组件，负责具体功能区域
components/ui/        → 基础 UI 组件（按钮、输入框等无业务逻辑）
core/        → 纯业务逻辑（不含任何 JSX），可独立测试
```

**好处：** 业务逻辑（`core/`）和 UI（`components/`）分离，改 UI 不影响逻辑，改逻辑不影响 UI。

### 7.4 TanStack Query — 服务器状态管理

React 的 `useState` 管理本地状态（本地数据）。服务器状态（需要 fetch 的数据）用 TanStack Query 更高效：

```typescript
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'

// useQuery：自动 fetch 数据，处理 loading/error 状态
function ThreadList() {
  const { data, isLoading, error } = useQuery({
    queryKey: ['threads'],        // 缓存 key
    queryFn: () => api.getThreads(), // 获取数据的函数
    staleTime: 1000 * 60,        // 1分钟内不重新 fetch
  })

  if (isLoading) return <p>加载中...</p>
  if (error) return <p>出错了</p>
  return <ul>{data.map(t => <li key={t.id}>{t.title}</li>)}</ul>
}

// useMutation：执行修改操作（POST/PUT/DELETE）
function DeleteButton({ threadId }) {
  const queryClient = useQueryClient()
  const { mutate } = useMutation({
    mutationFn: () => api.deleteThread(threadId),
    onSuccess: () => {
      // 删除成功后，让缓存的 threads 列表失效，触发重新 fetch
      queryClient.invalidateQueries({ queryKey: ['threads'] })
    }
  })

  return <button onClick={() => mutate()}>删除</button>
}
```

**在项目中** — `frontend/src/core/threads/hooks.ts` 第 5 行导入：

```typescript
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
```

### 7.5 理解 `useThreadStream`

这是项目中最核心的 Hook，它连接前端和 AI 后端：

```typescript
// core/threads/hooks.ts
export function useThreadStream({ threadId, isNewThread, onFinish }) {
  const queryClient = useQueryClient()

  // useStream 是 LangGraph SDK 提供的 Hook
  // 它建立一个流式连接，实时接收 AI 的输出
  const thread = useStream<AgentThreadState>({
    client: getAPIClient(),        // LangGraph 客户端实例
    assistantId: "lead_agent",     // 后端的 AI agent 名称
    threadId: isNewThread ? undefined : threadId,
    reconnectOnMount: true,        // 重新连接时恢复之前的对话

    // 每当 AI 发出自定义事件时调用
    onCustomEvent(event) {
      if (event.type === "task_running") {
        updateSubtask({ id: event.task_id, latestMessage: event.message })
      }
    },

    // AI 完成后调用
    onFinish(state) {
      onFinish?.(state.values)
      // 更新对话列表缓存
      queryClient.setQueriesData(
        { queryKey: ["threads", "search"] },
        // ...更新逻辑
      )
    },
  })

  return thread
  // thread.values  → 当前对话的所有状态（消息、artifacts、todos 等）
  // thread.isLoading → AI 是否还在生成
  // thread.stop()   → 停止生成
}
```

在 `ChatPage` 中使用：

```typescript
const thread = useThreadStream({
  isNewThread,
  threadId,
  onFinish: (state) => {
    setFinalState(state)
    // 浏览器在后台时，显示桌面通知
    if (document.hidden || !document.hasFocus()) {
      showNotification(state.title, { body: "对话完成" })
    }
  },
})

// 现在可以用 thread.values.messages 渲染消息
// thread.values.artifacts 渲染文件列表
// thread.values.todos 渲染待办列表
```

---

## 第八阶段：高级主题

### 8.1 国际化（i18n）

DeerFlow 支持中英文切换。实现路径：

```
core/i18n/
├── index.ts      → 导出类型和 locale 列表
├── context.tsx   → I18nContext + I18nProvider（上一节讲过）
├── hooks.ts      → useI18n() Hook（返回当前语言的翻译文本）
├── server.ts     → 服务端检测用户语言（读 cookie）
└── locales/
    ├── en-US.ts  → 英文翻译
    └── zh-CN.ts  → 中文翻译
```

使用方式：

```typescript
import { useI18n } from "@/core/i18n/hooks"

function MyComponent() {
  const { t } = useI18n()  // t 是翻译对象

  return (
    <div>
      <p>{t.common.artifacts}</p>      // "Artifacts" 或 "文件"
      <p>{t.inputBox.placeholder}</p>  // 输入框占位符
    </div>
  )
}
```

### 8.2 主题切换（亮色/暗色模式）

DeerFlow 用 `next-themes` 库实现主题切换：

```tsx
// layout.tsx 中包裹 ThemeProvider
<ThemeProvider attribute="class" enableSystem disableTransitionOnChange>
  {children}
</ThemeProvider>
```

这会在 `<html>` 标签上添加 `class="dark"` 或 `class="light"`，CSS 变量根据这个 class 自动切换颜色。

在 CSS 中：

```css
:root {
  --background: white;
  --foreground: black;
}

.dark {
  --background: #0a0a0a;
  --foreground: white;
}
```

### 8.3 useRef — 不触发渲染的引用

`useRef` 存储不需要触发渲染的值，或者引用 DOM 元素：

```typescript
import { useRef } from 'react'

// 引用 DOM 元素
function TextInput() {
  const inputRef = useRef<HTMLInputElement>(null)

  const focusInput = () => {
    inputRef.current?.focus()  // 直接操作 DOM
  }

  return (
    <>
      <input ref={inputRef} />
      <button onClick={focusInput}>聚焦</button>
    </>
  )
}

// 存储不影响渲染的数据（改变 ref.current 不会重渲染）
function Timer() {
  const countRef = useRef(0)

  const increment = () => {
    countRef.current++  // 不触发渲染
    console.log(countRef.current)
  }
}
```

**在项目中** — `frontend/src/app/workspace/chats/[thread_id]/page.tsx` 第 66-68 行：

```typescript
// 存储上一次的 initialValue，用于对比是否需要更新输入框
const lastInitialValueRef = useRef<string | undefined>(undefined)

// 存储 setInput 函数的最新引用（避免 useEffect 的闭包问题）
const setInputRef = useRef(promptInputController.textInput.setInput)
setInputRef.current = promptInputController.textInput.setInput
```

---

## 学习建议与实践任务

### 如何使用本学习方案

1. **顺序学习**：每个阶段都是下一阶段的基础，不要跳跃
2. **多问问题**：遇到不懂的代码直接问，我会结合项目文件解释
3. **动手实践**：每个阶段结束后做对应的小任务

### 实践任务列表

**第一阶段结束：**
- [ ] 打开浏览器开发者工具（F12），在 Elements 面板里找到 DeerFlow 的 `<html>` 结构

**第二阶段结束：**
- [ ] 阅读 `frontend/src/lib/utils.ts` 全文，理解每行代码的含义

**第三阶段结束：**
- [ ] 阅读 `frontend/src/core/settings/local.ts`，找出所有 TypeScript 类型注解

**第四阶段结束：**
- [ ] 阅读 `frontend/src/components/landing/hero.tsx`，标出所有 Props、JSX、组件调用
- [ ] 阅读 `frontend/src/core/i18n/context.tsx`，理解 Context 的完整使用流程

**第五阶段结束：**
- [ ] 在浏览器里访问 `/workspace/chats/new`，理解为什么 `[thread_id]` 匹配了 `new`

**第六阶段结束：**
- [ ] 在 `frontend/src/components/landing/hero.tsx` 中找 10 个 Tailwind 类，查文档确认含义

**第七阶段结束：**
- [ ] 通读 `frontend/src/app/workspace/chats/[thread_id]/page.tsx`，追踪一个消息从发送到显示的完整流程

---

## 快速参考

### React Hooks 速查

| Hook | 用途 | 触发重渲染 |
|------|------|-----------|
| `useState` | 存储需要触发渲染的状态 | ✅ |
| `useEffect` | 处理副作用（fetch、订阅） | ❌（但内部可以 setState） |
| `useCallback` | 缓存函数引用 | ❌ |
| `useMemo` | 缓存计算结果 | ❌ |
| `useRef` | 存储不触发渲染的值/DOM引用 | ❌ |
| `useContext` | 读取 Context 值 | ✅（Context 变化时） |

### Next.js App Router 速查

| 文件名 | 作用 |
|--------|------|
| `layout.tsx` | 持久布局，子路由切换时不重新渲染 |
| `page.tsx` | 路由的主内容页 |
| `loading.tsx` | 页面加载时的骨架屏 |
| `error.tsx` | 错误边界 |
| `[param]/` | 动态路由，`param` 匹配任意值 |

### Tailwind 最常用类速查

| 功能 | 类名 |
|------|------|
| Flex 横向排列 | `flex items-center gap-4` |
| Flex 垂直排列 | `flex flex-col gap-4` |
| 全宽高 | `size-full` 或 `w-full h-full` |
| 绝对定位覆盖 | `absolute inset-0` |
| 固定定位顶部 | `fixed top-0 left-0 right-0` |
| 响应式隐藏 | `hidden md:block`（手机隐藏，大屏显示） |
| 半透明背景 | `bg-background/80` |
| 条件类名 | 用 `cn()` 函数 |
