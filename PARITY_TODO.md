# 架构追平计划（对标 Codex / DeepSeek Harness / pi）

> 目标：补齐 2026-08-30 三家横向对比中暴露的**架构缺口**，按"挡不挡得住对外交付"排序。
> 依据：对四个仓库源码的实测对比（非 README 自述）。凡文档与源码冲突处，以源码为准。
>
> **与既有 TODO 的分工**（不要重复开条目）：
> - [`EVALS_TODO.md`](./EVALS_TODO.md) —— 评测流水线本身（Harbor / TB / SWE 的接入与跑分）
> - [`CORELOOP_TODO.md`](./CORELOOP_TODO.md) —— CoreLoop 自身的能力演进
> - [`TODO.md`](./TODO.md) —— 原始分期建设与遗留 follow-up
> - **本文件** —— 只收"和三家对比之后才暴露出来的差距"，以及为消除差距所需的改造

**对比结论要点**（完整版见 canvas `agent-framework-comparison`）：

- 非测试源码量：Codex 859k(Rust) · DSH 256k(TS) · pi 140k(TS) · 我们 26k(Py+TS)
- 我们站得住的领先：跨语言类型契约、host-seed 对账、面向便宜模型的质量层、可审计压缩边界
- **我们没有与竞品同条件的头对头跑分**，因此本文件不设"超过某家"这类出口

---

## P0 · 可信度与安全底座（阻塞对外交付）

出口：外部读者按 README 和 docs 做出的判断，与源码实际能力一致；agent 能在 Linux 与 Windows 上被真实约束。

### 0.1 修掉文档与代码的漂移

两处是硬伤，都已定位到行：

- [ ] **0.1.1** `README.md:214` 宣称 `@tool` 装饰器 "auto-derived JSON Schema from Python type hints"。
      源码中不存在该能力：`tools.py` 只用 `inspect.signature` 做参数注入与类型强制，schema 全部由调用方手写传入。
      二选一：**要么删除该表述**（改为"手写 schema + 参数自动注入"），**要么真的实现**并补测试。
      不允许维持现状。
- [ ] **0.1.2** `docs/spec/core-loop.md:1,3` 仍写着 `# CoreLoop Spec (draft)` /
      `Status: **draft — ports only, no implementation yet.**`，而实现该规范的 `loop.py` 已有 1680 行，且是 sidecar、桌面、评测三处共用的生产路径。
      更新状态与内容，使其描述实现而非草案。
- [ ] **0.1.3** 顺带扫一遍 `docs/spec/architecture.md` 里"没有 AgentLoop 类"之类的历史表述，与 `CoreLoop` 现状对齐。
- [ ] **0.1.4** 加一条门禁防复发：spec 文档中出现 `no implementation yet` 且对应模块已有实现时报红（可挂在现有 `doc-sync` 类检查里）。

**出口**：`rg "no implementation yet|auto-derived JSON Schema" README.md docs/` 无命中，或命中处与源码一致。

### 0.2 Linux Landlock 沙箱后端

现状：`packages/sidecar/py/src/steerable_sidecar/sandbox.py` 只有 `SeatbeltExecBackend`(macOS) 与 `BwrapExecBackend`(Linux)。
bubblewrap 依赖外部二进制且在容器内常不可用；Landlock 是内核 LSM，无需额外进程。

- [x] **0.2.1** 在 `sandbox.py` 增加 `LandlockExecBackend`，与现有 backend 同一 `SandboxBackend` 协议。
- [x] **0.2.2** 后端选择顺序显式化：Linux 上 bwrap → Landlock → 不可用（与 DSH 已验证的链一致：
      bwrap 可用时隔离维度更多——mount/PID 命名空间、私有 /tmp；Landlock 的价值是无外部二进制、
      无 userns 依赖，在 bwrap 跑不起来的容器里仍可用）。每一步的降级原因通过结果的
      `_sandbox` 字段（backend 名 + enforcement）可见，不能静默。
- [x] **0.2.3** 内核不支持 Landlock（<5.13）或缺少 ABI 时，走既有 `require_full` 失败关闭路径，不得静默放行。
- [x] **0.2.4** 测试：只读策略下写文件被拒、workspace-write 下越界写被拒、降级路径被正确上报。

**参考实现**：DeepSeek Harness 的 `native/landlock-run` 把 Landlock 做成独立启动器二进制，其"策略参数 → 启动器 argv"的切分方式可直接借鉴（我们不必抄它的 Node 原生插件形态）。

**出口**：Linux 上不装 bubblewrap 也能获得 `enforcement == "full"`。

### 0.3 Windows 沙箱后端

现状：Windows 上没有任何后端，`require_full` 一律拒绝，等于该平台不可用。

**定范围结论（2026-08-30）**：受限令牌 + Job Object 是宿主侧 spawn 能力
（`CreateProcessWithTokenW` 由发起进程应用令牌），**无法用命令改写表达**——
本层的架构就是命令改写，所以 rewriter 式的 `WindowsExecBackend` 只会是安全表演，
恰恰违反 0.3.3 "宁可少报能力，不可虚报"。真实支持的必要路径（未来工作流，按序）：
随宿主分发原生 spawn helper → 反向通道协议扩展（由合法持有该能力的宿主代为 confined spawn）
→ Windows CI 覆盖限制矩阵。在此之前 Windows 一律 `enforcement: "none"` + `requireFull` 拒绝。

- [x] **0.3.1** 定范围完成：结论如上，已写入 `docs/spec/safety.md` "Windows: no backend" 决策小节。
- [x] **0.3.2** 不实现 rewriter 式后端（安全表演）；决策与实现路径已记录。
      宿主侧 spawn 工作流转入 backlog（与 P3.2 的 TS 运行时包同属宿主能力面，可一并规划）。
- [x] **0.3.3** 失败关闭路径已有测试：`test_windows_constructs_no_backend`（平台门全部自然拒绝）
      + `test_require_full_denies_when_no_backend`（拒绝语义）。

**出口**：Windows 的空洞被如实标注为平台立场而非疏漏；真实支持有明确的架构路径。

### 0.4 Seatbelt 出网粒度的如实表述

现状：Seatbelt 的 `allowed_hosts` 在主机名层面会退化到端口级。

- [x] **0.4.1** 在 `docs/spec/safety.md` 写清这一退化的确切边界（什么情况下退化、退化后实际拦住了什么）。
      （前期已落实：egress allow-list 一节明确"非 localhost 条目退化为 `*:PORT`、仍拦住反弹 shell/信标/DNS 隧道、
      拦不住 443 上攻击者 HTTPS 端点"，并给出本地代理缓解；产品姿态一节同步如实标注 `partial`。）
- [x] **0.4.2** 评估是否需要像 Codex 那样引入独立出网代理来做主机名级管控——**本项只出结论，不在本阶段实施**。

**0.4.2 评估结论**：值得做，但作为独立可选组件，不进入框架关键路径。
业界参照成立（Claude Code 的 sandbox-runtime 即"本地代理 + 沙箱只放行代理端口"结构）；
价值是补上端口级退化堵不住的"443 外泄"口子，且三平台通吃（代理解析 CONNECT/SNI 做主机名管控，与 OS 沙箱正交）；
首版范围可控（仅 CONNECT 隧道 + 主机允许列表，不做 TLS 拦截），复杂度在运营面（企业代理链、SSE 长连接、IPv6）而非核心；
接入钩子已现成（sidecar httpx 栈遵从代理环境变量，`allowedHosts` 收敛到 `localhost:<proxy>` 即 fail-closed 对接）。
排期转入 3.3；不阻塞当前交付。

---

## P1 · 循环能力对齐

出口：长工具调用可被及时中断；我们知道自己在公开基准上相对竞品的真实位置。

### 1.1 循环内协作式取消

现状：`loop.py` 内没有取消令牌。取消只能由 sidecar 层 `agent.chat.cancel` 取消整个 asyncio 任务；循环自身只有审批中止（`loop.abort_skip`）与单工具超时。
另外三家都有贯穿模型流与工具执行的取消信号（Codex `CancellationToken` + 优雅超时窗口、DSH/pi 用 `AbortController`）。

- [x] **1.1.1** 引入取消令牌，贯穿 `CoreLoop.run` 的轮次边界、provider 流式消费、工具批量执行三处。
      已实现：`CoreLoop.cancel()` 设置协作令牌；轮次边界（steer 排空后）、流式消费（逐 chunk）、工具批量执行（顺序与并行两条路径）三处检查点。
- [x] **1.1.2** 取消后的记录语义要确定：已发出的工具调用如何收尾、历史里留下什么条目。
      语义（先定后写）：流式中途取消 → 部分内容记为终止 assistant 消息，未执行的流式 tool_calls 不入记录；工具执行中途取消 → 在飞调用 asyncio 取消并记 `cancelled` 失败结果，未启动调用记 `loop.cancel_skip` 合成结果（复用 abort/breaker 的 `_append_unexecuted_tool_results` 机制）——记录永不出现悬空 tool_calls，会话可续。
- [x] **1.1.3** 给一个优雅期（参考 Codex 的百毫秒级窗口），过期再硬取消。
      sidecar 侧 5s 看门狗（`_CANCEL_GRACE_S`）：协作取消发出后循环未收尾则硬取消任务。
- [x] **1.1.4** sidecar 的 `agent.chat.cancel` 改为走协作式路径，任务级取消降为兜底。
      CoreLoop 流走 `loop.cancel()`；非 CoreLoop 遗留路径保持硬取消。`stream.done` 对 `status="cancelled"` 补 `cancelled: true`，与硬取消路径的终止信号一致。
- [x] **1.1.5** 测试：流式过程中取消、工具执行中取消、取消后 record 仍能被下一轮正确 seed。
      `test_loop_cancellation.py` 6 例（运行前取消/流式中取消/轮次边界/顺序执行中取消/并行执行中取消/无悬空 tool_calls）+ sidecar 级 `test_coreloop_cancel_winds_down_cooperatively`。

**出口**：一次长 `bash` 调用能在秒级内被中断，且会话记录仍然自洽可续。✅ 已达成（2026-08-30）

### 1.2 竞品同条件对照跑分

> **已暂停（2026-08-30 用户决策）**：同条件对照跑分先不做。后续对比以源码/架构分析与公开方法论调研为准，本节的出口标准暂时失效，恢复时再启用。

现状：`evals/jobs/` 下只有 steerable 跑过多任务作业（12 / 51 / 77 题）；`claude-code` 与 `oracle` 各自只有 1 个 trial 的 canary。
**我们目前无法回答"我们排第几"。**

- [ ] **1.2.1** ~~至少让 `claude-code` 跑满同一份 cheap-12~~（暂停，见上）
- [ ] **1.2.2** ~~对照结果回写到本文件顶部的"对比结论要点"~~（暂停，见上）

---

## P2 · 生态与上下文（提升上限，不阻塞交付）

### 2.1 Provider 兼容性矩阵

现状：两条接入（OpenAI 兼容 + Anthropic）。pi 有 30+ 家，且把"这家的 OpenAI 兼容接口在哪里不兼容"编码成了数据（compat flags）而不是散落的分支判断。

- [x] **2.1.1** 把现有 `OpenAICompatProvider` 里隐含的各家差异抽成显式的兼容性数据结构。
      已实现：`llm/compat.py` 的 `OpenAICompatFlags`——请求侧 4 旗标（usage-in-streaming / max_tokens 字段名 / reasoning_effort / temperature，错了就是厂商侧 400）+ 响应侧 2 字段（reasoning delta 键序、cache token 点路径，默认宽容）。`PROVIDER_COMPAT_HOSTS` 按 base-URL host 子串匹配（pi 式自动探测），sidecar `compat` 参数显式覆盖优先，未知键 fail loud。
- [x] **2.1.2** 新增 provider 时只填数据、不改流式解析逻辑——以此作为验收标准。
      验收测试 `test_new_vendor_added_with_data_only`：虚构厂商的三处分歧（不收 stream_options / max_completion_tokens / thinking 推理键 + 自定义 cache 路径）全部由一条 flags 条目覆盖，请求构造与流式解析零改动。
- [x] **2.1.3** 不追求 30+ 家覆盖；目标是把接入新模型的**边际成本**降下来。
      注册表只收录有生产实据的分歧（当前：DeepSeek 响应字段，且默认宽容已覆盖——条目作用是把隐式容忍钉成显式数据）。文档落在 `docs/spec/sidecar.md`。

### 2.2 类型化有界上下文片段

现状：我们已有追加式记录与 `CompactionBoundary`，但注入物（技能目录、world state、软超时提示、叙述等）缺少统一的类型与大小上限约束。
Codex 的 `ContextualUserFragment` 要求每个注入物是类型化、有界、可分类的，并有硬性 token 上限规则。

- [x] **2.2.1** 给所有注入片段定义统一协议（类型 + 渲染 + 分类 + 上限）。
      已实现：`ContextFragment` 在既有 `content_kind`（分类）+ `render/markers`（渲染）之上增加 `max_tokens`（上限，默认 1024）与 `review_note`；`TranscriptAppend.fragment` 让 hook 注入携带类型化片段，循环统一走 `append_fragment`。技能目录从裸消息改造为 `SkillCatalogFragment`（线字节不变）。
- [x] **2.2.2** 设硬上限并在超限时可预期地降级，而不是把上下文撑爆后依赖压缩兜底。
      `append_fragment` 超限即降级：默认截断+可见标记；结构化片段覆盖 `degrade` 按整单位丢弃（目录丢尾部技能行、world-state 丢尾部 section 并重渲染，patch 片段保全新快照注释）。10K 绝对上限为常量 `FRAGMENT_TOKEN_CEILING`。
- [x] **2.2.3** 单个片段超过约定 token 数时，走显式评审而非默默合入。
      门禁测试 `test_gate_all_fragments_bounded`：遍历全部 `ContextFragment` 子类，cap > 1024（no-review 线）必须有非空 `review_note`，cap > 10K 直接报红——越线是一个 PR 里可见的代码动作。

---

## P3 · 架构层集成（2026-08-30 已决策：不再是开放问题）

> 决策记录：3.1（多 agent 编排）与 3.2（TS 生产能力）是历史遗留缺口，
> 结论是**将能力集成到框架架构层**，不再停留在产品层或"待决策"。

### 3.1 多 agent 编排集成到框架层

现状：`subagent.py` 的 `SubagentExecutor` 仅 depth-1 委派，子 CoreLoop 只能用内部执行器，
无并行池、无协调原语。Codex 的子 agent 是复用同一循环的完整 thread（spawn/send/wait/close），
DSH 有 subagent seam + workflow。

- [x] **3.1.1** 设计编排原语：spawn / send / wait / close、并行子 agent 池、深度与并发预算、
      血缘追踪。参考 Codex `multi_agents_v2` 与 DSH subagent seam 的边界划分，落在 CoreLoop 上。
      已实现 `orchestration.py`：四个工具（`agent_spawn`/`agent_send`/`agent_wait`/`agent_close`）+ `AgentPool`；血缘即数据——childId 为 `<lineage>.<seq>`（根 `0`，`0.2.1` = 根的第二个子的第一个孙），所有结果携带结构化 JSON。
- [x] **3.1.2** `orchestration.py`：并行子 agent 池——多个 child CoreLoop 并发运行，
      每个子的工具域可独立收窄（继承现有 `tool_filter`），事件可选并入父事件流。
      子循环为同一事件循环上的 asyncio 任务；`toolFilter` 经 `FilteredToolsExecutor`（从 subagent 提升为公共类）失败关闭；子同时只广告被委派的 schema（编排族工具按深度预算增删）。生命周期事件经 `event_sink` 外发（sidecar 接为 `agent.child` 通知）。
- [x] **3.1.3** 协调原语：父向子 send_message、wait（带超时）、interrupt；
      子的完成/失败以结构化事件回到父循环。
      send = `loop.steer`（轮次边界投递）；wait 带超时（shield——超时不取消子，返回 `running`）；close = P1.1 的协作式 `loop.cancel()` + 2s 硬取消兜底。完成/失败经 `child_completed`/`child_failed` 事件 + wait 结果 JSON 回到父循环。
- [x] **3.1.4** 预算失败关闭：delegation 深度上限、并行上限，超限拒绝而非静默排队。
      并行上限 → `orchestration_budget_exceeded`（可重试语义）；深度结构性执行——子的执行器只在 `depth+1 < max_depth` 时内嵌编排层，否则子根本没有编排工具。
- [x] **3.1.5** sidecar RPC 暴露 + 集成测试（父循环驱动两个并行子循环完成任务）。
      `orchestration: {maxDepth, maxParallel, childMaxRounds}` 参数 + 工具描述符注入 + 父流结束时协作式 shutdown。运行时 11 例（含并行并发证明、深度 2 允许孙代、记录可重建）+ sidecar 端到端 `test_coreloop_orchestration_spawn_wait_over_rpc`。

**出口**：框架内可运行"一个父 agent 并行编排多个子 agent"的完整路径，
深度/并发超限有明确拒绝，且全程可从会话记录重建。✅ 已达成（2026-08-30）

### 3.2 TS 生产能力集成到架构层

现状：Tier 2/3 生产逻辑仅 Python，TS harness 只做一致性测试；纯 TS 生态要用我们必须自己起 sidecar 进程。

**路线决策**：不做 TS CoreLoop——`docs/spec/core-loop.md` 明确把"用 TS 写 CoreLoop"列为陷阱
（会产生 deeppath-api 永远无法采用的第四份实现）。正确路线是把 **sidecar 内嵌**做成
官方的、架构级的 TS 运行时形态：TS 调用方拿到的是 CoreLoop 级 API，而不是"自己管一个子进程"。

- [x] **3.2.1** 在 `docs/spec/architecture.md` 把该路线写成正式立场：
      Python 是唯一生产实现；TS 的生产入口是内嵌 sidecar 的官方运行时包。
      已写入 "Official TS production entry: the embedded-sidecar runtime" 一节；
      sidecar.md 方法目录补全（fork/branches/steer/apply_edits/skills.list/trace.export 原先缺载）。
- [x] **3.2.2** `@steerable/agent-runtime`（TS）：托管 sidecar 生命周期
      （spawn / health-ping / auto-restart / 优雅退出），对 TS 调用方暴露
      CoreLoop 级 API（chat / stream / cancel / fork / branch）。
      新包 `packages/agent-runtime/ts`：`SidecarProcess`（ready 握手后才服务、
      崩溃有界自动重启+退避、boot 失败不重启、close 三级升级 SIGTERM→SIGKILL、
      close 抑制在途重启）+ `AgentRuntime`（session/chat/tool/skills/workspace/trace/config
      全覆盖，流式为 async-iterable + done promise，反向通道 `tool.invoke` 注册宿主工具）。
      修复两个真实 bug：重启预算被成功重启清零（崩溃循环会无限重启）；
      响应+chunk+done 合并进单次 read 时 done 先删 sink 导致消费端永久挂起（claim 协议 + 回归测试）。
- [x] **3.2.3** 与 `@steerable/agent-ui` 打通：`useAgentSession` 等 hooks 可直接消费该运行时。
      `transports.ts` 提供 `createChatStreamTransport` / `createSessionTransport`；
      分层约束下运行时包声明结构等价接口，`test/ui-transport.types.ts` 做双向可赋值
      编译门禁（`pnpm lint` 执行），UI 侧零改动直接消费。
- [x] **3.2.4** 复用现有跨语言 conformance：TS 运行时驱动的就是同一 Python CoreLoop，
      天然无漂移；补一条"TS API 面 ↔ sidecar 方法面"的一致性测试。
      `test/surface.test.ts`：解析 sidecar.py 的 `register("...")` 与 `SIDECAR_METHODS`
      集合相等 + runtime.ts 每个方法都有真实 request 接线；版本并入 lockstep 门禁（8 包）。

**出口**：纯 TS 项目 `npm install @steerable/agent-runtime` 即可获得生产级 CoreLoop，
无需手写子进程管理；文档不再把 sidecar 描述成"需要自己拼接的内部机制"。✅ 已达成（2026-08-30，25 例 TS 测试全绿）

- [x] **3.2.5** 真端到端门禁（E2E 补测，2026-08-30）：`test/e2e-real-sidecar.test.ts`
      用仓库 uv venv 里的**真实 Python sidecar** 跑通完整工具调用轮（CoreLoop →
      OpenAI 兼容 HTTP → 本地 mock SSE → 反向通道 `tool.invoke` → 第二轮 LLM → done）
      与协作取消；无 Python 环境时自跳过。该 E2E 当场抓住两个假 sidecar 测不出的
      线契约缺陷并已修复：① `chatStream` 未带 `useCoreLoop: true`，静默落到不支持
      cancel/steer/编排的 legacy 直发路径（现默认开启）；② `ChatStreamParams` 错把
      `sessionId`/`message` 标为必填而真实线契约要求 `provider`/`model`/`messages`，
      导致 transports 适配器发出的请求 messages 为空（已按线契约重写接口与适配器）。
      27 例 TS 测试全绿。

### 3.3 出网管控是否需要独立代理

- [x] **3.3** 结论已出（见 0.4.2）：做，作为独立可选组件 `steerable-egress-proxy`
      （CONNECT 隧道 + 主机允许列表，首版不做 TLS 拦截）。排期在 P1 之后；
      落地时同步更新 `docs/spec/safety.md` 的 egress 章节与桌面 `sandboxAllowedHosts` 默认推导。
      已实现 `packages/egress-proxy/py`：零依赖 asyncio CONNECT 代理——空允许列表构造即
      ValueError（失败关闭，永不静默开放）、非 CONNECT 一律 405、表外目标 403、
      不可达 502、头部 16KiB 上限 431、裸 host 条目放行 443/80（与 Seatbelt profile 语义对齐）、
      CLI  misconfiguration 退出码 2。23 例测试（真实 loopback 隧道双向字节 + 全部拒绝路径
      + CLI 失败关闭）。safety.md 两处 egress 章节已指向该组件并写明 HTTPS_PROXY 接法；
      桌面 `deriveSidecarEgressAllowList` 注释同步（ambient proxy 本就在列表内，行为不变）。
      包并入 uv workspace、pytest testpaths、lockstep 门禁（9 包）。

---

## 明确不做

- 不为了缩小 LOC 差距而堆功能。26k 对 859k 是定位差异，不是待办事项。
- 不在没有同条件对照跑分的情况下，对外宣称强于任何一家。
- 不把"另外三家有而我们没有"直接当成待办——先判断那是不是我们的场景（例如实时语音、cloud tasks）。
- 不为通过检查而软化事实：沙箱等级、评测分数、能力边界一律如实上报，宁可少报。
- 不在 P0 未清的情况下开 P2。文档漂移和沙箱缺口是对外可信度问题，优先级高于新能力。
