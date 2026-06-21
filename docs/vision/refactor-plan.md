# 改造计划：从"Agent 管道层"到"全栈框架"

| 字段 | 值 |
|---|---|
| **状态** | Accepted |
| **创建** | 2026-06-21 |
| **更新** | 2026-06-21 (P1 & P2 Completed) |
| **作者** | DeepPath / Steerable maintainers |
| **目标架构** | [target-architecture.md](./target-architecture.md) |
| **关联 RFC** | `spec/runtime/chat-loop.md`（ChatLoop，已有 runtime 雏形，需生产化接入） |
| **覆盖仓库** | `steerable-framework` · `deeppath-api` · `deeppath`(web) · `deeppath-agent` · `deeppath-desktop` |

> **TL;DR.** 把 [目标架构](./target-architecture.md) 拆成 **6 条工作流（WS1–WS6）× 5 个阶段（P0–P4）**。核心顺序调整为：先做**重基线 + 关键 ADR**（P0），再把已有 `ChatLoop` 做成**生产可信 runtime**（P1），然后用 DeepPath 的一个真实工具证明**后端插件化**（P2），再抽 **Desktop/Web 的低层 shell 能力**（P3），最后做高阶 App 装配与开源治理（P4）。每阶段都必须可独立上线、可回滚。预计**核心收敛（P0–P2）约 6–10 周**，全栈完成约 **5–7 个月**（视投入）。

---

## 目录

1. [指导原则](#1-指导原则)
2. [工作流总览（WS1–WS6）](#2-工作流总览ws1ws6)
3. [阶段路线图（P0–P4）](#3-阶段路线图p0p4)
4. [P0 · 重基线：关键决策 + SPI 契约](#4-p0--重基线关键决策--spi-契约)
5. [P1 · Runtime 稳定化（第一圈）](#5-p1--runtime-稳定化第一圈)
6. [P2 · 后端插件化切片（第二圈）](#6-p2--后端插件化切片第二圈)
7. [P3 · Shell 能力抽取（第三圈）](#7-p3--shell-能力抽取第三圈)
8. [P4 · 开源化与治理](#8-p4--开源化与治理)
9. [文件迁移映射表（按仓库）](#9-文件迁移映射表按仓库)
10. [风险与缓解](#10-风险与缓解)
11. [验收里程碑](#11-验收里程碑)

---

## 1. 指导原则

- **P-1 双切片优先**：先用框架最小工具验证 runtime，再用 DeepPath 真实工具验证业务插件，避免把产品复杂度误判为框架复杂度。
- **P-2 不放宽框架主体**：业务无法承载时新增扩展点，不改 loop body / app factory（继承 ChatLoop RFC §1.3 硬契约）。
- **P-3 直接替换与彻底重构**：不做双路径影子运行。在各层通过单元和集成测试保障功能对等后，直接将 `deeppath-api` 旧 loop 替换并彻底下线，避免维护两套业务逻辑的冗余。
- **P-4 Spec 先行**：任何跨语言契约变更先改 `spec/`，跑 `pnpm gen` + drift check。
- **P-5 Base 从严**：模型、handler、面板默认留业务；只有非 DeepPath 参考应用也自然需要的能力才进框架。
- **P-6 每阶段可回滚**：旧代码保留为 re-export shim 至少一个 release。

---

## 2. 工作流总览（WS1–WS6）

| 工作流 | 名称 | 目标产物 | 主要阶段 |
|---|---|---|---|
| **WS1** | Runtime 核心 | 已有 `ChatLoop` 稳定化 + provider/tool/storage/transport/SSE/trace 合同 | P1 |
| **WS2** | 后端插件化 | `steerable-agent-kit/app`：工具注册、上下文/技能引擎、极小模型基座、FastAPI 装配 | P2 |
| **WS3** | 客户端底座 | `@steerable/desktop-kit`：sidecar 监管 / IPC / PTY / 本地工具路由接口，Electron 壳后置 | P3 |
| **WS4** | 前端底座 | `@steerable/web-kit`：theme / runtime adapter / action renderer / panel slots，完整工作台后置 | P3 |
| **WS5** | 插件 SPI（横切） | 各层 SPI 接口 + 收费闸门 + 模型/面板边界 | P0（设计）→ 全程演进 |
| **WS6** | 开源治理（横切） | 文档 / 参考应用 / 发布策略 / 边界检查 | P4（收尾） |

---

## 3. 阶段路线图（P0–P4）

```
P0 重基线(1-2周)      WS5: 当前代码状态 + ADR-004/005/006 + SPI 收敛
   │
   ▼
P1 Runtime(2-4周)     WS1: ChatLoop 生产化 + 最小框架切片 + sidecar roundtrip
   ▼
P2 后端插件(4-6周)    WS2: DeepPath task 切片作为插件接入，验证 kit/app 边界
   ▼
P3 Shell(4-6周)       WS3+WS4: 先抽 desktop/web 低层能力，再评估高阶外壳
   │
   ▼
P4 产品化(持续)       WS6: 参考应用、高阶 App 装配、发布纪律、开源治理
```

> 周数为"专注投入"估算，可并行压缩。P1/P2 是关键路径；P3 不应抢在后端插件边界被真实业务验证之前。

---

## 4. P0 · 重基线：关键决策 + SPI 契约

**目标**：在动任何大改之前，把文档状态与当前代码现实对齐，并先拍板会阻塞 P1/P2 的关键边界。P0 的重点不是写更多代码，而是防止后续按过期前提推进。

### 4.1 任务

| # | 任务 | 产出 | 所属 |
|---|---|---|---|
| P0.1 | 重基线 ChatLoop 状态：从“未实现”改为“已有雏形，需生产化接入” | RFC / vision 状态更新 | WS1 |
| P0.2 | 落地六个前置决策（见 §4.2）写入 `docs/vision/decisions.md` | 决策记录 (ADR) | WS5 |
| P0.3 | 收敛后端 SPI 接口草案（工具/技能/模型/路由/hooks/context/entitlement/auth） | `docs/vision/spi-backend.md` + 接口 stub | WS5 |
| P0.4 | 收敛前端/桌面 SPI：先低层能力，后高阶 App | `docs/vision/spi-frontend.md` | WS5 |
| P0.5 | 定义兼容合同：SSE、trace、auth/session、DB schema、sidecar IPC | [compat-contract.md](./compat-contract.md) | WS1/WS2/WS3 |
| P0.6 | 在 `spec/` 锁定 SSE 事件子类型（RFC §6.2，A3 工作项） | discriminated JSON Schema | WS1 |

### 4.2 必须先拍板的六个决策（ADR）

1. **后端权威语言 = Python**（ChatLoop RFC 已论证：api loop 最全、sidecar 是 Python）。本地客户端不再写 TS loop，统一走 sidecar。
2. **框架零业务边界**（target-architecture §7）：禁止 payment/membership/品牌/cflog 进框架，CI 强制。
3. **kit/app 分包**（target-architecture §10 Q1）：`steerable-agent-kit`（无 HTTP）与 `steerable-agent-app`（FastAPI 装配）分离。
4. **模型基座从严**（ADR-004）：第一阶段只放 runtime 必需模型；`Project/Task/Goal/Event/Note` 默认留业务。
5. **web-kit 先低层后高阶**（ADR-005）：先抽 theme/runtime/action renderer/panel slots，完整 `SteerableWebApp` 后置。
6. **entitlement 形态统一**（ADR-006）：前后端先采用同一套声明式 feature/quota key；谓词式实现可作为业务适配层。

### 4.3 验收

- [x] ChatLoop RFC / vision 状态与当前 runtime 代码一致
- [x] ADR-004/005/006 至少达到 Accepted
- [x] 后端、前端、桌面 SPI 草案评审通过（接口签名级，不含实现）
- [x] 兼容合同定义完成：后续 P1/P2 的新旧路径可以按同一口径对比
- [ ] SSE event subtype schema 在 `spec/` 中细化到可生成类型（P0.6，可进入 P1 第一项）

---

## 5. P1 · Runtime 稳定化（第一圈）

**目标**：先把 Steerable 已有 `ChatLoop` 变成生产可信的唯一 runtime 核心，而不是立刻搬迁整个 `deeppath-api`。P1 只处理框架运行时本身：loop、hooks、provider/tool/storage/transport、SSE、trace、sidecar roundtrip。

### 5.1 双切片的第一刀：框架最小切片（P1.0）

先打通一个不含 DeepPath 产品模型的最小端到端链路：

> **切片目标**：`echo` / `read_only_search` 这类纯框架工具，经由 `ChatLoop` + `ToolRouter` + `FastAPISseTransport` / sidecar transport 跑通"用户发消息 → LLM → tool call → ToolResult → SSE/trace 回流"。这个切片只证明 runtime 合同，不混入 Task/Goal/Project 等业务复杂度。

这个切片通过后，才进入 P2 的 DeepPath 真实业务切片。

### 5.2 WS1 — 核心引擎

| # | 任务 | 来源 → 去向 | 备注 |
|---|---|---|---|
| P1.1 | 重基线 `ChatLoop` | 现有 `steerable_agent_runtime/chat_loop.py` | 标注已完成/未完成 slice；目标是生产可信，不是从零实现 |
| P1.2 | 收敛 11 hooks + `HookContext` 合同 | runtime + RFC | 明确哪些 hook 可 `HOOK_SKIP`、哪些只能 mutate ctx |
| P1.3 | 稳定 provider 流式工具参数重组 | runtime | OpenAI partial args 已有；Anthropic parity 作为明确后续项 |
| P1.4 | 稳定 harness 集成 | runtime ← harness | budget/retry/completion/tracing 的行为以 conformance 固化 |
| P1.5 | 稳定 SSE/trace 合同 | runtime + protocol spec | 所有 event subtype 可由 TS/Py 类型消费；错误/预算/取消语义固定 |
| P1.6 | sidecar roundtrip 驱动 ChatLoop | sidecar + runtime | stdio JSON-RPC transport 转发 typed SSE，不做业务工具 |
| P1.7 | 明确 orchestration 边界 | runtime `orchestration/*` + RFC NG1/NG2 | 决定保留为框架实验能力、正式能力，还是移出公共 surface |

### 5.3 P1 明确不做

- 不迁移 `deeppath-api` 的 `task/goal/event/note` handler。
- 不抽 SQLModel 产品表。
- 不删除 `deeppath-api` 旧 loop。
- 不抽完整 FastAPI app factory。
- 不抽 Web/Desktop 外壳。

### 5.4 验收

- [x] 最小框架工具切片端到端跑通，产出 typed SSE + trace
- [x] sidecar 通过 ChatLoop roundtrip 冒烟，且无需 TS loop 参与
- [x] ChatLoop hook / provider / tool / trace 行为有 conformance 或 golden 测试
- [x] runtime 公共 surface 与 RFC / docs 一致，orchestration 边界已定

---

## 6. P2 · 后端插件化切片（第二圈）

**目标**：在 P1 runtime 稳定后，用 DeepPath 的一个真实业务工具证明 SPI 可承载生产行为。P2 的重点是让 `deeppath-api` 逐步变成"框架 runtime + 业务插件"，但仍避免一口气抽完整模型、完整 app 和全部 handlers。

### 6.1 任务

| # | 任务 | 来源 → 去向 | 备注 |
|---|---|---|---|
| P2.1 | 建立 `ToolSpec` / `ToolContext` / `ToolRouter` 业务注册接口 | runtime → kit | **[已完成]** 建立 `ToolSpec`, `ToolContext` 强类型接口，支持 contextvars 上下文隔离 |
| P2.2 | 建立 `ContextProvider` / `SkillPack` 最小接口 | `context_system` / `skill_loader.py` → kit 接口 | **[已完成]** 抽象 `ContextProvider` 与 `SkillPack` / `SkillEngine` SPI 契约 |
| P2.3 | 建立极小模型基座 | ADR-004 | **[已完成]** `ChatMessageBase`, `AgentSessionBase` 极小 SQLModel 基类已移至 `agent-kit` |
| P2.4 | DeepPath `task` 工具作为业务插件接入 | `task_handlers.py` 留 `deeppath-api` | **[已完成]** 接入 `CreateTaskHandler` 进行 SPI 工具集成与 `test_steerable_task_plugin` 测试 |
| P2.5 | 后端业务工具全量适配与直接替换 | `deeppath-api` | 适配并注册全量业务工具（Task/Goal/Project/Event/Note 等）到框架，通过集成测试保障对齐 |
| P2.6 | `steerable-agent-app` 最小 FastAPI 装配 | `deeppath-api/app/main.py` 通用部分 | **[已完成]** 新建 `steerable-agent-app` 模块并提供完整的 `create_app` 骨架及 `FastAPISseTransport` 路由及测试 |
| P2.7 | 直接下线旧 Loop 与旧 Preload 逻辑 | `deeppath-api` / `deeppath-agent` | 一键移除旧 loop.py、旧 preload 桥、旧 local-backend，全部流量与调用直接切换至新框架 |


### 6.2 直接切换与一键下线标准 (Direct Switch & Instant Decommissioning)

不进行渐进式灰度或双路径影子并存，采取一键切流下线。为保障直接切换安全，下线前必须达成以下一键替换门槛：
1. **全量业务工具迁移 (Full Tool Migration)**: 所有核心业务工具（`task`、`goal`、`project`、`event`、`note` 等）均完全移至框架的 `ToolRouter` 注册。
2. **端到端集成测试通过 (End-to-End Integration Testing)**: 废除影子对比，全面改为覆盖率 > 90% 的集成测试，验证各业务模型的操作行为、时区转换和 DB side effects 符合业务规范。
3. **前端适配与联调完毕 (Client Migration Verification)**: 网页端和桌面端同步切换至新 API（`/api/v2/chats/stream`）和新 `desktop-kit`。
4. **一键删除旧冗余 (Zero-Legacy Cleanup)**: 在切流成功的当天，立即从 `deeppath-api` 中物理删除旧的 `loop.py`、旧 preload 桥等全部历史冗余，拒绝遗留技术债。

### 6.3 验收

- [x] `task` 业务切片经框架 runtime + SPI 跑通，行为与旧路径等价
- [x] 产品模型仍留 `deeppath-api`，框架未引入 `Task/Goal/Project/Event/Note`
- [x] `steerable-agent-kit/app` 的接口足够承载全部业务工具，支持一键切换
- [x] 制定了干净彻底的一键切流及代码清理计划

---

## 7. P3 · Shell 能力抽取（第三圈）

**目标**：在 runtime 与后端插件边界跑顺后，再抽 Web/Desktop 的低层 shell 能力。P3 不以一口气交付完整 `SteerableWebApp` / `createDesktopApp` 为目标，而是先抽可独立测试、可被非 DeepPath 示例复用的底座。

### 7.1 Desktop 任务

| # | 任务 | 来源 → 去向 | 备注 |
|---|---|---|---|
| P3.1 | 抽 `SidecarSupervisor` | `deeppath-agent/src/sidecar/` → desktop-kit | 不含 CIFLog 工具 |
| P3.2 | 抽 IPC bridge contract | `preload.ts` / local bridge | 类型化 request/response，安全边界明确 |
| P3.3 | 抽可见 PTY + local tool routing interface | `terminal-manager.ts`、`tool-router.ts` 框架部分 | `cflog_*` 仍是业务 toolPack |
| P3.4 | 抽 Python runtime packaging helpers | sidecar build/package 经验 | 体积目标分阶段，不把 300MB 当作当前硬门槛 |

### 7.2 Web 任务

| # | 任务 | 来源 → 去向 | 备注 |
|---|---|---|---|
| P3.5 | 设计 token / theme provider | `deeppath/apps/web` CSS 变量系统 → web-kit | 对接 `next-themes`，不硬编码 DeepPath 品牌 |
| P3.6 | runtime adapter | `src/lib/runtime/` → web-kit | remote HTTP/SSE + local IPC + custom adapter |
| P3.7 | action renderer | `src/lib/agentic/action-system/` → web-kit / agent-ui | 先抽协议驱动渲染，不抽业务 action |
| P3.8 | panel slot system | `goals/desktop` 外壳经验 → web-kit | `Tasks/Notes/ResourceLibrary` 作为业务 panel 注入 |
| P3.9 | 高阶 App 装配 PoC | `SteerableWebApp` / `createDesktopApp` | 只做 PoC；正式替换放 P4 |

### 7.3 验收

- [ ] desktop-kit 能在非 CIFLog 示例里启动 sidecar、打开 IPC、运行可见 PTY
- [ ] web-kit 能在示例应用里使用 theme/runtime/action renderer/panel slots
- [ ] DeepPath web/desktop 仍可渐进接入，不要求本阶段完全退化为业务皮
- [ ] 完整高阶外壳 API 的设计有 PoC，但是否替换生产应用留到 P4 决策

---

## 8. P4 · 开源化与治理

**目标**：让 Steerable 成为真正可对外开源/复用的框架。

### 8.1 任务

| # | 任务 | 备注 |
|---|---|---|
| P4.1 | 彻底剥离框架内残留 deeppath/cflog/品牌痕迹 | 以边界规则 + 评审清单长期守住 |
| P4.2 | 清理文档漂移 | 删/更新历史描述；更新 README / migration docs / examples |
| P4.3 | `examples/` 升级为"最小可跑参考产品" | 证明框架自洽：一个非 deeppath 的小 Agent 产品（Web+API+可选桌面） |
| P4.4 | License / NOTICE / 贡献指南完善 | Apache 2.0 已就位 |
| P4.5 | 发布线与 SPI 兼容性测试 | 提前评估 Layer C/D 是否独立版本线（架构 §10 Q4） |
| P4.6 | 更新 `docs/spec/architecture.md` 指针指向本 vision | 架构 §9 |

### 8.2 验收

- [ ] 一个全新业务（非 deeppath）能仅靠 Steerable 包 + 插件跑起最小产品
- [ ] 边界规则、发布规则、示例应用和文档一致
- [ ] 文档无漂移（agent_stack 等历史描述清除）

---

## 9. 文件迁移映射表（按仓库）

### 9.1 `deeppath-api` → 框架

| 现有路径 | 去向 | 拆分原则 |
|---|---|---|
| `app/services/harness/loop.py`(5004) | 生产经验反哺 `steerable-agent-runtime/chat_loop.py` + 业务 hooks(留 api) | 当前不是从零搬代码，而是用旧 loop 校验 runtime 行为 |
| `app/services/harness/{policy,budget,retry,completion,tracing}.py` | 已是 `steerable-agent-harness` 的薄封装 | 删封装，直接 import |
| `app/services/harness/mcp/action_runtime/manager.py` | `steerable-agent-kit`（执行引擎） | 引擎进框架 |
| `app/services/harness/mcp/action_runtime/handlers/*` | 第一阶段留 api；后续只抽可参数化 helper | `task/goal/event/note` 默认是业务 handler，不直接进框架 |
| `app/services/harness/context_system/*` | 引擎进 kit；Provider 业务注入 | `context_manager`/`prompt_builder` 进框架 |
| `app/services/harness/{skill_loader,prompt}.py` | `steerable-agent-kit`（技能引擎） | 引擎进框架 |
| `app/services/harness/skills/*.md`(34) | 通用技能进框架技能包；deeppath 技能留 api | 按 §7 边界 |
| `app/main.py`、`api/v2/fs_router.py`、`api/deps.py`、`entrypoint.py` | `steerable-agent-app`（通用骨架） | FastAPI 工厂 |
| `app/models/*`(85) | 极小 base 进 kit；产品表留 api | ADR-004；`Project/Task/Goal/Event/Note` 默认留业务 |
| `app/services/harness/{orchestrator,groupchat,goal_verifier}.py` | 待边界裁定，默认业务 | runtime 已有 orchestration surface，需先定 ADR/公共 surface |
| `membership/`、`payment/` | **留 api**（收费，业务独有） | 永不进框架 |

### 9.2 `deeppath-agent` → 框架

| 现有路径 | 去向 |
|---|---|
| `src/local-backend/router.ts`(1763) | P2/P3 后逐步旁路，最终删除 | 先由 sidecar ChatLoop 承担 chat loop；删除需等影子对比通过 |
| `src/harness/*.ts` | 最终删除生产路径 | TS 仅保留测试/类型 facade |
| `src/main.ts`、`preload.ts` | 后置到 desktop-kit 高阶装配 | 先抽 IPC contract，不急着抽完整 main |
| `src/local-backend/`（路由/编排骨架） | 只抽框架接口 | cflog/测井业务留业务 |
| `src/terminal-manager.ts`、`tool-router.ts`(框架部分)、`local-executor.ts` | desktop-kit 低层能力 | 工具框架进 kit，业务工具留 toolPack |
| `src/sidecar/supervisor.ts` | desktop-kit（第一批） | sidecar 监管是最先可复用能力 |
| `src/cflog/`、测井专家 Agent、`fixtures/cflog/` | **留**（垂直业务） |
| `local-backend/skills/*`（通用） | 框架技能包；cflog 技能留业务 |

### 9.3 `deeppath`(web) → 框架

| 现有路径 | 去向 |
|---|---|
| `apps/web/src/app/goals/desktop/`（三栏外壳/Provider） | 后置到 web-kit 高阶外壳 | 先抽 panel slots，不急着迁移完整 `/goals` |
| `apps/web/src/lib/runtime/` | web-kit（第一批） | 远程/本地/custom runtime adapter |
| `apps/web/src/lib/agentic/action-system/` | web-kit / agent-ui（第一批） | 先抽协议驱动 renderer |
| `apps/web/src/contexts/`（Chat/WebSocket 通用） | 逐项裁定 | 避免把 DeepPath app state 当框架 state |
| CSS 变量 / 设计 token | web-kit 主题（第一批） | 无品牌 token |
| 营销首页、`/pricing`、`/membership`、品牌、`/news` | **留**（业务皮） |
| 业务面板（Tasks/Notes/ResourceLibrary 具体实现） | **留**或作为面板注入（架构 §10 Q3） |

---

## 10. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| **R1 半抽取停滞**（只抽壳没抽肉，反增成本） | 高 | 双切片：P1 最小 runtime，P2 DeepPath task；每阶段有明确不做清单 |
| **R2 业务渗漏回框架** | 高 | Base 从严 + ADR-004/005；§7 评审授权直接拒 |
| **R3 切换直接导致业务回归** | 高 | 建立高覆盖率的集成测试套件，在迁移每个工具时严格保障原功能与时区一致，一键下线前完整通跑测试验证 |
| **R4 sidecar 打包/启动复杂度**（桌面带 Python） | 中 | P3 先抽 supervisor/packaging helpers；体积目标分阶段 |
| **R5 SPI 不足以承载某业务** | 中 | 新增扩展点而非改 body（P-2）；P2 用真实 task 切片验证 |
| **R6 模型基座边界争议** | 中 | P0 先逐表裁定（架构 §10 Q2），base 从严（只放真正通用表） |
| **R7 lockstep 11 包发布成本** | 中 | 不等 P4 才讨论，P0/P3 即定义 Layer C/D 发布线假设 |
| **R8 前端工作台过度通用化** | 中 | 先抽 theme/runtime/action/panel slots；完整工作台外壳后置 |

---

## 11. 验收里程碑

| 里程碑 | 完成标志 | 对应阶段 |
|---|---|---|
| **M0 重基线完成** | RFC / vision / runtime 状态一致 + ADR-004/005/006 Accepted + SPI 草案过审 | P0 |
| **M1 Runtime 可信** | 最小框架工具经 ChatLoop 端到端跑通，typed SSE + trace 稳定 | P1 |
| **M2 后端插件切片** | DeepPath 核心工具作为插件完全注册至新框架，全量集成测试跑通并通过一键替换 | P2 |
| **M3 单一 loop 路径可用** | deeppath-agent 可通过 sidecar ChatLoop 跑通核心 chat，不依赖 TS loop | P3 |
| **M4 Shell 底座可复用** | web-kit/desktop-kit 低层能力在非 DeepPath 示例中可用 | P3 |
| **M5 可开源产品框架** | 全新业务能仅靠 Steerable 包 + 插件跑起最小产品 | P4 |

---

## 附录：建议的下一步（落地第一锤）

1. **本周**：重基线 `ChatLoop` RFC 与 runtime 当前实现，明确哪些 slice 已完成、哪些仍需生产化。
2. **拍板六个决策**（P0.2）：Python 权威 / 零业务边界 / kit-app 分包 / 模型 base 从严 / web-kit 先低层后高阶 / entitlement 统一形态。
3. **全量业务工具适配与测试回归**：对齐所有 core handlers 并在切流前全面补充单元/集成测试。
4. **启动 P1.0 最小框架切片**：用非业务工具跑通 ChatLoop → ToolRouter → SSE/trace。
5. **推进全量工具直接替换并下线旧 Loop**：安全物理清理 `deeppath-api` 的旧逻辑。

---

*本计划与 [target-architecture.md](./target-architecture.md) 配套。落地中如发现 SPI 缺口，先更新 SPI 文档再实现。*
