"""FastAPI entrypoint: starts the REST API, the Telegram bot and the scheduler."""
import logging
import logging.handlers
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.api.dashboard import router as dashboard_router
from app.api.routes import router as api_router
from app.config import get_settings


def setup_logging() -> None:
    settings = get_settings()
    Path("logs").mkdir(exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler("logs/diyetisyen.log", maxBytes=2_000_000, backupCount=5),
    ]
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


log = logging.getLogger(__name__)


async def _seed_on_startup() -> None:
    from app.db import session_scope
    from app.services.seed import seed_foods, seed_kalibra

    try:
        async with session_scope() as session:
            await seed_foods(session)
            await seed_kalibra(session)
    except Exception:
        log.exception("food seeding on startup failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()

    await _seed_on_startup()

    bot_app = None
    scheduler = None
    if settings.telegram_bot_token and settings.allowed_telegram_ids:
        from app.bot.bot import create_application, start_bot
        from app.scheduler.jobs import create_scheduler

        bot_app = create_application()
        await start_bot(bot_app)
        scheduler = create_scheduler(bot_app)
        scheduler.start()
        log.info("scheduler started")
    else:
        log.warning(
            "TELEGRAM_BOT_TOKEN / ALLOWED_TELEGRAM_IDS not set — running API only (no bot, no scheduler)"
        )

    app.state.bot_app = bot_app
    try:
        yield
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)
        if bot_app:
            from app.bot.bot import stop_bot

            await stop_bot(bot_app)


app = FastAPI(title="Diyetisyen", lifespan=lifespan)
app.include_router(api_router)
app.include_router(dashboard_router)
