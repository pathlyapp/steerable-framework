from __future__ import annotations

from steerable_agent_harness import tracing


def test_trace_span_starts_open() -> None:
    span = tracing.TraceSpan(span_id="step_1", name="llm.generate")
    assert span.start_at  # ISO timestamp present
    assert span.end_at is None


def test_trace_span_finish_sets_end_at_once() -> None:
    span = tracing.TraceSpan(span_id="step_2", name="tool.exec")
    span.finish()
    assert span.end_at is not None
    first_end = span.end_at
    span.finish()
    assert span.end_at == first_end  # no double-overwrite


def test_trace_span_attrs_default_to_empty_dict() -> None:
    span = tracing.TraceSpan(span_id="step_3", name="planning")
    assert span.attrs == {}
    span.attrs["model"] = "gpt-4o"
    assert span.attrs == {"model": "gpt-4o"}


def test_trace_span_golden(assert_golden) -> None:
    span = tracing.TraceSpan(
        span_id="step_1",
        name="llm.generate",
        start_at="2026-01-01T00:00:00+00:00",
        attrs={"model": "gpt-4o"},
    )
    span.end_at = "2026-01-01T00:00:01+00:00"
    payload = {
        "span_id": span.span_id,
        "name": span.name,
        "start_at": span.start_at,
        "end_at": span.end_at,
        "attrs": span.attrs,
    }
    assert_golden("trace_span_basic", payload)
