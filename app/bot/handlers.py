"""Telegram command handlers + free-chat routing to the AI dietitian."""
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.bot import charts
from app.config import get_settings
from app.db import session_scope
from app.models import (
    ProgressPhoto,
    ShoppingItem,
    ShoppingList,
    SleepLog,
    User,
    WaterLog,
    WeightLog,
)
from app.services.reports import (
    body_comp_series,
    daily_facts,
    format_weekly_stats_tr,
    gather_weekly_stats,
    get_current_targets,
    weight_series,
)
from app.services.shopping import CATEGORY_LABELS_TR

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- helpers


async def get_user(tg_id: int) -> User | None:
    async with session_scope() as session:
        res = await session.execute(select(User).where(User.telegram_id == tg_id))
        return res.scalar_one_or_none()


async def get_or_create_user(update: Update) -> User:
    tg = update.effective_user
    async with session_scope() as session:
        res = await session.execute(select(User).where(User.telegram_id == tg.id))
        user = res.scalar_one_or_none()
        if user is None:
            user = User(telegram_id=tg.id, name=tg.first_name or "", onboarding_state="new")
            session.add(user)
            await session.flush()
        return user


def _is_group(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type in ("group", "supergroup"))


async def _capture_group(update: Update, *, force: bool = False) -> None:
    """Remember the shared group's chat id the first time we see it."""
    from app.services.group import confirm_group_cache, set_group_chat_id

    try:
        async with session_scope() as session:
            changed = await set_group_chat_id(session, update.effective_chat.id, force=force)
        # Only reached when the transaction committed; a failed commit leaves the
        # cache untouched so the next update retries the write.
        if changed:
            confirm_group_cache(update.effective_chat.id)
    except Exception:
        log.exception("failed to store group chat id")


async def guard_allowlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Group -1 gate: silently reject anyone not in ALLOWED_TELEGRAM_IDS."""
    from telegram.ext import ApplicationHandlerStop

    tg = update.effective_user
    if tg is None or tg.id not in get_settings().allowed_telegram_ids:
        # In the shared group strangers are ignored silently; in DMs we reply once.
        if update.effective_chat and not _is_group(update):
            await update.effective_chat.send_message(
                "Üzgünüm, bu asistan kişisel kullanım içindir. 🙏"
            )
        raise ApplicationHandlerStop

    if _is_group(update):
        await _capture_group(update)


async def cb_su(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """One-tap water logging from reminder buttons. Whoever taps logs their own
    water, so both housemates can use the same button in the group."""
    query = update.callback_query
    try:
        ml = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return await query.answer()
    user = await get_user(query.from_user.id)
    if user is None or user.onboarding_state != "active":
        return await query.answer("Önce /start ile tanışalım 🙂")
    async with session_scope() as session:
        session.add(WaterLog(user_id=user.id, amount_ml=ml))
    await query.answer(f"💧 +{ml} ml kaydedildi, sağlığına!")


async def cb_onboarding_foreign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Onboarding button tapped by someone who isn't in that questionnaire."""
    await update.callback_query.answer("Bu soru sana değil 🙂")


async def on_new_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Greet the household when the bot itself is added to the shared group."""
    members = update.message.new_chat_members if update.message else []
    if not any(m.id == context.bot.id for m in members):
        return
    # Being added to a group is a deliberate act: adopt it even over an old id.
    await _capture_group(update, force=True)
    await update.effective_chat.send_message(
        "Merhaba! Ben sizin diyetisyeninizim, artık buradayım. 🌱\n\n"
        "Bundan sonra ikinizle de bu gruptan konuşacağım: tartı sonuçlarınızı soracağım, "
        "planlarınızı buraya atacağım, gününüzü takip edeceğim.\n\n"
        "Tanışmadığımız kişi /start yazsın, hemen başlayalım! 👇"
    )


def _require_active(user: User | None) -> str | None:
    if user is None or user.onboarding_state != "active":
        return "Önce tanışalım! /start yazarak başlayabilirsin. 🙂"
    return None


# ---------------------------------------------------------------- /start & help


async def cmd_start_active(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start for an already-onboarded user (the ConversationHandler catches new users)."""
    user = await get_user(update.effective_user.id)
    await update.message.reply_text(
        f"Tekrar hoş geldin {user.name}! 🌟 Bana istediğini yazabilirsin: "
        "kilonu, yediklerini, suyunu... Komutlar için /yardim."
    )


HELP_TEXT = (
    "🥗 *Diyetisyen — Komutlar*\n\n"
    "Bana normal mesaj yazman yeterli: \"bugün 84.2'yim, öğlen mercimek çorbası içtim\" gibi. "
    "Her şeyi anlarım ve kaydederim.\n\n"
    "/plan — bugünün öğünleri (\"/plan hafta\" tüm hafta)\n"
    "/kilo 84.5 — hızlı kilo kaydı\n"
    "/su 500 — su kaydı (ml)\n"
    "/hedef — güncel kalori/makro hedeflerin\n"
    "/rapor — günlük / haftalık / aylık rapor\n"
    "/grafik — ilerleme grafikleri\n"
    "/alisveris — ortak alışveriş listesi\n"
    "/foto — ilerleme fotoğrafı gönder\n"
    "/ayarlar — hatırlatma ayarları\n"
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


# ---------------------------------------------------------------- quick logging


async def cmd_kilo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id)
    if msg := _require_active(user):
        return await update.message.reply_text(msg)
    if not context.args:
        return await update.message.reply_text("Kullanım: /kilo 84.5")
    try:
        weight = float(context.args[0].replace(",", "."))
    except ValueError:
        return await update.message.reply_text("Sayı olarak yazar mısın? Örn: /kilo 84.5")
    async with session_scope() as session:
        res = await session.execute(select(User).where(User.telegram_id == update.effective_user.id))
        user = res.scalar_one()
        session.add(WeightLog(user_id=user.id, weight_kg=weight))
        from app.services.targets import ensure_protein_floor

        raised = await ensure_protein_floor(session, user)
    extra = f"\n💪 Protein tabanın güncellendi: {raised.protein_g} g" if raised else ""
    await update.message.reply_text(f"Kaydettim: {weight:g} kg ✅{extra}")


async def cmd_su(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id)
    if msg := _require_active(user):
        return await update.message.reply_text(msg)
    amount = 200
    if context.args:
        try:
            val = int(float(context.args[0].replace(",", ".")))
            amount = val if val >= 50 else val * 200  # "2" -> 2 bardak
        except ValueError:
            return await update.message.reply_text("Örn: /su 500 (ml) veya /su 2 (bardak)")
    async with session_scope() as session:
        res = await session.execute(select(User).where(User.telegram_id == update.effective_user.id))
        user = res.scalar_one()
        session.add(WaterLog(user_id=user.id, amount_ml=amount))
        facts = await daily_facts(session, user)
    total = facts["water_ml"] + amount
    target = facts["targets"]["water_ml"]
    bar = f" ({total}/{target} ml)" if target else ""
    await update.message.reply_text(f"💧 +{amount} ml{bar}")


# ---------------------------------------------------------------- plan & targets


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id)
    if msg := _require_active(user):
        return await update.message.reply_text(msg)
    whole_week = bool(context.args and context.args[0].lower().startswith("haft"))

    from app.models import MealPlan, PlannedMeal

    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    async with session_scope() as session:
        res = await session.execute(
            select(MealPlan)
            .where(MealPlan.user_id == user.id, MealPlan.week_start == week_start, MealPlan.status == "active")
            .order_by(MealPlan.id.desc())
            .limit(1)
        )
        plan = res.scalar_one_or_none()
        if not plan:
            return await update.message.reply_text(
                "Bu hafta için henüz plan yok. Pazar akşamı otomatik hazırlanır; "
                "istersen \"bana yeni plan hazırla\" yazman yeterli. 🙂"
            )
        res = await session.execute(select(PlannedMeal).where(PlannedMeal.plan_id == plan.id))
        meals = list(res.scalars())

    day_names = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
    slot_names = {
        "kahvalti": "🍳 Kahvaltı",
        "ara_ogun_1": "🥜 Ara öğün",
        "ogle": "🍲 Öğle",
        "ara_ogun_2": "🍎 Ara öğün",
        "aksam": "🍽 Akşam",
        "gece_atistirmasi": "🌙 Gece",
    }
    slot_order = ["kahvalti", "ara_ogun_1", "ogle", "ara_ogun_2", "aksam", "gece_atistirmasi"]

    def day_block(day_idx: int) -> str:
        day_meals = sorted(
            (m for m in meals if m.day_index == day_idx),
            key=lambda m: slot_order.index(m.slot) if m.slot in slot_order else 9,
        )
        if not day_meals:
            return ""
        lines = [f"*{day_names[day_idx]}*"]
        total_kcal = sum(m.kcal for m in day_meals)
        total_p = sum(m.protein_g for m in day_meals)
        for m in day_meals:
            lines.append(f"{slot_names.get(m.slot, m.slot)}: {m.name} — {m.kcal} kcal, P{m.protein_g:g}")
        lines.append(f"_Toplam: {total_kcal} kcal, protein {total_p:g} g_")
        return "\n".join(lines)

    if whole_week:
        blocks = [day_block(i) for i in range(7)]
        text = f"📅 *Haftalık Plan* (strateji: {plan.diet_strategy})\n\n" + "\n\n".join(b for b in blocks if b)
    else:
        text = f"📅 *Bugünün Planı* (strateji: {plan.diet_strategy})\n\n" + (
            day_block(today.weekday()) or "Bugün için öğün bulunamadı."
        )
        text += "\n\nTarif için öğünün adını yazman yeterli. Beğenmediğin öğünü değiştirmemi isteyebilirsin. 😊"
    await update.message.reply_text(text[:4000], parse_mode="Markdown")


async def cmd_hedef(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id)
    if msg := _require_active(user):
        return await update.message.reply_text(msg)
    async with session_scope() as session:
        res = await session.execute(select(User).where(User.telegram_id == update.effective_user.id))
        user = res.scalar_one()
        targets = await get_current_targets(session, user.id)
        from app.services.targets import compute_targets_for_user

        computed = await compute_targets_for_user(session, user)
    if not targets:
        return await update.message.reply_text("Henüz hedef hesaplanmadı — /start ile tanışalım.")
    floor = computed.protein_floor_g if computed else targets.protein_g
    await update.message.reply_text(
        "🎯 *Güncel Hedeflerin*\n\n"
        f"Kalori: *{targets.kcal}* kcal\n"
        f"Protein: *{targets.protein_g} g*  (vücut analizi tabanı: {floor} g — asla altına inmeyiz)\n"
        f"Karbonhidrat: {targets.carb_g} g\nYağ: {targets.fat_g} g\nLif: {targets.fiber_g} g\n"
        f"Su: {targets.water_ml} ml\n\n"
        f"Diyet stratejisi: _{targets.diet_strategy}_\n"
        + (f"Gerekçe: {targets.reason}" if targets.reason else ""),
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------- shopping list


async def _render_shopping(session) -> tuple[str, InlineKeyboardMarkup | None]:
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    res = await session.execute(select(ShoppingList).where(ShoppingList.week_start == week_start))
    slist = res.scalar_one_or_none()
    if not slist:
        return ("Bu hafta için liste yok. Yeni plan oluşunca otomatik hazırlanır. 🛒", None)
    res = await session.execute(
        select(ShoppingItem).where(ShoppingItem.list_id == slist.id).order_by(ShoppingItem.category)
    )
    items = list(res.scalars())
    if not items:
        return ("Liste boş görünüyor.", None)
    lines = ["🛒 *Bu Haftanın Ortak Listesi*"]
    current_cat = None
    buttons = []
    for item in items:
        if item.category != current_cat:
            current_cat = item.category
            lines.append(f"\n{CATEGORY_LABELS_TR.get(current_cat, current_cat)}")
        mark = "✅" if item.checked else "•"
        qty = f" — {item.quantity}" if item.quantity else ""
        lines.append(f"  {mark} {item.name}{qty}")
        if not item.checked:
            buttons.append(InlineKeyboardButton(f"✔️ {item.name[:24]}", callback_data=f"shop:{item.id}"))
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)][:20]
    return ("\n".join(lines)[:4000], InlineKeyboardMarkup(rows) if rows else None)


async def cmd_alisveris(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id)
    if msg := _require_active(user):
        return await update.message.reply_text(msg)
    async with session_scope() as session:
        text, markup = await _render_shopping(session)
    await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")


async def cb_shopping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    item_id = int(query.data.split(":", 1)[1])
    async with session_scope() as session:
        item = await session.get(ShoppingItem, item_id)
        if item:
            item.checked = True
        text, markup = await _render_shopping(session)
    await query.answer("Alındı olarak işaretlendi ✅")
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    except Exception:  # message unchanged etc.
        pass


# ---------------------------------------------------------------- reports


async def cmd_rapor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id)
    if msg := _require_active(user):
        return await update.message.reply_text(msg)
    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📆 Günlük", callback_data="rapor:gun"),
                InlineKeyboardButton("📈 Haftalık", callback_data="rapor:hafta"),
                InlineKeyboardButton("🗓 Aylık", callback_data="rapor:ay"),
            ]
        ]
    )
    await update.message.reply_text("Hangi raporu istersin?", reply_markup=markup)


async def cb_rapor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kind = query.data.split(":", 1)[1]
    await query.edit_message_text("Rapor hazırlanıyor... ⏳")

    from app.ai.dietitian import generate_message

    async with session_scope() as session:
        res = await session.execute(select(User).where(User.telegram_id == update.effective_user.id))
        user = res.scalar_one()
        if kind == "gun":
            facts = await daily_facts(session, user)
            instruction = (
                "Bugünün verileriyle kısa bir GÜNLÜK RAPOR yaz (kalori/protein/su hedef karşılaştırması, "
                f"1-2 cümle değerlendirme + yarın için tek öneri):\n{facts}"
            )
        elif kind == "hafta":
            stats = await gather_weekly_stats(session, user)
            instruction = (
                "Aşağıdaki hesaplanmış haftalık verilerle sıcak bir HAFTALIK RAPOR yaz. Sayıları aynen kullan, "
                "yenisini uydurma. Kaçamak varsa suçlamadan geç:\n" + format_weekly_stats_tr(stats)
            )
        else:
            weights = await weight_series(session, user.id, days=31)
            month_change = round(weights[-1][1] - weights[0][1], 1) if len(weights) >= 2 else None
            stats = await gather_weekly_stats(session, user)
            instruction = (
                "AYLIK RAPOR yaz: 30 günlük kilo değişimi "
                + (f"{month_change:+} kg. " if month_change is not None else "(veri az). ")
                + "Son hafta özeti:\n"
                + format_weekly_stats_tr(stats)
                + "\nGenel gidişatı değerlendir, gelecek ay için 2-3 odak öner."
            )
        text = await generate_message(session, user, instruction)
    await query.edit_message_text(text[:4000] if text else "Rapor oluşturulamadı, tekrar dener misin?")


# ---------------------------------------------------------------- charts


async def cmd_grafik(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id)
    if msg := _require_active(user):
        return await update.message.reply_text(msg)
    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⚖️ Kilo", callback_data="grafik:kilo"),
                InlineKeyboardButton("📉 Yağ %", callback_data="grafik:yag"),
                InlineKeyboardButton("💪 Kas", callback_data="grafik:kas"),
            ],
            [
                InlineKeyboardButton("🔥 Kalori", callback_data="grafik:kalori"),
                InlineKeyboardButton("💧 Su", callback_data="grafik:su"),
                InlineKeyboardButton("😴 Uyku", callback_data="grafik:uyku"),
            ],
        ]
    )
    await update.message.reply_text("Hangi grafiği görmek istersin?", reply_markup=markup)


async def cb_grafik(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kind = query.data.split(":", 1)[1]

    async with session_scope() as session:
        res = await session.execute(select(User).where(User.telegram_id == update.effective_user.id))
        user = res.scalar_one()
        targets = await get_current_targets(session, user.id)

        buf = None
        if kind == "kilo":
            series = await weight_series(session, user.id, days=120)
            from app.services.targets import get_profile

            profile = await get_profile(session, user.id)
            goal = profile.goal_weight_kg if profile else None
            buf = charts.line_chart(series, "Kilo Takibi", "kg", target=goal, target_label="Hedef kilo")
        elif kind == "yag":
            fat, _ = await body_comp_series(session, user.id, days=180)
            buf = charts.line_chart(fat, "Vücut Yağ Oranı", "%")
        elif kind == "kas":
            _, muscle = await body_comp_series(session, user.id, days=180)
            buf = charts.line_chart(muscle, "Kas Kütlesi", "kg")
        elif kind == "kalori":
            from app.models import MealLog

            since = datetime.now(timezone.utc) - timedelta(days=14)
            res = await session.execute(
                select(MealLog).where(MealLog.user_id == user.id, MealLog.ts >= since)
            )
            by_day: dict[date, float] = {}
            for m in res.scalars():
                by_day[m.ts.date()] = by_day.get(m.ts.date(), 0) + (m.kcal or 0)
            series = sorted(by_day.items())
            buf = charts.bar_chart(series, "Günlük Kalori (son 14 gün)", "kcal", target=targets.kcal if targets else None)
        elif kind == "su":
            since = datetime.now(timezone.utc) - timedelta(days=14)
            res = await session.execute(
                select(WaterLog).where(WaterLog.user_id == user.id, WaterLog.ts >= since)
            )
            by_day = {}
            for w in res.scalars():
                by_day[w.ts.date()] = by_day.get(w.ts.date(), 0) + w.amount_ml
            series = sorted(by_day.items())
            buf = charts.bar_chart(series, "Günlük Su (son 14 gün)", "ml", target=targets.water_ml if targets else None)
        elif kind == "uyku":
            since = datetime.now(timezone.utc) - timedelta(days=21)
            res = await session.execute(
                select(SleepLog).where(SleepLog.user_id == user.id, SleepLog.ts >= since)
            )
            series = sorted((s.ts.date(), s.hours) for s in res.scalars())
            buf = charts.bar_chart(series, "Uyku (son 21 gün)", "saat", target=8)

    if buf is None:
        return await query.edit_message_text("Bu grafik için henüz yeterli veri yok. Birkaç gün kayıt sonrası tekrar dene 🙂")
    await query.message.reply_photo(photo=buf)
    await query.edit_message_text("İşte grafiğin 👇")


# ---------------------------------------------------------------- photos & settings


PHOTO_SAVE_SENTINEL = "[FOTO_KAYDET]"


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A photo is either a plate of food (estimate calories + log the meal) or a
    progress photo (store it). The vision model looks and decides."""
    import base64

    user = await get_user(update.effective_user.id)
    if msg := _require_active(user):
        return await update.message.reply_text(msg)
    photo = update.message.photo[-1]
    caption = (update.message.caption or "").strip()
    group_mode = _is_group(update)

    await update.effective_chat.send_action(ChatAction.TYPING)
    try:
        tg_file = await photo.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        image_b64 = base64.b64encode(data).decode()
    except Exception:
        log.exception("photo download failed")
        return await update.message.reply_text("Fotoğrafı indiremedim, tekrar gönderir misin? 🙏")

    directive = (
        "[Kullanıcı bir fotoğraf gönderdi]"
        + (f" Fotoğraf notu: {caption}" if caption else "")
        + "\nFotoğrafı dikkatle incele ve şuna göre davran:\n"
        "- YEMEK/İÇECEK fotoğrafıysa: porsiyonu gözünle tahmin et, log_meal ile kaydet ve "
        "kalori + protein tahminini samimi tek mesajla söyle (tahmin olduğunu belli et, gerekirse "
        "tek kısa netleştirme sorusu sor).\n"
        "- VÜCUT/İLERLEME fotoğrafıysa: kısa, motive edici bir şey yaz ve mesajının sonuna "
        "aynen şunu ekle: [FOTO_KAYDET]\n"
        "- Başka bir şeyse kısaca doğal biçimde yorumla."
    )

    from app.ai.dietitian import chat

    try:
        async with session_scope() as session:
            res = await session.execute(select(User).where(User.telegram_id == update.effective_user.id))
            db_user = res.scalar_one()
            reply = await chat(
                session, db_user, directive, group_mode=group_mode, image=(image_b64, "image/jpeg")
            )
            if PHOTO_SAVE_SENTINEL in reply:
                session.add(
                    ProgressPhoto(
                        user_id=db_user.id,
                        telegram_file_id=photo.file_id,
                        note=caption[:250],
                    )
                )
                reply = reply.replace(PHOTO_SAVE_SENTINEL, "").strip()
    except Exception:
        log.exception("photo analysis failed")
        reply = "Fotoğrafa şu an bakamadım, birazdan tekrar gönderir misin? 🙏"
    if reply:
        for i in range(0, len(reply), 4000):
            await update.message.reply_text(reply[i : i + 4000], do_quote=group_mode)


async def cmd_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Fotoğrafı doğrudan gönder: tabağınsa kalorisini tahmin edip kaydederim, "
        "vücut fotoğrafınsa ilerleme albümüne eklerim. 📸"
    )


REMINDER_LABELS = {
    "gunaydin": "🌅 Günaydın mesajı",
    "tarti": "⚖️ Tartı hatırlatması",
    "su_1": "💧 Su (öğleden önce)",
    "su_2": "💧 Su (öğleden sonra)",
    "su_3": "💧 Su (akşam)",
    "ogun_ogle": "🍲 Öğle yemeği",
    "ogun_aksam": "🍽 Akşam yemeği",
    "aksam_kontrol": "🌙 Akşam değerlendirmesi",
}


async def _render_settings(session, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from app.models import ReminderSetting

    res = await session.execute(select(ReminderSetting).where(ReminderSetting.user_id == user_id))
    settings = list(res.scalars())
    lines = ["⚙️ *Hatırlatma Ayarların*\nKapatmak/açmak için dokun:"]
    buttons = []
    for s in sorted(settings, key=lambda x: x.time_of_day):
        label = REMINDER_LABELS.get(s.kind, s.kind)
        state = "🟢" if s.enabled else "🔴"
        buttons.append(
            [InlineKeyboardButton(f"{state} {label} — {s.time_of_day.strftime('%H:%M')}", callback_data=f"ayar:{s.id}")]
        )
    lines.append("\n_Saatleri değiştirmek için bana yazman yeterli: örn. “tartı hatırlatmasını 7:30 yap”._")
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def cmd_ayarlar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id)
    if msg := _require_active(user):
        return await update.message.reply_text(msg)
    async with session_scope() as session:
        text, markup = await _render_settings(session, user.id)
    await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")


async def cb_ayar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from app.models import ReminderSetting

    query = update.callback_query
    setting_id = int(query.data.split(":", 1)[1])
    async with session_scope() as session:
        setting = await session.get(ReminderSetting, setting_id)
        if setting:
            setting.enabled = not setting.enabled
        text, markup = await _render_settings(session, setting.user_id)
    await query.answer("Güncellendi")
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    except Exception:
        pass


# ---------------------------------------------------------------- free chat -> AI


async def chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id)
    if msg := _require_active(user):
        return await update.message.reply_text(msg)
    text = update.message.text or ""
    if not text.strip():
        return
    group_mode = _is_group(update)

    # No typing indicator in the group: the dietitian may choose to stay silent
    # there, and "typing... then nothing" on every human-to-human message is creepy.
    if not group_mode:
        await update.effective_chat.send_action(ChatAction.TYPING)

    from app.ai.dietitian import SILENT_SENTINEL, chat

    try:
        async with session_scope() as session:
            res = await session.execute(select(User).where(User.telegram_id == update.effective_user.id))
            user = res.scalar_one()
            reply = await chat(session, user, text, group_mode=group_mode)
    except Exception:
        log.exception("chat failed")
        reply = "Bir aksaklık oldu, mesajını birazdan tekrar yazar mısın? 🙏"
    if SILENT_SENTINEL in reply and len(reply.strip()) <= len(SILENT_SENTINEL) + 4:
        return  # the dietitian chose to stay out of a human-to-human exchange
    for i in range(0, len(reply), 4000):
        # In the group, quote the message being answered so it's clear who it's for.
        await update.message.reply_text(reply[i : i + 4000], do_quote=group_mode)
