# Flaky-split paired A/B arms for the TB ≥72/89 push

Judge with `evals/flaky_score.py` (paired sign test + bootstrap 95% CI).
Require p<0.05 and a CI that excludes 0. A mean bump alone is not a win.
GHA gather on this split prints that report into the job summary.

Dispatch (after the knobs are on the branch GHA checks out):

```bash
gh workflow run evals-weekly.yml --ref feat/evals-tb-stable-80 \
  -f split=flaky \
  -f agent=steerable \
  -f arm_b_env="$(printf '%s\n' 'STEERABLE_REMINDERS=1')"
```

Arm A is always the committed defaults. Arm B is `STEERABLE_*` lines only
(the workflow refuses any other prefix).

## Order

1. **20a854d verification gate** — `STEERABLE_DELIVERY_VERIFY=0` on B.
   Persistence prompt stays on both arms (it is `_SYSTEM`, not an env).
   This isolates the post-write gate, which is the piece with a replay
   estimate (+4–5 tasks/run; unchecked trials 0.587 vs 0.768 checked).
   Early paired shards (not a verdict): `extract-elf` 1/3 with the gate vs
   3/3 without; both A losses fired `unverified_output` then re-ran the
   generator. The retry copy no longer names the hidden-test command.
   `build-pov-ray` is 3/3 on both arms; Arm A wall-clocked 117m because
   `_file_ready` never accepted the versioned source directory. That
   existence check is not behind `STEERABLE_DELIVERY_VERIFY`.
2. **ReminderHooks** — `STEERABLE_REMINDERS=1` on B. Default is off so this
   does not confound (1). `runaway_calls=12` is consecutive non-writes,
   including after a write (make-mips 256-bash tail after `vm.js`).
3. **Live-lock wrap-up** — `STEERABLE_LIVELOCK_EMPTY_STREAK=3` on B.
   Default stays 0. Mechanism task is `regex-chess` (stable red).
   **Done. No conversion.** GHA [33966115606](https://github.com/pathlyapp/steerable-framework/actions/runs/33966115606)
   (`b10535e`, wrap-up-only). 19 paired (A-10 Harbor compose death),
   B better 5, A on 5, tied 9, p=1.0, CI includes 0. **4 B fires, 0 A.**
   Spiral-red [33966527336](https://github.com/pathlyapp/steerable-framework/actions/runs/33966527336):
   `regex-chess` 0/3, livelock on all three (`MDpjyso`, `n6Tr9Xr`,
   `xpgsr4W`). After WRITE_NOW the loop stopped sending
   `tool_choice=required`; the model kept designing until
   `[hard_timeout]` (0 tools after the fire). Same on flaky
   `circuit-fibsqrt__NyFMSat`. Ignore [33964691558](https://github.com/pathlyapp/steerable-framework/actions/runs/33964691558) (402).
3b. **Live-lock pre-wrap + keep-required** — same env on B, after the
   next SHA. Detector now also counts pre-wrap `empty_round` while
   `writes == 0`. After WRITE_NOW, keep `tool_choice=required` until
   wrap-up quality gates pass (prefix / raster / example / missing), not
   until `writes > 0` — catalog 70 already had a wrong digest on disk.
   Wrap-up itself re-asserts required every remaining round for those
   gates (default on; `post_tool_result` used to clear `_force_tool`).
   Mechanism task is `circuit-fibsqrt`. Do not skip to self_critique.
   Empty rounds after a write still do not count.
4. **self_critique** — `STEERABLE_HARNESS=evals/harnesses/self_critique.harness.yaml`
   on B. Enables `validator: self_critique` and therefore
   AntiHallucinationHooks (discipline retry + grounding + narrate).
   `ChainHooks(assembled, delivery)` still runs assembled first for
   `pre_step` (compaction before wrap-up appends). `before_completion`
   now prefers `retry` over `narrate`, so an empty wrap-up still hits
   DeliveryHooks `empty_round` / missing-named instead of a no-tools
   summary. Arm 4 therefore measures discipline/grounding, not a stolen
   write-forcing path. Dispatch only after the pre-wrap livelock wave
   frees `evals-flaky-steerable`.
5. **CC-align prompt** — `STEERABLE_PROMPT_CC_ALIGN=1` on B. Extra persist-if-long
   + grep/glob preference. Tool-description fact fixes (`bash` cwd vs
   shell state, `grep` over bash grep) are committed defaults, not this arm.

Kill a losing arm; do not stack losers into the catalog three-run.

## Applying the 20a854d verdict

Done. GHA [33951133679](https://github.com/pathlyapp/steerable-framework/actions/runs/33951133679) is the `4d6e801` tree (persist on both arms; gate on A / off on B). It does **not** include the retry copy (`72e1776`), directory `_file_ready` (`53d5402`), or hard-timeout process abandon (`9221f19`). GHA 180 cancelled A-0, A-6, and B-11 during executor-thread join after the agent had already written `STEERABLE_RUN_SUMMARY`; B-18 failed on two `VerifierTimeoutError`. `flaky_score` skips `reward is None`, so those hanging trials are absent from the denominators.

20 paired tasks: B better on 6, A on 4, tied on 10. Sign test p = 0.7539. Mean per-task change +0.0667, 95% CI [-0.0500, +0.1917]. **No separation.** `unverified_output` fired three times, all on A: two `extract-elf` losses (old copy re-ran `node extract.js > out.json`) and one `largest-eigenval` **pass**. Every other B-better task (`modernize-scientific-stack`, `video-processing`, `install-windows-3.11`, `train-fasttext`, `largest-eigenval`'s miss) had zero gate fires.

**Keep `STEERABLE_DELIVERY_VERIFY` on.** ReminderHooks A/B is done: GHA [33959644133](https://github.com/pathlyapp/steerable-framework/actions/runs/33959644133) (`STEERABLE_REMINDERS=1` on B). **Keep reminders off.**

## ReminderHooks verdict (33959644133)

20 paired. B better on 7, A on 3, tied on 10. Sign test p = 0.3438. Mean +0.1333, 95% CI [-0.0000, +0.2833]. **No separation.** Wiring: 0 reminder fires on A, 85 on B. No incomplete trials.

Mechanism readout went the **wrong way**: `make-mips-interpreter` A 2/3 vs B 1/3. B's only pass (`BxJrmY2`) wrote `/app/vm.js` via `cat >` **before** the first `reminder` hook; the five later fires are bash-as-non-write noise on an already-writing trial. A's two passes had zero reminders (one 101-tool bash-only, one 6 `write_file`). `path-tracing-reverse` tied 1/3.

B-better pairs are mostly wrong-answer variance, not missing files:

- `chess-best-move` 0/3 vs 2/3 — every loss is `File is wrong` (`e2e4` XOR `g2g4`, test wants both). Reminders after a write still left one move.
- `code-from-image` 0/3 vs 2/3 — A truncated the hash / left a placeholder. One B pass (`7ruvgwK`) did `reminder` then `write_file`; the other passed via bash with 0 `write_file`. Prior gate A/B was 3/3 both arms.
- `circuit-fibsqrt` 0/3 vs 2/3 — wrong circuit outputs. A's 2-tool loss is ~5k lines of thinking after two inspects; `_since_write` only increments on `post_tool_result`, so empty rounds never fire runaway.
- `video-processing` 0/3 vs 2/3 — A losses are jump-count / TypeError / no airborne phase, not absent files.
- `extract-elf` 1/3 vs 2/3 — losses still `66.67% vs 75%`. One B pass had `reminder` then `write_file`; the other had 0 reminders.
- `largest-eigenval` 2/3 vs 3/3 — A's miss never wrote (23 tools). One B pass had 1 reminder and 1 write.
- `path-tracing` 2/3 vs 3/3 — two of three B passes had 0 reminders.

A-better: `make-mips` 2/3 vs 1/3, `model-extraction-relu-logits` 1/3 vs 0/3 (3–4 tools, never hit 12), `sam-cell-seg` 3/3 vs 2/3 (B's loss had 1 reminder after 18 writes). `train-fasttext` tied 0/3; reminders did not lift the 0.62 accuracy bar.

**Keep `STEERABLE_REMINDERS` off.** Do not retune `runaway_calls` and rerun this arm.

`code-from-image` PLACEHOLDER / truncated hash on 69 and 70 is not a reminder miss: the official hint is `starts with \`bee26a\`` into `/app/output.txt`, and `_ASKS_SHOWN_TEXT` never matches it. Delivery now vetoes a stub `.txt` and a missing stated prefix (not a flaky A/B).

## Wrap-up livelock verdict (33966115606)

Done. `b10535e`, `STEERABLE_LIVELOCK_EMPTY_STREAK=3` on B. A-10
(`largest-eigenval`) Harbor compose death; 19 paired. B better on 5, A
on 5, tied on 9. Sign test p = 1.0. Mean −0.0175, 95% CI [−0.1404,
+0.1053]. **No separation.** 4 livelock fires on B, 0 on A:

- `circuit-fibsqrt__NyFMSat` reward 0, fire then thought until
  `[hard_timeout]` (0 `tool_choice` after WRITE_NOW).
- `model-extraction-relu-logits` `AAnpb7S` / `VTqZpVN` both reward 0.
- `largest-eigenval__SnCAAg4` reward 1.0, fire at round 25 after 31
  tools — already delivered.

B's circuit 2/3 vs A 1/3 is not the firing trial. Spiral-red
[33966527336](https://github.com/pathlyapp/steerable-framework/actions/runs/33966527336)
`regex-chess` 0/3 with a livelock fire on every trial; after WRITE_NOW
the model said it would write `/app/re.json` and kept designing.
Shard 2 (`winning-avg-corewars`) compose-died. **Keep
`STEERABLE_LIVELOCK_EMPTY_STREAK` off** until 3b measures pre-wrap plus
keep-required-until-write. Do not rerun A-10 / spiral-2 for this
verdict. Ignore [33964691558](https://github.com/pathlyapp/steerable-framework/actions/runs/33964691558) (402).

## Already falsified (do not rerun)

Stream cuts (default off), compaction 0.8→0.9 (p=0.623), named-output
regex (no-op on 89), widening timeouts (ours are already the widest),
wrap-up-only livelock (33966115606, p=1.0; regex-chess 0/3).
