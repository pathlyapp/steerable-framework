from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class TraceSpan:
    span_id: str
    name: str
    start_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    end_at: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)

    def finish(self) -> None:
        if self.end_at is None:
            self.end_at = datetime.now(timezone.utc).isoformat()
