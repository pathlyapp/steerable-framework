# 前端 / 桌面 SPI 接口草案（`@steerable/web-kit` / `@steerable/desktop-kit`）

| 字段 | 值 |
|---|---|
| **状态** | Draft（接口签名级，实现以 PR 为准） |
| **日期** | 2026-06-21 |
| **关联** | [target-architecture.md](./target-architecture.md) §6.2/§6.3 · [spi-backend.md](./spi-backend.md) · `@steerable/agent-ui` |

> **目的.** 定义 Web 与桌面业务如何把**主题、品牌、页面、付费墙、工具包、垂直 Agent** 注入框架外壳，从而让框架承载工作台/聊天/Electron 壳，业务只留品牌/收费/垂直。

---

## 目录

1. [Web SPI（`@steerable/web-kit`）](#1-web-spisteerableweb-kit)
2. [桌面 SPI（`@steerable/desktop-kit`）](#2-桌面-spisteerabledesktop-kit)
3. [运行时抽象（remote / local）](#3-运行时抽象remote--local)
4. [付费墙 / feature-flag SPI](#4-付费墙--feature-flag-spi)
5. [覆盖性自检：现有前端业务 → SPI](#5-覆盖性自检现有前端业务--spi)

---

## 1. Web SPI（`@steerable/web-kit`）

### 1.1 装配入口

第一阶段的 web-kit 入口不是完整接管 Next 应用，而是提供低层 primitives。`SteerableWebApp` 是终态高阶装配，必须等 theme/runtime/action/panel slots 在示例应用中稳定后再定。

```tsx
// @steerable/web-kit（框架）
export function SteerableRuntimeProvider(props: {
  runtime: RuntimeConfig;
  entitlements?: () => Entitlements;
  children: React.ReactNode;
}): JSX.Element;

export function WorkspaceShell(props: {
  panels: PanelDef[];
  chat: React.ReactNode;
}): JSX.Element;

// 终态高阶装配（后置）
export interface SteerableWebAppProps {
  theme: SteerableTheme;                 // 设计 token / 暗色模式
  branding: Branding;                    // logo / 名称 / 文案
  runtime: RuntimeConfig;                // remote | local（见 §3）
  entitlements?: () => Entitlements;     // 付费墙钩子（见 §4）
  extraRoutes?: RouteDef[];              // 营销/定价/会员等业务页
  workspacePanels?: PanelDef[];          // 工作台 ContentPanel 插槽
  apiBaseUrl: string;
}

export function SteerableWebApp(props: SteerableWebAppProps): JSX.Element;
```

### 1.2 主题与品牌

```ts
export interface SteerableTheme {
  // 对接 deeppath 现有 CSS 变量系统（语义化 token，支持 next-themes）
  colors: { background: string; foreground: string; card: string;
            primary: string; muted: string; accent: string; border: string };
  radius: string;
  // 框架组件全部消费这些 token，业务换皮即换主题
}

export interface Branding {
  productName: string;     // "时踪"
  tagline: string;         // "AI 行动助手"
  logo: ReactNode;
  domain: string;          // "deeppath.cc"
}
```

> 框架组件**严禁硬编码颜色/品牌**（对应后端 ADR-002 的前端版边界）；一律走 `theme` / `branding`。

### 1.3 工作台插槽

```ts
export interface PanelDef {
  id: string;                            // "tasks" | "notes" | "cflog" …
  label: string;
  icon: ReactNode;
  component: React.ComponentType<PanelProps>;
  visible?: (e: Entitlements) => boolean; // 可按会员/feature 显隐
}
```

框架先提供 panel slot system 与基础 `WorkspaceShell`；**业务面板**（Tasks/Notes/ResourceLibrary 具体实现）作为 `workspacePanels` 注入。完整三栏外壳是否成为稳定高阶 API，等 ADR-005 的低层 API 验证后再定。

### 1.4 业务侧用法（deeppath web）

```tsx
// deeppath/apps/web/src/app/layout.tsx（终态）
import { SteerableWebApp } from "@steerable/web-kit";
import { deeppathTheme, deeppathBranding } from "@/branding";
import { useDeeppathEntitlements } from "@/membership";
import { marketingRoutes, membershipRoutes } from "@/routes";
import { tasksPanel, notesPanel, resourcesPanel } from "@/panels";

export default function App() {
  return (
    <SteerableWebApp
      theme={deeppathTheme}
      branding={deeppathBranding}
      runtime={{ mode: "remote", apiBaseUrl: process.env.NEXT_PUBLIC_API_V2_URL! }}
      apiBaseUrl={process.env.NEXT_PUBLIC_API_V2_URL!}
      entitlements={useDeeppathEntitlements}
      extraRoutes={[...marketingRoutes, ...membershipRoutes]}
      workspacePanels={[tasksPanel, notesPanel, resourcesPanel]}
    />
  );
}
```

---

## 2. 桌面 SPI（`@steerable/desktop-kit`）

```ts
// @steerable/desktop-kit（框架）
export interface DesktopAppConfig {
  productName: string;                   // "CIFLog智能助手"
  appId: string;                         // "cc.deeppath.agent"
  icon?: string;
  toolPacks?: ToolPack[];                // 垂直工具（cflog_*）
  seedAgents?: AgentSeed[];              // 行业专家 persona
  sidecar?: SidecarConfig;              // 复用框架 Python ChatLoop
  storageExtensions?: SqliteMigration[]; // SQLite 业务表扩展
  window?: WindowConfig;
}

export function createDesktopApp(config: DesktopAppConfig): void;
```

```ts
export interface ToolPack {
  name: string;                          // "cflog"
  tools: LocalToolSpec[];                // 本地工具（socket/fs/mcp）
}
```

框架提供：Electron 主进程脚手架、preload bridge、local-backend 引擎（`/api/v2/*` 路由约定）、sidecar 监管（`SidecarSupervisor`）、可见 PTY、本地工具路由框架。

### 业务侧用法（deeppath-agent）

```ts
// deeppath-agent/src/main.ts（终态）
import { createDesktopApp } from "@steerable/desktop-kit";
import { cflogToolPack } from "./cflog";
import { ciflogExpertAgent } from "./agents";

createDesktopApp({
  productName: "CIFLog智能助手",
  appId: "cc.deeppath.agent",
  toolPacks: [cflogToolPack],            // ← 垂直业务
  seedAgents: [ciflogExpertAgent],
  sidecar: { enabled: true },            // 走框架 ChatLoop（ADR-001）
});
```

---

## 3. 运行时抽象（remote / local）

复用 deeppath 现有 `lib/runtime` 双模式，提进框架：

```ts
export type RuntimeConfig =
  | { mode: "remote"; apiBaseUrl: string }              // HTTP + SSE（Web）
  | { mode: "local"; bridge: ElectronBridge }           // IPC → sidecar（桌面）
  | { mode: "custom"; adapter: RuntimeAdapter };

export interface RuntimeAdapter {
  sendChat(req: ChatRequest): AsyncIterable<SSEEvent>;   // 统一 SSE 流
  // …list/create 等
}
```

同一套工作台 UI 通过切换 `runtime` 即可服务 Web（远程）与桌面（本地/远程变体），无组件级改动——这是现有 `lib/runtime` 已验证的模式，框架化后 web-kit 与 desktop-kit 共用。

---

## 4. 付费墙 / feature-flag SPI

```ts
export interface Entitlements {
  check(key: string, amount?: number): EntitlementDecision;
  quota(kind: string): { used: number; limit: number };
}

export interface EntitlementDecision {
  allowed: boolean;
  reason?: string;
  upgradeHint?: string;
}

// 框架组件通过 context 消费；业务实现钩子
export function useEntitlements(): Entitlements;        // 框架 re-export
```

```tsx
// 业务实现（deeppath/membership）
export function useDeeppathEntitlements(): Entitlements {
  const { tier } = useUser();
  return {
    check: (key) => ({
      allowed: tier === "pro" || FREE_KEYS.has(key),
      upgradeHint: tier === "free" ? "升级 Pro 解锁" : undefined,
    }),
    quota: (k) => getQuota(tier, k),
  };
}
```

框架在面板显隐、工具入口、模型选择处咨询 `entitlements`；**具体计费/会员逻辑禁止进框架**（ADR-002 前端版）。与后端 SPI-7 `EntitlementGate` 共用 ADR-006 的声明式 key：`feature:<name>`、`tool:<name>`、`model:<id>`、`quota:<kind>`。

---

## 5. 覆盖性自检：现有前端业务 → SPI

| 现有前端业务（deeppath web / agent） | 承载 SPI | 备注 |
|---|---|---|
| `/goals` 三栏工作台外壳 | web-kit 高阶外壳（后置） | 第一阶段只抽 panel slots / shell primitives |
| Tasks/Notes/ResourceLibrary 面板 | web-kit `workspacePanels` 注入 | 业务面板 |
| ChatPanel / useChatStream | `@steerable/agent-ui` | 已有 |
| `lib/runtime` 远程/本地双模式 | web-kit/desktop-kit 运行时抽象 §3 | 提进框架 |
| `lib/agentic/action-system` Action 渲染 | web-kit / agent-ui | 协同 |
| 设计 token / 暗色模式 CSS 变量 | web-kit `theme` | 提进框架 |
| 营销首页 / 定价 / news | 高阶 `extraRoutes`（后置） | **业务皮** |
| 会员中心 / 付费墙 UI | 高阶 `extraRoutes`（后置） + `entitlements` | **收费，业务独有** |
| 品牌（logo/文案/域名） | web-kit `branding` | **业务** |
| Electron 壳 / preload | desktop-kit 脚手架 | 框架 |
| local-backend 引擎 | desktop-kit | 框架 |
| sidecar 监管 | desktop-kit `SidecarSupervisor` | 框架 |
| 可见 PTY / 本地工具路由 | desktop-kit | 框架（cflog 工具留业务） |
| cflog 工具链 / 测井专家 | desktop-kit `toolPacks` / `seedAgents` | **垂直业务** |

**结论草案**：现有前端/桌面业务可由 web-kit + desktop-kit + agent-ui 的 SPI 承载，但抽取顺序必须先低层后高阶。业务仅保留品牌、营销/会员页、付费墙、cflog 垂直。评审重点：ADR-005 的低层 API 是否足够支撑后续高阶外壳，以及 ADR-006 的 entitlement key 是否能前后端一致。
