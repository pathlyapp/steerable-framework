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
3. **Live-lock** — `STEERABLE_LIVELOCK_EMPTY_STREAK=3` on B. Default is 0.
   Mechanism fix (EVALS_TODO 2.5.10): consecutive wrap-up
   `tool_choice=required` rounds with no tool call stop forcing.
4. **self_critique** — `STEERABLE_HARNESS=evals/harnesses/self_critique.harness.yaml`
   on B. Enables `validator: self_critique` and therefore the narrate
   third state on empty wrap-up.
5. **CC-align prompt** — `STEERABLE_PROMPT_CC_ALIGN=1` on B. Extra persist-if-long
   + grep/glob preference. Tool-description fact fixes (`bash` cwd vs
   shell state, `grep` over bash grep) are committed defaults, not this arm.

Kill a losing arm; do not stack losers into the catalog three-run.

## Applying the 20a854d verdict

Done. GHA [33951133679](https://github.com/pathlyapp/steerable-framework/actions/runs/33951133679) is the `4d6e801` tree (persist on both arms; gate on A / off on B). It does **not** include the retry copy (`72e1776`), directory `_file_ready` (`53d5402`), or hard-timeout process abandon (`9221f19`). GHA 180 cancelled A-0, A-6, and B-11 during executor-thread join after the agent had already written `STEERABLE_RUN_SUMMARY`; B-18 failed on two `VerifierTimeoutError`. `flaky_score` skips `reward is None`, so those hanging trials are absent from the denominators.

20 paired tasks: B better on 6, A on 4, tied on 10. Sign test p = 0.7539. Mean per-task change +0.0667, 95% CI [-0.0500, +0.1917]. **No separation.** `unverified_output` fired three times, all on A: two `extract-elf` losses (old copy re-ran `node extract.js > out.json`) and one `largest-eigenval` **pass**. Every other B-better task (`modernize-scientific-stack`, `video-processing`, `install-windows-3.11`, `train-fasttext`, `largest-eigenval`'s miss) had zero gate fires.

**Keep `STEERABLE_DELIVERY_VERIFY` on.** ReminderHooks is in flight on HEAD: GHA [33959644133](https://github.com/pathlyapp/steerable-framework/actions/runs/33959644133) (`STEERABLE_REMINDERS=1` on B). Do not default the gate off.

## Already falsified (do not rerun)

Stream cuts (default off), compaction 0.8→0.9 (p=0.623), named-output
regex (no-op on 89), widening timeouts (ours are already the widest).
