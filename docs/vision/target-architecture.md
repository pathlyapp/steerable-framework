# 目标架构：Steerable 作为全栈可复用框架

| 字段 | 值 |
|---|---|
| **状态** | Accepted |
| **创建** | 2026-06-21 |
| **作者** | DeepPath / Steerable maintainers |
| **关联** | `spec/runtime/chat-loop.md`（ChatLoop RFC）、`docs/spec/architecture.md`、`docs/migration/deeppath.md` |
| **取代** | `docs/spec/architecture.md` 中"Tier 3 不含 AgentLoop""Tier 2/3 仅 Python、Tier 4 仅 TS"的**范围限定**（见 §9） |

> **TL;DR.** 本文定义 Steerable 的**终态目标**：从今天的"Agent 管道层"（协议 + 纯函数 harness + headless UI + sidecar）扩展为一个**全栈、可复用、可开源的 Agent 产品框架**。但落地必须按"收敛圈层"推进：**先稳定 runtime 核心**（唯一 ChatLoop + ToolRouter + SSE/trace），再验证 **DeepPath 作为后端插件**，最后再抽 **Web/Desktop 外壳**。`deeppath-web / deeppath-api / deeppath-agent / deeppath-desktop` 的终态是薄业务实现层，只承载**收费功能、品牌、垂直业务（如 CIFLog）**。框架内永久禁止出现任何收费 / 品牌 / 垂直代码。

---

## 目录

1. [愿景与设计约束](#1-愿景与设计约束)
2. [当前态 vs 目标态（差距全景）](#2-当前态-vs-目标态差距全景)
3. [分层模型（A/B/C/D 四层）](#3-分层模型abcd-四层)
4. [框架包清单（终态）](#4-框架包清单终态)
5. [业务实现层（deeppath-*）的终态形态](#5-业务实现层deeppath-的终态形态)
6. [扩展点 / 插件 SPI（技术核心）](#6-扩展点--插件-spi技术核心)
7. ["框架 vs 业务"硬边界规则](#7-框架-vs-业务硬边界规则)
8. [端到端数据流（终态）](#8-端到端数据流终态)
9. [对现有架构文档的取代说明](#9-对现有架构文档的取代说明)
10. [开放问题](#10-开放问题)

---

## 1. 愿景与设计约束

### 1.1 一句话愿景

> **Steerable 是"Agent 产品的全栈脚手架"**：你 `pip install` / `pnpm add` 几个 Steerable 包，注入自己的业务插件（工具、技能、模型、页面、收费闸门），就能得到一个完整的 Agent 产品（Web + 后端 + 桌面）。DeepPath 是 Steerable 的第一个、也是最完整的业务实现。

### 1.2 框架要"装进去"的三大主体

| 主体 | 含义 | 今天在哪 | 终态归属 |
|---|---|---|---|
| **Runtime 核心** | ChatLoop、Provider、ToolRouter、Storage、Transport、SSE/trace、harness 集成 | `steerable-agent-runtime` 已有雏形 + `deeppath-api` 生产经验 | **Steerable 第一优先级** |
| **后端产品能力** | FastAPI 应用骨架、MCP 执行引擎、上下文引擎、技能引擎、极小模型基座、认证脚手架 | `deeppath-api` | **Steerable 第二优先级** |
| **前端/客户端外壳** | 工作台外壳、设计系统/主题、运行时抽象、Electron/sidecar 监管、可见终端、本地工具路由 | `deeppath/apps/web`、`deeppath-agent` | **Steerable 第三优先级** |

### 1.2.1 收敛圈层（实施边界）

为了避免"把 DeepPath 整个搬进 Steerable 再加配置项"，框架化按三圈推进：

1. **Ring 1 · Runtime first**：只稳定 Agent 运行时的不可替代核心：`ChatLoop`、hooks、provider/tool/storage/transport adapters、SSE/trace、budget/retry/completion。
2. **Ring 2 · Backend plugin first**：让 `deeppath-api` 作为业务插件接入框架 runtime。第一阶段不急着把产品模型和 handler 全部抽进框架，而是验证 SPI 能否承载真实业务。
3. **Ring 3 · Shell later**：Web/Desktop 先抽低层可复用能力（theme、runtime adapter、panel slots、IPC/sidecar supervisor、PTY/tool-router interfaces），最后才提供完整 `SteerableWebApp` / `createDesktopApp` 级别的高阶装配。

### 1.3 业务层只保留三类东西

1. **收费 / 商业化**：会员、支付、配额、计费闸门（entitlement gate）。
2. **品牌 / 内容**：营销页、Logo、文案、定价页、域名配置。
3. **垂直业务**：CIFLog 测井工具链、行业专家 Agent、特定数据模型扩展。

### 1.4 不可妥协的设计约束

- **C1 — 单一权威实现**：每个能力（尤其 Agent 主循环）在整个生态中**只实现一次**。当前云端 Python / 本地 TS / sidecar 三份 loop 必须收敛为一份（Python 权威，见 ChatLoop RFC）。
- **C2 — Spec 驱动**：跨语言契约以 `spec/*.schema.json` 为唯一真相源，codegen 产出 TS/Py 类型，CI 检测漂移。
- **C3 — 框架零业务**：框架包内**永久禁止** import 任何 `deeppath` / `cflog` / `payment` / `membership` 符号，由 CI 强制（见 §7）。
- **C4 — 插件化扩展**：业务通过稳定 SPI 注入框架，**永远不 fork 框架主体**（不改 loop body、不改 app factory）。
- **C5 — 分层不上爬**：上层可依赖下层，下层禁止依赖上层（A→B→C→D 单向）。
- **C6 — 增量可交付**：每个迁移阶段都能独立上线，不存在"半抽取"中间态长期停留。
- **C7 — Base 从严**：模型、面板、handler 只有在非 DeepPath 参考应用也能自然使用时才进框架；`Task/Goal/Event/Note` 等产品概念默认留业务。

---

## 2. 当前态 vs 目标态（差距全景）

| 能力块 | 目标归属 | 当前位置 | 完成度 |
|---|---|---|---|
| Agent 协议 / 类型 | Steerable (protocol) | ✅ `steerable-agent-protocol` | 100% |
| Harness 纯函数 (policy/budget/retry/completion/tracing/safety) | Steerable (harness) | ✅ `steerable-agent-harness` | 100% |
| **Think-Act-Observe 主循环** | Steerable (runtime `ChatLoop`) | ⚠️ `steerable-agent-runtime/chat_loop.py` 已有雏形；api `loop.py` / agent `router.ts` 仍是生产路径 | 需稳定 + 业务切片接入 |
| **MCP 工具执行引擎 + 通用 handlers** | Steerable (app-kit) + 业务注入具体 handler | ❌ 全在 `deeppath-api/.../mcp/action_runtime/` | 0% |
| **上下文系统 / Providers** | Steerable (app-kit，引擎) + 业务 Provider | ❌ 全在 `deeppath-api/.../context_system/` | 0% |
| **技能引擎 + 通用 SKILL.md** | Steerable (app-kit) + 业务技能包 | ❌ api 34 个 / agent 7 个各自维护 | 0% |
| **FastAPI 应用骨架 / 路由约定 / 认证** | Steerable (`steerable-agent-app`) | ❌ 全在 `deeppath-api` | 未实现；在 runtime 切片稳定后做 |
| **数据模型基座** | Steerable (minimal base) + 业务扩展 | ❌ ~85 模型全在 api，混着 payment/membership | ADR-004 先裁定；base 从严 |
| **Headless 聊天 UI** | Steerable (agent-ui) | ✅ `@steerable/agent-ui` | 100% |
| **工作台外壳 / 设计系统 / 主题** | Steerable (`@steerable/web-kit`) | ❌ 全在 `deeppath/apps/web/goals` | 0% |
| **运行时抽象（远程/本地双模式）** | Steerable (web-kit) | ❌ 在 `deeppath/apps/web/lib/runtime` | 0% |
| **Electron 壳 + 本地后端脚手架** | Steerable (`@steerable/desktop-kit`) | ❌ 全在 `deeppath-agent/src` | 0% |
| **Sidecar（升级为驱动 ChatLoop）** | Steerable (sidecar) | ⚠️ 存在但仅 pass-through | 需升级 |
| 收费功能 (membership/payment) | **业务层** ✅ | ⚠️ 与引擎混在 `deeppath-api` | 需剥离 |
| 垂直业务 (CIFLog) | **业务层** ✅ | ⚠️ 与通用壳混在 `deeppath-agent` | 需剥离 |

**结论**：终态目标完成度仍很低，但 `ChatLoop` 已经不是"从零实现"问题，而是"把 runtime 雏形变成生产权威、再让 DeepPath 以插件方式接入"的问题。后续计划必须先重基线，再做业务切片，最后抽外壳。

---

## 3. 分层模型（A/B/C/D 四层）

在现有"Tier 1–4"基础上，把"Tier 3/4"细化扩张为四个**职责层**（A 最底，D 最顶）：

```
┌──────────────────────────────  Steerable（开源框架）  ──────────────────────────────┐
│                                                                                       │
│  Layer D · Clients (TS)                                                               │
│    @steerable/agent-ui      headless 聊天 hooks/组件        ✅ 已有                    │
│    @steerable/web-kit       主题 / runtime adapter / 面板插槽 / 工作台外壳          ❌ 后置 │
│    @steerable/desktop-kit   sidecar 监管 / IPC / PTY / 本地工具路由 / Electron 壳   ❌ 后置 │
│                                                                                       │
│  Layer C · Application Kits (Python)                                                  │
│    steerable-agent-app      FastAPI 工厂 / /api/v2 路由 / 认证 / 插件注册中心        ❌ 第二圈│
│    steerable-agent-kit      MCP 执行引擎 / 上下文引擎 / 技能引擎 / 极小模型基座       ❌ 第二圈│
│    steerable-sidecar        JSON-RPC server（驱动 ChatLoop）                  ⚠️ 第二圈│
│                                                                                       │
│  Layer B · Core Logic (Python 权威 + TS facade 仅测试)                                │
│    steerable-agent-runtime  ChatLoop / LLMProvider / ToolRouter / Storage / Transport ⚠️ 稳定化│
│    steerable-agent-harness  policy / budget / retry / completion / tracing / safety  ✅ 已有│
│                                                                                       │
│  Layer A · Contracts                                                                  │
│    spec/*.schema.json       跨语言 SSOT                     ✅ 已有                    │
│    steerable-agent-protocol 协议类型 (TS + Py)             ✅ 已有                    │
└───────────────────────────────────────────────────────────────────────────────────┘
            ▲ 业务通过 SPI 注入（工具/技能/模型/路由/页面/主题/收费闸门）
            │ 框架内严禁出现收费 / 品牌 / 垂直代码（CI 强制）
┌──────────────────────────────  deeppath-*（业务实现层，薄）  ─────────────────────────┐
│  deeppath-api     = steerable-agent-app + agent-kit + [membership/payment]            │
│                     + [deeppath 业务 handlers / 技能包 / 模型扩展 / context providers] │
│  deeppath (web)   = @steerable/web-kit + @steerable/agent-ui                          │
│                     + [营销/品牌/定价/会员页/付费墙]                                    │
│  deeppath-agent   = @steerable/desktop-kit + [CIFLog 工具链 / 测井专家 Agent]          │
│  deeppath-desktop = @steerable/desktop-kit（远程执行变体）                              │
└───────────────────────────────────────────────────────────────────────────────────┘
```

### 层职责一句话

- **Layer A 契约**：只有数据类型，无逻辑无 I/O。改动触发全生态 lockstep 版本。
- **Layer B 核心逻辑**：纯函数 harness + 运行时原语 + **唯一的 ChatLoop**。Python 权威，是第一阶段唯一不可绕开的核心。
- **Layer C 应用套件**：把"产品级后端能力"成品化——FastAPI 工厂、MCP 执行、上下文、技能、极小模型基座、sidecar。业务通过插件注入，base 从严。
- **Layer D 客户端**：把"产品级前端/桌面外壳"成品化——工作台 UI、设计系统、Electron 壳。先抽低层能力，最后再做完整高阶 App 装配。

---

## 4. 框架包清单（终态）

| 包 | 注册表 | 层 | 状态 | 职责 | 主要来源（从哪抽） |
|---|---|---|---|---|---|
| `steerable-agent-protocol` | PyPI + npm | A | ✅ | 协议类型 | — |
| `steerable-agent-harness` | PyPI (+TS facade) | B | ✅ | 纯函数原语 | — |
| `steerable-agent-runtime` | PyPI | B | ⚠️ 稳定化 | LLMProvider/ToolRouter/Storage/Transport + **ChatLoop** | 现有 `chat_loop.py` + `deeppath-api/.../harness/loop.py` 的生产经验 |
| `steerable-agent-kit` | PyPI | C | ❌ 新 | MCP 执行引擎、上下文引擎、技能引擎、极小 SQLModel 基座、SPI 注册接口 | `deeppath-api/.../harness/mcp/action_runtime/`、`context_system/`、`skill_loader.py`、`prompt.py`、少量 runtime 必需 models |
| `steerable-agent-app` | PyPI | C | ❌ 新 | FastAPI 工厂、`/api/v2/chats/*` 端点、认证脚手架、插件装配 | `deeppath-api/app/main.py`、`api/v2/fs_router.py`、`api/deps.py`、`entrypoint.py` 的通用部分 |
| `steerable-sidecar` | PyPI (binary) | C | ⚠️ 升 | JSON-RPC server，内部驱动 ChatLoop | 现有 sidecar + runtime ChatLoop 接入 |
| `@steerable/agent-ui` | npm | D | ✅ | headless 聊天 hooks/组件 | — |
| `@steerable/web-kit` | npm | D | ❌ 后置 | theme/provider、runtime adapter、panel slots、action renderer、工作台外壳、paywall/feature-flag SPI | 先抽 `lib/runtime/`、设计 token、panel slot；后抽 `goals/desktop` 外壳 |
| `@steerable/desktop-kit` | npm | D | ❌ 后置 | sidecar 监管、IPC bridge、PTY、本地工具路由接口、Electron 主进程脚手架 | 先抽 `sidecar/`、`terminal-manager.ts`、`tool-router.ts` 框架部分；后抽 `main.ts` 高阶装配 |

> 注：`steerable-agent-kit` 与 `steerable-agent-app` 是否合并为单包，是 §10 的开放问题。建议先分包，保持"无 HTTP 也能用 kit"。

---

## 5. 业务实现层（deeppath-*）的终态形态

### 5.1 `deeppath-api`（后端业务）

终态可以使用 `create_app(...)` 高阶装配，但第一阶段不要求一次性迁移整个 `deeppath-api`。推荐先让一个业务工具切片以插件形式接入 runtime，再扩大到 app/kit：

```python
# deeppath-api/app/main.py（终态，示意）
from steerable_agent_app import create_app, SteerableApp
from steerable_agent_kit import ChatLoopFactory

from app.business import (
    deeppath_tools, deeppath_skill_pack, deeppath_models,
    deeppath_context_providers, deeppath_loop_hooks,
)
from app.membership import entitlement_gate, billing_routes   # ← 收费，业务独有

steer: SteerableApp = create_app(
    title="DeepPath API",
    models=deeppath_models,                 # 扩展模型基座
    tools=deeppath_tools,                    # 注册业务 MCP handlers
    skill_packs=[deeppath_skill_pack],       # 注册业务 SKILL.md
    context_providers=deeppath_context_providers,
    loop_hooks=deeppath_loop_hooks,          # ChatLoop 11 hooks（见 RFC §5）
    entitlement_gate=entitlement_gate,       # 付费闸门
)
steer.include_router(billing_routes)         # 会员/支付路由（业务独有）
app = steer.fastapi
```

剩下的业务代码：`membership/`、`payment/`、`app/business/`（deeppath 专属 handlers/skills/providers/models）。**通用引擎全部来自框架。**

### 5.2 `deeppath`（Web 业务）

`SteerableWebApp` 是最终形态，不是第一阶段目标。Web 侧应先抽 `runtime adapter`、设计 token、action renderer、panel slot system；等这些低层能力在 DeepPath 和一个非 DeepPath 示例中跑通后，再把 Next App Router 下的完整工作台外壳提升为高阶组件。

```tsx
// deeppath/apps/web/src/app/layout.tsx（终态，示意）
import { SteerableWebApp } from "@steerable/web-kit";
import { deeppathTheme, deeppathBranding } from "@/branding";
import { useDeeppathEntitlements } from "@/membership";
import { marketingRoutes, membershipRoutes } from "@/business-routes";

export default function App() {
  return (
    <SteerableWebApp
      theme={deeppathTheme}
      branding={deeppathBranding}
      entitlements={useDeeppathEntitlements}   // ← 付费墙钩子
      extraRoutes={[...marketingRoutes, ...membershipRoutes]}  // 营销/会员页
    />
  );
}
```

剩下的业务代码：营销首页、定价、会员中心、品牌资产、付费墙 UI。**工作台/聊天/设计系统来自框架。**

### 5.3 `deeppath-agent`（桌面垂直业务）

`createDesktopApp(...)` 也是最终形态。桌面侧第一阶段先抽可独立验证的进程能力：`SidecarSupervisor`、IPC bridge contract、可见 PTY、本地工具路由接口、Python runtime packaging helpers；Electron 主进程完整装配最后再收敛。

```ts
// deeppath-agent/src/main.ts（终态，示意）
import { createDesktopApp } from "@steerable/desktop-kit";
import { cflogToolPack } from "./cflog";          // ← 垂直业务
import { ciflogExpertAgent } from "./agents";

createDesktopApp({
  productName: "CIFLog智能助手",
  appId: "cc.deeppath.agent",
  toolPacks: [cflogToolPack],                       // CIFLog socket/卡片工具
  seedAgents: [ciflogExpertAgent],                  // 测井解释专家
  sidecar: { enabled: true },                       // 复用框架 ChatLoop
});
```

剩下的业务代码：`cflog/`、测井专家 Agent、行业 fixtures。**Electron 壳 / local-backend / 终端 / sidecar 监管来自框架。**

### 5.4 `deeppath-desktop`

基于同一 `@steerable/desktop-kit`，配置为"远程执行变体"（连云端 socket 而非本地 LLM）。与 `deeppath-agent` 共享外壳，差异仅在 transport 配置。

---

## 6. 扩展点 / 插件 SPI（技术核心）

整个愿景能否成立，取决于 SPI 是否足以承载所有业务而无需 fork 框架。下面是各层 SPI 草案（接口级，签名以实现 PR 为准）。

### 6.1 后端 SPI（`steerable-agent-app` / `steerable-agent-kit`）

| 扩展点 | 接口 | 承载的业务 |
|---|---|---|
| **工具/Handler** | `register_tool(ToolSpec)` / `@tool` | MCP action handlers（task/goal/event…通用进框架；cflog/dp-action 业务注入） |
| **技能包** | `register_skill_pack(dir)` | SKILL.md（通用技能进框架；deeppath/cflog 技能业务注入） |
| **数据模型** | `register_models(*SQLModel)` | 在 base 模型上扩展业务字段/表（membership/payment/cflog） |
| **路由** | `include_router(APIRouter)` | 业务专属端点（billing、cflog） |
| **ChatLoop hooks** | `register_loop_hooks(Hooks)` | RFC §5 的 11 个 hook（系统提示、实体链接、时区、dp-action 队列…） |
| **上下文 Provider** | `register_context_provider(Provider)` | goals/tasks/graph_rag 等注入；业务可加自定义 Provider |
| **收费闸门** | `register_entitlement_gate(Gate)` | **业务独有**：框架只识别声明式 entitlement key，会员/支付实现留业务 |
| **认证后端** | `register_auth_backend(AuthBackend)` | JWT/OAuth 实现可替换 |

`SteerableApp` 即"装配中心"：框架提供引擎，`create_app(...)` 把业务插件装进去，产出 `FastAPI` 实例。

### 6.2 前端 SPI（`@steerable/web-kit`）

| 扩展点 | 接口 | 承载的业务 |
|---|---|---|
| **主题** | `theme: SteerableTheme` | 配色/圆角/暗色模式 token（对接 deeppath 的 CSS 变量系统） |
| **品牌** | `branding: { logo, name, tagline }` | Logo、产品名、文案 |
| **路由扩展** | `extraRoutes: RouteDef[]` | 营销、定价、会员页 |
| **付费墙** | `entitlements: () => Entitlements` | 使用声明式 entitlement key gate 工作台功能 |
| **运行时** | `runtime: "remote" \| "local" \| RuntimeAdapter` | 复用现有 `lib/runtime` 双模式抽象 |
| **工作台插槽** | `workspacePanels: PanelDef[]` | 在三栏 ContentPanel 注入业务面板 |

### 6.3 桌面 SPI（`@steerable/desktop-kit`）

| 扩展点 | 接口 | 承载的业务 |
|---|---|---|
| **工具包** | `toolPacks: ToolPack[]` | 垂直工具（cflog_*、行业 MCP） |
| **种子 Agent** | `seedAgents: AgentSeed[]` | 行业专家 Agent persona |
| **窗口/品牌** | `productName / appId / icon` | 桌面应用标识 |
| **sidecar** | `sidecar: SidecarConfig` | 复用框架 Python ChatLoop |
| **存储 schema** | `storageExtensions` | SQLite 业务表扩展 |

### 6.4 SPI 设计原则

- **Mutable ctx + 注册表**，而非继承重写（与 ChatLoop RFC §5.1 一致）。
- **加 hook 不改 body**：若某业务行为无法被现有扩展点承载，**新增一个扩展点**，绝不放宽框架主体（RFC §1.3 的硬契约）。
- **稳定版本契约**：SPI 接口纳入 lockstep 版本管理，破坏性变更走 deprecate 流程。

---

## 7. "框架 vs 业务"硬边界规则

### 7.1 框架内永久禁止

- 任何 **收费/计费/会员/配额** 逻辑（`payment`、`membership`、`subscription`、`billing`、`entitlement` 的**具体实现**——只允许出现"闸门接口"）。
- 任何 **品牌/产品** 标识（`deeppath`、`时踪`、域名、Logo、营销文案）。
- 任何 **垂直业务**（`cflog`、测井、行业专有协议）。
- 任何 **deeppath 专属数据表字段**（只允许 base 模型 + 扩展点）。

### 7.2 CI 强制（参照现有 spec drift checker）

```bash
# scripts/check_framework_boundary.py（新增）
# 1. 扫描 packages/**/src，禁止 import: deeppath* / cflog* / app.membership / app.payment
# 2. 关键字黑名单匹配（大小写不敏感）：deeppath / 时踪 / ciflog / wechat_pay / membership_tier
# 3. 命中即 CI 失败，附"该逻辑应放业务层"提示
```

### 7.3 评审授权

任何把上述内容塞进框架的 PR，评审者有权**仅凭本文 §7 直接拒绝**，无需进一步论证。

---

## 8. 端到端数据流（终态）

```
用户输入
  │
  ▼
[Layer D]  @steerable/web-kit  /  @steerable/desktop-kit
  │  useChatStream + 运行时抽象（remote: HTTP+SSE / local: IPC→sidecar）
  ▼
[Layer C]  steerable-agent-app（FastAPI /api/v2/chats/send）
  │         └─ 装配业务插件（hooks/tools/skills/providers/entitlement）
  ▼
[Layer B]  steerable-agent-runtime.ChatLoop（唯一主循环）
  │  ├─ before_send_messages → 业务注入系统提示/技能/上下文（hook）
  │  ├─ provider.stream()    → LLMProvider（OpenAI/Anthropic/Ollama）
  │  ├─ before_tool_call     → 业务实体链接/dp-action 队列/PTY 路由（hook）
  │  ├─ ToolRouter.dispatch  → steerable-agent-kit MCP 执行引擎 + 业务 handlers
  │  ├─ harness: budget/retry/completion/tracing（纯函数）
  │  └─ emit                 → 业务改写/脱敏 SSE（hook）
  ▼
[Layer A]  spec/SSEEvent → 类型化事件流，回传给 Layer D 渲染
```

同一份 ChatLoop 同时服务：云端 FastAPI（deeppath-api）、桌面 sidecar（deeppath-agent）、远程变体（deeppath-desktop）。**三份 loop 收敛为一份。**

---

## 9. 对现有架构文档的取代说明

本文**有意扩张并取代** `docs/spec/architecture.md` 中以下表述（这些表述反映的是"Agent 管道层"阶段，而非"全栈框架"终态）：

| 现有表述 | 本文终态 |
|---|---|
| "Tier 3 deliberately **does not include** an AgentLoop class" | 由 ChatLoop RFC 取代：**唯一 `ChatLoop` 进 `steerable-agent-runtime`**，业务通过 11 hooks 注入 |
| "Tier 2 / 3 Python-only，Tier 4 TS-only" | 仍成立，但**新增 Layer C 应用套件（Py）+ Layer D 的 web-kit/desktop-kit（TS）**；前端不再只有 headless 聊天，而是完整工作台/设计系统 |
| "your production TS code should not import the harness" | 仍成立——TS 端业务逻辑走 sidecar（Python 权威），TS 只做 UI/壳 |
| 框架定位 = "the agent plumbing" | 升级为 = "the full-stack agent product scaffold" |

> 实施时，`docs/spec/architecture.md` 应加一段指针指向本文，并标注其 Tier 模型为"Layer A/B 视角"，Layer C/D 以本文为准。

---

## 10. 开放问题

- **Q1 `steerable-agent-kit` 与 `steerable-agent-app` 是否合并？** 已倾向并建议定案为**先分包**（kit 无 HTTP 依赖，可被 CLI/notebook 复用；app 是 FastAPI 装配）。
- **Q2 数据模型基座的边界？** 需要 ADR-004 逐表裁定。默认策略：base 从严，只放 runtime 必需且跨产品自然成立的模型（如 `AgentSession`、`ChatMessage`、`HarnessTrace`）；`Project/Task/Goal/Event/Note` 默认留业务。
- **Q3 web-kit 的"工作台"有多通用？** 需要 ADR-005 定案。默认策略：先抽主题、runtime adapter、action renderer、panel slot；`Tasks/Notes/ResourceLibrary` 等业务面板留业务注入；完整 `SteerableWebApp` 后置。
- **Q4 开源节奏？** 7→11 个包后 lockstep 成本上升；是否对 Layer C/D 采用独立版本线。
- **Q5 收费闸门接口形态？** 已由 ADR-006 给出默认方向：框架合同采用声明式 entitlement key，业务实现可用谓词式 helper 适配。

---

*本文为终态目标。落地路径见 [refactor-plan.md](./refactor-plan.md)。*
