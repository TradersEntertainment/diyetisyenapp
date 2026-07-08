"""Onboarding questionnaire — every mandated question, none skippable.

Implemented as a data-driven state machine: QUESTIONS drives a single
ConversationHandler state, so adding/reordering questions is a list edit.
Answers land in Profile + FoodPreference rows; at the end targets are computed
(protein floor included), stored in TargetHistory, and the first weekly plan +
default reminders are created.
"""
import logging
from dataclasses import dataclass, field
from datetime import time

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from app.db import session_scope
from app.models import FoodPreference, Profile, ReminderSetting, User
from app.services.calculations import compute_targets
from app.services.targets import primary_goal_of, save_targets

log = logging.getLogger(__name__)

ASKING = 1  # single conversation state; progress tracked in user_data["onb_idx"]

SKIP_WORDS = {"yok", "bilmiyorum", "geç", "gec", "-", "hayır", "hayir"}


@dataclass
class Q:
    key: str
    text: str
    kind: str = "text"  # text | number | int | choice | multi | pref_list
    options: list[tuple[str, str]] = field(default_factory=list)  # (value, label)
    allow_unknown: bool = False  # "bilmiyorum" accepted -> stores None
    pref_level: str | None = None  # for pref_list: preference level to store
    pref_category: str = "genel"
    min_val: float | None = None
    max_val: float | None = None


YES_NO = [("evet", "Evet"), ("hayir", "Hayır")]

QUESTIONS: list[Q] = [
    # ---------- Temel Bilgiler ----------
    Q("name", "Adın ne? 😊"),
    Q("age", "Kaç yaşındasın?", kind="int", min_val=12, max_val=100),
    Q("gender", "Cinsiyetin?", kind="choice", options=[("kadin", "Kadın"), ("erkek", "Erkek")]),
    Q("height_cm", "Boyun kaç cm?", kind="number", min_val=120, max_val=230),
    Q("start_weight_kg", "Şu anki kilon kaç kg? (örn: 84.5)", kind="number", min_val=30, max_val=300),
    Q("goal_weight_kg", "Hedef kilon kaç kg?", kind="number", min_val=30, max_val=300),
    Q(
        "body_fat_pct",
        "Vücut yağ oranın kaç? (%)\nBilmiyorsan 'bilmiyorum' yaz — bel/kalça/boyun ölçülerinden tahmin ederiz.",
        kind="number",
        allow_unknown=True,
        min_val=3,
        max_val=60,
    ),
    Q("muscle_mass_kg", "Kas kütlen kaç kg? (bilmiyorsan 'bilmiyorum')", kind="number", allow_unknown=True, min_val=10, max_val=100),
    Q("waist_cm", "Bel çevren kaç cm?", kind="number", min_val=40, max_val=200),
    Q("hip_cm", "Kalça çevren kaç cm?", kind="number", min_val=40, max_val=200),
    Q("neck_cm", "Boyun çevren kaç cm?", kind="number", min_val=20, max_val=70),
    Q(
        "activity_level",
        "Günlük aktivite seviyen?",
        kind="choice",
        options=[
            ("sedanter", "Masa başı, az hareket"),
            ("hafif_aktif", "Hafif aktif (haftada 1-3 egzersiz)"),
            ("orta_aktif", "Orta aktif (haftada 3-5 egzersiz)"),
            ("aktif", "Aktif (haftada 6-7 egzersiz)"),
            ("cok_aktif", "Çok aktif (fiziksel iş + egzersiz)"),
        ],
    ),
    Q("occupation", "Mesleğin ne?"),
    Q("daily_movement", "Gün içi hareketini anlat: yürüyor musun, araba mı kullanıyorsun, merdiven var mı?"),
    Q("sleep_hours", "Ortalama kaç saat uyuyorsun?", kind="number", min_val=3, max_val=14),
    # ---------- Sağlık ----------
    Q(
        "health_flags",
        "Sağlık durumların — sende olanları işaretle, sonra 'Devam'a bas:",
        kind="multi",
        options=[
            ("has_diabetes", "Diyabet"),
            ("has_thyroid", "Tiroid"),
            ("has_insulin_resistance", "İnsülin direnci"),
            ("has_hypertension", "Yüksek tansiyon"),
            ("has_cholesterol", "Kolesterol"),
            ("has_digestive_issues", "Sindirim sorunları"),
        ],
    ),
    Q("diseases", "Bunların dışında bir hastalığın var mı? (yoksa 'yok' yaz)", allow_unknown=True),
    Q("allergies", "Besin alerjin var mı? (yoksa 'yok')", allow_unknown=True),
    Q("intolerances", "Besin intoleransın var mı? (laktoz, gluten... yoksa 'yok')", allow_unknown=True),
    Q("medications", "Düzenli kullandığın ilaç var mı? (yoksa 'yok')", allow_unknown=True),
    Q("supplements", "Kullandığın takviye var mı? (protein tozu, vitamin... yoksa 'yok')", allow_unknown=True),
    Q("surgeries", "Geçirdiğin ameliyat var mı? (yoksa 'yok')", allow_unknown=True),
    # ---------- Hedefler ----------
    Q(
        "goals",
        "Hedeflerin neler? Önce EN ÖNEMLİSİNİ seç, birden fazla işaretleyebilirsin:",
        kind="multi",
        options=[
            ("yag_kaybi", "Yağ kaybı"),
            ("kas_kazanimi", "Kas kazanımı"),
            ("kilo_koruma", "Kilo koruma"),
            ("kan_degerleri", "Kan değerlerini iyileştirme"),
            ("enerji", "Daha çok enerji"),
            ("uyku", "Daha iyi uyku"),
            ("saglikli_yasam", "Sağlıklı yaşam tarzı"),
        ],
    ),
    # ---------- Egzersiz ----------
    Q(
        "exercise_types",
        "Hangi egzersizleri yapıyorsun / yapmak istiyorsun?",
        kind="multi",
        options=[
            ("gym", "Spor salonu"),
            ("ev_antrenmani", "Ev antrenmanı"),
            ("yuruyus", "Yürüyüş"),
            ("kosu", "Koşu"),
            ("bisiklet", "Bisiklet"),
            ("yuzme", "Yüzme"),
            ("pilates", "Pilates"),
            ("yoga", "Yoga"),
        ],
    ),
    Q("exercise_frequency_per_week", "Haftada kaç gün egzersiz yapıyorsun? (0-7)", kind="int", min_val=0, max_val=7),
    Q("exercise_duration_min", "Bir seans ortalama kaç dakika sürüyor? (yapmıyorsan 0)", kind="int", min_val=0, max_val=300),
    # ---------- Beslenme Alışkanlıkları ----------
    Q(
        "meals_eaten",
        "Hangi öğünleri düzenli yersin?",
        kind="multi",
        options=[
            ("eats_breakfast", "Kahvaltı"),
            ("eats_lunch", "Öğle"),
            ("eats_dinner", "Akşam"),
            ("eats_snacks", "Ara öğün / atıştırma"),
        ],
    ),
    Q("eats_outside", "Dışarıda ne sıklıkla yemek yersin? (örn: haftada 2 kez, iş yemekleri...)"),
    Q("coffee_per_day", "Günde kaç fincan kahve içersin? (içmiyorsan 0)", kind="int", min_val=0, max_val=20),
    Q("tea_per_day", "Günde kaç bardak çay?", kind="int", min_val=0, max_val=30),
    Q("water_glasses_per_day", "Günde yaklaşık kaç bardak su içersin?", kind="int", min_val=0, max_val=30),
    Q("alcohol", "Alkol kullanıyor musun? Ne sıklıkla? (kullanmıyorsan 'yok')", allow_unknown=True),
    Q("smoking", "Sigara kullanıyor musun? (kullanmıyorsan 'yok')", allow_unknown=True),
    # ---------- Yiyecek Tercihleri ----------
    Q("pref_love", "Şimdi tercihlerin! 🥗\nBAYILDIĞIN yiyecekleri virgülle yaz (örn: mercimek çorbası, ızgara tavuk, çilek):", kind="pref_list", pref_level="bayilirim"),
    Q("pref_like", "SEVDİĞİN yiyecekler? (virgülle)", kind="pref_list", pref_level="severim"),
    Q("pref_can_eat", "Çok sevmesen de YİYEBİLDİĞİN yiyecekler?", kind="pref_list", pref_level="yiyebilirim"),
    Q("pref_dislike", "SEVMEDİĞİN yiyecekler?", kind="pref_list", pref_level="sevmem"),
    Q("pref_never", "ASLA yemediğin/yemeyeceğin yiyecekler? (alerji, inanç, tiksinti... yoksa 'yok')", kind="pref_list", pref_level="asla", allow_unknown=True),
    Q("pref_cuisines", "En sevdiğin mutfaklar? (Türk, İtalyan, Uzak Doğu...)", kind="pref_list", pref_level="severim", pref_category="mutfak"),
    Q("pref_vegetables", "Favori sebzelerin?", kind="pref_list", pref_level="severim", pref_category="sebze"),
    Q("pref_fruits", "Favori meyvelerin?", kind="pref_list", pref_level="severim", pref_category="meyve"),
    Q("pref_meat", "Favori et/protein kaynakların? (tavuk, balık, kırmızı et, yumurta...)", kind="pref_list", pref_level="severim", pref_category="et"),
    Q("pref_desserts", "Favori tatlıların?", kind="pref_list", pref_level="severim", pref_category="tatli"),
    Q("pref_drinks", "Favori içeceklerin?", kind="pref_list", pref_level="severim", pref_category="icecek"),
    Q("pref_snacks", "Favori atıştırmalıkların?", kind="pref_list", pref_level="severim", pref_category="atistirmalik"),
    Q("pref_breakfast", "Favori kahvaltın nasıl olur?", kind="pref_list", pref_level="severim", pref_category="kahvalti"),
    Q("pref_dinner", "Favori akşam yemeklerin?", kind="pref_list", pref_level="severim", pref_category="aksam"),
    Q("pref_restaurants", "Favori restoranların / restoran türlerin?", kind="pref_list", pref_level="severim", pref_category="restoran", allow_unknown=True),
    Q("pref_cheat", "Favori kaçamağın ne? 🍕", kind="pref_list", pref_level="bayilirim", pref_category="kacamak"),
    # ---------- Mutfak / Bütçe / Alışveriş ----------
    Q(
        "cooking_skill",
        "Yemek yapma becerin nasıl?",
        kind="choice",
        options=[
            ("yok", "Hiç yapamam"),
            ("temel", "Temel (basit şeyler)"),
            ("iyi", "İyi"),
            ("cok_iyi", "Çok iyi"),
        ],
    ),
    Q(
        "kitchen_equipment",
        "Mutfakta hangi ekipmanların var?",
        kind="multi",
        options=[
            ("firin", "Fırın"),
            ("airfryer", "Airfryer"),
            ("blender", "Blender"),
            ("duduklu", "Düdüklü tencere"),
            ("izgara", "Izgara/tava"),
            ("mikrodalga", "Mikrodalga"),
            ("robot", "Mutfak robotu"),
        ],
    ),
    Q("monthly_food_budget", "Aylık gıda bütçen yaklaşık ne kadar? (örn: 15000 TL, kısıtlı, rahat...)"),
    Q("shopping_preferences", "Alışverişi nereden ve ne sıklıkla yaparsınız? (pazar, market, online...)"),
]

KEY_INDEX = {q.key: i for i, q in enumerate(QUESTIONS)}

PROFILE_FIELDS = {
    "age", "gender", "height_cm", "start_weight_kg", "goal_weight_kg", "body_fat_pct",
    "muscle_mass_kg", "waist_cm", "hip_cm", "neck_cm", "activity_level", "occupation",
    "daily_movement", "sleep_hours", "diseases", "allergies", "intolerances", "medications",
    "supplements", "surgeries", "goals", "exercise_types", "exercise_frequency_per_week",
    "exercise_duration_min", "eats_outside", "coffee_per_day", "tea_per_day",
    "water_glasses_per_day", "alcohol", "smoking", "cooking_skill", "kitchen_equipment",
    "monthly_food_budget", "shopping_preferences",
}

DEFAULT_REMINDERS = [
    ("gunaydin", time(8, 0)),
    ("tarti", time(8, 15)),
    ("su_1", time(11, 0)),
    ("su_2", time(15, 0)),
    ("su_3", time(18, 30)),
    ("ogun_ogle", time(12, 30)),
    ("ogun_aksam", time(19, 0)),
    ("aksam_kontrol", time(21, 30)),
]


def _question_markup(q: Q, selected: set[str] | None = None) -> InlineKeyboardMarkup | None:
    if q.kind == "choice":
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(label, callback_data=f"onb:{value}")] for value, label in q.options]
        )
    if q.kind == "multi":
        selected = selected or set()
        rows = [
            [
                InlineKeyboardButton(
                    ("✅ " if value in selected else "☐ ") + label, callback_data=f"onbm:{value}"
                )
            ]
            for value, label in q.options
        ]
        rows.append([InlineKeyboardButton("➡️ Devam", callback_data="onbm:__done__")])
        return InlineKeyboardMarkup(rows)
    return None


async def _ask_current(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    idx = context.user_data["onb_idx"]
    q = QUESTIONS[idx]
    total = len(QUESTIONS)
    text = f"({idx + 1}/{total}) {q.text}"
    markup = _question_markup(q, context.user_data.get("onb_multi"))
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        chat = update.effective_chat
        await chat.send_message(text, reply_markup=markup)


async def start_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["onb_idx"] = 0
    context.user_data["onb_answers"] = {}
    context.user_data["onb_prefs"] = []
    context.user_data["onb_multi"] = set()
    await update.effective_chat.send_message(
        "Merhaba! Ben senin kişisel diyetisyeninim. 🌱\n\n"
        "Sana gerçekten uyan bir beslenme planı için önce seni tanımam gerekiyor. "
        "Sorular biraz detaylı ama hepsini bir kere cevaplıyorsun — her şeyi kalıcı olarak hatırlayacağım.\n\n"
        "Hazırsan başlıyoruz! 👇"
    )
    await _ask_current(update, context)
    return ASKING


def _parse_number(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ".").strip().rstrip("%"))
    except ValueError:
        return None


async def handle_text_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    idx = context.user_data.get("onb_idx", 0)
    q = QUESTIONS[idx]
    raw = (update.message.text or "").strip()
    answers = context.user_data["onb_answers"]

    if q.kind in ("choice", "multi"):
        await update.message.reply_text("Lütfen yukarıdaki butonlardan seç 🙂")
        return ASKING

    if q.allow_unknown and raw.lower() in SKIP_WORDS:
        if q.kind == "pref_list":
            pass  # nothing to store
        else:
            answers[q.key] = None
    elif q.kind in ("number", "int"):
        val = _parse_number(raw)
        if val is None:
            await update.message.reply_text("Bunu sayı olarak alabilir miyim? (örn: 84.5)")
            return ASKING
        if q.min_val is not None and val < q.min_val or q.max_val is not None and val > q.max_val:
            await update.message.reply_text(
                f"Bu değer beklenen aralığın dışında ({q.min_val:g}-{q.max_val:g}). Tekrar yazar mısın?"
            )
            return ASKING
        answers[q.key] = int(val) if q.kind == "int" else val
    elif q.kind == "pref_list":
        items = [s.strip() for s in raw.replace("\n", ",").split(",") if s.strip()]
        for item in items:
            context.user_data["onb_prefs"].append((item, q.pref_level, q.pref_category))
        answers[q.key] = items
    else:
        answers[q.key] = raw

    return await _advance(update, context)


async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    idx = context.user_data.get("onb_idx", 0)
    q = QUESTIONS[idx]
    data = query.data

    if data.startswith("onb:") and q.kind == "choice":
        context.user_data["onb_answers"][q.key] = data.split(":", 1)[1]
        return await _advance(update, context, edited=True)

    if data.startswith("onbm:") and q.kind == "multi":
        value = data.split(":", 1)[1]
        selected: set[str] = context.user_data.setdefault("onb_multi", set())
        if value == "__done__":
            context.user_data["onb_answers"][q.key] = sorted(selected)
            context.user_data["onb_multi"] = set()
            return await _advance(update, context, edited=True)
        selected.symmetric_difference_update({value})
        await _ask_current(update, context, edit=True)
        return ASKING

    return ASKING


async def _advance(update: Update, context: ContextTypes.DEFAULT_TYPE, edited: bool = False) -> int:
    context.user_data["onb_idx"] += 1
    if context.user_data["onb_idx"] >= len(QUESTIONS):
        return await _finish(update, context)
    await _ask_current(update, context)
    return ASKING


async def _finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    answers: dict = context.user_data["onb_answers"]
    prefs: list[tuple[str, str, str]] = context.user_data["onb_prefs"]
    tg_id = update.effective_user.id

    async with session_scope() as session:
        res = await session.execute(select(User).where(User.telegram_id == tg_id))
        user = res.scalar_one()
        user.name = answers.get("name") or update.effective_user.first_name or "Dostum"
        user.onboarding_state = "active"

        profile = Profile(user_id=user.id)
        for key in PROFILE_FIELDS:
            if key in answers:
                setattr(profile, key, answers[key])
        for flag in answers.get("health_flags", []):
            setattr(profile, flag, True)
        meals = set(answers.get("meals_eaten", []))
        profile.eats_breakfast = "eats_breakfast" in meals
        profile.eats_lunch = "eats_lunch" in meals
        profile.eats_dinner = "eats_dinner" in meals
        profile.eats_snacks = "eats_snacks" in meals
        session.add(profile)

        seen = set()
        for name, level, category in prefs:
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            session.add(FoodPreference(user_id=user.id, name=name, level=level, category=category))

        for kind, t in DEFAULT_REMINDERS:
            session.add(ReminderSetting(user_id=user.id, kind=kind, time_of_day=t))

        # Estimate body fat from Navy formula if unknown
        body_fat = profile.body_fat_pct
        if body_fat is None and profile.waist_cm and profile.neck_cm and profile.height_cm:
            from app.services.calculations import navy_body_fat_pct

            body_fat = navy_body_fat_pct(
                profile.gender or "kadin",
                profile.height_cm,
                profile.waist_cm,
                profile.neck_cm,
                profile.hip_cm,
            )
            profile.body_fat_pct = body_fat

        goal = primary_goal_of(profile)
        targets = compute_targets(
            weight_kg=profile.start_weight_kg,
            height_cm=profile.height_cm,
            age=profile.age,
            gender=profile.gender or "kadin",
            activity_level=profile.activity_level or "hafif_aktif",
            primary_goal=goal,
            body_fat_pct=body_fat,
            exercise_days_per_week=profile.exercise_frequency_per_week,
        )
        await save_targets(
            session,
            user.id,
            targets,
            diet_strategy="dengeli",
            reason="Başlangıç hedefleri (onboarding).",
        )

        # First weight log = starting weight
        from app.models import WeightLog

        session.add(WeightLog(user_id=user.id, weight_kg=profile.start_weight_kg))

    bf_line = f"• Yağ oranı: %{body_fat:g}\n" if body_fat else ""
    await update.effective_chat.send_message(
        "Harika, tanıştığımıza memnun oldum! 🎉 İşte başlangıç hedeflerin:\n\n"
        f"• Kalori: {targets.kcal} kcal/gün\n"
        f"• Protein: {targets.protein_g} g/gün  (taban: {targets.protein_floor_g} g — bunun altına asla inmeyiz 💪)\n"
        f"• Karbonhidrat: {targets.carb_g} g | Yağ: {targets.fat_g} g | Lif: {targets.fiber_g} g\n"
        f"• Su: {targets.water_ml} ml/gün\n"
        f"{bf_line}\n"
        "Protein hedefin vücut analizine göre hesaplandı ve her tartıda güncellenecek. "
        "Diyet tarzını ise sabitlemiyorum — gidişata göre birlikte şekillendireceğiz.\n\n"
        "Şimdi ilk haftalık planını hazırlıyorum, birkaç dakika sürebilir... 🍽️"
    )

    # Generate the first weekly plan in the background so the chat isn't blocked.
    import asyncio

    from app.scheduler.jobs import generate_plan_for_user_bg

    asyncio.create_task(generate_plan_for_user_bg(tg_id, context.application))

    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_chat.send_message(
        "Onboarding'i yarıda bıraktın. /start ile kaldığın yerden değil, baştan başlayabiliriz."
    )
    context.user_data.clear()
    return ConversationHandler.END
