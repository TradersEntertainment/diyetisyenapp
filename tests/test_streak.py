"""Tests for the consecutive-days logging streak used for celebrations."""
from datetime import datetime, timedelta, timezone

from app.models import MealLog, User, WaterLog, WeightLog
from app.services.reports import logging_streak_days


async def _make_user(session, tg_id=111):
    user = User(telegram_id=tg_id, name="Test", onboarding_state="active")
    session.add(user)
    await session.flush()
    return user


def _at(days_ago: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


async def test_no_logs_means_zero(session):
    user = await _make_user(session)
    assert await logging_streak_days(session, user.id) == 0


async def test_streak_counts_consecutive_days_across_log_kinds(session):
    user = await _make_user(session)
    session.add(WeightLog(user_id=user.id, weight_kg=80, ts=_at(0)))
    session.add(MealLog(user_id=user.id, description="x", kcal=1, protein_g=0, carb_g=0, fat_g=0, ts=_at(1)))
    session.add(WaterLog(user_id=user.id, amount_ml=200, ts=_at(2)))
    await session.flush()
    assert await logging_streak_days(session, user.id) == 3


async def test_gap_breaks_streak(session):
    user = await _make_user(session)
    session.add(WeightLog(user_id=user.id, weight_kg=80, ts=_at(0)))
    session.add(WeightLog(user_id=user.id, weight_kg=80, ts=_at(3)))  # gap at day 1-2
    await session.flush()
    assert await logging_streak_days(session, user.id) == 1


async def test_missing_today_does_not_break_streak(session):
    user = await _make_user(session)
    session.add(WeightLog(user_id=user.id, weight_kg=80, ts=_at(1)))
    session.add(WaterLog(user_id=user.id, amount_ml=200, ts=_at(2)))
    await session.flush()
    assert await logging_streak_days(session, user.id) == 2
