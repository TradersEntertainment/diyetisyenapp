"""Builds the per-user context block injected into every Claude call.

This is how the assistant "remembers everything": profile, current targets
(with the protein floor), recent stats, today's plan, learned preferences and
long-term memory notes are all rendered into one grounding document.
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    FoodPreference,
    MealPlan,
    MemoryNote,
    PlannedMeal,
    Profile,
    User,
)
from app.services.calculations import protein_floor_g
from app.services.reports import (
    daily_facts,
    gather_weekly_stats,
    get_current_targets,
    logging_streak_days,
)
from app.services.targets import get_profile, latest_body_fat, latest_weight, primary_goal_of

LEVEL_LABELS = {
    "bayilirim": "bayıldıkları",
    "severim": "sevdikleri",
    "yiyebilirim": "yiyebildikleri",
    "sevmem": "sevmedikleri",
    "asla": "ASLA yemeyecekleri",
}


def _profile_text(profile: Profile, name: str) -> str:
    parts = [f"İsim: {name}"]
    if profile.age:
        parts.append(f"Yaş: {profile.age}")
    if profile.gender:
        parts.append(f"Cinsiyet: {profile.gender}")
    if profile.height_cm:
        parts.append(f"Boy: {profile.height_cm:g} cm")
    if profile.goal_weight_kg:
        parts.append(f"Hedef kilo: {profile.goal_weight_kg:g} kg")
    if profile.activity_level:
        parts.append(f"Aktivite: {profile.activity_level}")
    if profile.occupation:
        parts.append(f"Meslek: {profile.occupation}")
    if profile.wake_time:
        parts.append(f"Uyanma saati: {profile.wake_time.strftime('%H:%M')}")
    if profile.goals:
        parts.append("Hedefler: " + ", ".join(profile.goals))
    health = []
    for flag, label in [
        (profile.has_diabetes, "diyabet"),
        (profile.has_thyroid, "tiroid"),
        (profile.has_insulin_resistance, "insülin direnci"),
        (profile.has_hypertension, "hipertansiyon"),
        (profile.has_cholesterol, "kolesterol"),
        (profile.has_digestive_issues, "sindirim sorunları"),
    ]:
        if flag:
            health.append(label)
    if profile.diseases:
        health.append(profile.diseases)
    if health:
        parts.append("Sağlık: " + ", ".join(health))
    if profile.allergies:
        parts.append(f"Alerjiler: {profile.allergies}")
    if profile.intolerances:
        parts.append(f"İntoleranslar: {profile.intolerances}")
    if profile.medications:
        parts.append(f"İlaçlar: {profile.medications}")
    if profile.supplements:
        parts.append(f"Takviyeler: {profile.supplements}")
    if profile.exercise_types:
        freq = f", haftada {profile.exercise_frequency_per_week}x" if profile.exercise_frequency_per_week else ""
        parts.append("Egzersiz: " + ", ".join(profile.exercise_types) + freq)
    if profile.cooking_skill:
        parts.append(f"Mutfak becerisi: {profile.cooking_skill}")
    if profile.kitchen_equipment:
        parts.append("Mutfak ekipmanı: " + ", ".join(profile.kitchen_equipment))
    if profile.monthly_food_budget:
        parts.append(f"Aylık gıda bütçesi: {profile.monthly_food_budget}")
    if profile.shopping_preferences:
        parts.append(f"Alışveriş tercihi: {profile.shopping_preferences}")
    return "\n".join(parts)


async def _preferences_text(session: AsyncSession, user_id: int) -> str:
    res = await session.execute(
        select(FoodPreference).where(FoodPreference.user_id == user_id).order_by(FoodPreference.level)
    )
    prefs = list(res.scalars())
    if not prefs:
        return ""
    by_level: dict[str, list[str]] = {}
    for p in prefs:
        by_level.setdefault(p.level, []).append(p.name)
    lines = []
    for level in ["asla", "sevmem", "bayilirim", "severim", "yiyebilirim"]:
        if level in by_level:
            lines.append(f"- {LEVEL_LABELS.get(level, level)}: {', '.join(by_level[level])}")
    return "\n".join(lines)


async def _memory_text(session: AsyncSession, user_id: int, limit: int = 40) -> str:
    res = await session.execute(
        select(MemoryNote)
        .where(MemoryNote.user_id == user_id, MemoryNote.active.is_(True))
        .order_by(MemoryNote.ts.desc())
        .limit(limit)
    )
    notes = list(res.scalars())
    if not notes:
        return ""
    return "\n".join(f"- [{n.category}] {n.text}" for n in reversed(notes))


async def _today_plan_text(session: AsyncSession, user_id: int, today: date) -> str:
    week_start = today - timedelta(days=today.weekday())
    res = await session.execute(
        select(MealPlan)
        .where(MealPlan.user_id == user_id, MealPlan.week_start == week_start, MealPlan.status == "active")
        .order_by(MealPlan.id.desc())
        .limit(1)
    )
    plan = res.scalar_one_or_none()
    if not plan:
        return ""
    res = await session.execute(
        select(PlannedMeal).where(
            PlannedMeal.plan_id == plan.id, PlannedMeal.day_index == today.weekday()
        )
    )
    meals = list(res.scalars())
    if not meals:
        return ""
    lines = [f"Bugünün planı (strateji: {plan.diet_strategy}):"]
    for m in meals:
        lines.append(f"- {m.slot}: {m.name} ({m.kcal} kcal, P{m.protein_g:g} K{m.carb_g:g} Y{m.fat_g:g})")
    return "\n".join(lines)


async def _partner_brief(session: AsyncSession, user: User, today: date) -> str:
    """One-paragraph summary of the housemate, so the dietitian can reason about
    shared dinners and address both people naturally in the group."""
    res = await session.execute(
        select(User).where(User.id != user.id, User.onboarding_state == "active")
    )
    partner = res.scalars().first()
    if not partner:
        return ""
    parts = [f"İsim: {partner.name or 'bilinmiyor'}"]
    profile = await get_profile(session, partner.id)
    if profile:
        goal = primary_goal_of(profile)
        if goal:
            parts.append(f"ana hedefi: {goal}")
    weight = await latest_weight(session, partner.id)
    if weight:
        parts.append(f"güncel kilo: {weight:g} kg")
    facts = await daily_facts(session, partner, today)
    parts.append(f"bugün: {facts['kcal_total']} kcal, {facts['water_ml']} ml su")
    return ", ".join(parts)


async def build_user_context(session: AsyncSession, user: User) -> str:
    """The full grounding document for one user."""
    now = datetime.now(timezone.utc)
    today = date.today()
    sections: list[str] = [f"Tarih: {today.isoformat()} ({now.strftime('%H:%M')} UTC)"]

    profile = await get_profile(session, user.id)
    if profile:
        sections.append("## Profil\n" + _profile_text(profile, user.name))

        weight = await latest_weight(session, user.id) or profile.start_weight_kg
        body_fat = await latest_body_fat(session, user.id) or profile.body_fat_pct
        if weight:
            floor = protein_floor_g(weight, body_fat, primary_goal_of(profile))
            line = f"Güncel kilo: {weight:g} kg"
            if body_fat:
                line += f", yağ oranı: %{body_fat:g}"
            line += f"\nPROTEİN TABANI (değişmez alt sınır): {floor} g/gün"
            sections.append("## Vücut Analizi\n" + line)

    targets = await get_current_targets(session, user.id)
    if targets:
        sections.append(
            "## Güncel Hedefler\n"
            f"Kalori: {targets.kcal} kcal | Protein: {targets.protein_g} g | "
            f"Karbonhidrat: {targets.carb_g} g | Yağ: {targets.fat_g} g | Lif: {targets.fiber_g} g | "
            f"Su: {targets.water_ml} ml\nDiyet stratejisi: {targets.diet_strategy}"
            + (f"\nSon değişiklik gerekçesi: {targets.reason}" if targets.reason else "")
        )

    facts = await daily_facts(session, user, today)
    streak = await logging_streak_days(session, user.id)
    sections.append(
        "## Bugün\n"
        f"Kalori: {facts['kcal_total']} kcal, Protein: {facts['protein_total']} g, "
        f"Su: {facts['water_ml']} ml"
        + (f", Tartı: {facts['weight_kg']:g} kg" if facts["weight_kg"] else "")
        + ("\nÖğünler: " + "; ".join(m["desc"] for m in facts["meals"]) if facts["meals"] else "")
        + f"\nKayıt serisi: {streak} gün üst üste"
    )

    plan_text = await _today_plan_text(session, user.id, today)
    if plan_text:
        sections.append("## Plan\n" + plan_text)

    stats = await gather_weekly_stats(session, user)
    week_lines = []
    if stats.weight_change_kg_per_week is not None:
        week_lines.append(f"Kilo trendi: {stats.weight_change_kg_per_week:+.2f} kg/hafta")
    if stats.plateau:
        week_lines.append("Plato: evet")
    if stats.avg_kcal_logged is not None:
        week_lines.append(f"Ort. kalori: {stats.avg_kcal_logged}")
    if stats.avg_protein_logged is not None:
        week_lines.append(f"Ort. protein: {stats.avg_protein_logged} g")
    if week_lines:
        sections.append("## Son 7 Gün\n" + " | ".join(week_lines))

    prefs = await _preferences_text(session, user.id)
    if prefs:
        sections.append("## Yiyecek Tercihleri\n" + prefs)

    memory = await _memory_text(session, user.id)
    if memory:
        sections.append("## Hafıza Notları\n" + memory)

    partner = await _partner_brief(session, user, today)
    if partner:
        sections.append("## Ev Arkadaşı (diğer kullanıcı)\n" + partner)

    return "\n\n".join(sections)
