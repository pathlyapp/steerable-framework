# steerable-agent-runtime

Tier 3 runtime for the Steerable framework.

Provides four orthogonal pluggable adapters:

- `LLMProvider` — chat-completion / streaming / tool-call abstraction, with
  reference implementations for OpenAI-compatible servers (covers OpenAI,
  Ollama, vLLM, SiliconFlow, etc.) and Anthropic native.
- `ToolRouter` — in-process tool registry. Auto-classifies tools into
  `ToolMode`s using `steerable_agent_harness.policy`, supports per-tool
  permission overrides, and dispatches `ToolCall` → `ToolResult`.
- `StorageAdapter` — persistence interface for `AgentSession`, `ChatMessage`,
  `ChatAgent`, and `HarnessTrace + spans + events`. Reference impls: in-memory
  (default for sidecar/dev) and SQLAlchemy (for hosted backends).
- `TransportAdapter` — wire format. `FastAPISseTransport` exports SSE for
  hosted setups; `StdioJsonRpcTransport` powers the steerable-sidecar.

The runtime is **Python only** by design — frontends never depend on it
directly. Browsers/Electron consume runtime output via either the SSE transport
(over HTTP) or the stdio JSON-RPC transport (sidecar pattern).

## Install

```bash
pip install steerable-agent-runtime[all]
```

Selectively install just the bits you need:

```bash
pip install "steerable-agent-runtime[openai]"
pip install "steerable-agent-runtime[anthropic]"
pip install "steerable-agent-runtime[sqlalchemy]"
pip install "steerable-agent-runtime[fastapi]"
```
