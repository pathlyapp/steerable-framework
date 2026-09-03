# Evals

Public **capability** evals for coding agents, not Steerable unit tests and not homemade prompts.

The gate is [Terminal-Bench 2.1](https://github.com/harbor-framework/terminal-bench-2-1) through [Harbor](https://www.harborframework.com/docs/run-jobs/run-evals). Task ids, the Harbor dataset name, and the git SHA used to enumerate the catalog live in `evals/suite.yaml`. Scoring is the tasks' hidden pytest; there is no LLM judge.

## Score of record

**Steerable + GLM-5.3-Flash = 80%** on the 89-task catalog. Four independent full runs at commit `27d521a`, mean **0.8006**, SD **0.0232**. We report 80, not the 0.8202 high-water mark (that value appeared twice and is the top of the distribution). Current `main` also contains the persistence prompt and post-write verification gate from `20a854d`; those changes are newer than the measured commit and are not included in the 80% score.

| Sample | GitHub Actions | Mean |
| ------ | -------------- | ---- |
| 1 | [33497477757](https://github.com/pathlyapp/steerable-framework/actions/runs/33497477757) | 0.8202 (73/89) |
| 2 | [33530806570](https://github.com/pathlyapp/steerable-framework/actions/runs/33530806570) | 0.7865 (70/89) |
| 3 | [33530856872](https://github.com/pathlyapp/steerable-framework/actions/runs/33530856872) | 0.8202 (73/89) |
| 4 | [33547943349](https://github.com/pathlyapp/steerable-framework/actions/runs/33547943349) | 0.7753 (69/89) |

**Pi + GLM-5.3-Flash = 73%** across three catalog runs, mean **0.7336**, SD **0.0222**:

| Sample | GitHub Actions | Mean |
| ------ | -------------- | ---- |
| 1 | [33593245247](https://github.com/pathlyapp/steerable-framework/actions/runs/33593245247) | 0.7528 (67/89) |
| 2 | [33712341301](https://github.com/pathlyapp/steerable-framework/actions/runs/33712341301) | 0.7093 (61/86) |
| 3 | [33712363232](https://github.com/pathlyapp/steerable-framework/actions/runs/33712363232) | 0.7386 (65/88) |

Samples 2 and 3 exclude three and one tasks respectively because the 360-minute GitHub Actions job timeout cancelled their shards before those trials produced results. Missing infrastructure results are excluded from each denominator rather than scored as Pi failures.

cheap-12 (12 ids) is a weekly smoke. Catalog 89 is the number we quote.

### Public agent + model pairs

Vendor-submitted Terminal-Bench 2.1 scores from the [Snorkel / tbench.ai board](https://snorkel.ai/leaderboard/terminal-bench-2-1/) (archived 2.1 leaderboard). These are different harnesses, timeouts, and models — they show which band 80% sits in, not a controlled A/B.

| Agent | Model | TB 2.1 |
| ----- | ----- | ------ |
| **Steerable** | **GLM-5.3-Flash** | **80%** |
| Claude Code | Claude 5 Fable | 83.8% ±1.2 |
| Codex CLI | GPT-5.5 | 83.1% ±1.1 |
| Terminus 2 | Claude 5 Fable | 80.4% ±1.2 |
| Claude Code | Claude Opus 4.8 | 78.9% ±1.3 |
| Codex CLI | GPT-5.6 Terra | 78.4% ±1.3 |
| Claude Code | Claude Sonnet 5 | 74.6% ±1.6 |
| Pi | GLM-5.3-Flash | 73% |
| Gemini CLI | Gemini 3.1 Pro | 65.8% ±1.7 |

Same model, different harness: [Z.AI](https://z.ai/blog/glm-5.3-flash) reports GLM-5.3-Flash at **84.3%** inside Claude Code 2.1.207 (`temperature=1.0`, 6-hour timeout). That protocol is not ours (we wrap at 170 minutes). The ~4-point gap is the honest harness difference, not a claim that the two numbers are interchangeable.

The Pi result is our own Harbor run rather than a vendor-submitted leaderboard score. Its model request parameters match the Steerable leg, subject to the protocol differences documented below.

## What runs

| Layer | Trigger | Agents | Tasks |
| ----- | ------- | ------ | ----- |
| L0 | every PR (`uv run pytest`) | none | suite YAML invariants |
| Oracle smoke | PR / push when `evals/**` changes, plus `workflow_dispatch` | Harbor `oracle` (Mean 1.0); product `steerable` canary when a key is set | `oracle-canary` (`fix-git`) |
| L2 weekly | Monday cron + `workflow_dispatch` | `steerable`, `claude-code`, `codex`, `pi`, `pi-glm`, `claude-code-glm`, `codex-glm` | `cheap-12` (1 attempt) |
| L2 failed-prev | `workflow_dispatch` on `Evals weekly` with split `failed-prev` | `steerable` | remaining catalog-89 zeros after run 33369888461 (31 ids, 24 shards) |
| L2 catalog | `workflow_dispatch` on `Evals weekly` with split `catalog` | `steerable`, `pi-glm`, `claude-code-glm`, `codex-glm` | full `catalog` (89 ids, 49 shards) |

L2 is **not** a required merge check. A matrix cell whose API key secret is empty is skipped. The product cell needs `STEERABLE_API_KEY` and `STEERABLE_BASE_URL` (the same OpenAI-compatible gateway used locally). Baseline cells need official Anthropic / OpenAI keys. The workflow fails if every live agent was skipped. Weekly Harbor uses `--n-concurrent 2` (local suite default stays 1). Feishu is best-effort: a webhook failure does not fail the eval. Mean is appended to the GitHub job summary when `GITHUB_STEP_SUMMARY` is set.

DeepSeek Harness is listed in `suite.yaml` as skipped: it has no Harbor `BaseInstalledAgent`. Its own ACP snapshots remain L0 harness-contract tests in that repository. Headless `pnpm dsh --profile headless` is not this gate.

## Agents

Harbor first-party names: `oracle`, `claude-code`, `codex`, `pi`. Product agent: `steerable` (`evals.harbor_steerable:SteerableHarborAgent`), headless CoreLoop with workspace bash/file tools. `pi-glm` (`evals.harbor_pi_glm:PiGlmHarborAgent`) subclasses Harbor's Pi to carry the product model's request parameters.

Pi installs [`@earendil-works/pi-coding-agent`](https://www.npmjs.com/package/@earendil-works/pi-coding-agent) in the trial container (`harbor run -a pi`). Claude Code and Pi default to `anthropic/claude-sonnet-4-5` so cheap-12 compares harness behavior. Codex uses `openai/gpt-5.5`. The product agent defaults to `openai/z-ai/glm-5.3-flash` (OpenRouter GLM-5.3-Flash). Override with `python -m evals.run --model …`.

`pi-glm` is the same Pi install on the product model and gateway. The weekly job hands `OPENROUTER_API_KEY` / `OPENROUTER_BASE_URL` to that cell alone: an unconditional base URL would point the Claude `pi` cell at the product gateway.

It runs through `evals.harbor_pi_glm:PiGlmHarborAgent` so the request matches the steerable leg — 1048576 context, 65536 output, `reasoning_effort: max`, temperature 1.0, Z.AI route pinned. Stock `harbor: pi` leaves Pi's own defaults in place (128000 / 16384 / no reasoning) and scored 18/54 against steerable's 44/54 average on the same tasks; `suite.py` rejects that configuration. See `evals/README.md`.

`claude-code-glm` (`evals.harbor_claude_code_glm:ClaudeCodeGlmHarborAgent`) and `codex-glm` (`evals.harbor_codex_glm:CodexGlmHarborAgent`) put the same model on two more harnesses, so a red task can be attributed to the model rather than to one other harness's limits. Each takes the gateway pair in the variables its own adapter reads — `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` and `OPENAI_BASE_URL` / `OPENAI_API_KEY` — and only on its own cell, because an unconditional gateway key in either would send our credential to Anthropic or OpenAI. `suite.py` requires the subclass for all three legs: the stock adapters report their baseline's agent name, leaving a `result.json` unable to say which of the two cells sharing that harness produced it.

These two legs speak protocols the product agent does not. Claude Code sends Anthropic Messages requests to `/v1/messages` and Codex sends Responses API requests to `/v1/responses`, while `steerable` and `pi-glm` use chat completions, so a gateway that serves only chat completions fails both on the first request. Parity is partial by construction: effort is `max` on all four legs, but Codex accepts no output-token ceiling in `config.toml`, so the 65536 cap the others carry has no counterpart there.

This aligns model request parameters, not the full evaluation protocol. Pi and Steerable retain their own prompts, tools, loop behavior, and timeout handling; Steerable has also been tuned through repeated Terminal-Bench runs while Pi uses its default harness behavior. The result compares those configured systems, not harness quality in isolation. The 65536 output cap is also consequential: at least five Pi failures consumed approximately the entire cap on their first request without making a tool call.

## cheap-12

Twelve Terminal-Bench 2.1 ids that avoid QEMU, GPU, video, and long compiles. They must stay a subset of the 89-id catalog (enforced in `evals/tests`).

`fix-git`, `openssl-selfsigned-cert`, `sqlite-db-truncate`, `nginx-request-logging`, `configure-git-webserver`, `sanitize-git-repo`, `polyglot-c-py`, `log-summary-date-ranges`, `filter-js-from-html`, `password-recovery`, `git-multibranch`, `sqlite-with-gcov`.

A product cheap-12 at `n_concurrent: 1` is a multi-hour job (local glm-5.3-flash, Mean 0.750: 2h06m). `filter-js-from-html` alone can take ~30 minutes. The weekly GHA job timeout is 240 minutes; `--n-concurrent 2` is the GHA override. Harbor prints `harbor progress: done/started` every minute so a long run is not mistaken for a hang.

The full 89-id catalog is `Evals weekly` → `workflow_dispatch` → split `catalog` (never on a pull request). It splits the suite into 49 shards (`--shard N --shards 49`), each with a 360-minute timeout. Feishu merges shard `result.json` files into one Mean. QEMU, Windows 3.11, video, and long compiles live only in this split.

Harness iteration uses split `failed-prev` (31 ids, 24 shards) so a headless change is not gated on rerunning all 89. Forty-eight catalog shards plus a 180-minute packing floor keep a 170-minute agent wrap inside the GitHub-hosted 360-minute job cap (at most two catalog tasks per shard, two concurrent). Catalog and failed-prev use `--agent-timeout-multiplier 12` so a 900s Terminal-Bench task gets 180 minutes (Harbor would otherwise kill it at 45 minutes, before wrap). cheap-12 stays at ×3 so the weekly smoke fits 240 minutes. That Mean is not the score of record; catalog 89 is.

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
| `pi-glm` | reuses `STEERABLE_API_KEY` / `STEERABLE_BASE_URL`, forwarded as `OPENROUTER_API_KEY` / `OPENROUTER_BASE_URL` to that cell only |
| `claude-code-glm` | the same pair, forwarded as `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` to that cell only |
| `codex-glm` | the same pair, forwarded as `OPENAI_API_KEY` / `OPENAI_BASE_URL` to that cell only |
| `codex` | `OPENAI_API_KEY` or `CODEX_API_KEY` (official; optional, cell skips) |
| `oracle` | none |
| Feishu 结果通知 | `FEISHU_BOT_WEBHOOK`（自定义机器人 webhook，标题为「成功」或「失败」） |

## Out of scope

- Homemade prompt YAML as the merge gate
- Coder Eval skill/CLI A/B as the primary gate
- LLM-as-judge
- DSH live Terminal-Bench until a Harbor adapter exists
