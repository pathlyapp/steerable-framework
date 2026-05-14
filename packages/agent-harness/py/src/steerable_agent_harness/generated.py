from __future__ import annotations

from pydantic import BaseModel


class HarnessMarker(BaseModel):
    status: str = "ok"
