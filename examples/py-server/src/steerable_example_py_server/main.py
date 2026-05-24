"""FastAPI + ChatLoop development server for Steerable Web-Shell end-to-end local testing.

Provides a fully working Python backend running ChatLoop with a local mock/real LLM
and a set of interactive tools, streaming SSE back to the frontend.
"""

from __future__ import annotations

import asyncio
import os
import logging
from typing import Any
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from steerable_agent_protocol.generated import SSEEvent
from steerable_agent_harness import BudgetLimit
from steerable_agent_runtime import (
    ChatLoop,
    LoopConfig,
    ToolRouter,
    tool,
    OpenAICompatProvider,
    EmitCtx,
)
from steerable_agent_runtime.transport import (
    FastAPISseTransport,
    sse_response,
)
from steerable_agent_runtime.llm import LLMProvider, LLMMessage, LLMStreamChunk

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("steerable_example_py_server")

app = FastAPI(title="Steerable Independent Dev Server")

# Enable CORS so the local Web-Shell (http://localhost:5180) can communicate seamlessly
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Fallback Mock Provider (Zero Setup)
# ---------------------------------------------------------------------------
class DummyLLMProvider:
    """A local mock LLM provider that handles basic conversation and tool triggers
    when no real OpenAI/Ollama keys or URLs are configured.
    """
    name = "mock-provider"
    model = "mock-model"

    async def complete(self, messages: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("Dev server uses streaming only")

    async def stream(self, messages: list[LLMMessage], **kwargs: Any) -> Any:
        user_text = messages[-1].content if messages else ""
        logger.info(f"MockProvider received query: {user_text}")

        # Basic router logic to trigger tools or talk back
        if "天气" in user_text or "weather" in user_text.lower():
            # Trigger a tool call to 'get_weather'
            yield LLMStreamChunk(
                tool_call_delta={
                    "index": 0,
                    "id": "call_weather_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "北京"}'}
                }
            )
            yield LLMStreamChunk(finish_reason="tool_calls")
        elif "计算" in user_text or "calculate" in user_text.lower() or "1+1" in user_text:
            # Trigger a tool call to 'calculate'
            yield LLMStreamChunk(
                tool_call_delta={
                    "index": 0,
                    "id": "call_calc_1",
                    "type": "function",
                    "function": {"name": "calculate", "arguments": '{"expression": "3 * 15 + 4"}'}
                }
            )
            yield LLMStreamChunk(finish_reason="tool_calls")
        else:
            # Check if we are responding to a tool result
            if any(msg.role == "tool" for msg in messages):
                tool_msg = next(msg for msg in reversed(messages) if msg.role == "tool")
                logger.info(f"Responding to tool result: {tool_msg.content}")
                response = f"根据工具执行的结果，得出的结论是：{tool_msg.content}。请问还有什么我可以帮您的吗？"
            else:
                response = f"您好！我是集成在 steerable-framework 独立开发后端（py-server）中的智能体。由于您未配置大模型 API 密钥，我正在以【Mock 本地离线模式】运行。若要体验大模型与工具的真实调用，您可以配置环境变量 `OPENAI_API_KEY`。试试问我「查询北京的天气」或「计算 3*15+4」，我会自动调起真实的本地 Python 工具函数！"

            # Stream the text response back chunk by chunk
            for char in response:
                await asyncio.sleep(0.015)
                yield LLMStreamChunk(content_delta=char)
            yield LLMStreamChunk(finish_reason="stop")


# ---------------------------------------------------------------------------
# Tool Declarations
# ---------------------------------------------------------------------------
router = ToolRouter()

@tool(router=router, description="查询指定城市的天气预报")
async def get_weather(city: str) -> dict[str, Any]:
    """获取指定城市的实时天气数据。"""
    logger.info(f"Executing tool [get_weather] for city: {city}")
    return {
        "city": city,
        "temperature": "22°C",
        "condition": "晴朗",
        "humidity": "45%",
        "wind": "微风 2级"
    }

@tool(router=router, description="执行数学算术表达式计算")
async def calculate(expression: str) -> dict[str, Any]:
    """安全的简单数学表达式计算。"""
    logger.info(f"Executing tool [calculate] for expression: {expression}")
    try:
        # Simple sandbox check
        allowed_chars = set("0123456789+-*/() .")
        if not set(expression).issubset(allowed_chars):
            return {"error": "表达式包含非法字符"}
        result = eval(expression, {"__builtins__": None}, {})
        return {"expression": expression, "result": result}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# POST /chat/stream
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    content: str
    metadata: dict[str, Any] | None = None


async def _run_chat_loop(transport: FastAPISseTransport, request: ChatRequest) -> None:
    """Wired core ChatLoop executor."""
    try:
        # Load local .env/deeppath.env keys if present
        # First, try to read standard environment variables
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("SILICONFLOW_API_KEY")
        base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("SILICONFLOW_API_URL")
        model = os.environ.get("OPENAI_MODEL") or os.environ.get("SILICONFLOW_MODEL")

        # Try to automatically read from local .env files inside the framework directory tree
        if not api_key:
            # Look at: 1. current working directory's .env, 2. py-server package folder's .env
            possible_paths = [
                os.path.join(os.getcwd(), ".env"),
                os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), ".env"),
            ]
            for env_path in possible_paths:
                if os.path.exists(env_path):
                    logger.info(f"Loading local environment variables from: {env_path}")
                    try:
                        with open(env_path, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if not line or line.startswith("#"):
                                    continue
                                if "=" in line:
                                    k, v = line.split("=", 1)
                                    k = k.strip()
                                    v = v.strip().strip("'\"")
                                    os.environ[k] = v
                        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("SILICONFLOW_API_KEY")
                        base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("SILICONFLOW_API_URL")
                        model = os.environ.get("OPENAI_MODEL") or os.environ.get("SILICONFLOW_MODEL")
                        if api_key:
                            break
                    except Exception as e:
                        logger.warning(f"Failed to read local env file {env_path}: {e}")

        # Fallbacks if still not set
        if api_key:
            base_url = base_url or "https://api.openai.com/v1"
            model = model or "gpt-4o"
        else:
            base_url = "https://api.openai.com/v1"
            model = "gpt-4o"

        provider: LLMProvider
        if api_key:
            logger.info(f"Using Real OpenAICompatProvider (Model: {model}, Base URL: {base_url})")
            provider = OpenAICompatProvider(
                name="openai-compat",
                model=model,
                base_url=base_url,
                api_key=api_key,
            )
        else:
            logger.info("No OPENAI_API_KEY found, falling back to Local Offline Mock Provider")
            provider = DummyLLMProvider()  # type: ignore

        # Map frontend request format to framework messages (expects LLMMessage, not ChatMessage)
        initial_messages = [
            LLMMessage(
                role="user",
                content=request.content,
            )
        ]

        # Since LoopConfig uses dataclasses, we should use standard parameter names.
        # It expects `tool_router` (not `tools`) and `provider_kind` (which we set to 'openai_compat').
        config = LoopConfig(
            provider=provider,
            provider_kind="openai_compat",
            tool_router=router,
            initial_messages=initial_messages,
            budget=BudgetLimit(max_tokens=4000, max_steps=10, max_tool_calls=5),
        )

        loop = ChatLoop(config=config)

        # Wire up ChatLoop's internal emit to transport's emit so events stream out in real time
        async def on_emit(ctx: EmitCtx) -> Any:
            # Emit standard event to SSE stream
            if ctx.event:
                await transport.emit(ctx.event)

        loop.on("emit", on_emit)

        # Execute the loop end-to-end and consume its events (loop.run() is an async generator)
        async for sse in loop.run():
            # Already emitted internally via the 'emit' event callback,
            # but we must iterate through the generator to execute it.
            pass

    except Exception as e:
        logger.exception("Error running ChatLoop")
        # Emit error event
        await transport.emit(SSEEvent(type="error", payload={"message": str(e)}))
    finally:
        # Safely close the SSE connection
        await transport.aclose()


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest) -> Any:
    """The standard framework-compatible SSE chat endpoint."""
    logger.info(f"New chat stream session requested: content='{request.content}'")
    transport = FastAPISseTransport()
    
    # Spawn the async task to execute the ChatLoop and pipe outputs to SSE
    asyncio.create_task(_run_chat_loop(transport, request))
    
    # Return Starlette StreamingResponse mapped to the transport
    return await sse_response(transport)


def run_server() -> None:
    import uvicorn
    uvicorn.run("steerable_example_py_server.main:app", host="127.0.0.1", port=5181, reload=True)


if __name__ == "__main__":
    run_server()
