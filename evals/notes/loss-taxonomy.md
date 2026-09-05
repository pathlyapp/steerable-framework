# Loss taxonomy — 20 flaky + 9 stable red at `27d521a`

Sources: `EVALS_TODO.md` (2.5.9–2.5.17), `evals/suite.yaml` four-run
pass counts, `docs/evals.md`, catalogs 33497477757 (73/89), 33530806570
(70/89), and 33547943349 (69/89), and the in-flight flaky A/B 33951133679
(partial). Claude Code GLM transcripts for the 83.1% run are not in this
repo. Recipe: `evals/README.md`.

The four buckets are failure *mechanisms*. A task can sit in more than
one. Arm decisions follow the mechanism, not the 0/4 label.

## Stable red (0/4)

| Task | Mechanism | Evidence | Arm? |
| ---- | --------- | -------- | ---- |
| `extract-moves-from-video` | Hard timeout / capability | OCR marathon (380-frame tesseract); 1 of 6 hard_timeouts; also spiral-red | No (capability + clock). Not 20a854d |
| `filter-js-from-html` | Runaway text / timeout | Burns the 65536 output cap (pi) or `AgentTimeoutError` ~2h11m (us); XSS assert false when it wraps | Prompt/tool timing (2.5.11), not verify gate. CC-align grep will not fix HTML |
| `gcode-to-text` | Hard timeout | Catalog 33497477757: `[hard_timeout]` after 47 tools; wrote `nseg 26` dump not `flag{…}`. Pi/CC passed | Live-lock/wrap-up; not 20a854d |
| `make-doom-for-mips` | Capability wall | 0/4 both harnesses; soft-timeout then wrap | No |
| `pytorch-model-cli` | Wrong weights/preprocess | 33497477757: 5/6 tests pass; hidden images predict 7 vs 2 etc. No timeout | Not verify/livelock; do not prompt-tune |
| `raman-fitting` | Wrong x unit | 33497477757: fitted native-file x (G=6328, 2D=3745) vs Raman cm⁻¹ (1580 / 2670) | Not 20a854d |
| `regex-chess` | Live-lock | `tool_choice=required` ~62% compliance; 16 empty wrap-up retries ate the window; spiral-red | **Live-lock (2.5.10)** — mechanism, do not score the arm on this one task |
| `sanitize-git-repo` | Over-rewrote git history | 33497477757: secret tests passed; `test_no_other_files_changed` needs pinned SHA `d6987af…` and `commit.diff(None)`. Agent `filter-branch` + `gc --prune` deleted it. Oracle only `sed`s the working tree | **Landed in `_SYSTEM`**. Not an A/B |
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
| `circuit-fibsqrt` | 1/4 | Catalog 33497477757: 2 tools then 1.3 MB reasoning, `[hard_timeout]`. Not empty wrap-up | Spiral. Live-lock empty-streak will not see this |
| `make-mips-interpreter` | 1/4 | 33497477757 pass. 33547943349: 256 bash tools, never `write_file`, `/tmp/frame.bmp` missing. Instruction never names that path (only `vm.js`). Named-output retry never ran because they never completed. | ReminderHooks runaway (arm 2), not a named-output regex |
| `path-tracing-reverse` | 1/4 | 33497477757: 0.464 vs 0.995, `empty_round` then `[soft_timeout]`, kept disassembling. 33547943349: 0.957 vs 0.995 after they `cmp`'d images; `empty_round` ×2 + `named_gzip_cap` | Live-lock is the empty half. The near-miss is a named `> 0.995` bar they already checked |
| `video-processing` | 1/4 | 33497477757 pass. 33547943349: landing frame 61 vs inclusive `[62, 64]`; `jump_analyzer.py` also crashed | Off-by-one on a named range. Not the verify gate |
| `code-from-image` | 2/4 | Flaky A/B B 3/3 (gate off; A still running) | Flaky A/B only |
| `dna-assembly` | 2/4 | 33497477757: primers written; forward Tm 73.64 > 72; `[soft_timeout]` then `[hard_timeout]` while iterating. Primer length note already in `_SYSTEM` | Timeout + named Tm bar. Do not add a Tm clause |
| `extract-elf` | 2/4 | A/B 33951133679: gate on 1/3, gate off 3/3. Both A losses fired `unverified_output` then re-ran `node extract.js > out.json` | **20a854d** — retry copy no longer names that command (`72e1776`). Do not call the arm until the full 20-task pair |
| `install-windows-3.11` | 2/4 | 33497477757 and 33547943349 pass. 33530806570: 103 tools, `instruction_listen` fired, keyboard test: no key caused ≥10% image difference | VM visual-feedback miss. Domain notes already cover daemonize/screenshot. Not a new clause |
| `model-extraction-relu-logits` | 2/4 | 33497477757 pass. 33547943349: 0 tools, 1.4 MB round-0 reasoning, `[hard_timeout]`, `stolen_A1.npy` never written | Spiral on the first stream. Live-lock empty-streak and ReminderHooks never see a tool. Do not re-enable stream cuts |
| `mteb-retrieve` | 2/4 | A/B: 1/3 both arms; gate never fired | Wrong answer, not the gate |
| `protein-assembly` | 2/4 | 33497477757 pass. 33547943349: wrong fusion order (flag-donor-dhfr-acceptor-snap) | Wrong answer. Order is in the instruction |
| `bn-fit-modify` | 3/4 | A/B: 2/3 both arms. The A loss is a wrong DAG; gate did not fire | Not shown as a gate win on the partial sample |
| `build-pov-ray` | 3/4 | Catalog 33497477757: binary + SSIM pass; `file_id.diz` missing (wrong/incomplete 2.2 tree). A/B B (gate off) 3/3 | Do **not** add a POV-Ray `_SYSTEM` clause. Instruction already names `/app/povray-2.2` |
| `chess-best-move` | 3/4 | 33497477757 pass. 33530806570: wrote `g2g4` to `/app/move.txt`; "File is wrong". One `empty_round`. Gate did not fire | Wrong move. Not a scoring-fact hole |
| `largest-eigenval` | 3/4 | Catalog 33497477757: `unverified_output` at round 53 then passed | **Delivery gate** — already in 20a854d |
| `modernize-scientific-stack` | 3/4 | A/B: 2/3 gate on, 3/3 gate off. A loss is wrong station mean (−19.8 vs −15.5); gate did not fire | Partial sample leans B; not a verdict |
| `path-tracing` | 3/4 | Catalog 33497477757 (pre-20a854d): `image.ppm` at 0.963 vs 0.99; `[soft_timeout]` then nine `fitN.py` writes until `[hard_timeout]`. A/B 33951133679 Arm A (gate on) 3/3, gate never fired; Arm B still running | Catalog fail is not the gate. ReminderHooks consecutive-non-write will not fire on the fit loop (they were writing) |
| `sam-cell-seg` | 3/4 | 33497477757 pass. 33530806570: 252 tools (235 bash / 16 edit), polyline scored as a rectangle, IoU 0.44 | Implementation. ReminderHooks bash-as-non-write would nudge; not the verify gate |
| `torch-tensor-parallelism` | 3/4 | Catalog 33497477757: 20 tools, local test ran; hidden TP `RuntimeError` 48 vs 64 on dim 1. Domain note already in `_SYSTEM` | Implementation, not verify. Do not add another tensor clause |
| `train-fasttext` | 3/4 | 33497477757 pass. 33547943349: local acc 0.6153 vs 0.62, they were retuning, `[hard_timeout]`. Hidden test 0.617 | Named bar + clock. Persist prompt already demands a visible margin. Not a new clause |

`unverified_output` fired **8 times** in catalog 33369888461, 7/8 then
passed. The 8 task names were not kept; `largest-eigenval` is the one
named. That is why the gate is measured on the whole flaky split, not
on a guessed 8-id list.

## Arm go / no-go

| Arm | Do it? | Why |
| --- | ------ | --- |
| 20a854d verify gate (`STEERABLE_DELIVERY_VERIFY=0` on B) | **Yes, first** | Only change with a replay estimate. Partial A/B (4 paired tasks): B better on 2, A on 0, p=0.50, **not a verdict**. `unverified_output` has fired only on the two Arm A `extract-elf` losses so far |
| ReminderHooks (`STEERABLE_REMINDERS=1`) | Yes, after (1) | `error_streak_ratio=0.5` and `runaway_calls=12` consecutive non-writes, including after a write. make-mips 256-bash tail is this mode |
| Live-lock (`STEERABLE_LIVELOCK_EMPTY_STREAK=3`) | Yes | 2.5.10; 16 empty wrap-up retries. Judge on flaky paired test, not `regex-chess` alone |
| `validator: self_critique` | Yes | Narrate third state is dead while `validator: null`. Empty wrap-up is the 16-retry family |
| CC-align prompt (`STEERABLE_PROMPT_CC_ALIGN=1`) | Yes | Persist-if-long + grep/glob. Tool-description facts landed as defaults |
| Keep pinned git SHA (`_SYSTEM`) | Landed | `sanitize-git-repo` over-prune. Grading fact, not a flaky A/B |
| Capability-wall 0/4 | No | `make-doom-for-mips`, `extract-moves-from-video`, `regex-chess` as a *score* target |
| Stream cuts / compaction 0.9 / named-output regex / wider timeouts | No | Already falsified |

Target for ≥72/round: hold 60 stable green and lift flaky contribution
from ~11.25 to ≥12, mainly the nine 3/4 tasks toward ~0.95.
