from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.eta import estimate_eta
from app.unipass import STAGE_FLOW, fetch_cargo

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

app = FastAPI(title="Customs Tracker", version="1.0.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    has_key = bool(os.getenv("UNIPASS_API_KEY", "").strip())
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "has_key": has_key,
            "stages_json": json.dumps(STAGE_FLOW, ensure_ascii=False),
        },
    )


@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "has_api_key": bool(os.getenv("UNIPASS_API_KEY", "").strip()),
    }


@app.get("/api/track")
async def track(
    hbl: str = Query(..., min_length=4, description="House B/L 또는 송장번호"),
    year: int | None = Query(None, ge=2018, le=2100),
) -> JSONResponse:
    cargo = await fetch_cargo(hbl=hbl, year=year)
    payload = cargo.to_dict()
    if cargo.found:
        eta = estimate_eta(
            arrival_date=cargo.arrival_date,
            status=cargo.status,
            carrier=cargo.carrier,
            vessel=cargo.vessel,
            cargo_type=cargo.cargo_type,
            forwarder=cargo.forwarder,
            current_stage_index=cargo.current_stage_index,
        )
        payload["eta"] = eta.to_dict()
    else:
        payload["eta"] = None
    return JSONResponse(payload)
