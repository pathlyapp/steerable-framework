# CoreLoop 下沉 · agent 先行改造清单

> 目标：把 deeppath-agent 的单 agent 循环下沉为 steerable-agent-runtime 的
> CoreLoop（Python），由 sidecar 托管，Electron 通过反向通道回调执行工具，
> 最终删掉 deeppath-agent/src/harness。deeppath-api 采纳排到最后，可选。
>
> 决策背景：见 2026-08-25 的边界评审与迁移计划（两份 canvas）。
> 关键前提：CoreLoop 用 Python 写进 agent-runtime，不是 TypeScript；
> LoopEvent 接口按 deeppath-api 的需求设计，实现按 deeppath-agent 验证。

---

## 两个必须避开的陷阱

- [ ] **不用 TypeScript 写 CoreLoop** —— 框架 Tier2/Tier3 是 Python-only，
      TS 侧只是一致性测试面。写成 TS 就是第四份实现，api 永远接不进来。
- [ ] **LoopEvent 分类不按 agent 的 8 种 SSE 形状设计** —— 按 api 约 100 个
      发射点归纳出的分类设计，否则 api 到时候接不进来。

## 硬阻塞（A3 之前必须解决）

- [x] **sidecar 反向通道**：✅ A1 已解决（2026-08-25）。sidecar 可发起
      `srv_` 前缀的反向请求并等响应，host supervisor 服务之。见 A1。
- [ ] **包体**：sidecar 实测 700–740MB（CI 预算 780MB），320MB 目标未达成。
      开发/canary 可用系统 python 绕过，正式发版前必须解决。

---

## A0 · 盘点 api，产出港口规格 ✅ 已完成（2026-08-25）

不改 deeppath-api 一行代码，只读。已交付：

- [x] **港口规格**：`docs/spec/core-loop.md` —— LoopEvent 五类分类
      （生命周期 / 内容流 / 工具侧 / 证据 / 预算控制）、ToolExecutor 端口
      （10 个 if/elif 分支 → 注册式处理器映射表）、产品侧 hook 清单、
      待统一的语义分歧。已上线 https://steerableframework.com/spec/core-loop/
- [x] **一致性用例**：`tests/conformance/cases/{policy,budget,completion}/`
      + 两侧 runner（`test_{policy,budget,completion}_conformance.py` /
      `{policy,budget,completion}.test.ts`）。Py 4 + TS 35 全绿，
      已进 CI（commit `30343ac`，CI run success）。
- [x] **agent 死代码清理**：删 `extractIdempotencyKey` / `stripHarnessMetadata`
      （生产零引用），agent `9888ab3`，harness 测试 35 通过。

**勘察发现（影响后续阶段）**：
- api 的 loop 直接 `yield` SSE 字节，约 100 个发射点，无内部事件抽象——
  A5 的最大工作量在这。
- `_run_tool_calls`（loop.py 4805–5554）是 10 分支 if/elif，横切关注点
  （去重/策略/预算）与产品执行混在一起。
- orchestrator.py 已把 HarnessLoop 当黑盒 worker 用——编排与单步的缝已存在。
- **agent 仓库没有跑测试的 CI**（只有 build-windows + pages）。A1 会同时动
  framework + agent，建议先给 agent 补一个跑 vitest 的 workflow。
- api 的 `trajectory_eval`（离线 JSONL 评分）与 `replay`（live stageData
  压缩轨迹）是两套系统；录制已在生产路径，但「导出→评分→回归」未产品化。

## A1 · sidecar 反向通道 ✅ 已完成（2026-08-25）

涉及 steerable-framework + deeppath-agent，与 api 无关。

- [x] **前置**：给 deeppath-agent 补一个跑 `vitest` 的 CI workflow —— ✅
      已补（2026-08-25，commit `5aa0b73`）。`.github/workflows/test.yml`：
      push 到 develop/main 或 PR 时触发，checkout 私有 framework 到同级目录
      （`@steerable/*` 是 `link:../steerable-framework/...`），build
      agent-protocol + agent-harness 出 dist，再 `pnpm test`。首跑
      run `32835973548` success，284 过 / 7 skipped，与本地一致。
      （supervisor 子进程集成测试仍 opt-in，CI 不启用。）
- [x] 扩展 `spec/sidecar/README.md`：新增「Reverse channel」一节 —— 帧判别
      （id+method=请求 / id 无 method=响应 / 无 id=通知）、id 命名空间约定
      （host 用整数、sidecar 用 `srv_` 前缀字符串防碰撞）、保留反向方法
      `tool.invoke`、以及「读循环等待响应时不得阻塞否则双端死锁」的约束。
- [x] 改 `transport/stdio_jsonrpc.py`：`JsonRpcServer.call()` 发起反向请求
      （`srv_` id + pending future 表），`attach_writer()` 绑定出站 writer，
      `_resolve_reverse_response()` 识别响应帧并 resolve；`serve_stdio` 与
      sidecar 的 `serve()` 读循环改为逐帧并发 task 分发（关键：handler 等待
      反向响应时读循环继续服务，否则死锁）。
- [x] 改 `deeppath-agent/src/sidecar/supervisor.ts`：`handleStdoutLine` 区分
      sidecar 请求（id+method）与响应（仅 id）；新增 `onReverseRequest()`
      注册 host 侧 handler，handler 返回值写回 sidecar stdin 作为 JSON-RPC
      result。配套类型 `SidecarReverseRequest` / `SidecarReverseHandler`。
- [x] `ToolRouter.register_remote()` 注册远程代理工具（dispatch 转发给异步
      invoker，如反向通道）；`_invoke()` 现在把完整参数集传给 `**kwargs`
      handler。metadata 标记 `remote: true`。
- [x] 端到端：Python 侧 `test_sidecar_reverse_channel.py`（真实子进程 stdio
      上 sidecar 反向 `tool.invoke` → host 执行 → 结果回 sidecar）+
      agent 侧 supervisor 集成测试新增反向通道用例（spawn framework 的
      `reverse_echo_sidecar`，host handler 回 `host-ran:pwd`）。

**测试**：framework py 116 过（+8）、framework TS 187 过、agent 284 过
（含 3 个 supervisor 集成测试）。
**线上回测**：framework CI run `32835311377` success（commit `1f1b7e1`）；
agent commit `7a960ab`（无测试 CI，见上前置项）。
**回滚**：新增协议方法 + 新注册点，旧路径不动，不启用即可。

## A2 · agent 轨迹录制与回放 ✅ 已完成（2026-08-25）

api 有 trajectory_eval.py + replay.py，agent 没有。这层安全网必须先建，
否则 A3 的 Python 重写没法证明行为没变。

**勘察关键结论**：api 是**两套并行契约**——compact trajectory
（`step_decision` 事件 + `reduce_execution_state` 还原执行状态机）和
full trace（`tool.call` 等事件供 trajectory_eval 离线评分）。本次只做
**compact 层**回放（TODO 原定范围），full trace 改为不截断落库做保真存档。

- [x] **录制**：router.ts 在两处 `decideCompletion` 调用点构造 `step_decision`
      事件（step 摘要含 `round`/`traceStepId`/`finishReason`/`toolCalls`/
      `toolCallCount`/`toolErrorCount`/`textLength`，对齐 api 契约），累积后
      落进 `harness_traces.payload.trajectory`（cap 最近 100 条，对齐 api
      `_MAX_TRAJECTORY_EVENTS`）。
- [x] **逐事件回放**：新建 `src/harness/replay.ts`，TS 移植 api 的
      `execution_state.py` + `replay.py`——`HarnessExecutionState` /
      `HarnessTrajectoryEvent` / `reduceExecutionState()`。契约对齐：step 按
      `(round, traceStepId)` 去重、只有 6 个白名单 status 驱动状态机、
      budgets 从 steps 派生、未知事件类型静默跳过、输入不被 mutate。
- [x] **full trace 保真**：`toMetadata({ full: true })` 落库不截断（原截断
      50 条事件）；消息 metadata 仍用截断版保持紧凑。

**决策记录**（2026-08-25 与用户确认）：
- compact trajectory 存 `harness_traces.payload`（agent 无 stageData），不新建表。
- full trace 不截断落库（full-fidelity），用于后续回归存档。
- 只做 compact 层回放，不做 full trace 评分层。

**测试**：`tests/harness/replay.test.ts`（replay round-trip / 去重 / status
白名单 / 静默跳过）+ tracing 的 full/uncapped 测试。本地 294 过。
**线上回测**：agent CI run `32837333624` success，295 过 / 7 skipped
（commit `81d72be`）。
**遗留**：「存档 ≥20 条真实桌面轨迹」需在真实桌面使用中积累，无法靠 CI
凭空产生——能力已就绪（每次 loop 自动落 trajectory + full trace），存档
随使用自然增长。后续可补一个「导出轨迹 → 回放比对」的回归脚本。
**备注**：这套录制回放能力本身以后该进框架。

## A3 · CoreLoop v0（Python，4–6 周，最大工作量）

在 `packages/agent-runtime/py` 里实现。这是跨语言重写，不是原地重构。

### Slice 1 · 最小循环 ✅ 已完成（2026-08-25，commit `dba343b`）

- [x] 内外层循环状态机与轮次控制（`CoreLoop.run` 内层工具轮 + completion 决策）
- [x] LLM 流消费（经 `LLMProvider.stream`，content/reasoning/tool_call delta）
- [x] 预算计数（token 预算经 `consume_budget`，连续工具错误熔断）
- [x] `LoopEvent` 五类结构化事件（lifecycle/content/tool/budget/completion），
      不编码字节，TransportAdapter 负责 wire 编码
- [x] `ToolExecutor` port + `RouterToolExecutor` 默认实现；去重/策略/预算
      留在 loop 内，产品注入 handler
- [x] `max_tool_errors` 采**连续**语义（成功清零），落地 A0 记录的分歧决定
- [x] **回放一致性**（A3 通过标准的核心）：`replay.py` 移植 api 的
      execution_state + replay 契约进框架；CoreLoop 每轮录 `step_decision`，
      `test_loop_replay.py` 验证回放还原的终态与 loop 实际终态一致

**测试**：`test_loop.py` 6 + `test_loop_replay.py` 4，全量 py 126 过。
**线上回测**：framework CI run `32838361130` success（pytest + codegen
idempotency + check_drift 全过；sidecar 包体 741MB/800MB，A4 门禁预警）。

### Slice 2 · 伪函数调用 / markdown 工具调用恢复 ✅ 已完成（2026-08-25，commit `59c3959`）

- [x] `pseudo.py`：`extract_inline_tool_calls()` 识别三族伪调用——
      MiniMax XML（`<invoke name=...>`）、DeepSeek XML（`<function=...>`）、
      Markdown pseudo（`[Tool call: NAME]\n{json}`，含平衡括号 JSON 扫描）
- [x] **恢复执行**（不是只剥离）：CoreLoop 在一轮无标准 tool_calls 时，
      从 content 提取伪调用还原为真实 `ToolCall` 走 act 段——
      这是离线 Ollama 等本地模型跑通的关键（A4 前置）
- [x] 设计决策：采「恢复执行」而非 api 生产现状的「流式剥离净化显示」。
      api 的 `_extract_inline_tool_calls` 本是死代码（只剥不显恢复）；
      剥离会丢弃模型意图导致 loop 误判收尾。流式剥离净化显示留后续切片。
- [x] 真实 tool_calls 存在时不触发恢复（防双重执行）

**测试**：`test_pseudo.py` 11 条（三族提取 + 嵌套/残缺 JSON + 接入 loop
端到端 + 真实调用不重复 + 纯文本不恢复），全量 py 74 过。
**线上回测**：framework CI run `32839498768` success（py/ts/lockstep/
sidecar-budget/examples 全过）。

### 剩余切片（未做）

> 2026-08-25 复审（steerable vs codex vs dsh，见 canvas
> `steerable-coreloop-vs-codex-dsh`）后重排优先级。原顺序按「实现依赖」排，
> 复审发现应按「阻塞 A4 的程度」排，且有两个**架构形状问题**比补功能更该先做。
> 复审核心结论：
> - **最大风险是 CoreLoop 零生产验证**：sidecar 的 `agent.chat.stream` 直接调
>   `provider.stream()` 绕过 CoreLoop，agent 仍跑自己的 TS 循环。测试全绿但
>   没有一行真实流量。每加一个切片都在累积未验证的风险。
> - **伪调用恢复（Slice 2）是唯一明确领先项**：codex / dsh 都没有（都假设
>   上游返回结构化 tool_calls；dsh 甚至不解析 DeepSeek 模型自己常输出的
>   `<function=name>` 格式，会落进 text block 永不执行）。
> - 规模现实：CoreLoop ~3K 行 Python vs codex 1.49M 行 Rust / dsh 468K 行 TS。
>   差距是取舍不是落后，只补「让 A4 跑不起来」的那些。

#### 架构形状问题（比补功能更该先做，越晚越贵）

- [x] **loop hook 扩展点** ✅ 已完成（2026-08-25，commit `318d5e6`）。
      新增 `hooks.py`：`LoopHooks` 协议三点——`pre_step`（压缩改写 transcript /
      拒绝本轮）、`post_tool_result`（外置改写超大结果）、`on_request_error`
      （重试 vs 失败）。默认 `NoopHooks` 全透传，行为不变。LLM 流包进
      `on_request_error` 驱动的重试循环；此前定义了从不 emit 的 `error` 事件
      现在在流终态失败时触发。后续切片 = 写 hooks 实现，不再动 `loop.py`。
- [x] **统一状态来源（轻量版）** ✅ 已完成（同上 commit）。采「单一写入路径」：
      completion 事件携带完整 step 摘要（round/finishReason/toolCalls/
      textLength/...），trajectory 在 `emit_completion` 内从事件派生，删掉独立
      `record()` 双写。事件流自此可独自重建轨迹——模型看到的与回放看到的不可能
      再漂移。完整 event-sourcing（dsh 式 SessionEvent 日志 + deriveMessages）
      留到有会话持久化/重载需求时（A4+）再评估。

**测试**：`test_hooks.py` 7 条（hooks 触发 / reject / 改写 / 重试 / 失败 /
NoopHooks 行为不变 / trajectory 与事件流一致），全量 py 81 过。
**线上回测**：framework CI run `32856885116` success（py/ts/lockstep/
sidecar-budget/examples 全过）。

#### Tier 1 · 阻塞 A4 桌面切换 ✅ 已完成（2026-08-25，commit `a62ebb4`）

三片全部落地为 hooks 实现（`loop.py` 零改动，验证了 hook 设计）：

- [x] **大结果外置 / 截断**：`spill.py`——`SpillHooks`（`post_tool_result`
      消费者）：结果序列化超 `max_inline_bytes` 写入 `SpillStore`
      （文件系统 / 内存两实现），transcript 里替换为首尾 preview + locator。
      几 MB 本地 shell 输出不再爆 60k 上下文。参照 dsh spill-policy。
- [x] **上下文压缩**：`compaction.py`——`CompactionHooks`（`pre_step`
      消费者）：chars/4 估 token，超 `threshold_ratio * max_context_tokens`
      先折叠旧 tool 结果为占位符，仍超则摘要中段（可选 LLM summarizer，
      无则确定性摘录兜底）。system + 首条 user（目标）永远保留。
      参照 dsh compaction-basic 阈值+保留比。
- [x] **shell 安全规则接线**：agent 61 条规则回流框架为唯一事实源——
      新增 Python 双生 `agent-harness/safety.py`，TS 侧从 6 条存根补齐到
      61 条。双端锁步由新一致性用例保证（22 条命令，两端 runner 全绿）。
      接线进 `ToolRouter`：工具声明 `metadata["shell_command_param"]` 即
      在 dispatch 前分类——critical 经 `PolicyDeniedError` 拦截，warning
      标注进 result.data。

**测试**：runtime py 99（+18：spill 3 / compaction 5 / safety gate 10），
harness py 41，一致性 py 5 / ts 36（+1 safety 用例）。ruff 全过。
**线上回测**：framework CI run `32858399070` success（py/ts/lockstep/
sidecar-budget/examples 全过）。

#### Tier 2 · 架构级，越早越便宜

- [ ] **CoreLoop 接真实流量**：让 sidecar 的 `agent.chat.stream` 改走 CoreLoop
      （flag 控制），最小成本验证。这是复审认定的最高优先项。
- [ ] **重试接入 loop**：harness 里 `next_retry_delay_ms` 写好了却没人调用。
      本地模型比云端更容易抖，白捡的健壮性不该空着。参照 dsh 每次 retry 开
      新 turn 的做法。
- [ ] **软超时**（原「软超时、压缩续跑、轮次扩展」拆分；压缩续跑见 Tier 1
      上下文压缩，轮次扩展暂缓——codex/dsh 核心 loop 都无硬上限，靠 token/
      compaction 驱动，steerable 的 max_rounds=32 已是更保守的选择）。

#### Tier 3 · 补齐，不急

- [ ] LLM 流消费的 UTF-16 代理对修复、推理内容提取（真实 bug 源但不阻塞）
- [ ] 伪调用的**流式剥离**（净化前端显示，对齐 api 生产路径）
- [ ] 工具去重、未知工具建议、参数 schema 强制转换
- [ ] **审批 / 沙箱**：桌面产品最终需要，可先留产品侧。框架里
      `waiting_approval` 状态枚举目前没人 emit，属半成品，要么接通要么删掉。
- [ ] **trace 接入 loop + OTel**：StorageAdapter 的 trace API 有了但 loop 不写。
      可观测是 codex/dsh 都远超的一块，但不阻塞功能。
- [ ] **补齐 LoopEvent**：`stage_complete` 和 `error` 两类定义了从不 emit，
      类型与实现不符，顺手补上。
- [ ] **MCP**：agent 侧已有 MCP client，框架侧没有。等 A4 之后按需下沉。
- [ ] **agent 独有的反幻觉层**（这是 api 缺的，是净增益，放最后一片）：
      data-need 路由、grounding 判定、deferred/claimed 重试、narration round

#### 明确不建议做（复审结论）

- **不要追 codex 的规模和安全栈**：149 万行 Rust + Guardian/execpolicy/多平台
  沙箱是 OpenAI 的产品投入，桌面端用不到那么重。steerable 的价值恰恰是
  薄核心 + 跨语言契约。
- **不要在 TS 侧再写一份 CoreLoop**：规格已禁止，继续守住——两份 loop 必然
  漂移，这正是当初 api 和 agent 两套 harness 的教训。
- **包体门禁仍是 A4 独立硬阻塞**：741MB/800MB，目标 320MB 未达成。与循环
  能力无关但会卡住桌面发布，需并行推进。

留在产品侧的（不下沉）：dp-action 提案、UI 工具、response 标签契约、
context_system 分层、目标校验器、技能预算、计费、时区、实体查库、桌面中继、
编排/群聊/协作。

**推进方式**：按切片，反幻觉层放最后一片；之前 sidecar 路径只在 canary 开启。
**通过标准**：回放 A2 的轨迹，Python CoreLoop 的决策序列与 TS 版逐事件一致
（差异需逐条可解释）。Slice 1 已在 Python 侧建立回放自洽性；与 TS 版的
**跨语言**逐事件比对待后续切片补（需要 TS reduce 与 Python reduce 对同一
轨迹跑分）。
**回滚**：CoreLoop 只在 flag 下启用，默认仍走 TS 路径。

## A4 · desktop 切换并删码（2–3 周）

- [ ] sidecar 托管 CoreLoop，工具走 A1 的反向通道回 Electron 执行
- [ ] 灰度通过后删 `deeppath-agent/src/harness`
- [ ] 把 61 条 shell 安全规则回流到框架（框架现在只有 6 条存根）
- [ ] 解决包体门禁（见上方硬阻塞）

**通过标准**：sidecar 模式下离线 Ollama + 本地 shell/文件/MCP 工具全部跑通；
包体过门禁。
**回滚**：关掉 STEERABLE_USE_SIDECAR 即回落本地 TS 路径。

## A5 · api 采纳（可选，以后）

- [ ] api 侧最大的活：把约 100 个 SSE 发射点改成结构化事件
- [ ] 此时 CoreLoop 已被真实产品验证，且带着 api 缺失的反幻觉能力

---

## 中途停靠点

只做 A0 + A1 + A2（约三周）也成立。拿到：反向通道、回放靶场、
基于 api 真实需求的港口规格。三样东西无论后面怎么选都不浪费，
到那再决定是否投入 A3 的重写。

## 已发生的漂移 · 处置决定（2026-08-25）

- [x] **跨语言一致性用例** —— 已补 policy / budget / completion（见 A0）。
- [~] **`maxToolErrors`：推迟到 CoreLoop 统一，A0 不改代码。**
      勘察发现两端语义根本不同，不能对齐成一个数：
      api 是**累计**工具错误（完成判定硬编码 2，预算断路器默认 3，
      api 内部自己都不一致）；agent 是**连续**失败、成功即清零（阈值 3，
      故意如此——累计制会惩罚「失败后换路径最终成功」的任务）。
      CoreLoop 需选定一种语义（建议连续）并做成可配置。
      已记录进 `docs/spec/core-loop.md`。
- [~] **token 预算：保持不同，只记录。** api 120k（服务端大上下文）、
      agent 60k（本地小上下文）是刻意的，做成可配置即可，不强求一个数。
      已记录进规格。
- [ ] **死桥接函数**：`consume_framework_budget` 等在 api 侧（定义了没人调）。
      因「不动 api」，本次未处理，留待 A5。
      agent 侧的死导出已在 A0 清掉。
