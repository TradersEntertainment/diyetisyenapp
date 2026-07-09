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
