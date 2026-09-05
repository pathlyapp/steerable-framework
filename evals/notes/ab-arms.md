# Flaky-split paired A/B arms for the TB ≥72/89 push

Judge with `evals/flaky_score.py` (paired sign test + bootstrap 95% CI).
Require p<0.05 and a CI that excludes 0. A mean bump alone is not a win.

Dispatch (after the knobs are on the branch GHA checks out):

```bash
gh workflow run evals-weekly.yml \
  -f split=flaky \
  -f agent=steerable \
  -f arm_b_env="$(cat <<'EOF'
STEERABLE_DELIVERY_VERIFY=0
EOF
)"
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

## Already falsified (do not rerun)

Stream cuts (default off), compaction 0.8→0.9 (p=0.623), named-output
regex (no-op on 89), widening timeouts (ours are already the widest).
