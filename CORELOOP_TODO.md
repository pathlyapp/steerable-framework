# CoreLoop 下沉 · agent 先行改造清单

> 目标：把 deeppath-agent 的单 agent 循环下沉为 steerable-agent-runtime 的
> CoreLoop（Python），由 sidecar 托管，Electron 通过反向通道回调执行工具。
> ✅ 终态达成（2026-08-26）：deeppath-agent `src/harness` 与 TS 循环已删除
> （agent `298eb82`，净 -3218 行），CoreLoop 是唯一聊天路径。
> deeppath-api 采纳排到最后，可选。
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
- [x] **包体**：✅ 2026-08-26 解决。根因不是依赖（pydantic+httpx 很轻），
      是 python-build-standalone 的 `install_only` 变体自带调试符号/工具链
      产物（Linux x64 解包 ~700MB）。切换到 `install_only_stripped`
      （`build_sidecar.py` 的 `ARCHIVE_VARIANT`），darwin-arm64 实测
      94.7MB；CI 预算回落到设计目标：ci.yml 800→320MB、
      sidecar-build.yml 780→320MB。构建产物冒烟通过（boot + ping +
      干净退出）。

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

#### Tier 2 · 架构级，越早越便宜 ✅ 已完成（2026-08-25，commit `df668d8` + `e041281`）

- [x] **CoreLoop 接真实流量**：sidecar `agent.chat.stream` 新增 CoreLoop 路径——
      请求级 `useCoreLoop` 优先，否则 `STEERABLE_SIDECAR_CORELOOP` 环境变量，
      默认仍走旧的直连流式路径。LoopEvent 映射到现有 wire 面（stream.chunk /
      stream.done / stream.error），宿主无需改协议即可接入。LoopConfig 从
      params 派生（maxRounds/maxToolErrors/budgetTokens/softTimeoutMs/...），
      默认 hooks 为 RetryHooks；嵌入方经 `loop_hooks_factory` 自组合
      （新增 `ChainHooks` 支持多 hooks 叠加）。
- [x] **重试接入 loop**：`retry.py`——`RetryHooks`（`on_request_error` 消费者）
      把 harness 的 `next_retry_delay_ms` 接进 loop。每轮独立重试预算
      （轮次推进计数器重置），有界指数退避，可插拔 retryable 判定。
- [x] **软超时**：`LoopConfig.soft_timeout_ms`，只在轮次边界检查（绝不打断
      在飞的流/工具）。过线后 emit `soft_timeout` 事件、收起工具描述符、
      追加收尾通知，模型再有工具意图也丢弃——以最终回答优雅收尾而非硬杀。

**测试**：runtime py 107（+8：retry 4 / soft timeout 4），sidecar py 24
（+6 CoreLoop 路径），harness py 41，一致性 py 5 / ts 36。ruff 触及文件全净。
**踩坑**：CI 从仓库根跑 pytest，新增 `test_retry.py` 与 harness 同名文件
撞模块名（无 `__init__.py`），导致 golden fixture 错位——改名
`test_retry_hooks.py` 修复（`e041281`）。
**线上回测**：framework CI run `32860387055` success（py/ts/lockstep/
sidecar-budget/examples 全过）。

#### Tier 3 · 补齐，不急 ✅ 大部分完成（2026-08-25，commit `bdfea84`）

- [x] **UTF-16 代理对修复**：`split_trailing_high_surrogate`——chunk 末尾的
      高位代理留进 carry，下一 chunk 拼回完整字符；流尾残余照常释放。
      **推理内容提取**：框架侧已由 provider adapter 产出 `reasoning_delta`
      事件覆盖；api 的 `<think title="… · 推理">` 包装与落库正则是产品
      展示格式，留产品侧不下沉。
- [x] **伪调用流式剥离**：`PseudoStreamStripper`（`pseudo.py`）——移植 api
      生产路径的双状态机（`<function_results>` 族回显块 + `[Tool call:]`
      markdown 块），跨 chunk 吞块、硬上限防永吞（32k / 4096）。原始文本
      仍进 recovery 与 transcript，剥离只作用于 `content_delta` 显示流；
      `strip_pseudo_fn_final` 在入 transcript 前兜底。顺带修了重试时
      部分内容重复进 transcript 的问题（重试前清空当轮 partials）。
- [x] **工具卫生三件套**：① 同 turn `(name, argsHash)` 去重——重复调用
      不执行，回 `duplicate_call` 软信号并计入连续错误断路器
      （`LoopConfig.tool_dedup` 可关）；② 未知工具 difflib 建议
      （cutoff=0.4，`did you mean`）；③ 参数按 schema 原始类型强制转换
      （string/integer/number）。②③ 在 ToolRouter，① 在 CoreLoop。
- [x] **审批 / 沙箱（决策记录）**：`waiting_approval` 枚举**保留**——
      api 的 dp-action 提案轨迹回放需要它正确 reduce；CoreLoop **不 emit**
      （审批等待是产品侧概念，桌面端刻意全允许；单工具同意由
      ToolRouter `require_consent` 同步强制，不做挂起态）。沙箱不下沉。
- [x] **trace 接入 loop**：`TraceRecorder`（`tracing.py`）——以消费者身份
      tee loop 事件流进 StorageAdapter：每工具调用一个 span、每事件一条
      TraceEvent、终态 upsert HarnessTrace（payload 截断）。loop 本身保持
      无存储依赖。OTel 导出器已补（2026-08-26，`otel.py`，见 P2）。
- [x] **补齐 LoopEvent**：`stage_complete` 现于每个工具轮次后 emit
      （round/toolCallCount/consecutiveToolErrors/elapsedMs）；
      `tool_call_result` 失败时带 `error`。`error` 事件自 hooks 切片已 emit。
- [ ] **MCP**：agent 侧已有 MCP client，框架侧没有。等 A4 之后按需下沉。
- [x] **agent 独有的反幻觉层**（这是 api 缺的，是净增益，放最后一片）：
      data-need 路由、grounding 判定、deferred/claimed 重试、narration round
      ✅ 2026-08-27 核实**已在 hooks 切片全量落地**（`antihallucination.py`，
      复选时逐项确认）：四能力齐备——data-need 路由（pre_step 首轮分类 +
      `tool_choice="required"`，分类失败保守落 require_tool）、deferred/
      claimed 纪律重试（检测正则 1:1 移植 TS，含 conditional-offer 豁免）、
      grounding 判定（零成功工具调用 + ≥2 处数字才触发语义判定，fail-open）、
      narration（上限 2 次）。sidecar 经 `antiHallucination: true` 接线
      （agent `coreloop-stream.ts` 已传），plan 模式自动全豁免。测试 ×12
      （路由 3 / deferred / claimed / grounding 2 / narration 3 / 重试
      预算 / plan 豁免）。本项只是补勾，无新增代码。

**测试**：runtime py 132（+18：hygiene 7 / stream strip 14 / tracing 3，
含既有用例适配去重守卫），sidecar py 24，harness py 41，一致性 py 5 /
ts 36。ruff 触及文件全净（注意：`ruff --fix` 整个包会顺手改未触及文件，
已回滚，只保留触及文件的修复）。
**踩坑**：又一次根目录 pytest 同名撞车——`test_tracing.py` 与 harness 的
tracing golden 测试撞模块名，改名 `test_trace_recorder.py` 修复
（`deafc1d`）。runtime 新增测试文件前先对照 harness 既有文件名。
**线上回测**：framework CI run `32863602178` success。

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

#### 第二轮复审后的优先级（2026-08-25，canvas `steerable-vs-codex-dsh-r2`）

Tier 1–3 落地后重排。上次 6 个红项 4 个转绿、2 个转橙（审批=决策记录、
trace=已落库无 OTel）；**头号风险不变：零生产验证**。
（2026-08-26 更新：P0 包体、P1 回放比对 + 反幻觉层、P2 三项全部转绿，
详见下表逐项标注；P3 按约定留待 A4 稳定后确认。）

- **P0 · 阻塞桌面发布**
  - [~] **A4 切换**（见下节）——Slice 1 已接线（2026-08-25）：agent 端
        `STEERABLE_USE_CORELOOP=1` 整轮走 sidecar CoreLoop，工具经反向通道
        回 Electron，SSE 契约不变。剩下的就是真实模型灰度——所有能力仍只有
        测试验证，灰度是唯一的真实校验（包括 61 条安全规则在真实命令分布
        上的误伤率）。
  - [x] **包体门禁 741MB → 320MB**（2026-08-25）——实测 558→310MB（mac
        unpacked）：`electron-builder.js` 去掉平台级 `files`（v25 会用它
        覆盖全局集导致整仓入包），全局 files 加 `!cflog/**`、`!src/**` 等
        否定模式，剪掉 better-sqlite3 编译残留；`scripts/check-bundle-size.mjs`
        门禁挂入 build-windows.yml（预算 420MB 待首测收紧）。
- **P1 · 校验闭环 + 净增益**
  - [x] **跨语言回放逐事件比对**（2026-08-25）——共享 JSON fixtures
        （`packages/agent-runtime/py/tests/fixtures/replay/`），Py
        `test_replay_crosslang.py` 与 agent `replay-crosslang.test.ts`
        对同一轨迹逐事件 + 终态比对；修掉 dedupe 键类型漂移分歧
        （Py 端字符串化对齐 TS）。
  - [x] **反幻觉层**（2026-08-25）——四机制全部下沉为 hooks：
        data-need 路由（`pre_step` 设 `tool_choice=required`）、
        deferred/claimed 纪律重试 + grounding 判定（新 `before_completion`
        钩子，retry/narrate/accept 三态）、narration round。loop 改
        `while True` 支持弹性轮次 + redo 预算。sidecar 按
        `antiHallucination` 请求参数接线，agent `coreloop-stream.ts`
        默认开启。py 186 测全绿。
- **P2 · 体验与健壮性**
  - [x] **会话恢复 / 续跑**（2026-08-26）——`resume.py`：
        `project_transcript(events)` 纯函数从 TraceEvents 重建
        `list[LLMMessage]`（content_delta 拼接 + tool_call_start 全参数 +
        tool_call_result 结果），`load_transcript(storage, trace_id)` 直接
        从存储加载。新增 `LoopConfig.persist_tool_results`（默认关，开则
        事件带全量结果而非 300 字符预览，供续跑保真）。
  - [x] **token 估算精度**（2026-08-26）——`tokens.py`：CJK 感知启发式
        （CJK 0.6/字符、其他 0.25/字符、每消息 +8，与 agent TS 版同系数
        同区间），`MODEL_TOKEN_FACTORS` 按模型名前缀校准系数（最长前缀
        优先），`register_model_factor` 运行时扩展；compaction 改走新
        估算器并支持 `model=` 参数。
  - [x] **OTel 导出器**（2026-08-26）——`otel.py`：stdlib-only，
        `to_otlp_json(trace, spans, events)` 生成 OTLP/HTTP JSON
        （trace→root span、tool span→子 span、events→root span events，
        ID 确定性编码保证重导幂等），`export_otlp_http` POST 到
        collector `/v1/traces`。无 opentelemetry-sdk 依赖。
- **P3 · 生态**
  - [ ] **MCP 下沉**：agent 侧已有 client，A4 稳定后按需，不提前做。

#### 第三轮复审后的优先级（2026-08-26，canvas `steerable-vs-codex-dsh-r3`）

P0–P2 落地后三仓代码实勘重排：13 轴对比 **3 领先（反幻觉层、跨语言契约、
CJK token 估算）、3 追平、7 落后**。领先项全部只有测试证据——**头号风险
连续三轮未变：零生产验证**。新优先级如下（与上方 r2 表并存，r2 表保留作
历史记录）：

- **P0 · 唯一硬阻塞**
  - [~] **A4 真实灰度（桌面端实测）**：开关已就绪
        （`STEERABLE_USE_SIDECAR=1` + `STEERABLE_USE_CORELOOP=1`，Ollama
        cloud 模型已配）。先自用 dogfood 一周，落 trace 后用回放比对与
        TS 路径对账；重点观测 61 条安全规则误伤率、反幻觉四机制的真实
        触发率/误判率、host 工具反向通道延迟。
        - **桌面金丝雀已绿**（2026-08-26）：`scripts/desktop-canary.mjs`
          经 CDP 驱动真实 Electron（preload bridge → IPC → router →
          coreloop-stream → sidecar → CoreLoop → Ollama cloud → 反向通道
          → 真实 tool router → SSE 回 renderer → trace 落库），8/8 通过，
          答案含 canary token（grounding 实证）。启动方式：
          `STEERABLE_USE_SIDECAR=1 STEERABLE_USE_CORELOOP=1
          STEERABLE_SIDECAR_PYTHON=<fw>/.venv/bin/python3
          DEEPPATH_FORCE_BUNDLED_WEB=1 npx electron .
          --remote-debugging-port=9222`，然后 `node
          scripts/desktop-canary.mjs`。
        - **灰度修掉的三个真实 bug**（测试全绿但生产才暴露）：
          ① sidecar 把 ollama 映射到 OpenAI-compat 时不补 `/v1`，桌面存的
          原生 daemon 根地址导致全量 404（factory 归一化 + 回归测试）；
          ② CoreLoop 路径 trace 只进 sidecar 内存、桌面 harness_traces
          零落库——dogfood 无数据可挖（router 现在经 `trace.fetch` 回拉
          并存库，stream.done 已带 traceId）；③ 失败回合恰好不留 trace
          （stream.error 缺 traceId）——已补，失败 trace 也落库。
        - **观测闭环**：loop 新增 `hook_action` 事件（pre_step compact /
          tool_choice、on_request_error retry、before_completion retry /
          narrate 全在决策点 emit，TraceRecorder 自动落库）；
          `scripts/trace-report.mjs` 直读桌面 sqlite 输出三项观测
          （安全拦截清单待人工标注 / hook 触发分布 / 反向通道延迟
          p50/p95）。首轮实测：2 trace 全 completed，hook 触发
          compact×5 + tool_choice×1，反向通道 p50=121ms（pty  spawn 占
          大头，max 21s 是 terminal-exec 轮询开销），安全规则 0 拦截，
          dedup 真实拦下 2 次重复调用。
        - **首日 dogfood（2026-08-26 18:00 快照，app 运行 ~4h 后正常
          退出）**：5 trace 全 completed，hook 触发 compact×22 +
          tool_choice×3 + narrate×1，dedup 拦截 7 次，安全规则 0 拦截。
          compact 22 次/5 trace 当时判为病理值 → 催生 R4 的 P1「压缩提频
          治理」（见下节）。**事后还原（P1 修复时发现）：该读数约一半是
          测量假象**——loop 的 compact 事件判据 `is not None` 在
          ChainHooks 下每轮必发伪事件，真实压缩远少于此；判据已改恒等
          比较。本次会话早于校准闭环提交，
          token-calibration.json 自下次 app 启动开始积累。
        - **剩余**：用户自用 dogfood 一周（手动操作，无法代办）；一周后
          跑 `trace-report.mjs` + 回放比对对账，按误伤率/误判率决定是否
          默认开 `STEERABLE_USE_CORELOOP`。
        - **线上数据实测（2026-08-26，经 devops SSH 隧道只读访问生产
          MySQL，纯聚合查询、零内容出库）**：`harness_trace` 31,350 条 +
          `harness_trace_event` 164 万条。结论分两类——
          - **能校准的（LoopConfig 默认值有了真实分布依据）**：
            轮次分布 p95≈8、max=24+ → `maxRounds=32` 覆盖 99.9%，
            默认值验证通过；完成率 97.2%（30,486/31,350），
            completed 中 3.8% 带工具错误但恢复 → `maxToolErrors=3`
            的 breaker 设计合理；工具延迟 <100ms 48% / 100ms-1s 36% /
            >30s 4.6%（云端工具长尾）；**budget_exhausted 占终态 6%
            （1,862 条）→ 预算默认值偏紧，按模型 token 分布定**；
            每 trace 平均 14.5 万 totalTokens（default 模型）→ 桌面
            60k 压缩阈值相对真实用量偏激进（金丝雀单轮就压缩 5 次
            与此吻合），dogfood 期间重点看 compact 触发频率。
            **反幻觉价值实证**：api 路径 646 次
            "no tool calls and no final response" 直接失败——CoreLoop
            的纪律重试会把这类失败转为恢复，是净收益的量化证据。
          - **不能用的（数据天然缺失）**：trace payload 只有
            argsHash/resultHash 无内容 → 安全规则误伤仿真、内容级回放
            比对不可行（命令内容本就不出桌面，这是隐私设计的正确性
            证明）。
          - **token 系数校准：部分可行，已落地首个实测系数**
            （2026-08-26 二次挖掘，经 api `.env` 公网端点直连）：此前
            结论"不可行"过于悲观——`chatmessage` 有 5.1 万条 assistant
            内容（均值 7.6k 字符），与 `llmusagedaily`
            （service='harness_loop'）按 (userId, date) 聚合连接得
            6,605 个单模型桶（99.8% modelId='default' → 经
            `models_config.py` 解析为 **deepseek-v4**）。聚合回归：
            **Σ(实际 completionTokens) / Σ(启发式估算) = 0.708**——
            基础启发式在真实 CJK 流量上**高估 ~41%**，压缩阈值因此
            比设计意图更早触发（金丝雀单轮 5 次压缩的独立证据链）。
            已落地 `MODEL_TOKEN_FACTORS["deepseek"] = 0.71` +
            回归测试（228 全绿）。思维链内容系数 ~0.40 tok/char
            在各设定下稳定。**cjk/other 单字符系数不可识别**
            （corr=0.88 共线性，回归给出负系数伪影）——按请求级
            estimated/observed 对仍按 P3 原方案由 sidecar 自记录。
- **P1 · 结构差距**（2026-08-26 落地，测试全绿）
  - [x] **轮中转向（steer / inject）**：dsh 的一等公民能力，steerable
        目前只能整轮取消。最简形态：CoreLoop 加 asyncio 队列 inbox，每轮
        `pre_step` 前 drain 并入 transcript；sidecar 加 `agent.chat.steer`
        RPC；agent 输入框映射「追加」。不做 dsh 完整 inbox 语义
        （followup/wakeup 对桌面单会话过度设计），先只支持 inject。
        - 落地：`CoreLoop.steer()` + 每轮顶 drain（emit `steer` 事件，
          在 hooks 前，routing/compaction 可见）；sidecar
          `agent.chat.steer`（未知 stream 软失败 `stream_not_active`），
          steer 事件转发为 `stream.chunk.notice`；agent 侧
          `supervisor.steerChat` → `local-backend:steer` IPC（chatId→
          sidecar streamId 注册表在 coreloop-stream.ts）→ preload
          `steerChat` → transport.steer → `useChatStream.steerUserMessage`
          （成功即上屏 user 消息）→ ChatInput streaming 期间 ⌘+Enter =
          追加（发送键仍是停止，失败保留草稿）。
        - 测试：py `test_steer.py` 4 例（下一轮可见/多条按序/run 前注入/
          run 后无害）；sidecar 3 例（RPC→运行中 loop/未知流软失败/参数
          校验）；agent-ui 3 例（接受上屏/非 streaming 拒绝/无 steer
          能力拒绝）。
  - [x] **并行工具执行**：codex（RwLock 门）/ dsh（并发池默认 10）都有；
        steerable 串行跑只读工具是纯延迟浪费。`RegisteredTool` 加
        `concurrency_safe` 标记（默认 False），连续 safe 调用
        `asyncio.gather`，unsafe 做屏障；host 工具经反向通道时由
        Electron 侧串行化兜底；事件按调用序 emit 保持确定性。
        - 落地：`RegisteredTool.concurrency_safe`（默认按 mode 推断：
          read→safe，可显式覆盖；register/register_remote/@tool 全链路
          透传）；loop act 阶段三段式——① start 事件+dedup 顺序执行，
          ② 连续 safe 批次 gather / unsafe 屏障单跑，③ 按调用序应用
          counters/hook/result 事件/transcript；`LoopConfig.parallel_tools`
          默认开，executor 无 `concurrency_safe` 鸭型方法（如
          HostToolExecutor 显式返回 False）则全串行。结果按批次位置键控
          （provider 可能发重复 tool_call id）。**顺带修了一个潜在
          bug**：breaker 触发且 narration 放行时，未执行的 tool_call 现在
          补合成 tool 消息（`_BREAKER_SKIP_MESSAGE`），否则 wrap-up 轮
          的 transcript 悬空会被 provider 拒绝。
        - 测试：py `test_parallel_tools.py` 7 例（并发重叠 rendezvous/
          事件序确定性/unsafe 屏障/批内 dedup/计数器按序/无检查方法
          executor 串行/config 关闭串行）。
- **P2 · 健壮性缺口**（2026-08-26 落地，测试全绿）
  - [x] **provider 错误分级 taxonomy**：codex 有 `is_retryable` 分级、
        dsh 有结构化错误码 + retryPolicy；steerable 的 RetryHooks 只认
        「瞬态」一种。llm 层定义 `LLMErrorKind`（transport / rate_limit /
        context_overflow / auth / invalid_request / server），
        openai_compat 按状态码映射，RetryHooks 按 kind 分路，补
        conformance 用例。
        - 落地：`llm/errors.py`——`LLMError(kind, status_code)` +
          `classify_http_status`（400/413 按 body 溢出语料识别
          context_overflow，覆盖 OpenAI/Ollama/vLLM/DeepSeek/Anthropic
          措辞）+ `classify_error`（httpx/asyncio 异常兜底）+
          `is_retryable`。openai_compat 的 complete/stream 均包装
          HTTPStatusError→LLMError、TransportError→transport；
          anthropic_native 包装 SDK 的 APIStatusError/APIConnectionError。
          RetryHooks 默认按 kind 分路：transport/rate_limit/server/unknown
          退避重试，auth/invalid_request 快速失败，context_overflow 留给
          CompactionHooks。
        - 测试：`test_error_taxonomy.py`（状态码映射/溢出语料/分类兜底/
          分路/auth 快速失败不重试/transport 重试恢复）。
  - [x] **上下文溢出恢复**：CompactionHooks 只在 `pre_step` 预防，真溢出
        时 RetryHooks 会重试同样的超长请求必然再失败。在
        `on_request_error` 识别 context-overflow 类错误，重试前先强制
        压缩一档（依赖上条的错误分级）。
        - 落地：`on_request_error` 签名扩展为 `(error, transcript, ctx)`，
          `RetryAction` 增加 `transcript` 字段——hook 重写 transcript 后
          loop 采用替换体重试。`CompactionHooks.on_request_error`：识别
          context_overflow → 无视阈值强制折叠+摘要一档 → retry；每轮上限
          2 次防 compact→overflow 死循环。sidecar 默认 hooks 链改为
          `ChainHooks(CompactionHooks(maxContextTokens 参数, model),
          RetryHooks)`——桌面端开箱获得压缩+溢出恢复。
        - 测试：溢出→压缩重试→完成（验证重试请求带折叠标记）；连续溢出
          超上限→fail；无 CompactionHooks 时溢出立即 fail。
  - [x] **崩溃恢复语义（合成闭合）**：dsh 冷启动给中断 turn 合成
        `TOOL_OUTCOME_UNKNOWN` 工具结果；steerable 的 `resume.py` 若遇
        崩在工具执行中的 trace，重建 transcript 会有悬空 tool_call（部分
        provider 直接拒绝）。`project_transcript` 收尾时检测未配对
        tool_call，补合成 tool 消息（"result unknown — process
        interrupted, verify side effects"）。纯投影层改动，不动 loop。
        - 落地：`_close_dangling_tool_calls` 后处理——每个 assistant 的
          tool_calls 与其后连续 tool 消息块配对，未配对的在块后按调用序
          补 `_INTERRUPTED_RESULT` 合成消息（保持已记录结果的原始位置）。
          完整 trace 零改动透传。
        - 测试：部分中断（c1 有结果 c2 合成且顺序保持）/完整 trace 透传/
          全部中断。
- **P3 · 数据闭环与生态**
  - [x] **校准系数实测闭环**（2026-08-26 落地，runtime 240 + sidecar 43
        全绿）：出厂表已非空——`deepseek: 0.71` 来自生产聚合回归
        （见 P0 线上数据实测节，6,605 桶，Σ实际/Σ估算=0.708）。
        请求级自记录回路已落地：
        - `calibration.py`：`UsageCalibration`（按模型滚动 Σobs/Σest，
          ratio-of-sums 抗小样本噪声；单请求比值超出 [0.1, 10] 截断防
          脏数据；≥20 样本自动 `register_model_factor`，精确模型名经
          最长前缀匹配覆盖族系数）+ `CalibratingProvider`（包装任意
          LLMProvider，complete/stream 全透传，**对基础启发式估值**——
          对已修正估值测量会让比值假性收敛到 1.0；completion 侧含
          reasoning_delta，思维链按 completion 计费）；JSON 原子落盘
          （tmp+rename），重启后续算。
        - sidecar 工厂默认开启（`STEERABLE_TOKEN_CALIBRATION=0` 关闭，
          `STEERABLE_TOKEN_CALIBRATION_PATH` 改路径，默认
          `~/.steerable/token-calibration.json`），加载时即注册已有
          系数——dogfood 零配置积累样本。
        - 测试 12 项：ratio-of-sums 数学/min_samples 门控/脏数据截断/
          自动注册/持久化往返/坏文件容错/stream usage 捕获/无 usage
          不记录/周期落盘/属性透明委托。
        - 剩余（dogfood 一周后）：读 `~/.steerable/token-calibration.json`
          对账 ollama 系模型实测系数，与 deepseek 0.71 对照决定是否
          拆分 cjk/other 单字符系数。
  - [ ] **MCP 下沉**：维持原约定（A4 稳定后按需，用户已确认推迟）。

**本轮明确不做**：沙箱下沉（产品侧决策，桌面端刻意全允许）；实时 OTel
span（事后导出已够用，等灰度暴露真实观测需求）；dsh 完整 inbox 语义；
真 tokenizer 依赖（校准闭环后启发式误差可控，不引入 tiktoken 二进制）。

#### 第四轮复审后的优先级（2026-08-26，canvas `steerable-vs-codex-dsh-r4`）

P0–P3 全落地 + 首日 dogfood 真实数据后重排：14 轴对比（新增 prompt
cache 友好性、多智能体两轴）**3 领先 / 8 追平 / 3 落后**。R3 的 6 个
落后轴闭了 5 个（溢出恢复 / 错误分级 / 转向 / 并行工具 / 崩溃闭合）；
剩余落后中沙箱是刻意产品决策、多智能体属观望。codex 本周演进集中在
Guardian v2 与企业 MCP OAuth，不影响轴级对比；dsh 停在 0.1.1-rc.2。

- **P0 · 已解除（2026-08-26 晚，用户决策）**
  - [x] **CoreLoop default-on**：不等一周 dogfood（样本收集太慢），直接
        默认开启。`STEERABLE_USE_CORELOOP` / `STEERABLE_USE_SIDECAR`
        语义从「=1 开启」翻为「=0 回退」；sidecar 默认随 app 启动
        （不再阻塞窗口创建，boot 竞态的回合自动落 TS 循环）；dev 机
        python 解析新增 sibling venv 候选（`../steerable-framework/
        .venv`），否则默认开启会静默退化成 TS 循环。零环境变量金丝雀
        8/8 通过（agent 325 测全绿，框架 332 全绿）。
  - **default-on 验证暴露并修掉的两个真实 bug**（都是「测试全绿但
        生产才暴露」同类）：
        ① `OpenAICompatProvider` 流式从不发 `stream_options.
        include_usage` → usage chunk 不到达 → **预算记账与校准采样
        双双失明**（dogfood 期间 0 次 budget 事件的根因）；修复后
        Ollama cloud 实测 usage 正常到达（prompt=72/completion=54）。
        ② 校准聚合器按请求新建（factory 每回合调用）→ 短回合永远
        到不了 persist 阈值；改为 sidecar 进程级单例 + 周期落盘 +
        `system.shutdown` 冲刷。活体验证：真实流式回合 →
        `stream.done=completed` → 优雅退出后文件含 1 条
        `gpt-oss:20b-cloud` 样本（est_prompt=14/obs=72，提示词侧
        该模型被低估 5×，20 样本后自动出系数）。
  - **测试基建修复**：agent-runtime `tests/__init__.py` 已在
        `7edb0f3` 删除（CI 三包同名撞车），但 4 个文件仍用
        `from .test_trace_recorder import` 相对导入 → 根目录跑全量
        收集失败。改平级导入 + 新增 tests/conftest.py 插入自身目录
        到 sys.path（importlib 模式不自动加）。根目录 332 全绿。
- **P1 · dogfood 暴露的真实问题** ✅ 已解决（2026-08-26 晚）
  - [x] **压缩提频治理**：修复中还原了真相——「22 次 compact / 5 trace」
        **约一半是测量假象**：loop.py 的 compact 事件判据是
        `pre.transcript is not None`，而 ChainHooks 永远返回非 None
        transcript → 每轮都发伪 compact 事件（已改恒等比较，只有真改写
        才发事件）。真实治理三件套仍全部落地：
        ② 阈值按模型上下文窗比例——`tokens.py` 新增
        `MODEL_CONTEXT_WINDOWS`（镜像 api models_config 权威值：
        deepseek/gpt-oss 131k、kimi-k2 262k、claude/gpt-5 200k…）+
        `resolve_context_window(model, explicit)`，sidecar
        `_default_loop_hooks` 改为按模型解析（显式 maxContextTokens 仍
        优先；未知模型回落 60k 保守值；本地 ollama num_ctx=4096 场景需
        显式配置，文档已注）。
        ③ 观测驱动压力 + 滞后防抖：loop 把每轮 provider 实报的
        `usage.prompt_tokens` 写进 `LoopContext.last_prompt_tokens`
        （含当时 transcript 长度），压缩压力 = 上轮实测 + 增量估算，
        首轮才用全量启发式；hook 改写 transcript 时重置观测（索引失效）。
        新增 `recompact_margin_ratio=0.1` 滞后——压缩后压力须再涨
        10%×window 才允许二次压缩，根治「压不下去就每轮重压」的 cache
        毁灭循环。折叠保留 160 字符 excerpt（文件路径/错误类型留线索）。
        ① 校准闭环（P3 已上线）继续自动修正系数。
  - [x] **terminal-exec pty 长尾（max 21s）**：取证推翻原假设——代码里
        **不存在固定间隔轮询**（terminal-manager 的 exec 是 onData 哨兵
        事件驱动）。日志配对显示：system_profiler 类命令 2.5–11.8s 是
        真实执行耗时；瞬时命令的 5.9s 是**会话首条命令**的 pty spawn +
        zshrc 初始化（dogfood 期间 load avg 124 放大）。已落地：app 启动
        2s 后预热共享 PTY 会话（`terminalManager.ensurePrimary()`），
        预热后金丝雀 echo span 从 767ms（温）/5914ms（冷）降到 128–204ms。
  - **default-on 回测中再抓一个真 bug**：`llm/index.ts` 的
        `getSidecarSupervisor()` 内部仍要求 `STEERABLE_USE_SIDECAR === '1'`
        ——P0 只翻了路由门的判据，这个 getter 的旧 opt-in 把 default-on
        静默架空（零环境变量启动时回合仍落 TS 循环，金丝雀「通过」因为
        它不分辨路径）。已翻为 `!== '0'`（LlmService 辅助调用同步走
        sidecar，LLM 流量单一路径）。修复后金丝雀 trace 确认
        `coreloop:true` 且 hook 事件只剩真实的 `tool_choice@r0`。
- **P2 · 产品与默认值（✅ 2026-08-26 完成，框架 344 测试全绿 + 金丝雀 PASS）**
  - [x] **fork / 变体语义**：✅ 落地。`project_transcript(events,
        until_sequence=N)` 截断参数（resume.py，`load_transcript` 透传）
        + sidecar `agent.chat.fork` RPC：`{traceId, untilSequence?,
        messages?}` → 投影截断 transcript + 追加消息 → 新 streamId 起跑
        并独立记录 trace（变体语义：每个变体一条 trace）。桌面端不需要
        此 RPC——regenerate 已在自有消息库上 truncate-and-rerun，CoreLoop
        路径天然兼容；fork RPC 服务 trace-sourced 会话（未来 api
        `chatmessagevariant` 采纳、sidecar 原生会话）。分叉点经
        `trace.fetch` 定位；注意轮边界（`stage_complete`）才是正确分叉点
        ——一轮的 content_delta 先于其 tool_call_start。测试 ×4。
  - [x] **budget 默认值按生产分布放宽**：✅ 落地，并修复一个默认开启
        回归——TS 循环有固定 60k token 预算，CoreLoop 路径上线时**完全
        没有**预算（只剩 maxRounds=32 兜底）。sidecar `_build_loop_config`
        现在在未传 `budgetTokens` 时默认 `max_tokens = 2 ×
        resolve_context_window(model)`（deepseek 262k / 未知模型 120k）。
        生产分布实证：均值 14.5 万/trace ≈ 1.1× 窗口，api 固定 120k 上限
        切断 6% 真实任务；2× 窗口让真实任务通过、失控成本仍有界。
        maxRounds 仍是主失控护栏（BudgetLimit 的 steps/tool_calls 轴当前
        不被 loop 消费）。显式 `budgetTokens` 优先。测试 ×1。
- **P3 · 生态（2026-08-26 两项落地，MCP 维持推迟；框架 353 测试全绿 + 金丝雀 PASS）**
  - [x] **多智能体 seam**：✅ 框架层落地。新模块 `subagent.py`：
        `SubagentExecutor`（ToolExecutor 装饰器，拦截 `delegate_subagent`
        调用 → 同 provider 跑有界子 CoreLoop，max_rounds=8，子代理答案 =
        completion 时累积的 assistant 文本）+ `subagent_tool_descriptor()`
        （OpenAI schema）。**深度 1 由构造保证**（子代理派发到 inner
        executor，无法再 spawn）；子代理在父 trace 中只占一个 tool span。
        sidecar 接线为 opt-in：`params.subagent: true` → 包装 executor +
        追加描述符。桌面端是否暴露仍是产品决策（默认不开启）。测试 ×6
        （往返、禁嵌套、无工具失败关闭、空任务、子代理耗尽、schema）。
  - [x] **实时可观测性（OTel 项的诚实切片）**：✅ 修复真实缺口——事件和
        span 本就增量持久化，但 **trace 行只在 finalize 时写入**，回合
        进行中 `trace.fetch` 报「trace not found」，长回合完全不可观测。
        TraceRecorder 现在在首个事件时以 `status="running"` upsert trace
        行（createdAt 固定于首次写入），finalize 覆写终态。完整 OTel
        collector 实时流仍推迟（事后导出够 dogfood 用）。测试 ×1。
  - [ ] **MCP 下沉**：维持约定推迟——桌面工具经反向通道在 Electron 侧
        执行，MCP 客户端留在 TS 层；sidecar 直连 MCP server 是独立的大
        改动，等 api 采纳或桌面产品需求驱动。

## A4 · desktop 切换并删码（2–3 周）

- [~] sidecar 托管 CoreLoop，工具走 A1 的反向通道回 Electron 执行
      **Slice 1 已落地（2026-08-25，框架 `9bed21a` + agent `28a12af`）**：
      - 框架：`HostToolExecutor`（反向 `tool.invoke` → ToolResult，超时/错误
        兜底为失败结果）；sidecar CoreLoop 路径新增 `toolsViaHost` 请求参数；
        wire 事件补齐 `arguments` + `resultPreview`（工具卡片可视化）。
      - agent：`STEERABLE_USE_CORELOOP=1`（配合 `STEERABLE_USE_SIDECAR=1`）时
        `handleStream` 整轮委托 sidecar CoreLoop；`main.ts` 注册反向
        `tool.invoke` handler（critical shell 命令硬阻断，比 TS 路径的
        仅标注更严）；`coreloop-stream.ts` 把 wire 通知翻译成既有 SSE 契约，
        前端/preload/IPC 零改动；关 flag 即回滚 TS 循环。
      - 验证：框架 py 159（+3）/ agent vitest 307（+12）/ 真实 sidecar 进程
        集成 3 / 双仓 CI 绿。
      - **已补（2026-08-26，框架 `9d92b12` + agent `7dfaf65`）**：plan 模式
        执行层硬阻断（请求带 `toolContext {mode}`，反向 handler 拒绝写工具，
        不再只靠工具广告过滤）；TraceRecorder 接入 sidecar CoreLoop 路径
        （事件落 sidecar storage，`stream.done` 带 `traceId`，宿主可
        `trace.fetch` 拉取完整轨迹）。
      - **还差**：桌面端真实对话灰度（无头金丝雀已过，见下）。
- [~] **真实流量灰度**：**无头金丝雀已过（2026-08-25，agent `a42db9f`）**——
      真实 sidecar + Ollama cloud（gpt-oss:20b-cloud）+ 反向通道端到端：
      模型自主 list_dir → read_file 两轮工具调用，最终回答含校验串
      （grounding 成立），7s 完成。金丝雀当场抓到两个脚本测试从未覆盖的
      真实缺口：provider 工厂缺 `name` 参数（此前从未成功构造过真实
      provider）、sidecar 缺 httpx 依赖（框架 `8551a98` 已修 + 回归测试）。
      复跑方式：`STEERABLE_SIDECAR_TEST=1 STEERABLE_SIDECAR_PYTHON=<venv> \
      npx vitest run tests/sidecar/coreloop.integration.test.ts`。
      **还差**：Electron 桌面端双 flag 真实对话（伪调用恢复率 / 工具结果
      大小分布 / 超时抖动 / 安全规则误伤率只有桌面分布能回答）。
- [x] ~~灰度通过后删 `deeppath-agent/src/harness`~~ ✅ 2026-08-26（agent
     `298eb82`，净 -3218 行）：TS 循环整段删除，`handleStream` 无条件走
     `handleCoreLoopTurn`（sidecar 不在时 503 失败 loud，无退路）；幸存者
     搬迁——`safety-patterns.ts` → `src/sidecar/`（反向通道 shell 分类），
     `ToolMode` 类型 → `tool-router.ts`；`turn-router.ts` /
     `grounding-judge.ts` 删除（反幻觉由 Python hooks 承担，CoreLoop 路径
     已传 `antiHallucination: true`）；`deferred-detector.ts` 裁到只剩
     历史污染检测在用的 `detectDeferredExecution`。验证：tsc 干净、
     vitest 243 过、electron 构建过、桌面金丝雀 PASS。
- [x] ~~把 61 条 shell 安全规则回流到框架~~（Tier 1 已完成：双语 61 条 +
     ToolRouter 接线 + 一致性测试）
- [x] ~~解决包体门禁~~ ✅ 2026-08-26（见上方硬阻塞；框架 `1ce60d0`）
- [x] **真实数据 E2E 全流回放** ✅ 2026-08-27（agent `scripts/e2e-real-replay.mjs`）：
     生产库抽样 12 条真实用户消息（短/中/长）+ 1 段真实 4 轮会话，CDP 驱动真实
     Electron 应用跑 6 阶段（单轮×12 / 多轮×4 / 工具×3 / 重生成 / 中途取消 /
     plan 模式），47 项断言。首轮 41/47，**当场抓到 3 个脚本测试从未覆盖的
     真实 bug 并全部修复**：
     1. sidecar stdio 传输 64KiB `StreamReader` 上限——大工具结果（文件读取/
        grep 输出）直接 `LimitOverrunError` 杀读循环，回合静默挂死、无 trace。
        修复：上限提至 16MiB（有界）+ 子进程回归测试（框架侧）。
     2. 可见终端超时级联卡死——一条 `grep -R` 超时后仍在跑，后续所有 exec 被
        打进忙终端的 stdin 全部连锁超时。修复：非 GUI 命令超时发 SIGINT +
        恢复探针哨兵（zsh 收到 ^C 会中断整条命令列，原哨兵不会执行，探针
        证明 shell 已回提示符），终端立即恢复可用（agent `terminal-manager.ts`）。
     3. 取消回合丢 traceId——abort 立即 reject，sidecar 的
        `stream.done{cancelled,traceId}` 无人接收。修复：abort 后等取消回执
        （10s 宽限），`cancelled` 正确映射为完成状态，取消回合也持久化 trace。
     4. 二次 narrate 兜底——wrap-up 回合空响应（错误密集 transcript 上的
        模型抽风）原先直接判 failed；现在空 wrap-up 会回投
        before_completion 触发第二次更直接的 narrate（上限 2 次，防循环）。
     最终成绩 **45/47**（单轮 11/12，其余阶段全过）。剩余失败均为
     provider/模型层（Ollama cloud 500 抖动——重试钩子 3 次后失败 loud
     符合设计；gpt-oss harmony 标记泄漏进工具名 `json<|channel|>commentary`），
     非 loop/传输/终端代码问题。复跑：`node scripts/e2e-real-replay.mjs`
     （需应用带 `--remote-debugging-port=9222` 启动）。
     **复跑 47/47（2026-08-27 10:08，harmony 修复后）**：单轮 12/12、
     多轮/工具/重生成/取消/plan 全过，全程 6.6 分钟（无 240s 超时 stall）。
     工具阶段模型另一次吐出垃圾工具名（非 harmony 标记，纯乱码），
     派发层兜底按设计工作：unknown tool + 全量合法工具名反馈 → 模型
     下轮自我纠正 → 回合正常完成——兜底路径获真实流量验证。
- [x] **harmony 泄漏治理** ✅ 2026-08-27（E2E 遗留项收口）：双层修复——
     ① 解码层 `_sanitize_tool_name`（openai_compat.py）：合法工具名永不
     含 `<|`，从首个标记截断 + 去 `to=`/`functions.` 路由前缀，部分泄漏
     （`exec_command<|channel|>commentary`）直接恢复成真名继续执行；
     ② 派发层兜底（tools.py）：difflib 模糊匹配对垃圾名（清洗后只剩
     `json` 这类 constrain 值残渣）必然零建议，此时错误消息改列全部
     合法工具名，模型下一轮可照单重发。测试 ×6（清洗 4 + 解码/流式
     各 1 + 派发兜底 1，含真实泄漏样本），既有零建议用例同步更新为新
     行为。框架 316 全绿。

**通过标准**：sidecar 模式下离线 Ollama + 本地 shell/文件/MCP 工具全部跑通；
包体过门禁。✅ 均已达成（金丝雀连续多轮 PASS；包体 darwin 实测 94.7MB；
四平台 CI 门禁 2026-08-27 全过——linux-x64/win32/darwin-arm64 秒过，
darwin-x64 因 macos-13 Intel runner 退役排队，构建本身无问题）。
**回滚**：git revert agent `298eb82` 可恢复 TS 循环；运行时开关已随
TS 循环一并删除。

## A5+ · 路线图：按 codex 演进路线映射（2026-08-27，canvas `steerable-roadmap-codex-route`）

codex 六阶段对照：① 单循环+执行+安全（沙箱刻意跳过）② 持久化真相源
（✅ 全齐）③ **协议面产品化 ← 当前转折点** ④ 生态接入（MCP 推迟）
⑤ 可观测纵深（事后导出够用）⑥ 多智能体/二审（seam 已备）。
codex 的教训：阶段 3 的 app-server 把「循环」变成「平台」，之后 MCP、
skills、多智能体全是挂在稳定协议面上的增量——顺序不能颠倒。

接下来三步**严格按序**：

- [ ] **第一步 · sidecar RPC 面 app-server 化（A5 勘察切片，本周可启动）**：
      盘点 api 仓约 100 个 SSE 发射点相对 A2 港口规格的漂移，产出采纳
      成本重估 + 「sidecar 协议 v1 冻结范围」（哪些 RPC/事件形状为 api
      采纳而定版）。不动 api 代码，纯勘察。前置条件已成立：CoreLoop
      default-on 生产验证 + 带着 api 缺失的反幻觉层。
- [ ] **第二步 · 沙箱（分发硬门槛，与第一步可并行）**：「桌面刻意全允许」
      只在自用 dogfood 成立。macOS Seatbelt 起步（sidecar 进程
      sandbox-exec 包装 + 写路径白名单），Linux Landlock 跟进；61 规则
      分类器降级为沙箱内第二道提示层，对齐 codex 双层结构。
- [ ] **第三步 · MCP client 下沉进 sidecar（严格依赖第一步结论）**：
      api 采纳则下沉（一次服务桌面+api，推迟约定自动解除）；不采纳则
      MCP 留在 Electron TS 层，此步取消。

**明确不抄 codex**：TUI/云任务/企业 OAuth/权限 profile（OpenAI 产品
广度逼出来的，桌面+api 形态抄了是负资产）；Guardian 独立二审模型
（grounding judge 已是轻量版，沙箱之前升级它是本末倒置）；多智能体
产品化（codex 两年后才上 v2，seam 已备等真实需求）。

**原 A5 备忘**（并入第一步勘察范围）：
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
