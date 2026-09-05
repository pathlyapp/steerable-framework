# Claude Code vs Steerable — request surface (static)

Extracted from `@anthropic-ai/claude-code-darwin-arm64@2.1.261` (native
`package/claude` binary). Strings are minified Bun-bundled fragments, not a
single contiguous prompt file. This note paraphrases for internal comparison;
it does not copy the vendor prompt into a product path.

Steerable side: `packages/sidecar/py/src/steerable_sidecar/headless.py`
(`_SYSTEM`) and `workspace_tools.py` `router.register(..., description=...)`.

The 83.1% same-model catalog run used `--agent claude-code-glm` (Harbor
adapter on `ci/evals-glm-harnesses`, CLI 2.1.259). Weekly GHA `claude-code`
is official Anthropic Sonnet, a different cell. Recipe: `evals/README.md`.

## Identity and persistence

| Clause | Claude Code 2.1.261 | Steerable `_SYSTEM` |
| ------ | ------------------- | -------------------- |
| Role | Interactive CLI for software engineering; act immediately, minimise interruptions | Coding agent in a Linux workspace; do not wait for confirmation |
| Planning | Prefers action over planning; do not enter plan mode unless asked | Keep going until resolved; do not stop at analysis, a plan, or a partial fix |
| Persistence | Do not stop because the context or session is long; end the turn only when the task is complete or blocked on user input | Keep going until resolved; persevere on command failure |
| Verify-before-finish | Not a top-level section in the extracted identity string | Unconditional `# Verify before you finish` (20a854d) |
| What hidden tests score | Absent (Claude Code is not TB-tuned) | `# What is scored` — files on disk, not chat |
| Long-running commands | Bash timeout notes exist as fragments | `# Long-running commands` — 1h bash wait, no short `timeout N` |
| Domain notes | General product CLI | TB-specific (VM, images, XSS HTML, embeddings, …) |

The persistence gap that is still ours to test is the “do not stop because
the session is long” sentence. The rest of our prompt is TB-specific and
should not be replaced by Claude Code’s product identity.

## Tools

| Tool | Claude Code (extracted blurb) | Steerable |
| ---- | ------------------------------- | --------- |
| Shell | `Bash`: cwd persists between commands; shell state (env, aliases) does not; profile-initialized | `bash`: “Run a shell command in the workspace directory.” Persistent state is a separate `bash_session` |
| Read | Absolute `file_path`; default line cap; images supported | `read_file`: UTF-8 text; binary images get an ASCII preview (called out in `_SYSTEM`) |
| Edit | Exact string replacement; preserve indent after Read line-number prefixes | `edit_file`: exact → whitespace-tolerant → Unicode-normalised; prefer over whole-file rewrite |
| Write | Overwrite if the file exists | `write_file`: create parents |
| Grep | Dedicated ripgrep tool; never `grep`/`rg` via Bash | `grep`: “Prefer this over bash grep -r” |
| Glob | Pattern match, sorted by mtime | `glob`: “Prefer this over bash find/ls” |
| Multi-file edit | not extracted as `apply_patch` | `apply_patch` (atomic) |
| Todo / subagent / notebook / web | `TodoWrite`, Task/Agent, `NotebookEdit`, WebSearch fragments | Subagent only on the `subagent` harness; web tools off in Harbor (`--no-web-tools`) |

## What to change (and what not to)

Worth an A/B (`STEERABLE_PROMPT_CC_ALIGN=1`):

1. Persist-if-long: we already demand completion, but not “don’t stop because the session is long”.
2. Prefer `grep`/`glob` in the system prompt, not only in the tool blurb.

Land as facts, not as an A/B (descriptions were incomplete, not a hypothesis):

- `bash`: cwd persists; exported variables do not — that is `bash_session`.
- `grep`: call this instead of `bash grep`/`rg`.

Do not copy Claude Code’s identity paragraph, tool schemas, or safety
text into `_SYSTEM`. Do not grow `_SYSTEM` past the 7000-character test.

## Wire capture (path, not TLS MITM)

Harbor writes `ANTHROPIC_BASE_URL` into the trial container. Point Claude
Code at the http scheme of the provider host and route through
`steerable-egress-proxy --inject-host … --record-requests PATH`. Same tasks
on our side: `STEERABLE_REQUEST_RECORD_PATH`. Compare JSONL rounds:
`system`, `tools`, message assembly, compaction.
