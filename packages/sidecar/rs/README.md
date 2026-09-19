# steerable-sidecar (Rust)

Newline-delimited JSON-RPC 2.0 over stdio. Speaks the same method catalog as
`python -m steerable_sidecar`. The host opts in with `STEERABLE_RUST_SIDECAR=1`
and `STEERABLE_RUST_SIDECAR_BIN=/path/to/steerable-sidecar`; if the binary is
missing the supervisor falls back to Python.

`steerable-sidecar sandbox profile`, `sandbox linux-wrap`, and `sandbox landlock`
replace `python -m steerable_sidecar.sandbox` when the Rust flag is on. The
supervisor wraps the Rust binary in Seatbelt / bwrap / Landlock using that CLI.

```sh
cargo test --manifest-path packages/sidecar/rs/Cargo.toml
STEERABLE_SIDECAR_FAKE_LLM=1 cargo run --manifest-path packages/sidecar/rs/Cargo.toml
cargo run --manifest-path packages/sidecar/rs/Cargo.toml -- sandbox profile --no-network
```
