"""Onboarding structural tests: every mandated question is present, and the
questionnaire covers all required Profile fields."""
from app.bot.onboarding import KEY_INDEX, PROFILE_FIELDS, QUESTIONS


def test_all_questions_have_unique_keys():
    keys = [q.key for q in QUESTIONS]
    assert len(keys) == len(set(keys))


def test_basic_questions_present():
    for key in ["name", "age", "gender", "height_cm", "start_weight_kg", "goal_weight_kg",
                "body_fat_pct", "muscle_mass_kg", "waist_cm", "hip_cm", "neck_cm",
                "activity_level", "occupation", "daily_movement", "sleep_hours"]:
        assert key in KEY_INDEX, f"missing basic question: {key}"


def test_health_questions_present():
    for key in ["health_flags", "diseases", "allergies", "intolerances",
                "medications", "supplements", "surgeries"]:
        assert key in KEY_INDEX, f"missing health question: {key}"


def test_preference_questions_cover_all_levels():
    levels = {q.pref_level for q in QUESTIONS if q.kind == "pref_list"}
    assert {"bayilirim", "severim", "yiyebilirim", "sevmem", "asla"} <= levels


def test_preference_categories_present():
    cats = {q.pref_category for q in QUESTIONS if q.kind == "pref_list"}
    for c in ["mutfak", "sebze", "meyve", "et", "tatli", "icecek",
              "atistirmalik", "kahvalti", "aksam", "restoran", "kacamak"]:
        assert c in cats, f"missing preference category: {c}"


def test_kitchen_budget_shopping_questions_present():
    for key in ["cooking_skill", "kitchen_equipment", "monthly_food_budget", "shopping_preferences"]:
        assert key in KEY_INDEX


def test_exercise_and_habit_questions_present():
    for key in ["exercise_types", "exercise_frequency_per_week", "exercise_duration_min",
                "meals_eaten", "eats_outside", "coffee_per_day", "tea_per_day",
                "water_glasses_per_day", "alcohol", "smoking", "goals"]:
        assert key in KEY_INDEX


def test_profile_fields_are_valid_columns():
    from app.models import Profile

    cols = set(Profile.__table__.columns.keys())
    for f in PROFILE_FIELDS:
        assert f in cols, f"PROFILE_FIELDS has unknown column: {f}"
