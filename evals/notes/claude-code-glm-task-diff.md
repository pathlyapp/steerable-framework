# Claude Code GLM vs Steerable — per-task set (historical)

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
hash, not the `bee26a` prefix). Native image input is the remaining CC
Read gap — GLM-5.3-Flash is multimodal; our `read_file` still ASCII-previews
unless `STEERABLE_READ_IMAGES=1`. `gcode-to-text` stayed in a long session
instead of writing the raster into `/app/out.txt` — that is the
shown-text veto, not a prompt transplant. `circuit-fibsqrt` spent 207k
output tokens on the circuit; our 69-run thought until `[hard_timeout]`
after two inspects (starter `N/2` still on disk). The example check now
compiles `sim.c` at wrap-up; that is a harness gate, not a prompt transplant.

## They passed, we failed (9)

| Task | Our 4-run layer | Note |
| ---- | --------------- | ---- |
| `gcode-to-text` | stable red | Harness loss vs Pi as well |
| `pytorch-model-cli` | stable red | Harness loss vs Pi |
| `raman-fitting` | stable red | Harness loss vs Pi |
| `sanitize-git-repo` | stable red | Not a GLM wall — CC passed |
| `winning-avg-corewars` | stable red / spiral-red | CC passed; our spiral |
| `build-pov-ray` | flaky 3/4 | 20a854d / persist candidate |
| `torch-tensor-parallelism` | flaky 3/4 | 20a854d candidate |
| `path-tracing` | flaky 3/4 | Timeout historically |
| `circuit-fibsqrt` | flaky 1/4 | Hard kill historically |

## We passed, they failed (8)

`bn-fit-modify`, `caffe-cifar-10`, `chess-best-move`,
`financial-document-processor`, `install-windows-3.11`, `mailman`,
`mteb-retrieve`, `pypi-server`.

## Both failed (7)

`dna-assembly`, `extract-elf`, `extract-moves-from-video`,
`filter-js-from-html`, `make-doom-for-mips`, `path-tracing-reverse`,
`regex-chess`.

`regex-chess` / `filter-js-from-html` / `make-doom-for-mips` /
`extract-moves-from-video` stay capability or runaway. Do not spend
catalog budget on those four as score targets.

Recompute with `python -m evals.task_diff` after the next paired jobs.
