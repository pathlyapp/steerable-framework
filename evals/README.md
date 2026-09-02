# Coding-agent evals

Pinned **Terminal-Bench 2.1** tasks, run through [Harbor](https://www.harborframework.com/docs/run-jobs/run-evals). The suite file is `suite.yaml`. Homemade prompts are not a gate.

Docs: [docs/evals.md](../docs/evals.md). Work order (TB then SWE-bench Verified): [EVALS_TODO.md](../EVALS_TODO.md).

## Agents

| Agent | Harbor `-a` | Default model | Keys |
| ----- | ------------ | ------------- | ---- |
| `oracle` | `oracle` | none | none |
| `steerable` | `evals.harbor_steerable:SteerableHarborAgent` | `openai/z-ai/glm-5.3-flash` | GHA: `STEERABLE_API_KEY` + `STEERABLE_BASE_URL`. Local also accepts `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` |
| `claude-code` | `claude-code` | `anthropic/claude-sonnet-4-5` | `ANTHROPIC_API_KEY` |
| `codex` | `codex` | `openai/gpt-5.5` | `OPENAI_API_KEY` or `CODEX_API_KEY` |
| `pi` | `pi` | `anthropic/claude-sonnet-4-5` | `ANTHROPIC_API_KEY` |
| `pi-glm` | `evals.harbor_pi_glm:PiGlmHarborAgent` | `openrouter/z-ai/glm-5.3-flash` | `OPENROUTER_API_KEY` (+ `OPENROUTER_BASE_URL` for a non-OpenRouter gateway) |
| `dsh` | — | — | skipped (no Harbor adapter) |

`steerable` is the product agent: headless CoreLoop with in-process `bash` / `read_file` / `write_file` jailed to the trial cwd. It is not Electron and not Harbor's first-party CLI agents.

Pi is Harbor's first-party installed agent (`-a pi`), which installs [`@earendil-works/pi-coding-agent`](https://www.npmjs.com/package/@earendil-works/pi-coding-agent) inside the trial container. Do not use a third-party Harbor import path.

Claude Code and Pi share a model so the cheap-12 job compares harnesses. Codex defaults to `openai/gpt-5.5`. The product agent defaults to OpenRouter `z-ai/glm-5.3-flash` (`--model openai/z-ai/glm-5.3-flash`); pass `--model openai/gpt-5.5` to match Codex's tier.

`pi-glm` is Harbor's Pi install pointed at the product model and the product gateway, so the harness is the only difference from a `steerable` run. When `OPENROUTER_BASE_URL` is set, Harbor writes a `models.json` naming a custom provider at that endpoint, and `model_api` (declared in `suite.yaml`) tells it the endpoint speaks OpenAI chat completions; the `pi` baseline must stay free of that kwarg, because Harbor rejects `model_api` when no base URL is configured.

It runs through `evals.harbor_pi_glm:PiGlmHarborAgent`, not stock `pi`, because Harbor writes that model entry as `{"id": …}` and lets Pi default everything else. Those defaults describe a model Pi ships metadata for, not GLM-5.3: 128000 context against GLM's 1048576, 16384 output against the steerable leg's 65536, and `reasoning` false, which makes Pi clamp `--thinking` to `off` and send no effort at all. The subclass restores the window, the output cap, `reasoning_effort: max`, temperature 1.0, and the Z.AI route pin, so the two legs issue the same request and differ only in harness.

Catalog run 33587641909 is why: stock `pi` scored 18/54 where steerable averages 44/54 on the same tasks, and one `pi.txt` logged `"reasoning": 16314` against the 16384 cap on a trial that then made no tool call. A score from stock `pi` on GLM measures Pi's defaults, not Pi's harness.

## Commands

```bash
uv tool install harbor==0.22.0   # Docker required for anything except --dry-run

python -m evals.run --agent oracle --split cheap-12 --dry-run
python -m evals.run --agent oracle --split oracle-canary --require-mean 1.0
python -m evals.run --agent steerable --split oracle-canary
python -m evals.run --agent steerable --split cheap-12
python -m evals.run --agent pi --split cheap-12
python -m evals.run --agent claude-code --split cheap-12
python -m evals.run --agent codex --split cheap-12 --tasks fix-git
```

`--split cheap-12` is the live weekly gate (12 ids). `--split failed-prev` reruns remaining catalog-89 zeros (31 ids, 24 shards) for harness iteration. `--split catalog` is all 89; GitHub Actions runs it via `Evals weekly` `workflow_dispatch` with split `catalog` (49 shards).

## Layers

| Layer | When | What |
| ----- | ---- | ---- |
| L0 | every PR | `evals/tests` via `uv run pytest` (no Harbor, no Docker) |
| Oracle smoke | PR when `evals/**` changes | Harbor `-a oracle` on `fix-git` (Mean 1.0). Product canary (`steerable` × `fix-git`) when `STEERABLE_API_KEY` is set |
| L2 weekly | schedule + `workflow_dispatch` | cheap-12 × `steerable` (gateway) / `claude-code` / `codex` / `pi` (official keys, skip if unset). Not a required merge check. |

Job outputs land in `evals/jobs/` (gitignored). GitHub Actions posts a Feishu card when `FEISHU_BOT_WEBHOOK` is set; the card title starts with 成功 or 失败. Weekly GHA passes `--n-concurrent 2`. Feishu posting is best-effort.
