"""Kalibra product seeding + apply_plan_day_to_week tool."""
from datetime import date, timedelta

from sqlalchemy import select

from app.models import Food, MealPlan, PlannedMeal, User
from app.services.seed import seed_kalibra


async def test_seed_kalibra_is_idempotent(session):
    first = await seed_kalibra(session)
    assert first >= 1
    again = await seed_kalibra(session)
    assert again == 0
    res = await session.execute(select(Food).where(Food.category == "kalibra_urun"))
    assert len(list(res.scalars())) == first


async def _make_user_with_plan(session):
    user = User(telegram_id=111, name="Test", onboarding_state="active")
    session.add(user)
    await session.flush()
    week_start = date.today() - timedelta(days=date.today().weekday())
    plan = MealPlan(user_id=user.id, week_start=week_start, status="active", diet_strategy="dengeli")
    session.add(plan)
    await session.flush()
    # Day 2 has a distinctive menu; days 0-1 have something else.
    for day, name in [(0, "Menü A"), (1, "Menü B"), (2, "Favori Menü")]:
        for slot in ("kahvalti", "aksam"):
            session.add(
                PlannedMeal(
                    plan_id=plan.id, day_index=day, slot=slot,
                    name=f"{name} {slot}", kcal=500, protein_g=40, carb_g=30, fat_g=20,
                )
            )
    await session.flush()
    return user, plan


async def test_apply_plan_day_to_week(session):
    from app.ai.tools import execute_tool

    user, plan = await _make_user_with_plan(session)
    result = await execute_tool(session, user, "apply_plan_day_to_week", {"day_index": 2})
    assert "7 gün" in result

    res = await session.execute(select(PlannedMeal).where(PlannedMeal.plan_id == plan.id))
    meals = list(res.scalars())
    assert len(meals) == 14  # 7 days x 2 slots
    assert all(m.name.startswith("Favori Menü") for m in meals)
    assert sorted({m.day_index for m in meals}) == list(range(7))


async def test_apply_plan_day_rejects_empty_day(session):
    from app.ai.tools import execute_tool

    user, _ = await _make_user_with_plan(session)
    result = await execute_tool(session, user, "apply_plan_day_to_week", {"day_index": 5})
    assert "HATA" in result


def _plan(*day_meals):
    return {"days": [{"day_index": i, "meals": [{"name": n} for n in names]} for i, names in enumerate(day_meals)]}


def test_single_meat_flags_mixed_day():
    from app.services.mealplan import validate_plan_single_meat

    plan = _plan(["Hindi Şiş + Atom Salata", "Fırında Levrek + Semizotu"])
    assert validate_plan_single_meat(plan) == {0: ["balik", "hindi"]}


def test_single_meat_deli_and_eggs_exempt():
    from app.services.mealplan import validate_plan_single_meat

    plan = _plan(
        ["Hindi Füme Rulo", "Fırında Levrek"],          # füme exempt -> clean
        ["Lorlu Yumurtasız Menemen", "Izgara Tavuk"],   # yumurta word exempt -> clean
        ["Izgara Köfte + Cacık", "Dana Bonfile Izgara"],  # same red-meat category -> clean
    )
    assert validate_plan_single_meat(plan) == {}


def test_single_meat_specific_animal_beats_generic_words():
    from app.services.mealplan import _meal_meat_category

    assert _meal_meat_category("Marul Sarmalı Hindi Burger Köftesi") == "hindi"
    assert _meal_meat_category("Kıymalı Kabak Sote") == "kirmizi"
    assert _meal_meat_category("Ton Balıklı Yeşil Salata") == "balik"
    assert _meal_meat_category("Çilekli Süzme Yoğurt") is None


async def test_set_wake_time_shifts_reminders(session):
    from datetime import time
    from sqlalchemy import select
    from app.ai.tools import execute_tool
    from app.models import Profile, ReminderSetting, User

    user = User(telegram_id=111, name="Test", onboarding_state="active")
    session.add(user)
    await session.flush()
    session.add(Profile(user_id=user.id))
    session.add(ReminderSetting(user_id=user.id, kind="ogun_ogle", time_of_day=time(12, 30)))
    await session.flush()

    result = await execute_tool(session, user, "set_wake_time", {"time": "11:00"})
    assert "11:00" in result

    res = await session.execute(select(ReminderSetting).where(ReminderSetting.user_id == user.id))
    by_kind = {r.kind: r.time_of_day for r in res.scalars()}
    assert by_kind["gunaydin"] == time(11, 0)
    assert by_kind["ogun_kahvalti"] == time(11, 45)
    assert by_kind["ogun_ogle"] == time(16, 0)   # existing row updated, not duplicated
    assert by_kind["ogun_aksam"] == time(21, 0)

    prof = await session.get(Profile, (await session.execute(select(Profile.id).where(Profile.user_id == user.id))).scalar_one())
    assert prof.wake_time == time(11, 0)


async def test_set_wake_time_wraps_past_midnight(session):
    from datetime import time
    from sqlalchemy import select
    from app.ai.tools import execute_tool
    from app.models import Profile, ReminderSetting, User

    user = User(telegram_id=222, name="Gece", onboarding_state="active")
    session.add(user)
    await session.flush()
    session.add(Profile(user_id=user.id))
    await session.flush()

    await execute_tool(session, user, "set_wake_time", {"time": "16:00"})
    res = await session.execute(select(ReminderSetting).where(ReminderSetting.user_id == user.id))
    by_kind = {r.kind: r.time_of_day for r in res.scalars()}
    # aksam_kontrol = 16:00 + 13h = 05:00 next day
    assert by_kind["aksam_kontrol"] == time(5, 0)


async def test_set_auto_water_toggles_profile(session):
    from sqlalchemy import select
    from app.ai.tools import execute_tool
    from app.models import Profile, User

    user = User(telegram_id=333, name="Su", onboarding_state="active")
    session.add(user)
    await session.flush()
    session.add(Profile(user_id=user.id))
    await session.flush()

    r = await execute_tool(session, user, "set_auto_water", {"enabled": True})
    assert "AÇILDI" in r
    prof = (await session.execute(select(Profile).where(Profile.user_id == user.id))).scalar_one()
    assert prof.auto_water is True

    await execute_tool(session, user, "set_auto_water", {"enabled": False})
    assert prof.auto_water is False


async def test_adjust_water_negative_reverses(session):
    from sqlalchemy import select
    from app.ai.tools import execute_tool
    from app.models import User, WaterLog

    user = User(telegram_id=444, name="Su2", onboarding_state="active")
    session.add(user)
    await session.flush()

    await execute_tool(session, user, "adjust_water", {"amount_ml": -250})
    logs = list((await session.execute(select(WaterLog).where(WaterLog.user_id == user.id))).scalars())
    assert len(logs) == 1 and logs[0].amount_ml == -250


async def test_pin_meal_slot_keeps_other_slots(session):
    from sqlalchemy import select
    from app.ai.tools import execute_tool
    from app.models import PlannedMeal

    user, plan = await _make_user_with_plan(session)
    # _make_user_with_plan gives days 0,1,2 with kahvalti+aksam ("Menü A/B/Favori Menü").
    r = await execute_tool(session, user, "pin_meal_slot", {"slot": "kahvalti", "source_day_index": 2})
    assert "sabitlendi" in r

    res = await session.execute(select(PlannedMeal).where(PlannedMeal.plan_id == plan.id))
    meals = list(res.scalars())
    kahvalti = [m for m in meals if m.slot == "kahvalti"]
    aksam = [m for m in meals if m.slot == "aksam"]
    # Breakfast is now the pinned one on all 7 days...
    assert len(kahvalti) == 7
    assert all(m.name == "Favori Menü kahvalti" for m in kahvalti)
    # ...while dinners are untouched (still only the original 3 days).
    assert {m.day_index for m in aksam} == {0, 1, 2}
    assert any(m.name == "Menü A aksam" for m in aksam)
