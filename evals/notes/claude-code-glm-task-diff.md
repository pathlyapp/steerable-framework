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

Gateway request logs are not in the artifacts (only `result.json`). Round-by-round
prompt/tool capture still needs `--record-requests` / `STEERABLE_REQUEST_RECORD_PATH`
on a new trial. Recipe: `evals/README.md`.

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
