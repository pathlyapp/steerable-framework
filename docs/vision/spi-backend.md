# 后端 SPI 接口草案（`steerable-agent-kit` / `steerable-agent-app`）

| 字段 | 值 |
|---|---|
| **状态** | Accepted (Signature-level Contract) |
| **日期** | 2026-06-21 |
| **关联** | [target-architecture.md](./target-architecture.md) §6.1 · `spec/runtime/chat-loop.md` §5（hooks）· [decisions.md](./decisions.md) ADR-003 |

> **目的.** 定义业务（deeppath-api）如何把工具、技能、模型、路由、ChatLoop hooks、上下文 Provider、收费闸门、认证后端**注入框架**，从而让框架承载通用引擎、业务只留收费/垂直逻辑。所有签名为**草案**，用于评审"SPI 是否足以承载现有业务"。

---

## 目录

1. [总览：装配中心 `SteerableApp`](#1-总览装配中心-steerableapp)
2. [SPI-1 工具 / Handler](#2-spi-1-工具--handler)
3. [SPI-2 技能包](#3-spi-2-技能包)
4. [SPI-3 数据模型基座](#4-spi-3-数据模型基座)
5. [SPI-4 路由](#5-spi-4-路由)
6. [SPI-5 ChatLoop hooks](#6-spi-5-chatloop-hooks)
7. [SPI-6 上下文 Provider](#7-spi-6-上下文-provider)
8. [SPI-7 收费闸门 EntitlementGate](#8-spi-7-收费闸门-entitlementgate)
9. [SPI-8 认证后端](#9-spi-8-认证后端)
10. [完整装配示例（deeppath-api）](#10-完整装配示例deeppath-api)
11. [覆盖性自检：现有业务 → SPI](#11-覆盖性自检现有业务--spi)

---

## 1. 总览：装配中心 `SteerableApp`

```python
# steerable_agent_app/__init__.py（框架）
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Sequence

from fastapi import APIRouter, FastAPI
from sqlmodel import SQLModel

from steerable_agent_kit import (
    ToolSpec, SkillPack, ContextProvider, EntitlementGate, AuthBackend,
)
from steerable_agent_runtime import LoopHooks


def create_app(
    *,
    title: str,
    tools: Sequence[ToolSpec] = (),
    skill_packs: Sequence[SkillPack] = (),
    models: Sequence[type[SQLModel]] = (),
    routers: Sequence[APIRouter] = (),
    loop_hooks: LoopHooks | None = None,
    context_providers: Sequence[ContextProvider] = (),
    entitlement_gate: EntitlementGate | None = None,
    auth_backend: AuthBackend | None = None,
    settings: "AppSettings | None" = None,
) -> "SteerableApp":
    """组装框架引擎 + 业务插件，返回可运行的 SteerableApp。"""
    ...


@dataclass
class SteerableApp:
    fastapi: FastAPI                       # 装配完成的 FastAPI 实例
    registry: "PluginRegistry"            # 运行期可查询的插件注册表

    def include_router(self, router: APIRouter) -> None: ...
    def register_tool(self, tool: ToolSpec) -> None: ...
    # …其余 register_* 与 create_app 参数一一对应，支持运行期追加
```

**设计原则**（与 ChatLoop RFC §5.1 一致）：
- 注册表模式（registry），不是继承重写。
- 同一扩展点可注册多个，按注册顺序生效。
- 业务永不 fork 框架 body；缺口通过**新增扩展点**解决（ADR / RFC §1.3 硬契约）。

---

## 2. SPI-1 工具 / Handler

把 MCP 工具拆成两半：**执行引擎**（框架）+ **具体 handler**（通用进框架 base，业务/垂直留业务）。

```python
# steerable_agent_kit/tools.py（框架）
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from steerable_agent_protocol import ToolCall, ToolResult

ToolMode = Literal["read", "safe_write", "destructive", "local"]
ToolHandler = Callable[["ToolContext", ToolCall], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolSpec:
    name: str                              # e.g. "create_task"
    description: str
    json_schema: dict[str, Any]            # 参数 JSON Schema（喂给 LLM）
    handler: ToolHandler
    mode: ToolMode = "read"                # 供 harness.policy.decide_tool_mode 使用
    tags: tuple[str, ...] = ()             # e.g. ("task",) / ("cflog",)


@dataclass
class ToolContext:
    """框架注入 handler 的运行期上下文（无业务字段）。"""
    user_id: str
    session_id: str
    db: Any                                # AsyncSession（由 StorageAdapter 提供）
    state: dict[str, Any]                  # 与 ChatLoop HookContext.state 同源
```

### 业务侧用法（deeppath-api）

```python
# deeppath-api/app/business/tools.py（业务）
from steerable_agent_kit import ToolSpec, ToolContext
from steerable_agent_protocol import ToolCall, ToolResult

async def create_task(ctx: ToolContext, call: ToolCall) -> ToolResult:
    # 现有 task_handlers.py 的写库逻辑迁到这里（业务）
    task = await _persist_task(ctx.db, ctx.user_id, call.arguments)
    return ToolResult(call_id=call.id, success=True, value=task.model_dump())

deeppath_tools = [
    ToolSpec(name="create_task", description="创建任务", json_schema={...},
             handler=create_task, mode="destructive", tags=("task",)),
    # …goal / event / note / document / cflog 等
]
```

> **拆分裁定**：`task/goal/event/note` 这类"标准 CRUD 模式"的 handler 骨架可提供框架 base helper（如 `crud_handler(model=...)`），但**具体业务字段/校验留业务**。`dp-action 提案队列`、`cflog_*` 完全留业务。

---

## 3. SPI-2 技能包

```python
# steerable_agent_kit/skills.py（框架）
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class SkillPack:
    name: str                              # e.g. "deeppath-core"
    directory: Path                        # 含 SKILL.md 的目录
    priority: int = 100                    # 决定 prompt 拼装顺序

class SkillEngine:
    """框架：加载 + 排序 + 拼装 SKILL.md（迁自 skill_loader.py + prompt.py）。"""
    def register_pack(self, pack: SkillPack) -> None: ...
    def build_system_skills(self, *, enabled: set[str] | None = None) -> str: ...
```

- **通用技能进框架技能包**（`00-identity`、`80-tool-usage`、`85-local-exec`…）。
- **业务/垂直技能留业务**（deeppath 产品技能、cflog 测井技能）。

```python
# 业务
deeppath_skill_pack = SkillPack(name="deeppath", directory=Path("app/skills"), priority=50)
```

---

## 4. SPI-3 数据模型基座

```python
# steerable_agent_kit/models/__init__.py（框架 base）
# 仅 runtime 必需、跨产品自然成立的表进框架。
# 第一批候选：AgentSession, ChatMessage, HarnessTrace, HarnessTraceEvent。
class AgentSessionBase(SQLModel):
    id: str = Field(primary_key=True)
    user_id: str = Field(index=True)
    created_at: datetime

class ChatMessageBase(SQLModel):
    id: str = Field(primary_key=True)
    session_id: str = Field(index=True)
    role: str
    content: str
```

```python
# 业务模型（deeppath-api）默认留业务，通过 adapter / user_id 接入。
class Task(SQLModel, table=True):
    __tablename__ = "task"
    # DeepPath 产品字段留在业务仓库，不进入框架 base。
```

> 边界裁定见 [decisions.md](./decisions.md) ADR-004。原则：base 从严。`Project/Task/Goal/Event/Note` 默认留业务；`User` 第一阶段也不默认进 base，而是通过 `AuthPrincipal` / `user_id` 接口解耦。

---

## 5. SPI-4 路由

```python
# 业务把专属端点作为 APIRouter 注入
steer = create_app(..., routers=[billing_router, cflog_router])
# 或运行期：steer.include_router(billing_router)
```

框架提供通用端点（`/api/v2/chats/*`、`/api/v2/agents/*`、`/health`）；业务只加专属路由（会员、支付、cflog）。

---

## 6. SPI-5 ChatLoop hooks

直接复用 ChatLoop RFC §5 的 **11 个 hook**。`LoopHooks` 是注册表封装：

```python
# steerable_agent_runtime/hooks.py（框架）
from steerable_agent_runtime import (
    SendMessagesCtx, ToolCallCtx, ToolResultCtx, EmitCtx, # …RFC §7
)

class LoopHooks:
    def on(self, hook: str, callback) -> "LoopHooks": ...   # 链式

# 业务（deeppath-api）
deeppath_loop_hooks = (
    LoopHooks()
    .on("before_send_messages", inject_system_prompt_and_context)  # 系统提示+上下文
    .on("before_tool_call", entity_link_args)                      # 实体链接
    .on("before_tool_call", queue_dp_action_proposal)              # dp-action 队列
    .on("after_tool_result", convert_payload_timezones)            # 时区转换
    .on("emit", strip_dp_actions_and_pseudo_calls)                 # 内容清洗/防御
    .on("budget_exhausted", notify_user_via_websocket)
)
```

> RFC §10 已证明：deeppath-api 的 33 项业务行为可全部映射到这 11 个 hook，**无需第 12 个**。本 SPI 不再重复，直接引用 RFC §10/§11 映射表。

---

## 7. SPI-6 上下文 Provider

```python
# steerable_agent_kit/context.py（框架）
from __future__ import annotations
from typing import Protocol
from steerable_agent_protocol import Message

class ContextProvider(Protocol):
    name: str
    async def provide(self, *, user_id: str, session_id: str,
                      query: str, state: dict) -> list[Message] | str: ...

class ContextEngine:
    """框架：编排多个 Provider（迁自 context_manager.py + operation_definitions.py）。"""
    def register(self, provider: ContextProvider) -> None: ...
    async def build(self, **kw) -> str: ...
```

```python
# 业务 Provider（deeppath-api）
deeppath_context_providers = [
    GoalsProvider(), TasksProvider(), EventsProvider(),
    GraphRagProvider(), SearchProvider(),
]
```

引擎进框架，**具体 Provider（goals/graph_rag…）留业务**——它们查 deeppath 产品表。

---

## 8. SPI-7 收费闸门 EntitlementGate

**这是业务层与框架交互的关键接口**：框架在工具/模型/配额处调用闸门，闸门实现留业务。

```python
# steerable_agent_kit/entitlement.py（框架，仅接口）
from __future__ import annotations
from typing import Protocol
from dataclasses import dataclass

@dataclass(frozen=True)
class EntitlementDecision:
    allowed: bool
    reason: str = ""
    upgrade_hint: str | None = None        # e.g. "升级 Pro 解锁"

class EntitlementGate(Protocol):
    async def check(self, *, user_id: str, key: str, amount: int = 1) -> EntitlementDecision: ...
```

框架内**只有这个 Protocol**；任何具体计费实现（会员等级、配额、微信/支付宝）**禁止进框架**（ADR-002）。`key` 采用 ADR-006 的声明式格式：`tool:<name>`、`model:<id>`、`feature:<name>`、`quota:<kind>`。框架在 ChatLoop 的 `before_tool_call` / `before_send_messages` 处自动咨询闸门（若已注册）。

```python
# 业务实现（deeppath-api/app/membership/gate.py）
class DeeppathEntitlementGate:
    async def check(self, *, user_id, key, amount=1) -> EntitlementDecision:
        tier = await get_tier(user_id)
        if key in PRO_KEYS and tier == "free":
            return EntitlementDecision(False, "需要 Pro", "升级 Pro 解锁该功能")
        return EntitlementDecision(True)
```

> 接口形态见 ADR-006：框架合同采用声明式 key，业务可在实现内部使用谓词式 helper。

---

## 9. SPI-8 认证后端

```python
# steerable_agent_kit/auth.py（框架，接口 + 默认 JWT 实现）
from typing import Protocol

class AuthBackend(Protocol):
    async def authenticate(self, token: str) -> "AuthPrincipal | None": ...
    async def issue_token(self, user_id: str) -> str: ...

# 框架提供默认 JwtAuthBackend；业务可替换（OAuth/微信登录等）
```

deeppath 现有 JWT（与 NextAuth 兼容、bcrypt rounds=10）可作为默认实现进框架 base；OAuth/微信留业务或作为可选 backend。

---

## 10. 完整装配示例（deeppath-api）

```python
# deeppath-api/app/main.py（终态）
from steerable_agent_app import create_app
from app.business import (
    deeppath_tools, deeppath_skill_pack, deeppath_models,
    deeppath_context_providers, deeppath_loop_hooks,
)
from app.membership import DeeppathEntitlementGate, billing_router
from app.cflog import cflog_router

steer = create_app(
    title="DeepPath API",
    tools=deeppath_tools,
    skill_packs=[deeppath_skill_pack],
    models=deeppath_models,
    routers=[billing_router, cflog_router],          # 业务专属路由
    loop_hooks=deeppath_loop_hooks,                  # RFC 11 hooks
    context_providers=deeppath_context_providers,
    entitlement_gate=DeeppathEntitlementGate(),      # ← 收费，业务独有
)
app = steer.fastapi
```

通用引擎（ChatLoop、MCP 执行、上下文/技能引擎、FastAPI 装配、SSE transport、认证）全部来自框架；business 目录只剩 deeppath 专属逻辑 + 收费。

---

## 11. 覆盖性自检：现有业务 → SPI

| 现有业务（deeppath-api） | 承载 SPI | 备注 |
|---|---|---|
| MCP handlers（task/goal/event/note/document） | SPI-1 工具 | 通用 CRUD 模式可用 base helper |
| dp-action 提案队列 | SPI-5 hook `before_tool_call`（返回 deferred ToolResult） | RFC §10、附录 B.2 |
| 实体链接（_extract_entity_hints） | SPI-5 hook `before_tool_call` | RFC §10 |
| 时区转换（_convert_payload_datetimes_to_local） | SPI-5 hook `after_tool_result` | RFC §10 |
| pseudo-tool 防御（Qwen/DeepSeek） | SPI-5 hook `emit` | RFC §10、Q1（或框架 defenses 工具） |
| 系统提示 + persona | SPI-5 hook `before_send_messages` + SPI-2 技能 | RFC §10 |
| 上下文窗口（goals/tasks/graph_rag） | SPI-6 上下文 Provider | 引擎进框架 |
| 技能（34 SKILL.md） | SPI-2 技能包 | 通用进框架、业务留 api |
| 会员/配额 gate | SPI-7 EntitlementGate | **业务独有** |
| 支付/会员路由 | SPI-4 路由 | **业务独有** |
| JWT 认证 | SPI-8 认证后端 | 默认实现可进框架 base |
| orchestration / groupchat / goal_verifier | **留业务**，用 hooks 包装 loop | RFC NG1/NG2 |
| cflog 工具 | SPI-1 工具（tags=("cflog",)） | **垂直业务** |

**结论草案**：现有后端业务 100% 可由 8 个 SPI + ChatLoop 11 hooks 承载，无需 fork 框架主体。评审重点：SPI-3 模型边界（ADR-004）与 SPI-7 闸门形态（ADR-006）。
