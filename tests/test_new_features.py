"""Tests for voice STT gating, lab logging, barcode, and adherence breakdown."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import HungerLog, LabResult, MealLog, User


async def _user(session, tg=111):
    u = User(telegram_id=tg, name="Test", onboarding_state="active")
    session.add(u)
    await session.flush()
    return u


def test_stt_unavailable_without_keys(monkeypatch):
    from app.ai import stt
    from app.config import Settings, get_settings

    get_settings.cache_clear()
    s = Settings(groq_api_key="", openai_api_key="")
    monkeypatch.setattr(stt, "get_settings", lambda: s)
    assert stt.stt_available() is False


def test_stt_available_with_groq_key(monkeypatch):
    from app.ai import stt
    from app.config import Settings

    s = Settings(groq_api_key="gsk_test")
    monkeypatch.setattr(stt, "get_settings", lambda: s)
    assert stt.stt_available() is True


async def test_log_and_get_lab_result(session):
    from app.ai.tools import execute_tool

    user = await _user(session)
    r = await execute_tool(session, user, "log_lab_result",
                           {"panel": "LDL kolesterol", "value": 145, "unit": "mg/dL"})
    assert "kaydedildi" in r.lower()
    rows = list((await session.execute(select(LabResult).where(LabResult.user_id == user.id))).scalars())
    assert len(rows) == 1 and rows[0].value == 145

    hist = await execute_tool(session, user, "get_lab_history", {"panel": "LDL"})
    assert "145" in hist


async def test_adherence_breakdown_flags_worst_slot(session):
    from app.services.analysis import adherence_breakdown

    now = datetime.now(timezone.utc)
    # Only lunches logged over the week -> breakfast is the most-missed slot.
    meals = [MealLog(user_id=1, description="x", kcal=1, protein_g=0, carb_g=0, fat_g=0,
                     slot="ogle", ts=now - timedelta(days=i)) for i in range(5)]
    b = adherence_breakdown(meals, cheat_count=0, cheat_hours=[], high_hunger_hours=[22, 23, 22])
    assert b["en_cok_atlanan_ogun"] == "kahvaltı"
    assert b["en_ac_hissedilen_zaman"] == "gece"
    assert "öğle" in b["kaydedilen_ogunler"]


async def test_get_adherence_analysis_tool(session):
    from app.ai.tools import execute_tool

    user = await _user(session, tg=222)
    session.add(MealLog(user_id=user.id, description="çorba", kcal=200, protein_g=10,
                        carb_g=20, fat_g=5, slot="ogle"))
    session.add(HungerLog(user_id=user.id, hunger=5, craving="tatlı"))
    await session.flush()
    import json
    out = json.loads(await execute_tool(session, user, "get_adherence_analysis", {}))
    assert "en_cok_atlanan_ogun" in out
