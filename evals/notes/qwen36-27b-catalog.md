# Steerable × Qwen3.6-27B catalog-89

GHA [35081136413](https://github.com/pathlyapp/steerable-framework/actions/runs/35081136413). SHA `735aa7a`. Model `openai/qwen/qwen3.6-27b` @ `medium`, OpenRouter pin `alibaba`. Harbor catalog-89, n=1, timeout / error / missing = fail.

**54/89 = 60.7%.** 256.6 M input + 5.3 M output + 0 cache. Alibaba list $0.45 / $2.70 per 1M → **$129.67 / run, $2.40 / solved**. Public write-up: [`docs/evals.md`](../../docs/evals.md) (Qwen3.6-27B · n=1 @medium). Not mixed with the Qwen3.8-27B Flash matrix (61/89 = 68.5%).

Shard 4/49 GHA-failed: `qemu-alpine-ssh` agent exit 143 (`NonZeroAgentExitCodeError`, SIGTERM). Counted as fail. Shard 8 retried `mailman` (two Harbor job dirs; last attempt passed; token total includes both).

## Passed (54)

`bn-fit-modify`, `build-cython-ext`, `build-pmars`, `caffe-cifar-10`, `cancel-async-tasks`, `cobol-modernization`, `code-from-image`, `compile-compcert`, `configure-git-webserver`, `constraints-scheduling`, `count-dataset-tokens`, `crack-7z-hash`, `custom-memory-heap-crash`, `distribution-search`, `extract-elf`, `feal-linear-cryptanalysis`, `financial-document-processor`, `fix-code-vulnerability`, `fix-git`, `fix-ocaml-gc`, `git-leak-recovery`, `git-multibranch`, `headless-terminal`, `hf-model-inference`, `kv-store-grpc`, `large-scale-text-editing`, `log-summary-date-ranges`, `mailman`, `mcmc-sampling-stan`, `merge-diff-arc-agi-task`, `modernize-scientific-stack`, `multi-source-data-merger`, `nginx-request-logging`, `openssl-selfsigned-cert`, `overfull-hbox`, `polyglot-c-py`, `polyglot-rust-c`, `portfolio-optimization`, `prove-plus-comm`, `pypi-server`, `pytorch-model-cli`, `pytorch-model-recovery`, `qemu-startup`, `query-optimize`, `regex-log`, `reshard-c4-data`, `rstan-to-pystan`, `sam-cell-seg`, `sanitize-git-repo`, `sparql-university`, `sqlite-db-truncate`, `tune-mjcf`, `vulnerable-secret`, `write-compressor`

## Failed (35)

`adaptive-rejection-sampler`, `break-filter-js-from-html`, `build-pov-ray`, `chess-best-move`, `circuit-fibsqrt`, `db-wal-recovery`, `dna-assembly`, `dna-insert`, `extract-moves-from-video`, `feal-differential-cryptanalysis`, `filter-js-from-html`, `gcode-to-text`, `gpt2-codegolf`, `install-windows-3.11`, `largest-eigenval`, `llm-inference-batching-scheduler`, `make-doom-for-mips`, `make-mips-interpreter`, `model-extraction-relu-logits`, `mteb-leaderboard`, `mteb-retrieve`, `password-recovery`, `path-tracing`, `path-tracing-reverse`, `protein-assembly`, `qemu-alpine-ssh`, `raman-fitting`, `regex-chess`, `schemelike-metacircular-eval`, `sqlite-with-gcov`, `torch-pipeline-parallelism`, `torch-tensor-parallelism`, `train-fasttext`, `video-processing`, `winning-avg-corewars`
