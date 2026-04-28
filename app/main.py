from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .models import HealthResponse, TrafficFineCheckRequest, TrafficFineCheckResponse
from .traffic_lookup_service import ProviderError, TrafficFineLookupService

load_dotenv()

app = FastAPI(title="Autobis Traffic Fine Scraper API", version="1.0.0")

origins = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

lookup_service = TrafficFineLookupService()


@app.get("/", response_model=HealthResponse)
async def root() -> HealthResponse:
    return HealthResponse()


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()


@app.post("/api/traffic-fines/check", response_model=TrafficFineCheckResponse)
async def check_traffic_fines(
    request: TrafficFineCheckRequest,
) -> TrafficFineCheckResponse:
    try:
        result = await lookup_service.check(
            license_plate=request.license_plate,
            vehicle_type=request.vehicle_type,
        )
        return TrafficFineCheckResponse(
            error=0,
            message=result.message,
            data=result.data,
            source=result.source,
        )
    except ProviderError as exc:
        return TrafficFineCheckResponse(
            error=1,
            message="Tra cuu that bai",
            error_text=str(exc),
            data=[],
        )
