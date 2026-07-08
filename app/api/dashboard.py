"""Serves the single-page dashboard (auth happens in the JS via ?token=...)."""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

_TEMPLATE = Path(__file__).parent / "templates" / "dashboard.html"


@router.get("/", response_class=HTMLResponse)
async def dashboard():
    return _TEMPLATE.read_text(encoding="utf-8")
