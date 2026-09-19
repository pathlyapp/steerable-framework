# CoreLoop Rust migration test catalog

Machine-readable source: [`coreloop-rust-test-catalog.json`](coreloop-rust-test-catalog.json).
Gates are executable: framework pytest and deeppath-agent vitest fail if a listed **existing** path is missing, if the PyO3 import surface drifts, or if a product `pythonRuntime` does not match the catalog.

**P0** blocks the matching plan stage or product ship. **P1** is required before deleting Python CoreLoop. Replay fixtures (`test_replay_crosslang`) are not a loop gate.

## Why this catalog exists

Rust CoreLoop must keep three consumers working without a product rewrite:

1. **deeppath-api** in-process via PyO3 (`async for event in loop.run(...)`).
2. **Desktop sidecar** over the current JSON-RPC stdio methods and sessions db write lease.
3. **Three frontend agents** — aroli, ciflog, etown — each with a different CPython policy.

## P0 by plan stage

| Stage | P0 evidence | Not a substitute |
| --- | --- | --- |
| API surface | `test_pyo3_api_surface.py` | A larger `__all__` dump |
| Loop / RPC / events | same + `test_rpc_method_contract.py` | Spec prose without a test |
| Rust loop MVP | `rs/tests/test_loop.rs` plus `test_loop.py` / `test_golden.py` / `test_harness.py` / cancel / steer | Cross-language replay reducer |
| LLM providers | `rs/tests/test_llm_wire.rs` plus `test_llm_*_wire.py`, presets, model_resolve | Golden chat text |
| Sidecar binary | `sidecar/rs/tests/stdio_ping.rs` + `test_file_edit.rs` + `test_skills.rs` + `test_write_lease.rs` plus `test_sidecar_coreloop.py`, `test_sidecar_methods.py`, write-lease | Health ping only |
| Egress + sandbox | `sidecar/rs/tests/test_sandbox.rs` + `test_landlock.rs` + `egress-proxy/rs/tests/test_proxy.rs` + `test_forward.rs` + `test_control.rs` + `supervisor-sandbox.test.ts` + agent `sandbox-posture` / `egress-widening` e2e | Packing without a Python runtime |
| Built-in tools | `rs/tests/test_todo.rs` + `test_web.rs` + `test_run_code.rs` + `sidecar/rs/tests/test_run_code.rs` + `test_ptc_js.rs` + `sidecar/rs/tests/test_ptc_js.rs` + `test_tool_contract.py` + web/run_code/ptc e2e | Dual-impl without expanding `tool_contract.json` |
| Per-product CPython | `tests/python-runtime.test.ts` | Skipping sidecar for aroli before Rust egress/sandbox |
| PyO3 wheel | `test_pyo3_api_surface.py` (`run_turn` on `steerable_agent_runtime_native`) | Python CoreLoop imports without the native module |
| Harbor | [evals.md](../evals.md) 80.7% catalog | A single cheap-12 smoke |

## P0 frontend agents

| Product | `pythonRuntime` (target) | Shipping CPython until `STEERABLE_RUST_SIDECAR=1` | P0 command |
| --- | --- | --- | --- |
| **aroli** | `none` | still bundled | `pnpm test` + `tests/ui/product-smoke.spec.ts` |
| **ciflog** | `bundle` | bundled | `pnpm test:cflog-e2e` + product-smoke with `APP_FLAVOR=ciflog` |
| **etown** | `bundle` | bundled | `tests/etown/*` + product-smoke with `APP_FLAVOR=etown` |

Aroli `none` is the **target**. Packing still prepares `python-runtime` until the Rust sidecar flag is on, because today's loop, egress proxy, and sandbox still need CPython.

Three-product regression from deeppath-agent:

```bash
pnpm test:products
```

## Dual-track rule

Python CoreLoop features freeze once the Rust MVP is the behavior source. New loop behavior lands in the P0 scripted-provider tests first. `web_search` / `web_fetch` stay single-implementation until the Rust copy exists; then they must enter `tool_contract.json`.
