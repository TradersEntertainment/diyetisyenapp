"""Deterministic nutrition math.

Core principle of this application: the PROTEIN TARGET IS AN INVARIANT ANCHOR.
It is always derived from body analysis (lean body mass when body fat % is
known, otherwise body weight scaled by goal) via `protein_floor_g`, and no
adaptive adjustment, AI decision or meal plan is allowed to go below it.
Calorie changes are always absorbed by carbohydrates and fat.
"""
import math
from dataclasses import dataclass

ACTIVITY_MULTIPLIERS = {
    "sedanter": 1.2,
    "hafif_aktif": 1.375,
    "orta_aktif": 1.55,
    "aktif": 1.725,
    "cok_aktif": 1.9,
}

GOAL_LOSE = "yag_kaybi"
GOAL_GAIN = "kas_kazanimi"
GOAL_MAINTAIN = "kilo_koruma"

MIN_KCAL_FEMALE = 1200
MIN_KCAL_MALE = 1500


def bmi(weight_kg: float, height_cm: float) -> float:
    h = height_cm / 100
    return round(weight_kg / (h * h), 1)


def lean_body_mass_kg(weight_kg: float, body_fat_pct: float) -> float:
    return weight_kg * (1 - body_fat_pct / 100)


def bmr_mifflin(weight_kg: float, height_cm: float, age: int, gender: str) -> float:
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base + (5 if gender == "erkek" else -161)


def bmr_katch_mcardle(lbm_kg: float) -> float:
    return 370 + 21.6 * lbm_kg


def bmr(
    weight_kg: float,
    height_cm: float,
    age: int,
    gender: str,
    body_fat_pct: float | None = None,
) -> float:
    """Katch-McArdle when body composition is known, otherwise Mifflin-St Jeor."""
    if body_fat_pct is not None and 3 <= body_fat_pct <= 60:
        return bmr_katch_mcardle(lean_body_mass_kg(weight_kg, body_fat_pct))
    return bmr_mifflin(weight_kg, height_cm, age, gender)


def tdee(bmr_value: float, activity_level: str) -> float:
    return bmr_value * ACTIVITY_MULTIPLIERS.get(activity_level, 1.375)


_ACTIVITY_ORDER = ["sedanter", "hafif_aktif", "orta_aktif", "aktif", "cok_aktif"]


def effective_activity_level(activity_level: str, bmi_value: float) -> str:
    """Conservative activity level used for TDEE.

    At high BMI the standard multipliers inflate TDEE and self-reported
    activity tends to be optimistic, which yields calorie targets too high to
    actually lose weight on. BMI >= 30 drops the level one step.
    """
    if bmi_value < 30 or activity_level not in _ACTIVITY_ORDER:
        return activity_level
    idx = _ACTIVITY_ORDER.index(activity_level)
    return _ACTIVITY_ORDER[max(0, idx - 1)]


def navy_body_fat_pct(
    gender: str, height_cm: float, waist_cm: float, neck_cm: float, hip_cm: float | None = None
) -> float | None:
    """US Navy circumference method. Returns None if inputs are unusable."""
    try:
        if gender == "erkek":
            if waist_cm <= neck_cm:
                return None
            val = 495 / (
                1.0324 - 0.19077 * math.log10(waist_cm - neck_cm) + 0.15456 * math.log10(height_cm)
            ) - 450
        else:
            if hip_cm is None or (waist_cm + hip_cm) <= neck_cm:
                return None
            val = 495 / (
                1.29579
                - 0.35004 * math.log10(waist_cm + hip_cm - neck_cm)
                + 0.22100 * math.log10(height_cm)
            ) - 450
    except (ValueError, ZeroDivisionError):
        return None
    if val <= 0 or val >= 70:
        return None
    return round(val, 1)


def protein_floor_g(
    weight_kg: float,
    body_fat_pct: float | None,
    primary_goal: str,
) -> int:
    """The invariant protein floor, from body analysis.

    - Body fat known  -> grams per kg of LEAN BODY MASS: 2.2 cutting, 2.0 otherwise.
    - Body fat unknown -> grams per kg of body weight: 2.0 cutting, 1.8 gaining, 1.6 maintaining.

    Nothing in the system may set a protein target below this value.
    """
    if body_fat_pct is not None and 3 <= body_fat_pct <= 60:
        lbm = lean_body_mass_kg(weight_kg, body_fat_pct)
        per_kg = 2.2 if primary_goal == GOAL_LOSE else 2.0
        return round(lbm * per_kg)
    per_kg = {GOAL_LOSE: 2.0, GOAL_GAIN: 1.8}.get(primary_goal, 1.6)
    return round(weight_kg * per_kg)


def calorie_target(tdee_value: float, primary_goal: str, gender: str, bmi_value: float | None = None) -> int:
    """Goal-based calorie target with safety floors.

    The fat-loss deficit deepens with BMI: higher body fat safely supports a
    larger deficit, and at high weights a flat 20% leaves the target too close
    to real-world maintenance to reliably lose on.
    """
    if primary_goal == GOAL_LOSE:
        deficit = 0.20  # -> roughly 0.5-1% bodyweight/week
        if bmi_value is not None:
            if bmi_value >= 32:
                deficit = 0.25
            elif bmi_value >= 27:
                deficit = 0.22
        kcal = tdee_value * (1 - deficit)
    elif primary_goal == GOAL_GAIN:
        kcal = tdee_value * 1.10
    else:
        kcal = tdee_value
    floor = MIN_KCAL_MALE if gender == "erkek" else MIN_KCAL_FEMALE
    return round(max(kcal, floor))


KCAL_PER_KG_FAT = 7700


def kcal_for_weekly_loss(tdee_value: float, weekly_kg: float, gender: str) -> int:
    """Daily calorie target that yields ~weekly_kg of fat loss per week."""
    kcal = tdee_value - weekly_kg * KCAL_PER_KG_FAT / 7
    floor = MIN_KCAL_MALE if gender == "erkek" else MIN_KCAL_FEMALE
    return round(max(kcal, floor))


def max_safe_weekly_loss_kg(weight_kg: float) -> float:
    """Sustainable ceiling: ~1% of bodyweight per week."""
    return round(weight_kg * 0.01, 2)


def water_target_ml(weight_kg: float, exercise_days_per_week: int | None = None) -> int:
    ml = weight_kg * 33
    if exercise_days_per_week and exercise_days_per_week >= 3:
        ml += 500
    return round(ml / 50) * 50


def fiber_target_g(kcal: int) -> int:
    return max(20, round(kcal / 1000 * 14))


@dataclass
class Targets:
    kcal: int
    protein_g: int
    fat_g: int
    carb_g: int
    fiber_g: int
    water_ml: int
    protein_floor_g: int  # carried along so downstream consumers can re-check the invariant


def compute_targets(
    *,
    weight_kg: float,
    height_cm: float,
    age: int,
    gender: str,
    activity_level: str,
    primary_goal: str,
    body_fat_pct: float | None = None,
    exercise_days_per_week: int | None = None,
    kcal_override: int | None = None,
    protein_override_g: int | None = None,
) -> Targets:
    """Compute a full macro target set around the protein floor.

    `kcal_override` / `protein_override_g` let the adaptive engine adjust intake,
    but the protein floor is ALWAYS enforced: overrides below the floor are raised
    to it, and calories always leave room for the floor + minimum fat.
    """
    floor = protein_floor_g(weight_kg, body_fat_pct, primary_goal)

    bmi_value = bmi(weight_kg, height_cm)
    bmr_value = bmr(weight_kg, height_cm, age, gender, body_fat_pct)
    tdee_value = tdee(bmr_value, effective_activity_level(activity_level, bmi_value))
    kcal = (
        kcal_override
        if kcal_override is not None
        else calorie_target(tdee_value, primary_goal, gender, bmi_value)
    )

    protein = max(floor, protein_override_g or 0)

    # Minimum healthy fat: 0.8 g per kg body weight.
    min_fat = round(weight_kg * 0.8)

    # Guarantee the calorie budget can hold protein floor + minimum fat.
    min_kcal_needed = protein * 4 + min_fat * 9
    kcal = max(kcal, min_kcal_needed + 100)  # +100 kcal leaves at least some carbs

    remaining = kcal - protein * 4
    fat = max(min_fat, round(remaining * 0.35 / 9))  # ~35% of non-protein kcal from fat
    carb = max(0, round((kcal - protein * 4 - fat * 9) / 4))

    return Targets(
        kcal=kcal,
        protein_g=protein,
        fat_g=fat,
        carb_g=carb,
        fiber_g=fiber_target_g(kcal),
        water_ml=water_target_ml(weight_kg, exercise_days_per_week),
        protein_floor_g=floor,
    )
