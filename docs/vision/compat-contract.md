# P0 兼容合同：新旧路径等价与回滚标准

| 字段 | 值 |
|---|---|
| **状态** | Accepted |
| **创建** | 2026-06-21 |
| **关联** | [refactor-plan.md](./refactor-plan.md) P0/P1/P2 · [target-architecture.md](./target-architecture.md) · `spec/runtime/chat-loop.md` |

本文定义 P1/P2 迁移时的**兼容比较口径**。目标不是要求新框架路径逐字节复刻旧实现，而是确保用户可见行为、数据副作用、可观测性和回滚能力在迁移期间可验证。

---

## 1. 适用范围

### 1.1 覆盖路径

| 路径 | 旧实现 | 新实现 | 使用阶段 |
|---|---|---|---|
| Runtime 最小切片 | 无统一旧实现；以 spec + runtime tests 为基线 | `steerable-agent-runtime.ChatLoop` | P1 |
| Cloud chat | `deeppath-api/app/services/harness/loop.py` | `ChatLoop` + DeepPath hooks/plugins | P2 |
| Desktop local chat | `deeppath-agent/src/local-backend/router.ts` | sidecar + Python `ChatLoop` | P3 |
| Web runtime stream | 现有 `/api/v2/*` SSE 消费 | typed SSE + compatibility adapter | P1/P2/P3 |

### 1.2 不覆盖

- 不要求 LLM token 内容逐字一致。模型调用本身非确定，比较重点是结构、工具、状态和错误语义。
- 不要求旧 DeepPath 私有字段进入 Steerable spec。旧字段需要 adapter 映射或继续留业务层。
- 不要求第一阶段删除旧路径。旧路径必须保留到影子运行和灰度门槛通过。

---

## 2. 兼容原则

1. **结构强兼容，文本弱兼容**：SSE event 类型、顺序、tool call/result、trace、DB side effect 必须可比；自然语言内容只做存在性和关键状态校验。
2. **新路径必须可旁路**：任一切片上线前必须有 feature flag / env flag 可切回旧路径。
3. **副作用默认单写**：影子运行阶段不能让旧新路径同时写生产 DB。新路径 side effect 需写 sandbox、dry-run sink，或只记录 intent。
4. **Spec 优先**：跨语言合同以 `spec/` 为准；业务兼容 adapter 只能做过渡，不反向污染 spec。
5. **回滚比删除更早设计**：每个切片先定义回滚条件，再定义删除旧代码条件。

---

## 3. SSE 兼容合同

### 3.1 Canonical event

新路径必须输出 `spec/events/SSEEvent.schema.json` 可校验的 event。

最低字段：

| 字段 | 要求 |
|---|---|
| `type` | 必填；必须来自 schema enum |
| `event` | 可选；用于子类型，如 `agent.event=round_start` |
| `content` | content delta 使用 |
| `message` / `code` | error 使用 |
| `payload` | 结构化扩展；允许业务 adapter 使用 |

### 3.2 Event 顺序

P1 最小 runtime 切片必须满足：

1. `agent` / `loop_start`
2. `agent` / `round_start`
3. 零个或多个 `content`
4. 零个或多个 `tool_call`
5. 零个或多个 `tool_result`
6. `agent` / `round_end`
7. 重复 round，直到完成
8. `done` 或 `error` 或 `budget_exhausted`

P2 DeepPath task 切片允许业务 adapter 插入旧前端需要的兼容 event，但 canonical event 仍必须存在。

### 3.3 允许差异

| 差异 | 是否允许 | 说明 |
|---|---|---|
| content delta 分片边界不同 | 允许 | 只要拼接后语义等价 |
| event `payload` 多出新字段 | 允许 | schema `additionalProperties=true`，但不能破坏旧消费者 |
| `agent` 子事件名称变化 | 限制允许 | 必须在 adapter 中映射，并写入对照表 |
| error code 变化 | 不允许 | 需要先更新错误码映射表 |
| tool_call / tool_result 丢失 | 不允许 | 这是行为回归 |

### 3.4 Legacy adapter

如果旧前端还依赖历史 `data: {...}` 形态，应使用临时 adapter：

- adapter 只存在于 app/web 边界，不进入 `steerable-agent-runtime` loop body。
- adapter 必须从 canonical `SSEEvent` 派生 legacy payload，不能反向让 loop 直接 emit legacy payload。
- adapter 删除条件：所有消费者改读 canonical event，且一轮灰度无 legacy-only 字段依赖。

---

## 4. Trace 兼容合同

### 4.1 最低 trace

新路径每个 run 至少产出：

| Trace 字段 | 要求 |
|---|---|
| `trace_id` | 每次 run 唯一 |
| `session_id` | 与 ChatLoop session 一致 |
| `span` | 至少包含 `loop`、`round`、`llm`、`tool` |
| `event` | 至少包含 `loop.start`、`round.start`、`round.end`、`loop.end` |
| `error` | 失败时必须记录，并与 SSE error code 对齐 |

### 4.2 对比口径

影子运行比较：

- round 数量是否相同或在可解释范围内。
- tool call 名称和参数是否等价。
- tool result 成功/失败状态是否一致。
- final status 是否一致：`completed`、`failed`、`budget_exhausted`、`cancelled`。
- budget / usage 字段是否存在；具体 token 数允许 provider 误差。

### 4.3 敏感信息

trace 不得包含：

- access token / refresh token
- API key
- cookie
- raw payment payload
- 用户上传文件全文，除非明确标记为允许 trace

敏感信息清洗应在 trace adapter 或 hook 内完成，不应写入 loop body。

---

## 5. Auth / Session 兼容合同

### 5.1 AuthPrincipal

框架层只识别最小身份：

| 字段 | 含义 |
|---|---|
| `user_id` | 当前用户 ID |
| `session_id` | agent/chat session ID |
| `tenant_id` | 可选；多租户应用使用 |
| `timezone` | 可选；业务可通过 hook 使用 |
| `scopes` | 可选；工具权限、模型权限、feature key |

框架第一阶段不要求 `User` 模型进入 base。DeepPath 的 NextAuth/JWT/会员字段由业务 adapter 映射到 `AuthPrincipal`。

### 5.2 Session 生命周期

新旧路径必须在以下语义上保持一致：

- 同一个用户请求不能串到其他用户 session。
- cancelled run 不得继续写入 assistant final message。
- retry / reconnect 不得重复执行 destructive tool。
- session lock / concurrency 策略由业务层或 app 层提供；runtime 只暴露足够 hooks 和 IDs。

---

## 6. DB Side Effect 兼容合同

### 6.1 单写规则

影子运行阶段默认：

| 类型 | 规则 |
|---|---|
| Read-only tool | 可旧新同时执行 |
| Idempotent write | 可新路径写 sandbox 或 dry-run sink |
| Destructive write | 只允许旧路径生产写；新路径只记录 proposed intent |
| Proposal / approval flow | 新路径可生成 proposal，但不得自动执行 |

### 6.2 Task 切片比较

P2 `task` 业务切片必须比较：

- 创建/更新/删除 intent 是否一致。
- project/goal/task 关联 ID 是否一致。
- 时间字段时区转换是否一致。
- 排序字段和 parent/child 关系是否一致。
- 权限失败时错误码和用户提示是否一致。
- SSE 中工具执行状态是否一致。

### 6.3 删除旧路径门槛

旧 handler / 旧 loop 只有同时满足以下条件才可删除：

- 影子运行覆盖主要 task 场景。
- 生产灰度期间无 P0/P1 级回归。
- trace / DB diff 无不可解释差异。
- 回滚开关至少保留一个 release。

---

## 7. Sidecar IPC 兼容合同

### 7.1 请求

sidecar JSON-RPC 请求必须包含：

| 字段 | 要求 |
|---|---|
| `id` | 调用唯一 ID |
| `method` | 稳定方法名，如 `chat.stream` |
| `params.session_id` | 必填 |
| `params.messages` | 必填或可由 session 恢复 |
| `params.tools` | 可选；由 tool registry 生成 |
| `params.auth` | 最小 `AuthPrincipal` 或本地权限上下文 |

### 7.2 通知

streaming notification 必须携带 canonical `SSEEvent` 或其无损 JSON 形式。Electron 层可以转换成前端 legacy shape，但转换不应发生在 sidecar loop body 内。

### 7.3 回滚

桌面端必须保留：

- sidecar disabled flag。
- local TS loop fallback 至少一个 release。
- sidecar startup timeout 后的用户可见错误。
- crash 后不会重复执行已提交的 write tool。

---

## 8. 错误码与状态

### 8.1 标准状态

| 状态 | 含义 |
|---|---|
| `completed` | 正常完成 |
| `failed` | 不可恢复失败 |
| `budget_exhausted` | budget / round / time 限制触发 |
| `cancelled` | 用户或上游取消 |

### 8.2 错误码要求

新路径 error 必须至少归类为：

- `provider_error`
- `tool_error`
- `validation_error`
- `permission_denied`
- `budget_exhausted`
- `cancelled`
- `internal_error`

旧路径若使用不同 code，必须在 P2 adapter 里有映射表。

---

## 9. 影子运行记录格式

每次新旧路径对比输出一条记录：

```json
{
  "run_id": "run_xxx",
  "session_id": "sess_xxx",
  "slice": "runtime-minimal | deeppath-task | desktop-sidecar",
  "old_path": "deeppath-api.loop",
  "new_path": "steerable-runtime.chat_loop",
  "comparison": {
    "sse": "pass | warn | fail",
    "trace": "pass | warn | fail",
    "db_side_effect": "pass | warn | fail",
    "tool_calls": "pass | warn | fail",
    "final_status": "pass | fail"
  },
  "diffs": [],
  "decision": "eligible_for_gray | keep_shadowing | rollback"
}
```

---

## 10. P0 完成条件

- [x] 本合同被 `refactor-plan.md` P0.5 引用。
- [x] `spec/runtime/chat-loop.md` 的状态与当前 runtime 雏形一致。
- [x] ADR-004/005/006 已 Accepted。
- [x] P1 最小 runtime 切片知道要产出哪些 SSE / trace 字段。
- [x] P2 DeepPath task 切片知道如何做新旧路径对比。

