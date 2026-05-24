# Python Dev Server Example (Independent E2E)

This example is a fully working FastAPI development server built using `steerable-agent-runtime`'s `ChatLoop` and `FastAPISseTransport`.

It acts as the **complete, independent local backend** for the frontend `web-shell` example, enabling a seamless end-to-end local developer experience without needing the main `deeppath-api` repo.

## Features

1. **Dual LLM Mode**:
   * **Local Mock Mode (Zero Setup)**: If no `OPENAI_API_KEY` is configured, it falls back to a rules-based mock provider that streams mock answers and realistically triggers tools (weather, calculation).
   * **Real LLM Mode**: If `OPENAI_API_KEY` is set, it uses `OpenAICompatProvider` to stream real completions from OpenAI, Ollama, SiliconFlow, or any other OpenAI-compatible endpoint.
2. **Local Python Tools**: Exposes real Python `@tool` definitions (`get_weather` and `calculate`) registered with the framework's `ToolRouter` and automatically executed during the run.
3. **CORS Enabled**: Pre-configured to accept incoming connections from the local `web-shell` frontend.

## Run Server

From the framework monorepo root:

```bash
# Sync workspace environment
uv sync

# Run the backend dev server (starts on http://127.0.0.1:5181)
pnpm server:dev
```

## Connect Frontend (Web-Shell)

In another terminal, run the web-shell in **online** mode to connect directly to this backend:

```bash
# Start Web-Shell in Sidecar/Server mode pointing to the Python dev server
pnpm shell:online
```

Open `http://localhost:5180` in your browser.

## Try These Prompts

To test the end-to-end tool execution and streaming experience:

1. **Talk normally**:
   > *"你好！"* -> Will stream back a welcoming message introducing the framework's dev server.
2. **Trigger the Weather tool**:
   > *"北京的天气怎么样？"* -> Will trigger the `get_weather` tool, run the Python function locally, print execution logs on the server console, and stream back the formatted result.
3. **Trigger the Math calculation tool**:
   > *"帮我算一下 3 * 15 + 4"* -> Will trigger the `calculate` tool, sandboxed-evaluate the expression, print execution logs, and stream the response.
