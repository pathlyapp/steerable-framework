# Evals

Public **capability** evals for coding agents, not Steerable unit tests and not homemade prompts.

The gate is [Terminal-Bench 2.1](https://github.com/harbor-framework/terminal-bench-2-1) through [Harbor](https://www.harborframework.com/docs/run-jobs/run-evals). Task ids, the Harbor dataset name, and the git SHA used to enumerate the catalog live in `evals/suite.yaml`. Scoring is the tasks' hidden pytest; there is no LLM judge.

## What runs

| Layer | Trigger | Agents | Tasks |
| ----- | ------- | ------ | ----- |
| L0 | every PR (`uv run pytest`) | none | suite YAML invariants |
| Oracle smoke | PR / push when `evals/**` changes, plus `workflow_dispatch` | Harbor `oracle` (Mean 1.0); product `steerable` canary when a key is set | `oracle-canary` (`fix-git`) |
| L2 weekly | Monday cron + `workflow_dispatch` | `steerable`, `claude-code`, `codex`, `pi` | `cheap-12` (1 attempt) |
| L2 failed-prev | `workflow_dispatch` on `Evals weekly` with split `failed-prev` | `steerable` | previous catalog zeros + install errors + 1→0 flips (~41 ids, 16 shards) |
| L2 catalog | `workflow_dispatch` on `Evals weekly` with split `catalog` | `steerable` | full `catalog` (89 ids, 16 shards) |

L2 is **not** a required merge check. A matrix cell whose API key secret is empty is skipped. The product cell needs `STEERABLE_API_KEY` and `STEERABLE_BASE_URL` (the same OpenAI-compatible gateway used locally). Baseline cells need official Anthropic / OpenAI keys. The workflow fails if every live agent was skipped. Weekly Harbor uses `--n-concurrent 2` (local suite default stays 1). Feishu is best-effort: a webhook failure does not fail the eval. Mean is appended to the GitHub job summary when `GITHUB_STEP_SUMMARY` is set.

DeepSeek Harness is listed in `suite.yaml` as skipped: it has no Harbor `BaseInstalledAgent`. Its own ACP snapshots remain L0 harness-contract tests in that repository. Headless `pnpm dsh --profile headless` is not this gate.

## Agents

Harbor first-party names: `oracle`, `claude-code`, `codex`, `pi`. Product agent: `steerable` (`evals.harbor_steerable:SteerableHarborAgent`), headless CoreLoop with workspace bash/file tools.

Pi installs [`@earendil-works/pi-coding-agent`](https://www.npmjs.com/package/@earendil-works/pi-coding-agent) in the trial container (`harbor run -a pi`). Claude Code and Pi default to `anthropic/claude-sonnet-4-5` so cheap-12 compares harness behavior. Codex uses `openai/gpt-5.5`. The product agent defaults to `openai/z-ai/glm-5.3-flash` (OpenRouter GLM-5.3-Flash). Override with `python -m evals.run --model …`.

## cheap-12

Twelve Terminal-Bench 2.1 ids that avoid QEMU, GPU, video, and long compiles. They must stay a subset of the 89-id catalog (enforced in `evals/tests`).

`fix-git`, `openssl-selfsigned-cert`, `sqlite-db-truncate`, `nginx-request-logging`, `configure-git-webserver`, `sanitize-git-repo`, `polyglot-c-py`, `log-summary-date-ranges`, `filter-js-from-html`, `password-recovery`, `git-multibranch`, `sqlite-with-gcov`.

A product cheap-12 at `n_concurrent: 1` is a multi-hour job (local glm-5.3-flash, Mean 0.750: 2h06m). `filter-js-from-html` alone can take ~30 minutes. The weekly GHA job timeout is 240 minutes; `--n-concurrent 2` is the GHA override. Harbor prints `harbor progress: done/started` every minute so a long run is not mistaken for a hang.

The full 89-id catalog is `Evals weekly` → `workflow_dispatch` → split `catalog` (never on a pull request). It splits the suite into 16 shards (`--shard N --shards 16`), each with a 360-minute timeout. Feishu merges shard `result.json` files into one Mean. QEMU, Windows 3.11, video, and long compiles live only in this split.

Harness iteration uses split `failed-prev` (41 ids, 16 shards) so a headless change is not gated on rerunning all 89. Sixteen shards keep a 170-minute agent wrap inside the GitHub-hosted 360-minute job cap when several long tasks land together. Catalog and failed-prev use `--agent-timeout-multiplier 12` so a 900s Terminal-Bench task gets 180 minutes (Harbor would otherwise kill it at 45 minutes, before wrap). cheap-12 stays at ×3 so the weekly smoke fits 240 minutes. That Mean is not the score of record; catalog 89 is.

Do not run all 89 on every PR. SWE-bench Verified is the next public standard **after** the product agent has a Terminal-Bench Harbor score; run the full Verified set, never a homemade 20-task subset. Work order: [`EVALS_TODO.md`](https://github.com/pathlyapp/steerable-framework/blob/main/EVALS_TODO.md).

## Local

Install Harbor (`uv tool install harbor`). Docker is required except for `--dry-run`.

```bash
python -m evals.run --agent oracle --split oracle-canary --dry-run
python -m evals.run --agent steerable --split oracle-canary
python -m evals.run --agent pi --split cheap-12
python -m evals.run --agent claude-code --split cheap-12 --tasks fix-git
```

Wrapper flags map onto Harbor: `--dataset terminal-bench/terminal-bench-2-1`, `--include-task-name` per id, `--yes`, `--n-attempts 1`. Outputs go to `evals/jobs/<agent>/`.

## Secrets

| Agent | GitHub Actions secret |
| ----- | --------------------- |
| `steerable` | `STEERABLE_API_KEY` and `STEERABLE_BASE_URL` (OpenRouter / 万界; same pair as local glm) |
| `claude-code`, `pi` | `ANTHROPIC_API_KEY` (official; optional, cell skips) |
| `codex` | `OPENAI_API_KEY` or `CODEX_API_KEY` (official; optional, cell skips) |
| `oracle` | none |
| Feishu 结果通知 | `FEISHU_BOT_WEBHOOK`（自定义机器人 webhook，标题为「成功」或「失败」） |

## Out of scope

- Homemade prompt YAML as the merge gate
- Coder Eval skill/CLI A/B as the primary gate
- LLM-as-judge
- DSH live Terminal-Bench until a Harbor adapter exists
