from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TrafficFineCheckRequest(BaseModel):
    license_plate: str = Field(..., min_length=3)
    vehicle_type: str = "car"


class TrafficFineCheckResponse(BaseModel):
    error: int
    message: str = ""
    data: list[dict[str, Any]] = Field(default_factory=list)
    source: str = "csgt_scraper"
    error_text: str | None = None


class HealthResponse(BaseModel):
    status: str = "ok"
