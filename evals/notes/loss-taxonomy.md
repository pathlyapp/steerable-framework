# Loss taxonomy — 20 flaky + 9 stable red at `27d521a`

Sources: `EVALS_TODO.md` (2.5.9–2.5.17), `evals/suite.yaml` four-run
pass counts, `docs/evals.md`, catalogs 33497477757 (73/89), 33530806570
(70/89), and 33547943349 (69/89), and the closed flaky A/B 33951133679
(20 paired; no separation). Claude Code GLM transcripts for the 83.1% run
are not in this repo. Recipe: `evals/README.md`.

The four buckets are failure *mechanisms*. A task can sit in more than
one. Arm decisions follow the mechanism, not the 0/4 label.

## Stable red (0/4)

| Task | Mechanism | Evidence | Arm? |
| ---- | --------- | -------- | ---- |
| `extract-moves-from-video` | Hard timeout / capability | OCR marathon (380-frame tesseract); 1 of 6 hard_timeouts; also spiral-red | No (capability + clock). Not 20a854d |
| `filter-js-from-html` | Runaway text / timeout | Burns the 65536 output cap (pi) or `AgentTimeoutError` ~2h11m (us); XSS assert false when it wraps | Prompt/tool timing (2.5.11), not verify gate. CC-align grep will not fix HTML |
| `gcode-to-text` | Hard timeout / raster dump / OCR miss | Four catalogs, four different `out.txt`s: 33497477757 `[hard_timeout]` / `nseg` dump; 33530806570 `{ragigc0d3_iz_ch4LLenGiNg}` (short OCR miss, raster gate would **accept**); 33530856872 `top left and a b c`; 33547943349 huge labelled XY dump of `.`/`#` rows. 69-run kept inspecting (~3M log lines) and re-ran `python3 parse.py`. Wrap-up named skipped because `out.txt` existed. Instruction matches `_ASKS_SHOWN_TEXT`. The dump is ≫4KB and has ≥8 `_RASTER_LINE` rows; a caption matcher is unnecessary. Pi/CC passed. CC GLM `TFRrkPC` 1.0 with 4.5M input / 78k output, no transcript | **Landed**: shown-text veto no longer stands down; wrap-up shown-text + inspect-block of dump reads **and** of helper rewrite (`python3 parse.py`). Converts dump trials if they rewrite a short string; not the short OCR misses. Does not invent the flag |
| `make-doom-for-mips` | Capability wall | 0/4 both harnesses; soft-timeout then wrap | No |
| `pytorch-model-cli` | Wrong weights/preprocess | 33497477757: 5/6 tests pass; hidden images predict 7 vs 2 etc. No timeout | Not verify/livelock; do not prompt-tune |
| `raman-fitting` | Wrong x unit | 33497477757: fitted native-file x (G=6328, 2D=3745) vs Raman cm⁻¹ (1580 / 2670) | Not 20a854d |
| `regex-chess` | Live-lock | Catalog 33497477757 trial `Rbko3vm`: 4 tools then `wrap_up_named_output` at round 2, then **9** `tool_choice=required` pre_steps with **0** tools, then `[hard_timeout]`. spiral-red [33966527336](https://github.com/pathlyapp/steerable-framework/actions/runs/33966527336) 0/3, livelock on all three; after WRITE_NOW the loop stopped requiring and they kept designing | **Wrap-up livelock fired and did not convert.** Do not score this task. Keep-required until wrap-up quality gates pass is on `de45915`; livelock itself stays default-off until 3b |
| `sanitize-git-repo` | Over-rewrote git history | Catalog 33497477757 `B7ux4TG`: secret tests passed; then `git filter-branch` + `gc --prune` left `d6987af… missing`. Oracle only `sed`s the working tree. CC GLM `bTtfUKu` passed all 3 tests. `_SYSTEM` SHA clause was not in that run | **Landed**: `_SYSTEM` + bash gate refusing `filter-branch` / `git-filter-repo` / `gc --prune` unless the instruction requires a rewrite. 69-run also probed `which git-filter-repo` (not a veto). `git-multibranch` 4/4 did not run those commands. Measure on catalog |
| `winning-avg-corewars` | Reasoning spiral / timeout | Catalog 33497477757: `[hard_timeout]` after 12 tools; win-rate assert. spiral-red | Live-lock is the wrap-up half; spiral itself is 2.5.11 |

The Pi-pass / we-0/4 triple is not one mechanism: timeout (`gcode-to-text`),
wrong inference (`pytorch-model-cli`), wrong unit (`raman-fitting`).
`sanitize-git-repo` is the 0/4 with a harness-side fix (keep the pinned SHA).
Do not count the other three toward ≥72/round.

## Flaky (20)

These are the only ids a harness change can win or lose. Four-run counts
in `suite.yaml`. Historical timeout table (one catalog, 16 then-reds)
is older than the four-run split; used only when it names a flaky id.

| Task | 4-run | Closest mechanism | Arm? |
| ---- | ----- | ---------------- | ---- |
| `circuit-fibsqrt` | 1/4 | Gate A/B A 2/2 vs B 2/3. Reminders A/B A 0/3 vs B 2/3. A's 2-tool loss is thousands of thinking lines after two inspects; empty rounds do not increment ReminderHooks `_since_write`. 69-run `FPWBnA5`: 2 inspects, 5 `empty_round`, `[hard_timeout]`; image has `sim.c` but no `/app/sim`. 70-run `ggMSUhL` compiled, saw 104 = N/2, kept designing in chat, `[hard_timeout]`. Hidden tests graded leftover starter. Wrap-up livelock `NyFMSat` fired then thought until timeout (no `tool_choice` after WRITE_NOW) | **Landed**: compile sibling `{bin}.c` once; wrap-up instruction-example + inspect-block. Pre-wrap livelock + keep-required-until-write still default-off (3b). 70 already knew 104 after gcc |
| `make-mips-interpreter` | 1/4 | Gate A/B **both 0/3**. Reminders A/B **A 2/3 vs B 1/3**. B's pass wrote `vm.js` via bash **before** the first reminder. A's two passes: 101-tool bash-only, and 6 `write_file`. Failures still missing `/tmp/frame.bmp` | ReminderHooks did not convert this. Keep default off unless the full 20-pair verdict reverses |
| `path-tracing-reverse` | 1/4 | A/B **both arms 1/3**. A's pass also had `empty_round` ×6; both losses are SSIM after `image.c` compiled | Live-lock will not separate pass from fail here. Near-miss is the 0.995 bar |
| `video-processing` | 1/4 | Gate A/B A 0/3 vs B 2/3. Reminders A/B same 0/3 vs 2/3. A losses: jump-count, TypeError, no airborne phase | Off-by-one / implementation. Not verify or reminders |
| `code-from-image` | 2/4 | Gate A/B both 3/3. Reminders A/B A 0/3 vs B 2/3. A truncated the hash / left PLACEHOLDER. Official instruction is "starts with `bee26a`" into `/app/output.txt` — `_ASKS_SHOWN_TEXT` never matches, so the old stub retry was a no-op on catalog. 69-run wrote exactly `bee26a` after `[soft_timeout]`. 70-run wrote a 64-hex digest that does **not** start with the prefix (`39ad5ff9…`) then `[hard_timeout]` — completion retry never ran | **Landed**: stub/prefix veto (no stand-down) **and** wrap-up prefix + inspect-block of the named `.txt` (image reads still allowed). Tools stay until the prefix matches. Shared left-tail miss |
| `dna-assembly` | 2/4 | A/B A 1/3 vs B 0/3. Gate never fired. Losses are Tm/timeout, not verify | Timeout + named Tm bar. Do not add a Tm clause |
| `extract-elf` | 2/4 | Gate A/B (old copy): 1/3 vs 3/3. Reminders A/B (copy + gate on both): 1/3 vs 2/3. Losses still `Only found 66.67% of expected values (required: 75%)`. One B pass had reminder then `write_file`; the extra B pass `fBAfvCP` had 0 reminders | Copy fix landed. Remaining miss is ELF coverage |
| `install-windows-3.11` | 2/4 | A/B A 1/2 vs B 2/3. A's third trial wrote a run summary then executor hang; GHA cancelled. Gate never fired | VM visual-feedback miss. Not the verify gate |
| `model-extraction-relu-logits` | 2/4 | A/B A 2/3 vs B 1/2. B's third trial wrote a run summary then executor hang; GHA cancelled. Gate never fired | Spiral. Live-lock and ReminderHooks never see a tool. Do not re-enable stream cuts |
| `mteb-retrieve` | 2/4 | Gate A/B: 1/3 both arms. Livelock A/B so far A 3/3 vs B 1/3 (B wrote HumanEval title instead of MTEB; 210k–522k tokens, 0 livelock fires) | Wrong retrieval, not the gate or livelock |
| `protein-assembly` | 2/4 | 33497477757 pass. 33547943349: wrong fusion order. Gate A/B both 1/3. Reminders A/B both 1/3; B's losses fired 2 and 5 runaway reminders (one trial never wrote, max_since=39) and still failed | Wrong answer. ReminderHooks does not fix fusion order |
| `bn-fit-modify` | 3/4 | A/B: 2/3 both arms. The A loss is a wrong DAG; gate did not fire | Wrong DAG. Not the verify gate |
| `build-pov-ray` | 3/4 | Catalog 33497477757: binary + SSIM pass; `file_id.diz` missing (wrong/incomplete 2.2 tree). A/B 33951133679 both arms 3/3. Arm A wall 117m vs B 41m: one A trial 115 min; `_file_ready` treated `/app/povray-2.2` as a missing file after the tree existed (versioned dir looks like an extension). Not `STEERABLE_DELIVERY_VERIFY` — `unverified_output` did not fire | Do **not** add a POV-Ray `_SYSTEM` clause. Directory `_file_ready` (`53d5402`) was not in 33951133679; it is on HEAD for later arms |
| `chess-best-move` | 3/4 | Catalog: 3/4. Gate A/B both 2/3. Reminders A/B 0/3 vs 2/3. Every loss is `File is wrong`: file has `e2e4` or `g2g4`, test wants both. B fail `4YsgNyX` fired 3 reminders after a write and still wrote one move | Wrong move set, not missing `move.txt` |
| `largest-eigenval` | 3/4 | A/B A 2/3 vs B 3/3. A's pass `t8ynMPy` fired `unverified_output` then passed. A's loss is `[hard_timeout]` at 170 min with **zero** gate fires. 69-run `HFP5x57` is a speed near-miss (`0.000018 > 0.000017`), not a hang | Gate helped one pass. Hang abandon does not convert the 69 trial |
| `modernize-scientific-stack` | 3/4 | A/B: 2/3 gate on, 3/3 gate off. A loss is wrong station mean (−19.8 vs −15.5); gate did not fire | Variance / wrong answer. Not a gate-off win |
| `path-tracing` | 3/4 | Catalog 33497477757 (pre-20a854d): `image.ppm` at 0.963 vs 0.99; `[soft_timeout]` then nine `fitN.py` writes until `[hard_timeout]`. A/B 33951133679 **both arms 3/3**; gate never fired | Catalog fail is not the gate. Persist is on both arms here |
| `sam-cell-seg` | 3/4 | Gate A/B A 3/3 vs B 2/3. Reminders A/B A 3/3 vs B 2/3. B's loss had 1 reminder after 18 writes | Implementation. ReminderHooks did not lift it |
| `torch-tensor-parallelism` | 3/4 | Catalog 33497477757: 20 tools, local test ran; hidden TP `RuntimeError` 48 vs 64 on dim 1. A/B 33951133679 A 3/3 (one trial 135m), B 1/1 (two `VerifierTimeoutError`, skipped by `flaky_score`) | Implementation, not verify. Do not add another tensor clause |
| `train-fasttext` | 3/4 | A/B A 1/3 vs B 2/3. One A loss is `[soft_timeout]` then `[hard_timeout]` at round 56. Gate never fired | Named bar + clock. Persist prompt already demands a visible margin. Not a new clause |

`unverified_output` fired **8 times** in catalog 33369888461, 7/8 then
passed. The 8 task names were not kept; `largest-eigenval` is the one
named. That is why the gate is measured on the whole flaky split, not
on a guessed 8-id list.

## Arm go / no-go

| Arm | Do it? | Why |
| --- | ------ | --- |
| 20a854d verify gate (`STEERABLE_DELIVERY_VERIFY=0` on B) | **No. Keep the gate on** | 20 paired: B better 6, A on 4, tied 10, p=0.7539, CI includes 0. Gate-fire-then-fail is only `extract-elf` (old copy). See `ab-arms.md` |
| ReminderHooks (`STEERABLE_REMINDERS=1`) | **No. Keep off** | GHA [33959644133](https://github.com/pathlyapp/steerable-framework/actions/runs/33959644133). 20 paired, p=0.3438, CI includes 0. Mechanism task `make-mips` A 2/3 vs B 1/3. See `ab-arms.md` |
| Live-lock wrap-up (`STEERABLE_LIVELOCK_EMPTY_STREAK=3`) | **No. Keep off** | Flaky [33966115606](https://github.com/pathlyapp/steerable-framework/actions/runs/33966115606) 19 paired, p=1.0, CI includes 0. 4 B fires, 0 conversion. `regex-chess` 0/3 with a fire on every trial ([33966527336](https://github.com/pathlyapp/steerable-framework/actions/runs/33966527336)). After WRITE_NOW the in-flight SHA stopped requiring |
| Live-lock pre-wrap + keep-required | **In flight** | Same env on B, SHA `de45915`, GHA [33976774219](https://github.com/pathlyapp/steerable-framework/actions/runs/33976774219). Count `empty_round` while `writes == 0`; after WRITE_NOW keep `tool_choice=required` until wrap-up quality gates pass. Mechanism: `circuit-fibsqrt` |
| `validator: self_critique` | Yes | Discipline retry + grounding. `ChainHooks` now prefers `retry` over `narrate`, so wrap-up write-forcing is not stolen. Measure after livelock |
| CC-align prompt (`STEERABLE_PROMPT_CC_ALIGN=1`) | Yes | Persist-if-long + grep/glob. Tool-description facts landed as defaults |
| Keep pinned git SHA (`_SYSTEM` + bash gate) | Landed | `sanitize-git-repo` over-prune. Grading fact, not a flaky A/B. Prompt was absent on the 0/4 run; bash now refuses `filter-branch` / `git-filter-repo` / `gc --prune` unless the instruction requires a rewrite |
| Named `.txt` prefix / stub (`starts with`) | Landed | `code-from-image` official hint. Shared 69+70 miss. Completion veto converts 69 (they finished). 70 `[hard_timeout]` never completed — wrap-up prefix + inspect-block, and wrap-up re-asserts `tool_choice=required` every remaining round while the prefix misses (`post_tool_result` used to clear it; `writes > 0` used to stand down). Product matchers do not hardcode the hash |
| Shown-text raster (no stand-down + wrap-up) | Landed | Completion veto converts a dump only if they complete. 69 `gcode-to-text__9WfWgdj` left a labelled XY dump in `out.txt` (dot/hash rows, ≫4KB) and kept inspecting (~3M log lines) plus `python3 parse.py`. Wrap-up named skipped because the file existed; wrap-up shown-text + inspect-block of dump reads **and** helper rewrite is the timeout path. Does **not** convert short OCR misses (`{ragi…}`, `top left and a b c`). Does not invent the flag |
| Instruction example stdout (`running … should output`) | Landed | Compiles sibling `{bin}.c` when the binary is missing. Wrap-up also runs the check (69 never completed). Does **not** guarantee a correct circuit after they already know 104 (70). Pre-wrap livelock is the write-forcing half |
| Capability-wall 0/4 | No | `make-doom-for-mips`, `extract-moves-from-video`, `regex-chess` as a *score* target |
| Stream cuts / compaction 0.9 / named-output regex / wider timeouts | No | Already falsified |

## Left tail (69/89 and 70/89)

The gate is every catalog run ≥72, not the 80% mean. Feishu pass lists
from the four gather jobs (complete 73/70/73/69 ids, not the truncated
card lines):

| Run | Pass | Flaky misses (11 on 69, 10 on 70) |
| --- | ---- | ---------------------------------- |
| 33497477757 | 73 | `build-pov-ray`, `circuit-fibsqrt`, `dna-assembly`, `extract-elf`, `path-tracing`, `path-tracing-reverse`, `torch-tensor-parallelism` |
| 33530806570 | 70 | `chess-best-move`, `circuit-fibsqrt`, `code-from-image`, `install-windows-3.11`, `make-mips-interpreter`, `model-extraction-relu-logits`, `modernize-scientific-stack`, `mteb-retrieve`, `sam-cell-seg`, `video-processing` |
| 33530856872 | 73 | `bn-fit-modify`, `install-windows-3.11`, `make-mips-interpreter`, `mteb-retrieve`, `path-tracing-reverse`, `protein-assembly`, `video-processing` |
| 33547943349 | 69 | `circuit-fibsqrt`, `code-from-image`, `dna-assembly`, `extract-elf`, `largest-eigenval`, `make-mips-interpreter`, `model-extraction-relu-logits`, `path-tracing-reverse`, `protein-assembly`, `train-fasttext`, `video-processing` |

Nine stable reds fail all four. `build-pov-ray` / `path-tracing` /
`torch-tensor-parallelism` failed a 73-run and passed both left-tail
runs, so directory `_file_ready` does not move the floor.

Failed on **both** 69 and 70, among flaky (the shared floor):
`circuit-fibsqrt`, `code-from-image`, `make-mips-interpreter`,
`model-extraction-relu-logits`, `video-processing`.

Arithmetic if a change converts on every catalog, including the unlucky
ones (order 73 / 70 / 73 / 69):

- `sanitize-git-repo` +1 every run (0/4, SHA missing on 69 and 70) → 74 / 71 / 74 / 70.
- plus `code-from-image` on the two left tails (passed both 73s) → 74 / 72 / 74 / 71. **70 clears if wrap-up prefix converts the `[hard_timeout]` digest; 69 still needs +1.**
- 69's extra +1 is the gcode dump (labelled XY raster) via wrap-up shown-text, **or** circuit via wrap-up example (compile `sim.c`) / pre-wrap livelock. Raster does **not** convert 70's `{ragi…}`. Circuit example at wrap-up converts the 69 timeout; 70 already knew 104. 69 `largest-eigenval` is a speed near-miss, not a hang.

`regex-chess` +1 would clear 70 with sanitize alone and still leave 69 short.
`model-extraction-relu-logits` is a 3–4 tool spiral; hang abandon does not
convert a 170 min trial that never wrote.

Target for ≥72/round: hold 60 stable green. Every-run: sanitize. Left-tail:
code-from-image (prefix/stub, including 70's wrong digest). 69's third
point is wrap-up shown-text on the gcode dump — they may still write a
short OCR miss — or pre-wrap livelock on circuit. Circuit example-check
is not insurance on these two tails. `regex-chess` livelock is extra.
