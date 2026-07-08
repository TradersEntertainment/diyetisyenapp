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
        _cached_id = int(row.value) if row else None
        _cache_loaded = True
    return _cached_id


async def set_group_chat_id(session: AsyncSession, chat_id: int) -> bool:
    """Persist a newly seen group chat id. Returns True when it changed."""
    global _cached_id, _cache_loaded
    if _cache_loaded and _cached_id == chat_id:
        return False
    row = await session.get(AppSetting, GROUP_KEY)
    changed = row is None or row.value != str(chat_id)
    if row is None:
        session.add(AppSetting(key=GROUP_KEY, value=str(chat_id)))
    else:
        row.value = str(chat_id)
    _cached_id = chat_id
    _cache_loaded = True
    if changed:
        log.info("shared group chat id set to %s", chat_id)
    return changed
