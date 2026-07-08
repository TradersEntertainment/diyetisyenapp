"""Tests for trend analysis: velocity, plateau, water retention, adherence scores."""
from datetime import date, timedelta

from app.services.analysis import (
    WeeklyStats,
    adherence_score,
    is_plateau,
    water_retention_suspected,
    weekly_change,
    weekly_change_pct,
)


def _series(values, start=None):
    start = start or date(2026, 1, 1)
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


def test_weekly_change_steady_loss():
    # losing 0.1 kg/day -> -0.7 kg/week
    series = _series([80 - 0.1 * i for i in range(14)])
    assert weekly_change(series) == -0.7


def test_weekly_change_pct():
    series = _series([80 - 0.1 * i for i in range(14)])
    pct = weekly_change_pct(series)
    assert pct is not None and pct < 0


def test_weekly_change_insufficient_data():
    assert weekly_change([(date(2026, 1, 1), 80)]) is None


def test_plateau_detected_when_flat():
    series = _series([80.0, 80.05, 79.98, 80.02, 80.0, 79.99] + [80.0] * 9)
    assert is_plateau(series) is True


def test_plateau_not_detected_when_moving():
    series = _series([80 - 0.15 * i for i in range(15)])
    assert is_plateau(series) is False


def test_water_retention_on_sudden_spike():
    series = [
        (date(2026, 1, 1), 80.0),
        (date(2026, 1, 2), 81.2),  # +1.5% overnight
    ]
    assert water_retention_suspected(series) is True


def test_no_water_retention_on_gradual_change():
    series = _series([80.0, 80.1, 80.2, 80.1])
    assert water_retention_suspected(series) is False


def test_adherence_score_caps_at_100():
    assert adherence_score(3000, 2500) == 100
    assert adherence_score(1250, 2500) == 50


def test_nutrition_adherence_within_band_is_100():
    s = WeeklyStats(avg_kcal_logged=2050, kcal_target=2000)
    assert s.nutrition_adherence == 100  # within 10%


def test_nutrition_adherence_degrades_outside_band():
    s = WeeklyStats(avg_kcal_logged=3000, kcal_target=2000)  # +50%
    assert s.nutrition_adherence is not None and s.nutrition_adherence < 100


def test_possible_overeating_and_undereating_flags():
    assert WeeklyStats(avg_kcal_logged=2400, kcal_target=2000).possible_overeating is True
    assert WeeklyStats(avg_kcal_logged=1400, kcal_target=2000).possible_undereating is True


def test_water_and_exercise_adherence():
    s = WeeklyStats(avg_water_ml=2000, water_target_ml=2500, exercise_sessions=3, exercise_target_sessions=4)
    assert s.water_adherence == 80
    assert s.exercise_adherence == 75
