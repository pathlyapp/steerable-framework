# Sidecar Roundtrip Example

Spawn `python -m steerable_sidecar` from a parent process, wait for the
`__SIDECAR_READY__:` stderr marker, send a few JSON-RPC frames, and shut
the sidecar down cleanly. This is the **smallest possible** working
embedder — Electron / Tauri / native shells do exactly the same dance.

## Run

From the framework monorepo root:

```bash
uv sync
uv run --package steerable-example-sidecar-roundtrip \
    python -m steerable_example_sidecar_roundtrip.main
```

Expected output:

```
[ready] protocolVersion=0.1.0 version=0.1.0
[notif] lifecycle.ready {'version': '0.1.0', 'protocolVersion': '0.1.0', 'pid': …, 'listenInfo': {'transport': 'stdio'}}
[ping]  {'status': 'ok', 'version': '0.1.0', 'protocolVersion': '0.1.0', 'uptimeMs': …, 'pid': …, ...}
[tools] []
[bye]   None
```

## What's happening

```
parent  ─── spawn ───►  python -m steerable_sidecar
   │                         │ writes "__SIDECAR_READY__:{json}" to stderr
   │  ◄───── stderr ─────────┤
   │                         │
   │  ── system.ping ──────► │
   │  ◄── result ───────────│
   │                         │
   │  ── tool.list ────────► │
   │  ◄── result ───────────│
   │                         │
   │  ── system.shutdown ──► │
   │  ◄── result + exit ────│
```

Read [Sidecar spec](../../docs/spec/sidecar.md) for the full method
catalog (including `agent.chat.stream`, `tool.invoke`, and the
notification stream).
