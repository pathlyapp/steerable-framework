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
- [ ] **1.3.2** compat 显式开关：桌面设置页暴露 `OpenAICompatFlags` 关键旗标，
      传给 `chat.stream` 的 `compat` 参数。当前靠框架 URL 自动探测兜底，
      用户自建 OpenAI 兼容端点（私有部署）可能漏配。
- [ ] **1.3.3**（可选）桌面集成 `steerable-egress-proxy`：`main.ts` 已有注释指引，
      集成后 Seatbelt 的端口级管控升级为按主机名管控。
- [ ] **1.3.4**（卫生）桌面自绘组件与 agent-ui 导出的去重评估：`ToolCallRenderer`、
      `state/` 会话原语（`useChatList` / `useChatSession` / `ChatSessionProvider`）
      vs 桌面 `MessageList` / `LocalChatPanel`。能换则换，不能换写明理由。

**出口**：`framework-desktop-parity` 复测，RPC 接通 11 → 15+，agent-ui 组件 2/8 → 4/8+。

---

## W2 · R9 剩余差距（B 源，框架侧建设）

出口：R10 复评时对应轴的判定移动，且每个移动都有 file:line 证据。

### 2.1 MCP 生态（R9 最大差距 ①：架构上没有任何对应物）

MCP 已是 2026 事实工具集成标准；codex 客户端+服务端齐备，DSH 有 client，我们为零。

- [ ] **2.1.1** 先做 **MCP 客户端**：框架作为 MCP client 接入外部 MCP server 的工具，
      经既有 `@tool` 契约投影进 CoreLoop（工具面不变，多一个来源）。
- [ ] **2.1.2** 与桌面的关系定义：deeppath-agent 已有 `mcp-executor.ts`（宿主侧 MCP），
      框架侧客户端落地后明确两者分工——框架管循环内工具，桌面管宿主集成，避免双头。
- [ ] **2.1.3**（二期再定）MCP 服务端：把框架工具面暴露为 MCP server。先不做，
      客户端落地后复评必要性。

### 2.2 Windows 沙箱 + 出网凭证层（R9 最大差距 ②，共用前置：宿主侧 spawn）

- [ ] **2.2.1** Windows 改为 host 侧 spawn 路线（参照 codex 受限令牌 + JobObject + WFP、
      DSH 受限令牌 + kill-on-close Job）：sidecar 不改写命令，改为向宿主请求受约束 spawn。
      前置：定义"宿主 spawn 能力面"的 RPC 契约。
- [ ] **2.2.2** 出网管控从主机允许列表升级到**凭证代理**（credential broker）：
      agent 侧永远拿不到真实 token（codex network-proxy 的路线）。egress-proxy
      增加凭证注入模式，密钥只存在于代理进程。
- [ ] **2.2.3** 两条共用同一个"宿主能力面"契约文档（docs/spec/safety.md 扩展）。

### 2.3 Provider 注册表兑现（R9 最大差距 ③：机制已建成，注册表只有 1 条）

- [ ] **2.3.1** 真实接入第二、第三家 OpenAI 兼容厂商（建议：一个国产 reasoning 厂商 +
      一个海外主流厂商），只填 `OpenAICompatFlags` 数据、不改解析代码——
      这正是 P2.1 验收测试承诺的边际成本。
- [ ] **2.3.2** 每接一家，`PROVIDER_COMPAT_HOSTS` 加条目 + 对应厂商的实测记录
      （哪个旗标必须翻、为什么）留在 compat.py 注释里。
- [ ] **2.3.3** 与 1.3.2 联动：桌面设置页的 compat 开关复用同一份旗标定义。

### 2.4 审批策略机（R9：决策格追平，策略机落后）

- [ ] **2.4.1** 规则引擎：持久化的 execpolicy 类规则（命令模式 → 自动 allow/deny），
      与现有八变体决策格正交。
- [ ] **2.4.2** 修正案载荷：用户批准的同时可顺带改策略（"以后都允许这类"），
      而不是每次重问。

### 2.5 编排工具面与跨厂商委派（R9：核心追平，广度落后）

- [x] **2.5.1**（2026-08-30 完成：六工具 + 多轮 resume + interrupt + dedup_exempt 协议；编排测试 22 例） 编排原语从 4 个补齐到对齐 codex 的 8 个：补 `send_message`（已派生代理的
      二次输入）、`interrupt_agent`、`list_agents`、`followup_task`。
- [ ] **2.5.2**（二期）跨厂商委派：编排执行器支持把子任务委派给非自家循环
      （DSH 可路由 claude-code/codex/acp）。依赖 2.1 MCP 客户端落地后再评。
- [ ] **2.5.3** 与 1.1 联动：桌面接线时工具面应已是补齐后的版本，避免桌面接两次。

### 2.6 会话工程设施（R9：语义领先，设施薄）

- [ ] **2.6.1** sqlite 后端 + 会话索引（codex rollout 15k 行、DSH session 16 子包的
      对应物）：会话枚举/搜索不再扫 jsonl。
- [ ] **2.6.2** 维护作业：压缩、归档、损坏修复的离线工具。
- [ ] **2.6.3** 保持 `CompactionBoundary` 可审计语义不变——这是领先点，设施加厚不能丢。

### 2.7 可观测性加厚（R9：落后）

- [ ] **2.7.1** 决策：继续零依赖手写 OTLP 并补 span 模型/采样，还是引入真
      opentelemetry-sdk（依赖体积换后端适配面）。先出决策记录再动手。
- [ ] **2.7.2** span 模型对齐 OTel 语义（工具调用、LLM 请求、审批等待为独立 span）。

### 2.8 小项

- [ ] **2.8.1** 循环内核排空策略可配置化（pi 的 steeringMode 比我们的轮次边界排空更细）：
      `steer` 到达时排空中断 vs 排空完成的策略做成 `CoreLoopConfig` 字段。
- [ ] **2.8.2** 上下文片段覆盖扩展：机制（max_tokens/degrade/CI 门禁）已领先，
      片段类型从个位数向 codex 的 53 处 impl 靠拢——优先把桌面实际注入的片段类型全部类型化。

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
