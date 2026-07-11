"""Trend analysis over logged data. Pure functions -> easy to unit test.

All series are lists of (date, value) tuples sorted ascending by date.
Claude never invents these numbers — they are computed here and handed to it.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta


def _window(series: list[tuple[date, float]], days: int, today: date | None = None):
    if not series:
        return []
    end = today or series[-1][0]
    start = end - timedelta(days=days)
    return [(d, v) for d, v in series if start <= d <= end]


def weekly_change(series: list[tuple[date, float]], days: int = 14) -> float | None:
    """Average change per 7 days over the trailing window (linear fit on days)."""
    pts = _window(series, days)
    if len(pts) < 2:
        return None
    x0 = pts[0][0]
    xs = [(d - x0).days for d, _ in pts]
    ys = [v for _, v in pts]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return None
    slope_per_day = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    return round(slope_per_day * 7, 3)


def weekly_change_pct(series: list[tuple[date, float]], days: int = 14) -> float | None:
    change = weekly_change(series, days)
    if change is None or not series:
        return None
    latest = series[-1][1]
    if latest == 0:
        return None
    return round(change / latest * 100, 2)


def is_plateau(series: list[tuple[date, float]], days: int = 14, threshold_pct: float = 0.25) -> bool:
    """True when total movement over the window is under threshold_pct of body value."""
    pts = _window(series, days)
    if len(pts) < 3:
        return False
    span_days = (pts[-1][0] - pts[0][0]).days
    if span_days < days - 3:  # not enough coverage to call it a plateau
        return False
    total_change_pct = abs(pts[-1][1] - pts[0][1]) / pts[0][1] * 100
    return total_change_pct < threshold_pct


def water_retention_suspected(series: list[tuple[date, float]], spike_pct: float = 1.0) -> bool:
    """A sudden >=spike_pct jump within 1-2 days usually means water, not fat."""
    pts = _window(series, 4)
    if len(pts) < 2:
        return False
    for (d1, v1), (d2, v2) in zip(pts, pts[1:]):
        if (d2 - d1).days <= 2 and v1 > 0 and (v2 - v1) / v1 * 100 >= spike_pct:
            return True
    return False


SLOT_LABELS_TR = {
    "kahvalti": "kahvaltı",
    "ara_ogun_1": "ara öğün (sabah)",
    "ogle": "öğle",
    "ara_ogun_2": "ara öğün (öğleden sonra)",
    "aksam": "akşam",
    "gece_atistirmasi": "gece atıştırması",
}
_WEEKDAY_TR = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]


def adherence_breakdown(meal_logs, cheat_count: int, cheat_hours, high_hunger_hours) -> dict:
    """Where the week was hard, computed deterministically for the AI to narrate.

    meal_logs: iterable of objects with .slot and .ts (aware datetime).
    cheat_hours / high_hunger_hours: iterables of int hour-of-day.
    """
    from collections import Counter

    slot_counts = Counter(m.slot for m in meal_logs if m.slot)
    logged_slots = [s for s in SLOT_LABELS_TR if slot_counts.get(s, 0) > 0]
    # Which planned slots are most often NOT logged (7 days expected each).
    missed = sorted(
        ((s, 7 - min(slot_counts.get(s, 0), 7)) for s in SLOT_LABELS_TR),
        key=lambda x: x[1], reverse=True,
    )
    worst_slot = next((SLOT_LABELS_TR[s] for s, miss in missed if miss >= 3), None)

    def peak(hours):
        c = Counter(h for h in hours)
        if not c:
            return None
        hour, _ = c.most_common(1)[0]
        if 5 <= hour < 11:
            return "sabah"
        if 11 <= hour < 16:
            return "öğle civarı"
        if 16 <= hour < 21:
            return "akşamüstü"
        return "gece"

    return {
        "toplam_ogun_kaydi": sum(slot_counts.values()),
        "kaydedilen_ogunler": [SLOT_LABELS_TR[s] for s in logged_slots],
        "en_cok_atlanan_ogun": worst_slot,
        "kacamak_sayisi": cheat_count,
        "kacamak_zamani": peak(cheat_hours),
        "en_ac_hissedilen_zaman": peak(high_hunger_hours),
    }


def adherence_score(actual: float, target: float) -> int:
    """0-100. Meeting or exceeding target = 100 (no punishment for overshooting water etc.)."""
    if target <= 0:
        return 100
    return min(100, round(actual / target * 100))


@dataclass
class WeeklyStats:
    """One user's computed week — the factual payload passed to the adaptive engine and to Claude."""

    weight_change_kg_per_week: float | None = None
    weight_change_pct_per_week: float | None = None
    fat_pct_change_per_week: float | None = None
    muscle_kg_change_per_week: float | None = None
    plateau: bool = False
    water_retention_suspected: bool = False
    avg_kcal_logged: float | None = None
    kcal_target: int | None = None
    avg_protein_logged: float | None = None
    protein_target: int | None = None
    avg_water_ml: float | None = None
    water_target_ml: int | None = None
    exercise_sessions: int = 0
    exercise_target_sessions: int | None = None
    avg_sleep_hours: float | None = None
    avg_mood: float | None = None
    avg_energy: float | None = None
    avg_hunger: float | None = None
    cheat_meals: int = 0
    days_meals_logged: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def nutrition_adherence(self) -> int | None:
        if self.avg_kcal_logged is None or not self.kcal_target:
            return None
        # 100 when within +-10% of target, degrading linearly outside
        ratio = self.avg_kcal_logged / self.kcal_target
        deviation = abs(ratio - 1.0)
        if deviation <= 0.10:
            return 100
        return max(0, round(100 - (deviation - 0.10) * 250))

    @property
    def water_adherence(self) -> int | None:
        if self.avg_water_ml is None or not self.water_target_ml:
            return None
        return adherence_score(self.avg_water_ml, self.water_target_ml)

    @property
    def exercise_adherence(self) -> int | None:
        if self.exercise_target_sessions is None:
            return None
        return adherence_score(self.exercise_sessions, self.exercise_target_sessions)

    @property
    def possible_overeating(self) -> bool:
        return bool(
            self.avg_kcal_logged and self.kcal_target and self.avg_kcal_logged > self.kcal_target * 1.15
        )

    @property
    def possible_undereating(self) -> bool:
        return bool(
            self.avg_kcal_logged and self.kcal_target and self.avg_kcal_logged < self.kcal_target * 0.75
        )
