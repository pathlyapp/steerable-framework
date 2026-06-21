# 架构决策记录（ADR）

本文记录"全栈框架"演进中**必须先拍板**的关键决策。每条决策一旦 Accepted，后续 PR 评审可直接援引。

| 关联 | [target-architecture.md](./target-architecture.md) · [refactor-plan.md](./refactor-plan.md) · `spec/runtime/chat-loop.md` |
|---|---|

---

## ADR-001 · 后端权威语言 = Python

| 字段 | 值 |
|---|---|
| **状态** | Accepted |
| **日期** | 2026-06-21 |
| **关联** | ChatLoop RFC、`docs/migration/deeppath.md` §架构 pivot |

### 背景

当前 Think-Act-Observe 主循环存在三份实现：
- `deeppath-api/.../harness/loop.py`（Python，5004 行，最完整）
- `deeppath-agent/src/local-backend/router.ts`（TS，1763 行，精简）
- `steerable-sidecar`（Python，~50 行 pass-through）

三份独立维护，行为持续漂移。

### 决策

**后端业务逻辑（Tier 2/3，含 ChatLoop、MCP 执行、上下文、技能）以 Python 为唯一权威实现。** 本地客户端不再维护 TS loop，统一通过 `steerable-sidecar` 调用 Python ChatLoop。TS 仅保留 UI / 桌面壳，以及 harness 的 parity-test facade（不进生产路径）。

### 理由

1. deeppath-api 的 Python loop 是 5 年积累最完整的实现，迁移成本最低。
2. sidecar 已是 Python，桌面端嵌入便携 CPython 的能力已就绪（`resources/python-runtime`，体积 CI <300MB）。
3. 维护 TS+Py 双实现必然漂移（迁移指南已论证）。
4. 一份引擎同时服务云端 FastAPI、桌面 sidecar、远程变体。

### 影响

- ✅ 三份 loop 收敛为一份（改造计划 P2 里程碑 M3）。
- ⚠️ 桌面端启动依赖 sidecar 进程（需处理启动时序，见迁移指南 gotchas）。
- ⚠️ deeppath-agent 需删除 `src/harness/*.ts`、`local-backend/router.ts`（保留 fallback 一个 release）。

### 备选（已否决）

- **TS 权威**：否决——业务最全的实现在 Python，且服务端必须 Python。
- **双权威 + 一致性测试**：否决——漂移成本过高，违反"单一实现"约束 C1。

---

## ADR-002 · 框架零业务边界（CI 强制）

| 字段 | 值 |
|---|---|
| **状态** | Accepted |
| **日期** | 2026-06-21 |
| **关联** | target-architecture §7、`scripts/check_framework_boundary.py` |

### 背景

愿景要求"收费/品牌/垂直业务留业务层，其余进框架"。若无强制约束，重构过程中业务代码会不断渗漏回框架（改造计划风险 R2）。

### 决策

**Steerable 框架包内永久禁止出现任何收费、品牌、垂直业务代码**，由 CI（`check_framework_boundary.py`）强制：

禁止项（在 `packages/**` 内）：
- import：`deeppath*`、`cflog*`、`app.membership`、`app.payment` 等
- 关键词（大小写不敏感）：`deeppath`、`时踪`、`ciflog`、`cflog`、`membership`、`subscription`、`wechat_pay`、`alipay` 等
- 收费/计费的**具体实现**（只允许"闸门接口"`EntitlementGate`，不允许其实现）

命中即 CI 失败。评审者有权仅凭本 ADR + 架构 §7 直接拒绝 PR。

### 理由

- 边界靠人治必然失守；靠测试才能长期守住。
- 参照已成熟的 spec drift checker 模式，团队已习惯此类硬门禁。

### 影响

- ✅ 框架可安全开源（无业务/品牌泄漏）。
- ⚠️ 需维护白名单（如文档示例、CHANGELOG 中提及 deeppath 属合法），见脚本 `ALLOWLIST`。

### 例外

- `docs/`、`examples/`、`CHANGELOG.md`、`README.md`、`docs/migration/deeppath.md` 允许提及 deeppath（作为消费者案例）。
- 检查范围限定 `packages/**/src`（即可发布的源码）。

---

## ADR-003 · `agent-kit` 与 `agent-app` 分包

| 字段 | 值 |
|---|---|
| **状态** | Accepted |
| **日期** | 2026-06-21 |
| **关联** | target-architecture §4、§10 Q1 |

### 背景

后端"应用套件"包含两类能力：(a) 无 HTTP 的引擎（MCP 执行、上下文、技能、模型基座）；(b) FastAPI 装配（应用工厂、路由、认证、SSE transport）。是否合为一包？

### 决策

**分为两包：**
- `steerable-agent-kit`（Layer C，无 Web 框架依赖）：MCP 执行引擎、上下文引擎、技能引擎、SQLModel 基座、SPI 注册接口。
- `steerable-agent-app`（Layer C，依赖 kit + FastAPI）：`create_app()` 工厂、`/api/v2/chats/*` 端点、认证脚手架、`FastAPISseTransport`。

### 理由

1. kit 无 HTTP 依赖，可被 CLI、notebook、Celery worker、其它 Web 框架复用。
2. app 是"开箱即用的 FastAPI 装配"，但不应强制所有消费者都用 FastAPI。
3. 符合分层"单一职责 + 小表面"原则。

### 影响

- ✅ 更灵活的复用边界。
- ⚠️ lockstep 发布包数 7→11，发布成本上升（架构 §10 Q4 评估是否独立版本线）。

### 备选（已否决）

- **合为单包 `steerable-agent-app`**：否决——强绑 FastAPI，限制非 HTTP 复用场景。

---

## ADR-004 · 数据模型基座从严

| 字段 | 值 |
|---|---|
| **状态** | Accepted |
| **日期** | 2026-06-21 |
| **关联** | target-architecture §10 Q2、refactor-plan P0/P2 |

### 背景

`deeppath-api/app/models/` 中有约 85 个模型，混有 runtime 数据、DeepPath 产品数据、会员/支付数据和迁移历史。若直接抽"通用模型基座"，很容易把 DeepPath 产品形态固化进 Steerable。

### 决策

**模型 base 从严。第一阶段只允许 runtime 必需、跨产品自然成立的模型进框架。**

第一批候选：
- `AgentSession`
- `ChatMessage` / 最小 `Chat`
- `HarnessTrace` / `HarnessTraceEvent`

默认留业务：
- `Project`
- `Task`
- `Goal`
- `Event`
- `Note`
- `Calendar`
- `Membership` / `Payment` / `Invitation`
- 任何 CIFLog / 垂直业务模型

`User` 暂不默认进 base。它看似通用，但会牵出认证、租户、时区、NextAuth 兼容、会员字段和历史迁移。第一阶段可以通过 `AuthPrincipal` / `user_id` 接口解耦，等 app 层稳定后再决定是否提供可选 `UserBase`。

### 理由

1. 框架必须证明非 DeepPath 产品也自然需要这些模型。
2. 产品模型一旦进入框架，会强迫外部用户接受 DeepPath 的目标/任务/笔记范式。
3. 先用接口和 adapter 传递 `user_id` / `session_id`，比抽错表更容易回滚。

### 影响

- ✅ P2 的 `task` 切片会作为业务插件接入，不会把 `Task` 表搬入框架。
- ✅ `steerable-agent-kit` 的第一版模型面更小，更适合开源。
- ⚠️ app 层需要更明确的 StorageAdapter / AuthPrincipal 合同。

---

## ADR-005 · web-kit 先低层后高阶

| 字段 | 值 |
|---|---|
| **状态** | Accepted |
| **日期** | 2026-06-21 |
| **关联** | target-architecture §10 Q3、refactor-plan P3 |

### 背景

`deeppath/apps/web/src/app/goals/desktop/` 的三栏工作台是成熟产品形态，但其中混有 DeepPath 的目标、任务、笔记、资源库、会员、路由和 Next App Router 约束。若一开始抽完整 `SteerableWebApp`，风险是把 DeepPath 的工作台范式固化成框架。

### 决策

**web-kit 第一阶段只抽低层可复用能力；完整 `SteerableWebApp` 后置为 PoC。**

优先抽：
- theme / design tokens / dark mode provider
- runtime adapter（remote HTTP/SSE、local IPC、custom）
- action renderer
- panel slot system
- 与 `@steerable/agent-ui` 的桥接

后置：
- 完整 Next App Router 集成
- `/goals` 三栏工作台高阶外壳
- 营销/会员/定价路由集成

### 理由

1. 低层能力能被 DeepPath 和非 DeepPath 示例同时验证。
2. Next App Router、SSR/client boundary、PWA 和现有路由会显著增加第一阶段复杂度。
3. 面板注入比"框架自带 Tasks/Notes"更符合零业务边界。

### 影响

- ✅ `Tasks/Notes/ResourceLibrary` 继续作为业务面板注入。
- ✅ P3 可先交付可复用底座，不阻塞 DeepPath 现有页面。
- ⚠️ `SteerableWebApp` 终态 API 需要等低层 API 稳定后再定。

---

## ADR-006 · Entitlement 采用声明式 key，谓词式实现

| 字段 | 值 |
|---|---|
| **状态** | Accepted |
| **日期** | 2026-06-21 |
| **关联** | target-architecture §10 Q5、spi-backend.md、spi-frontend.md |

### 背景

收费闸门需要同时作用在后端工具/模型/配额和前端功能/面板/入口。若前端使用 `can(feature)`，后端使用一套完全不同的 `can_use_tool()` / `check_quota()` 命名，会很快出现权限漂移。

### 决策

**框架层使用声明式 entitlement key；业务层可以用谓词式实现适配这些 key。**

框架合同：
- `feature:<name>`：功能开关，如 `feature:workspace.notes`
- `tool:<name>`：工具开关，如 `tool:create_task`
- `model:<id>`：模型开关，如 `model:gpt-4.1`
- `quota:<kind>`：配额，如 `quota:messages_per_day`

后端 `EntitlementGate` 与前端 `Entitlements` 都围绕这些 key 工作。DeepPath 可以在业务实现里把 key 映射到会员等级、支付状态、试用期或运营配置。

### 理由

1. 声明式 key 是跨前后端的稳定合同。
2. 谓词式实现保留业务灵活性，不把会员等级、价格、支付渠道放进框架。
3. key 可以进入工具 schema、UI 面板配置和测试 fixture，便于做一致性测试。

### 影响

- ✅ 前后端付费墙可以共享 feature/quota 命名。
- ✅ 框架仍只定义接口和 key，不包含具体收费逻辑。
- ⚠️ 需要维护 entitlement key registry，避免拼写漂移。

---

## 待决策（后续补充 ADR）

| 编号 | 议题 | 关联 |
|---|---|---|
| ADR-007 | Layer C/D 是否脱离 lockstep 独立版本线 | 架构 §10 Q4 |
