"""OTel export: HarnessTrace → OTLP/HTTP JSON payload."""

from __future__ import annotations

import json

import pytest
from steerable_agent_runtime import (
    CoreLoop,
    RouterToolExecutor,
    ToolRouter,
    TraceRecorder,
    export_otlp_http,
    to_otlp_json,
)
from steerable_agent_runtime.llm import LLMMessage
from steerable_agent_runtime.storage import InMemoryStorage

from .test_trace_recorder import make_provider, tc


def _attrs(span: dict) -> dict[str, object]:
    out: dict[str, object] = {}
    for attr in span["attributes"]:
        value = attr["value"]
        out[attr["key"]] = next(iter(value.values()))
    return out


async def _recorded_trace():
    router = ToolRouter()

    async def add(a: int, b: int) -> int:
        return a + b

    router.register(add)
    storage = InMemoryStorage()
    recorder = TraceRecorder(storage, chat_id="chat_1")
    loop = CoreLoop(make_provider([
        {"content": "", "tool_calls": [tc("add", {"a": 1, "b": 2})]},
        {"content": "3"},
    ]), RouterToolExecutor(router))
    async for _ in recorder.tee(loop.run([LLMMessage(role="user", content="go")])):
        pass
    trace = await storage.get_trace(recorder.trace_id)
    spans = await storage.list_spans(recorder.trace_id)
    events = await storage.list_events(recorder.trace_id)
    return trace, spans, events


@pytest.mark.asyncio
async def test_otlp_payload_structure() -> None:
    trace, spans, events = await _recorded_trace()
    payload = to_otlp_json(trace, spans, events, service_name="test-svc")

    assert set(payload) == {"resourceSpans"}
    rs = payload["resourceSpans"][0]
    assert _attrs(rs["resource"])["service.name"] == "test-svc"
    otel_spans = rs["scopeSpans"][0]["spans"]
    assert len(otel_spans) == 2  # root + 1 tool span

    root, tool = otel_spans[0], otel_spans[1]
    # 32-hex trace id shared, 16-hex span ids, parenting intact
    assert len(root["traceId"]) == 32 and root["traceId"] == tool["traceId"]
    assert len(root["spanId"]) == len(tool["spanId"]) == 16
    assert tool["parentSpanId"] == root["spanId"]

    assert root["name"] == "coreloop.run"
    assert tool["name"] == "tool.add"
    assert _attrs(root)["steerable.chat_id"] == "chat_1"
    assert _attrs(tool)["toolCallId"].startswith("call_add")

    # statuses: clean run → both OK
    assert root["status"]["code"] == 1
    assert tool["status"]["code"] == 1

    # every recorded event became a root span event, in order
    names = [e["name"] for e in root["events"]]
    assert names[0].startswith("stage_start:")
    assert names[-1].startswith("completion:")
    assert any(n.startswith("tool_call_result:") for n in names)
    # monotonic non-decreasing timestamps
    nanos = [int(e["timeUnixNano"]) for e in root["events"]]
    assert nanos == sorted(nanos)


@pytest.mark.asyncio
async def test_otlp_error_status_mapping() -> None:
    router = ToolRouter()

    async def boom() -> None:
        raise RuntimeError("nope")

    router.register(boom)
    storage = InMemoryStorage()
    recorder = TraceRecorder(storage)
    loop = CoreLoop(
        make_provider([{"content": "", "tool_calls": [tc("boom")]}, {"content": "x"}]),
        RouterToolExecutor(router),
    )
    async for _ in recorder.tee(loop.run([LLMMessage(role="user", content="go")])):
        pass

    payload = to_otlp_json(
        await storage.get_trace(recorder.trace_id),
        await storage.list_spans(recorder.trace_id),
        await storage.list_events(recorder.trace_id),
    )
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert spans[0]["status"]["code"] == 2  # root: hadError
    assert spans[1]["status"]["code"] == 2  # tool span: error


def test_id_encoding_is_deterministic_for_foreign_ids() -> None:
    from steerable_agent_runtime.otel import _span_id, _trace_id

    # framework-native ids pass through
    native = "trace_" + "ab" * 16
    assert _trace_id(native) == "ab" * 16
    # foreign ids hash deterministically (idempotent re-export)
    assert _trace_id("chat-42") == _trace_id("chat-42")
    assert len(_trace_id("chat-42")) == 32
    assert _span_id("span_0001", salt="t") == _span_id("span_0001", salt="t")
    assert _span_id("span_0001", salt="t") != _span_id("span_0001", salt="u")


@pytest.mark.asyncio
async def test_export_otlp_http_posts_json() -> None:
    """Round-trip against a stub collector over a local socket."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    received: dict = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received["path"] = self.path
            received["content_type"] = self.headers["Content-Type"]
            received["body"] = json.loads(body)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):  # silence
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        trace, spans, events = await _recorded_trace()
        payload = to_otlp_json(trace, spans, events)
        status = export_otlp_http(
            payload, f"http://127.0.0.1:{server.server_port}/v1/traces"
        )
    finally:
        server.shutdown()

    assert status == 200
    assert received["path"] == "/v1/traces"
    assert received["content_type"] == "application/json"
    assert received["body"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
