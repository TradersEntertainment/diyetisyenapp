"""Seed the food database from app/data/foods_tr.json (idempotent)."""
import json
import logging
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Food

log = logging.getLogger(__name__)

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "foods_tr.json"


async def seed_foods(session: AsyncSession) -> int:
    count = (await session.execute(select(func.count()).select_from(Food))).scalar_one()
    if count:
        return 0
    foods = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    for f in foods:
        session.add(Food(**f))
    await session.flush()
    log.info("seeded %d foods", len(foods))
    return len(foods)
