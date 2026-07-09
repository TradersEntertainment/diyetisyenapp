"""Shared Telegram group support.

The dietitian lives in one group chat with both users. The group's chat id is
auto-detected the first time an allowed user (or the bot being added) is seen
in a group, persisted in app_settings and cached in memory. GROUP_CHAT_ID env
var, when set, overrides everything.
"""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import AppSetting

log = logging.getLogger(__name__)

GROUP_KEY = "group_chat_id"

_cached_id: int | None = None
_cache_loaded = False


async def get_group_chat_id(session: AsyncSession) -> int | None:
    """The shared group's chat id, or None while it hasn't been discovered yet."""
    env_id = get_settings().group_chat_id
    if env_id:
        return env_id
    global _cached_id, _cache_loaded
    if not _cache_loaded:
        row = await session.get(AppSetting, GROUP_KEY)
        # Re-check after the await: a concurrent writer may have just set the
        # cache; its fresh value must not be clobbered with our older snapshot.
        if not _cache_loaded:
            _cached_id = int(row.value) if row else None
            _cache_loaded = True
    return _cached_id


async def set_group_chat_id(session: AsyncSession, chat_id: int, *, force: bool = False) -> bool:
    """Write a group chat id to the DB. Returns True when a write happened.

    The first discovered group is sticky: without force, an already-stored id is
    never overwritten (so the bot casually seeing another group can't hijack
    routing). force=True is for deliberate acts like adding the bot to a group.

    NOTE: this only stages the DB write; call confirm_group_cache() after the
    surrounding transaction commits so a failed commit can be retried later.
    """
    current = await get_group_chat_id(session)
    if current == chat_id:
        return False
    if current is not None and not force:
        return False
    row = await session.get(AppSetting, GROUP_KEY)
    if row is None:
        session.add(AppSetting(key=GROUP_KEY, value=str(chat_id)))
    else:
        row.value = str(chat_id)
    return True


def confirm_group_cache(chat_id: int) -> None:
    """Update the in-memory cache once the DB write is safely committed."""
    global _cached_id, _cache_loaded
    _cached_id = chat_id
    _cache_loaded = True
    log.info("shared group chat id set to %s", chat_id)
