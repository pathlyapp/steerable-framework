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
  - [ ] **MCP 下沉**：推迟（与 skills 下沉一并评估，结论相反）。2026-08-27
        实勘 codex + dsh 后的准确理由：
        ① **产品权衡，不是技术不可能**：`mcp-executor.ts` 用
        `localRequire.resolve(pkgName)` 从 Electron 的 node_modules 解析
        MCP server npm 包，并以 `process.execPath` +
        `ELECTRON_RUN_AS_NODE=1` 拿 Electron 二进制当 Node 运行时——
        换来「用户零前置条件就能跑 npm MCP 服务器」。**codex 与 dsh
        都不打包 Node**（都要求用户声明 `command: npx` 并继承 PATH；
        codex 无 Node 发现逻辑，dsh 连自己的 subprocess capability 都
        不用、让 MCP SDK 自己 spawn 且文档写明豁免）。所以下沉的真实
        代价是放弃这个零前置条件体验、退回两家的水平，而不是「做不到」。
        往 sidecar 塞 Node 则当场爆刚过的 320MB 包体门禁。
        ② **循环不需要知道，且三家一致**：MCP 工具经反向通道执行，在 loop
        眼里与 `local_exec_shell` 无区别。codex 把每个 MCP 工具注册成
        `McpHandler`、实现与原生工具**同一个** `ToolExecutor` trait；dsh 走
        纯 `ctx.tools.register()`、循环层零 MCP 代码（连提示词里的
        「via MCP」标注都刻意不加）。命名约定三家也相同：
        `mcp__<server>__<tool>`（codex SHA1 冲突后缀 + 128 字节上限，
        dsh 64 字节 + 哈希后缀）。抽象已到位，下沉不解决任何问题。
        对比 skills：catalog 注入是 `pre_step` 的循环行为，留 TS 层等于
        又写一份将来要删的循环逻辑。这是两者结论相反的根本原因。
        ③ **失败模型**：sidecar 现在近乎无状态、重启便宜（15s boot）；
        拥有 MCP 服务器进程树后它变成进程监管者，重启/升级/崩溃恢复全部变重。
        ④ OAuth 流程需要窗口，属宿主职责——codex 的分工可直接参照：
        app-server 返回 `authorization_url`，**客户端开浏览器**，app-server
        完成流程后发 `mcpServer/oauthLogin/completed` 通知。（dsh 干脆没有
        OAuth：用户自己塞 env/headers。）
        **唯一解除条件**：api 采纳 CoreLoop **且** api 侧也需要 MCP——
        那时 sidecar 内的 Python MCP 客户端同时服务 api（服务端无 Electron）。
        即使那天到了，桌面端仍可保留 TS 客户端：两边 seam 都是 `params.tools`。
        **两个可抄的细节（届时或做 MCP 懒加载时）**：dsh 的重连预算重置
        （指数退避 500ms→30s、单次故障 10 次上限，但连接存活超过 maxDelay
        就重置预算——一个判据区分「服务器恢复」与「崩溃循环」）；codex 的
        工具目录进程级 LRU（32 条 / 30 分钟 TTL，键=服务器身份+环境+客户端
        能力）配合 `LazyWhenCached` 启动策略，省掉冷启动 30s 超时窗口。
        **两家的分歧供参考**：`tools/list_changed` codex 只记日志不刷新、
        dsh 序列化重同步；自己当 MCP server codex 做了
        （`codex-mcp-server` 暴露 `codex`/`codex-reply`）、dsh 明确拒绝
        （ACP 已覆盖「把 harness 暴露成 agent」，再加是同事换协议）。

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

#### 第五轮复审（2026-08-28，三方对照：codex 深潜 / dsh 深潜 / 2026 生态审计）

英文全文已进文档站：[`docs/roadmap.md`](docs/roadmap.md)
（nav「Roadmap」，https://steerableframework.com/roadmap/）——含差距记分卡、
按依赖排序的 wave 计划、协议定位决策与明确不做清单。本节只记结论与它对
上面「三步严格按序」的改动。

- **差异化项被三方独立确认是「模型质量工程层」**，不是协议也不是 sidecar：
  伪调用恢复（`pseudo.py:7-9`，三族 + 流式剥离）、`before_completion`
  三态否决（`loop.py:688-730`，codex / Claude SDK 均无对应物；生产证据
  646 次 "no tool calls and no final response" 硬失败可转重试）、
  经验 token 校准（`calibration.py`，6,605 桶得 0.708）、反幻觉判定
  （`antihallucination.py`）、软超时收尾、去重软反馈、breaker 跳过的
  合成 tool 消息。**结构性护城河**：厂商 SDK 的商业目的是让自家前沿模型
  发光，把量化本地模型做可靠与之相悖；LangGraph 是底座不拥有循环，没地方
  放这层；codex / dsh 拥有循环但面向前沿模型。双形态交付（Electron 签名
  sidecar + FastAPI）正好服务需要这层的市场（本地/离线/成本敏感/受监管
  桌面软件）。**双形态是交付渠道，不是差异化本身**——`README.md` 现在
  以 wire 协议和 headless React 开篇（人人都有的能力），把这层埋了。
- **根因是一处倒置，衍生五个后果**：模型可见 transcript 是**可变
  `list[LLMMessage]`**（`loop.py:320` 建列表就地改写；steer `:374`、
  grounding `:400`、软超时 `:440`、纪律重试 `:699`、narration `:726`；
  hook 可整体替换 `:464-465`），而不是持久追加记录的投影；
  `self.trajectory` 另走一路，`resume.py:71` 再重建**第三份**。三者无
  强制一致且实际不一致。五个后果：① 续跑失真（喂模型一份它没见过的
  历史，慢漂移；且默认 300 字符预览 `loop.py:871-873`，模型记录反倒
  下游于展示记录）；② **prompt cache 破坏（最贵）**——`recompact_margin`
  滞后（`compaction.py:83-88`）是让前缀失效**更便宜**而非阻止它，是
  dogfood 病理（22 compact / 5 trace）的疤痕组织；且**当前无法测量**：
  `cached_tokens` / `cache_control` 全 `packages/` 零匹配，`LLMUsage`
  只有三个字段；③ 注入内容无法设界（skill catalog / steer / 三方 hook
  输出都无上限，对比 codex 的 2500 token + 落盘）；④ **MCP 无法安全落
  地**（见下）；⑤ 测试证明不了任何事（无录制 provider，没有一条测试断言
  模型实际看到了什么）。**`LLMMessage.content: str`（`llm/__init__.py:28`）
  是全仓最贵的一行**——挡住多模态、结构化输出、以及 `cache_control`
  （逐 block 注解），Tier 1 破坏性变更，必须在 1.0 前落。
- **MCP 时序分歧的裁决：采纳 codex 侧论点——基础设施之前不做 MCP。**
  生态审计（2026-07-28 spec 把 core 改成无状态 HTTP，无握手/无 session
  id，「sidecar 变进程监管者」的旧理由确实失效）与 dsh 侧论点（装饰器链
  里加个 `McpToolExecutor`，架构上是琐事）都成立，但它们回答的是「贵不
  贵」，不是「什么时候」。MCP 是任何 agent 系统里**最大的无界、第三方、
  可变的模型可见上下文源**；落在可变列表 + 无逐项设界 + 无曝光分层 +
  无状态 diff 上，会精确复现已记录的 cache 抖动病理，且更难诊断（成因在
  另一个进程里）。届时的前置：逐工具超时、逐服务器目录上限、确定性名字
  限定（`mcp__<server>__<tool>`，三方一致）、逐 step 不可变工具绑定、
  曝光分层（Direct / Deferred / Hidden，注册 ≠ 曝光）。架构位置不变：
  host 侧（Electron 主进程）启动，经既有 `ToolRouter.register_remote`
  （`tools.py:110-149`）接入，**不**由 Seatbelt 收容的 sidecar spawn。
- **安全：沙箱收容了错误的进程（三方一致）**。`docs/spec/safety.md` 诚实
  写明 layer 1 收容 sidecar、工具执行刻意不收容——但 sidecar 是**低风险**
  那个。更糟的是 profile 读全开（`safety.md:98`）+ `network-outbound`
  全开（`:99`），私有数据 + 不可信内容（工具输出与网页结果进 transcript）
  + 出网三者同进程 = lethal trifecta，OWASP Agentic Top 10 2026 第一位，
  61 条正则分类器不解决它。分阶段：(a) 出网白名单（由 provider `baseUrl`
  + 显式配置推导，profile 生成器约 30 行）；(b) 子代理独立工具域——
  `SubagentExecutor` 现在把子代理派发给**父自己的 executor**
  （`subagent.py:107`，要么全父工具要么无工具），只读研究型子代理可按
  构造打断 trifecta；(c) `SandboxedToolExecutor` 端口（桌面逐 exec
  Seatbelt / 服务端 E2B 类沙箱）。另采纳 dsh 的
  `SandboxEnforcement: full | partial | none` 作为**返回值**而非日志行
  （`dsh docs/subsystems/sandbox.md:30`），要求绝对边界的调用方可以拒绝；
  steerable 现在是让宿主「大声记日志并继续」（`safety.md:109-112`）。
- **新的 wave 排序（按依赖，不按吸引力）**：
  - [x] **Wave 0（全 S，先做）** ✅ 2026-08-28 落地：① `RecordingProvider` + 断言
    （`assert_stable_prefix`：第 n 次请求的消息是第 n+1 次的前缀，除声明
    的压缩边界外——「不重写历史」的可执行形式，**今天就会失败**；
    `assert_bounded_items`）。这是**前置**，没有它 Wave 1 会写对然后
    静默劣化；② 逐工具超时（`soft_timeout_ms` 只在轮次边界检查
    `loop.py:425-429`，挂死的工具会挂死整轮；也是 MCP 硬前置）；
    ③ 出网白名单。
  - [x] **Wave 1（L，地基，一个项目不是三个）** ✅ 2026-08-29 落地：带类型的追加式历史
    （`HistoryItem` 信封：序号 / turn id / content kind / token 估值）
    + `ContextFragment`（注入内容带稳定标记，可在保留历史里认出自己的
    渲染，对应 codex `ContextualUserFragment`）+ `pre_step` 改为只追加、
    `ContextManager.replace_all` 是唯一声明式重写路径 + 与展示流分离的
    持久模型可见记录 + 续跑改为反向扫描到最近压缩检查点（O(tail)）。
    **`content: str` → content parts 在同一个 wave 落**——两者都是 Tier 1
    破坏性变更，分开做等于让消费者断两次。
  - [ ] **Wave 2（回报）**：cache 仪表 → world-state 分节 diff →
    工具曝光分层 → **然后**才是 MCP。
    顺序本身就是论证：先有仪表才能验证 diff 有效，先有分层 MCP 才能规模化。
    - [x] **cache 仪表** ✅ 2026-08-29（`d111387`）：`LLMUsage` 加
      `cached_prompt_tokens` / `cache_creation_tokens`（OpenAI
      `prompt_tokens_details.cached_tokens`、DeepSeek 顶层
      `prompt_cache_hit_tokens`、Anthropic `cache_read_input_tokens` /
      `cache_creation_input_tokens`），loop 挂既有 `stage_complete`
      事件（`promptTokens` / `cachedPromptTokens` / `cacheCreationTokens`），
      TraceRecorder 零新增管道持久化。命中率 cached/prompt 就是 diff
      要撬动的可观测值。
    - [x] **world-state 分节 + RFC 7386 merge-patch diff** ✅ 2026-08-29：
      `world_state.py`——section 协议（`id` + `snapshot()`）、RFC 7386
      `merge_patch` / `apply_merge_patch`（spec 官方向量全过）、
      `<world-state>` / `<world-state-patch>` 片段（完整快照以 base64url
      HTML 注释嵌入每个片段——记录自包含，resume/fork 零侧通道）、
      `WorldStateHooks`（每轮 round 0 注入：无基线全量、有基线只追加
      变化节的尾部 patch、未变零 token；压缩折掉片段则自愈式全量重注）。
      sidecar `worldState` 参数接入（宿主改传数据节，不再每轮重建系统
      提示词）。**同波修复 W1 播种缺口**：生产宿主每轮从自身 DB 重建
      有损视图（无 tool 轮、无注入片段、assistant 文本被展示变换），
      严格前缀检查误判 `host_revision` 并重存全量种子——改为记录感知
      播种：continuation 时从记录投影 + 宿主新尾巴播种（user/system
      精确比对、assistant 前缀容忍展示追加），模型跨轮记住 tool 轮与
      注入片段，world-state diff 在生产真正生效；`HistorySeed` 带
      `message_kinds` 保真 fork 后的 reconciliation。
    - [x] **工具曝光分层** ✅ 2026-08-29：`ToolExposure` 三层
      （`direct` / `deferred` / `hidden`，codex `tool_executor.rs` 同构）
      落在 `RegisteredTool` 上，`@tool` / `register` / `register_remote`
      全链路透传；`describe_model()` 只出 direct 层（模型可见列表有界），
      `describe()` 保留全量供宿主自省；dispatch 不按层设卡（发现即可调）。
      deferred 层的发现缝是 `tool_search.py`：`register_tool_search` 注册
      一个 direct 层搜索工具，AND 语义关键词匹配 deferred 名录（name 命中
      权重高于 description），返回完整 schema，命中即调，结果有界
      （默认 5，封顶 20）。hidden 层不进搜索、不进 unknown-tool 建议
      （不再泄漏进模型可见错误文本）。
    - [x] **MCP** ✅ 2026-08-29（前置全齐后落地）：`mcp.py`——
      ① 确定性命名 `qualify_mcp_name` / `parse_mcp_name`
      （`mcp__<server>__<tool>`，server 名禁含分隔符，qualified 名超 64
      字符即 loud fail）；② `register_mcp_catalog` 每 server 目录上限
      （默认 64，超帽 loud fail 且原子——先全量校验再注册，不留半注册
      状态），目录默认 deferred 层（模型经 tool_search 发现，不为每个
      schema 每轮付 token），invoker 契约 `(未限定名, arguments)` 与
      `register_remote` 同形——host 侧 MCP client 经反向通道接入与
      进程内 client 插法一致；③ `McpStdioClient`：NDJSON JSON-RPC
      2.0 子进程客户端（initialize 握手 / cursor 分页 tools/list /
      tools/call），server 通知忽略、server 主动请求（sampling 等）
      回 -32601 防 wedge，每请求超时（3.10/3.11 的 TimeoutError 分裂
      已处理），非文本 content 以占位符呈现。架构决定不变：桌面产品里
      server 由 host（Electron 主进程）启动，sidecar 不 spawn；
      `McpStdioClient` 服务直接嵌入 runtime 的宿主（CLI / 测试）。
      测试含真实假 server 子进程 + 全链路 e2e（server → 目录 →
      deferred → tool_search 发现 → loop 调用）。
  - [x] **Wave 3** ✅ 2026-08-29 全部落地：审批代数（8 变体决策 + 三种
    持久化域，`Denied{reason}` 回喂模型且与 `Abort` 语义不同；逐类别
    自动拒绝，headless 才不会挂起）→ 工具执行沙箱（先只做
    shell/subprocess）→ AG-UI / ACP transport → 金轨迹评测门禁（复用
    既有 `replay.py` fixtures）。四项交付细节见下方各子条。
    - [x] **审批代数** ✅ 2026-08-29：`approval.py`——8 变体
      `ApprovalKind`（`allow_once` / `allow_for_session` / `allow_always` /
      `deny_once` / `deny_for_session` / `deny_always` / `abort` /
      `timed_out`，codex `ReviewDecision` 同构，其 policy-amendment 变体
      泛化为 durable 域）；三种持久化域 = request（不缓存）/ session
      （`SessionApprovalCache` 按 category）/ durable（`ApprovalStore`
      协议 + `InMemoryApprovalStore` / `JsonApprovalStore` 原子写实现，
      跨 session）；durable 优先于 session（更强的承诺先查）。
      `ApprovalExecutor` 是 ToolExecutor 装饰器——审批立在任意分发路径
      之前（router / host 反向通道 / MCP 一视同仁），不是埋在某个注册表
      里；allow 时经 `ctx.consent_granted` 桥接 router 的
      `require_consent` 门（每个调用仍先过审批装饰器，桥接无法绕过）。
      deny 变体返回失败 ToolResult（`error="approval_denied"` + reason
      进 data）——模型看到 `Denied{reason}` 继续跑；`abort` 抛
      `ApprovalAborted`，loop 像 breaker 一样补齐本批 tool 响应（真实
      结果 + `loop.abort_skip` 合成跳过，不留悬空 tool_call）后以
      failed 收尾——与 Denied 语义截然不同；`timed_out` fail-closed 当
      deny 但保留变体名供观测。`AutoApprover` 是 headless 审批器：按
      mode 逐类别自动允许/拒绝，永不阻塞。sidecar 接线：
      `chat.stream` 新增 `approval: {mode: "auto"|"host", timeoutMs,
      storePath}` 参数（缺省 = 无审批层，行为不变）；host 模式走反向
      通道 `approval.request`（`HostApprover`，宿主不可达/回复非法一律
      fail-closed deny）；session 域按 chatId LRU（64）挂 Sidecar 实例；
      包装在 executor 最内层，子 agent 的工具调用同样过审批。测试 20
      （框架：代数/域/超时/桥接/loop 回喂与 abort 收尾）+ 7（sidecar：
      auto/host/不可达/跨轮 session 携带/缺省不变）。
    - [x] **工具执行沙箱（shell/subprocess）** ✅ 2026-08-29：
      `sandboxed.py`——`SandboxedToolExecutor` 是 ToolExecutor 装饰器
      （与 ApprovalExecutor 同缝）：把 shell 类调用的 `command` 参数重写
      为沙箱化调用串再委托，因此立在任意分发路径之前——桌面部署下被
      重写的命令经反向通道到宿主 shell 执行，宿主零沙箱机制知识即得逐
      exec Seatbelt。`SandboxBackend` 协议（`name` / `enforcement` /
      `wrap_command`）可插拔：sidecar `sandbox.py` 新增
      `SeatbeltExecBackend` 参考实现——复用 layer-1 profile 生成器但按
      工具执行默认收紧（deny-by-default、缺省断网、写限声明根 + 系统
      scratch），profile 内联进命令串（`-p`），不留临时文件、命令由谁
      spawn 都可以。采纳 dsh 的 `SandboxEnforcement: full | partial |
      none` 作为**返回值**：结果 `data["_sandbox"]` 带
      `{backend, enforcement}` 标记进 transcript（模型可见，沙箱拒绝
      与命令失败可区分）；`require_full=True` 时 enforcement 不足 full
      （无后端 / 仅 partial）在执行前拒绝（`sandbox_unavailable`），
      替代现状「大声记日志并继续」。enforcement 判定诚实：断网或
      egress 钉死 localhost = full；出网全开或非 localhost 条目退化为
      按端口 = partial。sidecar 接线：`chat.stream` 新增
      `execSandbox: {enabled, writableRoots, network, allowedHosts,
      shell, tools, commandArg, requireFull}`（缺省 = 不收容，行为不
      变）；包装顺序 base → sandbox → approval → subagent——审批器看到
      的是**原始命令**而非 sandbox-exec 调用串。测试 8（框架：重写/
      标记/透传/require_full 双向/自定义工具集/非命令形调用）+ 10
      （sidecar backend：enforcement 三档判定、sh -n 可解析、**真实
      sandbox-exec 冒烟**——声明根可写、$HOME 写被内核拒绝、缺省断网）
      + 5（sidecar 接线：重写生效/缺省不变/非 shell 不动/requireFull
      无后端拒绝/端到端内核收容）。Linux Landlock 后端是刻意的后续项。
    - [x] **AG-UI / ACP transport** ✅ 2026-08-29：两个生态信封作为
      peer transport 落地，自研 `stream.chunk` 面保留给 DeepPath 字节
      兼容。**AG-UI**（`ag_ui.py`）：`AgUiRenderer` 把 LoopEvent 投影为
      AG-UI 事件流（依赖官方 `ag-ui-protocol` pydantic 模型，不手搓
      dict）——content/reasoning 增量开闭 TEXT_MESSAGE_/REASONING_
      MESSAGE_ 段（工具调用先关消息段，段间不交错）；tool_call_start
      展开为 START+ARGS（loop 带全量参数，单发）+END；result/error 进
      TOOL_CALL_RESULT（AG-UI 无 tool-error 事件，错误乘 result
      content）；completion 按 status 收 RUN_FINISHED / RUN_ERROR；
      stage/hook/steer/budget 等框架观测事件一律 CUSTOM
      （`steerable.<kind>`，无损且不冒充 AG-UI "step"）。HTTP  serving
      归嵌入方 web 层，`encode_sse` 渲染字节流。**ACP**
      （`acp_adapter.py`，依赖官方 `agent-client-protocol` SDK v0.12）：
      `SteerableAcpAgent` 实现 `acp.Agent` 稳定核心——initialize /
      new_session / prompt / cancel / close_session；session↔chat_id，
      多轮靠 loop 自己的 record-aware seeding（适配器只维护
      user/assistant 文本的宿主视图，record 投影补齐 tool 轮次）；
      content→AgentMessageChunk、reasoning→AgentThoughtChunk、工具→
      ToolCallStart/Progress；completion→end_turn，cancel→cancelled，
      failed 先把 reason 作为最终 agent 消息发出再 end_turn。provider
      配置走环境变量（编辑器 spawn agent 的方式）；session 加载/fork/
      mode RPC 与 editor 终端/文件反向桥（agent 工具借编辑器 terminal
      执行）是记录的后续项。入口 `steerable-sidecar-acp` =
      `python -m steerable_sidecar.acp_adapter`（stdio）。测试 11
      （AG-UI：生命周期/推理族/工具序列/消息边界/失败收尾/CUSTOM 映射/
      SSE 编码/全 loop 投影）+ 7（ACP：capabilities/文本流/工具转发/
      未知 session/cancel/多轮 record 播种/失败原因可见）。
    - [x] **金轨迹评测门禁** ✅ 2026-08-29：`tests/golden/*.json` +
      `test_golden.py`——每个场景用脚本化 provider + 工具表（+ 可选
      approval/sandbox 装饰器）驱动**真实 CoreLoop**，钉死发出的轨迹：
      逐轮 `step_decision`（round/traceStepId/finishReason/toolCalls/
      toolErrorCount/textLength + 决策 status）、工具结果序列、终局
      completion、以及（接 storage 的场景）持久记录的 kind 序列。六场
      景钉住 Wave 1-3 行为面：basic_tool_round（**直接复用**跨语言
      replay fixture `basic.json` 的步序列，两族 fixture 同一事实源，
      有联动测试防漂移）/ approval_deny_feedback（Denied 回喂后继续）
      / approval_abort（abort 收尾 + `loop.abort_skip` 补齐，不留悬空
      tool_call）/ sandbox_marker（命令重写 + `_sandbox` enforcement
      标记进 transcript）/ unknown_tool_recovers / budget_exhausted_
      rounds。record 模式（`STEERABLE_GOLDEN_RECORD=1`）只重写 golden
      段，纪律同快照：重生成必须人工 review——没人审的金轨迹是被祝福
      的回归。与既有 replay 测试的分工：crosslang fixture 门 **reducer**
      （含 hand-authored/fuzz 的鲁棒性场景），金轨迹门 **loop 本身**
      发出的轨迹。审查记录：终局 stop 轮的 toolErrorCount 沿用
      consecutive 计数（不归零）与 budget 退出轮复用 round 序号均为
      现状语义，门禁如实钉住。
  - [x] **Wave 4（接线，零新机制）**：第六轮复审（见下）发现 Wave 0-3 造的
    九项机制里桌面只开了四项。这一波**不加任何机制**，只做两件事——把已建
    的插上电（W4-1/2/3），补 Wave 2 只做了读侧的写侧（W4-4）；外加三个第五
    轮就记录、至今未动的小口子（W4-5/6/7）。**全部完成**：回测
    deeppath-agent 292 过 / steerable-framework 629 过 / build 绿。
    - [x] **W4-1 桌面接线审批代数**（P0，是真产品活不是纯管道）：
      `coreloop-stream.ts` 下发 `approval: {mode: "host", timeoutMs,
      storePath}`，反向通道 `approval.request` 在 sidecar 侧已就绪，宿主
      只需应答——**难的是 UI**：8 变体 × 3 持久化域要在 Electron 里可表达，
      否则 host 模式实际退化成 `allow_once`/`deny_once` 两个按钮，代数白造。
      验收：拒绝一次 shell 调用，模型收到 `Denied{reason}` 后继续跑；abort
      一次，transcript 里无悬空 `tool_call`。
      **落地**：`reverse-approval.ts` 桥（fail-closed：无窗口/超时/非法
      决策一律 `deny_once`）+ `ApprovalModal.tsx` 七按钮模态 +
      `~/.steerable/approvals.json` 持久化；`STEERABLE_APPROVAL=0` 退回旧
      行为。8 变体中 `deny_for_session` 由宿主桥表达（store 三域在框架侧）。
    - [x] **W4-2 桌面接线 `execSandbox`**（P0）：`coreloop-stream.ts` 下发
      `execSandbox: {enabled: true, writableRoots: [工作区], network,
      allowedHosts, requireFull}`。**`requireFull` 的缺省值需要产品决策**：
      true 会在无 Seatbelt 的平台上直接拒掉工具调用（诚实但可能砸用户流程），
      false 则退回「标记了 partial 但照跑」。验收：桌面跑一次
      `local_exec_shell`，结果 `_sandbox` 标记 `enforcement=full`，且写
      `$HOME` 被内核拒绝。
      **落地**：`requireFull: false`（诚实降级，`_sandbox.enforcement` 上
      工具卡）；`writableRoots` 取会话项目绑定；顺手补了一个真缺口——
      CoreLoop 反向通道的 `tool.invoke` 原来丢 `projectRoot`，项目模式围栏
      对 sidecar 驱动的回合不生效，现按 `context.chatId` 逐调用解析。
    - [x] **W4-3 sidecar 沙箱与出网白名单转缺省开**（P0）：去掉
      `STEERABLE_SIDECAR_SANDBOX=1` 的 opt-in，改为缺省开 + 显式关，
      `allowed_hosts` 由 provider `baseUrl` 推导。**W0-3 记录的 sbpl 限制
      在这里变成决策点**：按主机名的 enforcement 在 Seatbelt 里不可表达，
      缺省态要么接受按端口降级（弱），要么起本地 egress 代理并只声明
      `localhost:<代理端口>`（强，但多一个进程）。验收：
      `docs/spec/safety.md` 的「当前产品姿态」一节可以整节删掉。
      **落地**：缺省开 + `STEERABLE_SIDECAR_SANDBOX=0` 显式关；白名单按
      `baseUrl` 逐次启动推导（远端主机名按 sbpl 限制降级为端口级，safety.md
      姿态节已重写为「三层全开 + 各层逃生门」而非删掉——限制本身仍需记录）。
    - [x] **W4-4 `cache_control` 发射**（P1，Wave 2 的另一半）：四家里我们
      是唯一只读不写的。抄 pi 的三锚点——系统提示词 / 最后一条工具定义 /
      transcript 尾部，压缩摘要那一次请求走 `retention: none` 且**只作用于
      该次请求**。类型侧无需再动：W1 落的 `ContentPart` 已支持逐 block 注解
      （这正是当初把 `content: str` 拆开的理由之一）。验收信号就是已有的读侧
      仪表——`cachedPromptTokens / promptTokens` 命中率。
    - [x] **W4-5 子代理工具域收窄**（P2，第五轮记录至今未动）：
      `subagent.py:107` 现在是 `self._inner if allow_tools else _NoTools()`
      的二元选择，子代理因此不是权限边界。抄 dsh 的 `toolFilter` →
      `tools.restrict()`，让只读研究型子代理**按构造**打断 lethal trifecta；
      子代理审批钉死为拒绝（dsh 的 `never`）一并做。
    - [x] **W4-6 持久记录格式版本 + 读策略**（P2）：`history.py:519` 现在
      对未知信封直接 `ValueError` 硬失败，触发场景是桌面用户降级。加版本
      字段 + 采纳 dsh 的 fail-closed 读策略——注意**不要**抄它 08-25 删掉的
      `ignorable`：dsh 是想清楚后选择更严，我们现在是「没做」而不是「选择
      了更严」。
    - [x] **W4-7 注入内容设界**（P2）：`SpillHooks` 进 sidecar 缺省钩子链
      （现在造了但不在链上）；skill catalog 加聚合上限；world-state 与 steer
      的上限一并补。参照 codex 的 hook 输出 2500 token 硬顶 + 落盘。
    - [x] **W4-8 实测修出的两个产品 bug**（2026-08-29 桌面实盘 CDP 驱动验证
      发现，单测不可见）：① sidecar 的 httpx 走环境/系统代理
      （`getproxies()`），出网白名单只写了 provider 端点 → 代理用户全量
      LLM 调用被 EPERM；修复为白名单 = provider 端点 ∪ 环境代理 ∪
      macOS 系统代理（`scutil --proxy`）。② 审批模态原挂在 chat 视图层，
      用户切走页面即永不渲染、只能等超时 fail-closed；改挂 AgentLayout。
      实盘全绿：审批请求 → 模态渲染 → 真实点击「允许一次」→ 工具执行
      （工具卡 `sandbox: {enforcement: full, backend: seatbelt}`）→ 答案
      基于真实工具输出 → trace 落库。
    - [x] **W5-1 Linux 逐 exec 沙箱后端（bwrap）**（2026-08-29）：
      `BwrapExecBackend` 落进 `sidecar/sandbox.py`，profile 抄 dsh 已证
      最小集（只读根绑定 + 私有 PID ns 配自有 /proc——procfs magic link
      逃逸必修 + 私有 /tmp tmpfs + die-with-parent + 默认 unshare-net）。
      可用性 = **功能性 probe**（真实 maximal wrap 跑 no-op，lru_cache），
      不是版本/平台检查——实测 Docker Desktop VM 连 CAP_SYS_ADMIN 都拒
      pivot_root（仅 --privileged 通过），版本检查会误判。后端选择收敛为
      `select_exec_backend` 阶梯：macOS→Seatbelt，Linux→bwrap（probe 门控），
      其余→None（requireFull 拒绝）。bwrap 无语义化出网白名单：
      network=false→full，true→partial，allowedHosts 接受但不执行
      （文档写明，与 Seatbelt 端口级降级同一补救：本地 egress 代理）。
      Windows 不构造后端（受限令牌是宿主侧 spawn 支持，不是命令包装器，
      不适配 rewriter 架构；记录在 safety.md 待后续）。验证三层矩阵：
      默认 Docker（probe 拒→全 none，fail-closed 正确）、SYS_ADMIN
      （仍拒）、--privileged（4 个真禁锢测试全绿：声明根可写/越界拒/
      断网//proc/1/root 不逃逸/宿主进程表不可见）。本机 728 全绿。
    - [x] **W5-2 会话分支（record 级 fork 树 + 非破坏 regen）**（2026-08-29）：
      模型取 codex/dsh 的 fork 形（分支 = 新 record + seed 溯源），不抄 pi 的
      日志内 entry 树——steerable 的 record 是 append-only 线性日志，分支在
      record 粒度发生，源 record 永不变异。框架新增 `branch.py`：
      `fork_record`（一次性原语：load 前缀→写带溯源/ kinds 的 HistorySeed→
      返回 ForkResult{point, messages}）、`branch_label`（确定性免 LLM 摘要：
      fork 点最后一条 user 消息预览）、`resolve_fork_seq`（语义寻址：
      before_last_user=regen 地址；user_index=K 按 user 消息序数寻址——
      桌面 regen 的截断点是消息序数不是 record seq；seed 内 user 消息计入
      序数但不可寻址，序数落进 seed → None → 宿主降级）、`lineage`
      （向上走 seed 溯源链，环/超深 fail-loud）。sidecar 新增
      `agent.session.fork`（不跑回合的纯分叉 RPC）与 `agent.session.branches`
      （lineage 恒可用；children 靠存储的可选 `list_history_records` 扩展——
      InMemory/SQLAlchemy 已实现，无此能力的存储降级为仅 lineage）。
      **桌面采纳（修真实 bug）**：此前 CoreLoop 聊天的 regen 截断桌面库后
      继续往同一 record 追加——durable log 里新旧两条尾巴交织无标记。
      现在 regen 先 `session.fork`（beforeUserIndex 寻址）再截断：旧尾
      完整留在源 record、可经 branches RPC 发现；chat→活跃 recordId 映射
      存 settings_kv（重启不丢），后续回合显式传 recordId。fork 失败
      （无 record/序数落进 seed）降级为旧截断路径。验证：框架 756 全绿
      （branch.py 19 测 + sidecar 两 RPC 4 测），desktop vitest 308 全绿
      （fork 序数纯函数 4 测）+ tsc 干净。仍开：pi 式会话内树 UI（需
      entry 级 parent 链 + 渲染层设计，等真实需求）。
    - 记录但不排期：**供应链/发布完整性**（pi 的依赖钉死、
      `min-release-age=2`、shrinkwrap、`--ignore-scripts`、OIDC 可信发布、
      发布前隔离冒烟）——我们从未把它当作一轴考虑过，值得借鉴但不阻塞产品。

- **Wave 6 · 第七轮四方复审后的产品面补课**（立项 2026-08-30）。
  背景：前六轮的 13 轴全部落在 CoreLoop 内核（cache、循环韧性、沙箱、审批、
  伪调用恢复），恰是我们投入最深处，「13 轴全 par/lead」有选题效应。本轮改用
  客观立轴规则——**三个对照框架里 ≥2 家有可指认的实质实现才成轴**，只有一家的
  归「单方特有」不计缺口——扩到产品面与工程面重测 16 轴：**9 项落后、
  2 项有机制未接线、5 项追平**。缺口集中在工具原语与产品交互，不在推理内核。
  基线 codex@0b45b17 / dsh@cd5ef81 / pi@853a80d。详见
  canvas `steerable-r7-product-axes`。
  - **P0 真实产品痛点（用户当下就在踩）**
    - [ ] **W6-1 结构化文件编辑**：现状是 `path + 全文件 content` 覆盖
      （`local-executor.ts` 的 `writeLocalFile`、`workspace_tools.py` 的
      `write_file`），改长文件必须让模型重述全文——烧 token 且一次生成失误
      整段丢内容，也无从发现文件被外部改过。三家都做了结构化编辑：codex
      `apply-patch` 的 `seek_sequence` 三级降级定位（精确→去空白→Unicode
      标点归一）、pi `edit-diff` 的批量 `edits[]` 逆序替换禁重叠、dsh 用
      `FsVersion` 不透明版本标识强制 read-before-write。做：`edits[]` 批量 +
      三级模糊定位 + mtime/摘要冲突检测 + tmp&rename 原子写；工具卡渲染
      unified diff。
    - [ ] **W6-2 follow-up 输入队列**：agent 在跑时用户再发一条只能走 steer
      （轮内注入、有字符上限），turn 一结束未消费内容直接丢弃——没有「本轮结束
      后自动作为下一轮发出」的通道。这是可复现的产品缺陷而非能力差距。三家都把
      steer 与 follow-up 拆成两条队列（codex `Steer|Mailbox` 双模并禁止 steer
      审查/压缩轮；dsh 按 `target=next-turn|next-step` 分流；pi 直接给用户
      `steeringMode`/`followUpMode` 两个 `all | one-at-a-time` 开关）。
    - [ ] **W6-3 多模态接线**：`ImagePart` 的类型 / wire / 双厂商序列化 /
      token 估算全链路早已打通，桌面拖拽却只把**文件路径**写进文本——典型的
      W4 式接线断层，成本最低收益最直接。做：附件读成 base64 `ImagePart` +
      尺寸/字节上限 + 超限缩放并把说明注入上下文（codex 的做法）。
  - **P1 安全与合规（有明确风险）**
    - [ ] **W6-4 凭据脱敏对齐 spec**：spec 明写记录器要过 `sanitize_for_trace()`，
      而**该函数根本不存在**，`TraceRecorder` 只按长度截断；默认 preset 里还有
      硬编码 key。规格与实现不符本身即缺陷。做：实现并接入脱敏 + 清掉硬编码 key +
      补一条「密钥不得进 trace」的机械门禁测试。
    - [ ] **W6-5 项目信任门控**：现在打开任意项目目录就会加载其技能与规则文件，
      没有信任门。pi 把这条设成安全边界（`project-trust.ts`：未信任前只加载
      user/CLI 扩展，项目扩展与 settings 一律延后，`trust.json` 持久化）。
      做：首次绑定项目时询问、未信任仅加载用户级技能、可在设置里撤销。
    - [x] **W6-6 遥测合规化** ✅：`otel.py` 加 `PrivacyMode`（`full`/`metadata`）
      + `export_trace` 一键导出；脱敏 waterfall 双段——record 时（W6-4
      `sanitize_for_trace`）+ export 时再脱敏（防旧/非 conforming 路径漏 key）；
      `metadata` 档只出结构/时延/状态、丢 payload 正文与自由属性。sidecar 新增
      `trace.export` RPC；桌面 `telemetry-settings.ts`（默认关=无 endpoint）+
      `settings_kv` 持久化 + `/api/v2/local-settings/telemetry` IPC + 设置页
      TelemetrySettingsPanel + 每轮结束按档位导出（失败不阻断主流程）。测试：
      otel 7 项 + sidecar export 1 项 + telemetry-settings 11 项全绿。
  - **P2 能力扩展（排在痛点之后）**
    - [x] **W6-7 `world_state` 扩面 + 规则文件加载** ✅：
      - **W6-7a 规则文件加载**（见 W6-5，与信任门控同落地）：`project-rules.ts`
        向上遍历 `AGENTS.md`/`CLAUDE.md`（有界、root-first、覆盖优先级、截断），
        信任门控后注入系统提示词【项目规则】节。
      - **W6-7b world_state 扩面**：桌面 `buildWorldState` 从只有 time 扩到
        `time`/`mode`/`permissions`/`skills` 四节（permissions=approval 模式+
        沙箱可写根/网络；skills=活跃条件+模式级排除的紧凑过滤面，正文仍走分层
        披露）。sidecar 逐节 merge-patch diff，没变零 token。coreloop-stream
        12 项测试全绿。
    - [x] **W6-8 结构化模型能力** ✅：新增 `model_info.py`——`ModelInfo`
      （`context_window`/`modalities`/`tool_format`/`reasoning_levels`，派生
      `supports_tools`/`supports_vision`）+ 内置表 `MODEL_INFOS`（最长前缀匹配，
      窗口值与 deeppath-api 对齐）+ 运行时 `register_model_info` 覆盖（模型迭代
      不再等框架发版）。`resolve_context_window` 改为委托 `resolve_model_info`
      （行为不变，test_tokens 全绿）；`STEERABLE_REASONING_EFFORT` 由裸 env 直通
      改为 `clamp_reasoning_effort` 按模型能力夹取（无 reasoning 档位的模型一律
      不发该参数，避免 strict API 报错）。model_info 12 项 + wire-helpers 25 项全绿。
    - [ ] **W6-9 用量与成本归因**：已有 token/步数/工具预算，缺金额估算与用量
      面板。codex 逐轮美元成本 + rate limit 阈值警告 + `/status`；pi 按
      model/provider 与工具/摘要分桶并常驻 footer。
    - [ ] **W6-10 压缩双层去重核查**：sidecar 回合内 `CompactionHooks` 与桌面
      跨回合滚动摘要职责不同，但可能对同一段对话重复压缩——需要构造长会话实证，
      必要时让桌面摘要跳过已被 `CompactionBoundary` 覆盖的区间。
  - **记录不排期**：多 agent 编排（codex MultiAgentV2 / dsh workflow+Agent Teams
    投入大，pi 核心也没有，等真实需求）、通用第三方插件 runtime（投入大且安全面宽，
    我们的技能 + MCP 已覆盖主要场景）、LSP（只有 dsh 一家，不成轴）、桌面
    autoUpdater（先定发布策略）。
  - **本轮追平项（不需动作，仅备查）**：上下文压缩策略、长驻 PTY（我们有完整
    node-pty 实现，pi 甚至没有 PTY 抽象）、MCP 客户端、崩溃恢复语义（与 dsh
    同源）、供应链完整性（框架侧 lockstep + provenance + 代码签名已不弱，
    桌面缺自更新通道）。
- **明确不做**：Cordis 式插件运行时（装饰器链 ~40 行已给到 provider 替换，
  抄**缝的纪律**不抄运行时）；workflow 编排（dsh 自己 README 写明无
  journaling / 无 resume / 仅前台）；durable execution（等消费者提需求）；
  启发式 token 估算的进一步投入（本地模型场景之外，厂商正在服务端吸收）。

- [x] **Wave 0 · 三个前置（全 S）** ✅ 已完成（2026-08-28）。交付：
  - **W0-1 `RecordingProvider` + 提示词断言**（`agent-runtime/py`
    `recording.py`）：包装任意 `LLMProvider`，发送前快照每次出站请求
    （messages + params，provider 报错也留痕）；sink 两枚——
    `InMemoryRequestSink`（测试）与 `JsonlRequestSink`（JSONL 追加，
    房屋格式，供 E2E harness tail）。断言即「不重写历史」的可执行形式：
    `assert_stable_prefix`（第 n 次请求须为第 n+1 次的前缀，
    `compaction_boundaries` 声明压缩豁口——**今天的压缩/重试改写路径
    会触发它，这是刻意的**，Wave 1 靠它防静默劣化）与
    `assert_bounded_items`（逐项硬顶，默认 10k token，用压缩层同一个
    估算器）。sidecar 侧经 `STEERABLE_REQUEST_RECORD_PATH` 环境变量
    挂载（默认关，录制含完整提示词），与校准包装同一模式。
  - **W0-2 逐工具超时**：`LoopConfig.tool_timeout_ms`（默认 300_000，
    挂死兜底而非预算；`None` 关闭；非正值 `ValueError`）。loop 在
    executor 端口上包 `asyncio.wait_for`（串行与并行 gather 两个调用点
    都包），超时**返回**失败 `ToolResult`（`error="tool_timeout"`）而
    不上抛——走正常结果路径，连续错误熔断原样生效；对一切 executor
    生效（进程内 / 反向通道 / 未来 MCP）。迟到响应由
    `JsonRpcServer` 的 pending 表自然丢弃，已核实无泄漏。sidecar 经
    `toolTimeoutMs` 请求参数覆盖（与 `softTimeoutMs` 同一通道）；
    agent 侧 `SidecarChatStreamRequest` 类型镜像同步补字段。
  - **W0-3 出网白名单（安全第一阶段）**：`build_seatbelt_profile(
    allowed_hosts=...)` + CLI `--allow-host HOST[:PORT]`。语义：未配置
    （`None`）→ 全开（默认不变，老用户不破）；已配置（含空列表）→
    fail-closed，除声明端点外全部拒出网；裸 host 放 443+80；非法条目
    生成期 `ValueError`（profile 注入按主机名字母表拒绝）。DNS/TLS 的
    mach 服务保留（本地服务非出网，否则连白名单主机都解析不了）。
    **对 roadmap 的一处偏差**：sbpl 的 `remote` 过滤器只接受 `*` 或
    `localhost`（macOS 26 实测：主机名与 IP 字面量在编译期即被拒），
    按主机名的出网控制在 Seatbelt 里**不可表达**——localhost 条目精确
    钉 `localhost:PORT`，其余条目降级为按端口（`*:PORT`），生成的
    profile 自带注释声明此限制。该限制已写进 `docs/spec/safety.md`：
    真按主机名 enforcement 的正解是本地白名单 egress 代理 + 只声明
    `localhost:<proxy 端口>`（白名单把 sidecar 钉到代理，代理持有主机
    列表）。agent 侧 `SidecarStartOptions.sandboxAllowedHosts` / 环境
    变量 `STEERABLE_SIDECAR_SANDBOX_ALLOWED_HOSTS`（逗号分隔）透传。
  - **测试**：框架 py 新增 34 例——recording 15（快照语义 / 出错也录 /
    JSONL 往返 / 前缀断言通过+改写+收缩+边界豁免 / 上限断言 / 干净
    CoreLoop 双不变量集成）、tool timeout 8（挂死工具→失败结果且整轮
    完成 / 熔断仍触发 / 任意 executor 含远程 / 并行批隔离 / None 关闭 /
    限内慢工具 / 默认值 / 非正值拒绝）、sandbox 8（未配置全开 /
    localhost 精确钉 / 远程降级端口 / 去重 / 空列表全拒 / 非法条目 /
    CLI / **真实 sandbox-exec 冒烟**：声明端口连通、同机未声明端口被
    内核拒绝 Operation not permitted）+ provider factory 2 + loop config
    接线 1；全量 433 过。agent 侧 supervisor 沙箱测试 11 过（+4：
    默认无 flag / option 透传 / env 兜底 / option 压 env）。
  - **E2E 回测（2026-08-28 晚，框架 `23b0c92` + agent `869eea7`，
    gpt-oss:20b-cloud）**：五轮有效全量——沙箱关 44/47、45/47；
    沙箱开 46/47（single 阶段 25/25 全过）、44/47、44/47。全部失败
    均为已知模型抖动签名（空/短回复，失败消息下标逐轮漂移
    msg[0]/[6]/[7]/[9]/[10]，一次 ollama HTTP 500，一次慢推理触发
    cancel 兜底），落在 W0 之前基线（22/25～25/25 同款失败集）包络
    内，**零回归**。`tool_timeout` 五轮零触发（300s 兜底未被任何
    正常消息触及）。录制链路双沙箱态端到端验证：共录 233 个真实
    请求，`assert_stable_prefix` 对全部 62 条重建回合链通过
    （回合内追加-only 成立）；`assert_bounded_items` **如期触发**
    ——`local_exec_shell` 对 node_modules 跑 `grep -R` 产出
    ~68k token 结果（宿主工具截断上限远高于 10k），正是 tripwire
    要暴露的既有问题。**意外收获（既有 app 侧问题）**：多轮回合的
    当前用户消息在下一回合首个请求里被重复注入（`router.ts` 去重
    时序——上一条 assistant 回复在新用户消息落库后才写入）；另见
    一条 assistant 消息跨回合内容变化（321→102ch，hygiene-n 续写
    所致）。**两问题已于当日晚修复**（agent `develop`）：用户消息
    改按 id 剔除（`history-helper.dropCurrentUserMessage`，竞态
    免疫），工具结果在反向通道边界复用 `compactToolResultJson`
    封顶 8000 字符；修复后录制复验：82 请求零重复、
    `assert_bounded_items` 通过。assistant 跨回合内容变化留 W1
    （持久记录与展示流分离后自然消解）。
  - **遗留（Wave 1+）**：`assert_stable_prefix` 对压缩/重试改写的失败
    是刻意的 tripwire，Wave 1 落追加式历史后翻默认；hook 输出设界与
    逐注入项设界（roadmap 另两行）未动；按主机名出网 enforcement 等
    第三阶段 `SandboxedToolExecutor` / 代理方案；`SandboxEnforcement`
    返回值化（dsh 借鉴）未做。

- [x] **Wave 1 · 地基** ✅ 已完成（2026-08-29 成文当日落地，六步全绿）。
  范围按 roadmap「一个项目不是三个」：append-only 历史、ContextFragment、
  持久记录、resume O(tail)、`content: str` → parts 同波落地。现状勘察与
  影响面已核（全部 `file:line` 经 2026-08-29 双人复核）：

  - **根因复述**：模型可见 transcript 是可变 `list[LLMMessage]`
    （`loop.py:370` 建表后就地改写 11 处：steer `:424`、软超时 `:489`、
    pre_step 整表替换 `:514`、overflow 重试替换 `:634`、纪律重试
    `:743-751`、narration `:773/:966`、assistant `:801`、tool 结果
    `:940/:998`）；`self.trajectory` 与 trace 展示流各走一路，
    `resume.py:71` 再从展示流重建**第三份**（300 字符 preview 有损）。
    三份记录无强制一致。

  - **设计六件**：

    1. **`HistoryItem` 信封 + `ContextManager`**（新建
       `agent-runtime/py/.../history.py`）：`HistoryItem` 冻结 dataclass
       ——`seq`（记录内单调序号）/ `turn_id` / `kind`（`<feature>.<name>`
       分类，对应 codex `ContentItemKind`）/ `message: LLMMessage` /
       `token_estimate`（追加时用 compaction 同一估算器算好，设界与
       压缩压力零重算）。`ContextManager` 拥有记录并产出投影：
       `append()` / `projection()` / **`replace_all(items, *, reason)`
       是唯一声明式重写路径**——它不修改记录，而是追加一条
       `compaction_boundary` marker item（记录本身永远只追加），投影时
       跳过被 supersede 的区间。loop 内 11 处就地改写全部改为
       manager 调用；`ctx.last_prompt_transcript_len` 改为
       「上次请求时的 item seq」，重写不再让索引漂移（boundary 自带
       seq 区间）。
    2. **`ContextFragment`**（同模块，对应 codex
       `ContextualUserFragment`，`codex-rs/context-fragments/src/fragment.rs:64-119`）：
       Protocol——`role` / `content_kind` / `markers()`（首尾稳定标记）
       / `body()` / `render()` / `matches_text()`（在保留历史里认出
       自己的渲染）。loop 全部内建注入改为带标记 fragment：
       `_SOFT_TIMEOUT_NOTICE` / `_DISCIPLINE_RETRY_NOTICE` /
       `_NARRATION_REQUEST` / `_BREAKER_SKIP_MESSAGE` / 压缩
       `_SUMMARY_MARKER` / `_FOLDED_TOOL_MARKER` / steer 注入。
       收益：resume 可识别、压缩可感知、测试可断言「模型看到了什么」。
    3. **`pre_step` 只追加化**：`PreStepAction` 去掉 `transcript` 整表
       替换，改为 `appends: list[...]`（只增）+ `rewrite: RewriteRequest
       | None`（声明式：新 items + reason + action 标签）。loop 是唯一
       写者：appends 经 `manager.append`，rewrite 经
       `manager.replace_all`（自动落 boundary marker）。ChainHooks 保持
       纯组合：工作投影在线程内传递，每个 hook 看到前序效应后的投影，
       最终合并为（rewrite?, appends）——「hook1 改写、hook2 追加」
       语义等价于先 replace_all 再 append。`RetryAction.transcript`
       同理改为 `rewrite`。skills 注入（`skills.py:521`，首轮改写
       system）改为**追加一条独立 system fragment**（首轮、任何请求
       发出前，cache 安全；OpenAI/Ollama 原生接受多 system，
       Anthropic 侧本就把 system 拼接为单独参数）。
    4. **持久模型可见记录**（对应 codex rollout + 持久化策略
       `codex-rs/rollout/src/policy.rs`；通道按决策②走专用方法组）：
       `StorageAdapter` 新增 `append_history(record_id, entries)` /
       `list_history(record_id, *, after_seq, until_seq, limit, reverse)`
       ——ContextManager 每次 append/replace_all 时以全保真落盘
       （完整 content，不是 300 字符 preview），与展示流
       （content_delta 等）**数据各自写、互不推导**。记录按 chat
       一条连续 append-only 日志（`record_id` 默认取 chat_id），
       跨 run 不碎；standalone run（无 store）纯内存，行为不变。
    5. **resume O(tail) + 全保真**：`load_transcript` 改走
       `list_history` 反向扫描，到最近 `compaction_boundary` 即停，
       从该 boundary 投影尾部——模型续跑看到的历史与它上次所见
       **逐字节一致**（不再有 preview 失真）。fork/regenerate 开新
       record_id，以 `until_seq` 前缀投影作为首条 `history_seed`
       条目内联播种（有界——压缩保证工作集有界），任何 record
       都自包含、单日志可读、无引用链。
    6. **`content: str` → content parts**（Tier 1 同波）：
       `LLMMessage.content` 改为 `list[ContentPart]`（`TextPart` /
       `ImagePart` 起步，冻结 dataclass + 判别标签）；提供
       `LLMMessage.text_of()` 便捷构造与 `content_text` 投影属性覆盖
       纯文本常客。provider 序列化：OpenAI 兼容侧**纯文本退化为
       string 简写**（与今日线上字节一致，prompt cache 与录制对比
       零扰动），多模态才用数组形；Anthropic 侧本已用 blocks，顺直。
       tokens.py / recording.py / compaction / spill / resume 全部
       改在 parts 上操作。

  - **三个决策点（2026-08-29 已拍板）**：
    ① **wire `ChatMessage` 加性扩展**（采纳建议）——schema 增加可选
    `parts` 字段，`content: string` 保留为纯文本投影（agent-ui /
    deeppath 零破坏；sidecar `_coerce_messages` 优先读 parts）。
    破坏性只落在 Python 运行时 `LLMMessage`（deeppath-api 一个消费者，
    随框架升级迁移一次）。
    ② **记录通道：新开 StorageAdapter 方法组**（物理分离，不复用
    trace 事件流）——`append_history` / `list_history`（支持
    `after_seq` / `until_seq` / `limit` / `reverse` 尾部扫描），
    InMemory + SQLAlchemy 两实现仓内同步；下游适配成本已接受。
    ③ **跨回合记录：连续日志 + 内联播种**——记录按 chat 一条连续
    append-only 日志（跨 run 不碎），resume 反向扫同一条日志即停，
    无跨 trace 递归；fork/regenerate 开新 record_id，以前缀投影
    （有界，压缩保证）作为首条 `history_seed` 条目内联播种。

  - **落地序列**（每步独立 commit、测试全绿才进下一步——roadmap
    「incremental but one project」）：
    - [x] `history.py`：HistoryItem / ContextManager / ContextFragment
       基建 + loop 内部行为不变接入（事件流零变化）；
    - [x] content parts：LLMMessage + 两个 provider + tokens + recording +
       compaction/resume/skills/spill 适配（纯文本字节等价）；
    - [x] wire 加性 `parts` + 双端 codegen + sidecar  coercion + 一致性测试；
    - [x] hooks 翻转：PreStepAction/RetryAction 只追加 + 声明式 rewrite，
       compaction 与 skills 迁移，loop 11 处改写点收口；
    - [x] 持久记录 + resume O(tail)：history_item 事件、持久化策略、
       storage 尾部扫描、sidecar fork/resume 切换；
    - [x] tripwire 翻绿：`assert_stable_prefix` 默认零声明边界通过
       （压缩回合的 boundary 从记录自动导出，不再手工传下标）；
       文档（docs/spec/core-loop.md 等）+ 本文件条目勾选。

  - **测试计划**（新增集中在 `agent-runtime/py/tests/`）：
    manager 单测（append/projection/replace_all boundary 语义）；
    fragment render/matches 往返；parts 序列化（OpenAI 纯文本 string
    简写等价、多模态数组、Anthropic blocks）；resume 保真（续跑投影
    == 上次所见）与 O(tail)（大 trace 只读尾部）；**集成 money test**：
    干净 CoreLoop 全程录制 → `assert_stable_prefix` 零边界声明通过、
    压缩回合 boundary 自动对齐；`assert_bounded_items` 覆盖全部
    fragment 注入。既有 433 测试随步骤 2/4 适配迁移。

  - **明确不做（本 wave）**：cache_control 逐 block 注解与 cache 仪表
    （Wave 2 第一件事，parts 为它开好门）；world-state diff；MCP；
    `SSEEvent.content` 保持 string（展示流 delta 不是历史 parts）；
    agent-ui 渲染层不动。

  - **交付记录（2026-08-29，六步各独立 commit）**：
    - **步骤 1 `history.py` 基建 + loop 接入**（`318b93f`）：`HistoryItem`
      信封（seq/turn_id/kind/token_estimate）+ `ContextManager`
      （`append`/`projection`/`replace_all` 唯一声明式重写路径，落
      `CompactionBoundary` marker）+ `ContextFragment`（markers/body/
      render/matches_text，内建注入全部带稳定标记）；loop 11 处就地
      改写全部收口为 manager 调用，事件流零变化。
    - **步骤 2 content parts**：`LLMMessage.content: list[ContentPart]`
      （`TextPart`/`ImagePart` 冻结 dataclass），`text_of()`/
      `content_text` 覆盖纯文本常客；tokens/recording/compaction/
      resume/skills/spill 全适配；27 个测试文件迁移（425 过）。
    - **步骤 3 wire 加性 `parts`**：`docs/spec/chat.md` schema 增可选
      `parts`（`content` 保留为纯文本投影），双端 codegen + sidecar
      `_coerce_messages` 优先读 parts + 一致性测试。
    - **步骤 4 hooks 翻转**：`PreStepAction`/`RetryAction` 去掉整表
      `transcript`，改 `appends: list[TranscriptAppend]` + 声明式
      `rewrite: RewriteRequest`；loop 是唯一写者；ChainHooks 纯组合
      （rewrite 后 append 折叠为单 boundary）；compaction/skills 迁移
      （skills 目录改追加独立 system 消息，`kind="skills.catalog"`，
      仍落在压缩 head 保留区内）。
    - **步骤 5 持久记录 + resume O(tail)**：`StorageAdapter` 新增
      `append_history`/`list_history`（after/until/limit/reverse），
      InMemory + SQLAlchemy 两实现；全保真 codec（`entry_to_dict` 等，
      与 recording 的展示向格式解耦）；loop 在每次请求前、工具批后、
      回合末 `_flush_history`；连续日志语义——`_plan_record_seeding`
      识别已持久前缀只落增量，宿主改历史则先声明 `host_revision`
      boundary；`HistorySeed` 内联播种 fork（带 source provenance，
      新 record 自包含）；`load_history_transcript` 反向分页扫到最近
      boundary 即停（测试以 monkeypatch 页大小验证 O(tail)）；sidecar
      `chat/stream` 接 `history_store` + `recordId`，`chat/fork` 支持
      record 播种。
    - **步骤 6 tripwire 翻绿**：新增 `assert_requests_match_record`
      ——每个录制请求须等于记录时间线上某投影，声明的 boundary 自动
      对齐（不再手工传下标），未声明改写报「matches no record
      projection」；文档同步（roadmap Wave 1 标记落地 +
      `docs/spec/runtime.md` 记录通道）。
    - **测试**：`test_history_persistence.py` 16 例（codec 往返 /
      drain_pending / 前缀去重 / 连续日志增量 / host_revision /
      resume 保真与 O(tail) / tripwire 对齐与抓篡改）+ hooks 组合
      3 例 + compaction/skills 断言迁移；全量 425 过。

接下来三步**严格按序**（第一步的冻结建议已于本轮撤销，见其条目内注）：

- [x] **第一步 · sidecar RPC 面 app-server 化（A5 勘察切片）** ✅ 已完成
      （2026-08-27，纯勘察，api 零改动）。交付：
      `docs/migration/api-sse-drift.md`（已上线 mkdocs nav）。要点：
      - **实测 114 个发射点**（A0 估 ~100），18 种线上 wire type、
        15 个 collaboration 事件、5 个 live orchestration 事件。
      - **最大漂移：api 线上没有结构化工具事件**——工具以 content 标签
        （`<dp-action>`/`<ask-user>`）+ `executed_actions` 帧呈现；
        `tool-proposal` 前端有处理后端零发射（死代码）。采纳时 transport
        必须把 CoreLoop 的 `tool_call_*` 渲染回 content 标签保字节兼容。
      - **成本重估**：不是 O(100) 点改写，而是 O(20) 形状映射 +
        1 个 FastAPISseTransport + 编排/协作直通通道；loop.py 51 点随
        CoreLoop 采纳自动消失，resume 层的字节重解析
        （`_extract_content_delta`）可删。
      - ~~**冻结范围建议**：Layer 1 LoopEvent 13 kinds 闭环（产品事件不进
        loop，编排/协作走 transport 直通）；Layer 2 sidecar 15 方法 +
        通知集 + 反向通道，protocolVersion 0.1.0→1.0.0；
        `spec/sidecar.md` 方法目录（缺 steer/fork）与 `spec/events.md`
        （P1 时代已过时）在冻结 PR 重写。~~
      - **⚠️ 冻结已撤销（2026-08-28 决策，第五轮复审）**。上面那条保留作
        历史记录：勘察本身（114 发射点、O(20) 形状映射、字节兼容策略）
        全部有效，**只有「把自研 15 方法面冻结到 protocolVersion 1.0.0」
        这个动作被取消**。
        - **撤销理由**：协议层与 sidecar 层在 2026 年都撞上了已经收敛的
          标准。AG-UI 是 Microsoft Agent Framework / Google ADK / AWS
          Strands / Bedrock AgentCore / Mastra / Pydantic AI 的一等公民；
          ACP（JSON-RPC over stdio，编辑器↔agent）**正是 sidecar 的传输
          方式和问题陈述**，已有 25+ agent、JetBrains、Google、GitHub 与
          官方 Python SDK，stable v1。在多厂商标准落进同一个位置的当口，
          把一个只有 DeepPath 一个消费者的自研面冻起来是反方向。
        - **改为四件事**：
          ① **修真正的并发 bug**：逐 RPC 方法声明串行化域（照 codex 的
          `ClientRequestSerializationScope`，
          `codex-rs/app-server-protocol/src/protocol/common.rs:128-139`）。
          同一 session 上的 `agent.chat.stream` / `steer` / `cancel` /
          `fork` 有真实顺序要求，而 `docs/spec/sidecar.md:162-163` 现在
          只承诺「按 JSON-RPC id 排序」——那不是顺序保证。派发器按域上锁，
          表本身可脱离 server 单测。
          ② **AG-UI 与 ACP transport 与既有传输并列**，自研 `SSEEvent`
          路径保留给 DeepPath 字节兼容（`docs/migration/api-sse-drift.md`
          已经确立「transport 负责渲染 wire 格式」，这只是把该规则向外用）。
          第二个协议消费者也是「事件分类法真的与传输无关」的唯一真检验。
          ③ **Tier 1 定位改口**：从「我们的信封」改成「codegen 一致性
          纪律 + 映射进生态的信封」。纪律可辩护，信封不可辩护。
          ④ **list 方法加游标分页**：`trace.fetch` 把长会话的全部事件从
          stdio 管道倒出来是真实隐患（`docs/spec/sidecar.md:164-166` 已
          自承无背压）。
        - **不变的部分**：spec 漂移仍要修，但作为上述工作的一部分，不再
          作为「冻结 PR」的一部分——`docs/spec/events.md` 记着
          `orchestration`（`:35`）/ `loader-hint`（`:36`）/ `keepalive`
          （`:37`）这些 `LoopEventKind`（`loop.py:71-92`）根本没有的变体，
          而 `hook_action` / `steer` / `soft_timeout` / `reasoning_delta` /
          `stage_complete` 在规格里一个字都没有；`docs/spec/sidecar.md:65-82`
          列 13 个方法而 `sidecar.py:147-161` 注册了 15 个（缺
          `agent.chat.steer` / `agent.chat.fork`）。
        - 另一处需要修的事实源倒置：`MODEL_CONTEXT_WINDOWS`
          （`tokens.py:121-132`）已过期（`claude: 200_000`，而 Opus 4.6
          是 1M），且镜像下游产品的表
          （`deeppath-api/app/core/models_config.py`）——框架依赖消费者的
          数据表是反的，而压缩阈值由它派生，过期会静默误触发压缩。
      - 三个待决策点（原为冻结 PR 前）：工具事件字节兼容策略（**仍然
        有效**，是 api 采纳的必答题）/ api 是否永久 in-process /
        maxToolErrors 语义 + token 预算默认值。
- [x] **第二步 · 沙箱（macOS Seatbelt 起步）** ✅ 已完成（2026-08-27）。
      桌面「刻意全允许」时代结束：sidecar 进程现可被 Seatbelt 收容，
      61 规则分类器降为第二道提示层（对齐 codex 双层结构）。交付：
      - **框架侧 `steerable_sidecar/sandbox.py`**：deny-by-default
        Seatbelt profile 生成器（策略文本唯一属主），CLI
        `python -m steerable_sidecar.sandbox profile`。策略要点：读全开
        （skill roots 是宿主按请求传入的动态路径）、network-outbound 全开
        （provider baseUrl 用户可配，含 localhost Ollama）且**无
        network-bind**、写白名单仅 `~/.steerable`（token 校准原子写）+
        系统 scratch 目录、exec/fork 允许（子进程继承同一沙箱，非逃逸口
        ——codex 立场）。契约文档：`docs/spec/safety.md` 新增
        「OS sandbox: layer 1」节。
      - **agent 侧 supervisor 接线**：`SidecarStartOptions.sandbox` /
        环境变量 `STEERABLE_SIDECAR_SANDBOX=1`（opt-in，dogfood 后再
        default-on）。宿主先建 `~/.steerable`（沙箱拒 $HOME 写）、设
        `PYTHONDONTWRITEBYTECODE=1`，经
        `sandbox-exec -p <profile> python -m steerable_sidecar` 启动；
        非 macOS / profile 生成失败一律回退非沙箱并打日志（加固层绝不
        砖化应用）。
      - **测试**：框架 py 10 例（含真实 sandbox-exec 收容冒烟：受限子进程
        可读 /etc/hosts、写 /etc 被拒）；agent 单测 7 例（spawn 计划：
        开关门控、argv 形状、双回退路径）+ 集成 1 例（真实沙箱启动
        ping 通）。沙箱内 HTTPS+DNS 直连验证通过。
      - **E2E 回测（沙箱开）**：tools 6/6、multi 7/7、regen 3/3、
        cancel 4/4、plan 2/2；single 两轮各 22/25（msg[9]/[10] 与
        msg[9]/[11] 空回复）。四轮 A/B 归因：无沙箱基线同样抖动
        （25/25 与 23/25，msg[11] 空回复），失败集随轮次变化、全程
        HTTP 200 零错误、内核零沙箱拒绝日志——空回复是
        gpt-oss:20b-cloud 在最难数据接地 prompt 上的模型抖动
        （基线 msg[9] 需 116s 多重试才出字），非沙箱回归。
        踩坑：E2E 须等 `[sidecar] ready` 再发车——CDP 就绪 ≠ sidecar
        就绪，抢跑会吃 503（coreloop-stream 的 fail-loud 设计，
        非沙箱缺陷）。
      - **遗留**：Linux Landlock（`seatbelt_available()` 已留平台门）；
        default-on 时机（dogfood 一周无沙箱事故后翻默认）；宿主侧工具
        执行的沙箱化（当前工具跑在 Electron 宿主进程，由分类器+审批
        把守——codex 的 exec 沙箱化是更后面的事）。
- [ ] **第三步 · MCP client 下沉进 sidecar（严格依赖第一步结论）**：
      api 采纳则下沉（一次服务桌面+api，推迟约定自动解除）；不采纳则
      MCP 留在 Electron TS 层，此步取消。**注意**：即使 api 采纳，也存在
      运行时硬阻塞（MCP server 是 Electron node_modules 里的 npm 包，
      跑在 Electron 二进制上；Python sidecar 无 Node）——详见 P3 节的
      四条复核理由。下沉的现实形态是「sidecar 内 Python MCP 客户端服务
      api，桌面保留 TS 客户端」，不是统一成一份。
      **2026-08-28 补正（第五轮复审）**：P3 的理由 ③「sidecar 变进程
      监管者」已被 2026-07-28 的 MCP spec 作废（core 改无状态 HTTP：无
      握手、无 session id、请求自描述），理由 ① 也只对 stdio 服务器成立
      ——即「建的时候确实便宜了」。但**时序判据换成了别的东西**：不是
      「api 是否采纳」，而是「Wave 1 地基是否已落」。MCP 是最大的无界
      第三方可变模型可见上下文源，落在可变 transcript 上会复现已记录的
      cache 抖动病理。排序见上方第五轮复审的 Wave 2。

**明确不抄 codex**：TUI/云任务/企业 OAuth/权限 profile（OpenAI 产品
广度逼出来的，桌面+api 形态抄了是负资产）；Guardian 独立二审模型
（grounding judge 已是轻量版，沙箱之前升级它是本末倒置）；多智能体
产品化（codex 两年后才上 v2，seam 已备等真实需求）。

#### 第六轮复审（2026-08-29，四方对照：codex / dsh / **pi**（新增）/ 自勘）

Wave 0-3 全部落地后重新拉的对照。**pi 首次进入对照**
（`earendil-works/pi`，MIT，TS 单仓约 12 万行，HEAD `853a80d`），与
codex / dsh 同级别深潜；另加一路**独立自勘**（不读路线图、只读源码），
专门查「文档是否跑在代码前面」。

**头号结论：差距的性质变了。** 前五轮都是「我们缺这个机制」；这一轮
自勘的结论是「**机制大多已经有了，但产品不用**」。桌面端真实接线：
world-state ✅ / 工具分层 ✅（宿主 TS 侧，sidecar 的 Python 分层机制
在聊天路径闲置）/ skills catalog ✅ / **审批代数 ❌**（
`coreloop-stream.ts` 根本不发 `approval` 参数，桌面无审批 UI，
`harness/README.md` 写明「本地默认全部允许」）/ **逐 exec 沙箱 ❌**（
不发 `execSandbox`，命令无收容运行）/ **sidecar 进程沙箱 + 出网白名单
❌**（要 `STEERABLE_SIDECAR_SANDBOX=1`，缺省出网全开）/
**RecordingProvider ❌**（env 开关，生产从不录制）/ **AG-UI、ACP ❌**（
只有测试与 examples 证明）。「零生产验证」这个连续五轮的头号风险，
现在变形为「**机制—产品接线断层**」：不是没造，是造了不插电。

**路线图措辞需就地修正的一处**：「Wave 0 出网白名单已落地」——代码
确实落了，但**产品缺省出网全开**。这条在安全叙述里承重，不能按「已
落地」宣读。

**pi 是安全轴上的第三极，且是刻意的。** 不是「还没做」而是写进文档的
拒绝：核心不含权限系统、不含沙箱、不含出网控制，`security.md` 直接
承认仓库内容的提示注入是预期内的，隔离必须落在 OS/容器边界（Gondolin
微 VM / Docker / OpenShell 三种模式）。核心也刻意不含 MCP、子代理、
plan 模式、todo——一律推给扩展。三方现在是三种答案：codex 逐 exec 沙箱
+ 8 变体审批 + 审批↔沙箱升级协议；dsh 三平台后端（bwrap/Landlock、
Seatbelt、Windows 受限令牌）+ enforcement 返回值 + fail-closed；pi 整
进程容器化。我们站 codex/dsh 那侧，但**没插电**（见上）。

**pi 的新架构不能算数（重要，防止误记分）**：`AgentHarness`（v4 lane
records + 纯 reducer + SQLite）看着比我们和 dsh 都激进，但
`prompt` / `steer` / `abort` / `resume` / `compact` / `watch` / `lane`
等运行期操作**全部抛 `HarnessNotImplemented`**，`hooks.on` / `events.on`
也是 `UnavailableRegistry` 桩。CLI / RPC / SDK / evals 跑的都是旧的
`SessionManager`（JSONL v3 树）+ `AgentSession`。changelog 自述是
「compile-complete scaffold，durable execution 实现期间拒绝」。记分只
认在跑的那套。

**四方确认的领先项（第一次有非自证的外部对照）**：
- **`before_completion` 三态否决**：codex、dsh、pi 三家**均无对应物**。
  codex 的 Guardian V2 是安全审查（异步分类 + 同步 reviewer），不是
  完成稿否决；dsh 的 `llm-retry` 重试的是**失败请求**不是草稿。
- **伪工具调用恢复**：三家均无。codex 只在 JSON schema 层容错，dsh 把
  非法 JSON 参数原样透传给工具，pi 纯 schema 校验、纯文本里的伪调用
  直接忽略。
- **经验 token 校准**：三家均为启发式——codex `approx_token_count`、
  dsh 4 字符/token、pi `ceil(chars/4)` 且**无 CJK 处理**。
- **循环韧性预算**：pi **完全没有**——无最大轮次、无工具错误断路器，
  循环可无限迭代。我们 + codex + dsh 有。这是 pi 最弱的一轴。
- **软超时收尾 / 去重软反馈**：codex 只有 token 预算提醒（不是执行软
  超时），dsh 的 `repeat-tool-reminder` 是 3/5/8 阈值的劝告而非抑制。
- **跨语言契约**（仍是独有）。

**新暴露的三个真实缺口（代码问题，非措辞）**：
1. **`cache_control` 一行都没有** —— Wave 2 的「cache 仪表」只做了读
   （`cached_prompt_tokens` / `cache_creation_tokens` 进 trace），零处
   **写**断点。pi 在这一轴明确领先：`cacheRetention: none|short|long`
   是流式选项一等公民（默认 `short`，`PI_CACHE_RETENTION` 可覆盖），
   断点是**三个固定语义锚点**——系统提示词 / 最后一个工具定义 / 最后
   一条 user 消息的最后一个 block；OpenAI-completions 兼容路径同构，
   从尾部反向扫描找第一条有文本的 user|assistant|tool。压缩请求显式
   `cacheRetention: "none"`（一次性摘要不值得写缓存，且**只作用于那
   一次 HTTP 请求**，不标记产出的摘要）。dsh 是把断点委托给 pi-ai
   （`llm-pi-ai` 只有 `cacheControlFormat` / `cacheRetention` 配置，
   无第一方放置逻辑）；codex 走另一条路——稳定的 Responses input item
   id + 合成输出命名空间 + world-state 哈希来保前缀稳定
   （`context_manager/normalize.rs:18-19` 明写是为 prompt cache）。
   **我们是四家里唯一只读不写的。**
2. **子代理不是权限边界**：`subagent.py:107` 是
   `self._inner if allow_tools else _NoTools()`——全父工具或零工具的
   二元选择。dsh 有 `toolFilter` → `tools.restrict()` 做真正的工具域
   收窄且子代理审批钉死 `'never'`；codex 有 permission profile；pi 的
   子代理扩展直接 spawn 独立 `pi` 子进程（自带独立上下文）。第五轮
   记录的这条至今未动。
3. **持久记录无格式版本**：未知条目直接 `ValueError` 硬失败
   （`history.py:519`）。dsh 这一轮反而**加强**了——2026-08-25 把
   `ignorable` 整个删掉，改成全事件 required-on-read +
   `SessionFormatUnsupportedError`，是想清楚后选择更严（
   `SESSION_FORMAT_VERSION` 仍为 0，无迁移承诺）；pi 有 v1→v2→v3 +
   载入时自动迁移。我们是「没做」。

**对照结论的三处修订（旧记分卡引用已漂移）**：
- codex 工具分层从 3 档扩到 **6 档**（新增 `DirectModelOnly` /
  `DeferredModelOnly` / `CodeModeOnly`，`tool_executor.rs:51-79`）；
  `tool_search` 默认返回上限 **8**、BM25 检索；MCP 目录上限 2048
  （Codex Apps 8192）。
- dsh 的 `SandboxEnforcement` 类型里**没有 `none`**（只有
  `full | partial`）——我们照抄时加的 `none` 是自己的扩展。
- 「rollout policy 区分模型可见/展示」不能只引 `policy.rs`，现在分三
  层：`policy.rs` 决定持久与否、`context_manager/history.rs:79-91`
  过滤 world-state 片段、`rollout/src/list.rs:1177-1184` 决定线程预览。
- codex 的 MCP `tools/list_changed` **不是自动刷新**（handler 只记
  日志），目录刷新是显式的；dsh 是监听后排队重同步。

**第六轮新开轴（按 2026 现状，非 r3 的 13 轴）**：
- **cache 塑形（写）** 与 **cache 仪表（读）** 拆成两轴——我们只有后者。
- **机制—产品接线率**：新轴，且是我们独有的问题。codex / dsh 的审批与
  沙箱都是产品缺省开，pi 的「不做」也是产品事实；只有我们存在「框架
  有、产品不接」的断层。
- **扩展/插件架构作为交付载体**：pi 的扩展经 jiti 在进程内加载、**不
  沙箱**、可注册工具/替换系统提示词/改写模型请求/写自己的持久条目，
  且**每一轮 LLM 都过 `transformContext`**——是承重的；但 `context`
  钩子**无任何 token 上限**，写坏的扩展能撑爆上下文。dsh 是 Cordis
  插件运行时。我们是 hooks，面窄一档（这是刻意的，见「明确不做」）。
- **会话分支作为产品原语**：pi 的历史是**可分支的树**（`/tree`、fork、
  分支摘要都在一个文件里）、dsh 有 `Session.fork`、codex 有跨 fork
  的 context baseline 保持。我们只有 resume，无分支。
- **供应链/发布完整性**：pi 显著强（依赖钉死、`min-release-age=2`、
  shrinkwrap、`--ignore-scripts`、OIDC 可信发布、发布前隔离冒烟）。
  我们没考虑过这一轴。
- **注入内容设界**：codex hook 输出 2500 token 硬顶 + 落盘；pi 工具
  输出 2000 行/50KB + bash 溢出落盘 + 子代理 50KB；dsh 有
  `spill-policy`。我们有 `spill.py` 但**不在 sidecar 缺省钩子链里**，
  skill catalog 无聚合上限、world-state 无上限、steer 无上限。

**第六轮排序**——可执行条目在上方 **Wave 4** 波次里（W4-1 … W4-7），
这里只留判断：
- **P0 = W4-1/2/3（把已经造好的插上电）**。判据很直白：我们在安全轴上
  宣称站 codex/dsh 那侧，但产品跑在 pi 的姿势上（全放行 + 无收容 + 出网
  全开），却又没有 pi 那套「所以请容器化」的诚实说明。三件里 W4-1 是真
  产品活（要新建 Electron 审批 UI），另两件是管道。
- **P1 = W4-4（`cache_control` 发射）**。仪表装了、方向盘没接；四家里
  我们是唯一只读不写的。
- **P2 = W4-5/6/7（三个第五轮就记录、至今未动的小口子）**：子代理不是
  权限边界、持久记录无格式版本、注入内容无界。
- 明确**不做**：pi 式扩展运行时（进程内不沙箱加载 + 无上限 context
  钩子，风险面比收益大）；会话分支树（等真实产品需求）；`AgentHarness`
  式 lane/reducer 重写（我们的 `HistoryItem` 追加式记录已覆盖同一问
  题，且我们的在跑）。

**原 A5 备忘**（并入第一步勘察范围，2026-08-27 勘察结论）：
- [ ] api 侧最大的活：把约 100 个 SSE 发射点改成结构化事件 —— 实测 114
      点，但勘察重估为「O(20) 形状映射 + transport + 编排旁路」，
      详见 `docs/migration/api-sse-drift.md` 采纳成本重估一节
- [x] 此时 CoreLoop 已被真实产品验证，且带着 api 缺失的反幻觉能力
      （A4 default-on + E2E 47/47，2026-08-27）

## A6 · skills 好用（产品目标，2026-08-27 立项）

**这是第三份三仓重复实现**，与当初驱动 CoreLoop 下沉的模式同构：
`deeppath-api/app/services/harness/skill_loader.py`（Python 原版）、
`deeppath-agent/src/local-backend/skill-loader.ts`（注释明写是 port）、
框架侧**零**。同时机制上落后生态一整代。

**实勘现状（2026-08-27）**：
- agent 8 个内置 skill（identity 424 / plan-mode 1513 / tool-usage 1117 /
  anti-deferred 1544 / data-grounding 1325 / local-exec 3795 /
  proactive-coding 1431 / cflog 3449 字符），**按 conditions 门控后全量
  注入系统提示词**，`DEFAULT_CHAR_BUDGET=60_000`，超预算按 priority
  从低到高丢弃。
- 无模型可见的 skill 加载工具（确定性结论）：按需 = 用户 `/name` 强制
  把整段正文塞进系统提示词尾部。
- UI 已完整（`SkillsSettingsPanel` 导入/列表/删除 + ChatInput `/` 选择器
  + 三个 REST 端点），**只缺渐进披露的加载机制**。
- 框架侧无任何 skill 概念；系统提示词就是 `messages[0]`，无独立参数。
- `ChatAgent.skillIds` / `allowExternalSkills` 字段在三仓都有定义但
  **没人读**（死 schema，等这次接线）。

**要解决的三个真实问题**：
1. **装不多**：现在装 20 个 skill 就撞 60k 预算，开始按 priority 丢——
   丢的是用户刚导入的领域 skill（priority 默认 500，低于全部基座）。
2. **生态不通**：codex / Claude / Cursor 的 SKILL.md 格式上兼容（frontmatter
   是我们字段的子集，name 校验全过，`{scripts}` 也支持），但直接导入
   会每轮全量注入且互相干扰——「能加载」不等于「好用」。
3. **三仓重复**：任何 skill 机制改进要写三遍（api Python / agent TS /
   框架无），与 CoreLoop 迁移前的处境完全一样。

**设计要点 · 分层披露（不是全盘改渐进）**：
渐进披露把「skill 一定被看到」换成「模型可能不加载」。生态三家都接受
这个权衡，但**基座类 skill 不能交给模型自选**——identity / tool-usage /
反幻觉三件套是行为约束，模型不会主动去加载约束自己的规则。所以分两层：
- **eager 层**（保持现状）：identity / tool-usage / anti-deferred /
  data-grounding / plan-mode——系统提示词常驻，总量约 6k 字符有界。
- **catalog 层**（新增）：local-exec / cflog / proactive-coding + 全部
  用户导入 skill——系统提示词只出 `- name: description` 清单（约 600
  字符），模型调 `skill` 工具或用户 `/name` 才注入正文。
分层判据写进 frontmatter：新增 `layer: eager|catalog`（缺省按 priority
≥850 判 eager，兼容既有 8 个内置 skill 的现有行为）。

**分片计划**（框架先行，与 codex 路线第一步同向——skill seam 是协议面
产品化的一部分）：

- [x] **Slice 1 · 框架 skill seam（Python，纯新增）**（2026-08-27 完成）：
      新模块 `skills.py` 按 capability seam 三件套——Definition
      （`SkillProvider` 协议：`list() -> Sequence[SkillSummary]` /
      `get(name) -> SkillDefinition`）、Provider（`FilesystemSkillProvider`：
      读 `<root>/<dir>/SKILL.md`，frontmatter 解析与既有 TS/api 格式
      **逐字段兼容** + 新增 `layer` / `disable-model-invocation`）、
      Consumer（`skill_tool_descriptor()` + `SkillExecutor` 装饰器 +
      `SkillHooks.pre_step` 首轮注入 catalog）。严格照 `subagent.py` 的
      既有范式（descriptor 函数 + executor 装饰器 + opt-in），渲染沿用
      dsh 的 `<skill_content name="...">` 标记。测试：清单渲染 / 工具
      往返 / 未知 skill / 非 model-invocable 拒绝 / 分层判据 / frontmatter
      兼容既有 8 个内置 skill（`test_skills.py` 22 例）。
      顺带修了 `ChainHooks.pre_step` 的 `rewrite_action` 传递 bug
      （后续 no-op hook 会把先前 hook 的动作标签覆盖回 "compact"——
      现在按 transcript 引用是否变化判定谁是真正的改写者）。
- [x] **Slice 2 · sidecar 接线**（2026-08-27 完成）：`params.skills =
      {roots, conditions, exclude, ignoreConditions, mode}`。sidecar 与
      Electron 同机，直接读内置 + `userData/skills` 路径，不需要反向
      通道。`eager` 保留为兼容模式（api 采纳前不强迫它改）。catalog
      注入落 trace（model-visible ⟺ logged）：`hook_action` 事件带
      `action: "skill_catalog"`（`PreStepAction.rewrite_action` 新字段，
      默认值 "compact" 保持既有事件不变）。测试
      `test_sidecar_skills.py` 5 例（注入+广告工具 / 进程内执行 /
      eager 空转 / 空目录 / exclude 隐藏）。
- [x] **Slice 3 · agent 切换**（2026-08-27 完成）：`coreloop-stream.ts`
      传 `skills: {roots, conditions, exclude, ignoreConditions}`；
      `prompt-builder.ts` 新增 `eagerOnly`（CoreLoop 路径恒 true）只注入
      eager 层；`/` 触发从「塞系统提示词尾部」改为 router 注入一次性
      用户消息（`buildForcedSkillMessage` = 指令头 + 正文，并入本轮最后
      一条用户消息——保留强制语义，不再污染系统提示词前缀，prompt
      cache 净收益，历史消息不残留技能正文）；TS `skill-loader.ts`
      保留给 UI 列表/导入 REST 端点与 `findSkill` 别名解析，删掉
      `forcedSkillName` 注入路径。模式排除清单（plan 模式排执行类技能）
      同一份下发 sidecar catalog 与 `findSkill`。
- [x] **Slice 4 · 生态兼容 + UI**（2026-08-27 完成）：`disable-model-invocation`
      在 TS/Python 两个 loader 都映射为 `modelInvocable=false`（不进
      catalog、skill 工具拒绝、`/name` 仍可强制）；设置面板标注每个
      skill 的层级（常驻/按需）与「仅手动」；trace-report 的
      `hook_action` 通用计数自动覆盖 `skill_catalog` 与 skill 工具调用
      频次。**注意**：codex skill 正文引用它自己的工具链（`just` /
      `cargo-insta` / 子代理编排），导入时这类指令需改写或删除，
      否则模型会尝试并失败。

**不做**：skill 版本管理 / 远程 skill registry（dsh 的 provider 抽象已
预留 provider 名，等真实需求）；skill 之间的依赖声明（生态三家都没有）。

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
