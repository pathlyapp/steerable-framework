# steerable-agent-runtime (Rust)

Rust port of `packages/agent-runtime/py` `CoreLoop`.

The Python package remains the sidecar/API entry until the PyO3 wheel
(`steerable_agent_runtime._native`) is wired. Behavior gates live in
`tests/test_loop.rs`, ported from `py/tests/test_loop.py`.

```sh
cargo test --manifest-path packages/agent-runtime/rs/Cargo.toml
```

Opt-in PyO3 (Python provider/executor callbacks; Rust owns the loop):

```sh
# from packages/agent-runtime/rs
maturin develop --features python,extension-module
STEERABLE_RUST_CORELOOP=1 pytest packages/agent-runtime/py/tests/test_loop.py
```

LLM wire helpers (OpenAI-compat encode/SSE assemble + Anthropic split) live in
`src/openai_wire.rs` / `src/anthropic_wire.rs`; HTTP streaming is `src/openai_http.rs`.
