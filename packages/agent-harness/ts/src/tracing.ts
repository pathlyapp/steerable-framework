export interface TraceSpan {
  spanId: string;
  name: string;
  startAt: string;
  endAt?: string;
  attrs?: Record<string, unknown>;
}

export function createSpan(spanId: string, name: string): TraceSpan {
  return {
    spanId,
    name,
    startAt: new Date().toISOString(),
    attrs: {},
  };
}

export function finishSpan(span: TraceSpan): TraceSpan {
  return {
    ...span,
    endAt: span.endAt ?? new Date().toISOString(),
  };
}
