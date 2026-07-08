"""Weekly adaptive engine: deterministic guardrails + room for AI strategy choice.

Division of labor:
  * This module decides the NUMBERS (kcal / protein / fiber adjustments) with
    deterministic, testable rules. The protein floor from body analysis is a hard
    invariant — every path goes through `calculations.compute_targets`, which
    clamps protein to the floor and absorbs calorie changes in carbs/fat.
  * Claude decides the diet STRATEGY (macro emphasis, meal structure, approach)
    each week, given these numbers as constraints it cannot override.
"""
from dataclasses import dataclass, field

from app.services.analysis import WeeklyStats
from app.services.calculations import GOAL_GAIN, GOAL_LOSE, Targets, compute_targets


@dataclass
class AdjustmentDecision:
    kcal_delta: int = 0
    protein_bonus_g: int = 0  # extra protein ON TOP of the floor
    fiber_bonus_g: int = 0
    carb_bias: bool = False  # low energy -> AI should bias kcal toward carbs / around training
    volume_bias: bool = False  # high hunger -> AI should prefer high-volume, filling meals
    activity_nudge: bool = False  # plateau at calorie floor -> suggest more movement instead of less food
    reasons: list[str] = field(default_factory=list)


def decide_adjustments(
    stats: WeeklyStats,
    current_kcal: int,
    primary_goal: str,
) -> AdjustmentDecision:
    """Apply the spec'd guardrail rules to one week of stats."""
    d = AdjustmentDecision()

    losing = primary_goal == GOAL_LOSE
    gaining = primary_goal == GOAL_GAIN
    pct = stats.weight_change_pct_per_week

    # Weight loss too fast (> 1% / week) -> eat a bit more
    if losing and pct is not None and pct < -1.0:
        d.kcal_delta += 175
        d.reasons.append(
            f"Kilo kaybı çok hızlı (haftada %{abs(pct):.1f}); kas kaybını önlemek için kalori +175."
        )

    # Plateau (and not just water) -> reduce calories ~10% or nudge activity
    if losing and stats.plateau and not stats.water_retention_suspected:
        proposed = round(current_kcal * 0.10)
        d.kcal_delta -= proposed
        d.activity_nudge = True
        d.reasons.append(
            f"Kilo {14}+ gündür sabit; kalori -%10 ({proposed} kcal) veya günlük hareketi artırma önerisi."
        )

    if stats.water_retention_suspected:
        d.reasons.append("Ani kilo artışı büyük olasılıkla su tutulumu; hedef değişikliği yapılmadı.")

    # Muscle decreasing -> raise protein above the floor
    if stats.muscle_kg_change_per_week is not None and stats.muscle_kg_change_per_week < -0.05:
        d.protein_bonus_g += 15
        d.reasons.append("Kas kütlesi düşüşte; protein hedefi +15 g artırıldı.")

    # Low energy -> shift calories toward carbohydrates
    if stats.avg_energy is not None and stats.avg_energy <= 2.5:
        d.carb_bias = True
        d.reasons.append("Enerji düşük; karbonhidratlar antrenman çevresine ve güne yayılacak.")

    # Excessive hunger -> more volume and fiber
    if stats.avg_hunger is not None and stats.avg_hunger >= 4.0:
        d.fiber_bonus_g += 5
        d.volume_bias = True
        d.reasons.append("Açlık yüksek; lif +5 g ve daha hacimli/doyurucu öğünler planlanacak.")

    # Gaining too fast while bulking -> trim a little
    if gaining and pct is not None and pct > 0.5:
        d.kcal_delta -= 100
        d.reasons.append(f"Kilo alımı hızlı (haftada %{pct:.1f}); kalori -100.")

    # Undereating flag (informational; targets unchanged)
    if stats.possible_undereating:
        d.reasons.append("Kayıtlara göre hedefin belirgin altında yeniyor olabilir; metabolizmayı korumak için hedefe yaklaşılmalı.")
    if stats.possible_overeating:
        d.reasons.append("Kayıtlara göre hedefin üzerinde yeme eğilimi var; plan sadakati konuşulacak.")

    return d


def apply_adjustments(
    decision: AdjustmentDecision,
    *,
    current_kcal: int,
    weight_kg: float,
    height_cm: float,
    age: int,
    gender: str,
    activity_level: str,
    primary_goal: str,
    body_fat_pct: float | None,
    exercise_days_per_week: int | None,
) -> Targets:
    """Turn a decision into a concrete target set.

    Goes through compute_targets, which enforces the protein floor no matter what
    kcal_delta was decided — protein can only ever be raised (protein_bonus_g),
    and calorie cuts land on carbs/fat.
    """
    new_kcal = current_kcal + decision.kcal_delta
    floor_probe = compute_targets(
        weight_kg=weight_kg,
        height_cm=height_cm,
        age=age,
        gender=gender,
        activity_level=activity_level,
        primary_goal=primary_goal,
        body_fat_pct=body_fat_pct,
        exercise_days_per_week=exercise_days_per_week,
    )
    targets = compute_targets(
        weight_kg=weight_kg,
        height_cm=height_cm,
        age=age,
        gender=gender,
        activity_level=activity_level,
        primary_goal=primary_goal,
        body_fat_pct=body_fat_pct,
        exercise_days_per_week=exercise_days_per_week,
        kcal_override=new_kcal,
        protein_override_g=floor_probe.protein_floor_g + decision.protein_bonus_g,
    )
    if decision.fiber_bonus_g:
        targets.fiber_g += decision.fiber_bonus_g
    return targets


def spread_cheat_surplus(surplus_kcal: int, days: int = 3, max_daily_cut: int = 250) -> list[int]:
    """Rebalance a cheat meal over the following days WITHOUT punishment.

    Returns per-day kcal reductions (taken from carbs/fat only — never protein).
    The cut is capped so a big cheat never turns the next days into starvation.
    """
    if surplus_kcal <= 0:
        return [0] * days
    per_day = min(max_daily_cut, round(surplus_kcal / days))
    return [per_day] * days
