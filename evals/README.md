# Coding-agent evals

Pinned **Terminal-Bench 2.1** tasks, run through [Harbor](https://www.harborframework.com/docs/run-jobs/run-evals). The suite file is `suite.yaml`. Homemade prompts are not a gate.

Docs: [docs/evals.md](../docs/evals.md).

## Agents

| Agent | Harbor `-a` | Default model | Keys |
| ----- | ------------ | ------------- | ---- |
| `oracle` | `oracle` | none | none |
| `claude-code` | `claude-code` | `anthropic/claude-sonnet-4-5` | `ANTHROPIC_API_KEY` |
| `codex` | `codex` | `openai/gpt-5.5` | `OPENAI_API_KEY` or `CODEX_API_KEY` |
| `pi` | `pi` | `anthropic/claude-sonnet-4-5` | `ANTHROPIC_API_KEY` |
| `dsh` | — | — | skipped (no Harbor adapter) |

Pi is Harbor's first-party installed agent (`-a pi`), which installs [`@earendil-works/pi-coding-agent`](https://www.npmjs.com/package/@earendil-works/pi-coding-agent) inside the trial container. Do not use a third-party Harbor import path.

Claude Code and Pi share a model so the cheap-12 job compares harnesses. Codex uses its usual pairing.

## Commands

```bash
uv tool install harbor   # Docker required for anything except --dry-run

python -m evals.run --agent oracle --split cheap-12 --dry-run
python -m evals.run --agent oracle --split oracle-canary
python -m evals.run --agent pi --split cheap-12
python -m evals.run --agent claude-code --split cheap-12
python -m evals.run --agent codex --split cheap-12 --tasks fix-git
```

`--split cheap-12` is the live weekly gate (12 ids). `--split catalog` is all 89 and is not a CI job.

## Layers

| Layer | When | What |
| ----- | ---- | ---- |
| L0 | every PR | `evals/tests` via `uv run pytest` (no Harbor, no Docker) |
| Oracle smoke | PR when `evals/**` changes | Harbor `-a oracle` on `fix-git` |
| L2 weekly | schedule + `workflow_dispatch` | cheap-12 × `claude-code` / `codex` / `pi`. Not a required merge check. |

Job outputs land in `evals/jobs/` (gitignored).
