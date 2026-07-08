"""User lifecycle admin helpers (used by the management panel).

Deleting a User requires removing every dependent row first — none of the
foreign keys in this schema cascade at the DB level, so a naive
`session.delete(user)` would fail with an IntegrityError the moment the user
has any history at all.
"""
from sqlalchemy import delete, select

from app.models import (
    BodyCompositionLog,
    BodyMeasurement,
    ConversationMessage,
    ExerciseLog,
    FoodPreference,
    HungerLog,
    MealLog,
    MealPlan,
    MemoryNote,
    MoodLog,
    PlannedMeal,
    Profile,
    ProgressPhoto,
    ReminderSetting,
    SleepLog,
    StepsLog,
    TargetHistory,
    User,
    WaterLog,
    WeightLog,
)

# Simple per-user_id tables — deleted directly, no further dependents.
_DIRECT_TABLES = [
    ConversationMessage,
    MemoryNote,
    FoodPreference,
    TargetHistory,
    WeightLog,
    BodyCompositionLog,
    BodyMeasurement,
    WaterLog,
    StepsLog,
    ExerciseLog,
    SleepLog,
    MoodLog,
    HungerLog,
    ProgressPhoto,
    ReminderSetting,
]


async def delete_user_cascade(session, user_id: int) -> None:
    # MealLog may point at a PlannedMeal (nullable) -> clear first so the FK
    # doesn't block deleting that meal plan's rows below.
    res = await session.execute(select(MealLog).where(MealLog.user_id == user_id))
    for m in res.scalars():
        m.planned_meal_id = None
    await session.flush()
    await session.execute(delete(MealLog).where(MealLog.user_id == user_id))

    res = await session.execute(select(MealPlan).where(MealPlan.user_id == user_id))
    plan_ids = [p.id for p in res.scalars()]
    if plan_ids:
        await session.execute(delete(PlannedMeal).where(PlannedMeal.plan_id.in_(plan_ids)))
        await session.execute(delete(MealPlan).where(MealPlan.user_id == user_id))

    for table in _DIRECT_TABLES:
        await session.execute(delete(table).where(table.user_id == user_id))

    await session.execute(delete(Profile).where(Profile.user_id == user_id))

    user = await session.get(User, user_id)
    if user:
        await session.delete(user)
