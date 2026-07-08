"""Target persistence + protein-floor enforcement helpers."""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BodyCompositionLog, Profile, TargetHistory, User, WeightLog
from app.services.calculations import GOAL_LOSE, Targets, compute_targets, protein_floor_g
from app.services.reports import get_current_targets


async def latest_weight(session: AsyncSession, user_id: int) -> float | None:
    res = await session.execute(
        select(WeightLog).where(WeightLog.user_id == user_id).order_by(WeightLog.ts.desc()).limit(1)
    )
    row = res.scalar_one_or_none()
    return row.weight_kg if row else None


async def latest_body_fat(session: AsyncSession, user_id: int) -> float | None:
    since = datetime.now(timezone.utc) - timedelta(days=60)
    res = await session.execute(
        select(BodyCompositionLog)
        .where(BodyCompositionLog.user_id == user_id, BodyCompositionLog.ts >= since)
        .order_by(BodyCompositionLog.ts.desc())
        .limit(1)
    )
    row = res.scalar_one_or_none()
    return row.body_fat_pct if row else None


async def get_profile(session: AsyncSession, user_id: int) -> Profile | None:
    res = await session.execute(select(Profile).where(Profile.user_id == user_id))
    return res.scalar_one_or_none()


def primary_goal_of(profile: Profile) -> str:
    goals = profile.goals or []
    return goals[0] if goals else GOAL_LOSE


async def save_targets(
    session: AsyncSession,
    user_id: int,
    targets: Targets,
    *,
    diet_strategy: str,
    reason: str,
    effective: date | None = None,
) -> TargetHistory:
    row = TargetHistory(
        user_id=user_id,
        effective_date=effective or date.today(),
        kcal=targets.kcal,
        protein_g=targets.protein_g,
        fat_g=targets.fat_g,
        carb_g=targets.carb_g,
        fiber_g=targets.fiber_g,
        water_ml=targets.water_ml,
        diet_strategy=diet_strategy,
        reason=reason,
    )
    session.add(row)
    await session.flush()
    return row


async def compute_targets_for_user(
    session: AsyncSession,
    user: User,
    *,
    kcal_override: int | None = None,
    protein_override_g: int | None = None,
) -> Targets | None:
    """Compute targets from the user's current profile + latest logs."""
    profile = await get_profile(session, user.id)
    if not profile or not profile.height_cm or not profile.age:
        return None
    weight = await latest_weight(session, user.id) or profile.start_weight_kg
    if not weight:
        return None
    body_fat = await latest_body_fat(session, user.id)
    if body_fat is None:
        body_fat = profile.body_fat_pct
    return compute_targets(
        weight_kg=weight,
        height_cm=profile.height_cm,
        age=profile.age,
        gender=profile.gender or "kadin",
        activity_level=profile.activity_level or "hafif_aktif",
        primary_goal=primary_goal_of(profile),
        body_fat_pct=body_fat,
        exercise_days_per_week=profile.exercise_frequency_per_week,
        kcal_override=kcal_override,
        protein_override_g=protein_override_g,
    )


async def ensure_protein_floor(session: AsyncSession, user: User) -> TargetHistory | None:
    """Called after every new weight / body-composition log.

    Recomputes the body-analysis protein floor; if the current stored target has
    fallen below it, writes a corrected TargetHistory row (protein raised, kcal kept).
    """
    profile = await get_profile(session, user.id)
    current = await get_current_targets(session, user.id)
    if not profile or not current:
        return None
    weight = await latest_weight(session, user.id) or profile.start_weight_kg
    if not weight:
        return None
    body_fat = await latest_body_fat(session, user.id)
    if body_fat is None:
        body_fat = profile.body_fat_pct
    floor = protein_floor_g(weight, body_fat, primary_goal_of(profile))
    if current.protein_g >= floor:
        return None
    targets = await compute_targets_for_user(
        session, user, kcal_override=current.kcal, protein_override_g=floor
    )
    if targets is None:
        return None
    return await save_targets(
        session,
        user.id,
        targets,
        diet_strategy=current.diet_strategy,
        reason=f"Vücut analizi güncellendi: protein tabanı {floor} g'a yükseltildi.",
    )
