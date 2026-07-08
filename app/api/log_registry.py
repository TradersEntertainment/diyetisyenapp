"""Maps a log "kind" (as used in the panel URLs) to its model + editable fields.

Used by the generic log CRUD endpoints in routes.py so one set of handlers
covers weight/water/meal/sleep/mood/hunger/exercise/body-composition logs.
"""
from app.models import (
    BodyCompositionLog,
    BodyMeasurement,
    ExerciseLog,
    HungerLog,
    MealLog,
    MoodLog,
    SleepLog,
    WaterLog,
    WeightLog,
)

LOG_REGISTRY: dict[str, dict] = {
    "weight": {"model": WeightLog, "fields": ["weight_kg"]},
    "water": {"model": WaterLog, "fields": ["amount_ml"]},
    "meal": {
        "model": MealLog,
        "fields": ["description", "kcal", "protein_g", "carb_g", "fat_g", "fiber_g", "slot", "is_cheat"],
    },
    "sleep": {"model": SleepLog, "fields": ["hours", "quality"]},
    "mood": {"model": MoodLog, "fields": ["mood", "stress", "energy", "note"]},
    "hunger": {"model": HungerLog, "fields": ["hunger", "craving"]},
    "exercise": {"model": ExerciseLog, "fields": ["activity", "duration_min", "intensity", "note"]},
    "bodycomp": {"model": BodyCompositionLog, "fields": ["body_fat_pct", "muscle_mass_kg"]},
    "measurement": {"model": BodyMeasurement, "fields": ["waist_cm", "hip_cm", "neck_cm"]},
}


def get_kind(kind: str) -> dict:
    entry = LOG_REGISTRY.get(kind)
    if not entry:
        raise KeyError(kind)
    return entry
