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


def test_plan_model_only_for_fresh_menu():
    """Fable is used only to CREATE a menu; the partner copy uses Sonnet."""
    from app.ai.client import get_model, get_plan_model
    from app.services.mealplan import _plan_model_for

    assert _plan_model_for(None) == get_plan_model()          # fresh menu -> Fable
    assert _plan_model_for([{"day_index": 0}]) == get_model()  # copy -> Sonnet
    assert get_plan_model() != get_model()                     # they really differ


def test_tts_unavailable_without_key(monkeypatch):
    from app.ai import tts
    from app.config import Settings

    monkeypatch.setattr(tts, "get_settings", lambda: Settings(elevenlabs_api_key=""))
    assert tts.tts_available() is False


def test_tts_clean_for_speech_strips_markup_and_emoji():
    from app.ai.tts import clean_for_speech

    out = clean_for_speech("*Merhaba* Ömer 💪 bugün harika! [SESSIZ] https://x.co")
    assert "*" not in out and "💪" not in out and "[SESSIZ]" not in out
    assert "http" not in out
    assert "Merhaba" in out and "Ömer" in out


async def test_set_voice_replies_toggles_profile(session):
    from sqlalchemy import select
    from app.ai.tools import execute_tool
    from app.models import Profile, User

    user = User(telegram_id=555, name="Ses", onboarding_state="active")
    session.add(user)
    await session.flush()
    session.add(Profile(user_id=user.id))
    await session.flush()

    r = await execute_tool(session, user, "set_voice_replies", {"enabled": True})
    assert "AÇILDI" in r
    prof = (await session.execute(select(Profile).where(Profile.user_id == user.id))).scalar_one()
    assert prof.voice_replies is True


async def test_log_meal_works_after_adherence_call(session):
    """Regression: a local 'from app.models import MealLog' in a later dispatch
    branch made MealLog function-local, breaking log_meal with UnboundLocalError."""
    from sqlalchemy import select
    from app.ai.tools import execute_tool
    from app.models import MealLog as ML

    user = await _user(session, tg=777)
    # Touch the branch that previously shadowed MealLog...
    await execute_tool(session, user, "get_adherence_analysis", {})
    # ...then log a meal in the same dispatch module scope.
    r = await execute_tool(session, user, "log_meal", {
        "description": "kıyma kavurma + lavaş", "kcal": 720, "protein_g": 45, "carb_g": 40, "fat_g": 30,
    })
    assert "HATA" not in r
    meals = list((await session.execute(select(ML).where(ML.user_id == user.id))).scalars())
    assert len(meals) == 1 and meals[0].kcal == 720
