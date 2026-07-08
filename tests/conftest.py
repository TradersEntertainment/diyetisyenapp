import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("ALLOWED_TELEGRAM_IDS", "111,222")
os.environ.setdefault("DASHBOARD_TOKEN", "testtoken")

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
import app.models  # noqa: F401  register models


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()
