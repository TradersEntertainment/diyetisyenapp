"""Weekly meal plan generation.

Plans are NEVER random: the prompt is grounded in the user's learned food
preferences (asla/sevmem hard-excluded), health constraints, budget, cooking
skill, equipment, the Turkish food database and recent meal logs. Output is a
structured JSON document validated in code — every day must meet the protein
floor — then stored as MealPlan/PlannedMeal rows.
"""
import json
import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.client import get_client, get_plan_model
from app.ai.prompts import DIETITIAN_PERSONA
from app.models import Food, FoodPreference, MealLog, MealPlan, PlannedMeal, User
from app.services.reports import get_current_targets
from app.services.targets import compute_targets_for_user, get_profile

log = logging.getLogger(__name__)

SLOTS = ["kahvalti", "ara_ogun_1", "ogle", "ara_ogun_2", "aksam", "gece_atistirmasi"]

MEAL_SCHEMA = {
    "type": "object",
    "properties": {
        "slot": {"type": "string", "enum": SLOTS},
        "name": {"type": "string"},
        "recipe": {"type": "string", "description": "Kısa tarif, 2-4 cümle"},
        "prep_minutes": {"type": "integer"},
        "kcal": {"type": "integer"},
        "protein_g": {"type": "number"},
        "carb_g": {"type": "number"},
        "fat_g": {"type": "number"},
        "fiber_g": {"type": "number"},
        "ingredients": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "qty": {"type": "number"},
                    "unit": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": [
                            "sebze", "meyve", "protein", "sut_urunleri", "donuk",
                            "tahil", "baharat", "icecek", "diger",
                        ],
                    },
                },
                "required": ["name", "qty", "unit", "category"],
                "additionalProperties": False,
            },
        },
        "alternatives": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "note": {"type": "string"}},
                "required": ["name", "note"],
                "additionalProperties": False,
            },
        },
        "shared_with_partner": {"type": "boolean"},
    },
    "required": [
        "slot", "name", "recipe", "prep_minutes", "kcal",
        "protein_g", "carb_g", "fat_g", "fiber_g", "ingredients", "alternatives",
        "shared_with_partner",
    ],
    "additionalProperties": False,
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "days": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    # NOTE: structured-output schemas don't support minimum/maximum
                    # on integers; the description carries the constraint instead.
                    "day_index": {"type": "integer", "description": "0=Pazartesi ... 6=Pazar"},
                    "meals": {"type": "array", "items": MEAL_SCHEMA},
                },
                "required": ["day_index", "meals"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["days"],
    "additionalProperties": False,
}


async def _preferences_block(session: AsyncSession, user_id: int) -> str:
    res = await session.execute(select(FoodPreference).where(FoodPreference.user_id == user_id))
    prefs = list(res.scalars())
    if not prefs:
        return "Kayıtlı tercih yok."
    groups: dict[str, list[str]] = {}
    for p in prefs:
        groups.setdefault(p.level, []).append(p.name)
    lines = []
    for level, label in [
        ("asla", "ASLA KULLANMA"),
        ("sevmem", "KULLANMA (sevmiyor)"),
        ("bayilirim", "Bayılıyor (sık kullan)"),
        ("severim", "Seviyor"),
        ("yiyebilirim", "Yiyebilir"),
    ]:
        if level in groups:
            lines.append(f"{label}: {', '.join(groups[level])}")
    return "\n".join(lines)


async def _foods_block(session: AsyncSession, limit: int = 120) -> str:
    res = await session.execute(select(Food).limit(limit))
    foods = list(res.scalars())
    lines = [
        f"{f.name_tr} ({f.category}): {f.kcal:g} kcal, P{f.protein_g:g} K{f.carb_g:g} Y{f.fat_g:g} /100g"
        for f in foods
    ]
    return "\n".join(lines) if lines else "(besin veritabanı boş)"


async def _kalibra_block(session: AsyncSession) -> str:
    """The household's SDM Kalibra high-protein products (category kalibra_urun)."""
    res = await session.execute(select(Food).where(Food.category == "kalibra_urun"))
    foods = list(res.scalars())
    if not foods:
        return ""
    lines = []
    for f in foods:
        portion = (
            f" | porsiyon: {f.typical_portion_name or ''} ~{f.typical_portion_g:g} g".rstrip()
            if f.typical_portion_g
            else ""
        )
        lines.append(
            f"- {f.name_tr}: {f.kcal:g} kcal, P{f.protein_g:g} K{f.carb_g:g} Y{f.fat_g:g} "
            f"Lif{f.fiber_g:g} /100g{portion}"
        )
    return "\n".join(lines)


async def _plan_memory_block(session: AsyncSession, user_id: int) -> str:
    """Durable memory notes so plan-shaping wishes (e.g. same menu every day) stick."""
    from app.ai.context import _memory_text

    return await _memory_text(session, user_id, limit=20)


async def _recent_meals_block(session: AsyncSession, user_id: int, days: int = 21) -> str:
    from datetime import datetime, timezone

    since = datetime.now(timezone.utc) - timedelta(days=days)
    res = await session.execute(
        select(MealLog).where(MealLog.user_id == user_id, MealLog.ts >= since).order_by(MealLog.ts.desc()).limit(40)
    )
    meals = list(res.scalars())
    if not meals:
        return "(henüz öğün kaydı yok)"
    return "\n".join(f"- {m.description}" for m in meals)


def validate_plan_protein(plan_data: dict, protein_floor: int, tolerance: float = 0.95) -> list[int]:
    """Return the day_indexes whose total protein misses the floor."""
    bad = []
    for day in plan_data.get("days", []):
        total = sum(m.get("protein_g", 0) for m in day.get("meals", []))
        if total < protein_floor * tolerance:
            bad.append(day["day_index"])
    return bad


def validate_plan_kcal(plan_data: dict, target_kcal: int, tolerance: float = 0.10) -> list[int]:
    """Return the day_indexes whose total kcal is off the target by > tolerance."""
    bad = []
    for day in plan_data.get("days", []):
        total = sum(m.get("kcal", 0) for m in day.get("meals", []))
        if abs(total - target_kcal) > target_kcal * tolerance:
            bad.append(day["day_index"])
    return bad


async def generate_weekly_plan(
    session: AsyncSession,
    user: User,
    week_start: date | None = None,
    partner_menu: list[dict] | None = None,
) -> MealPlan | None:
    """Generate + persist one user's weekly plan. Returns None if prerequisites missing."""
    today = date.today()
    week_start = week_start or (today - timedelta(days=today.weekday()))

    profile = await get_profile(session, user.id)
    targets_row = await get_current_targets(session, user.id)
    computed = await compute_targets_for_user(session, user)
    if not profile or not targets_row or not computed:
        log.warning("cannot generate plan for user %s: missing profile/targets", user.id)
        return None
    protein_floor = computed.protein_floor_g

    night_snack = "" if profile.eats_snacks else "Gece atıştırması ekleme."
    partner_block = ""
    if partner_menu:
        slot_lines: dict[int, list[str]] = {}
        for d in partner_menu:
            slot_lines.setdefault(d["day_index"], []).append(f"{d['slot']}: {d['name']}")
        menu_text = "\n".join(
            f"- Gün {day}: " + " | ".join(items) for day, items in sorted(slot_lines.items())
        )
        partner_block = (
            "\n## Ev arkadaşının haftalık menüsü — İKİSİ HER ÖĞÜNÜ BERABER YİYOR\n"
            "AYNI yemekleri, aynı gün ve öğünlerde kullan (yemek adları birebir aynı olsun, "
            "shared_with_partner=true). SADECE porsiyonları bu kullanıcının kendi hedeflerine göre "
            "ayarla; kalori/makro/malzeme miktarları bu kullanıcının porsiyonuna göre olsun. "
            "Bu kullanıcının 'asla' yiyecekleriyle çakışan bir yemek varsa yalnızca o öğünü değiştir:\n"
            + menu_text
        )

    prompt = f"""7 günlük (Pazartesi=0 ... Pazar=6) kişisel yemek planı oluştur.

## Hedefler (günlük — kcal toleransı ±%5)
- Kalori: {targets_row.kcal} kcal
- PROTEİN: en az {protein_floor} g (SERT ALT SINIR — her günün toplamı bunu karşılamalı), hedef {targets_row.protein_g} g
- Karbonhidrat: ~{targets_row.carb_g} g, Yağ: ~{targets_row.fat_g} g, Lif: en az {targets_row.fiber_g} g
- Diyet stratejisi: {targets_row.diet_strategy}

## Öğün yapısı
Her gün: kahvalti, ara_ogun_1, ogle, ara_ogun_2, aksam{"" if night_snack else ", istenirse gece_atistirmasi"}.
{night_snack}
Her öğünde: isim, KISA tarif, hazırlık süresi, kalori+makrolar+lif, malzemeler.
ÖNEMLİ — PORSİYON: Her öğünün recipe alanı bu kullanıcının porsiyon miktarlarıyla BAŞLASIN ve
HER kalem ÖLÇÜLÜ olsun: gram ("100 g lor"), adet+gramaj ("2 adet tam buğday lavaş, 60 g/adet")
veya hacim+gram karşılığı ("1 su bardağı süt (200 ml)"). "Biraz", "bir kase", "bir tabak" gibi
ölçüsüz ifade YASAK. Örnek: "Porsiyon: 250 g tavuk göğsü, 150 g haşlanmış bulgur, 200 g cacık
(süzme yoğurttan)." Sonra 1-3 cümle hazırlanış gelsin. Malzemeler de aynı porsiyona göre.

ÖNEMLİ — GÜNDE TEK PİŞEN ET: Bir günde yalnızca BİR pişen et türü kullan (tavuk / hindi / balık
ve deniz ürünleri / kırmızı et-kıyma-kuzu türlerinden sadece biri). Öğlen kıyma varsa akşama balık
KOYMA — akşam ya aynı et türünden ya da etsiz/baklagilli olur. Hindi füme, salam gibi hazır
şarküteri ürünleri ve yumurta bu kurala DAHİL DEĞİLDİR (her gün serbest).

Malzemeleri alışveriş listesi için gerçekçi miktarlarla yaz ve her öğüne 1-2 alternatif ekle.

## Kişiselleştirme verileri
### Tercihler
{await _preferences_block(session, user.id)}

### Sağlık kısıtları
{"Diyabet/insülin direnci: rafine şekerden kaçın, düşük glisemik tercih et. " if (profile.has_diabetes or profile.has_insulin_resistance) else ""}{"Hipertansiyon: tuzu sınırla. " if profile.has_hypertension else ""}{"Kolesterol: doymuş yağı sınırla. " if profile.has_cholesterol else ""}{"Sindirim hassasiyeti: ağır/kızartma yemeklerden kaçın. " if profile.has_digestive_issues else ""}
Alerjiler: {profile.allergies or "yok"} | İntoleranslar: {profile.intolerances or "yok"}

### Mutfak & bütçe
Beceri: {profile.cooking_skill or "orta"} | Ekipman: {", ".join(profile.kitchen_equipment or []) or "standart"} | Bütçe: {profile.monthly_food_budget or "orta"}

### Son yediklerinden örnekler (alışkanlıklarını yansıt, birebir kopyalama)
{await _recent_meals_block(session, user.id)}

### Evdeki SDM Kalibra yüksek proteinli ürünler
Ara öğün, tatlı ihtiyacı ve gece atıştırmalarında bu ürünlere ÖNCELİK ver (pratik ve protein açısından verimli):
{await _kalibra_block(session) or "(kayıtlı ürün yok)"}

### Hafıza notları (plan tercihlerini MUTLAKA yansıt: tek-tip menü isteği, öğün düzeni vb.)
{await _plan_memory_block(session, user.id) or "(not yok)"}

### Besin veritabanından örnek değerler (porsiyon hesaplarında kullan)
{await _foods_block(session)}
{partner_block}

Türk ve Akdeniz mutfağı ağırlıklı, pratik bir hafta hazırla; kullanıcı tek-tip menü istemedikçe günleri tekrar ettirme."""

    client = get_client()
    plan_data: dict | None = None
    feedback = ""
    for attempt in range(2):
        try:
            async with client.messages.stream(
                model=get_plan_model(),
                max_tokens=64000,
                system=[
                    {"type": "text", "text": DIETITIAN_PERSONA, "cache_control": {"type": "ephemeral"}}
                ],
                output_config={"format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
                messages=[{"role": "user", "content": prompt + feedback}],
            ) as stream:
                response = await stream.get_final_message()
            if response.stop_reason == "refusal":
                raise RuntimeError("refusal")
            text = next(b.text for b in response.content if b.type == "text")
            candidate = json.loads(text)
        except Exception:
            log.exception("meal plan generation attempt %s failed for user %s", attempt + 1, user.id)
            continue

        bad_protein_days = validate_plan_protein(candidate, protein_floor)
        bad_kcal_days = validate_plan_kcal(candidate, targets_row.kcal)
        plan_data = candidate
        if not bad_protein_days and not bad_kcal_days:
            break
        log.warning(
            "plan for user %s off-target (attempt %s): protein days %s, kcal days %s",
            user.id, attempt + 1, bad_protein_days, bad_kcal_days,
        )
        problems = []
        if bad_protein_days:
            problems.append(
                f"şu günlerin toplam proteini {protein_floor} g alt sınırının altında: {bad_protein_days}"
            )
        if bad_kcal_days:
            problems.append(
                f"şu günlerin toplam kalorisi {targets_row.kcal} kcal hedefinden %10'dan fazla sapıyor: {bad_kcal_days}"
            )
        feedback = (
            "\n\nÖNEMLİ DÜZELTME: Önceki denemede "
            + "; ".join(problems)
            + f". Her günün toplamı {targets_row.kcal} kcal (±%10) ve en az {protein_floor} g protein "
            "olacak şekilde planı yeniden yaz."
        )

    if plan_data is None:
        return None

    # Archive any existing active plan for this week, then store the new one.
    res = await session.execute(
        select(MealPlan).where(
            MealPlan.user_id == user.id, MealPlan.week_start == week_start, MealPlan.status == "active"
        )
    )
    for old in res.scalars():
        old.status = "archived"

    plan = MealPlan(
        user_id=user.id,
        week_start=week_start,
        status="active",
        diet_strategy=targets_row.diet_strategy,
    )
    session.add(plan)
    await session.flush()
    for day in plan_data["days"]:
        for meal in day["meals"]:
            session.add(
                PlannedMeal(
                    plan_id=plan.id,
                    day_index=day["day_index"],
                    slot=meal["slot"],
                    name=meal["name"],
                    recipe=meal.get("recipe", ""),
                    prep_minutes=meal.get("prep_minutes", 0),
                    kcal=meal.get("kcal", 0),
                    protein_g=meal.get("protein_g", 0),
                    carb_g=meal.get("carb_g", 0),
                    fat_g=meal.get("fat_g", 0),
                    fiber_g=meal.get("fiber_g", 0),
                    ingredients=meal.get("ingredients"),
                    alternatives=meal.get("alternatives"),
                    shared_with_partner=meal.get("shared_with_partner", False),
                )
            )
    await session.flush()
    return plan


def extract_menu(plan_data_meals: list[PlannedMeal]) -> list[dict]:
    """The full weekly menu (every slot) — the household eats the SAME dishes,
    only portions differ, so the partner's generation copies this menu."""
    return [
        {"day_index": m.day_index, "slot": m.slot, "name": m.name}
        for m in plan_data_meals
    ]


async def generate_household_plans(session: AsyncSession, users: list[User], week_start: date | None = None) -> list[MealPlan]:
    """Generate plans for both users; the second user gets the SAME menu,
    portioned to their own targets."""
    plans: list[MealPlan] = []
    partner_menu: list[dict] | None = None
    for user in users:
        plan = await generate_weekly_plan(session, user, week_start, partner_menu)
        if plan:
            plans.append(plan)
            await session.refresh(plan, ["meals"])
            partner_menu = extract_menu(plan.meals)
    return plans


async def render_week_plan_png(session: AsyncSession, user: User, week_start: date | None = None):
    """The user's active weekly plan as a PNG buffer (None when there's no plan)."""
    from app.bot.charts import plan_image

    today = date.today()
    week_start = week_start or (today - timedelta(days=today.weekday()))
    res = await session.execute(
        select(MealPlan)
        .where(MealPlan.user_id == user.id, MealPlan.week_start == week_start, MealPlan.status == "active")
        .order_by(MealPlan.id.desc())
        .limit(1)
    )
    plan = res.scalar_one_or_none()
    if not plan:
        return None
    res = await session.execute(select(PlannedMeal).where(PlannedMeal.plan_id == plan.id))
    by_day: dict[int, list[dict]] = {}
    for m in res.scalars():
        # Recipes start with "Porsiyon: 100 g lor, 2 adet lavaş (60 g)..." —
        # surface that first sentence in the table.
        portion = ""
        if m.recipe:
            first = m.recipe.split(".")[0].strip()
            if first.lower().startswith("porsiyon"):
                portion = first.split(":", 1)[-1].strip()
        by_day.setdefault(m.day_index, []).append(
            {"slot": m.slot, "name": m.name, "kcal": m.kcal, "protein_g": m.protein_g, "portion": portion}
        )
    targets = await get_current_targets(session, user.id)
    title = f"{user.name} — Haftalık Plan ({week_start.strftime('%d.%m')})"
    return plan_image(
        [{"day_index": d, "meals": meals} for d, meals in sorted(by_day.items())],
        title,
        targets.kcal if targets else None,
        targets.protein_g if targets else None,
    )
