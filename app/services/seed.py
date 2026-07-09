"""Seed the food database from app/data/foods_tr.json (idempotent)."""
import json
import logging
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Food

log = logging.getLogger(__name__)

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "foods_tr.json"
KALIBRA_FILE = Path(__file__).resolve().parent.parent / "data" / "foods_kalibra.json"


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


async def seed_kalibra(session: AsyncSession) -> int:
    """Per-item idempotent (seed_foods skips entirely once the table is filled,
    so the Kalibra products added later need their own name-based check)."""
    added = 0
    for f in json.loads(KALIBRA_FILE.read_text(encoding="utf-8")):
        res = await session.execute(select(Food).where(Food.name_tr == f["name_tr"]))
        if res.scalar_one_or_none():
            continue
        session.add(Food(**f))
        added += 1
    if added:
        await session.flush()
        log.info("seeded %d kalibra products", added)
    return added
