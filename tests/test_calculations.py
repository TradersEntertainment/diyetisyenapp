"""Unit tests for the deterministic nutrition math — especially the protein floor invariant."""
import pytest

from app.services.calculations import (
    GOAL_GAIN,
    GOAL_LOSE,
    GOAL_MAINTAIN,
    bmi,
    bmr_katch_mcardle,
    bmr_mifflin,
    compute_targets,
    lean_body_mass_kg,
    navy_body_fat_pct,
    protein_floor_g,
    tdee,
    water_target_ml,
)


def test_bmi():
    assert bmi(80, 180) == 24.7


def test_bmr_mifflin_male():
    # 80kg, 180cm, 30y male: 10*80 + 6.25*180 - 5*30 + 5 = 1780
    assert bmr_mifflin(80, 180, 30, "erkek") == pytest.approx(1780)


def test_bmr_mifflin_female():
    # 65kg, 165cm, 30y female: 10*65 + 6.25*165 - 5*30 - 161 = 1370.25
    assert bmr_mifflin(65, 165, 30, "kadin") == pytest.approx(1370.25)


def test_katch_mcardle_uses_lbm():
    lbm = lean_body_mass_kg(80, 20)  # 64 kg
    assert lbm == pytest.approx(64)
    assert bmr_katch_mcardle(64) == pytest.approx(370 + 21.6 * 64)


def test_tdee_multiplier():
    assert tdee(1780, "orta_aktif") == pytest.approx(1780 * 1.55)


def test_protein_floor_with_body_fat_cutting():
    # 80kg at 20% -> LBM 64kg, cutting -> 2.2 g/kg LBM = 140.8 -> 141
    assert protein_floor_g(80, 20, GOAL_LOSE) == 141


def test_protein_floor_with_body_fat_maintain():
    # LBM 64, non-cut -> 2.0 g/kg -> 128
    assert protein_floor_g(80, 20, GOAL_MAINTAIN) == 128


def test_protein_floor_without_body_fat():
    assert protein_floor_g(80, None, GOAL_LOSE) == 160  # 2.0 g/kg bw
    assert protein_floor_g(80, None, GOAL_GAIN) == 144  # 1.8
    assert protein_floor_g(80, None, GOAL_MAINTAIN) == 128  # 1.6


def test_navy_body_fat_male_reasonable():
    bf = navy_body_fat_pct("erkek", 180, 90, 40)
    assert bf is not None
    assert 10 < bf < 30


def test_navy_body_fat_invalid_returns_none():
    assert navy_body_fat_pct("erkek", 180, 40, 40) is None  # waist <= neck


def test_water_target_scales_with_weight_and_exercise():
    assert water_target_ml(80, 0) == pytest.approx(round(80 * 33 / 50) * 50)
    assert water_target_ml(80, 4) > water_target_ml(80, 0)


# --- The core invariant: protein target never drops below the floor ---


def test_compute_targets_meets_floor():
    t = compute_targets(
        weight_kg=80, height_cm=180, age=30, gender="erkek",
        activity_level="orta_aktif", primary_goal=GOAL_LOSE, body_fat_pct=20,
    )
    assert t.protein_g >= t.protein_floor_g
    assert t.protein_g == t.protein_floor_g == 141


def test_protein_override_below_floor_is_raised():
    t = compute_targets(
        weight_kg=80, height_cm=180, age=30, gender="erkek",
        activity_level="orta_aktif", primary_goal=GOAL_LOSE, body_fat_pct=20,
        protein_override_g=50,  # absurdly low
    )
    assert t.protein_g == t.protein_floor_g  # clamped up to floor, not 50


def test_protein_override_above_floor_is_kept():
    t = compute_targets(
        weight_kg=80, height_cm=180, age=30, gender="erkek",
        activity_level="orta_aktif", primary_goal=GOAL_LOSE, body_fat_pct=20,
        protein_override_g=170,
    )
    assert t.protein_g == 170


def test_low_kcal_override_still_holds_protein_and_fat():
    """Even a starvation-level kcal override must leave room for protein floor + min fat."""
    t = compute_targets(
        weight_kg=80, height_cm=180, age=30, gender="erkek",
        activity_level="sedanter", primary_goal=GOAL_LOSE, body_fat_pct=20,
        kcal_override=800,
    )
    assert t.protein_g >= t.protein_floor_g
    # kcal was bumped up so protein*4 + minfat*9 fits
    assert t.kcal >= t.protein_g * 4 + round(80 * 0.8) * 9
    assert t.carb_g >= 0


def test_macros_sum_close_to_kcal():
    t = compute_targets(
        weight_kg=70, height_cm=170, age=35, gender="kadin",
        activity_level="hafif_aktif", primary_goal=GOAL_MAINTAIN,
    )
    macro_kcal = t.protein_g * 4 + t.carb_g * 4 + t.fat_g * 9
    assert abs(macro_kcal - t.kcal) <= 60  # rounding tolerance


# --- High-BMI conservative corrections ---


def test_high_bmi_gets_conservative_target():
    """152 kg / 180 cm: activity is stepped down one level and the deficit deepens
    to 25%, so the target lands in a realistic dietitian range instead of ~3200."""
    t = compute_targets(
        weight_kg=152, height_cm=180, age=30, gender="erkek",
        activity_level="orta_aktif", primary_goal=GOAL_LOSE,
    )
    assert 2300 <= t.kcal <= 2750
    assert t.protein_g >= t.protein_floor_g


def test_normal_bmi_keeps_standard_deficit():
    from app.services.calculations import bmr, calorie_target, tdee

    t = compute_targets(
        weight_kg=70, height_cm=180, age=30, gender="erkek",
        activity_level="orta_aktif", primary_goal=GOAL_LOSE,
    )
    expected = calorie_target(tdee(bmr(70, 180, 30, "erkek"), "orta_aktif"), GOAL_LOSE, "erkek", 21.6)
    assert t.kcal == expected


def test_effective_activity_level_only_drops_at_high_bmi():
    from app.services.calculations import effective_activity_level

    assert effective_activity_level("orta_aktif", 25) == "orta_aktif"
    assert effective_activity_level("orta_aktif", 33) == "hafif_aktif"
    assert effective_activity_level("sedanter", 40) == "sedanter"
