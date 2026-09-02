# 产品能力评测流水线

> 目标：用业界公开集给 **DeepPath agent / Steerable CoreLoop** 打可对外的能力分。
> 顺序：**先 Terminal-Bench 2.1 跑通，再上全量 SWE-bench Verified。**
> 跑分器是 [Harbor](https://www.harborframework.com/docs/run-jobs/run-evals)，隐藏测试打分，不用 LLM judge，不自制 prompt 当门禁。
>
> 套件钉死在 `[evals/suite.yaml](./evals/suite.yaml)`。当前能力说明：`[docs/evals.md](./docs/evals.md)`。

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
- [x] **0.3** oracle workflow：`evals/`\*\* 变更 + `workflow_dispatch`；Mean ≠ 1.0 或安装异常 → 红；上传 `result.json`
- [x] **0.4** weekly：cheap-12 × `steerable` / `claude-code` / `codex` / `pi`；产品走 `STEERABLE_`\*，对照走官方 key；缺 key skip，全 skip 失败；artifact + Mean 摘要
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

**目标已达成：`27d521a` Mean 0.8006 ± 0.0232（四次全量实测，见文末"四个完整样本"一节），对基线 0.6517 净 +13 题。**首跑 [33497477757](https://github.com/pathlyapp/steerable-framework/actions/runs/33497477757) 是 0.8202（73/89），但那是分布上沿；**报数和后续对照都用 0.8006，不要用 0.8202**。这一跑合入三项改动：切流三个预算全部默认关掉、验证门禁、提示词精简 35%。奖励只有 0.0/1.0 两值，无部分分。

下面 2.5 各节里的逐题归因（转绿/转红名单、"仍失败 16 道"）都出自首跑那**单个**样本。四次样本下只有 9 道是稳定红，20 道会翻面——**读这些名单时按"那一次 trial 里发生了什么"理解，不要当成"这道题的能力边界"**。

净 +15 的构成：18 道转绿，3 道转红（`extract-elf`、`pytorch-model-cli`、`torch-tensor-parallelism`）。转绿里有 7 道原属 22 道稳定红，其中 `torch-pipeline-parallelism` 正是"交付程序自身崩溃"（`UnboundLocalError`）那一类——**切流关闭后模型自行发现并修好了崩溃**，不需要原计划的"收工前跑一遍"门禁。这也说明先前"删切流只值 +2 题"的估计低估了：当时只按"轨迹极短"一个特征计数被切流害死的 trial，漏掉了没被切死但交出半成品的那一类。

仍失败 16 道：`build-pov-ray`、`circuit-fibsqrt`、`dna-assembly`、`extract-elf`、`extract-moves-from-video`、`filter-js-from-html`、`gcode-to-text`、`make-doom-for-mips`、`path-tracing`、`path-tracing-reverse`、`pytorch-model-cli`、`raman-fitting`、`regex-chess`、`sanitize-git-repo`、`torch-tensor-parallelism`、`winning-avg-corewars`。

已落地但**这一跑未测量**：命名输出检测修复（`8acc022`），针对 6 道"必需文件缺失"的红题。

以下为达成 0.75 之前的分析记录。分数记录：`b929af1` / [33464114983](https://github.com/pathlyapp/steerable-framework/actions/runs/33464114983) **Mean 0.6629**（59/89），基线 0.6517。两次跑的同题对照：**50 稳定绿 / 17 翻面 / 22 稳定红**——翻面题全转绿也只到 67/89 = 0.753，所以稳定红里必须出题。

单次全量的测量噪声：SD ±0.0232，95% 带 ±0.0454。`**n_attempts=1` 的单次 A/B 无意义\*\*，故新增 `flaky` split（17 翻面 + 8 受切流影响的稳定绿作对照，共 25 题）× `n_attempts=3` × 双臂 = 50 job，配对符号检验 + bootstrap CI 见 `[evals/flaky_score.py](./evals/flaky_score.py)`。每 trial 加 85 分钟绝对帽（两次全量里只有 4/117 个获胜 trial 超过 90 分钟）。实测一轮约 1.75 h，对比全量 89 的约 6 h。

**功效（模拟，翻面题按 50/50、对照按 0.85 建模）**：

| 真实每题效应 | +0.05 | +0.10 | +0.15 | +0.20 | +0.30 |
| ------------ | ----- | ----- | ----- | ----- | ----- |
| 功效（n=3）  | 0.11  | 0.30  | 0.59  | 0.74  | 0.95  |
| 功效（n=5）  | 0.15  | 0.43  | 0.80  | 0.92  | 1.00  |

假阳性率 0.025。bootstrap CI 只比符号检验高 1–3 个点（+0.10 时 0.33 vs 0.31），**瓶颈是数据量而非检验方法**。含义：这套循环**不能逐个验证 +0.05~+0.10 的小赢**（+0.10 有 70% 概率被判为无差异），但 0.663 → 0.75 需要 +7.7 题、若全来自 17 道翻面题则每题 +0.45，那个量级功效接近 1.00。**所以改动要打包成一个 arm 测，不要一条一条试。**

`flaky` 覆盖不到稳定红（22 道里 0 道），而 7 道恒红螺旋题恰是删切流后最可能受益的。它们的基线是"两跑零通过"，没有可配对的比率，故另开 `spiral-red` split 单臂跑 7 job：**任何一次通过就是结论**，不需要第二臂也不需要配对检验。

### 对照 pi / dsh / codex：参数与循环逻辑

|                    | Steerable                            | pi                        | dsh                | codex                      |
| ------------------ | ------------------------------------ | ------------------------- | ------------------ | -------------------------- |
| 单命令超时         | 1 h                                  | 无（按需传参）            | 120 s（沙箱 60 s） | 10 s 默认 / 用户 shell 1 h |
| 会话软超时         | 150 min（Harbor ×12 → 180 min 硬杀） | 无                        | 无                 | 无                         |
| 最大轮次           | 250                                  | 无                        | 无                 | 无                         |
| 压缩阈值           | **0.8**                              | ctx − 16384（≈0.87–0.92） | 0.8                | **0.9**（有效窗口 95%）    |
| 压缩保留           | 6 条消息 / 2 工具结果                | 20 000 tokens             | retain 0.16        | —                          |
| 流空闲超时         | 170 min（GLM 静默思考可达 48 min）   | 300 s                     | 300 s              | 300 s                      |
| 只推理即切流       | **有**                               | 无                        | 无                 | 无                         |
| `tool_choice` 强制 | **有**（约 18 处）                   | 无                        | 无                 | 无（auto）                 |
| 运行时验证门禁     | **有**                               | 无                        | 无                 | 无（仅提示词）             |
| 持久性提示词       | 无                                   | 无                        | 无                 | **有**                     |

结论：**"竞品分高是因为超时更宽"不成立**——我们的单命令超时和流空闲超时都是四家里最宽的，会话软超时和轮次上限也没有任何一家更紧。三家的高分不来自参数余量。真正的差异是：三家都**没有**切流、轮次上限、`tool_choice` 强制这些机制，而 codex 有一句我们缺的持久性指令。

参数对齐仅剩一处候选：压缩阈值 0.8 → 0.9（codex 值）。压缩次数与失败的横截面梯度很强（0 次 0.743 / 1 次 0.591 / 2–3 次 0.200 / 4+ 次 0.000），但**同题配对 5 比 5、p=0.623 完全为零**，且 `llm-inference-batching-scheduler` 压缩 5 次两轮全过。压缩次数只是"长 trial"的代理量，**没有证据说提阈值能提分**，只能按对齐做，不能当提分手段。overflow 重试 0 次，说明 0.8 全是主动触发。

### 失败日志分析（不做单题特例）

**一个主导失败模式：模型进入无界推理状态后，忽略 harness 的全部强制手段。**

- 推理量 / 工具调用次数是最强预测量，单调：0–3 KB **0.889** / 3–10 KB 0.804 / 10–50 KB 0.426 / ≥50 KB **0.071**。灾难区 12 题（7 恒红、4 翻面、1 恒绿）。**⚠️ 这条只对切流开着时成立。**切流关掉后重测，梯度不再单调，最高推理量桶从 0.071 抬到 0.615——见下文 2.5.9 一节。
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

|                             | arm a 切流开  | arm b 切流关                    |
| --------------------------- | ------------- | ------------------------------- |
| 通过率                      | 51/74 = 0.689 | 59/73 = 0.808                   |
| ≤5 次工具调用就结束的 trial | **15/74**     | 5/73                            |
| 其中通过                    | 4             | 3                               |
| 每题通过率均值变化          | —             | **+0.0933**（净 +2.33 题 / 25） |
| 符号检验                    | —             | 7 胜 4 负，**p=0.55（不显著）** |

**判决依据不是通过率，是可复现的饿死。** 通过率的配对检验没过 0.05，而且按功效模拟这个量级的效应本来就只有约三成概率被测出来，所以它既不能支持也不能否定。决定性的是饿死信号完全可复现：

| 题                              | arm a 工具调用数 | arm a | arm b 工具调用数   | arm b   |
| ------------------------------- | ---------------- | ----- | ------------------ | ------- |
| write-compressor                | **[2, 2, 2]**    | 0/3   | [21, 31, 31]       | **3/3** |
| feal-linear-cryptanalysis       | [2, 4, 13]       | 0/3   | [6, 14, 17]        | **3/3** |
| feal-differential-cryptanalysis | [2, 3, **5435**] | 1/3   | [13, 20, 26]       | **3/3** |
| model-extraction-relu-logits    | **[3, 3, 3]**    | 0/3   | [0, 3]（job 失败） | 0/2     |
| regex-log                       | [0, 3, 4]        | 2/3   | [6, 9, 10]         | **3/3** |

`[2,2,2]` 和 `[3,3,3]` 这种零方差不是采样噪声能产生的，是机制在稳定复现同一个失败。`5435` 那次是切流与重试活锁。目标陈述里独立点名的两道被饿死的题（feal-differential 21→2、write-compressor 14→1）**全部恢复**。

反向代价真实但看着像抖动：sam-cell-seg 3/3→1/3，dna-insert / sparql-university / mailman 各 −0.33，合计 −1.66 题，都是单次 trial 翻面。

落地：三个预算默认值改为 0，机制与环境变量覆盖保留（后续臂可以直接重测，不必重新推导）。加了 `test_stream_cuts_are_off_by_default` 锁住默认值——已验证它能拦住改回 200_000。

### 0.75 还差多少：失败形态拆解（run 33369888461，58/89 = 0.6517）

31 个失败按"干了多少活"拆开，结论是**剩下的缺口不是时间/轮次不够，是产出错误**：

| 形态                                  | 题数   | 能靠什么补         |
| ------------------------------------- | ------ | ------------------ |
| 什么都没产出（≤5 次调用，被切流卡住） | 4      | 切流关闭（已落地） |
| 跑了 10 次调用仍无产出                | 1      | 未知               |
| **产出了但产出是错的**                | **26** | 交付前验证         |

对照组信号很干净：**58 个通过的 trial 里，产出为空的是 0 个**；≤5 次调用结束的 trial 也是 0 个通过。所以"停在 5 次调用以内"= 必败，"什么都没产出"= 必败，两者都已被切流关闭覆盖。

**算术上：58 → 67（0.75）需要 +9 题，切流最多补 4 题，剩下 5 题必须从那 26 道"产出错误"里拿。**

产出为空的统计必须用 `delivery.py` 自己的 `_BASH_WRITES` 判定——只数 `write_file`/`edit_file` 会把 bash heredoc 写文件的情况全部误判成"没产出"（初版算出 7 题，实际 1 题）。

### 验证门禁的触发率与依据修正

门禁要求完成时 `consecutive_explore == 0`，而 `bash` 和 `read_file` 都会让这个计数器 +1，所以**只在"最后一个动作正好是落盘写"时触发**。实测覆盖面：89 个 trial 里 32 个（36%）处于该状态，其中 10 个失败——够覆盖 5 题缺口，不是空转。

但原注释引用的依据（0.647 → 0.775）是用 `write_file`/`edit_file` 这个窄定义算的，用门禁自己那套判定重算后幅度小得多：

| 写完之后又跑了几次调用 | n   | 通过率     |
| ---------------------- | --- | ---------- |
| 0（写完就收工）        | 32  | 0.6875     |
| 1–2                    | 35  | **0.7429** |
| 3–5                    | 11  | 0.7273     |
| 6–10                   | 3   | 0.6667     |
| 11+                    | 4   | **0.0000** |

方向不变（多跑一轮值约 5 个百分点、再多就是瞎折腾），但"再多就有害"这一侧只有 7 个样本。注释已改成按这个口径陈述并写明样本量，避免以后拿它当"放宽预算"的依据。

### 门禁判据已放宽，但"覆盖面 36% → 53%"这个依据是错的

判据已按原计划改成**自上次落盘以来有没有任何东西被执行过**（`ran_since_write`），设计上的矛盾用**反转分类方向**解决：`_BASH_NO_RUN` 枚举的是"看"（cat/head/wc/ls/grep/sed/awk…），任何不认识的词一律算"跑过了"。所以分类器只会让门禁**闭嘴**，不会让它对一个真跑过检查的 trial 乱叫——"看文件的方式"是闭集，"检查文件的方式"不是，这正是原 docstring 反对正则的理由，反转之后就不成立了。

但把这套判据拿回两次全量的日志上重放，**原来估的收益不成立**。重放脚本先用旧判据复现出 `32/89（36%）、通过率 0.688`，与本文档先前记录逐位吻合，所以口径是对的；在此基础上：

| 口径 | run 33369888461（0.6517） | run 33497477757（0.8202） |
|---|---|---|
| 旧判据「最后一个动作是写」 | 32（36%） | 36（40%） |
| 新判据「落盘后什么都没跑」 | 37（42%） | 40（45%） |
| **新增覆盖** | **5（3 个失败）** | **4（0 个失败）** |

不是 47/89（53%）。差在原来那 15 道的口径是"**最后一条命令**只看了一眼"，而实现的判据是"**落盘之后**什么都没跑"——一个先跑了测试、再 `cat` 一眼产出的 trial 属于前者但不属于后者，本来就不该再逼它跑一轮。按实现的判据重切，0.6517 那跑收尾是"看"的只有 7 个，其中 4 个确实没跑过检查；0.8202 那跑 5 个里 3 个，且**三个全过**。

**更要紧的是：36% 从来不是触发率，是"处于该状态"的比例。** 数 0.8202 那跑日志里的 `hook_action … unverified_output`，全量 89 个 trial **实际只触发了 8 次**。差距来自实现：`writes` 在 `self._required` 非空时数的是"指令命名的输出文件由缺变全"，不是 `_bash_writes`；重放用后者当然宽得多。这 8 次里 7 次通过（0.875，全局 0.8202），其中 `largest-eigenval` 正是"交付的程序自己崩了"那 4 道之一，这一跑过了——n=1，只算方向一致。

**结论：这个改动方向对、风险低（上限仍是 1 次重试），但单独测不出来。** 实际触发率会从 8/89 升到大约 9–11/89，远在单次全量 ±4 题的噪声带以内。它必须和别的改动打包成一个 arm，不能指望自己出分。

另外：16 道红题里，门禁实测只对 `extract-elf` 触发过一次。**剩下的缺口基本不在这个门禁的射程内**，下一个靶子要另找。

### 22 道稳定红按 verifier 实际报错分类（不是"模型能力不够"）

按 `verifier/test-stdout.txt` 的真实断言分类，而不是靠日志启发式：

| 形态                     | 题数  | 题目                                                                                                                                                                                                     |
| ------------------------ | ----- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **要求的文件根本不存在** | **6** | adaptive-rejection-sampler `ars.R`、cobol-modernization `TRANSACTIONS.DAT`、dna-assembly `primers.fasta`、path-tracing `image.c`、regex-chess `re.json`、winning-avg-corewars `my_warrior.red`           |
| **交付的程序自己崩了**   | **4** | largest-eigenval（matmul 维度不匹配）、torch-pipeline-parallelism（`UnboundLocalError: mb_idx`）、video-processing（`KeyError: 0`）、make-doom-for-mips（等 frame.bmp 超时）                             |
| 数值差一点               | 3     | extract-moves-from-video 84.03% 差 90%、path-tracing-reverse 0.930 差 0.995、train-fasttext 0.562 差 0.62                                                                                                |
| 实质性错误               | 8     | raman-fitting（拟合值 x0=19196 应为 1580）、gcode-to-text（交了 `PROVISIONAL`）、mteb-retrieve、protein-assembly、schemelike-metacircular-eval、filter-js-from-html、install-windows-3.11、gpt2-codegolf |

**前 10 道是 harness 能碰到的**（文件没交 + 交了但程序崩），值 +0.11。第二类正是验证门禁的目标：跑一次自己的程序就会看到崩溃。

### 命名输出检测：`8acc022` 是空操作，整跑测了个寂寞

**把 `8acc022^` 拉成 worktree、两个版本并排跑完整的 89 份指令：检测结果有差异的题 = 0 道。** 包括它自己的 6 道目标题。

新正则本身是对的，单独喂提交信息引用的那几句都能命中（`` `image.c` must exist `` → `/app/image.c`，`` output files (`ACCOUNTS.DAT`, …) `` → 三个全中，旧版这几句全部返回空）。但放回完整指令，**旧代码早就通过同一份指令中别处的措辞找到了同样的路径**。所以这几个模式是冗余的，不是新增的。

由此：

- **catalog-89 [33514869908](https://github.com/pathlyapp/steerable-framework/actions/runs/33514869908) 是纯空对照**，跑的行为和 0.8202 那跑逐位相同。**已在 54/89 处取消**，不值得为一个空对照烧完剩下的两小时。
- 取消前的翻面**全部是跑间波动**，和代码无关。取消后把 artifact 拉全（67 道完成），逐题配对见下节"同配置重跑"：**均值只差 1 题（0.9254 / 0.9104），但逐题不一致 9/67**。取消当时按 54 道算出的"净 −3"是采样不足，均值口径上 SD ±0.0232 的旧估计没问题；真正被低估的是**逐题稳定性**，不是均值。
- `torch-pipeline-parallelism` 这次绿→红特别要紧：0.8202 那跑它是"切流关闭后模型自行发现并修好 `UnboundLocalError`"的招牌案例，还被当成"不需要收工前跑一遍门禁"的依据。它一跑就掉，说明**那个结论只建立在单次 trial 上，不能再拿来否决验证门禁**。

**方法论教训：验证一个修复"命中目标"不等于验证它"改变了行为"。上机前必须和改动前的 commit 并排比，不能只跑新版本看命中。**

**6 道文件缺失的真实原因已查明，见下一节。** 原推断"检测器漏了路径"已被证伪——路径一直都检测得到，`_required` 一直包含它们，门禁本该拒绝完成（最多 32 次重试）。

以下为证伪前的分析记录。`DeliveryHooks` 本来会在指令命名的输出缺失时拒绝完成（最多 32 次重试），所以文件缺失只有两种解释：被切流杀掉，或者**检测器从没把那个路径当成输出**。当时只在新版本上实测这 6 道全部命中，误判为"检测器全部漏掉"。

原因：现有 5 个模式各锚定一种动词措辞（`write a file X`、`named X`、`a new file X`……），而这几道把要求写成**检查清单**：

- `1. **File Existence**: \`image.c must exist`
- `2. **Warrior Exists**: Confirms \`my_warrior.red was created`
- `1. `primers.fasta` exists and contains exactly 8 primer pairs`
- `- The output files (`ACCOUNTS.DAT`, `BOOKS.DAT`, `TRANSACTIONS.DAT`) must match byte-for-byte`

**通用规则"反引号裸文件名都算输出"已被实测否决**：跨 89 份指令会给 58 道通过题里的 49 道加上共 105 个假需求，包括 `test_outputs.py`（隐藏测试本身）和 `np.float64`（根本不是文件）。所以短语锚定是承重的，不是堆积。

改为锚定**存在性断言**（`X must exist` / `Confirms X was created` / `output files (X, Y, Z)`）：**6/6 全部命中，通过题上只多出 3 个候选**（dna-insert `primers.fasta`、financial-document-processor `summary.csv`、polyglot-rust-c `main.rs`，都是真实输出，已交付所以门禁不触发）。输出列表要捕获整段，因为文件名本身带点号，正则不能在句号处停。

新增 3 个测试，含一个反向测试锁住选择性（断言 `test_outputs.py` / `np.float64` / `chess_board.png` 不被当成输出）。

### 2.5.9 结论："6 道文件缺失"这个靶子在 0.8202 那跑只剩 1 道，且不是门禁问题

那份分类出自 0.6517 / 0.6629（切流还开着）。回到 0.8202 的 `result.json` + `verifier/test-stdout.txt` 逐题看，六道题的真实状态是：

| 题                        | 0.8202 结果 | 实际断言                                              |
| ------------------------- | ----------- | ----------------------------------------------------- |
| adaptive-rejection-sampler | **绿**      | reward 1.0，65 min                                    |
| cobol-modernization        | **绿**      | reward 1.0，撞满 170 min 仍过——**靠收工窗口救回来的** |
| path-tracing               | 红          | `test_image_c_exists` **PASS**，编译运行都过，图像相似度 0.9626 差 0.99 |
| winning-avg-corewars       | 红          | `test_warrior_exists` **PASS**，胜率 31% 差 75%、20% 差 33% |
| dna-assembly               | 红          | `primers.fasta` 存在，正向引物 Tm 73.64 超上限 72     |
| **regex-chess**            | 红          | `/app/re.json` 真的不存在                             |

**只剩 1 道真的文件缺失。** 另外三道文件都交了，是内容不达标；`dna-assembly` 差 1.64 °C，是"差一点"而不是"没干"。

**门禁从未被绕过，它触发了。** 日志里 `retry missing_named_output`：regex-chess **5** 次、winning-avg-corewars 2 次、path-tracing 1 次、cobol-modernization 1 次。软超时也每道都在 150 分钟准点发出（`elapsedMs` 9000001 / `softTimeoutMs` 9000000），收工模式确实进了。**拒绝完成并不能让模型把文件写出来**——这是这次调查最该记住的一句。

`cobol-modernization` 是反面证据，说明收工窗口本身是有效的：软超时后它在窗口里做了 13 次调用（含 `write_file` / `edit_file`），从而转绿。

### 真正的约束是墙上时钟，判别量是"每步吐多少字"而不是"走了多少步"

16 道红题按结束方式重分（`/tmp/reclass.py`）：

| 结束方式                       | 题数   | 题目                                                                                     |
| ------------------------------ | ------ | ---------------------------------------------------------------------------------------- |
| 撞 170 min 硬杀                | **6**  | dna-assembly、gcode-to-text、regex-chess、path-tracing、circuit-fibsqrt、winning-avg-corewars |
| 过了 150 min 软超时、自己收了尾 | **4**  | path-tracing-reverse、extract-moves-from-video、make-doom-for-mips、filter-js-from-html   |
| 没碰到任何超时，早早交了错答案  | 6      | raman-fitting、build-pov-ray、torch-tensor-parallelism、extract-elf、pytorch-model-cli、sanitize-git-repo |

**10/16 用完了 150 分钟的软超时预算；73 道绿题里只有 5 道碰到软超时。** 硬杀都是 10201 s = 170.02 min，即 `_hard_run_timeout_sec()` 的 10_200。

**但"撞钟"是它们的结束方式，不是失败原因，别把这 10 道当成"给够时间就能拿到"。**逐题看判据：

| 题                      | 结束        | 判据差距                    | 多给时间有戏？ |
| ----------------------- | ----------- | --------------------------- | -------------- |
| dna-assembly            | 硬杀        | Tm 73.64 差 ≤72             | **有**         |
| path-tracing            | 硬杀        | 相似度 0.9626 差 0.99       | **有**         |
| winning-avg-corewars    | 硬杀        | 胜率 31% 差 75%             | 差太远         |
| gcode-to-text           | 硬杀        | 交出 `nseg 26…` 而非 flag   | 答案是错的     |
| regex-chess             | 硬杀        | 无产出，170 min 未找到思路  | 没找到方法     |
| circuit-fibsqrt         | 硬杀        | 最后两次调用还是 `ls /app`  | 从未开工       |
| path-tracing-reverse    | 自己收尾    | 相似度 0.464 差 0.995       | 差太远         |
| extract-moves-from-video | 自己收尾    | 63.2% 差 90%                | 差太远         |
| make-doom-for-mips      | 自己收尾    | 0.745 差 0.95               | 差太远         |
| filter-js-from-html     | 自己收尾    | XSS 过滤断言 False          | 答案是错的     |

**放宽时钟能指望的只有 2 道，不是 10 道。**后 4 道尤其说明问题：它们有完整 150 分钟 + 收工窗口、自己主动收了尾，交出来的东西离判据很远。时间不是它们的瓶颈。

中位数对照（`/tmp/where_time_goes.py`）：

|      | 时长       | 日志       | 工具调用 | 每次调用耗时  |
| ---- | ---------- | ---------- | -------- | ------------- |
| 绿   | **25 min** | 135 KB     | 26       | **0.8 min**   |
| 红   | **160 min** | 1391 KB    | 32       | **3.3 min**   |

**调用次数几乎一样（26 vs 32），时长差 6.4 倍。**时间不是花在干活上，是花在每一步的文本生成上。极端例子：`circuit-fibsqrt` 170 分钟只做了 2 次调用（85 min/次），`regex-chess` 4 次（42.5 min/次）。

**顺带证伪一条旧结论：推理量的单调梯度在切流关掉之后不成立了。**原记录是 ≥50 KB 通过率 0.071，现在按日志推理字节分桶：

| 推理量     | n   | 通过率    | 中位时长 |
| ---------- | --- | --------- | -------- |
| 0–100 KB   | 35  | 0.943     | 6 min    |
| 100–300 KB | 18  | 0.778     | 29 min   |
| 300–700 KB | 12  | 0.917     | 74 min   |
| 700–1500 KB | 11  | 0.636     | 124 min  |
| 1500+ KB   | 13  | **0.615** | 89 min   |

不单调，且高推理量桶从 0.071 抬到 0.615。**"推理多就要死"是切流造出来的相关性，不是模型的性质。**推理多只是慢，慢到撞钟才死。

### regex-chess：收工窗口被 16 次无效重试烧干（唯一一道真缺文件）

150 分钟内 4 次调用、1210 KB 文本，全在纸上设计一个用 `re.sub` 实现的走子生成器。软超时后的 20 分钟窗口里：**0 次工具调用、138 KB 文本、`tool_choice=required` 连设 10 次、`round` 始终停在 2**。模型自己写下"given time pressure, I need to write something"，然后继续设计到被硬杀。

轮次不前进是因为这条路走的是 `completion_redos`（上限 `_MAX_COMPLETION_REDOS = 32`）和 `wrap_up_tool_rounds_used`（上限 `wrap_up_max_tool_rounds = 16`），不是 `round_index`，所以 `max_rounds=250` 拦不住。`loop.py` 那段注释本来就点名了这道题（"regex-chess summarized instead of writing /app/re.json"），设计意图是"文本收尾在保留工具的收工模式里不算终态，再给几轮"。**意图对，结果是：16 次机会 × 每次约 2 分钟生成 = 32 分钟 > 剩下的 20 分钟，整个窗口被无效重试吃光。**

这是"`tool_choice=required` 只有 62% 效力"的终局形态：强制不生效时，循环没有任何别的手段，只会原地再问一次。

下一步候选（都还没测）：

1. **活锁检测**：连续 N 轮强制且零工具调用就停止重问，改做别的（缩短提示、截断历史、或直接放弃这道题省下时钟给别的分片）。这是唯一一处明确坏掉的地方，可测、有界。**但要先承认它的上限：`regex-chess` 和 `circuit-fibsqrt` 是 170 分钟没找到解法，把窗口还给它们未必换到分。**修它的理由是机制坏了，不是它值 2 题。
2. **压每步文本量**而不是打断它。切流是"打断"，已证伪；`max_tokens` 目前没设，是"按 token 截断"，同样会截在思路中间，必须 A/B。
3. **别再往命名输出检测和门禁上加东西**——这条线的靶子只剩 1 道题，而且那道题的门禁已经触发过 5 次。

### 同配置重跑：逐题 pass/fail 的翻面率是 13.4%，"16 道红题"这份名单不能当靶子

**这件事仓库里早有记录**：`harbor_steerable.py` 的 `_TUNING_KEYS` 注释写着"17 of 89 tasks flip outcome between runs of the same commit"（19%），本文档前面也记着"50 稳定绿 / 17 翻面 / 22 稳定红"。下面只是用同配置的第二个样本复现了它，**不是新发现**；新的只是由它反推出的功效表。

被取消那跑（`33514869908`）和 0.8202 行为逐位相同，所以两者配对就是**纯噪声测量**。把已完成分片的 artifact 拉全，67 道有配对（`/tmp/stability.py`）：

|                | 0.8202 | 同配置重跑 |
| -------------- | ------ | ---------- |
| 这 67 道的均值 | 0.9254 | 0.9104     |
| 逐题不一致     | —      | **9 / 67 = 13.4%** |

- 红→绿 4 道：`build-pov-ray`、`extract-elf`、`pytorch-model-cli`、`torch-tensor-parallelism`
- 绿→红 5 道：`chess-best-move`、`dna-insert`、`mteb-retrieve`、`protein-assembly`、`torch-pipeline-parallelism`

**16 道红题里只有 5 道拿到了同配置的第二个样本，其中 4 道变绿。**（拿到第二样本的恰好是跑得最快的 5 道，因为慢的分片在取消前没跑完——这是选择性,不是随机抽样，但方向足够清楚。）稳定红的完整名单要看下面三个完整样本那节，不是这里的 67 道子集。

**结论：均值比逐题稳定得多**，因为翻面双向抵消（丢 5 得 4，净 −1）。所以：

- 用均值衡量改动是可以的，SD ±0.0232 那套仍然成立。
- **用"哪几道是红的"当靶子是不行的。**13.4% 的翻面率意味着 16 道红题里大约有 9 道分量是掷硬币。今天做的全部逐题归因，成立的口径是"那一个 trial 里发生了什么"，**不是"这道题为什么失败"**。

这条也把上一节"12/16 是答案实质错误、harness 碰不到"打掉了一半：那 6 道早收工的红题里，5 道有第二样本，4 道直接变绿。它们不是能力墙。

**唯一不受这条影响的发现是收工窗口活锁**：16 次重试 × 每次约 2 分钟 > 剩余 20 分钟是算术，不是抽样。机制坏了就是坏了，与那道题这一跑红不红无关。

### 由实测翻面率反推：单次全量只能分辨 ≥10 题的改动

两次同配置跑在 67 道上不一致 9 道。某道题真实通过率为 p 时两跑不一致的概率是 2p(1−p)，所以这个比率直接给出全套的平均逐题方差，不需要知道每道题的 p（`/tmp/power.py`）：

| 量                     | 值                        |
| ---------------------- | ------------------------- |
| 逐题不一致率           | 0.134                     |
| 反推平均 p(1−p)        | 0.0672                    |
| 单跑均值的 SD          | **0.0275（2.4 题）**      |
| 单跑对单跑差值的 SD    | **0.0388（3.5 题）**      |

**独立路径反推出的 0.0275 和文档里原有的 SD ±0.0232 基本吻合**，两个估计互相印证，所以这套噪声口径可以信。

单臂单跑、双侧 0.05、功效 0.8 所需的全量次数：

| 真实效应 | 每臂全量次数 | 每臂机时（按 3 h） |
| -------- | ------------ | ------------------ |
| 1 题     | 94           | 282 h              |
| 2 题     | 24           | 72 h               |
| 3 题     | 11           | 33 h               |
| 5 题     | 4            | 12 h               |
| **10 题** | **1**        | **3 h**            |

25 题子集 × n 次的最小可分辨效应：n=1 → 5.1 题，n=3 → 3.0 题，n=5 → 2.3 题，n=10 → 1.6 题。

**操作规则：单次全量只能用来验收 ≥10 题的改动。**手上所有候选（活锁检测、`max_tokens`、持久性指令、门禁判据）单个都值 1–3 题，**没有一个能用全量验收**。要么打包成一个 ≥10 题的 arm，要么走 25 题 ×3 的配对子集，要么按机制正确性验收、根本不上全量。

### `27d521a` 四个完整样本：均值 0.8006 ± 0.0232，0.8202 是分布上沿而非定点

2.5.13 的三次复现跑全部收齐。四个样本都是 89 道全量、同一个 commit `27d521a`、同一份配置，**唯一变量是随机性**：

| 样本 | run | Mean |
| ---- | --- | ---- |
| 基线 | [33497477757](https://github.com/pathlyapp/steerable-framework/actions/runs/33497477757) | 0.8202（73/89） |
| 第二 | [33530806570](https://github.com/pathlyapp/steerable-framework/actions/runs/33530806570) | 0.7865（70/89） |
| 第三 | [33530856872](https://github.com/pathlyapp/steerable-framework/actions/runs/33530856872) | 0.8202（73/89） |
| 第四 | [33547943349](https://github.com/pathlyapp/steerable-framework/actions/runs/33547943349) | 0.7753（69/89） |

**均值 0.8006，SD 0.0232（2.1 题），极差 4 题（0.7753–0.8202）。**

这个 0.0232 和文档前面早就记着的 SD ±0.0232 完全吻合，也和由翻面率反推的 0.0275 吻合。**三条互相独立的路径给出同一个噪声量级，这套口径可以当定论用。**

**对外报数应当用 0.80，不是 0.82。**0.8202 在四次里出现两次，是分布上沿；把它当基线会让后续任何改动都平白背上 −2 题的起点。

两两逐题翻面率：16.9%、13.5%、11.2%、10.1%、12.4%、11.2%，平均 12.6%。和之前 67 道配对测出的 13.4% 一致。

四次样本下 89 道分三档：

- **稳定绿 60 道**（4/4 通过）
- **稳定红 9 道**（0/4）：`extract-moves-from-video`、`filter-js-from-html`、`gcode-to-text`、`make-doom-for-mips`、`pytorch-model-cli`、`raman-fitting`、`regex-chess`、`sanitize-git-repo`、`winning-avg-corewars`
- **翻面 20 道**，按通过次数排（G=过，顺序为基线/第二/第三/第四）：

| 4 次里过 1 次 | 4 次里过 2 次 | 4 次里过 3 次 |
| ------------- | ------------- | ------------- |
| `circuit-fibsqrt` RRGR | `code-from-image` GRGR | `bn-fit-modify` GGRG |
| `make-mips-interpreter` GRRR | `dna-assembly` RGGR | `build-pov-ray` RGGG |
| `path-tracing-reverse` RGRR | `extract-elf` RGGR | `chess-best-move` GRGG |
| `video-processing` GRRR | `install-windows-3.11` GRRG | `largest-eigenval` GGGR |
| | `model-extraction-relu-logits` GRGR | `modernize-scientific-stack` GRGG |
| | `mteb-retrieve` GRRG | `path-tracing` RGGG |
| | `protein-assembly` GGRR | `sam-cell-seg` GRGG |
| | | `torch-tensor-parallelism` RGGG |
| | | `train-fasttext` GGGR |

**这 20 道是 2.5.12 要的 `flaky` split 的定稿底稿**，比按切流那轮翻面集选的旧 25 题可信：来自四个纯同配置样本，不掺代码差异。**配对 A/B 只在这 20 道上跑**——60 道稳定绿和 9 道稳定红提供不了信号，只烧机时。20 道 ×3 次的最小可分辨效应约 3.3 题，是单次全量 10 题门槛的三分之一，机时只要 1/5。

**9 道稳定红是唯一可以按"这道题为什么失败"归因的集合**，其余 20 道只能按"那一个 trial 里发生了什么"讲。注意 4 次样本判"稳定"仍带 1/16 的单题误判率，20 道里预期有 1–2 道其实是低通过率而非真稳定。

### 同模型换 harness（`pi-glm`）：均值持平，但错的题不一样，且 `max_tokens` 就此证伪

新增 `pi-glm` agent——Harbor 自带的 Pi 装在容器里，指向**同一个模型、同一个网关**，所以与 `steerable` 相比只差 harness 一个变量。首跑 `cheap-12`（12 道）[33583927705](https://github.com/pathlyapp/steerable-framework/actions/runs/33583927705)：

| | 这 12 道 |
| --- | --- |
| `pi-glm` | 10/12 = 0.833 |
| `steerable`（4 样本期望） | 10/12 = 0.833（10 道 4/4，2 道 0/4，无翻面题） |

均值持平，**但失败的题不重合**，这才是有信息量的部分：

- **`sanitize-git-repo`：pi 过了，steerable 4/4 全挂。** 这道题在上节 9 道稳定红里，而上节刚说过那 9 道是"唯一可以按能力归因的集合"。**证伪：至少这 1 道不是 GLM 的能力墙，是我们 harness 的问题。**"稳定红 = 能力上限"这个推断不成立，必须逐题拿同模型对照去证。
- **`polyglot-c-py`：steerable 4/4 过，pi 挂。**
- `filter-js-from-html`：两边都挂（steerable 也是 0/4）。

**pi 的两道失败是同一个机制，而且机制清楚**：`out` 恰好等于 16384（pi 的输出上限），`in` 只有约 1500（且 1344 是缓存），说明**整个 trial 只发出过一个请求，模型在第一轮就跑飞、被截断，一个工具都没调**，verifier 报的是目标文件根本不存在。`polyglot-c-py` 为此烧了 8 分钟。

这正是我们在 steerable 上追的"推理跑飞"，换个 harness、同一个模型照样发作——**说明这个病至少有一部分在模型侧，不是我们 harness 独有的缺陷**。

**并且它直接把 2.5.11 的 `max_tokens` 方案证伪了，不用再 A/B：**pi 就是设了 16384 硬上限，结果是"截断即失败"而不是"截断后收敛"；steerable 设的是 65536，这两道题 4/4 全过。**硬截断不解决跑飞，只是把跑飞换成截断。**要压的是每步文本量的产生方式，不是给它一把铡刀。

两个已知限制：

1. **reasoning effort 没对齐。** steerable 用 `reasoning_effort=max`；pi 走 Harbor 写的 `models.json` 自定义 provider，那个模型条目没有 OpenRouter 的 `compat` 块，`thinking: xhigh` 可能到不了请求。所以"均值持平"这个结论的强度有限，但**逐题差异（谁过谁挂）不受这个影响**，那是本节的主要产出。
2. **对照 agent today 拿不到官方基线。** 仓库 secrets 只有 `STEERABLE_API_KEY` / `STEERABLE_BASE_URL` / `FEISHU_BOT_WEBHOOK` / `NPM_TOKEN` / `PYPI_API_TOKEN`，没有 `ANTHROPIC_API_KEY` 和 `OPENAI_API_KEY`，所以 `claude-code` / `codex` / `pi`（Claude）三格都 skip。

- [x] **2.5.1** cuts A/B 完成
- [x] **2.5.2** 切流默认关闭（保留 env 覆盖）
- [x] **2.5.5** 命名输出检测补上检查清单措辞（6/6，假阳性 3）—— **实为空操作**，89 道题上检测结果零差异，见上节
- [x] **2.5.3** 加 codex 式持久性指令（绿 trial 中位数只用掉 150 分钟里的 12 分钟）。codex 是三家对照里唯一带这条的，参数对照也没找到别的对我们不利的不对称
- [x] **2.5.6** 门禁判据改成 `ran_since_write`（见上节；方向对但单独测不出来）
- [x] **2.5.4** 全量 89 记录 Mean ≥ 0.75 —— `27d521a` 0.8202
- [x] **2.5.7** 测 `8acc022`：[33514869908](https://github.com/pathlyapp/steerable-framework/actions/runs/33514869908) 已证实是空对照，改动本身不改变任何一道题的行为。这一跑改用途，只当第三个噪声样本
- [ ] **2.5.8** 持久性指令 + 门禁判据打包成一个 arm 测。单次全量分辨不了这个量级，按本文档自己的功效表要走配对 A/B，子集用上节定稿的 20 道翻面题 ×3 次
- [x] **2.5.9** 查那 6 道"文件缺失"红题的真实结束方式 —— 已查明：分类过期（只剩 1 道真缺文件），门禁触发过但无效，真约束是墙上时钟。见上两节
- [ ] **2.5.10** 收工窗口活锁检测：连续 N 轮 `tool_choice=required` 且零工具调用就换策略，不再原地重问。`regex-chess` 在 20 分钟窗口里被 16 次无效重试吃光。**理由是机制的算术错了，不是它值几题**，不要拿单题结果验收
- [ ] **2.5.11** 压每步文本量（红题 3.3 min/次调用 vs 绿题 0.8）。切流式"打断"已证伪；**`max_tokens` 也已证伪**，不必再 A/B——`pi-glm` 的 16384 硬上限把跑飞变成截断即失败，见上节。要改的是文本产生方式（提示词、工具调用时机），不是加铡刀
- [ ] **2.5.14** 逐题拿 `pi-glm` 对照那 9 道稳定红，判定哪几道真是 GLM 能力墙、哪几道是我们 harness 的问题。`sanitize-git-repo` 已确认属后者。这是当前唯一能把"能力上限"和"harness 损耗"分开的手段
- [ ] **2.5.15** `pi-glm` 的 reasoning effort 与 steerable 对齐（Harbor 写的 `models.json` 缺 OpenRouter `compat` 块）。要么在我们仓库里子类化 Harbor 的 Pi 补上 compat，要么改用内置 `openrouter` provider（前提是网关就是 openrouter.ai）。不对齐时只能读逐题差异，不能读均值差
- [x] **2.5.12** 逐题翻面率平均 12.6% 已在四个完整同配置样本上实测，**所有靶子选择都要按均值和多样本来，不能按单跑的红题名单**。`flaky` split 定稿为上节那 20 道
- [x] **2.5.13** 0.8202 复现性：在 `ci/evals-stability-27d521a`（钉死 `27d521a`，**不含 `20a854d` 的门禁与提示词改动**）连跑 3 次全量，与基线合成 4 个样本。结论：**均值 0.8006 ± 0.0232，0.8202 是上沿不是定点，对外报数用 0.80**。workflow 的并发组只容得下 1 个运行中 + 1 个待队列，三次是串行的，共约 10 h

---

## 明确不做（除非另开文档改目标）

- 自制 prompt YAML / 知识库场景题当作能力门禁
- LLM-as-judge
- 把 Electron GUI、PTY、deeppath-knowledge 场景塞进 TB/SWE
- 把网关 key 写进 `OPENAI_API_KEY`（Codex 对照会误打官方端点）
- 本机 Clash 当 GHA 出网代理
- DeepSeek Harness 进同一矩阵（它也还没有 Harbor adapter）
- 在产品还不能 TB 交卷时开 SWE
