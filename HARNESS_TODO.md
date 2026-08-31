# Harness 计划（R10 复评产出的三个重点方向）

> 目标：把 R10 四方复评的结论收敛成一张可执行的规划。
> 来源（2026-08-31）：
> - **A 源**：canvas `steerable-r10-framework-review` —— 四框架源码实测（14 轴：
>   领先 3 / 追平 4 / 落后 4 / 混合 3），四份 file:line 证据取自当日各仓 HEAD。
> - **B 源**：2026 年公开研究与厂商实践 —— arXiv 2605.23950（harness 披露协议）、
>   arXiv 2605.27922（Harness-Bench）、HarnessLab（harness 六模块划分）、
>   arXiv 2603.05344（OpenDev：自适应压缩 + 系统提醒）、arXiv 2510.11967（context folding）。
>
> **核心判断**：harness 已被实证为一阶变量——固定模型只换 harness，
> Terminal-Bench 2 的 pass@1 从 69.7% 到 77.0%，LangChain 换 harness 从 52.8% 到 66.5%，
> Opus 修复 harness 缺陷后 CORE-Bench 从 42% 到 95%。我们是四家里唯一自我定位为
> **框架**的，且是唯一把公开基准与竞品 agent 一起钉进 CI 的（`evals/suite.yaml`
> 已注册 oracle / claude-code / codex / pi / steerable 五个 Harbor agent）。
> 这条轴目前没有任何一家拥有，是我们体量最小却最有利的坐标。
>
> **与既有 TODO 的分工**（不要重复开条目）：
> - [`PARITY_TODO.md`](./PARITY_TODO.md) —— R9 之前的架构追平（已完成）
> - [`ALIGN_TODO.md`](./ALIGN_TODO.md) —— 框架↔桌面接线 + R9 剩余差距（已完成，
>   仅 2.1.3 MCP 服务端与 2.5.2 跨厂商委派留二期，本文件接手其中一条）
> - [`EVALS_TODO.md`](./EVALS_TODO.md) —— 评测流水线本身的建设
> - [`CORELOOP_TODO.md`](./CORELOOP_TODO.md) —— CoreLoop 自身能力演进
> - **本文件** —— 只收 R10 判定为重点的三个方向，加一条必补的诚实缺口
>
> **明确不做**（R10 决策，理由见文末）：追 provider 的 wire 适配器数量；插件市场；
> 在框架内自建 Windows 沙箱原语；跨厂商委派升为重点。

---

## P0 · ACP 路径上审批归零（安全缺口，不排队）

**单独列在最前面，因为它既不依赖本文件的任何其他工作项，也不该等它们。**

`acp_adapter.py:210` 构造执行器时写的是：

```python
RouterToolExecutor(router, consent_granted=True),
```

对比正规 sidecar 路径——`sidecar.py:1220` 用 `ApprovalExecutor` 包了一层，
`sidecar.py:429` 从参数读 `consentGranted` 且默认 `False`。ACP 路径**没有
`ApprovalExecutor`**，且把 consent 写死为 `True`；而 `tools.py:251` 的门是
`if tool.require_consent and not consent_granted`，于是这道门永远不触发。

后果：`ALIGN_TODO.md` W2.4 刚落地的整套审批机制——`ApprovalPolicy`、规则引擎、
`JsonApprovalPolicyStore`、八变体决策格——在 ACP 上一个都到不了。
编辑器经 ACP 拉起我们，拿到的是所有工具无条件执行的 agent。
（shell 安全模式匹配是另一层，仍然生效；归零的是 consent 这道门。）

严重性在于审批代数是 R10 认定的**领先点**。W3.2.2 写过「不能在 MCP 边界上退化成
允许/拒绝两态」——ACP 这边已经比两态更糟，是零态。而 `acp.Client.request_permission`
就在 SDK（`agent-client-protocol 0.12.1`）里放着，没接。

`headless.py:82` 是同样的写法，但可以接受：它跑在 Harbor 试题容器内且 `jailed=True`。
ACP 不同——它在开发者的真实机器上被编辑器拉起。**两处必须区别对待，
不要图省事一起改成同一个值。**

- [x] **P0.1** ACP 路径改为经 `ApprovalExecutor` 执行，`consent_granted` 不再写死。
      `acp_adapter.py` prompt() 现在装配 `RouterToolExecutor`（门闩保持 armed）
      + `ApprovalExecutor`（带每会话 `SessionApprovalCache`）；无客户端时门闩
      fail-closed。
- [x] **P0.2** 接 `acp.Client.request_permission`：审批决策经 ACP 回调交给客户端。
      新增 `AcpApprover`：read 模式静默放行（编辑器不门禁读操作），其余模式
      弹出四选项；allow/always/reject/always-reject 映射到八变体格的
      allow_once/allow_always/deny_once/deny_always（无 durable store 时由
      ApprovalExecutor 响亮降级到会话域），取消/未知选项/连接失败 fail-closed
      为 deny_once。**未塌缩成两态**。`ApprovalRequest` 增加 `call_id` 字段
      供 ACP 关联工具卡片。
- [x] **P0.3** 回归用例：一个 `destructive` 模式的工具在 ACP 会话中必须触发
      `request_permission`，拒绝后不执行。三个新测试：
      允许则执行且 tool_call_id 关联、拒绝则不执行且进度为 failed、
      read 工具不触发弹窗。
- [x] **P0.4** 复核 `headless.py:82` 的豁免理由并写成注释——
      豁免成立（Harbor 容器内 + jailed + 安全黑名单仍生效 + AutoApprover
      会拒掉 bash 毁掉评测），前提已写入注释并明确禁止照抄到交互路径。

---

## W1 · Harness 矩阵化（重点一，最高杠杆）

**现状缺口**：`LoopConfig` 只有 12 个旋钮（`loop.py:234–292`：`max_rounds`、
`steer_mode`、`parallel_tools`、`tool_timeout_ms` 等）；压缩、重试、校验三类策略
各只有一个实现（`CompactionHooks` / `RetryHooks` / `before_completion` 纪律重试），
全部在 sidecar 工厂里手工装配（`hooks.py:173–215`、`sidecar.py:141–152`）。
我们有接缝，但没有矩阵。

**出口**：能产出「锁模型、变 harness」的方差归因报告——目前没有任何开源 agent
框架能自证这项能力，而学界刚刚宣布它是有效比较的前提。

### 1.1 模块化接口与基线实现

对齐 HarnessLab 的六模块划分。不推翻现有 `LoopHooks`，而是在其之上把「策略」
从「装配」中分出来：hook 仍是接缝，模块是可命名、可替换的策略单元。

- [x] **1.1.1** 定义六个模块的 Protocol：`ContextManager`、`RetryPolicy`、
      `ToolSelector`、`MemorySystem`、`OutputValidator`、`Orchestration`。
      每个 Protocol 必须能由现有 hook 实现适配，不得要求重写 CoreLoop。
      已实现 `harness.py`：维度名与既有类型撞名（`ContextManager` 在 history.py、
      `RetryPolicy` 在 agent-harness），协议取 `*Strategy` 后缀——
      `ContextStrategy`/`RetryStrategy`/`ValidationStrategy` 产出 `LoopHooks`，
      `ToolSelection` 过滤模型可见描述符，`MemoryStrategy` 产出 `StorageAdapter`
      + 可选注入 hooks，`OrchestrationStrategy` 包装 `ToolExecutor`。
      跨切片实现（CompactionHooks 同时占 pre_step 与 on_request_error）用
      投影适配器拆到两个维度，装配不会重复应用。
- [x] **1.1.2** **补齐基线（Null）实现**——这是全篇最容易被跳过、却最关键的一条：
      没有 `NullContext` 就无法测出 compaction 的边际贡献，没有 `NoRetry` 就无法
      归因重试策略。当前六个模块各只有一个「真」实现，一个基线都没有。
      | 模块 | 已有 | 需补 |
      | --- | --- | --- |
      | ContextManager | `PressureCompaction`（CompactionHooks） | ~~`NullContext`~~ ✅；~~`ObservationAging`~~ ✅（W2.1 已产出） |
      | RetryPolicy | `SimpleRetry`（RetryHooks） | ~~`NoRetry`~~ ✅；~~`InformedBacktrack`~~ ✅（CompactionHooks 溢出切片） |
      | ToolSelector | `ProgressiveDisclosure`（exposure 三级 + tool_search） | ~~`FullToolset`~~ ✅；~~`MinimalToolset`~~ ✅（命名现有评测基线） |
      | MemorySystem | `Stateless` | ~~`FilesystemState`~~ ✅（AGENTS.md 笔记注入，round 0 一次） |
      | OutputValidator | `SelfCritique`（纪律重试 + narration） | ~~`NullValidator`~~ ✅ |
      | Orchestration | `SubAgentDelegation`（AgentPool 六工具） | ~~`SingleAgent`~~ ✅ |
      表中「已有」指**框架里有**。评测路径上并不是这一套：`headless.py:79` 只装
      4 个工具、不接 AgentPool，所以 Harbor agent 实际跑的是 `MinimalToolset` +
      `SingleAgent`。这两个基线是白捡的——已经存在，只是此前没被当作基线命名。
      兑现见 §1.4。
- [x] **1.1.3** 每个实现附一句「它假设了什么」的契约说明，供归因报告解读时引用。
      每个实现的 `assumes` 字段即契约；注册表一致性测试强制非空。

### 1.2 声明式装配

- [x] **1.2.1** `HarnessSpec` 数据类 + YAML/JSON 加载器：一个 harness 是一份声明，
      不是一段工厂函数。装配从 sidecar 工厂移到 spec 解析。
      已实现 `harness_spec.py`：hooks 型维度（context/retry/validator）允许列表
      组合（现状默认就是 backtrack+simple 的组合），tools/memory/orchestration
      为单选；`runtime_params` 解决模型相关参数（如 max_context_tokens）的
      运行期注入，spec 字面值优先。YAML 走 PyYAML 惰性导入（非运行时依赖），
      JSON 零依赖。
- [x] **1.2.2** sidecar `--harness` 参数 + `harness.describe` RPC，照 `compat.describe`
      的先例（W1.3.2 已验证这个模式：框架是唯一真源，宿主按描述符动态渲染）。
      已落实：headless `--harness PATH` 走 `_assemble_harness`——spec 的
      context/retry/validator/memory 策略**替换**内建默认链（因子协议的本意），
      DeliveryHooks 保留（传输语义不是 harness 维度）；`harness.describe` RPC
      返回注册表全量词汇 + 默认 spec 选择，RPC 级测试锁定 describe/registry 不偏斜。
- [x] **1.2.3** fail loud：未知模块名或未知实现名在**加载期**报错，不静默跳过。
      沿用既定立场——缺失的引用绝不当作空配置放过。已实现并测试：未知维度、
      未知实现名、未知条目键、单选维度给列表、缺维度、坏参数名六类全在加载/
      装配期报错。
- [~] **1.2.4** 现有默认装配等价迁移为一份 `default.harness.yaml`，
      迁移前后行为逐测试比对，证明这一步是纯重构。
      spec 文件已入库（pressure_compaction+spill / informed_backtrack+simple /
      null / full / stateless / single），等价性测试已验证三种核心行为
      （压力压缩、溢出回退、瞬态退避）。sidecar 工厂的实际切换待 arm A 基线
      完成后进行（改既有文件）。

### 1.3 评测台升级为因子设计

- [x] **1.3.1** `evals/suite.yaml` 增加 harness 维度，运行单元从 agent 变为
      agent × harness 的笛卡尔积。保持 pinned 的 `git_rev` 与 89 题 catalog 不变。
      已落实：`harnesses:` 段（标签 → spec 文件，加载时验证文件存在——指向空处的
      标签会以新名字静默重跑默认配置）；`AgentSpec.accepts_harness` 只有 steerable
      为 true（基线 agent 按出厂形态跑，harness 不是它们的变量）；`run.py --harness`
      把任务目录命名为 `<agent>-<harness>` 并经 `--agent-kwarg harness=<abs path>`
      传给 harbor_steerable，后者上传 spec 进容器并加 `--harness` 标志——
      headless 支持该标志前（W1.2.2）argparse 会响亮失败，不会静默错标。
      笛卡尔积由多次调用组合（run.py 保持单次运行原语）。
- [ ] **1.3.2** 锁模型对照运行：同一模型、同一题集、N 个 harness。
      `suite.yaml:167–169` 的注释已经预留了做法（`--model openai/gpt-5.5` 同档对照），
      本条是把它真正跑起来。
- [x] **1.3.3** 方差归因输出：报告 harness 主效应与模型主效应、二者比值、
      以及跨 harness 的模型排名反转次数（arXiv 2605.23950 要求的披露口径）。
      已实现 `evals/attribution.py`：`load_job` 从 config.json 取 agent/model、
      从各 trial 的 verifier_result.rewards 取分（verifier 未打分的 trial 跳过
      而非计零——基建失败不与任务失败混淆）；`attribute` 只在所有 job 共有的
      题集上算均值（配对完整， harness 不能躲掉它的最差题）；主效应为固定另一
      因子后各水平均值的极差再取均值；CLI `--job HARNESS=PATH` 打标签，
      markdown 报告含全部四项披露指标。11 项测试。
- [x] **1.3.4** 归因报告进 CI 产物（不设为门禁——跑分有成本，先做可复现产物）。
      已落实：evals-weekly 的 eval 工件补上 `config.json`（归因需要 agent/model
      标签，trial 级 config 由 `"agents"` 键区分）；新增 `attribution` job
      （needs: eval, if: always）下载全部工件、按 job 目录重建、跑
      `evals.attribution` 生成 markdown 上传为 `attribution` 工件并写进步骤摘要。
      矩阵未引入 harness 维度前报告如实标注 n/a（harness 主效应需要 ≥2 个水平）。

### 1.4 评测 agent 工具面补齐（1.1.2 中 ToolSelector 与 Orchestration 两行的首次兑现）

**实测基线**（2026-08-31）：`headless.py:79` 调 `workspace_tools_for_cwd(cwd, jailed=True)`，
Harbor 评测 agent 只拿到 4 个工具——`bash`、`read_file`、`write_file`、`edit_file`
（`workspace_tools.py:297–335`）。循环配置 `max_tool_errors=16`、`tool_dedup=False`，
hooks 为 `DeliveryHooks + CompactionHooks + RetryHooks`，存储 `InMemoryStorage`。
没有结构化搜索、没有多文件补丁、没有交互式会话、没有接 AgentPool。
对面 codex 带 30 余个工具进 Terminal-Bench，claude-code 十余个。

**定位**：本节不是「加功能」，是 W1 的第一个消融实验——只变工具面一个模块，
其余全锁死，看分数差。它同时检验「harness 是一阶变量」这个前提在**我们的题集上**
是否成立；若不成立，W1 的立论就要重估，这比任何单个工具都重要。

#### 1.4.1 效率组：把已能做的事做得更省

三条都不开新题型，只降 token 与轮次。省下的上下文直接换成更多轮次预算。

- [x] **1.4.1.1** `grep` 结构化搜索。现状是模型只能用 `bash grep -rn`，输出无结构、
      被 `_clip` 砍到 32KB（`workspace_tools.py:25`），一次大仓搜索吃掉数千 token，
      且 shell 转义写错正则要重试。改为返回 `{path, line, text}` 列表并可限条数，
      同样信息约占三分之一到五分之一。`rg` 优先、纯 Python 回退（题目容器里不一定有 `rg`）。
      **纯逻辑层已实现**（`search_tools.py`，新文件不在评测路径）：SearchHit 结构化
      返回、limit 上限 500、行截断 500 字符、正则先校验再选后端（rg 非零退出不抛异常，
      校验必须前置才能 fail loud）、rg glob 用 `!**/dir/**` 锚定任意深度。
      **工具接线待 arm A**（workspace_tools.py 在评测路径）。
- [x] **1.4.1.2** `glob` 文件模式列举。同理；另有一个具体病症：模型常用 `ls -R`
      然后被 `node_modules` 淹没。与 1.4.1.1 共用遍历与忽略逻辑。
      已实现：`glob_files` 与 search 共用 `_walk_files` + IGNORE_DIRS
      （node_modules/.git/__pycache__/.venv/venv/dist/build），fnmatch 同时匹配
      相对路径与 basename。接线同待 arm A。
- [x] **1.4.1.3** `apply_patch` 多文件原子补丁。现状改 5 个文件 = 5 次 `edit_file`
      = 5 个往返 = 5 次 LLM 请求；Terminal-Bench 大量题是多文件改动，这里砍的是**轮次**
      而不只是 token。原子性另有一个作用：部分失败可整体回滚，不留半改状态误导模型。
      **格式选择**：不抄 codex 的 `*** Begin Patch` 自有语法，也不用 unified diff
      （LLM 生成的行号经常错）。做成「多文件版 `edit_file`」，复用 `file_edit.py`
      已有的三级匹配（精确 → 容忍空白 → Unicode 归一），比行号稳。
      已实现 `multi_file_edit.py`：两阶段——全部文件先对原始字节规划（任一定位失败
      整体中止、零写入），再统一写入；写阶段失败回滚已写文件。重复文件条目拒绝。
- [x] **1.4.1.5**（补）工具接线：grep/glob/apply_patch 描述符已进
      `workspace_tools.py`（read/safe_write 模式、工作区管辖、与 edit_file
      共用 per-path 锁），4 项 router 级接线测试。
- [x] **1.4.1.4** 三个工具各自单测，与 `test_workspace_tools.py` 同风格（`tmp_path` 隔离）；
      路径逃逸用例必须覆盖——新工具同样受 `_resolve_under` 的工作区管辖约束。
      18 项测试全绿：结构化命中、忽略集、limit、正则/大小写、纯 Python 回退与 rg
      结果一致、原子中止零写入、写阶段回滚、resolver 逃逸拒绝、空白容忍匹配沿用。

#### 1.4.2 编排接线（作为独立变量，不与 1.4.1 混算）

- [x] **1.4.2.1** 在 `headless.py` 注册 AgentPool 工具。经 `_assemble_harness`
      落地：spec 的 `orchestration: subagent` 触发 `SubAgentDelegation.wrap` →
      `OrchestrationExecutor` 包执行器，六工具委派面随 spec 进 headless。
      默认 arm（无 --harness）保持 single，arm A 基线不受污染。
- [~] **1.4.2.2** 单独成 arm 跑对照，不与 1.4.1 合并计分——否则无法区分是工具面
      还是编排带来的差异，就又回到了「凭感觉说变好了」。**arm C spec 已就位**：
      `evals/harnesses/subagent.harness.yaml`（与 default 仅 orchestration
      一维之差，契约测试锁定）+ suite.yaml 注册 `subagent` harness 标签。
      跑对照待 arm A 完成与 1.4.2.1 接线。

#### 1.4.3 对照协议（先注册后跑，本节的硬约束）

**这一小节写在动手之前，文件本身即预注册产物。** 跑完再挑指标或挑题集是自毁信誉，
而且我们刚在 R10 里引用了批评这种做法的论文。

- [ ] **1.4.3.1** 三个 arm，同模型、同题集、同轮次上限、同随机种子：
      | arm | ToolSelector | Orchestration |
      | --- | --- | --- |
      | A（基线，现状） | `MinimalToolset`（4 工具） | `SingleAgent` |
      | B | `FullToolset`（+ grep/glob/apply_patch） | `SingleAgent` |
      | C | `FullToolset` | `SubAgentDelegation` |
- [ ] **1.4.3.2** 题集固定为 cheap-12（避开 QEMU/GPU/视频/长编译），**跑之前**写死在
      本文件里；结果出来后不得增删题目。
- [ ] **1.4.3.3** 指标同样预先定死：pass@1、平均轮次、平均 token、峰值上下文占用、
      工具错误率、恢复率（工具失败后成功恢复的比例）、单题成本。
      **不只报 pass@1**——精简 harness 在成本归一化口径上有结构性优势，
      而 Harness-Bench 的口径本就包含 token 成本。
- [x] **1.4.3.4** arm A 必须在动任何代码之前先跑，否则没有基线。
      **已跑完**（2026-08-31，`evals/jobs/steerable-arm-a/2026-08-31__11-14-23`）：
      12/12 完成、0 错误、**mean 0.833**。满分 10 题；零分两题：
      password-recovery、sqlite-with-gcov。基线锁定，接线批开工。
- [ ] **1.4.3.5** 结果无论正负都写回本节。若 B 相对 A 无显著提升，
      结论是「工具面在我们的题集上不是主效应」——这同样是有效产出，并直接影响 1.3 的设计。

### 1.5 交互式会话（单独一组，不进 1.4 的对照）

当前 `bash` 是一次性 `Popen` + `communicate()`（`workspace_tools.py:169–202`），
进程结束即销毁。需要交互的题——REPL、ssh、要回答 y/n 的安装、gdb 调试——
我们现在**不是做得慢，是零分**。这条开的是新题型，与 1.4 的效率优化不同质，
所以分开一组、分开计分。

代价也最大：进程常驻、增量读 stdout、超时、会话回收、CoreLoop 异常退出时的清理，
是本计划里唯一有僵尸进程风险的一项。

- [x] **1.5.1** `bash_session` 常驻会话 + `write_stdin`，参照 codex 的 `exec_command`
      / `write_stdin` 分工。**会话层已实现**（`shell_session.py`，新文件不在评测路径）：
      PTY 而非管道（提示符、Ctrl-C 经终端行规、isatty 探测才有交互语义），
      读写游标增量返回，无提示符探测魔法——模型轮询判断完成，与 codex 同契约。
      **工具接线已完成**：bash_session/write_stdin 描述符进 `workspace_tools.py`
      （会话管理器挂在 router 上，headless 运行结束与 ACP prompt 结束都
      close_all 回收，不泄漏真进程）；4 项接线测试（开会话→命令→轮询→关闭、
      未知会话 fail loud）。
- [x] **1.5.2** 生命周期测试：超时、会话泄漏、异常退出时的进程组回收
      （复用 `_kill_process_group`，`workspace_tools.py:354–365`）。
      13 项真子进程测试全绿（4.8s）：状态跨读持久、read 提示符往返、Python REPL
      （头条新能力）、增量轮询、Ctrl-C 中断后会话存活、exit 7 上报、
      close 灭整个进程组、close_all 全回收、TTL 闲置回收、会话上限 fail loud、
      外部 kill -9 后下一次读报 exited。关键修正：交互式 bash 作业控制开着，
      后台作业有**自己的进程组**，killpg(shell) 会漏——回收顺序改为 SIGHUP
      （bash 转发给作业）→ killpg SIGKILL → ps 按 sid 清扫残余。
- [~] **1.5.3** 单独 arm 对照，题集须包含至少 3 道当前必然 0 分的交互题，
      否则这条的收益无法被 cheap-12 观测到。
      **题集预登记（2026-08-31，跑前写死，结果出来后不得增删）**：
      对 TB-2.1 全部 89 题 instruction 做交互标记扫描后的诚实结论——
      目录里硬交互题只有一道，题集按硬度分级：
      | 题 | 交互硬度 | 理由 |
      | --- | --- | --- |
      | `qemu-alpine-ssh` | 硬 | 必须读 VM 控制台输出并在登录提示符下应答；
      | | | 一次性 bash 只能靠 sleep+管道盲写击键，必然 0 分 |
      | `install-windows-3.11` | 半 | QEMU monitor socket 可程序化 sendkey，
      | | | 一次性 bash 理论可脚本化但极脆；会话读屏大幅降低难度 |
      | `headless-terminal` | 软 | 本体是写 PTY 类（一次性 bash 可解），
      | | | 但有会话后 agent 可以交互地验证自己的实现 |
      跑法：同模型同种子，arm D = 默认 harness（无 bash_session 工具面，
      用 `tools: minimal` 规格挡住会话工具），arm E = 默认 + 会话工具。
      预期：D 在 qemu-alpine-ssh 上 0 分，E 非零即收益被观测；
      若 E 也 0 分，结论是「会话工具面对 QEMU 级交互不足」，同样写回。

---

## W2 · 上下文工程分级（重点二）

**现状缺口**：压缩只有压力比、滞回、溢出恢复两三档（`compaction.py:57–100`），
没有观测值老化；`ContextFragment` 只有 7 个子类，codex 有 58 个。
`SoftTimeoutNotice`、`DisciplineRetryNotice`、`NarrationRequest`（`loop.py:1804/1817/1833`）
其实已经是系统提醒的雏形，但没有目录化，也没有按失败模式触发。

**为什么这条我们能做得比别人好**：我们的 history 是追加式的，且重写必须被声明
（`CompactionBoundary`，`history.py:81–105`），所以观测值降级也能被审计。
codex 的三阶段压缩没有可审计的降级轨迹，dsh 与 pi 连片段硬上限门禁都没有。
我们是在一个更强的不变量上做同一件事。

**出口**：长会话（30 次以上工具调用）的指令遵守率与峰值上下文占用成为可回归指标。

### 2.1 观测值分级老化

- [x] **2.1.1** `ObservationState` 三态（active / faded / archived）与迁移规则，
      取代当前的单级压力触发。对齐 OpenDev 的自适应压缩（报告峰值降约 54%）。
      已实现 `observation_aging.py`：`AgingRules` 规则表按 (age_rounds, size_tokens)
      决定 keep / compress / fold 三态；fresh（<3 轮）与 cheap（≤200 token）恒保留，
      ≥8 轮折叠，中间档超 1000 token 压缩。测试中发现并确认的设计点：压缩后小于
      keep_tokens 的结果会（正确地）永远保持——折叠只处理仍有体积的旧观测。
- [x] **2.1.2** 降级写成可审计条目——复用 `CompactionBoundary` 的声明式重写立场，
      不允许静默替换观测值内容。这是本条的立身之本，不能为省事丢掉。
      已落实：改写走声明式 `RewriteRequest`，循环经 `ContextManager.replace_all`
      应用并记录 `CompactionBoundary`（action="observation_aging"）——append-only
      记录保留每个原始字节，只有可见投影收缩。无迁移的轮次不声明重写、不记边界。
- [x] **2.1.3** 触发阈值下移并可配置：业界已从 90% 降到 75%，理由是给模型留
      工作余量而非仅够收尾。阈值作为 `ContextManager` 实现的声明字段，不写死。
      已落实：`AgingRules` 全部六个阈值（fresh_rounds / keep_tokens /
      fold_after_rounds / compress_tokens / compress_value_chars /
      compress_list_items）是声明字段，经 `ObservationAging` 策略的 spec 参数可调。
      压缩保 schema：envelope 键（success/error/data/message）全保留，只截断长值。
- [x] **2.1.4** 分级策略注册为 W1.1.1 `ContextManager` 的一个具名实现，
      与 `PressureCompaction`、`NullContext` 在同一维度上可对照。
      已注册：`harness.py` 的 `ObservationAging`（impl 名 `observation_aging`），
      折叠 stub 指明工具名、原始大小、起始轮次与 tool call id——完整记录留在
      会话历史中，可展开性由记录层保证。

### 2.2 事件驱动的系统提醒

- [x] **2.2.1** `ReminderCatalog`：把三个 notice 片段抽象为目录条目，
      每条单一用途、有界、可降级，仍走 `append_fragment` 的统一强制点。
      已实现 `reminders.py`：`REMINDER_CATALOG` 六条——三个既有 loop notice
      （soft_timeout / discipline_retry / narration，惰性导入避免循环依赖）+
      三个新失败模式提醒（ErrorStreak / AbandonedRecovery / RunawayExploration，
      各 200 token 上限）。`reminder_entry()` 未知 id fail loud。
- [x] **2.2.2** 触发规则表，按**已观测到的失败模式**绑定，而不是按轮次周期：
      提前宣布完成、放弃错误恢复、探索失控、连续工具错误逼近熔断、
      长时间无文件改动。每条规则要能指出它对应哪个真实失败。
      已落实：每个 `ReminderEntry.failure_mode` 中文字段即审计轨迹；`ReminderRules`
      只有三个事件阈值（error_streak_ratio=0.5 熔断比例、runaway_calls=12 零写调用、
      refire_rounds=6 重发间隔），刻意没有轮次周期规则。
- [x] **2.2.3** 注入位置固定在最高 recency——紧邻下一次 LLM 请求之前，
      而不是周期性重发系统提示。指令衰减的成因是注意力向近端漂移，
      位置错了就没有效果。已落实：`ReminderHooks.pre_step` 在请求前把到期提醒
      append 到 transcript 末尾（最高 recency 位）；`post_tool_result` 只跟踪信号
      （连续错误、是否见写）。每条规则触发一次后按 refire_rounds 间隔才重发。
- [x] **2.2.4** 每条提醒过既有 fragment CI 门禁
      （`test_fragment_bounds.py:91–114` 自动覆盖新子类，无需额外接线）。
      已验证：门禁走 `ContextFragment.__subclasses__()` 遍历，test_reminders.py
      导入模块后三个新片段自动进入门禁视野，17 项测试全绿。修正过一个契约细节：
      `render()` 用实例 `markers()`（默认空）而 `matches_text` 用 `type_markers()`
      ——新片段的不变前缀（"Tool-call error streak:" 等）放进 body 开头作 type marker。

### 2.3 片段覆盖扩展与长会话回归

- [x] **2.3.1** 盘点 sidecar 与桌面实际注入、但尚未类型化的内容，逐项收编。
      质量约束不放松：新片段仍须有界、可降级、过门禁。
      **盘点结果（2026-08-31）**：已类型化 = skills 目录（SkillCatalogFragment）、
      world_state（WorldStateFragment/PatchFragment）、loop 三 notice、reminders 六条。
      未类型化三处：(1) `headless.py:101` `_SYSTEM` 裸字符串系统提示——应为
      SystemPromptFragment（评测路径文件，arm A 完成后收编）；(2) `delivery.py:67`
      `_EXPLORE_NUDGE` 裸 user 消息无 fragment——且与 W2.2 的 RunawayExplorationReminder
      同一失败模式两个 owner，接线 reminders 时应去重（评测路径文件，暂缓）；
      (3) `harness.py` FilesystemState 笔记注入——**已收编**：新增 AgentNotesFragment
      （2000 token 上限 + review_note，门禁实测拦截有效）。`acp_adapter.py:271` 是
      真实用户输入而非注入，保持裸消息。
- [x] **2.3.2** 长会话回归用例：构造 30 次以上工具调用的会话，
      断言指令遵守率与峰值上下文占用。这两个数字此前只是感觉，本条把它变成指标。
      已实现 `test_long_session.py`（35 轮工具调用，对照/实验双跑）：(1) 老化后
      峰值上下文 < 对照 60%——效果减弱即回归失败；(2) 指令遵守率结构代理：每个
      请求（含第 35 轮、多次改写后）仍以系统指令开头，标记短语不丢；(3) 晚期请求
      中早期结果已折叠且 stub 指向会话历史；(4) 失控探索提醒落在最高 recency 位。
      测试再次确认设计点：压缩到 keep_tokens 以下的结果按设计永不折叠。

---

## W3 · 记录可移植性（重点三）

**现状缺口**：`StorageAdapter` 协议上没有检索、没有分支枚举、没有历史记录列举
（`storage/__init__.py:19–99`）——这三个只是 `SqliteStorage` 的实现扩展
（`sqlite_store.py:172–189`、`branch.py:23–26`），可移植的应用得靠鸭子类型。
没有 MCP 服务端，别人无法用标准协议调用我们。

**为什么这条有战略价值**：业界正在把记忆做成托管服务（Google Agent Memory Bank、
Anthropic 的交接文件式完整 context reset），而这些服务成立的前提正是记录的
可移植性与可审计性——恰好是我们已经做对的那一半。补上协议缺口，
会话记录就从内部实现变成可跨宿主、跨 agent 迁移的资产。

**出口**：一份会话记录可以在不同宿主、不同 `StorageAdapter` 之间无损迁移并重放，
且分支族可枚举。

### 3.1 StorageAdapter 协议补全

- [x] **3.1.1** `search_sessions` 提升为协议一等方法。
- [x] **3.1.2** `list_history_records` 与分支枚举提升为协议一等方法。
      协议方法已加入 `StorageAdapter`；sidecar.py 的 getattr 鸭子类型
      已清理（分支发现直接调协议方法，文档化的降级路径删除）。
- [x] **3.1.3** `InMemoryStorage` 与 `SqlAlchemyStorage` 补齐上述实现——
      不能只有 sqlite 有，否则「协议」只是文档。`list_history_records`
      三家本就有；`search_sessions` 补了 InMemory（序列化 JSON 子串，
      与 sqlite LIKE 同语义）与 SqlAlchemy（content 列 LIKE）。
- [x] **3.1.4** 协议一致性测试：三个实现跑同一套契约测试，
      语义差异（消息 limit 取尾、after_seq 排他等）在契约层锁死。
      `test_storage_contract.py` 4 项契约 × 三实现。**契约测试当场抓住
      SqlAlchemyStorage 四个既有 bug**（它从未被真实模型跑通过）：
      全量 dump 写入含表外列（id/parts unconsumed）、ISO 字符串直插
      DateTime 列、limit 取头而非取尾、None 显式插入非空默认列。
      全部修复（`_model_to_row`/`_row_to_model` 双向桥接 + 尾语义）。

### 3.2 MCP 服务端

承接 `ALIGN_TODO.md` 2.1.3 的二期项。它是「让别人调用我们」的唯一标准接口，
与 3.1 的可移植性同源：协议补全解决数据可迁移，MCP 服务端解决能力可调用。

- [x] **3.2.1** 把框架工具面暴露为 MCP server（客户端已在 `mcp.py` 落地，
      服务端为零；codex 有 mcp-server crate，dsh 用 ACP 服务端替代）。
      已实现 `mcp_server.py`：零依赖换行分隔 JSON-RPC（与客户端同立场），
      initialize 协商 + tools/list 投影路由器（hidden 不暴露）+ tools/call 经
      RouterToolExecutor 分发——模式、同意门、shell 安全规则在 MCP 边界上
      与循环内一致。关键架构点：独立读泵任务路由入站消息——elicitation 的
      响应必须在 serve 循环阻塞于 tools/call 时仍能送达，否则双向死锁。
- [x] **3.2.2** 审批代数经 MCP 表达：八变体决策格如何映射到 MCP elicitation。
      这是我们的领先点，不能在 MCP 边界上退化成「允许/拒绝」两态。
      已落实：`McpElicitationApprover` 向客户端发 `elicitation/create`，
      枚举全部六个用户可决变体（allow/deny × once/session/always；abort 与
      timed_out 是系统结果不进菜单）。read 模式静默放行（与 ACP 同立场）；
      decline/cancel/未知选项/传输失败全部 fail-closed 为 deny_once；
      无 elicitation 能力的客户端对受控调用直接拒。会话缓存生命周期 =
      MCP 连接生命周期。

### 3.3 结构化交接

- [x] **3.3.1** `HistorySeed` + `CompactionBoundary` 已经是完整 context reset 的骨架，
      把它显式化成可导出的交接产物。已实现 `handoff.py`：`HandoffBundle` =
      可见投影 + 每消息 kind + token 估计 + 来源溯源（record id / until seq），
      单文件 JSON，版本门禁 fail-closed（新构建写的包整体拒读，不部分解析），
      message_kinds 与 messages 不等长同样拒读。
- [x] **3.3.2** 完整 context reset 路径：拆掉会话，再从交接文件重建。
      业界结论是超长任务上摘要式压缩不够，必须能做完整重置。
      已落实：`export_handoff`（快照投影）→ 丢弃原 manager → `seed_from_handoff`
      （新记录播种）。测试验证重建投影与原投影逐字节相等、源记录不被触碰、
      重建后可继续追加、压缩后的交接只含可见跨度（被取代的段不漏进交接文件）。

### 3.4 ACP 服务端补齐

与 3.2 同属「让别人调用我们」，只是粒度不同：**MCP 服务端暴露我们的工具，
ACP 暴露我们的整个 agent**。两者都在 3.1 协议补全的下游。

**现状不是「没有」，是「只有骨架」**：`acp_adapter.py` 311 行，
`SteerableAcpAgent(acp.Agent)`，SDK 为 `agent-client-protocol 0.12.1`，
入口 `steerable-sidecar-acp`。SDK 的 `acp.Agent` 定义 13 个方法，我们实现 5 个：

| 已实现 | 未实现 |
| --- | --- |
| `initialize`、`new_session`、`prompt`、`cancel`、`close_session` | `load_session`、`resume_session`、`fork_session`、`list_sessions`、`set_session_mode`、`set_config_option`、`authenticate`、`ext_method` |

反向的 `acp.Client` 有 15 个回调，我们只用 `session_update` 一个。
未用的包括 `request_permission`（见 P0）、`create_elicitation`、
`read_text_file` / `write_text_file`、以及 5 个 `terminal/*`。

**边界立场照搬 dsh**：它的 ACP 是 **automation-only**——只暴露标准 ACP v1 面，
绝不外泄私有展示数据（plans、titles、todos、terminal views、elicitation 一律不走 ACP），
服务对象是脚本、测试运行器、进程外子代理，不是人类 UI。
我们同构：桌面 UI 走 sidecar 的私有方法面，ACP 只服务自动化。

#### 3.4.1 会话生命周期（能力已有，只是没接出来）

`acp_adapter.py:150` 现在广告 `load_session=False`，但 `ALIGN_TODO.md` 已经交付了
所需的一切：W2.6 的 `SqliteStorage` 给了持久会话与索引枚举，
W1.2 的 `agent.session.messages` 与分支族给了投影与分叉。

- [x] **3.4.1.1** `list_sessions` / `load_session` / `resume_session`，
      并把 `AgentCapabilities.load_session` 改为 `True`。已落实：
      new_session/fork 登记 AgentSession 行（stageData 带 cwd），
      list 经存储协议枚举，load/resume 经 `load_history_items` 投影水合
      宿主视图（与 `agent.session.messages` 同一条读路径），未知会话
      RequestError fail loud。4 项测试。
- [x] **3.4.1.2** **`fork_session`——本节重点**。已落实：直接调运行时的
      `fork_record`（分支族），新分支带水合种子注册为活会话并登记
      AgentSession（stageData.forkedFrom 溯源）。dsh 明确不支持的能力
      我们在标准协议上有了。
- [x] **3.4.1.3** 硬前置是 **3.1.2**：`list_history_records` 与分支枚举必须先升为
      `StorageAdapter` 协议一等方法。已于 W3.1 完成，本节直接受益。

#### 3.4.2 会话配置与 MCP 挂载

- [x] **3.4.2.1** `new_session` 接收并挂载 `mcpServers` 参数。已落实：
      stdio 服务器在 prompt 时 spawn（`McpStdioClient`）+ 目录注册到本轮
      router（限定名），finally 里 aclose；HTTP/SSE 是诚实缺口——
      会话创建时 fail loud，不静默丢工具。
- [x] **3.4.2.2** `set_config_option`：会话级覆盖 `provider`/`model`/`baseUrl`，
      prompt 时合入环境参数（会话显式选择赢）；未知键 fail loud。
- [x] **3.4.2.3** `set_session_mode`：**plan mode 在运行时不存在**（诚实缺口，
      不硬接）。落地的是可执行的 `read-only` 门：非 read 工具在审批之前
      直接拒绝（`_ReadOnlyExecutor` 包在 ApprovalExecutor 内侧）。
      模式面经 new_session 的 modes 字段广告。
- [x] **3.4.2.4** `LoopConfig` 硬编码清除：HarnessSpec 增加可选 `loop:` 节
      （max_rounds/max_tool_errors/tool_dedup，未知键 fail loud），
      default.harness.yaml 声明 80/16/false；headless 与 ACP 都从 spec 取，
      CLI `--max-rounds` 显式给定时仍赢（explicit > implicit）。

#### 3.4.3 编辑器桥（IDE 嵌入才需要，可延后）

- [ ] **3.4.3.1** `read_text_file` / `write_text_file` 客户端桥。当前用进程内
      workspace tools，意味着在编辑器里我们读的是磁盘而非未保存缓冲区——
      `acp_adapter.py` 自己的 docstring 已把这条标为 follow-up。
- [ ] **3.4.3.2** `terminal/*` 五个回调。与 W1.5 的交互式会话同源，
      两者应共用一套会话生命周期，不要各做一遍。

---

## W4 · 必补的诚实缺口（不是差异化，但不能装作没有）

### 4.1 Windows 宿主 spawn 实现

`ALIGN_TODO.md` 2.2.1 的遗留项：契约已就位（`host_spawn.py:1–25`），
但 `select_exec_backend()` 在 Windows 上直接返回 `None`（`sandbox.py:505`），
宿主侧没有真实现。**当前 Windows 是不可用，而不是「委派」**——四家里只有我们如此。

- [ ] **4.1.1** deeppath-agent 侧实现 `host.process.spawn`（受限令牌 + JobObject），
      参照 codex `windows-sandbox-rs` 与 dsh 的受限令牌 + ACL runner。
- [ ] **4.1.2** Windows 环境实盘验证，写入 `docs/spec/safety.md` 宿主能力面章。

### 4.2 运行时指标

`TraceRecorder` 有 span 与 OTLP 导出，但没有 counter / histogram。
可观测性目前是重放导向的，不是运维导向的。

- [x] **4.2.1** 决定是否补运行时指标，以及是否复用 W2.7.1 的零依赖立场。
      先出决策记录再动手（沿用 W2.7.1 的做法）。

      **决策（2026-08-31）：不造指标 SDK，补 span 维度缺口。**
      理由：(1) counter/histogram 意味着手写 OTLP metrics 编码 + 聚合语义
      （temporality、桶、exemplar），与零依赖立场直接冲突，且预发布期
      不该多养一个半建成的 API 面；(2) 标准运维路径是 OTel Collector 的
      spanmetrics connector 从 span 派生 RED/令牌指标——我们的 span 已经
      带 durationMs 与 ok/error status，接上 collector 今天就能出指标；
      (3) 真正缺的是维度：`llm_response` 事件与 `llm.request` span 只有
      promptTokens/cachedPromptTokens，**没有 model 与 completionTokens**，
      collector 派生不出 per-model 的 token/成本指标。
      落地动作（小改动，排进 W2 批次）：`llm_response` 事件补
      `completionTokens` 与 `model`，span attrs 同步带上。
      重开条件：真实部署提出运维指标需求时再评估。

---

## W5 · 模型目录外置（把 provider 立场从一句话变成一个实现）

**立场没变，变的是兑现**：文末「明确不做」写的是「provider 广度由宿主或**外部目录**
解决」。此前这句只是一句话——真正在跑的是 `model_info.py:71–84` 里 13 条硬编码前缀。
本章把那个「外部目录」做出来。我们仍然不追 provider 的 wire 适配器代码。

**dsh 的结构可抄，实现不可抄**：dsh 挂两个适配器——`llm-deepseek` 手写、独占
`deepseek-official` 路由；`llm-pi-ai` 服务其余 provider 与手工声明的网关，
路由名不冲突所以并排挂载，重复注册同一路由以 `DUPLICATE_ADAPTER` 失败。
但 `llm-pi-ai` 的实质是 `package.json:47` 依赖 `@earendil-works/pi-ai@^0.84.2`、
`src/catalog.ts:15` 导入 `getBuiltinModels / getBuiltinProviders`——**就是 npm install pi**。
dsh 与 pi 同为 TypeScript 才成立。字面照抄要求我们跑 Node sidecar 托管 pi-ai，
把 Node 运行时塞进 Python 框架的依赖链，与零依赖 OTLP 导出器、stdlib `sqlite3`
的既定立场冲突。**抄它的两层划分（一手适配器自有 + 长尾委派给目录），不抄它的载体。**

**数据接一手**：pi 的目录不是原创数据。`deepseek.models.ts` 是自动生成的，
内容只是 `import values from "./data/deepseek.json"`，而 `data/` 是 gitignore 的
构建产物（pi `.gitignore:11`），由 `scripts/generate-models.ts` 从 **models.dev**
加各家 `/models` 活端点拉取、再叠 pi 的手工修正生成。dsh 消费 pi，pi 消费 models.dev。
我们直接接 models.dev：`api.json` 4.2MB、211 个 provider、7489 个模型，
字段与 `ModelInfo` 基本一一对应（`limit.context` → `context_window`，
`modalities.input` → `modalities`，`reasoning` + `reasoning_options[].values` →
`reasoning_levels`，`tool_call` → `tool_format`），另带 provider 的 `api` 端点与 `env` 变量名。

**病根是前缀匹配，不是表太小**（2026-08-31 实测比对）：

| 我们的前缀 | 我们的窗口 | models.dev 实际范围 | 命中模型数 |
| --- | --- | --- | --- |
| `deepseek-reasoner` | 131,072 | 64,000–128,000 | 4 |
| `qwen2.5` | 131,072 | 32,000–128,000 | 5 |
| `deepseek` | 131,072 | 4,000–1,310,720 | 406 |
| `claude` | 200,000 | 20,000–1,000,000 | 316 |
| `qwen3` | 129,024 | 8,000–1,048,576 | 353 |
| `gpt-5` | 200,000 | 128,000–1,050,000 | 298 |

前两行是直接高报：声称 131k、实际上限 128k，这个方向要等 provider 拒绝才发现。
后四行更重：一条 `deepseek` 规则盖住 406 个模型、跨度 4,000 到 1,310,720，全按 131,072 算。
两头都错——小模型被当大的用，直接撞 provider 报错；大模型被当小的用，
**在 131k 处压缩一个 1M 窗口的模型，早压了八倍**。

后一个失败模式正是 `sidecar.py` 中 `_default_loop_hooks` 注释亲口记下的
「a fixed 60k against a 131k model compacted far earlier than the provider required
—— the dogfood 22-compacts/5-traces pathology」。那次修的是常量，没修机制；
只要模型不在这 13 条前缀里，同一个病必然复发。

**与 W2 的关系**：这是压缩阈值的**分母**。W2 把压缩策略做得再精细，
分母错了都白搭。W5 不是 W2 的前置，但两者共享同一个真实收益指标。

**出口**：任取一个 models.dev 收录的模型，其上下文窗口、模态、推理档位、
工具格式都能被正确解析，且解析结果可追溯到一个带日期的目录快照。

### 5.1 目录生成器（构建期，入库为生成产物）

- [x] **5.1.1** `scripts/generate_model_catalog.py`：拉 models.dev `api.json`，
      生成 Python 目录模块。零运行时依赖、可离线、版本可审计——与运行期 ETag 拉取
      相比，构建期方案让「这次跑用的是哪份目录」成为一个 git 事实。
      已实现：stdlib only，`--input` 离线复现，`--check` 字节级漂移门禁。
      首跑产物：7359 模型 / 212 provider（134 个无 context limit 的上游模型跳过计数）。
      实测价值：评测模型 `openrouter/z-ai/glm-5.3-flash` 真实窗口 1,310,720，
      现有前缀表报 202,752——**低估 6.5 倍**，正是 W5 要修的病。
- [x] **5.1.2** 生成产物钉住抓取日期与上游摘要，随产物入库。
      沿用 `evals/suite.yaml` pinned `git_rev` 的同一纪律。
      产物头部钉 `GENERATED_AT` + `UPSTREAM_SHA256`（2026-08-31T03:19:51Z 快照）。
- [x] **5.1.3** 修正覆盖层：我们自己维护的小表，在生成产物之上应用。
      参考 pi 的修正**逻辑**（Copilot 目录收窄、OpenAI 定价覆盖、Kimi Coding
      订阅制导致 models.dev 报零成本、已退役的 preview id），但不做 pi 的下游。
      pi 为 MIT，参考其判断可行。已实现 `scripts/model_catalog_overlay.json`
      （初始为空表），支持字段替换与 `remove`，每条修正须带 `reason`。
- [x] **5.1.4** 修正条目 fail loud：当一条修正指向的模型已不在上游目录中，
      生成期报错而不是静默跳过——否则修正会腐烂成不可见的死代码。
      沿用 1.2.3 的既定立场。已实现并测试：悬空条目、未知字段、未知推理档位
      三类都在生成期报错。
- [x] **5.1.5** 确认 models.dev 数据的许可与署名要求，写入生成产物头部。
      **已查清（2026-08-31）**：models.dev 为 MIT（Copyright (c) 2025 models.dev，
      github.com/anomalyco/models.dev/blob/dev/LICENSE）。署名要求 = 在副本或
      实质部分中包含版权与许可声明。落地方式：MIT 全文直接嵌入生成产物头部
      注释——单文件产物自带署名，避免数据文件的打包收录问题（zaly 的
      独立 `MODELS_DEV_LICENSE` 文件的等价做法）。

### 5.2 解析改为精确匹配优先（本章的核心）

- [~] **5.2.1** `resolve_model_info` 三级解析：精确 id → provider 限定 id → 前缀回退。
      **解析器已实现**（`model_resolve.py`，新文件不在评测路径）：精确 →
      同 provider 末段匹配（网关场景：openrouter + `glm-5.3-flash` →
      `openrouter/z-ai/glm-5.3-flash`）→ **同 provider** 最长前缀
      （跨 provider 前缀会认领别家部署的上下文窗，已禁）。7 项测试全绿。
      `model_info.py` 改为委托调用待 arm A。
- [x] **5.2.2** 保留 `register_model_info` 运行时覆盖（`model_info.py:94`），
      部署仍可描述 fine-tune 或刚发布的模型而无需发版。已验证：
      `test_custom_override_still_wins_over_catalog`——自定义条目压目录精确命中。
- [x] **5.2.3** 未知模型回退到保守默认时**必须可观测**——记一次事件，
      而不是静默用 `DEFAULT_CONTEXT_WINDOW`。已落实：`register_resolution_observer`
      订阅回退（`legacy_prefix` / `default` 两种 source），无订阅者时至少
      logging 留痕。两个回退路径各有测试。
- [x] **5.2.4** 重估 `DEFAULT_CONTEXT_WINDOW = 60_000`（`model_info.py:28`）：
      **结论：维持 60k，依据已重写**。目录数据（7359 条）：全体 p05=32k；
      工具模型（harness 真正服务的，6441 条）p05=128k、仅 3.2% 低于 60k。
      默认只服务目录外未知模型，成本不对称——高估窗 = 请求硬失败，
      低估 = 提前压缩的软降级——所以取保守侧；提到 128k 会让 3.2%
      小窗工具模型硬失败，而那恰是我们最不了解的模型。

### 5.3 端点与 compat flags 对接

- [x] **5.3.1** models.dev 的 provider `api` / `env` 字段接入。落点比原计划更准：
      flags 表不认识新网关时返回 None 本来就是正确行为（目录没有 flags 事实），
      真正该接的是**端点与密钥**——`default_llm_provider_factory` 在缺 base_url 时
      从目录取（`provider: deepseek` 免配端点），`_env_provider_params` 在通用
      变量缺席时读目录的 per-provider env 名（`DEEPSEEK_API_KEY`）。
      另有 `catalog_provider_for_base_url`：wire provider 是 compat shim 时按
      端点反查目录命名空间（openrouter.ai → openrouter），glm-5.3-flash 的
      上下文窗由此从 202k 修正到 1.31M。
- [x] **5.3.2** 划清归属：**端点来自目录，compat flags 仍由我们拥有**。
      `PROVIDER_COMPAT_HOSTS` 保持手工，未动。

### 5.4 验证

- [x] **5.4.1** 等价回归：现有 13 条前缀的解析结果在新三级解析下逐条比对，
      差异必须能逐条解释（预期至少 `deepseek-reasoner` 与 `qwen2.5` 两条会变，
      因为它们本来就是错的）。`test_model_equivalence.py`（14 项）：旧表实际
      12 条，逐条真实探针比对，**无解释的分歧直接 fail**。实测 10/12 有分歧，
      比预期严重得多：claude/gpt-5 旧表停留在 200k 时代（新 1M/1.05M）、
      bare gpt-4 旧表虚报 128k（实际 8k，前缀多年超额认领）、z-ai/glm 旧表
      202k（实际 1.31M）、qwen2.5 窗口与工具支持双错、deepseek 一家被上游
      目录除名（v4 换代，旧 id 需 overlay）、ollama 本地守护进程按设计走
      W5.2.2 运行时覆盖。
- [x] **5.4.2** 压缩阈值回归：取一个 1M 窗口模型与一个 4k 窗口模型，
      断言压缩触发点随目录而动。**这条是本章的真实收益**，也是它与 W2 的接点。
      已落实：`test_compaction_threshold_follows_the_catalog`——同一 10k
      transcript，alibaba-cn/qwen-math-plus（目录 4k）触发压缩 rewrite，
      anthropic/claude-sonnet-4-6（目录 1M）原样通过。
- [~] **5.4.3** 顺带清掉 `compat.py:187` 与 `211` 标注的 live-key run pending。
      **openrouter.ai 已实盘验证**（2026-08-31）：deepseek-r1 经网关原始线
      `reasoning` + `reasoning_details`，钉住顺序经框架归一化输出 965 字符
      推理 + 正文，注释已改为 live-verified。`api.moonshot.cn` 条目**阻塞于
      无 Moonshot key**（deeppath-api/.env 只有 OpenRouter），保留 pending。

---

## 顺序与依赖

```text
W1.4.3.4 arm A 基线跑 ──→ W1.4.1/1.4.2 动代码
  （不先跑基线就没有对照，补完也说不出补了多少）

W1.4 工具面消融 ──→ W1.1 / W1.2 / W1.3 的全部投入
  （最轻的一次前提检验：若工具面这一个模块在我们题集上都测不出差异，
    「harness 是一阶变量」在这里就不成立，整个 W1 的规模要重估）

W1.1.2 基线实现 ──→ W1.3 因子设计
  （没有 NullContext / NoRetry 就无法归因，这是硬前置）

W1.1.1 模块 Protocol ──→ W2.1.4 分级策略注册为 ContextManager 实现
  （接口先定，否则 W2 的产出无处挂载）

W1.2 声明式装配 ──→ W1.3.1 harness 维度
  （评测台要能按名字取 harness）

W3.1 协议补全 ──→ W3.3 结构化交接
W3.1 协议补全 ──→ W3.2 MCP 服务端（可并行，但协议先稳更省事）
W3.1.2 枚举升协议 ──→ W3.4.1 ACP 会话生命周期（硬前置：list/fork 要枚举）
W1.5 交互式会话 ←──→ W3.4.3.2 terminal 回调（同源，共用会话生命周期）

P0 审批归零 独立，不依赖任何工作项，也不该等它们

W1.5 交互式会话 独立，收益不与 W1.4 同质，分开计分

W5 模型目录 ──→ W2.1.3 压缩阈值可配置
  （目录给的是阈值的分母；分母错了，阈值调得再准也没有意义。
    不是硬前置——W2.1 可以先做——但 W5 不落地前，W2 的收益测不准）

W4 独立，可随时插入
```

**建议落地顺序**：**P0（ACP 审批归零——安全缺口，先修）** → **W1.4（工具面消融——先跑 arm A 基线，再补 grep/glob/apply_patch，
再接编排，出第一份三 arm 对照）** → W1.1（接口 + 基线实现，纯增量、风险最低）
→ W1.2（声明层，含 1.2.4 等价迁移证明这一步是纯重构）→ **W1.3（第一次出跨 harness
归因报告——这是本计划第一个对外可见的成果）** → W2.1 + W2.2（分级老化与提醒目录，
两者共用 fragment 门禁）→ W3.1 + W3.2 → W1.5 → W2.3 → W4。

**W5 不排在这条链上**：它是独立的一条短线，随时可插，且不依赖 W1 的任何产出。
建议紧跟在 W1.4 之后做——它体量小、边界清楚，而 5.4.2 的压缩阈值回归会给
W2.1 提供一个真实的度量基线。

把 W1.4 放在最前面，是因为它用最小的代价回答最大的问题。W1.1–W1.3 是一整套
模块化与因子设计的基建，投入不小；而 W1.4 只动一个模块、复用现成的 Harbor 通路，
就能先看出「换 harness 到底有没有用」。若 1.4.3.5 的结论是没用，
省下的是 W1 剩余部分的全部工作量。

W1.3 之前不要开 W2 和 W3：如果因子设计跑出来发现 harness 主效应在我们的题集上
不显著，整个重点一的前提就需要重估，此时 W2/W3 的投入方向也会变。

---

## 明确不做（R10 决策）

- **不追 provider 的 wire 适配器数量**。pi 有 39 个 provider 目录，我们有 2 个
  provider 加 6 个 compat host（`llm/compat.py:163–239`）。这是纯集成工作量的比拼，
  追不上也不该追：`openai_compat.py` 通吃 OpenAI 兼容、`anthropic_native.py` 走原生，
  真正结构不同的 wire 只有 Gemini、Bedrock、Azure Responses 那么几个。
  **但「provider 广度由外部目录解决」这句话必须真有一个目录**——此前它只是一句立场，
  实际在跑的是 13 条硬编码前缀。W5 兑现它。
  注意 R10 时对 dsh 的描述不够准确：它不是「wrap 了 pi 的目录」那么轻，
  而是 npm 依赖了 pi 的整个 AI 库；那条路对 Python 框架不通，理由见 W5。
- **不做插件市场**。codex、dsh、pi 三家的插件体系服务终端用户，
  我们的扩展受众是嵌入者。该补的是配置驱动的 harness 声明（W1.2），
  不是可发现、可版本化、可隔离的第三方插件运行时。
- **不在框架内自建 Windows 沙箱原语**。host-spawn 委派给宿主是正确的架构选择，
  不应改成在框架里造一个受限令牌实现——但宿主侧必须真做出来（W4.1）。
- **跨厂商委派优先级下调**。dsh 已能委派给 Claude Code SDK、Codex app-server
  和通用 ACP。这是集成深度的差异，不是架构差异化，复制它对我们的定位没有增益。
  `ALIGN_TODO.md` 2.5.2 保留在二期，不进本计划。

---

## 复评出口


- 每条完成时在 canvas `steerable-r10-framework-review` 复测更新判定，
  证据精确到 file:line（沿用 R9/R10 纪律：未能定位 file:line 的能力不计入判定）。
- **W1 的独立出口**：一份锁模型的跨 harness 归因报告，含主效应比值与排名反转计数。
  这份报告本身就是对外资产，独立于其余工作项的完成度。
- 全部完成后出 R11 四方复评，重点复核三条轴：可扩展性（W1.2 是否把它从
  「落后」推动了）、上下文片段覆盖（W2.3 后是否仍是「机制领先、覆盖落后」）、
  以及新增的「harness 可测量性」轴——该轴目前四家均为空白，
  是本计划唯一一条以「建立领先」而非「追平」为目标的工作。
