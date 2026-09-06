# Claude Code GLM vs Steerable — per-task set

Current comparison lives in **Recomputed on the `8e260de` three-run**
below. The `27d521a` single-sample sets are kept under it as history.

Claude Code GLM catalog: GHA
[33798916303](https://github.com/pathlyapp/steerable-framework/actions/runs/33798916303)
(49 shards on `ci/evals-glm-harnesses`) plus fill-in
[33833495592](https://github.com/pathlyapp/steerable-framework/actions/runs/33833495592)
(`train-fasttext`). Combined **74/89**.

Steerable comparison: GHA
[33497477757](https://github.com/pathlyapp/steerable-framework/actions/runs/33497477757)
at `27d521a` (**73/89**, one of the four-run high-water marks). One sample
on our side, so ids here that we pass 3/4 can still show as "we failed".

Gateway request logs are not in the artifacts (only `result.json` +
verifier stdout). Round-by-round prompt/tool capture still needs
`--record-requests` / `STEERABLE_REQUEST_RECORD_PATH` on a new trial.
Recipe: `evals/README.md`.

Token counts from those `result.json` files (CC GLM 33798916303):

| Task | reward | input | output | cache |
| ---- | ------ | ----- | ------ | ----- |
| `sanitize-git-repo` `bTtfUKu` | 1.0 | 586k | 7.6k | 486k |
| `gcode-to-text` `TFRrkPC` | 1.0 | 4.5M | 78k | 3.7M |
| `circuit-fibsqrt` `cY8ynUc` | 1.0 | 2.0M | 207k | 1.6M |
| `code-from-image` `2nAsayC` | 1.0 | 53k | 600 | 45k |

There is no CC tool trace to copy. Sanitize was a short pass (working-tree
edit, not a history rewrite). `code-from-image` was a short pass (full
hash, not the `bee26a` prefix). Native image input was the remaining CC
Read gap; flaky [33985962466](https://github.com/pathlyapp/steerable-framework/actions/runs/33985962466)
killed `STEERABLE_READ_IMAGES=1` (A wins, cfi tied 3/3). `gcode-to-text` stayed in a long session
instead of writing the raster into `/app/out.txt` — that is the
shown-text veto, not a prompt transplant. `circuit-fibsqrt` spent 207k
output tokens on the circuit; our 69-run thought until `[hard_timeout]`
after two inspects (starter `N/2` still on disk). The example check now
compiles `sim.c` at wrap-up; that is a harness gate, not a prompt transplant.

## Recomputed on the `8e260de` three-run (70/71/77) vs CC 74/89

Our side is now three samples (34031313764 / 34031319806 / 34040053173);
tiers are x/3. CC side unchanged (33798916303 + fill-in 33833495592).

### They passed, we never pass (0/3) — 5

| Task | Mechanism (ours) | Regression audit |
| ---- | ---------------- | ---------------- |
| `gcode-to-text` | Shown-text dump / short OCR miss; left-tail shown-text stack landed but still 0/3 | CC wrote in one long session (4.5 M in / 78 K out) |
| `protein-assembly` | Wrong fusion order, all 3 runs (`flag - donor - dhfr - acceptor - snap` assert) | Was flaky 2/4 at `27d521a`. Gates fire (delivery nudges, empty_round retries) but no veto touches output — content failure, variance not a left-tail regression |
| `pytorch-model-cli` | Wrong weights/preprocess; 5/6 tests | Same as before |
| `video-processing` | Jump-analyzer count/phase asserts | Was flaky 1/4. Same audit: empty_round retries pushed more work, failures are content. Variance, not regression |
| `winning-avg-corewars` | Spiral-red (0/9 across probes) | CC passed on the same model — capability exists, our loop spirals |

### They passed, we flaky (x/3) — 9

`circuit-fibsqrt` 1/3, `db-wal-recovery` 2/3, `dna-insert` 1/3,
`git-multibranch` 2/3, `largest-eigenval` 2/3, `make-mips-interpreter`
1/3, `model-extraction-relu-logits` 1/3, `raman-fitting` 1/3,
`sanitize-git-repo` 2/3.

### We 3/3, they failed — 5

`bn-fit-modify`, `chess-best-move`, `financial-document-processor`,
`mailman`, `pypi-server`.

### We flaky, they failed — 6

`caffe-cifar-10` 2/3, `dna-assembly` 1/3, `extract-elf` 2/3,
`install-windows-3.11` 2/3, `mteb-retrieve` 2/3,
`path-tracing-reverse` 1/3.

### Both never pass — 4

`extract-moves-from-video`, `filter-js-from-html`, `make-doom-for-mips`,
`regex-chess`. Capability or runaway; do not spend catalog budget on
these four as score targets.

Net: CC leads on 14 ids (5 clean + 9 flaky), we lead on 11 (5 clean +
6 flaky); 74 vs 72.7 mean is a ~1.3-task gap, inside the ±4.3 SD band.

Recompute with `python -m evals.task_diff` after the next paired jobs.
