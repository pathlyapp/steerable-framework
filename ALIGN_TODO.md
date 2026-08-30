# 对齐计划（框架 ↔ 桌面接线 + R9 复评剩余差距）

> 目标：把两份实测分析暴露的对齐问题收敛成一张可执行的规划。
> 来源（均为 2026-08-30 源码实测，非 README 自述）：
> - **A 源**：canvas `framework-desktop-parity` —— steerable-framework v0.3.0 已发布能力在
>   deeppath-agent v0.0.021 上的接线缺口（19 个 RPC 方法接通 11，agent-ui 组件 2/8）。
> - **B 源**：canvas `steerable-r9-parity-axes`（四方框架架构对比 R9 补齐后复评）——
>   13 轴判定后剩余的落后/混合项。
>
> **与既有 TODO 的分工**（不要重复开条目）：
> - [`PARITY_TODO.md`](./PARITY_TODO.md) —— R9 之前的架构追平（已全部完成并随 v0.3.0 发布）
> - [`EVALS_TODO.md`](./EVALS_TODO.md) —— 评测流水线本身
> - [`CORELOOP_TODO.md`](./CORELOOP_TODO.md) —— CoreLoop 自身能力演进
> - **本文件** —— 只收两类：①框架有而桌面没接的线；②R9 复评后仍然成立的差距
>
> **明确不做**（沿用既定决策）：竞品同条件头对头跑分（用户 2026-08-30 指示暂缓）；
> 体量差（30.7k vs 864k 行）是定位差异，不设条目。

---

## W1 · 桌面接线（A 源，框架能力已发布、只缺桌面侧）

出口：deeppath-agent 用户能在产品里真实用到这些框架能力，而非仅框架 API 存在。

### 1.1 多智能体编排接线（唯一高优先级缺口）

框架侧三件套已就绪：`chat.stream` 的 `orchestration` 参数、`agent.child` 通知、
agent-ui 的 `OrchestrationPlanCard` 组件。桌面零调用点。

- [x] **1.1.1**（2026-08-30 完成：coreloop-stream 传 orchestration + agent.child 进 SSE；coreloop-stream 测试 3 例） deeppath-agent `coreloop-stream.ts` 支持向 sidecar 传 `orchestration` 配置
      （`OrchestrationConfig`：并行上限、子代理预算），默认关闭，设置里开。
- [x] **1.1.2**（2026-08-30 完成：OrchestrationChildrenCard 接入消息流；映射测试 4 例 + 框架卡片点击测试） 订阅 `agent.child` 通知并映射进 SSE 事件流，渲染用框架的
      `OrchestrationPlanCard`（不自绘第四套组件）。
- [x] **1.1.3**（2026-08-30 完成：canary 第 10 节：真实输入/回车/卡片渲染/点击折叠展开，全 PASS） 桌面审批链路与子代理工具调用的交互定义清楚：子代理触发的 approval
      如何归属到父轮次展示。
- [x] **1.1.4**（2026-08-30 完成：README 增补多代理编排接线段落） E2E：desktop-canary 增加一条编排用例（父轮派生 1 个子代理并回收结果）。

### 1.2 会话分支与可移植性

- [ ] **1.2.1** `agent.session.branches` 接入桌面 UI：regenerate 产生的分叉可见、可切换
      （现在 fork 只用于 regenerate，分支列表用户看不见）。
- [ ] **1.2.2** 评估 `agent.session.create/list/resume` 与桌面本地聊天存储的关系：
      二选一——桌面存储适配到框架会话（获得跨端恢复），或文档明确"桌面会话是宿主私有、
      不可移植"的立场。不允许维持"两套都在但互不知道"的现状。

### 1.3 小项

- [x] **1.3.1**（2026-08-30 完成：whenSidecarSupervisor 启动承诺；handle/skill-loader 测试 19 例） skill-loader 启动竞态：sidecar 未就绪时静默返回空（实测启动日志
      `no skills loaded`）。加就绪后重试，或挂到 supervisor 的 ready 事件上。
- [x] **1.3.2**（2026-08-30 完成：sidecar `compat.describe` RPC 服务化旗标词汇表（2.3.3 单一真源），
      桌面设置页"高级兼容旗标"区按描述符动态渲染（bool 三态/枚举/列表），
      `sanitizeCompatOverrides` 收敛持久化；canary 第 11 节模拟用户操作
      验证开关-保存-持久化全链路） compat 显式开关：桌面设置页暴露
      `OpenAICompatFlags` 关键旗标，传给 `chat.stream` 的 `compat` 参数。
- [x] **1.3.3**（2026-08-30 完成：默认关、`STEERABLE_EGRESS_PROXY=1` 显式开启；
      `src/sidecar/egress-proxy.ts` 计划/启动/TCP 探针就绪；开启后 Seatbelt
      只放行代理端口、主机名单由代理持有；启动失败回退端口级管控（加固
      不断 LLM 通路）；build_sidecar.py 打包 egress-proxy。实盘验证：
      curl 经代理 401（隧道通）/ 禁主机 000（拒绝）；桌面开启后
      DeepSeek 请求经隧道拿到真实 401（key 过期，路径通），Seatbelt
      egress 只剩 127.0.0.1:57780）桌面集成 `steerable-egress-proxy`。
- [x] **1.3.4**（2026-08-30 评估完成，结论：2 项早已去重、3 项不换并写明理由）
      - **已去重**：`useChatList`（桌面 `useChatsAndAgents.ts` 就是它的 transport
        适配器）、`useChatStream`（AgentPage 直接用）、`ToolExecutionCard`
        （桌面 `ExecutedActionsCard` 已委托它渲染，自身只是 executed_actions
        SSE 形状的薄适配器）。加上 W1.1 的 `OrchestrationPlanCard`，桌面消费
        agent-ui 表面达 4 处（出口线 4/8 达成）。
      - **不换 `MessageList`**：agent-ui 版是 headless 最小滚动容器（恒
        auto-scroll）；桌面版有 near-bottom 检测 + 回到底部浮钮 + agent 徽标
        + executed-actions + 编排子代理卡片 + 轮次状态，换 = UX 回退。
      - **不换 `useChatSession`/`ChatSessionProvider`**：它只是 useChatStream
        +useChatComposer 的便捷组合；桌面 ChatInput（1566 行：轮中转向、
        ⌘+Enter 排队、skill 提及、文件拖拽）远超 useChatComposer 的
        draft+enter 模型；底层原语已在用，组合层无换的价值。
      - **不换 `ToolCallRenderer`**：其内联审批按钮模式与桌面模态审批流
        （ApprovalModal + 反向通道桥）冲突；工具执行展示已由
        ToolExecutionCard 覆盖。

**出口**：`framework-desktop-parity` 复测，RPC 接通 11 → 15+，agent-ui 组件 2/8 → 4/8+。

---

## W2 · R9 剩余差距（B 源，框架侧建设）

出口：R10 复评时对应轴的判定移动，且每个移动都有 file:line 证据。

### 2.1 MCP 生态（R9 最大差距 ①：架构上没有任何对应物）

MCP 已是 2026 事实工具集成标准；codex 客户端+服务端齐备，DSH 有 client，我们为零。

- [x] **2.1.1**（2026-08-29 已落地（9e612ea）：mcp.py 限定名/目录上限/stdio client；test_mcp.py 14 例含 loop 端到端） 先做 **MCP 客户端**：框架作为 MCP client 接入外部 MCP server 的工具，
      经既有 `@tool` 契约投影进 CoreLoop（工具面不变，多一个来源）。
- [x] **2.1.2**（分工已定并文档化：桌面宿主侧 MCP（toolsViaHost 一等工具），框架 client 服务嵌入型宿主；roadmap L224/L398） 与桌面的关系定义：deeppath-agent 已有 `mcp-executor.ts`（宿主侧 MCP），
      框架侧客户端落地后明确两者分工——框架管循环内工具，桌面管宿主集成，避免双头。
- [ ] **2.1.3**（二期再定）MCP 服务端：把框架工具面暴露为 MCP server。先不做，
      客户端落地后复评必要性。

### 2.2 Windows 沙箱 + 出网凭证层（R9 最大差距 ②，共用前置：宿主侧 spawn）

- [x] **2.2.1**（2026-08-30 契约+路由层完成：`host.process.spawn` 反向 RPC 契约落地
      ——sidecar `HostSpawnExecutor`（无本地改写后端且 `execSandbox.hostSpawn` 时接管
      shell 调用，策略随请求下发，宿主自报 enforcement，缺能力 fail-closed 绝不回退
      裸跑）；TS runtime `onProcessSpawn` 注册位 + 反向分发；sidecar 单测 6 例 +
      TS 测试 2 例。**剩余**：Windows 原生 spawn 助手本体（受限令牌 + JobObject）
      需 Windows 环境实现与验证，契约已就位） Windows 改为 host 侧 spawn 路线
      （参照 codex 受限令牌 + JobObject + WFP、DSH 受限令牌 + kill-on-close Job）：
      sidecar 不改写命令，改为向宿主请求受约束 spawn。
- [x] **2.2.2**（2026-08-30 完成：egress-proxy 凭证注入模式——`--inject-host`/
      `--inject-secret-env` 把代理变成凭证持有者，plain-HTTP 绝对 URI 命中即剥客户端
      凭证、注入真实密钥、TLS 转发；密钥只经 env 进代理进程。框架测试 13 例
      （改写/剥离/403/405/501/SSE 增量流/CLI）。桌面接线：broker 活跃时 router 把
      baseUrl 改写 http 且不下发 apiKey，sidecar 只经 HTTP_PROXY 走代理。
      **实盘验证**：curl 经桌面代理拿到 DeepSeek 真实 401 点名注入密钥尾号 e5d8
      （curl 未带任何凭证）；UI 聊天全链路同样 401 回 surfacing；禁主机 403）
      出网管控从主机允许列表升级到**凭证代理**（credential broker）：agent 侧永远
      拿不到真实 token（codex network-proxy 的路线）。
- [x] **2.2.3**（2026-08-30 完成：docs/spec/safety.md 新增"Host capability surface"
      章——`host.process.spawn` 请求/应答/规则 + 凭证代理模式契约，两条共用；
      egress-proxy README 同步） 两条共用同一个"宿主能力面"契约文档
      （docs/spec/safety.md 扩展）。

### 2.3 Provider 注册表兑现（R9 最大差距 ③：机制已建成，注册表只有 1 条）

- [x] **2.3.1**（接入 Moonshot（温度/effort 双翻转，platform.kimi.ai 文档 + vercel/ai#19543 实测 400 佐证）、OpenRouter、DashScope；纯数据零解析改动；compat 测试 16 例） 真实接入第二、第三家 OpenAI 兼容厂商（建议：一个国产 reasoning 厂商 +
      一个海外主流厂商），只填 `OpenAICompatFlags` 数据、不改解析代码——
      这正是 P2.1 验收测试承诺的边际成本。
- [x] **2.3.2**（每厂商注释含旗标翻转原因与文档出处，标注 doc-verified 2026-08-30 待 key 实测） 每接一家，`PROVIDER_COMPAT_HOSTS` 加条目 + 对应厂商的实测记录
      （哪个旗标必须翻、为什么）留在 compat.py 注释里。
- [x] **2.3.3**（2026-08-30 随 1.3.2 完成：桌面设置页 compat 区由 sidecar
      `compat.describe` RPC 的旗标描述符动态渲染，框架 compat.py 是唯一真源，
      新增旗标零桌面改动；canary 第 11 节验证） 与 1.3.2 联动：桌面设置页的
      compat 开关复用同一份旗标定义。

### 2.4 审批策略机（R9：决策格追平，策略机落后）

- [x] **2.4.1**（2026-08-30 完成：`approval_policy.py`——`ApprovalRule`
      （tool 精确名 + argv 前缀 token，shlex 解析失败 fail-closed 不匹配）、
      `ApprovalPolicy`（有序首中即决）、`JsonApprovalPolicyStore`（原子写、
      损坏文件 fail-closed 为空策略）、`PolicyApprover`（规则命中免询问，
      未中委派内层）；sidecar `approval.policyPath` 接线，决议顺序=
      持久类别→会话类别→规则→宿主。框架测试 14 例 + sidecar 端到端 3 例）
      规则引擎：持久化的 execpolicy 类规则（命令模式 → 自动 allow/deny），
      与现有八变体决策格正交。
- [x] **2.4.2**（2026-08-30 完成：宿主 approval.request 应答可携
      `amendment: {decision, commandPrefix?}`——sidecar 解码为规则
      （工具名锁定为被批准调用的工具，不可重定向），同时写入内存策略
      （同轮即生效）与持久文件（跨轮生效）；无效修正案丢弃不阻决策；
      未接线 sink 时告警丢弃。端到端验证：同轮第二次 echo 免询问 +
      规则落盘） 修正案载荷：用户批准的同时可顺带改策略（"以后都允许
      这类"），而不是每次重问。

### 2.5 编排工具面与跨厂商委派（R9：核心追平，广度落后）

- [x] **2.5.1**（2026-08-30 完成：六工具 + 多轮 resume + interrupt + dedup_exempt 协议；编排测试 22 例） 编排原语从 4 个补齐到对齐 codex 的 8 个：补 `send_message`（已派生代理的
      二次输入）、`interrupt_agent`、`list_agents`、`followup_task`。
- [ ] **2.5.2**（二期）跨厂商委派：编排执行器支持把子任务委派给非自家循环
      （DSH 可路由 claude-code/codex/acp）。依赖 2.1 MCP 客户端落地后再评。
- [x] **2.5.3**（2026-08-30 满足：1.1 桌面接线（组一）在 2.5.1 工具面补齐之后
      落地，桌面一次接到六工具版本，无二次接线） 与 1.1 联动：桌面接线时工具面
      应已是补齐后的版本，避免桌面接两次。

### 2.6 会话工程设施（R9：语义领先，设施薄）

- [x] **2.6.1**（2026-08-30 完成：`SqliteStorage`——stdlib sqlite3 零依赖
      （沿用 W2.7.1 决策逻辑：嵌入包体积预算内不引 SQLAlchemy）；实体全量
      JSON 存 data 列，过滤/排序字段（id/chat_id/seq/created_at）冗余为索引列，
      会话枚举与 resume 尾扫皆为索引查询；`search_sessions` 按消息内容 SQL
      检索；WAL 模式读不阻塞写。sidecar `--storage-path` 接线（重启会话仍在），
      桌面传 `~/.steerable/sessions.db`——Seatbelt 唯一可写根，userData 在沙箱
      下会被拒。语义对齐 InMemoryStorage：消息 limit 取尾、after_seq 排他
      （修正协议 docstring 原误书 inclusive）、trace 计数器随 append 更新、
      list_history_records 分支发现扩展。测试 7 例 + sidecar 接线 1 例）
      sqlite 后端 + 会话索引（codex rollout 15k 行、DSH session 16 子包的
      对应物）：会话枚举/搜索不再扫 jsonl。
- [x] **2.6.2**（2026-08-30 完成：`maintenance.py` 四作业 + CLI
      （`python -m steerable_agent_runtime.maintenance`）——`check`
      （integrity_check 预检，他作业遇损拒跑）、`compact`（清旧
      traces/spans/events + VACUUM，会话/消息/历史不动）、`archive`（旧会话
      连消息搬入独立归档库，归档库本身可读）、`salvage`（损坏库逐行导出
      JSONL，跳过必计数）。测试 5 例） 维护作业：压缩、归档、损坏修复的离线工具。
- [x] **2.6.3**（2026-08-30 完成：构造保证——history 表形态无关（seq 序 JSON
      行），CompactionBoundary 条目字节级往返一致；测试走真实 resume 路径
      （load_history_items 反向扫到边界、只投影边界后条目）；维护作业不改写
      历史记录） 保持 `CompactionBoundary` 可审计语义不变——这是领先点，
      设施加厚不能丢。

### 2.7 可观测性加厚（R9：落后）

- [x] **2.7.1**（2026-08-30 完成：决策记录落 docs/spec/runtime.md「Observability
      export decision」——维持零依赖手写 OTLP/HTTP。理由：sidecar 嵌入桌面受 CI
      体积门禁，SDK 依赖链换不来后端适配面（OTLP/HTTP JSON 线格式即兼容面，
      Jaeger/Tempo/Honeycomb 均可摄入）；真正的缺口是 span 覆盖而非导出器。
      重访条件：后端需要手写映射表达不了的特性（exemplars、log 关联）时再议）
      决策：继续零依赖手写 OTLP 并补 span 模型/采样，还是引入真
      opentelemetry-sdk（依赖体积换后端适配面）。先出决策记录再动手。
- [x] **2.7.2**（2026-08-30 完成：span 模型对齐 OTel 语义——loop 新增
      `llm_request`/`llm_response` 事件对（每次 provider 请求一对，重试按
      attempt 可见，错误亦闭 span）；`ApprovalExecutor` 实际询问时把
      `_approval{kind,category,waitMs}` 标记并入 ToolResult.data，loop 提升进
      `tool_call_result` 事件；TraceRecorder 据此产出三类 span（`llm.request`
      每请求一个、`tool` 每派发一个、`approval.wait` 挂为工具 span 子 span），
      并实现确定性头采样（`sample_rate`，trace id 哈希定桶，未采样零持久化、
      事件照常透传）；otel.py 导出按 kind 命名（tool.<name> 保持旧形）并兑现
      parentSpanId 嵌套。runtime.md span 表同步。框架 945 + TS 29 + evals 37
      全 PASS） span 模型对齐 OTel 语义（工具调用、LLM 请求、审批等待为独立 span）。

### 2.8 小项

- [x] **2.8.1**（2026-08-30 完成：`LoopConfig.steer_mode`（`"boundary"` 默认 /
      `"interrupt"`）落地——interrupt 模式下 steer 到达即在途工具阶段被取消
      （与 cancel 共用竞速机制，但轮次继续：在途调用记 synthetic 中断结果、
      未启动调用记 skip 通知、不计入连续错误熔断），下一轮边界 drain 把 steer
      交给模型；sidecar `steerMode` 参数接线；steer 测试 8 例（中断在途/
      跳过未启动/默认边界不变/非法值拒绝）） 循环内核排空策略可配置化
      （pi 的 steeringMode 比我们的轮次边界排空更细）：`steer` 到达时排空中断
      vs 排空完成的策略做成 `CoreLoopConfig` 字段。
- [x] **2.8.2**（2026-08-30 完成：`SystemPromptFragment`（role=system、无标记
      保前缀缓存、4096 cap + review_note）+ `render_fragment_capped` 单一强制
      点（append_fragment 与种子边界共用）；sidecar `systemPrompt` 参数（与
      messages 内 system 消息互斥，fail loud）；桌面 router 改为参数下发。
      桌面注入面盘点：system prompt（本次类型化）、worldState/skills 目录
      （已类型化）、@引用摘录与附件说明（用户发起内容，桌面侧已封顶，立场：
      不算框架片段）。框架门禁测试自动覆盖新类型） 上下文片段覆盖扩展：
      机制（max_tokens/degrade/CI 门禁）已领先，片段类型从个位数向 codex 的
      53 处 impl 靠拢——优先把桌面实际注入的片段类型全部类型化。

---

## 顺序与依赖

```text
W1.1 编排接线 ────────────┐
                          ├─ 与 W2.5 同源：建议 W2.5.1 先补工具面，桌面一次接到位
W2.5 编排工具面 ──────────┘
W2.1 MCP 客户端 ──→ W2.5.2 跨厂商委派（二期）
W2.2 宿主 spawn 契约 ──→ W1.3.3 桌面 egress 集成（可选）
W2.3 Provider 兑现 ──→ W1.3.2 桌面 compat 开关
其余各条独立，可按人力并行
```

**建议落地顺序**：W1.3.1（skill 竞态，小且影响日常）→ W2.5.1 + W1.1（编排一体两面包）
→ W2.1（MCP 客户端，最大生态缺口）→ W2.3（Provider 兑现，机制收益兑现）
→ W2.2（Windows + 凭证代理，最大安全缺口）→ 其余按资源排。

## 复评出口

- 每条完成时在对应 canvas 复测更新判定，证据精确到 file:line（沿用 R9 纪律：
  未能定位 file:line 的能力不计入判定）。
- 全部完成后出 R10 四方复评 + 桌面 parity 复测，两份 canvas 同步刷新。
