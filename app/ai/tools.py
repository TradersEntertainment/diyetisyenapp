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
        "name": "get_energy_profile",
        "description": (
            "Kullanıcının enerji profilini getir: bazal metabolizma (BMR), tahmini günlük harcama (TDEE), "
            "mevcut kalori hedefi, sistemin önerdiği güncel hedef ve protein tabanı. "
            "Kalori/hedef konuşulurken veya yeni plan öncesi MUTLAKA bunu çağır; sayı uydurma."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_weight_loss_pace",
        "description": (
            "Kilo verme temposunu ayarla: kullanıcı 'haftada X kg vereyim' dediğinde çağır. "
            "Tempoyu kaloriye çevirir, güvenlik sınırlarını (haftada ~%1 vücut ağırlığı, protein "
            "tabanı + minimum yağ kalori tabanı) uygular ve GERÇEKLEŞEBİLİR tempoyu raporlar — "
            "dönüşteki sayıları kullan, kendin hesaplama."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kg_per_week": {"type": "number", "description": "İstenen haftalık kilo kaybı (kg), örn. 0.5 veya 1.0"},
                "reason": {"type": "string", "description": "Kısa gerekçe"},
            },
            "required": ["kg_per_week"],
        },
    },
    {
        "name": "set_calorie_target",
        "description": (
            "Günlük kalori hedefini değiştir (kullanıcı onayıyla). Makrolar yeniden hesaplanır; "
            "protein tabanı korunur — taban + minimum yağ için gereken kalorinin altına inilemez."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kcal": {"type": "integer", "description": "Yeni günlük kalori hedefi"},
                "reason": {"type": "string", "description": "Kısa gerekçe"},
            },
            "required": ["kcal"],
        },
    },
    {
        "name": "apply_plan_day_to_week",
        "description": (
            "Aktif haftalık plandaki bir günün menüsünü haftanın 7 gününe kopyala "
            "(kullanıcı her gün aynı beslenmek istediğinde). day_index: 0=Pazartesi ... 6=Pazar. "
            "Alışveriş listesi otomatik güncellenir."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"day_index": {"type": "integer", "description": "Kopyalanacak kaynak gün (0-6)"}},
            "required": ["day_index"],
        },
    },
    {
        "name": "pin_meal_slot",
        "description": (
            "TEK bir öğünü (ör. kahvaltı) haftanın 7 gününe sabitle; diğer öğünler değişmeden kalır. "
            "Kullanıcı 'şu kahvaltıyı her gün yapıyorum/istiyorum' derse kullan. source_day_index: "
            "beğenilen öğünün alındığı gün (0=Pazartesi..6=Pazar). slot: kahvalti/ara_ogun_1/ogle/"
            "ara_ogun_2/aksam/gece_atistirmasi. remember=true ise gelecekteki planlarda da sabit tutulur."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "slot": {
                    "type": "string",
                    "enum": ["kahvalti", "ara_ogun_1", "ogle", "ara_ogun_2", "aksam", "gece_atistirmasi"],
                },
                "source_day_index": {"type": "integer", "description": "Beğenilen öğünün olduğu gün (0-6)"},
                "remember": {"type": "boolean", "description": "Gelecek planlarda da sabit kalsın mı"},
            },
            "required": ["slot", "source_day_index"],
        },
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
            "günün toplam proteini protein tabanının altına düşmemeli. "
            "DİKKAT: Bu araç SADECE tek öğünü değiştirir — kalori/tempo hedefi değiştiğinde planı "
            "bununla güncellemeye çalışma, regenerate_meal_plan kullan."
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
            "Bu haftanın yemek planlarını EV HALKI İÇİN baştan oluştur (ortak menü, kişiye özel "
            "porsiyonlar; iki kullanıcı için de yenilenir). Arka planda çalışır, bitince tablolar "
            "gruba otomatik gönderilir. Kullanıcı kalori/tempo belirtmediyse ÖNCE get_energy_profile "
            "ile sayıları paylaşıp teyit et."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "log_lab_result",
        "description": (
            "Bir kan/tahlil değerini kaydet (tahlil fotoğrafındaki her satır için ayrı çağır). "
            "panel örn: 'LDL kolesterol', 'Açlık kan şekeri', 'HbA1c', 'TSH', 'Trigliserit', 'Vitamin D'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "panel": {"type": "string"},
                "value": {"type": "number"},
                "unit": {"type": "string", "description": "örn. mg/dL, %, mIU/L"},
                "taken_on": {"type": "string", "description": "Tahlil tarihi YYYY-MM-DD (biliniyorsa)"},
            },
            "required": ["panel", "value"],
        },
    },
    {
        "name": "get_lab_history",
        "description": "Kaydedilmiş tahlil değerlerini getir (panel verilirse sadece o panelin geçmişi).",
        "input_schema": {
            "type": "object",
            "properties": {"panel": {"type": "string"}},
        },
    },
    {
        "name": "lookup_barcode",
        "description": (
            "Bir ürün barkodunun besin değerlerini OpenFoodFacts'tan getir ve veritabanına ekle. "
            "Kullanıcı barkod numarası verdiğinde veya barkod fotoğrafından okuduğunda çağır."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "Barkod numarası"}},
            "required": ["code"],
        },
    },
    {
        "name": "get_adherence_analysis",
        "description": (
            "Son 7 günün plana uyum kırılımını getir: en çok atlanan öğün, kaçamak/açlık zamanları. "
            "Kullanıcı 'nerede takılıyorum', 'neyi beceremiyorum' diye sorunca kullan."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_dining_out_context",
        "description": (
            "Kullanıcı dışarıda/restoranda olduğunu söyleyip ne yiyeceğini sorunca çağır: güncel "
            "hedef, protein tabanı, bugün kalan kalori ve 'asla/sevmem' yiyecekleri döndürür ki "
            "hedefe uygun 2-3 seçenek önerebilesin."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_auto_water",
        "description": (
            "Otomatik su takibini aç/kapat. Kullanıcı 'su içtiğimi her seferinde belirtmek "
            "istemiyorum, sen varsay' derse aç (enabled=true): su saatlerinde senin adına bardak "
            "eklenir ve sadece bildirim gider, tıklama gerekmez. Kullanıcı 'ben söylerim/otomatik "
            "ekleme' derse kapat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
        },
    },
    {
        "name": "set_voice_replies",
        "description": (
            "Sesli cevabı aç/kapat. Kullanıcı 'sesli de cevap ver / sesini duyayım' derse aç, "
            "'sesli cevabı kapat / sadece yaz' derse kapat. Yazı her zaman gönderilir; ses sadece ek."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
        },
    },
    {
        "name": "adjust_water",
        "description": (
            "Bugünkü su kaydını düzelt. Kullanıcı otomatik eklenen suyu içmediyse ('su içmedim') "
            "negatif değerle geri al (örn. amount_ml=-250). Fazladan eklemek için pozitif kullan."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"amount_ml": {"type": "integer", "description": "Eklenecek (+) veya geri alınacak (-) ml"}},
            "required": ["amount_ml"],
        },
    },
    {
        "name": "set_wake_time",
        "description": (
            "Kullanıcının uyanma saatini kaydet. Kullanıcı ne zaman kalktığını/uyandığını söylerse "
            "çağır (örn. 'biz 11 gibi kalkıyoruz'). TÜM günlük hatırlatmalar (günaydın, kahvaltı, "
            "öğle, akşam, su, akşam kontrolü) bu saate göre otomatik kayar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "time": {"type": "string", "description": "Uyanma saati HH:MM (24 saat)"},
            },
            "required": ["time"],
        },
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
                        "ogun_kahvalti", "ogun_ogle", "ogun_aksam", "aksam_kontrol",
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

    if name == "get_energy_profile":
        from app.services.calculations import (
            bmi as bmi_fn,
            bmr as bmr_fn,
            effective_activity_level,
            tdee as tdee_fn,
        )
        from app.services.targets import (
            compute_targets_for_user,
            get_profile,
            latest_body_fat,
            latest_weight,
        )

        profile = await get_profile(session, uid)
        if not profile or not profile.height_cm or not profile.age:
            return "HATA: profil eksik (boy/yaş yok)."
        weight = await latest_weight(session, uid) or profile.start_weight_kg
        if not weight:
            return "HATA: kayıtlı kilo yok."
        body_fat = await latest_body_fat(session, uid) or profile.body_fat_pct
        bmi_value = bmi_fn(weight, profile.height_cm)
        bmr_value = round(bmr_fn(weight, profile.height_cm, profile.age, profile.gender or "kadin", body_fat))
        activity = profile.activity_level or "hafif_aktif"
        eff_activity = effective_activity_level(activity, bmi_value)
        tdee_value = round(tdee_fn(bmr_value, eff_activity))
        recommended = await compute_targets_for_user(session, user)
        current = await get_current_targets(session, uid)

        from app.services.calculations import kcal_for_weekly_loss, max_safe_weekly_loss_kg

        gender = profile.gender or "kadin"
        pace_table = {
            f"{pace:g} kg/hafta": kcal_for_weekly_loss(tdee_value, pace, gender)
            for pace in (0.5, 0.75, 1.0)
        }
        payload = {
            "guncel_kilo_kg": weight,
            "bmi": bmi_value,
            "bazal_metabolizma_kcal": bmr_value,
            "beyan_edilen_aktivite": activity,
            "hesapta_kullanilan_aktivite": eff_activity,
            "tahmini_gunluk_harcama_kcal": tdee_value,
            "mevcut_hedef_kcal": current.kcal if current else None,
            "sistemin_onerdigi_guncel_hedef_kcal": recommended.kcal if recommended else None,
            "protein_tabani_g": recommended.protein_floor_g if recommended else None,
            "tempo_secenekleri_kcal": pace_table,
            "max_guvenli_tempo_kg_hafta": max_safe_weekly_loss_kg(weight),
            "not": (
                "Mevcut hedef ile önerilen hedef arasında belirgin fark varsa kullanıcıya söyle ve "
                "onaylarsa set_calorie_target ile güncelle. Tempo konuşulursa set_weight_loss_pace kullan."
            ),
        }
        return json.dumps(payload, ensure_ascii=False)

    if name == "set_weight_loss_pace":
        from app.services.calculations import (
            bmi as bmi_fn,
            bmr as bmr_fn,
            effective_activity_level,
            kcal_for_weekly_loss,
            max_safe_weekly_loss_kg,
            tdee as tdee_fn,
        )
        from app.services.targets import (
            compute_targets_for_user,
            get_profile,
            latest_body_fat,
            latest_weight,
            save_targets,
        )

        profile = await get_profile(session, uid)
        if not profile or not profile.height_cm or not profile.age:
            return "HATA: profil eksik (boy/yaş yok)."
        weight = await latest_weight(session, uid) or profile.start_weight_kg
        if not weight:
            return "HATA: kayıtlı kilo yok."
        body_fat = await latest_body_fat(session, uid) or profile.body_fat_pct
        gender = profile.gender or "kadin"

        requested = float(p["kg_per_week"])
        max_safe = max_safe_weekly_loss_kg(weight)
        pace = min(requested, max_safe)

        bmi_value = bmi_fn(weight, profile.height_cm)
        bmr_value = bmr_fn(weight, profile.height_cm, profile.age, gender, body_fat)
        tdee_value = tdee_fn(
            bmr_value, effective_activity_level(profile.activity_level or "hafif_aktif", bmi_value)
        )
        kcal = kcal_for_weekly_loss(tdee_value, pace, gender)
        targets = await compute_targets_for_user(session, user, kcal_override=kcal)
        if not targets:
            return "HATA: hedef hesaplanamadı."
        current = await get_current_targets(session, uid)
        row = await save_targets(
            session,
            uid,
            targets,
            diet_strategy=current.diet_strategy if current else "dengeli",
            reason=p.get("reason") or f"Hedef tempo: haftada {pace:g} kg kilo kaybı.",
        )
        # compute_targets may have raised kcal for the protein floor + minimum
        # fat; report the pace that the FINAL calorie figure actually delivers.
        achievable = round((tdee_value - row.kcal) * 7 / 7700, 2)
        notes = []
        if requested > max_safe:
            notes.append(
                f"İstenen {requested:g} kg/hafta güvenli sınırın üstünde; {max_safe:g} kg/hafta'ya kırpıldı."
            )
        if row.kcal > kcal:
            notes.append(
                f"Protein tabanı + minimum yağ için kalori {row.kcal} kcal'nin altına inemez; "
                f"bu kaloriyle gerçekçi tempo ~{achievable:g} kg/hafta."
            )
        return (
            f"Hedef güncellendi: {row.kcal} kcal/gün (protein {row.protein_g} g, karb {row.carb_g} g, "
            f"yağ {row.fat_g} g). Beklenen tempo: ~{achievable:g} kg/hafta."
            + ((" " + " ".join(notes)) if notes else "")
            + " ÖNEMLİ: Aktif haftalık plan ESKİ hedefe göre hazırlandı ve artık uyumsuz. "
            "Kullanıcı planın da güncellenmesini istiyorsa TEK ÖĞÜN DEĞİŞTİRMEYE ÇALIŞMA — "
            "regenerate_meal_plan çağır ki tüm hafta yeni hedefe göre yeniden hazırlansın."
        )

    if name == "set_calorie_target":
        from app.services.targets import compute_targets_for_user, save_targets

        kcal = int(p["kcal"])
        targets = await compute_targets_for_user(session, user, kcal_override=kcal)
        if not targets:
            return "HATA: profil/kilo eksik, hedef hesaplanamadı."
        current = await get_current_targets(session, uid)
        row = await save_targets(
            session,
            uid,
            targets,
            diet_strategy=current.diet_strategy if current else "dengeli",
            reason=p.get("reason") or "Kullanıcı isteğiyle kalori hedefi güncellendi.",
        )
        note = ""
        if row.kcal != kcal:
            note = f" (İstenen {kcal} kcal, protein tabanı + minimum yağ için {row.kcal} kcal'ye yükseltildi.)"
        return (
            f"Hedef güncellendi: {row.kcal} kcal, protein {row.protein_g} g, "
            f"karbonhidrat {row.carb_g} g, yağ {row.fat_g} g.{note} "
            "ÖNEMLİ: Aktif haftalık plan ESKİ hedefe göre hazırlandı ve artık uyumsuz. "
            "Kullanıcı planın da güncellenmesini istiyorsa TEK ÖĞÜN DEĞİŞTİRMEYE ÇALIŞMA — "
            "regenerate_meal_plan çağır ki tüm hafta yeni hedefe göre yeniden hazırlansın."
        )

    if name == "apply_plan_day_to_week":
        from app.services.shopping import build_weekly_shopping_list

        day_index = int(p["day_index"])
        if not 0 <= day_index <= 6:
            return "HATA: day_index 0-6 olmalı."
        today = date.today()
        week_start = today - timedelta(days=today.weekday())
        res = await session.execute(
            select(MealPlan)
            .where(MealPlan.user_id == uid, MealPlan.week_start == week_start, MealPlan.status == "active")
            .order_by(MealPlan.id.desc())
            .limit(1)
        )
        plan = res.scalar_one_or_none()
        if not plan:
            return "HATA: bu hafta için aktif plan yok."
        res = await session.execute(
            select(PlannedMeal).where(PlannedMeal.plan_id == plan.id)
        )
        all_meals = list(res.scalars())
        template = [m for m in all_meals if m.day_index == day_index]
        if not template:
            return f"HATA: planın {day_index}. gününde öğün yok."
        for m in all_meals:
            if m.day_index != day_index:
                await session.delete(m)
        for target_day in range(7):
            if target_day == day_index:
                continue
            for m in template:
                session.add(
                    PlannedMeal(
                        plan_id=plan.id,
                        day_index=target_day,
                        slot=m.slot,
                        name=m.name,
                        recipe=m.recipe,
                        prep_minutes=m.prep_minutes,
                        kcal=m.kcal,
                        protein_g=m.protein_g,
                        carb_g=m.carb_g,
                        fat_g=m.fat_g,
                        fiber_g=m.fiber_g,
                        ingredients=m.ingredients,
                        alternatives=m.alternatives,
                        shared_with_partner=m.shared_with_partner,
                    )
                )
        await session.flush()
        await build_weekly_shopping_list(session, week_start)
        day_names = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
        return f"{day_names[day_index]} menüsü haftanın 7 gününe uygulandı; alışveriş listesi güncellendi."

    if name == "pin_meal_slot":
        from app.services.shopping import build_weekly_shopping_list

        slot = p["slot"]
        source_day = int(p["source_day_index"])
        if not 0 <= source_day <= 6:
            return "HATA: source_day_index 0-6 olmalı."
        today = date.today()
        week_start = today - timedelta(days=today.weekday())
        res = await session.execute(
            select(MealPlan)
            .where(MealPlan.user_id == uid, MealPlan.week_start == week_start, MealPlan.status == "active")
            .order_by(MealPlan.id.desc())
            .limit(1)
        )
        plan = res.scalar_one_or_none()
        if not plan:
            return "HATA: bu hafta için aktif plan yok."
        res = await session.execute(select(PlannedMeal).where(PlannedMeal.plan_id == plan.id))
        all_meals = list(res.scalars())
        source = next((m for m in all_meals if m.day_index == source_day and m.slot == slot), None)
        if not source:
            return f"HATA: {source_day}. günde {slot} öğünü bulunamadı."
        # Overwrite that slot on every other day with a copy of the source meal.
        for m in all_meals:
            if m.slot == slot and m.day_index != source_day:
                await session.delete(m)
        for target_day in range(7):
            if target_day == source_day:
                continue
            session.add(
                PlannedMeal(
                    plan_id=plan.id, day_index=target_day, slot=slot,
                    name=source.name, recipe=source.recipe, prep_minutes=source.prep_minutes,
                    kcal=source.kcal, protein_g=source.protein_g, carb_g=source.carb_g,
                    fat_g=source.fat_g, fiber_g=source.fiber_g, ingredients=source.ingredients,
                    alternatives=source.alternatives, shared_with_partner=source.shared_with_partner,
                )
            )
        await session.flush()
        await build_weekly_shopping_list(session, week_start)
        slot_tr = {"kahvalti": "kahvaltı", "ara_ogun_1": "sabah ara öğünü", "ogle": "öğle",
                   "ara_ogun_2": "ikindi ara öğünü", "aksam": "akşam", "gece_atistirmasi": "gece atıştırması"}.get(slot, slot)
        msg = f"'{source.name}' her günün {slot_tr} öğünü olarak sabitlendi; alışveriş listesi güncellendi."
        if p.get("remember"):
            session.add(MemoryNote(
                user_id=uid, category="tercih",
                text=f"Sabit {slot_tr}: '{source.name}' — plan üretilirken bu öğün her gün aynı kalsın.",
            ))
            msg += " Gelecek planlarda da sabit tutacağım."
        return msg

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
        import asyncio

        from app.bot.bot import APPLICATION
        from app.scheduler.jobs import household_regenerate

        if APPLICATION is None:
            return "HATA: bot çalışmıyor, plan üretimi başlatılamadı."
        asyncio.create_task(household_regenerate(APPLICATION, user.telegram_id))
        return (
            "Ev halkının yeni haftalık planları arka planda hazırlanıyor (ortak menü, kişiye özel "
            "porsiyonlar). Birkaç dakika sürer; hazır olunca tablolar gruba otomatik gelecek. "
            "Kullanıcıya bekleyeceğini söyle."
        )

    if name == "log_lab_result":
        from datetime import date as _date

        from app.models import LabResult

        taken = None
        if p.get("taken_on"):
            try:
                taken = _date.fromisoformat(p["taken_on"])
            except ValueError:
                taken = None
        session.add(
            LabResult(
                user_id=uid, panel=p["panel"], value=float(p["value"]),
                unit=p.get("unit"), taken_on=taken,
            )
        )
        return f"Tahlil kaydedildi: {p['panel']} = {p['value']} {p.get('unit') or ''}".strip() + "."

    if name == "get_lab_history":
        from app.models import LabResult

        stmt = select(LabResult).where(LabResult.user_id == uid)
        if p.get("panel"):
            stmt = stmt.where(LabResult.panel.ilike(f"%{p['panel']}%"))
        stmt = stmt.order_by(LabResult.ts.desc()).limit(40)
        rows = list((await session.execute(stmt)).scalars())
        if not rows:
            return "Kayıtlı tahlil bulunamadı."
        return json.dumps(
            [
                {"panel": r.panel, "value": r.value, "unit": r.unit,
                 "tarih": (r.taken_on or r.ts.date()).isoformat()}
                for r in rows
            ],
            ensure_ascii=False,
        )

    if name == "lookup_barcode":
        from app.services.foodfacts import lookup

        info = await lookup(p["code"])
        if not info or info.get("kcal") is None:
            return (
                "Bu barkod OpenFoodFacts'ta bulunamadı. Ürünün besin değerleri tablosunu "
                "fotoğraflarsan değerleri elle ekleyebilirim."
            )
        display = " ".join(x for x in [info.get("brand"), info.get("name")] if x) or f"Ürün {p['code']}"
        res = await session.execute(select(Food).where(Food.name_tr == display))
        if not res.scalar_one_or_none():
            session.add(
                Food(
                    name_tr=display, category="market_urun",
                    kcal=info["kcal"] or 0, protein_g=info.get("protein_g") or 0,
                    carb_g=info.get("carb_g") or 0, fat_g=info.get("fat_g") or 0,
                    fiber_g=info.get("fiber_g") or 0,
                )
            )
        return (
            f"{display} (100 g): {info['kcal']} kcal, P{info.get('protein_g')} "
            f"K{info.get('carb_g')} Y{info.get('fat_g')}. Veritabanına eklendi."
        )

    if name == "get_adherence_analysis":
        from datetime import datetime as _dt, timezone as _tz

        from app.models import HungerLog, MealLog
        from app.services.analysis import adherence_breakdown

        since = _dt.now(_tz.utc) - timedelta(days=7)
        meals = list((await session.execute(
            select(MealLog).where(MealLog.user_id == uid, MealLog.ts >= since)
        )).scalars())
        cheats = [m for m in meals if m.is_cheat]
        hungers = list((await session.execute(
            select(HungerLog).where(
                HungerLog.user_id == uid, HungerLog.ts >= since, HungerLog.hunger >= 4
            )
        )).scalars())
        breakdown = adherence_breakdown(
            meals,
            cheat_count=len(cheats),
            cheat_hours=[m.ts.hour for m in cheats],
            high_hunger_hours=[h.ts.hour for h in hungers],
        )
        return json.dumps(breakdown, ensure_ascii=False)

    if name == "get_dining_out_context":
        from app.models import MealLog

        from datetime import datetime as _dt, time as _t, timezone as _tz

        targets = await get_current_targets(session, uid)
        today = date.today()
        start = _dt.combine(today, _t.min, tzinfo=_tz.utc)
        eaten = list((await session.execute(
            select(MealLog).where(MealLog.user_id == uid, MealLog.ts >= start)
        )).scalars())
        eaten_kcal = sum(m.kcal or 0 for m in eaten)
        res = await session.execute(
            select(FoodPreference).where(
                FoodPreference.user_id == uid, FoodPreference.level.in_(["asla", "sevmem"])
            )
        )
        avoid = [fp.name for fp in res.scalars()]
        payload = {
            "hedef_kcal": targets.kcal if targets else None,
            "protein_hedefi_g": targets.protein_g if targets else None,
            "bugun_yenen_kcal": eaten_kcal,
            "kalan_kcal": (targets.kcal - eaten_kcal) if targets else None,
            "kacinilacaklar": avoid,
        }
        return json.dumps(payload, ensure_ascii=False)

    if name == "set_auto_water":
        from app.services.targets import get_profile

        profile = await get_profile(session, uid)
        if not profile:
            return "HATA: profil bulunamadı."
        profile.auto_water = bool(p["enabled"])
        if profile.auto_water:
            return (
                "Otomatik su takibi AÇILDI. Bundan sonra su saatlerinde senin adına bardak ekleyip "
                "sadece haber vereceğim; tıklaman gerekmeyecek. İçmediğin olursa 'su içmedim' de."
            )
        return "Otomatik su takibi KAPATILDI. Yine tek dokunuşluk butonlarla soracağım."

    if name == "set_voice_replies":
        from app.services.targets import get_profile

        profile = await get_profile(session, uid)
        if not profile:
            return "HATA: profil bulunamadı."
        profile.voice_replies = bool(p["enabled"])
        if profile.voice_replies:
            return (
                "Sesli cevap AÇILDI. Bundan sonra yazının yanına kısa bir ses notu da ekleyeceğim. "
                "Kapatmak istersen 'sesli cevabı kapat' de."
            )
        return "Sesli cevap KAPATILDI. Yalnızca yazıyla devam."

    if name == "adjust_water":
        session.add(WaterLog(user_id=uid, amount_ml=int(p["amount_ml"])))
        ml = int(p["amount_ml"])
        if ml < 0:
            return f"{abs(ml)} ml su geri alındı."
        return f"{ml} ml su eklendi."

    if name == "set_wake_time":
        from datetime import time as dtime

        from app.models import ReminderSetting
        from app.services.targets import get_profile

        try:
            hh, mm = p["time"].split(":")
            wake_minutes = int(hh) * 60 + int(mm)
            if not 0 <= wake_minutes < 1440:
                raise ValueError
        except (ValueError, AttributeError, KeyError):
            return "HATA: saat HH:MM biçiminde olmalı."

        profile = await get_profile(session, uid)
        if profile:
            profile.wake_time = dtime(wake_minutes // 60, wake_minutes % 60)

        # Reminder offsets from wake time (minutes).
        offsets = {
            "gunaydin": 0,
            "tarti": 15,
            "ogun_kahvalti": 45,
            "su_1": 120,
            "ogun_ogle": 300,
            "su_2": 420,
            "ogun_aksam": 600,
            "su_3": 660,
            "aksam_kontrol": 780,
        }
        res = await session.execute(select(ReminderSetting).where(ReminderSetting.user_id == uid))
        existing = {r.kind: r for r in res.scalars()}
        summary = {}
        for kind, off in offsets.items():
            total = (wake_minutes + off) % 1440
            t = dtime(total // 60, total % 60)
            if kind in existing:
                existing[kind].time_of_day = t
            else:
                session.add(ReminderSetting(user_id=uid, kind=kind, time_of_day=t))
            summary[kind] = t.strftime("%H:%M")
        return (
            f"Uyanma saati {p['time']} olarak kaydedildi ve tüm hatırlatmalar buna göre kaydırıldı. "
            f"Kahvaltı ~{summary['ogun_kahvalti']}, öğle ~{summary['ogun_ogle']}, "
            f"akşam ~{summary['ogun_aksam']}, akşam değerlendirmesi ~{summary['aksam_kontrol']}."
        )

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
