"""Data gathering + deterministic report content.

Everything numeric is computed here from the database; the AI layer only wraps
these facts in an empathetic Turkish narrative (see app/ai/dietitian.py).
"""
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    BodyCompositionLog,
    ExerciseLog,
    HungerLog,
    MealLog,
    MoodLog,
    Profile,
    SleepLog,
    TargetHistory,
    User,
    WaterLog,
    WeightLog,
)
from app.services.analysis import (
    WeeklyStats,
    is_plateau,
    water_retention_suspected,
    weekly_change,
    weekly_change_pct,
)


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


async def get_current_targets(session: AsyncSession, user_id: int) -> TargetHistory | None:
    res = await session.execute(
        select(TargetHistory)
        .where(TargetHistory.user_id == user_id)
        .order_by(TargetHistory.effective_date.desc(), TargetHistory.id.desc())
        .limit(1)
    )
    return res.scalar_one_or_none()


async def logging_streak_days(session: AsyncSession, user_id: int, lookback_days: int = 90) -> int:
    """Consecutive days with at least one log (meal/weight/water), counted back
    from today; today not having a log yet doesn't break the streak."""
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    logged: set[date] = set()
    for model in (MealLog, WeightLog, WaterLog):
        res = await session.execute(
            select(model.ts).where(model.user_id == user_id, model.ts >= since)
        )
        logged.update(ts.date() for ts in res.scalars())
    today = date.today()
    streak = 0
    day = today if today in logged else today - timedelta(days=1)
    while day in logged:
        streak += 1
        day -= timedelta(days=1)
    return streak


async def weight_series(session: AsyncSession, user_id: int, days: int = 90) -> list[tuple[date, float]]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    res = await session.execute(
        select(WeightLog).where(WeightLog.user_id == user_id, WeightLog.ts >= since).order_by(WeightLog.ts)
    )
    return [(row.ts.date(), row.weight_kg) for row in res.scalars()]


async def body_comp_series(
    session: AsyncSession, user_id: int, days: int = 90
) -> tuple[list[tuple[date, float]], list[tuple[date, float]]]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    res = await session.execute(
        select(BodyCompositionLog)
        .where(BodyCompositionLog.user_id == user_id, BodyCompositionLog.ts >= since)
        .order_by(BodyCompositionLog.ts)
    )
    fat, muscle = [], []
    for row in res.scalars():
        if row.body_fat_pct is not None:
            fat.append((row.ts.date(), row.body_fat_pct))
        if row.muscle_mass_kg is not None:
            muscle.append((row.ts.date(), row.muscle_mass_kg))
    return fat, muscle


async def gather_weekly_stats(
    session: AsyncSession, user: User, end_day: date | None = None
) -> WeeklyStats:
    """Compute one week of facts for the adaptive engine / weekly report."""
    end_day = end_day or date.today()
    start_dt = datetime.combine(end_day - timedelta(days=7), time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(end_day, time.max, tzinfo=timezone.utc)
    stats = WeeklyStats()

    weights = await weight_series(session, user.id, days=30)
    stats.weight_change_kg_per_week = weekly_change(weights)
    stats.weight_change_pct_per_week = weekly_change_pct(weights)
    stats.plateau = is_plateau(weights)
    stats.water_retention_suspected = water_retention_suspected(weights)

    fat, muscle = await body_comp_series(session, user.id, days=45)
    stats.fat_pct_change_per_week = weekly_change(fat)
    stats.muscle_kg_change_per_week = weekly_change(muscle)

    targets = await get_current_targets(session, user.id)
    if targets:
        stats.kcal_target = targets.kcal
        stats.protein_target = targets.protein_g
        stats.water_target_ml = targets.water_ml

    # Meals
    res = await session.execute(
        select(MealLog).where(MealLog.user_id == user.id, MealLog.ts >= start_dt, MealLog.ts <= end_dt)
    )
    meals = list(res.scalars())
    by_day: dict[date, list[MealLog]] = {}
    for m in meals:
        by_day.setdefault(m.ts.date(), []).append(m)
    stats.days_meals_logged = len(by_day)
    stats.cheat_meals = sum(1 for m in meals if m.is_cheat)
    day_kcals = [sum(m.kcal or 0 for m in day) for day in by_day.values()]
    day_prot = [sum(m.protein_g or 0 for m in day) for day in by_day.values()]
    if day_kcals:
        stats.avg_kcal_logged = round(sum(day_kcals) / len(day_kcals))
        stats.avg_protein_logged = round(sum(day_prot) / len(day_prot))

    # Water (avg per logged day)
    res = await session.execute(
        select(WaterLog).where(WaterLog.user_id == user.id, WaterLog.ts >= start_dt, WaterLog.ts <= end_dt)
    )
    water_by_day: dict[date, int] = {}
    for w in res.scalars():
        water_by_day[w.ts.date()] = water_by_day.get(w.ts.date(), 0) + w.amount_ml
    if water_by_day:
        stats.avg_water_ml = round(sum(water_by_day.values()) / len(water_by_day))

    # Exercise
    res = await session.execute(
        select(ExerciseLog).where(
            ExerciseLog.user_id == user.id, ExerciseLog.ts >= start_dt, ExerciseLog.ts <= end_dt
        )
    )
    stats.exercise_sessions = len(list(res.scalars()))
    prof_res = await session.execute(select(Profile).where(Profile.user_id == user.id))
    profile: Profile | None = prof_res.scalar_one_or_none()
    if profile and profile.exercise_frequency_per_week:
        stats.exercise_target_sessions = profile.exercise_frequency_per_week

    # Sleep / mood / hunger
    res = await session.execute(
        select(SleepLog).where(SleepLog.user_id == user.id, SleepLog.ts >= start_dt, SleepLog.ts <= end_dt)
    )
    sleeps = [s.hours for s in res.scalars()]
    if sleeps:
        stats.avg_sleep_hours = round(sum(sleeps) / len(sleeps), 1)

    res = await session.execute(
        select(MoodLog).where(MoodLog.user_id == user.id, MoodLog.ts >= start_dt, MoodLog.ts <= end_dt)
    )
    moods = list(res.scalars())
    mood_vals = [m.mood for m in moods if m.mood is not None]
    energy_vals = [m.energy for m in moods if m.energy is not None]
    if mood_vals:
        stats.avg_mood = round(sum(mood_vals) / len(mood_vals), 1)
    if energy_vals:
        stats.avg_energy = round(sum(energy_vals) / len(energy_vals), 1)

    res = await session.execute(
        select(HungerLog).where(
            HungerLog.user_id == user.id, HungerLog.ts >= start_dt, HungerLog.ts <= end_dt
        )
    )
    hungers = [h.hunger for h in res.scalars() if h.hunger is not None]
    if hungers:
        stats.avg_hunger = round(sum(hungers) / len(hungers), 1)

    return stats


async def daily_facts(session: AsyncSession, user: User, day: date | None = None) -> dict:
    """Today's raw facts for the daily report / evening check-in."""
    day = day or date.today()
    start_dt, end_dt = _day_bounds(day)

    res = await session.execute(
        select(MealLog).where(MealLog.user_id == user.id, MealLog.ts >= start_dt, MealLog.ts < end_dt)
    )
    meals = list(res.scalars())
    res = await session.execute(
        select(WaterLog).where(WaterLog.user_id == user.id, WaterLog.ts >= start_dt, WaterLog.ts < end_dt)
    )
    water = sum(w.amount_ml for w in res.scalars())
    res = await session.execute(
        select(WeightLog).where(WeightLog.user_id == user.id, WeightLog.ts >= start_dt, WeightLog.ts < end_dt)
    )
    weight = [w.weight_kg for w in res.scalars()]
    targets = await get_current_targets(session, user.id)

    return {
        "date": day.isoformat(),
        "meals": [
            {"desc": m.description, "kcal": m.kcal, "protein_g": m.protein_g, "is_cheat": m.is_cheat}
            for m in meals
        ],
        "kcal_total": sum(m.kcal or 0 for m in meals),
        "protein_total": round(sum(m.protein_g or 0 for m in meals)),
        "water_ml": water,
        "weight_kg": weight[-1] if weight else None,
        "targets": {
            "kcal": targets.kcal if targets else None,
            "protein_g": targets.protein_g if targets else None,
            "water_ml": targets.water_ml if targets else None,
        },
    }


def format_weekly_stats_tr(stats: WeeklyStats) -> str:
    """Deterministic Turkish fact block (used as fallback and as AI grounding)."""
    lines = ["📊 Haftalık Veriler"]
    if stats.weight_change_kg_per_week is not None:
        lines.append(
            f"• Kilo değişimi: {stats.weight_change_kg_per_week:+.2f} kg/hafta"
            + (f" (%{stats.weight_change_pct_per_week:+.2f})" if stats.weight_change_pct_per_week is not None else "")
        )
    if stats.fat_pct_change_per_week is not None:
        lines.append(f"• Yağ oranı değişimi: {stats.fat_pct_change_per_week:+.2f} puan/hafta")
    if stats.muscle_kg_change_per_week is not None:
        lines.append(f"• Kas kütlesi değişimi: {stats.muscle_kg_change_per_week:+.2f} kg/hafta")
    if stats.plateau:
        lines.append("• ⚠️ Plato: kilo 2 haftadır sabit")
    if stats.water_retention_suspected:
        lines.append("• 💧 Ani artış — muhtemel su tutulumu")
    if stats.avg_kcal_logged is not None:
        lines.append(f"• Ortalama kalori: {stats.avg_kcal_logged} / hedef {stats.kcal_target or '—'} kcal")
    if stats.avg_protein_logged is not None:
        lines.append(f"• Ortalama protein: {stats.avg_protein_logged} / hedef {stats.protein_target or '—'} g")
    if stats.avg_water_ml is not None:
        lines.append(f"• Ortalama su: {stats.avg_water_ml} / hedef {stats.water_target_ml or '—'} ml")
    lines.append(f"• Egzersiz: {stats.exercise_sessions} seans" + (f" / hedef {stats.exercise_target_sessions}" if stats.exercise_target_sessions else ""))
    if stats.avg_sleep_hours is not None:
        lines.append(f"• Ortalama uyku: {stats.avg_sleep_hours} saat")
    if stats.avg_mood is not None:
        lines.append(f"• Ruh hali: {stats.avg_mood}/5, Enerji: {stats.avg_energy or '—'}/5")
    if stats.avg_hunger is not None:
        lines.append(f"• Açlık: {stats.avg_hunger}/5")
    if stats.cheat_meals:
        lines.append(f"• Kaçamak: {stats.cheat_meals} öğün (sorun değil, dengeledik 😉)")
    scores = []
    if stats.nutrition_adherence is not None:
        scores.append(f"Beslenme %{stats.nutrition_adherence}")
    if stats.water_adherence is not None:
        scores.append(f"Su %{stats.water_adherence}")
    if stats.exercise_adherence is not None:
        scores.append(f"Egzersiz %{stats.exercise_adherence}")
    if scores:
        lines.append("• Uyum skorları: " + " | ".join(scores))
    return "\n".join(lines)
