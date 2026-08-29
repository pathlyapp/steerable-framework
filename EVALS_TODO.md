# 产品能力评测流水线

> 目标：用业界公开集给 **DeepPath agent / Steerable CoreLoop** 打可对外的能力分。
> 顺序：**先 Terminal-Bench 2.1 跑通，再上全量 SWE-bench Verified。**
> 跑分器是 [Harbor](https://www.harborframework.com/docs/run-jobs/run-evals)，隐藏测试打分，不用 LLM judge，不自制 prompt 当门禁。
>
> 套件钉死在 [`evals/suite.yaml`](./evals/suite.yaml)。当前能力说明：[`docs/evals.md`](./docs/evals.md)。

**评测对象**：无头 CoreLoop + 能改文件、跑 shell 的工具面（sidecar ACP）。
不是 Electron 窗口，也不是 Harbor 自带的 `claude-code` / `codex` / `pi`（那些只做同题对照）。

**正式分在 GitHub Actions**（`ubuntu-latest`，官方 `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`）。本机代理只用于改 adapter，不作为分数来源。

---

## 已完成（对照基线，还不是产品分）

- [x] TB 2.1 catalog（89）+ `cheap-12` + `oracle-canary`（`fix-git`）钉在 `evals/suite.yaml`
- [x] L0：`evals/tests` 随 `uv run pytest`（无 Harbor、无 Docker）
- [x] Harbor wrapper：`python -m evals.run`
- [x] workflow 草稿：`.github/workflows/evals-oracle.yml`、`evals-weekly.yml`
- [x] 本机 Harbor oracle × `fix-git`：**Mean 1.000**（题和隐藏测试通）
- [x] Harbor CLI 版本钉在 suite：`run.harbor_version: "0.22.0"`

未完成：GHA 正式 cheap-12 产品分尚未落盘；本机 Claude Code 因容器 Debian 源失败，**忽略，改走 GHA**。

---

## Phase 0 · TB 基础设施（GHA 能打对照分）

出口：GHA 上 oracle × `fix-git` Mean **1.0**；weekly 能跑基线（有官方 key）或明确 skip（无 key 则整次失败）。

- [x] **0.1** `harbor_argv` 把 `--include-task-name` 写成 `terminal-bench/<短 id>`（否则过滤为空）
- [x] **0.2** Harbor 安装：composite action 钉 `harbor==0.22.0`，`~/.local/bin` 进 `PATH`
- [x] **0.3** oracle workflow：`evals/**` 变更 + `workflow_dispatch`；Mean ≠ 1.0 或安装异常 → 红；上传 `result.json`
- [x] **0.4** weekly：cheap-12 × `steerable` / `claude-code` / `codex` / `pi`；官方 key；缺 key skip，全 skip 失败；artifact + Mean 摘要
- [ ] **0.5** 仓库 secrets：`ANTHROPIC_API_KEY`、`OPENAI_API_KEY`（官方，不用万界、不配 `*_BASE_URL`）
- [x] **0.6** 安装失败当红（`n_errored_trials > 0`）；题没做对只记 Mean（0 是成绩）

---

## Phase 1 · 产品能在 TB 上交卷（阻塞 SWE）

出口：GHA 上 **产品 agent** × `fix-git` 跑完，有 Harbor Mean（允许 0，但必须是隐藏测试分，不能是 setup timeout / apt 失败）。
前一项不过，不要铺 cheap-12，不要开 SWE。

- [x] **1.1** ACP 工具桥：默认 in-process `bash` / `read_file` / `write_file`（`workspace_tools.py`）。Editor fs/terminal 仍是 follow-up
- [x] **1.2** 无头启动：`python -m steerable_sidecar.headless`（`--instruction` / `--instruction-file`）
- [x] **1.3** Harbor adapter：`evals.harbor_steerable:SteerableHarborAgent`；`suite.yaml` `steerable` 未 skipped
- [ ] **1.4** 同一题对照：oracle Mean = 1.0 **且** 产品 canary 交卷（Harbor Mean 落盘）
- [x] **1.5** GHA 矩阵加上产品 agent（oracle workflow 的 `steerable` job + weekly 矩阵）

---

## Phase 2 · TB cheap-12 产品分

出口：GHA 一张表：产品 vs `claude-code` / `codex` / `pi`，同一 cheap-12、同一隐藏 pytest、n-attempts=1。

- [ ] **2.1** weekly（或独立 workflow）跑产品 × cheap-12
- [ ] **2.2** 钉模型档，便于和基线比 harness 而不是比模型（Claude/Pi 默认 `anthropic/claude-sonnet-4-5`，Codex `openai/gpt-5.5`；产品用同一档或文档写明差异）
- [ ] **2.3** Mean / exception / artifact 进 job summary；周更
- [ ] **2.4** 全量 89 题：可选、手动/`workflow_dispatch`，**不上每 PR**

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

## 明确不做（除非另开文档改目标）

- 自制 prompt YAML / 知识库场景题当作能力门禁
- LLM-as-judge
- 把 Electron GUI、PTY、deeppath-knowledge 场景塞进 TB/SWE
- 用万界 key 或本机 Clash 当 GHA 正式分
- DeepSeek Harness 进同一矩阵（它也还没有 Harbor adapter）
- 在产品还不能 TB 交卷时开 SWE
