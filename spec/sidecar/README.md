# `spec/sidecar/`

Wire protocol for the **steerable-sidecar**: a portable Python runtime spawned by an
Electron/desktop host (e.g. `deeppath-agent`) and addressed via stdio JSON-RPC 2.0.

## Frames

| Schema | Direction | Use |
| --- | --- | --- |
| `SidecarRequest.schema.json` | host → sidecar | Synchronous calls (require id + response). |
| `SidecarResponse.schema.json` | sidecar → host | Reply for a previous request id (success or error). |
| `SidecarNotification.schema.json` | both ways | Fire-and-forget (streaming SSE chunks, log lines, lifecycle). |
| `SidecarError.schema.json` | sidecar → host | Embedded inside `SidecarResponse.error`. |
| `SidecarHealth.schema.json` | sidecar → host | `result` payload for `system.ping`. |

## Reserved methods

```
system.ping                  → SidecarHealth
system.shutdown              → null            (graceful, drain in-flight calls)
system.shutdown_now          → null            (force kill)
agent.session.create         → AgentSession
agent.session.resume         → AgentSession
agent.session.list           → AgentSession[]
agent.turn.start             → { traceId }     followed by stream.chunk notifications
agent.turn.cancel            → null
tool.list                    → ToolCall[]      (descriptors only)
tool.invoke                  → ToolResult
trace.fetch                  → HarnessTrace + spans + events
config.get                   → object
config.set                   → null
```

## Reserved notifications

```
lifecycle.ready              → { version, protocolVersion, pid, listenInfo }
lifecycle.shutdown           → { reason }
stream.chunk                 → SSEEvent
stream.done                  → { traceId, status }
trace.event                  → TraceEvent
log.line                     → { level, message, ts }
```

## Lifecycle handshake

1. Host spawns sidecar with stdin/stdout pipes.
2. Sidecar prints **one line** `__SIDECAR_READY__:<json>` to stderr (matches
   `SidecarHealth`) so the host can detect ready state without waiting for the first
   request, and immediately starts emitting `lifecycle.ready` over stdout JSON-RPC.
3. Host MAY send `system.ping` periodically; missing 3 consecutive pings (default 5s
   interval) MUST trigger a sidecar restart.
4. Host requests `system.shutdown`; sidecar drains, sends `lifecycle.shutdown`, then
   closes stdout.
5. If the sidecar is still alive after the configured grace window, host issues
   `system.shutdown_now` and finally `SIGKILL`.

## Framing

Each JSON-RPC frame is a **single line of UTF-8 JSON** terminated by `\n`. Frames MUST
NOT contain unescaped newlines. This keeps the transport line-delimited and easy to
multiplex with stderr-side log streaming.
