# Evals

Public **capability** evals for coding agents, not Steerable unit tests and not homemade prompts.

The gate is [Terminal-Bench 2.1](https://github.com/harbor-framework/terminal-bench-2-1) through [Harbor](https://www.harborframework.com/docs/run-jobs/run-evals). Task ids, the Harbor dataset name, and the git SHA used to enumerate the catalog live in `evals/suite.yaml`. Scoring is the tasks' hidden pytest; there is no LLM judge.

## Score of record

**Steerable + GLM-5.3-Flash = 81.7%** on the 89-task catalog. Three independent full runs at commit `8e260de`, mean **0.8165**, SD **0.0425**. We report the mean, not the 0.8652 high-water mark (run 3 is the top of the distribution). The every-round ≥72/89 gate was attempted on this stack and **missed**: rounds posted 70, 71, 77 — the floor is the flaky layer's coin tosses, not a crash or a systematic hole (zero infrastructure failures across all three runs; see Pending measurement for the full arm history). `8e260de` includes everything the old `27d521a` score predates: the persistence prompt and post-write verify gate (`20a854d`), the left-tail stack (`4746d14`: git-history rewrite gate, named-output prefix/stub vetoes, wrap-up instruction-example), and the EOL-apt bring-up fix that converted two deterministic setup crashes (`qemu-alpine-ssh`, `qemu-startup`) into passes.

| Sample | GitHub Actions | Mean |
| ------ | -------------- | ---- |
| 1 | [34031313764](https://github.com/pathlyapp/steerable-framework/actions/runs/34031313764) | 0.7865 (70/89) |
| 2 | [34031319806](https://github.com/pathlyapp/steerable-framework/actions/runs/34031319806) | 0.7978 (71/89) |
| 3 | [34040053173](https://github.com/pathlyapp/steerable-framework/actions/runs/34040053173) | 0.8652 (77/89) |

**Cost is a co-equal metric, not a footnote.** The score of record is two numbers, not one: pass rate **81.7%** and cost per solved task **~$0.138** (mean 258.3M input tokens/run, ~$10.00/run across the same three `8e260de` runs; per-trial `result.json` telemetry, not the ±15% OpenRouter analytics panel). Every future catalog run updates both numbers together — a pass-rate move that hides a token move is half the information. For calibration on the same model and protocol: Pi solves at **$0.061**/task (138.9M tokens, 73.4%), Claude Code at **$0.162**/task (299.2M tokens, 83.1%); we spend 2.3× Pi's tokens to buy 8.3 points.

Superseded: four runs at `27d521a` posted mean 0.8006 (73/70/73/69; GHA [33497477757](https://github.com/pathlyapp/steerable-framework/actions/runs/33497477757), [33530806570](https://github.com/pathlyapp/steerable-framework/actions/runs/33530806570), [33530856872](https://github.com/pathlyapp/steerable-framework/actions/runs/33530856872), [33547943349](https://github.com/pathlyapp/steerable-framework/actions/runs/33547943349)). A fifth run on the pre-`8e260de` tree ([34019591179](https://github.com/pathlyapp/steerable-framework/actions/runs/34019591179)) is excluded: OpenRouter credit exhaustion mid-run (HTTP 402) truncated 45 of 89 trials, and its 36/89 measures the wallet, not the harness.

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
| **Steerable** | **GLM-5.3-Flash** | **81.7%** |
| Claude Code | Claude 5 Fable | 83.8% ±1.2 |
| Codex CLI | GPT-5.5 | 83.1% ±1.1 |
| Terminus 2 | Claude 5 Fable | 80.4% ±1.2 |
| Claude Code | Claude Opus 4.8 | 78.9% ±1.3 |
| Codex CLI | GPT-5.6 Terra | 78.4% ±1.3 |
| Claude Code | Claude Sonnet 5 | 74.6% ±1.6 |
| Pi | GLM-5.3-Flash | 73% |
| Gemini CLI | Gemini 3.1 Pro | 65.8% ±1.7 |

Same model, different harness — measured on this repo's protocol, not vendor pages. All four rows run `z-ai/glm-5.3-flash` through the same OpenRouter account on the Harbor catalog-89 protocol:

| Harness | TB 2.1 | Runs | Input tokens / run | Cost / run | Cost / solved task |
| ------- | ------ | ---- | ------------------ | ---------- | ------------------ |
| Claude Code | 83.1% (74/89) | 1 | 299.2 M | $11.96 | $0.162 |
| **Steerable** | **81.7% ±4.3** | 3 | 258.3 M | ~$10.00 | ~$0.138 |
| Pi | 73.4% ±2.2 | 3 | 138.9 M | ~$3.90 | $0.061 |
| Codex CLI | 58.4% (52/89) | 1+fill-in | 888.5 M | $42.32 | $0.814 |

Codex CLI completed the full 89-task catalog after a fill-in run for the 7 tasks cut by job timeouts (5 passed). Its score is still protocol-depressed, not a clean model reading: OpenRouter's Responses API translation layer rejected long-conversation payloads (15,522 zero-token failed requests across both runs), and errored trials score 0 without the model being wrong. Steerable's per-run tokens are exact per-trial `result.json` telemetry (the `8e260de` runs postdate telemetry; the superseded `27d521a` rows used OpenRouter analytics at ±15%). For reference, [Z.AI](https://z.ai/blog/glm-5.3-flash) reports GLM-5.3-Flash at **84.3%** inside Claude Code 2.1.207 (`temperature=1.0`, 6-hour timeout) — consistent with our own 83% Claude Code measurement on the 170-minute protocol.

The Pi result is our own Harbor run rather than a vendor-submitted leaderboard score. Its model request parameters match the Steerable leg, subject to the protocol differences documented below.

## Task stratification at `8e260de`

Three catalog runs at that commit (GHA [34031313764](https://github.com/pathlyapp/steerable-framework/actions/runs/34031313764), [34031319806](https://github.com/pathlyapp/steerable-framework/actions/runs/34031319806), [34040053173](https://github.com/pathlyapp/steerable-framework/actions/runs/34040053173)) split the 89 ids as:

- **65 stable green** (3/3) — includes `qemu-alpine-ssh` and `qemu-startup`, converted from deterministic setup crashes by the EOL-apt fix
- **9 stable red** (0/3): `extract-moves-from-video`, `filter-js-from-html`, `gcode-to-text`, `make-doom-for-mips`, `protein-assembly`, `pytorch-model-cli`, `regex-chess`, `video-processing`, `winning-avg-corewars`
- **15 flaky** (listed under `splits.flaky` in `evals/suite.yaml`, with x/3 comments): 7×1/3, 8×2/3

Expected passes = 65 + 7.67 = 72.67 (81.65%). The 70/71/77 spread is the flaky layer's coin tosses (3 to 12 of 15 flaky tasks pass per run), so the floor misses 72 while the mean clears it. Vs the `27d521a` table: `raman-fitting` and `sanitize-git-repo` left the stable-red set (the git-history rewrite gate works — its run-2 failure was wrong replacement text, not over-pruning); `protein-assembly` and `video-processing` fell in. `flaky` / `spiral-red` were rebuilt from this three-run table (flaky is now 15 ids, `loss-29` is now `loss-24`). Rebuild them only after a new multi-run catalog, not from a single dispatch. Mechanism labels for the 24 losses: `evals/notes/loss-taxonomy.md`.

Claude Code on the same model and protocol: **83.1% (74/89), 1 run**. Catalog shards: GHA [33798916303](https://github.com/pathlyapp/steerable-framework/actions/runs/33798916303); fill-in [33833495592](https://github.com/pathlyapp/steerable-framework/actions/runs/33833495592). Recipe: `evals/README.md` (`--agent claude-code-glm`, CLI 2.1.259). Per-task set vs steerable sample 1: `evals/notes/claude-code-glm-task-diff.md`.

## Pending measurement

None. The three-run gate on `8e260de` was attempted and **missed** (70/71/77 — one of three rounds ≥72/89), and the score of record moved to that mean anyway: every harness arm queued against the gap was falsified (history below), so the floor is documented as flaky-layer variance rather than chased with stacked losers. Future catalog three-runs track the mean; a run is an alarm only below the `27d521a` low-water mark of 69/89.

Arm history on the road to `8e260de`. Flaky A/B of the `20a854d` verify gate is done: GHA [33951133679](https://github.com/pathlyapp/steerable-framework/actions/runs/33951133679) (`STEERABLE_DELIVERY_VERIFY=0` on B). 20 paired tasks, p=0.7539, CI includes 0 — **no separation; keep the gate on**. The only gate-fire-then-fail is `extract-elf` (old retry copy, already fixed on HEAD). ReminderHooks A/B is done: GHA [33959644133](https://github.com/pathlyapp/steerable-framework/actions/runs/33959644133) (`STEERABLE_REMINDERS=1` on B). 20 paired, p=0.3438, CI includes 0; `make-mips-interpreter` A 2/3 vs B 1/3 — **keep reminders off**. Wrap-up livelock A/B is done: GHA [33966115606](https://github.com/pathlyapp/steerable-framework/actions/runs/33966115606) (`STEERABLE_LIVELOCK_EMPTY_STREAK=3` on B). 19 paired, p=1.0, CI includes 0; 4 B fires, 0 conversion — **keep livelock off**. Spiral-red [33966527336](https://github.com/pathlyapp/steerable-framework/actions/runs/33966527336): `regex-chess` 0/3 with a fire on every trial. Ignore [33964691558](https://github.com/pathlyapp/steerable-framework/actions/runs/33964691558) (402). Left-tail stack is `de45915` plus helper-script git rewrite, wrap-up generator writes, and git-leak prune allow (`4746d14`): Harbor env-start retry, git-history bash gate (command **or** invoked helper; allow `rewriting history` / recover-then-purge so `git-leak-recovery` can still `gc --prune`), named `.txt` stub/prefix/raster, wrap-up shown-text + inspect-block of dump reads and helper rewrite, wrap-up prefix/stub, wrap-up re-asserts `tool_choice=required` while those gates fail, wrap-up instruction-example (compile sibling `.c`; `cat > gen.py` and `python3 gen.py` that writes the named file still run), pre-wrap livelock detector (default still 0), `ChainHooks` retry-over-narrate. Arm 3b is done: GHA [33976774219](https://github.com/pathlyapp/steerable-framework/actions/runs/33976774219) (`STEERABLE_LIVELOCK_EMPTY_STREAK=3` on B, SHA `de45915`). 20 paired, p=1.0000, CI includes 0; mechanism `circuit-fibsqrt` A 3/3 vs B 2/3 with 7 B fires and 6 reward-0 — **keep livelock off**. Native image-read A/B is done: GHA [33985962466](https://github.com/pathlyapp/steerable-framework/actions/runs/33985962466) (`STEERABLE_READ_IMAGES=1` on B, SHA `ea81b77`). 20 paired, p=0.0225, CI excludes 0, A wins (mean −0.20); `code-from-image` tied 3/3 — **keep ASCII**. self_critique A/B is done: GHA [34003963766](https://github.com/pathlyapp/steerable-framework/actions/runs/34003963766) (`STEERABLE_HARNESS=evals/harnesses/self_critique.harness.yaml` on B, SHA `7ad0b26` with the TB instruction wired as `user_question`). 20 paired, p=1.0000, CI includes 0 (A 39/60 vs B 40/60); both 3h-cancelled shards were arm B — **keep self_critique off**. CC-align prompt A/B is done: GHA [34011962039](https://github.com/pathlyapp/steerable-framework/actions/runs/34011962039) (`STEERABLE_PROMPT_CC_ALIGN=1` on B, SHA `81f682d`). 20 paired, p=0.5078, CI includes 0; arm A swung 39/60→30/60 between runs on identical default code — **keep CC-align off**. Reasoning-effort A/B is done: spiral-red GHA [34040196148](https://github.com/pathlyapp/steerable-framework/actions/runs/34040196148) (`STEERABLE_REASONING_EFFORT=high`, SHA `8e260de`) went 0/9 — no unlock; flaky paired GHA [34040202706](https://github.com/pathlyapp/steerable-framework/actions/runs/34040202706), p=0.4240, CI includes 0, mean −0.05 — **keep `max`**. The flaky arm queue is empty. Remaining arms: `evals/notes/ab-arms.md`.

## What runs

| Layer | Trigger | Agents | Tasks |
| ----- | ------- | ------ | ----- |
| L0 | every PR (`uv run pytest`) | none | suite YAML invariants |
| Oracle smoke | PR / push when `evals/**` changes, plus `workflow_dispatch` | Harbor `oracle` (Mean 1.0); product `steerable` canary when a key is set | `oracle-canary` (`fix-git`) |
| L2 weekly | Monday cron + `workflow_dispatch` | `steerable`, `claude-code`, `codex`, `pi`, `pi-glm` | `cheap-12` (1 attempt) |
| L2 failed-prev | `workflow_dispatch` on `Evals weekly` with split `failed-prev` | `steerable` | remaining catalog-89 zeros after run 33369888461 (31 ids, 24 shards) |
| L2 catalog | `workflow_dispatch` on `Evals weekly` with split `catalog` | `steerable` | full `catalog` (89 ids, 49 shards) |

L2 is **not** a required merge check. A matrix cell whose API key secret is empty is skipped. The product cell needs `STEERABLE_API_KEY` and `STEERABLE_BASE_URL` (the same OpenAI-compatible gateway used locally). Baseline cells need official Anthropic / OpenAI keys. The workflow fails if every live agent was skipped. Weekly Harbor uses `--n-concurrent 2` (local suite default stays 1). Feishu is best-effort: a webhook failure does not fail the eval. Mean is appended to the GitHub job summary when `GITHUB_STEP_SUMMARY` is set.

DeepSeek Harness is listed in `suite.yaml` as skipped: it has no Harbor `BaseInstalledAgent`. Its own ACP snapshots remain L0 harness-contract tests in that repository. Headless `pnpm dsh --profile headless` is not this gate.

## Agents

Harbor first-party names: `oracle`, `claude-code`, `codex`, `pi`. Product agent: `steerable` (`evals.harbor_steerable:SteerableHarborAgent`), headless CoreLoop with workspace bash/file tools. `pi-glm` (`evals.harbor_pi_glm:PiGlmHarborAgent`) subclasses Harbor's Pi to carry the product model's request parameters.

Pi installs [`@earendil-works/pi-coding-agent`](https://www.npmjs.com/package/@earendil-works/pi-coding-agent) in the trial container (`harbor run -a pi`). Claude Code and Pi default to `anthropic/claude-sonnet-4-5` so cheap-12 compares harness behavior. Codex uses `openai/gpt-5.5`. The product agent defaults to `openai/z-ai/glm-5.3-flash` (OpenRouter GLM-5.3-Flash). Override with `python -m evals.run --model …`.

`pi-glm` is the same Pi install on the product model and gateway. The weekly job hands `OPENROUTER_API_KEY` / `OPENROUTER_BASE_URL` to that cell alone: an unconditional base URL would point the Claude `pi` cell at the product gateway.

It runs through `evals.harbor_pi_glm:PiGlmHarborAgent` so the request matches the steerable leg — 1048576 context, 65536 output, `reasoning_effort: max`, temperature 1.0, Z.AI route pinned. Stock `harbor: pi` leaves Pi's own defaults in place (128000 / 16384 / no reasoning) and scored 18/54 against steerable's 44/54 average on the same tasks; `suite.py` rejects that configuration. See `evals/README.md`.

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
| `codex` | `OPENAI_API_KEY` or `CODEX_API_KEY` (official; optional, cell skips) |
| `oracle` | none |
| Feishu 结果通知 | `FEISHU_BOT_WEBHOOK`（自定义机器人 webhook，标题为「成功」或「失败」） |

## Out of scope

- Homemade prompt YAML as the merge gate
- Coder Eval skill/CLI A/B as the primary gate
- LLM-as-judge
- DSH live Terminal-Bench until a Harbor adapter exists
