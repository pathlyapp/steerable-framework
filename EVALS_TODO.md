# 产品能力评测流水线

> 目标：用业界公开集给 **DeepPath agent / Steerable CoreLoop** 打可对外的能力分。
> 顺序：**先 Terminal-Bench 2.1 跑通，再上全量 SWE-bench Verified。**
> 跑分器是 [Harbor](https://www.harborframework.com/docs/run-jobs/run-evals)，隐藏测试打分，不用 LLM judge，不自制 prompt 当门禁。
>
> 套件钉死在 [`evals/suite.yaml`](./evals/suite.yaml)。当前能力说明：[`docs/evals.md`](./docs/evals.md)。

**评测对象**：无头 CoreLoop + 能改文件、跑 shell 的工具面（sidecar ACP）。
不是 Electron 窗口，也不是 Harbor 自带的 `claude-code` / `codex` / `pi`（那些只做同题对照）。

**产品分在 GitHub Actions**（`ubuntu-latest`，`STEERABLE_API_KEY` + `STEERABLE_BASE_URL`，与本机 glm 同一套 OpenAI 兼容网关）。对照 agent（`claude-code` / `codex` / `pi`）才要官方 Anthropic / OpenAI key，缺则 skip。本机 Clash 只用于改 adapter，不上 GHA。

---

## 已完成（对照基线，还不是产品分）

- [x] TB 2.1 catalog（89）+ `cheap-12` + `oracle-canary`（`fix-git`）钉在 `evals/suite.yaml`
- [x] L0：`evals/tests` 随 `uv run pytest`（无 Harbor、无 Docker）
- [x] Harbor wrapper：`python -m evals.run`
- [x] workflow 草稿：`.github/workflows/evals-oracle.yml`、`evals-weekly.yml`
- [x] 本机 Harbor oracle × `fix-git`：**Mean 1.000**（题和隐藏测试通）
- [x] Harbor CLI 版本钉在 suite：`run.harbor_version: "0.22.0"`

本机产品 cheap-12：`evals/jobs/steerable/2026-08-29__23-02-40`，glm-5.3-flash，**Mean 0.750**。GHA 产品分已落盘（见 2.1）。本机 Claude Code 因容器 Debian 源失败，**忽略，改走 GHA**。

---

## Phase 0 · TB 基础设施（GHA 能打对照分）

出口：GHA 上 oracle × `fix-git` Mean **1.0**；weekly 能跑产品（网关 key）或明确 skip；对照基线有官方 key 才跑。全部 skip 则整次失败。

- [x] **0.1** `harbor_argv` 把 `--include-task-name` 写成 `terminal-bench/<短 id>`（否则过滤为空）
- [x] **0.2** Harbor 安装：composite action 钉 `harbor==0.22.0`，`~/.local/bin` 进 `PATH`
- [x] **0.3** oracle workflow：`evals/**` 变更 + `workflow_dispatch`；Mean ≠ 1.0 或安装异常 → 红；上传 `result.json`
- [x] **0.4** weekly：cheap-12 × `steerable` / `claude-code` / `codex` / `pi`；产品走 `STEERABLE_*`，对照走官方 key；缺 key skip，全 skip 失败；artifact + Mean 摘要
- [x] **0.5** 仓库 secrets：`STEERABLE_API_KEY` + `STEERABLE_BASE_URL`（本机同一网关，产品分必填）。`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` 仅对照基线，可选
- [x] **0.6** 安装失败当红（`n_errored_trials > 0`）；题没做对只记 Mean（0 是成绩）

---

## Phase 1 · 产品能在 TB 上交卷（阻塞 SWE）

出口：GHA 上 **产品 agent** × `fix-git` 跑完，有 Harbor Mean（允许 0，但必须是隐藏测试分，不能是 setup timeout / apt 失败）。
前一项不过，不要铺 cheap-12，不要开 SWE。

- [x] **1.1** ACP 工具桥：默认 in-process `bash` / `read_file` / `write_file`（`workspace_tools.py`）。Editor fs/terminal 仍是 follow-up
- [x] **1.2** 无头启动：`python -m steerable_sidecar.headless`（`--instruction` / `--instruction-file`）
- [x] **1.3** Harbor adapter：`evals.harbor_steerable:SteerableHarborAgent`；`suite.yaml` `steerable` 未 skipped
- [x] **1.4** 同一题对照：oracle Mean = 1.0 **且** 产品 canary 交卷（`evals/jobs/steerable/2026-08-29__15-09-24` Mean **1.000**，隐藏 pytest 全过）
- [x] **1.5** GHA 矩阵加上产品 agent（oracle workflow 的 `steerable` job + weekly 矩阵）

---

## Phase 2 · TB cheap-12 产品分

出口：GHA 一张表：产品 vs `claude-code` / `codex` / `pi`，同一 cheap-12、同一隐藏 pytest、n-attempts=1。

- [x] **2.1** weekly（或独立 workflow）跑产品 × cheap-12。GHA artifact：[33307477867](https://github.com/pathlyapp/steerable-framework/actions/runs/33307477867) glm-5.3-flash **Mean 0.750**（9 过 / 3 不过：`filter-js-from-html`, `password-recovery`, `git-multibranch`），12/12，`n_errored_trials=0`。硬化 `--n-concurrent 2`：[33308738073](https://github.com/pathlyapp/steerable-framework/actions/runs/33308738073) **Mean 0.833**（10 过 / 2 不过：`filter-js-from-html`, `password-recovery`；`git-multibranch` 这次过了），约 16 min，飞书 `成功 · GHA cheap-12 · steerable 0.833`（无 unknown）。本机 `2026-08-29__23-02-40` 同为 Mean 0.750（失败题不完全相同）。
- [ ] **2.2** 钉模型档，便于和基线比 harness 而不是比模型（Claude/Pi 默认 `anthropic/claude-sonnet-4-5`，Codex `openai/gpt-5.5`；产品默认 `openai/z-ai/glm-5.3-flash`，同档对照时 `--model openai/gpt-5.5`）
- [ ] **2.3** Mean / exception / artifact 进 job summary；周更
- [ ] **2.4** 全量 89 题：可选、手动/`workflow_dispatch`（`Evals weekly` split=`catalog`，49 分片），**不上每 PR**

**Phase 2 出口成立之后，才开始 Phase 3。**

---

## Phase 3 · SWE-bench Verified（TB 跑通之后）

出口：Harbor 上 **全量 SWE-bench Verified** 的产品 Mean，可与公开榜对照。

- [ ] **3.1** 在 Harbor 钉官方 Verified 数据集名与 git/digest（写入 `evals/suite.yaml` 的独立 dataset，不要和 TB catalog 混成一个 Mean）
- [ ] **3.2** 复用 Phase 1 的同一无头 agent / adapter（SWE 也是改仓库 + 跑测试）
- [ ] **3.3** 先 1 个 instance 冒烟（产品交卷 + oracle/官方解若存在则对照）
- [ ] **3.4** 全量 Verified（约 500 题）在 GHA 或专用 runner 上跑；时限、并发、费用单独预算
- [ ] **3.5** 基线同集对照（至少一种 Harbor 一等 agent），两套标准两行分，不合成一个数

禁止：自造 20 题 SWE 子集对外叫 Verified。

---

## Phase 2.5 · catalog-89 冲 0.75

分数记录：`b929af1` / [33464114983](https://github.com/pathlyapp/steerable-framework/actions/runs/33464114983) **Mean 0.6629**（59/89），基线 0.6517。两次跑的同题对照：**50 稳定绿 / 17 翻面 / 22 稳定红**——翻面题全转绿也只到 67/89 = 0.753，所以稳定红里必须出题。

单次全量的测量噪声：SD ±0.0232，95% 带 ±0.0454。**`n_attempts=1` 的单次 A/B 无意义**，故新增 `flaky` split（17 翻面 + 8 受切流影响的稳定绿作对照，共 25 题）× `n_attempts=3` × 双臂 = 50 job，配对符号检验 + bootstrap CI 见 [`evals/flaky_score.py`](./evals/flaky_score.py)。每 trial 加 85 分钟绝对帽（两次全量里只有 4/117 个获胜 trial 超过 90 分钟）。实测一轮约 1.75 h，对比全量 89 的约 6 h。

**功效（模拟，翻面题按 50/50、对照按 0.85 建模）**：

| 真实每题效应 | +0.05 | +0.10 | +0.15 | +0.20 | +0.30 |
|---|---|---|---|---|---|
| 功效（n=3） | 0.11 | 0.30 | 0.59 | 0.74 | 0.95 |
| 功效（n=5） | 0.15 | 0.43 | 0.80 | 0.92 | 1.00 |

假阳性率 0.025。bootstrap CI 只比符号检验高 1–3 个点（+0.10 时 0.33 vs 0.31），**瓶颈是数据量而非检验方法**。含义：这套循环**不能逐个验证 +0.05~+0.10 的小赢**（+0.10 有 70% 概率被判为无差异），但 0.663 → 0.75 需要 +7.7 题、若全来自 17 道翻面题则每题 +0.45，那个量级功效接近 1.00。**所以改动要打包成一个 arm 测，不要一条一条试。**

`flaky` 覆盖不到稳定红（22 道里 0 道），而 7 道恒红螺旋题恰是删切流后最可能受益的。它们的基线是"两跑零通过"，没有可配对的比率，故另开 `spiral-red` split 单臂跑 7 job：**任何一次通过就是结论**，不需要第二臂也不需要配对检验。

### 对照 pi / dsh / codex：参数与循环逻辑

| | Steerable | pi | dsh | codex |
|---|---|---|---|---|
| 单命令超时 | 1 h | 无（按需传参） | 120 s（沙箱 60 s） | 10 s 默认 / 用户 shell 1 h |
| 会话软超时 | 150 min（Harbor ×12 → 180 min 硬杀） | 无 | 无 | 无 |
| 最大轮次 | 250 | 无 | 无 | 无 |
| 压缩阈值 | **0.8** | ctx − 16384（≈0.87–0.92） | 0.8 | **0.9**（有效窗口 95%） |
| 压缩保留 | 6 条消息 / 2 工具结果 | 20 000 tokens | retain 0.16 | — |
| 流空闲超时 | 170 min（GLM 静默思考可达 48 min） | 300 s | 300 s | 300 s |
| 只推理即切流 | **有** | 无 | 无 | 无 |
| `tool_choice` 强制 | **有**（约 18 处） | 无 | 无 | 无（auto） |
| 运行时验证门禁 | **有** | 无 | 无 | 无（仅提示词） |
| 持久性提示词 | 无 | 无 | 无 | **有** |

结论：**"竞品分高是因为超时更宽"不成立**——我们的单命令超时和流空闲超时都是四家里最宽的，会话软超时和轮次上限也没有任何一家更紧。三家的高分不来自参数余量。真正的差异是：三家都**没有**切流、轮次上限、`tool_choice` 强制这些机制，而 codex 有一句我们缺的持久性指令。

参数对齐仅剩一处候选：压缩阈值 0.8 → 0.9（codex 值）。压缩次数与失败的横截面梯度很强（0 次 0.743 / 1 次 0.591 / 2–3 次 0.200 / 4+ 次 0.000），但**同题配对 5 比 5、p=0.623 完全为零**，且 `llm-inference-batching-scheduler` 压缩 5 次两轮全过。压缩次数只是"长 trial"的代理量，**没有证据说提阈值能提分**，只能按对齐做，不能当提分手段。overflow 重试 0 次，说明 0.8 全是主动触发。

### 失败日志分析（不做单题特例）

**一个主导失败模式：模型进入无界推理状态后，忽略 harness 的全部强制手段。**

- 推理量 / 工具调用次数是最强预测量，单调：0–3 KB **0.889** / 3–10 KB 0.804 / 10–50 KB 0.426 / ≥50 KB **0.071**。灾难区 12 题（7 恒红、4 翻面、1 恒绿）。
- `tool_choice=required` 实测 **1667 次强制轮里仅 61.9% 真的产生工具调用**；违背的 635 次中位数吐 12.9 KB 文本（最大 135 KB），其中 480 次之后循环又设一次 `required`、模型继续不理。故 `delivery.py` 里约 18 处 `_force_tool` 上限只有 62% 效力。合规率 <40% 的 24 个 trial 通过 0.167，>80% 的 122 个通过 0.746。
- **切流自己造出了它要防的死循环**：cut 在长推理的自然终点**之前**反复打断，模型每次从 `staleChars` 上限（147 KB）重新烧满，轮次永不前进。`schemelike-metacircular-eval` 26 次调用 / 2 cut → **2 次调用 / 9 cut / hard_timeout**，`gpt2-codegolf` 18 → 4 次调用。**不是失忆**：相邻推理段 8-gram 重叠仅 0.2–4.7%，模型每次都想新内容；通知文本明确写了"立刻调工具"，模型只听"不要重推"那半。

hard_timeout 归因：178 个 trial 共 6 次，**5 次是推理螺旋**（日志 ≥100 KB），其中 3 次正是 final 跑里被饿到只剩 2–4 次调用的 trial——**切流机制造成了 5 次螺旋超时里的 3 次**。剩下 1 次（`extract-moves-from-video`）是时间耗在单次 bash 里（380 帧 tesseract OCR，18 KB 日志却撞硬超时）。

已证伪、不要再试：

- 收紧单命令超时（我们 1 h vs codex 10 s / dsh 120 s）——全量里"时间耗在长命令"只造成 **1** 次失败，改它换不到分。

- dsh 式重复调用提醒——178 个 trial 的最长连续相同调用都是 1，我们没这个问题（`tool_dedup=False` 无害）。
- "先写一个能跑的版本"提示词——通过与失败的首次落盘**都在第 1 次调用**，首写位置与结果无关（0.649 / 0.644 / 0.706）。agent 本来就立刻动手，这条会是空指令。
- 轮次上限——两次全量 runaway guard 从未触发，250 不是瓶颈。

因果口径：上述剂量反应都是横截面的。同题配对检验对推理量（11/17，p=0.17）、合规率（6/4，p=0.377）、压缩次数（5/5，p=0.623）**均不显著**，可用配对只有 10–17 对。故只声称"这些是出问题 trial 的共同标记"，不声称因果；切流的因果由 A/B 定。

### cuts A/B 结论：切流默认关掉（run 33481095845，25 题 ×3 次 ×2 臂）

两臂唯一差别是三个切流预算（b 臂全设 0），已从 job log 核实无其它变量。

| | arm a 切流开 | arm b 切流关 |
|---|---|---|
| 通过率 | 51/74 = 0.689 | 59/73 = 0.808 |
| ≤5 次工具调用就结束的 trial | **15/74** | 5/73 |
| 其中通过 | 4 | 3 |
| 每题通过率均值变化 | — | **+0.0933**（净 +2.33 题 / 25） |
| 符号检验 | — | 7 胜 4 负，**p=0.55（不显著）** |

**判决依据不是通过率，是可复现的饿死。** 通过率的配对检验没过 0.05，而且按功效模拟这个量级的效应本来就只有约三成概率被测出来，所以它既不能支持也不能否定。决定性的是饿死信号完全可复现：

| 题 | arm a 工具调用数 | arm a | arm b 工具调用数 | arm b |
|---|---|---|---|---|
| write-compressor | **[2, 2, 2]** | 0/3 | [21, 31, 31] | **3/3** |
| feal-linear-cryptanalysis | [2, 4, 13] | 0/3 | [6, 14, 17] | **3/3** |
| feal-differential-cryptanalysis | [2, 3, **5435**] | 1/3 | [13, 20, 26] | **3/3** |
| model-extraction-relu-logits | **[3, 3, 3]** | 0/3 | [0, 3]（job 失败） | 0/2 |
| regex-log | [0, 3, 4] | 2/3 | [6, 9, 10] | **3/3** |

`[2,2,2]` 和 `[3,3,3]` 这种零方差不是采样噪声能产生的，是机制在稳定复现同一个失败。`5435` 那次是切流与重试活锁。目标陈述里独立点名的两道被饿死的题（feal-differential 21→2、write-compressor 14→1）**全部恢复**。

反向代价真实但看着像抖动：sam-cell-seg 3/3→1/3，dna-insert / sparql-university / mailman 各 −0.33，合计 −1.66 题，都是单次 trial 翻面。

落地：三个预算默认值改为 0，机制与环境变量覆盖保留（后续臂可以直接重测，不必重新推导）。加了 `test_stream_cuts_are_off_by_default` 锁住默认值——已验证它能拦住改回 200_000。

### 0.75 还差多少：失败形态拆解（run 33369888461，58/89 = 0.6517）

31 个失败按"干了多少活"拆开，结论是**剩下的缺口不是时间/轮次不够，是产出错误**：

| 形态 | 题数 | 能靠什么补 |
|---|---|---|
| 什么都没产出（≤5 次调用，被切流卡住） | 4 | 切流关闭（已落地） |
| 跑了 10 次调用仍无产出 | 1 | 未知 |
| **产出了但产出是错的** | **26** | 交付前验证 |

对照组信号很干净：**58 个通过的 trial 里，产出为空的是 0 个**；≤5 次调用结束的 trial 也是 0 个通过。所以"停在 5 次调用以内"= 必败，"什么都没产出"= 必败，两者都已被切流关闭覆盖。

**算术上：58 → 67（0.75）需要 +9 题，切流最多补 4 题，剩下 5 题必须从那 26 道"产出错误"里拿。**

产出为空的统计必须用 `delivery.py` 自己的 `_BASH_WRITES` 判定——只数 `write_file`/`edit_file` 会把 bash heredoc 写文件的情况全部误判成"没产出"（初版算出 7 题，实际 1 题）。

### 验证门禁的触发率与依据修正

门禁要求完成时 `consecutive_explore == 0`，而 `bash` 和 `read_file` 都会让这个计数器 +1，所以**只在"最后一个动作正好是落盘写"时触发**。实测覆盖面：89 个 trial 里 32 个（36%）处于该状态，其中 10 个失败——够覆盖 5 题缺口，不是空转。

但原注释引用的依据（0.647 → 0.775）是用 `write_file`/`edit_file` 这个窄定义算的，用门禁自己那套判定重算后幅度小得多：

| 写完之后又跑了几次调用 | n | 通过率 |
|---|---|---|
| 0（写完就收工） | 32 | 0.6875 |
| 1–2 | 35 | **0.7429** |
| 3–5 | 11 | 0.7273 |
| 6–10 | 3 | 0.6667 |
| 11+ | 4 | **0.0000** |

方向不变（多跑一轮值约 5 个百分点、再多就是瞎折腾），但"再多就有害"这一侧只有 7 个样本。注释已改成按这个口径陈述并写明样本量，避免以后拿它当"放宽预算"的依据。

### 门禁覆盖不到的收尾形态（下一个靶子，等全量结果后再动手）

门禁看不见的 56 个 trial（收尾是非写入 bash），按最后一条命令实际做了什么拆开（`cd X &&` 前缀已剥离，整条流水线判定）：

| 最后一条 bash | n | 通过率 |
|---|---|---|
| 跑了程序 | 22 | 0.7273 |
| **只看了一眼产出** | **15** | **0.5333** |
| `sed`（盲改） | 4 | 0.0000 |
| 跑了测试或评分器 | 2 | 0.5000 |

**空档是那 15 个"只看了一眼"的，7 个失败**，与"跑了程序"差 20 个百分点——比门禁自身 0.6875 → 0.7429 那 5 个百分点强得多。样例：circuit-fibsqrt 收尾 `cat gates.txt | head -50; wc -l`（只数自己产出的行数，从未送进电路求值器）；extract-moves-from-video `wc -l solution.txt; head -5; tail -3`；regex-chess 收尾是 `ls -la /`。

覆盖面若扩到这一类，从 32/89（36%）到 47/89（53%）。

**设计上有个必须先解决的矛盾**：`_unverified_retry` 的 docstring 明确论证过"不用命令文本正则，因为正则只认得写它时想到的那几种检查"。而识别"只看了一眼"必然需要某种命令分类。可用的既有概念是 `_BASH_VIEW_FILE`（已存在，用途正是"这条 bash 其实等于 read_file"），但它只匹配单条命令，真实案例都是 `cat x | head; echo ---; wc -l x` 这种复合形式。所以正确的方向可能不是扩大正则，而是**改门禁的判据**：从"最后一个动作是不是写"改成"自上次落盘以来有没有任何东西被执行过"，这样两类都覆盖，且只需判断"是否执行"而非"是否是某种检查"。

不在全量跑完前实现：这一跑第一次测量现行门禁的真实效果，改了就分不清是哪版的功劳。

- [x] **2.5.1** cuts A/B 完成
- [x] **2.5.2** 切流默认关闭（保留 env 覆盖）
- [ ] **2.5.3** 加 codex 式持久性指令（绿 trial 中位数只用掉 150 分钟里的 12 分钟）
- [ ] **2.5.4** 全量 89 记录 Mean ≥ 0.75

---

## 明确不做（除非另开文档改目标）

- 自制 prompt YAML / 知识库场景题当作能力门禁
- LLM-as-judge
- 把 Electron GUI、PTY、deeppath-knowledge 场景塞进 TB/SWE
- 把网关 key 写进 `OPENAI_API_KEY`（Codex 对照会误打官方端点）
- 本机 Clash 当 GHA 出网代理
- DeepSeek Harness 进同一矩阵（它也还没有 Harbor adapter）
- 在产品还不能 TB 交卷时开 SWE
