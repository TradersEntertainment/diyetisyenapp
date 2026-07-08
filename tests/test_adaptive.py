"""Tests for the adaptive engine — every spec branch + the protein-floor invariant."""
from app.services.adaptive import (
    apply_adjustments,
    decide_adjustments,
    spread_cheat_surplus,
)
from app.services.analysis import WeeklyStats
from app.services.calculations import GOAL_GAIN, GOAL_LOSE

BODY = dict(
    weight_kg=80, height_cm=180, age=30, gender="erkek",
    activity_level="orta_aktif", body_fat_pct=20, exercise_days_per_week=4,
)


def test_too_fast_loss_adds_calories():
    stats = WeeklyStats(weight_change_pct_per_week=-1.5)
    d = decide_adjustments(stats, current_kcal=2000, primary_goal=GOAL_LOSE)
    assert d.kcal_delta == 175


def test_plateau_reduces_calories_and_nudges_activity():
    stats = WeeklyStats(plateau=True, weight_change_pct_per_week=-0.05)
    d = decide_adjustments(stats, current_kcal=2000, primary_goal=GOAL_LOSE)
    assert d.kcal_delta == -200  # 10% of 2000
    assert d.activity_nudge is True


def test_water_retention_blocks_no_change_flag():
    stats = WeeklyStats(plateau=True, water_retention_suspected=True)
    d = decide_adjustments(stats, current_kcal=2000, primary_goal=GOAL_LOSE)
    # plateau rule is skipped when water retention suspected
    assert d.kcal_delta == 0


def test_muscle_loss_raises_protein_bonus():
    stats = WeeklyStats(muscle_kg_change_per_week=-0.2)
    d = decide_adjustments(stats, current_kcal=2000, primary_goal=GOAL_LOSE)
    assert d.protein_bonus_g == 15


def test_low_energy_sets_carb_bias():
    stats = WeeklyStats(avg_energy=2.0)
    d = decide_adjustments(stats, current_kcal=2000, primary_goal=GOAL_LOSE)
    assert d.carb_bias is True


def test_high_hunger_adds_fiber_and_volume():
    stats = WeeklyStats(avg_hunger=4.5)
    d = decide_adjustments(stats, current_kcal=2000, primary_goal=GOAL_LOSE)
    assert d.fiber_bonus_g == 5
    assert d.volume_bias is True


def test_gaining_too_fast_trims_calories():
    stats = WeeklyStats(weight_change_pct_per_week=0.8)
    d = decide_adjustments(stats, current_kcal=2500, primary_goal=GOAL_GAIN)
    assert d.kcal_delta == -100


def test_apply_adjustments_never_drops_protein_below_floor():
    """Big calorie cut must not touch protein — it comes off carbs/fat."""
    stats = WeeklyStats(plateau=True, weight_change_pct_per_week=-0.02)
    d = decide_adjustments(stats, current_kcal=2200, primary_goal=GOAL_LOSE)
    targets = apply_adjustments(d, current_kcal=2200, primary_goal=GOAL_LOSE, **BODY)
    assert targets.protein_g >= targets.protein_floor_g


def test_apply_adjustments_muscle_loss_raises_protein_above_floor():
    stats = WeeklyStats(muscle_kg_change_per_week=-0.3)
    d = decide_adjustments(stats, current_kcal=2200, primary_goal=GOAL_LOSE)
    targets = apply_adjustments(d, current_kcal=2200, primary_goal=GOAL_LOSE, **BODY)
    assert targets.protein_g == targets.protein_floor_g + 15


def test_extreme_deficit_still_holds_floor():
    """Stack plateau cuts repeatedly; protein floor must hold every time."""
    kcal = 2500
    for _ in range(5):
        stats = WeeklyStats(plateau=True, weight_change_pct_per_week=-0.02)
        d = decide_adjustments(stats, current_kcal=kcal, primary_goal=GOAL_LOSE)
        targets = apply_adjustments(d, current_kcal=kcal, primary_goal=GOAL_LOSE, **BODY)
        assert targets.protein_g >= targets.protein_floor_g
        kcal = targets.kcal


def test_cheat_surplus_spread_no_punishment():
    cuts = spread_cheat_surplus(900, days=3)
    assert cuts == [250, 250, 250]  # capped at 250, not 300
    assert all(c <= 250 for c in cuts)


def test_cheat_surplus_zero():
    assert spread_cheat_surplus(0) == [0, 0, 0]
