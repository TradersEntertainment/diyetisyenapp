"""Tool definitions + executor for the conversational dietitian agent.

Users write natural language; Claude calls these tools to persist every piece
of data, then answers with one natural message.
"""
import json
import logging
from dataclasses import asdict
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    BodyCompositionLog,
    BodyMeasurement,
    ExerciseLog,
    Food,
    FoodPreference,
    HungerLog,
    MealLog,
    MealPlan,
    MemoryNote,
    MoodLog,
    PlannedMeal,
    ShoppingList,
    SleepLog,
    StepsLog,
    User,
    WaterLog,
    WeightLog,
)
from app.services.reports import gather_weekly_stats, get_current_targets
from app.services.shopping import format_list_tr
from app.services.targets import ensure_protein_floor

log = logging.getLogger(__name__)

TOOLS: list[dict] = [
    {
        "name": "log_weight",
        "description": "Kullanıcının kilosunu kaydet. Kullanıcı yeni bir tartı sonucu söylediğinde çağır.",
        "input_schema": {
            "type": "object",
            "properties": {"weight_kg": {"type": "number", "description": "Kilo (kg)"}},
            "required": ["weight_kg"],
        },
    },
    {
        "name": "log_body_composition",
        "description": "Vücut analizi sonuçlarını (yağ oranı %, kas kütlesi kg) kaydet.",
        "input_schema": {
            "type": "object",
            "properties": {
                "body_fat_pct": {"type": "number"},
                "muscle_mass_kg": {"type": "number"},
            },
        },
    },
    {
        "name": "log_measurements",
        "description": "Bel/kalça/boyun çevresi ölçümlerini (cm) kaydet.",
        "input_schema": {
            "type": "object",
            "properties": {
                "waist_cm": {"type": "number"},
                "hip_cm": {"type": "number"},
                "neck_cm": {"type": "number"},
            },
        },
    },
    {
        "name": "log_water",
        "description": "İçilen suyu ml cinsinden kaydet (1 bardak ≈ 200 ml).",
        "input_schema": {
            "type": "object",
            "properties": {"amount_ml": {"type": "integer"}},
            "required": ["amount_ml"],
        },
    },
    {
        "name": "log_meal",
        "description": (
            "Yenen bir öğünü kaydet. Kalori ve makroları porsiyon tahminiyle SEN doldur. "
            "Plan dışı keyif yemeğiyse is_cheat=true."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "kcal": {"type": "integer"},
                "protein_g": {"type": "number"},
                "carb_g": {"type": "number"},
                "fat_g": {"type": "number"},
                "fiber_g": {"type": "number"},
                "slot": {
                    "type": "string",
                    "enum": ["kahvalti", "ara_ogun_1", "ogle", "ara_ogun_2", "aksam", "gece_atistirmasi"],
                },
                "is_cheat": {"type": "boolean"},
            },
            "required": ["description", "kcal", "protein_g", "carb_g", "fat_g"],
        },
    },
    {
        "name": "log_exercise",
        "description": "Yapılan egzersizi kaydet.",
        "input_schema": {
            "type": "object",
            "properties": {
                "activity": {"type": "string"},
                "duration_min": {"type": "integer"},
                "intensity": {"type": "string", "enum": ["hafif", "orta", "yogun"]},
                "note": {"type": "string"},
            },
            "required": ["activity"],
        },
    },
    {
        "name": "log_steps",
        "description": "Günlük adım sayısını kaydet.",
        "input_schema": {
            "type": "object",
            "properties": {"steps": {"type": "integer"}},
            "required": ["steps"],
        },
    },
    {
        "name": "log_sleep",
        "description": "Uyku süresini (saat) ve istersen kaliteyi (1-5) kaydet.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "number"},
                "quality": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["hours"],
        },
    },
    {
        "name": "log_mood",
        "description": "Ruh hali / stres / enerji (1-5) kaydet.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mood": {"type": "integer", "minimum": 1, "maximum": 5},
                "stress": {"type": "integer", "minimum": 1, "maximum": 5},
                "energy": {"type": "integer", "minimum": 1, "maximum": 5},
                "note": {"type": "string"},
            },
        },
    },
    {
        "name": "log_hunger",
        "description": "Açlık seviyesi (1-5) ve canının çektiği şeyleri kaydet.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hunger": {"type": "integer", "minimum": 1, "maximum": 5},
                "craving": {"type": "string"},
            },
        },
    },
    {
        "name": "get_progress_stats",
        "description": "Son 7 günün hesaplanmış istatistiklerini ve güncel hedefleri getir (sayı uydurma; bunları kullan).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_meal_plan",
        "description": "Aktif haftalık plandan bir günün öğünlerini getir. day_offset: 0=bugün, 1=yarın...",
        "input_schema": {
            "type": "object",
            "properties": {"day_offset": {"type": "integer", "minimum": 0, "maximum": 6}},
        },
    },
    {
        "name": "swap_planned_meal",
        "description": (
            "Plandaki bir öğünü kullanıcının isteği üzerine başka bir yemekle değiştir. "
            "Önce get_meal_plan ile planned_meal_id'yi bul. Yeni öğünün makrolarını sen hesapla; "
            "günün toplam proteini protein tabanının altına düşmemeli."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "planned_meal_id": {"type": "integer"},
                "name": {"type": "string"},
                "recipe": {"type": "string"},
                "kcal": {"type": "integer"},
                "protein_g": {"type": "number"},
                "carb_g": {"type": "number"},
                "fat_g": {"type": "number"},
                "prep_minutes": {"type": "integer"},
            },
            "required": ["planned_meal_id", "name", "kcal", "protein_g", "carb_g", "fat_g"],
        },
    },
    {
        "name": "remember_fact",
        "description": (
            "Uzun vadeli hafızaya kalıcı bir not yaz: yaşam tarzı, aile, alışkanlık, sağlık, tercih... "
            "Gelecekte işine yarayacak HER yeni bilgiyi kaydet."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["tercih", "aliskanlik", "saglik", "aile", "yasam", "ozet", "diger"],
                },
                "text": {"type": "string"},
            },
            "required": ["category", "text"],
        },
    },
    {
        "name": "update_food_preference",
        "description": "Bir yiyecek/mutfak tercihini kaydet veya güncelle (planları kişiselleştiren ana veri).",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "level": {
                    "type": "string",
                    "enum": ["bayilirim", "severim", "yiyebilirim", "sevmem", "asla"],
                },
                "category": {
                    "type": "string",
                    "enum": [
                        "sebze", "meyve", "et", "tatli", "icecek", "atistirmalik",
                        "kahvalti", "aksam", "restoran", "kacamak", "mutfak", "genel",
                    ],
                },
            },
            "required": ["name", "level"],
        },
    },
    {
        "name": "add_food",
        "description": "Besin veritabanına yeni bir yiyecek ekle (100 g başına değerler).",
        "input_schema": {
            "type": "object",
            "properties": {
                "name_tr": {"type": "string"},
                "category": {"type": "string"},
                "kcal": {"type": "number"},
                "protein_g": {"type": "number"},
                "carb_g": {"type": "number"},
                "fat_g": {"type": "number"},
                "fiber_g": {"type": "number"},
                "typical_portion_g": {"type": "number"},
                "typical_portion_name": {"type": "string"},
            },
            "required": ["name_tr", "category", "kcal", "protein_g", "carb_g", "fat_g"],
        },
    },
    {
        "name": "get_shopping_list",
        "description": "Bu haftanın ortak alışveriş listesini getir.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "regenerate_meal_plan",
        "description": (
            "Bu haftanın yemek planını baştan oluştur (kullanıcı yeni plan istediğinde veya plan yoksa). "
            "Birkaç dakika sürebilir; kullanıcıya bekleyeceğini söyle."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_reminder_time",
        "description": "Bir hatırlatmanın saatini değiştir veya aç/kapat.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": [
                        "gunaydin", "tarti", "su_1", "su_2", "su_3",
                        "ogun_ogle", "ogun_aksam", "aksam_kontrol",
                    ],
                },
                "time": {"type": "string", "description": "HH:MM (24 saat)"},
                "enabled": {"type": "boolean"},
            },
            "required": ["kind"],
        },
    },
]


async def execute_tool(session: AsyncSession, user: User, name: str, tool_input: dict) -> str:
    """Run one tool call against the database and return a short result string."""
    try:
        return await _dispatch(session, user, name, tool_input)
    except Exception:
        log.exception("tool %s failed for user %s", name, user.id)
        return "HATA: araç çalıştırılamadı."


async def _dispatch(session: AsyncSession, user: User, name: str, p: dict) -> str:
    uid = user.id

    if name == "log_weight":
        session.add(WeightLog(user_id=uid, weight_kg=float(p["weight_kg"])))
        await session.flush()
        raised = await ensure_protein_floor(session, user)
        msg = f"Kilo kaydedildi: {p['weight_kg']} kg."
        if raised:
            msg += f" Protein tabanı güncellendi: {raised.protein_g} g."
        return msg

    if name == "log_body_composition":
        session.add(
            BodyCompositionLog(
                user_id=uid,
                body_fat_pct=p.get("body_fat_pct"),
                muscle_mass_kg=p.get("muscle_mass_kg"),
            )
        )
        await session.flush()
        raised = await ensure_protein_floor(session, user)
        msg = "Vücut analizi kaydedildi."
        if raised:
            msg += f" Protein tabanı güncellendi: {raised.protein_g} g."
        return msg

    if name == "log_measurements":
        session.add(
            BodyMeasurement(
                user_id=uid,
                waist_cm=p.get("waist_cm"),
                hip_cm=p.get("hip_cm"),
                neck_cm=p.get("neck_cm"),
            )
        )
        return "Ölçüler kaydedildi."

    if name == "log_water":
        session.add(WaterLog(user_id=uid, amount_ml=int(p["amount_ml"])))
        return f"{p['amount_ml']} ml su kaydedildi."

    if name == "log_meal":
        session.add(
            MealLog(
                user_id=uid,
                description=p["description"],
                kcal=int(p["kcal"]),
                protein_g=float(p["protein_g"]),
                carb_g=float(p["carb_g"]),
                fat_g=float(p["fat_g"]),
                fiber_g=float(p.get("fiber_g") or 0),
                slot=p.get("slot"),
                is_cheat=bool(p.get("is_cheat", False)),
            )
        )
        return f"Öğün kaydedildi: {p['description']} ({p['kcal']} kcal, P{p['protein_g']} g)."

    if name == "log_exercise":
        session.add(
            ExerciseLog(
                user_id=uid,
                activity=p["activity"],
                duration_min=p.get("duration_min"),
                intensity=p.get("intensity"),
                note=p.get("note"),
            )
        )
        return "Egzersiz kaydedildi."

    if name == "log_steps":
        session.add(StepsLog(user_id=uid, steps=int(p["steps"])))
        return "Adımlar kaydedildi."

    if name == "log_sleep":
        session.add(SleepLog(user_id=uid, hours=float(p["hours"]), quality=p.get("quality")))
        return "Uyku kaydedildi."

    if name == "log_mood":
        session.add(
            MoodLog(
                user_id=uid,
                mood=p.get("mood"),
                stress=p.get("stress"),
                energy=p.get("energy"),
                note=p.get("note"),
            )
        )
        return "Ruh hali kaydedildi."

    if name == "log_hunger":
        session.add(HungerLog(user_id=uid, hunger=p.get("hunger"), craving=p.get("craving")))
        return "Açlık bilgisi kaydedildi."

    if name == "get_progress_stats":
        stats = await gather_weekly_stats(session, user)
        targets = await get_current_targets(session, uid)
        payload = asdict(stats)
        payload["nutrition_adherence"] = stats.nutrition_adherence
        payload["water_adherence"] = stats.water_adherence
        payload["exercise_adherence"] = stats.exercise_adherence
        if targets:
            payload["current_targets"] = {
                "kcal": targets.kcal,
                "protein_g": targets.protein_g,
                "carb_g": targets.carb_g,
                "fat_g": targets.fat_g,
                "fiber_g": targets.fiber_g,
                "water_ml": targets.water_ml,
                "diet_strategy": targets.diet_strategy,
            }
        return json.dumps(payload, ensure_ascii=False, default=str)

    if name == "get_meal_plan":
        day = date.today() + timedelta(days=int(p.get("day_offset", 0)))
        week_start = day - timedelta(days=day.weekday())
        res = await session.execute(
            select(MealPlan)
            .where(MealPlan.user_id == uid, MealPlan.week_start == week_start, MealPlan.status == "active")
            .order_by(MealPlan.id.desc())
            .limit(1)
        )
        plan = res.scalar_one_or_none()
        if not plan:
            return "Bu hafta için aktif plan yok."
        res = await session.execute(
            select(PlannedMeal).where(PlannedMeal.plan_id == plan.id, PlannedMeal.day_index == day.weekday())
        )
        meals = [
            {
                "planned_meal_id": m.id,
                "slot": m.slot,
                "name": m.name,
                "kcal": m.kcal,
                "protein_g": m.protein_g,
                "carb_g": m.carb_g,
                "fat_g": m.fat_g,
                "prep_minutes": m.prep_minutes,
                "recipe": m.recipe,
                "alternatives": m.alternatives,
            }
            for m in res.scalars()
        ]
        return json.dumps({"date": day.isoformat(), "strategy": plan.diet_strategy, "meals": meals}, ensure_ascii=False)

    if name == "swap_planned_meal":
        meal = await session.get(PlannedMeal, int(p["planned_meal_id"]))
        if not meal:
            return "HATA: öğün bulunamadı."
        plan = await session.get(MealPlan, meal.plan_id)
        if not plan or plan.user_id != uid:
            return "HATA: bu öğün bu kullanıcıya ait değil."
        meal.name = p["name"]
        meal.recipe = p.get("recipe") or meal.recipe
        meal.kcal = int(p["kcal"])
        meal.protein_g = float(p["protein_g"])
        meal.carb_g = float(p["carb_g"])
        meal.fat_g = float(p["fat_g"])
        if p.get("prep_minutes") is not None:
            meal.prep_minutes = int(p["prep_minutes"])
        return f"Öğün değiştirildi: {meal.slot} -> {meal.name}."

    if name == "remember_fact":
        session.add(MemoryNote(user_id=uid, category=p["category"], text=p["text"]))
        return "Not hafızaya kaydedildi."

    if name == "update_food_preference":
        res = await session.execute(
            select(FoodPreference).where(FoodPreference.user_id == uid, FoodPreference.name == p["name"])
        )
        pref = res.scalar_one_or_none()
        if pref:
            pref.level = p["level"]
            if p.get("category"):
                pref.category = p["category"]
        else:
            session.add(
                FoodPreference(
                    user_id=uid, name=p["name"], level=p["level"], category=p.get("category", "genel")
                )
            )
        return f"Tercih kaydedildi: {p['name']} -> {p['level']}."

    if name == "add_food":
        res = await session.execute(select(Food).where(Food.name_tr == p["name_tr"]))
        if res.scalar_one_or_none():
            return "Bu yiyecek zaten veritabanında."
        session.add(
            Food(
                name_tr=p["name_tr"],
                category=p["category"],
                kcal=float(p["kcal"]),
                protein_g=float(p["protein_g"]),
                carb_g=float(p["carb_g"]),
                fat_g=float(p["fat_g"]),
                fiber_g=float(p.get("fiber_g") or 0),
                typical_portion_g=p.get("typical_portion_g"),
                typical_portion_name=p.get("typical_portion_name"),
            )
        )
        return f"{p['name_tr']} veritabanına eklendi."

    if name == "get_shopping_list":
        today = date.today()
        week_start = today - timedelta(days=today.weekday())
        res = await session.execute(select(ShoppingList).where(ShoppingList.week_start == week_start))
        slist = res.scalar_one_or_none()
        if not slist:
            return "Bu hafta için alışveriş listesi henüz oluşturulmadı."
        await session.refresh(slist, ["items"])
        from app.services.shopping import AggregatedItem

        items = [AggregatedItem(i.name, i.quantity, i.category) for i in slist.items if not i.checked]
        if not items:
            return "Listedeki her şey alınmış görünüyor. 🎉"
        return format_list_tr(items)

    if name == "regenerate_meal_plan":
        from app.services.mealplan import extract_dinners, generate_weekly_plan
        from app.services.shopping import build_weekly_shopping_list

        today = date.today()
        week_start = today - timedelta(days=today.weekday())
        res = await session.execute(
            select(MealPlan).where(
                MealPlan.week_start == week_start,
                MealPlan.status == "active",
                MealPlan.user_id != uid,
            )
        )
        partner_plan = res.scalars().first()
        partner_dinners = None
        if partner_plan:
            await session.refresh(partner_plan, ["meals"])
            partner_dinners = extract_dinners(partner_plan.meals)
        plan = await generate_weekly_plan(session, user, week_start, partner_dinners)
        if not plan:
            return "HATA: plan oluşturulamadı (profil veya hedefler eksik olabilir)."
        await build_weekly_shopping_list(session, week_start)
        return "Yeni haftalık plan oluşturuldu ve alışveriş listesi güncellendi."

    if name == "set_reminder_time":
        from datetime import time as dtime

        from app.models import ReminderSetting

        res = await session.execute(
            select(ReminderSetting).where(
                ReminderSetting.user_id == uid, ReminderSetting.kind == p["kind"]
            )
        )
        setting = res.scalar_one_or_none()
        if not setting:
            setting = ReminderSetting(user_id=uid, kind=p["kind"], time_of_day=dtime(9, 0))
            session.add(setting)
        if p.get("time"):
            try:
                hh, mm = p["time"].split(":")
                setting.time_of_day = dtime(int(hh), int(mm))
            except (ValueError, AttributeError):
                return "HATA: saat HH:MM biçiminde olmalı."
        if p.get("enabled") is not None:
            setting.enabled = bool(p["enabled"])
        state = "açık" if setting.enabled else "kapalı"
        return f"Hatırlatma güncellendi: {p['kind']} -> {setting.time_of_day.strftime('%H:%M')} ({state})."

    return f"HATA: bilinmeyen araç {name}."
