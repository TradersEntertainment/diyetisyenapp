"""Pydantic request bodies for the management panel's write endpoints."""
from pydantic import BaseModel


class UserUpdate(BaseModel):
    name: str | None = None
    onboarding_state: str | None = None  # "active" | "paused"


class UserCreate(BaseModel):
    telegram_id: int
    name: str = ""


class ProfileUpdate(BaseModel):
    age: int | None = None
    gender: str | None = None
    height_cm: float | None = None
    start_weight_kg: float | None = None
    goal_weight_kg: float | None = None
    body_fat_pct: float | None = None
    muscle_mass_kg: float | None = None
    waist_cm: float | None = None
    hip_cm: float | None = None
    neck_cm: float | None = None
    activity_level: str | None = None
    occupation: str | None = None
    daily_movement: str | None = None
    sleep_hours: float | None = None
    diseases: str | None = None
    has_diabetes: bool | None = None
    has_thyroid: bool | None = None
    has_insulin_resistance: bool | None = None
    has_hypertension: bool | None = None
    has_cholesterol: bool | None = None
    has_digestive_issues: bool | None = None
    allergies: str | None = None
    intolerances: str | None = None
    medications: str | None = None
    supplements: str | None = None
    surgeries: str | None = None
    goals: list[str] | None = None
    exercise_types: list[str] | None = None
    exercise_frequency_per_week: int | None = None
    exercise_duration_min: int | None = None
    eats_breakfast: bool | None = None
    eats_lunch: bool | None = None
    eats_dinner: bool | None = None
    eats_snacks: bool | None = None
    eats_outside: str | None = None
    coffee_per_day: int | None = None
    tea_per_day: int | None = None
    water_glasses_per_day: int | None = None
    alcohol: str | None = None
    smoking: str | None = None
    cooking_skill: str | None = None
    kitchen_equipment: list[str] | None = None
    monthly_food_budget: str | None = None
    shopping_preferences: str | None = None


class TargetOverride(BaseModel):
    kcal: int | None = None
    protein_g: int | None = None
    carb_g: int | None = None
    fat_g: int | None = None
    fiber_g: int | None = None
    water_ml: int | None = None
    diet_strategy: str | None = None
    reason: str | None = None


class LogUpdate(BaseModel):
    """Generic partial update — only keys valid for the log's `kind` are applied."""

    fields: dict


class LogCreate(BaseModel):
    fields: dict


class PreferenceUpsert(BaseModel):
    name: str
    level: str
    category: str = "genel"


class ReminderUpdate(BaseModel):
    time: str | None = None  # "HH:MM"
    enabled: bool | None = None


class ReminderCreate(BaseModel):
    kind: str
    time: str  # "HH:MM"
    enabled: bool = True


class ShoppingItemUpdate(BaseModel):
    checked: bool
