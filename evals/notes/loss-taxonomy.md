# Loss taxonomy — 20 flaky + 9 stable red at `27d521a`

Sources: `EVALS_TODO.md` (2.5.9–2.5.17), `evals/suite.yaml` four-run
pass counts, `docs/evals.md`. No new catalog logs were invented here.
Gateway-side Claude Code GLM transcripts for the 83.1% run are not in
this repo (no GHA run id). Rerun recipe: `evals/README.md`.

The four buckets are failure *mechanisms*. A task can sit in more than
one. Arm decisions follow the mechanism, not the 0/4 label.

## Stable red (0/4)

| Task | Mechanism | Evidence | Arm? |
| ---- | --------- | -------- | ---- |
| `extract-moves-from-video` | Hard timeout / capability | OCR marathon (380-frame tesseract); 1 of 6 hard_timeouts; also spiral-red | No (capability + clock). Not 20a854d |
| `filter-js-from-html` | Runaway text / timeout | Burns the 65536 output cap (pi) or `AgentTimeoutError` ~2h11m (us); XSS assert false when it wraps | Prompt/tool timing (2.5.11), not verify gate. CC-align grep will not fix HTML |
| `gcode-to-text` | Harness loss + hard kill | Pi passed; **CC GLM passed**; we 0/4 | Live-lock/wrap-up maybe; 2.5.16 still needed |
| `make-doom-for-mips` | Capability wall | 0/4 both harnesses; soft-timeout then wrap | No |
| `pytorch-model-cli` | Harness loss | Pi passed, we 0/4; early wrong answer, no timeout | 2.5.16 trajectory; not verify/livelock |
| `raman-fitting` | Harness loss | Pi passed; we fitted x0=19196 vs 1580; early wrong | 2.5.16; not 20a854d |
| `regex-chess` | Live-lock | `tool_choice=required` ~62% compliance; 16 empty wrap-up retries ate the window; spiral-red | **Live-lock (2.5.10)** — mechanism, do not score the arm on this one task |
| `sanitize-git-repo` | Hard task, not GLM wall | Pi passed once; **Claude Code GLM passed**; steerable 0/4. Early wrong, no timeout | **Harness loss vs CC.** Reminders/runaway may touch it; 20a854d is the first measurement |
| `winning-avg-corewars` | Reasoning spiral / timeout | spiral-red; hard kill historically | Live-lock is the wrap-up half; spiral itself is 2.5.11 |

Harness-loss three (`gcode-to-text`, `pytorch-model-cli`, `raman-fitting`)
are the only 0/4 ids where the same model passed under Pi. Plan does not
count on flipping them for ≥72/round.

## Flaky (20)

These are the only ids a harness change can win or lose. Four-run counts
in `suite.yaml`. Historical timeout table (one catalog, 16 then-reds)
is older than the four-run split; used only when it names a flaky id.

| Task | 4-run | Closest mechanism | Arm? |
| ---- | ----- | ---------------- | ---- |
| `circuit-fibsqrt` | 1/4 | Hard kill (10201s) historically | Live-lock if wrap-up empty; otherwise spiral |
| `make-mips-interpreter` | 1/4 | — | Flaky A/B only |
| `path-tracing-reverse` | 1/4 | Soft timeout then wrap | Live-lock |
| `video-processing` | 1/4 | — | Flaky A/B only |
| `code-from-image` | 2/4 | — | Flaky A/B only |
| `dna-assembly` | 2/4 | Hard kill historically | Live-lock / timeout |
| `extract-elf` | 2/4 | Early wrong, no timeout | **20a854d** candidate |
| `install-windows-3.11` | 2/4 | Wrong answer historically | Flaky A/B; VM domain notes already in `_SYSTEM` |
| `model-extraction-relu-logits` | 2/4 | 180 min timeout on pi-glm | Timeout, not verify |
| `mteb-retrieve` | 2/4 | Wrong answer historically | Flaky A/B |
| `protein-assembly` | 2/4 | Wrong answer historically | Flaky A/B |
| `bn-fit-modify` | 3/4 | — | **Primary 20a854d / persist target** |
| `build-pov-ray` | 3/4 | Early wrong, no timeout | **20a854d** candidate |
| `chess-best-move` | 3/4 | — | **Primary 20a854d / persist target** |
| `largest-eigenval` | 3/4 | `unverified_output` fired then passed (n=1) | **Delivery gate** — already in 20a854d |
| `modernize-scientific-stack` | 3/4 | — | **Primary 20a854d / persist target** |
| `path-tracing` | 3/4 | Hard kill historically | Live-lock |
| `sam-cell-seg` | 3/4 | — | **Primary 20a854d / persist target** |
| `torch-tensor-parallelism` | 3/4 | Early wrong | **20a854d** candidate |
| `train-fasttext` | 3/4 | — | **Primary 20a854d / persist target** |

`unverified_output` fired **8 times** in catalog 33369888461, 7/8 then
passed. The 8 task names were not kept; `largest-eigenval` is the one
named. That is why the gate is measured on the whole flaky split, not
on a guessed 8-id list.

## Arm go / no-go

| Arm | Do it? | Why |
| --- | ------ | --- |
| 20a854d verify gate (`STEERABLE_DELIVERY_VERIFY=0` on B) | **Yes, first** | Only change with a replay estimate. Target: 3/4 flaky + unverified_output |
| ReminderHooks (`STEERABLE_REMINDERS=1`) | Yes, after (1) | `error_streak_ratio=0.5` and `runaway_calls=12` match recorded modes; unwired until this branch |
| Live-lock (`STEERABLE_LIVELOCK_EMPTY_STREAK=3`) | Yes | 2.5.10; 16 empty wrap-up retries. Judge on flaky paired test, not `regex-chess` alone |
| `validator: self_critique` | Yes | Narrate third state is dead while `validator: null`. Empty wrap-up is the 16-retry family |
| CC-align prompt (`STEERABLE_PROMPT_CC_ALIGN=1`) | Yes | Persist-if-long + grep/glob. Tool-description facts landed as defaults |
| Capability-wall 0/4 | No | `make-doom-for-mips`, `extract-moves-from-video`, `regex-chess` as a *score* target |
| Stream cuts / compaction 0.9 / named-output regex / wider timeouts | No | Already falsified |

Target for ≥72/round: hold 60 stable green and lift flaky contribution
from ~11.25 to ≥12, mainly the nine 3/4 tasks toward ~0.95.
