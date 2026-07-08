"""DB-backed tests: target persistence + protein-floor enforcement, tool execution,
weekly stats gathering, shopping list build. AI network calls are never made here."""
from datetime import date


from app.models import (
    MealLog,
    MealPlan,
    PlannedMeal,
    Profile,
    User,
    WeightLog,
)
from app.services.calculations import GOAL_LOSE, compute_targets
from app.services.reports import gather_weekly_stats, get_current_targets
from app.services.shopping import build_weekly_shopping_list
from app.services.targets import ensure_protein_floor, save_targets


async def _make_user(session, tg_id=111, goal=GOAL_LOSE, body_fat=20.0):
    user = User(telegram_id=tg_id, name="Test", onboarding_state="active")
    session.add(user)
    await session.flush()
    profile = Profile(
        user_id=user.id, age=30, gender="erkek", height_cm=180,
        start_weight_kg=80, goal_weight_kg=75, body_fat_pct=body_fat,
        activity_level="orta_aktif", goals=[goal], exercise_frequency_per_week=4,
        eats_snacks=True,
    )
    session.add(profile)
    session.add(WeightLog(user_id=user.id, weight_kg=80))
    await session.flush()
    return user


async def test_save_and_get_targets(session):
    user = await _make_user(session)
    t = compute_targets(
        weight_kg=80, height_cm=180, age=30, gender="erkek",
        activity_level="orta_aktif", primary_goal=GOAL_LOSE, body_fat_pct=20,
    )
    await save_targets(session, user.id, t, diet_strategy="dengeli", reason="test")
    current = await get_current_targets(session, user.id)
    assert current is not None
    assert current.protein_g == t.protein_g
    assert current.diet_strategy == "dengeli"


async def test_ensure_protein_floor_raises_when_below(session):
    user = await _make_user(session)
    # Store a target with an artificially low protein
    t = compute_targets(
        weight_kg=80, height_cm=180, age=30, gender="erkek",
        activity_level="orta_aktif", primary_goal=GOAL_LOSE, body_fat_pct=20,
    )
    from app.models import TargetHistory

    session.add(
        TargetHistory(
            user_id=user.id, effective_date=date.today(), kcal=t.kcal,
            protein_g=80, fat_g=t.fat_g, carb_g=t.carb_g, fiber_g=t.fiber_g,
            water_ml=t.water_ml, diet_strategy="dengeli", reason="artificially low",
        )
    )
    await session.flush()
    raised = await ensure_protein_floor(session, user)
    assert raised is not None
    assert raised.protein_g == t.protein_floor_g
    # And it's now the current target
    current = await get_current_targets(session, user.id)
    assert current.protein_g == t.protein_floor_g


async def test_ensure_protein_floor_noop_when_ok(session):
    user = await _make_user(session)
    t = compute_targets(
        weight_kg=80, height_cm=180, age=30, gender="erkek",
        activity_level="orta_aktif", primary_goal=GOAL_LOSE, body_fat_pct=20,
    )
    await save_targets(session, user.id, t, diet_strategy="dengeli", reason="ok")
    raised = await ensure_protein_floor(session, user)
    assert raised is None


async def test_log_weight_tool_persists_and_checks_floor(session):
    from app.ai.tools import execute_tool

    user = await _make_user(session)
    result = await execute_tool(session, user, "log_weight", {"weight_kg": 79.5})
    assert "79.5" in result
    res = await session.execute(WeightLog.__table__.select().where(WeightLog.user_id == user.id))
    assert len(res.fetchall()) == 2  # starting weight + new


async def test_log_meal_and_preference_tools(session):
    from app.ai.tools import execute_tool

    user = await _make_user(session)
    await execute_tool(
        session, user, "log_meal",
        {"description": "Izgara tavuk", "kcal": 300, "protein_g": 45, "carb_g": 0, "fat_g": 12},
    )
    await execute_tool(
        session, user, "update_food_preference",
        {"name": "Brokoli", "level": "bayilirim", "category": "sebze"},
    )
    from app.models import FoodPreference

    res = await session.execute(FoodPreference.__table__.select().where(FoodPreference.user_id == user.id))
    prefs = res.fetchall()
    assert len(prefs) == 1


async def test_body_composition_tool_updates_floor(session):
    from app.ai.tools import execute_tool

    user = await _make_user(session, body_fat=25.0)
    t = compute_targets(
        weight_kg=80, height_cm=180, age=30, gender="erkek",
        activity_level="orta_aktif", primary_goal=GOAL_LOSE, body_fat_pct=25,
    )
    await save_targets(session, user.id, t, diet_strategy="dengeli", reason="init")
    # New body comp with much lower body fat -> higher LBM -> higher floor
    result = await execute_tool(
        session, user, "log_body_composition", {"body_fat_pct": 15, "muscle_mass_kg": 65}
    )
    assert "kaydedildi" in result.lower()
    current = await get_current_targets(session, user.id)
    # floor at 15% bf: LBM 68 * 2.2 = 149.6 -> 150 > previous
    assert current.protein_g >= 149


async def test_gather_weekly_stats_from_logs(session):
    user = await _make_user(session)
    t = compute_targets(
        weight_kg=80, height_cm=180, age=30, gender="erkek",
        activity_level="orta_aktif", primary_goal=GOAL_LOSE, body_fat_pct=20,
    )
    await save_targets(session, user.id, t, diet_strategy="dengeli", reason="init")
    session.add(MealLog(user_id=user.id, description="x", kcal=1800, protein_g=150, carb_g=100, fat_g=60))
    await session.flush()
    stats = await gather_weekly_stats(session, user)
    assert stats.kcal_target == t.kcal
    assert stats.avg_kcal_logged == 1800
    assert stats.days_meals_logged == 1


async def test_build_shopping_list_from_plans(session):
    u1 = await _make_user(session, tg_id=111)
    u2 = await _make_user(session, tg_id=222)
    week_start = date.today()
    for u in (u1, u2):
        plan = MealPlan(user_id=u.id, week_start=week_start, status="active")
        session.add(plan)
        await session.flush()
        session.add(
            PlannedMeal(
                plan_id=plan.id, day_index=0, slot="aksam", name="Tavuk",
                kcal=400, protein_g=45, carb_g=10, fat_g=15,
                ingredients=[{"name": "Tavuk göğsü", "qty": 200, "unit": "g", "category": "protein"}],
            )
        )
    await session.flush()
    slist = await build_weekly_shopping_list(session, week_start)
    await session.refresh(slist, ["items"])
    chicken = [i for i in slist.items if "tavuk" in i.name.lower()][0]
    assert "400" in chicken.quantity  # 200 + 200 g merged
