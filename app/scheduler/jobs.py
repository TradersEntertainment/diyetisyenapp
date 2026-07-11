"""APScheduler jobs: reminders, weekly adaptive review + planning, reports, backups.

Reminders are driven by a per-minute tick that reads ReminderSetting rows, so
time changes made via the AI tool or /ayarlar take effect immediately without
re-registering jobs.
"""
import asyncio
import logging
import subprocess
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from telegram.ext import Application

from app.config import get_settings
from app.db import session_scope
from app.models import ReminderSetting, User
from app.services.adaptive import apply_adjustments, decide_adjustments
from app.services.mealplan import extract_menu, generate_weekly_plan, render_week_plan_png
from app.services.reports import (
    daily_facts,
    format_weekly_stats_tr,
    gather_weekly_stats,
    get_current_targets,
)
from app.services.shopping import build_weekly_shopping_list
from app.services.targets import (
    get_profile,
    latest_body_fat,
    latest_weight,
    primary_goal_of,
    save_targets,
)

log = logging.getLogger(__name__)


def create_scheduler(application: Application) -> AsyncIOScheduler:
    tz = ZoneInfo(get_settings().tz)
    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(reminder_tick, CronTrigger(minute="*", timezone=tz), args=[application], max_instances=1)
    scheduler.add_job(
        daily_care, CronTrigger(hour=16, minute=30, timezone=tz), args=[application]
    )
    scheduler.add_job(
        weekly_review, CronTrigger(day_of_week="sun", hour=17, minute=0, timezone=tz), args=[application]
    )
    scheduler.add_job(
        monthly_report, CronTrigger(day=1, hour=9, minute=30, timezone=tz), args=[application]
    )
    scheduler.add_job(nightly_backup, CronTrigger(hour=3, minute=30, timezone=tz))
    return scheduler


async def _active_users(session) -> list[User]:
    res = await session.execute(select(User).where(User.onboarding_state == "active"))
    return list(res.scalars())


async def _resolve_chat(session, user: User) -> tuple[int, bool]:
    """Where to talk to this user: the shared group when known, else their DM.

    Returns (chat_id, is_group).
    """
    from app.services.group import get_group_chat_id

    group_id = await get_group_chat_id(session)
    if group_id:
        return group_id, True
    return user.telegram_id, False


async def _send_to(application: Application, chat_id: int, text: str, reply_markup=None) -> None:
    if not text:
        return
    try:
        chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]
        for i, chunk in enumerate(chunks):
            # Attach the keyboard to the last chunk only.
            markup = reply_markup if i == len(chunks) - 1 else None
            await application.bot.send_message(chat_id=chat_id, text=chunk, reply_markup=markup)
    except Exception:
        log.exception("failed to send message to chat %s", chat_id)


async def _send(
    application: Application,
    session,
    user: User,
    text: str,
    *,
    mention: bool = False,
    reply_markup=None,
) -> None:
    """Send a user-directed message to the group (name-prefixed when static) or DM."""
    if not text:
        return
    chat_id, is_group = await _resolve_chat(session, user)
    if is_group and mention and user.name:
        text = f"{user.name}, {text}"
    await _send_to(application, chat_id, text, reply_markup)


async def _send_plan_image(application: Application, session, user: User, week_start=None) -> None:
    """Post the user's weekly plan grid as a PNG."""
    from app.services.mealplan import render_week_plan_png

    try:
        buf = await render_week_plan_png(session, user, week_start)
        if not buf:
            return
        chat_id, _ = await _resolve_chat(session, user)
        # Document, not photo: Telegram recompresses photos and the table
        # becomes unreadable.
        await application.bot.send_document(
            chat_id=chat_id, document=buf, filename=f"plan_{user.name or user.id}.png"
        )
    except Exception:
        log.exception("plan image send failed for user %s", user.id)


async def _send_challenge_note(application: Application, session, user: User, is_group: bool) -> None:
    """A short 'what challenged you this week' note from the adherence breakdown."""
    import json

    from app.ai.dietitian import SILENT_SENTINEL, generate_message
    from app.models import HungerLog, MealLog
    from app.services.analysis import adherence_breakdown

    try:
        since = datetime.now(timezone.utc) - timedelta(days=7)
        meals = list((await session.execute(
            select(MealLog).where(MealLog.user_id == user.id, MealLog.ts >= since)
        )).scalars())
        if len(meals) < 3:
            return  # not enough data to say anything useful
        cheats = [m for m in meals if m.is_cheat]
        hungers = list((await session.execute(
            select(HungerLog).where(
                HungerLog.user_id == user.id, HungerLog.ts >= since, HungerLog.hunger >= 4
            )
        )).scalars())
        breakdown = adherence_breakdown(
            meals, cheat_count=len(cheats),
            cheat_hours=[m.ts.hour for m in cheats],
            high_hunger_hours=[h.ts.hour for h in hungers],
        )
        text = await generate_message(
            session, user,
            "Aşağıda bu haftanın plana uyum kırılımı var. Bir cümleyle 'bu hafta seni en çok "
            "zorlayan' şeyi söyle ve 1-2 uygulanabilir, nazik öneri ver. Suçlama yok. Kısa tut.\n\n"
            + json.dumps(breakdown, ensure_ascii=False),
            group_mode=is_group,
        )
        if text and SILENT_SENTINEL not in text:
            await _send(application, session, user, "💡 " + text)
    except Exception:
        log.exception("challenge note failed for user %s", user.id)


async def _send_weight_chart(application: Application, session, user: User, days: int) -> None:
    """Post the user's weight trend as a PNG below a report message."""
    from app.bot.charts import line_chart
    from app.services.reports import weight_series

    try:
        series = await weight_series(session, user.id, days=days)
        buf = line_chart(series, f"{user.name} — kilo ({days} gün)", "kg")
        if not buf:
            return
        chat_id, _ = await _resolve_chat(session, user)
        await application.bot.send_photo(chat_id=chat_id, photo=buf)
    except Exception:
        log.exception("weight chart send failed for user %s", user.id)


# ------------------------------------------------------------------ reminders


async def reminder_tick(application: Application) -> None:
    """Runs every minute; fires reminders whose HH:MM matches the user's local time."""
    async with session_scope() as session:
        users = await _active_users(session)
        for user in users:
            tz = ZoneInfo(user.timezone or get_settings().tz)
            now = datetime.now(tz)
            hhmm = (now.hour, now.minute)
            res = await session.execute(
                select(ReminderSetting).where(
                    ReminderSetting.user_id == user.id, ReminderSetting.enabled.is_(True)
                )
            )
            for setting in res.scalars():
                if (setting.time_of_day.hour, setting.time_of_day.minute) != hhmm:
                    continue
                try:
                    await _fire_reminder(application, session, user, setting.kind)
                except Exception:
                    log.exception("reminder %s failed for user %s", setting.kind, user.id)


async def _fire_reminder(application: Application, session, user: User, kind: str) -> None:
    """Reminders are need-based: when the data is already in, we stay silent
    instead of nagging (the household told us their habits once already)."""
    from app.ai.dietitian import SILENT_SENTINEL, generate_message

    _, is_group = await _resolve_chat(session, user)

    if kind == "gunaydin":
        from app.services.reports import logging_streak_days

        streak = await logging_streak_days(session, user.id)
        streak_note = (
            f" Bugün kayıt serisi {streak}. güne ulaştı — bunu coşkuyla kutla!"
            if streak in (7, 14, 21) or (streak and streak % 30 == 0)
            else ""
        )
        text = await generate_message(
            session,
            user,
            "Kısa, samimi bir günaydın mesajı yaz: bugünün plandaki öğünlerini 1'er satırla hatırlat "
            "(menü evde ortak — 'bugün akşama X yapıyoruz, beraber' tonunda yaz), günün su ve protein "
            "hedefini söyle, motive edici tek cümleyle bitir. Emoji kullan ama abartma." + streak_note,
            group_mode=is_group,
            include_recent_dialogue=True,
            store_in_history=True,
        )
        await _send(application, session, user, text or f"Günaydın {user.name}! ☀️ Bugün de birlikteyiz.")
        return

    if kind == "tarti":
        facts = await daily_facts(session, user)
        if facts["weight_kg"]:
            return  # already weighed in today — no nagging
        await _send(
            application,
            session,
            user,
            "⚖️ tartı zamanı! Aç karnına, tuvaletten sonra tartılıp kilonu bana yazar mısın?",
            mention=True,
        )
        return

    if kind.startswith("su_"):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        from app.models import WaterLog
        from app.services.targets import get_profile

        facts = await daily_facts(session, user)
        target = facts["targets"]["water_ml"] or 2000
        drunk = facts["water_ml"]
        if drunk >= target * 0.9:
            return  # on track — silence is golden

        profile = await get_profile(session, user.id)
        if profile and profile.auto_water:
            # Hands-off mode: assume they drank a glass, log it, just notify.
            glass = 250
            session.add(WaterLog(user_id=user.id, amount_ml=glass))
            await _send(
                application,
                session,
                user,
                f"💧 senin adına {glass} ml su ekledim (bugün {drunk + glass}/{target} ml). "
                "İçmediysen 'su içmedim' yaz, düzeltirim.",
                mention=True,
            )
            return

        res = await session.execute(
            select(WaterLog)
            .where(WaterLog.user_id == user.id)
            .order_by(WaterLog.ts.desc())
            .limit(1)
        )
        last = res.scalars().first()
        if last and (datetime.now(last.ts.tzinfo) - last.ts) < timedelta(minutes=90):
            return  # they just logged water; don't pester
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("💧 +1 bardak (200 ml)", callback_data="su:200"),
                    InlineKeyboardButton("💧 +500 ml", callback_data="su:500"),
                ]
            ]
        )
        await _send(
            application,
            session,
            user,
            f"💧 su molası! Bugün {drunk}/{target} ml içtin. Bir bardak daha?",
            mention=True,
            reply_markup=markup,
        )
        return

    if kind.startswith("ogun_"):
        _slot_map = {"ogun_kahvalti": ("kahvalti", "kahvaltı"), "ogun_ogle": ("ogle", "öğle"), "ogun_aksam": ("aksam", "akşam")}
        slot, slot_name = _slot_map.get(kind, ("aksam", "akşam"))
        from app.models import MealLog, MealPlan, PlannedMeal

        today = date.today()
        day_start = datetime.combine(today, dtime.min, tzinfo=timezone.utc)
        res = await session.execute(
            select(MealLog).where(
                MealLog.user_id == user.id,
                MealLog.ts >= day_start,
                MealLog.slot == slot,
            )
        )
        if res.scalars().first():
            return  # that meal is already logged — no reminder needed
        week_start = today - timedelta(days=today.weekday())
        res = await session.execute(
            select(MealPlan)
            .where(MealPlan.user_id == user.id, MealPlan.week_start == week_start, MealPlan.status == "active")
            .order_by(MealPlan.id.desc())
            .limit(1)
        )
        plan = res.scalar_one_or_none()
        meal_line = ""
        if plan:
            res = await session.execute(
                select(PlannedMeal).where(
                    PlannedMeal.plan_id == plan.id,
                    PlannedMeal.day_index == today.weekday(),
                    PlannedMeal.slot == slot,
                )
            )
            meal = res.scalars().first()
            if meal:
                meal_line = f"\nPlanda: {meal.name} ({meal.kcal} kcal, P{meal.protein_g:g} g) — hazırlık ~{meal.prep_minutes} dk"
        emoji = {"kahvalti": "🍳", "ogle": "🍲", "aksam": "🍽"}.get(slot, "🍽")
        await _send(
            application,
            session,
            user,
            f"{emoji} {slot_name} vakti yaklaşıyor!{meal_line}\nNe yediğini bana yazmayı unutma.",
            mention=True,
        )
        return

    if kind == "aksam_kontrol":
        from app.models import HungerLog, MoodLog, SleepLog

        day_start = datetime.combine(date.today(), dtime.min, tzinfo=timezone.utc)
        missing = []
        for model, label in ((MoodLog, "ruh hali/enerji (1-5)"), (SleepLog, "uyku süresi"), (HungerLog, "açlık durumu")):
            res = await session.execute(
                select(model).where(model.user_id == user.id, model.ts >= day_start).limit(1)
            )
            if not res.scalars().first():
                missing.append(label)
        if missing:
            ask = "Nazikçe SADECE şunları sor (gerisini sorma): " + ", ".join(missing) + "."
        else:
            ask = (
                "Bugün her veri girilmiş; hiçbir şey sorma. Ya günü kapatan 1-2 cümlelik sıcak bir "
                "mesaj yaz ya da söylenecek değerli bir şey yoksa sadece [SESSIZ] yaz."
            )
        text = await generate_message(
            session,
            user,
            "Kısa bir akşam check-in mesajı yaz: bugünkü kalori/protein/su durumunu 1-2 satırda özetle. "
            + ask
            + " Tek mesaj, samimi ton.",
            group_mode=is_group,
            include_recent_dialogue=True,
            store_in_history=True,
        )
        if text and SILENT_SENTINEL in text:
            return
        await _send(
            application,
            session,
            user,
            text or "🌙 Günün nasıl geçti? Ruh halin, uykun ve açlığın nasıldı? Bana yazarsan kaydedeyim.",
        )
        return


# ------------------------------------------------------------------ daily care

async def daily_care(application: Application) -> None:
    """Afternoon proactive check: a real dietitian notices missing data and asks.

    Looks at each user's day so far (weigh-in age, meals, water) and lets the AI
    decide whether a short caring nudge is worth sending; it stays silent otherwise.
    """
    from app.ai.dietitian import SILENT_SENTINEL, generate_message
    from app.models import WeightLog

    async with session_scope() as session:
        users = await _active_users(session)

    for user in users:
        # One session per user: a failure for one must not poison the other's check.
        try:
            async with session_scope() as session:
                _, is_group = await _resolve_chat(session, user)
                facts = await daily_facts(session, user)
                res = await session.execute(
                    select(WeightLog)
                    .where(WeightLog.user_id == user.id)
                    .order_by(WeightLog.ts.desc())
                    .limit(1)
                )
                last_w = res.scalars().first()
                days_since_weigh = (
                    (datetime.now(last_w.ts.tzinfo) - last_w.ts).days if last_w else 99
                )
                from app.services.reports import logging_streak_days

                streak = await logging_streak_days(session, user.id)
                signals = (
                    f"Saat 16:30 civarı. Bugüne kadar kayıtlar: {len(facts['meals'])} öğün, "
                    f"{facts['kcal_total']} kcal, {facts['water_ml']} ml su. "
                    f"Son tartıdan bu yana geçen gün: {days_since_weigh}. "
                    f"Kayıt serisi: {streak} gün (bugün hiç kayıt yoksa seri kırılmak üzere demektir)."
                )
                text = await generate_message(
                    session,
                    user,
                    signals
                    + " Gerçek bir diyetisyen gibi davran: bu tabloda seni harekete geçirecek bir şey "
                    "varsa (öğün yazılmamış, su çok az, uzun süredir tartı yok, dün plan sapmış...) "
                    "KISA ve sıcak tek bir mesaj yaz — soru sor, veri iste ya da hatırlat. "
                    "Her şey yolundaysa ve söylenecek değerli bir şey yoksa sadece [SESSIZ] yaz.",
                    group_mode=is_group,
                    include_recent_dialogue=True,
                    store_in_history=True,
                )
                if not text or SILENT_SENTINEL in text:
                    continue
                await _send(application, session, user, text)
        except Exception:
            log.exception("daily care failed for user %s", user.id)


# ------------------------------------------------------------------ weekly review


async def weekly_review(application: Application) -> None:
    """Sunday: analyze week -> adjust targets (protein floor invariant) -> AI picks
    strategy -> generate next week's plans for both users -> shared shopping list."""
    from app.ai.dietitian import decide_strategy

    today = date.today()
    next_monday = today + timedelta(days=((7 - today.weekday()) % 7) or 7)

    async with session_scope() as session:
        users = await _active_users(session)
        for user in users:
            try:
                stats = await gather_weekly_stats(session, user)
                current = await get_current_targets(session, user.id)
                profile = await get_profile(session, user.id)
                if not current or not profile:
                    continue
                goal = primary_goal_of(profile)
                decision = decide_adjustments(stats, current.kcal, goal)
                weight = await latest_weight(session, user.id) or profile.start_weight_kg
                body_fat = await latest_body_fat(session, user.id) or profile.body_fat_pct
                if not weight:
                    continue
                new_targets = apply_adjustments(
                    decision,
                    current_kcal=current.kcal,
                    weight_kg=weight,
                    height_cm=profile.height_cm,
                    age=profile.age,
                    gender=profile.gender or "kadin",
                    activity_level=profile.activity_level or "hafif_aktif",
                    primary_goal=goal,
                    body_fat_pct=body_fat,
                    exercise_days_per_week=profile.exercise_frequency_per_week,
                )
                strategy, message = await decide_strategy(
                    session, user, stats, decision, new_targets, current.diet_strategy
                )
                reason = "; ".join(decision.reasons) or "Haftalık rutin değerlendirme."
                await save_targets(
                    session, user.id, new_targets, diet_strategy=strategy, reason=reason,
                    effective=next_monday,
                )
                if not message:
                    message = (
                        format_weekly_stats_tr(stats)
                        + f"\n\nYeni hafta hedeflerin: {new_targets.kcal} kcal, protein {new_targets.protein_g} g."
                    )
                header = f"📋 Haftalık Değerlendirme — {user.name}\n\n" if user.name else "📋 Haftalık Değerlendirme\n\n"
                await _send(application, session, user, header + message)
                await _send_weight_chart(application, session, user, days=30)
                await _send_challenge_note(application, session, user, is_group)
            except Exception:
                log.exception("weekly review failed for user %s", user.id)

        # Generate next week's plans (both users share one menu, portioned per person)
        partner_menu = None
        for user in users:
            try:
                plan = await generate_weekly_plan(session, user, next_monday, partner_menu)
                if plan:
                    await session.refresh(plan, ["meals"])
                    partner_menu = extract_menu(plan.meals)
                    await _send(
                        application,
                        session,
                        user,
                        "🍽 yeni haftalık planın hazır! /plan hafta yazarak inceleyebilirsin.",
                        mention=True,
                    )
                    await _send_plan_image(application, session, user, next_monday)
            except Exception:
                log.exception("plan generation failed for user %s", user.id)

        try:
            slist = await build_weekly_shopping_list(session, next_monday)
            if slist and users:
                from app.services.group import get_group_chat_id

                group_id = await get_group_chat_id(session)
                text = "🛒 Haftalık ortak alışveriş listeniz hazır! /alisveris ile görebilirsiniz."
                if group_id:
                    await _send_to(application, group_id, text)  # one shared announcement
                else:
                    for user in users:
                        await _send_to(application, user.telegram_id, text)
        except Exception:
            log.exception("shopping list build failed")


# ------------------------------------------------------------------ monthly report


async def monthly_report(application: Application) -> None:
    from app.ai.dietitian import generate_message
    from app.services.reports import weight_series

    async with session_scope() as session:
        for user in await _active_users(session):
            try:
                weights = await weight_series(session, user.id, days=31)
                change = round(weights[-1][1] - weights[0][1], 1) if len(weights) >= 2 else None
                stats = await gather_weekly_stats(session, user)
                _, is_group = await _resolve_chat(session, user)
                text = await generate_message(
                    session,
                    user,
                    "AYLIK RAPOR yaz. 30 günlük kilo değişimi: "
                    + (f"{change:+} kg. " if change is not None else "(yeterli veri yok). ")
                    + "Son hafta verileri:\n"
                    + format_weekly_stats_tr(stats)
                    + "\nAyı değerlendir, başarıları kutla, gelecek ay için 2-3 odak öner.",
                    group_mode=is_group,
                )
                if text:
                    header = f"🗓 Aylık Rapor — {user.name}\n\n" if user.name else "🗓 Aylık Rapor\n\n"
                    await _send(application, session, user, header + text)
                    await _send_weight_chart(application, session, user, days=90)
            except Exception:
                log.exception("monthly report failed for user %s", user.id)


# ------------------------------------------------------------------ backup


async def nightly_backup() -> None:
    settings = get_settings()
    try:
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "scripts/backup.sh",
            settings.backup_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode == 0:
            log.info("backup ok: %s", out.decode().strip())
        else:
            log.error("backup failed: %s", err.decode().strip())
    except Exception:
        log.exception("backup job crashed")


# ------------------------------------------------------------------ plan rules version

async def ensure_plans_current(application: Application) -> None:
    """One-shot after deploy: when plan generation rules changed (PLAN_RULES_VERSION
    bumped), archive & regenerate the household's active plans and post the new
    tables — no user action needed. No-op on every later restart."""
    from app.models import AppSetting, MealPlan
    from app.services.mealplan import PLAN_RULES_VERSION

    try:
        today = date.today()
        week_start = today - timedelta(days=today.weekday())
        async with session_scope() as session:
            row = await session.get(AppSetting, "plan_rules_version")
            if row and row.value == PLAN_RULES_VERSION:
                return
            users = await _active_users(session)
            res = await session.execute(
                select(MealPlan).where(
                    MealPlan.week_start == week_start, MealPlan.status == "active"
                ).limit(1)
            )
            has_plans = res.scalars().first() is not None
            primary_tg = users[0].telegram_id if users else None
            # Record the version first so a crash mid-generation can't loop.
            if row is None:
                session.add(AppSetting(key="plan_rules_version", value=PLAN_RULES_VERSION))
            else:
                row.value = PLAN_RULES_VERSION
        if primary_tg and has_plans:
            log.info("plan rules version changed -> regenerating household plans")
            await household_regenerate(application, primary_tg)
    except Exception:
        log.exception("ensure_plans_current failed")


# ------------------------------------------------------------------ household plan regen


async def household_regenerate(application: Application, primary_telegram_id: int) -> None:
    """Regenerate this week's plans for the WHOLE household (same menu, per-person
    portions), rebuild the shopping list, and post announcements + plan images.

    Triggered from chat via the regenerate_meal_plan tool; runs as a background
    task so the conversation isn't blocked for minutes.
    """
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    try:
        async with session_scope() as session:
            users = await _active_users(session)
            # The requester's plan drives the shared menu.
            users.sort(key=lambda u: u.telegram_id != primary_telegram_id)
            partner_menu = None
            generated: list[User] = []
            for user in users:
                plan = await generate_weekly_plan(session, user, week_start, partner_menu)
                if plan:
                    await session.refresh(plan, ["meals"])
                    partner_menu = extract_menu(plan.meals)
                    generated.append(user)
            if generated:
                await build_weekly_shopping_list(session, week_start)

        if not generated:
            async with session_scope() as session:
                res = await session.execute(select(User).where(User.telegram_id == primary_telegram_id))
                user = res.scalar_one_or_none()
                if user:
                    await _send(application, session, user,
                                "planı şu an oluşturamadım 😕 Birazdan tekrar dener misin?", mention=True)
            return

        async with session_scope() as session:
            names = " ve ".join(u.name for u in generated if u.name)
            chat_id, _ = await _resolve_chat(session, generated[0])
            await _send_to(
                application, chat_id,
                f"📋 Yeni haftalık planlarınız hazır ({names})! Menü ortak, porsiyonlar kişiye özel. "
                "🛒 Alışveriş listesi de güncellendi (/alisveris).",
            )
            for user in generated:
                await _send_plan_image(application, session, user, week_start)
    except Exception:
        log.exception("household plan regeneration failed")


# ------------------------------------------------------------------ post-onboarding plan


async def generate_plan_for_user_bg(telegram_id: int, application: Application) -> None:
    """Generate the first weekly plan right after onboarding (current week),
    aligned with the partner's plan when one exists."""
    from app.models import MealPlan

    chat_id = telegram_id
    name = ""
    try:
        async with session_scope() as session:
            res = await session.execute(select(User).where(User.telegram_id == telegram_id))
            user = res.scalar_one_or_none()
            if not user:
                return
            chat_id, is_group = await _resolve_chat(session, user)
            name = user.name if is_group and user.name else ""
            today = date.today()
            week_start = today - timedelta(days=today.weekday())

            partner_menu = None
            res = await session.execute(
                select(MealPlan).where(
                    MealPlan.week_start == week_start,
                    MealPlan.status == "active",
                    MealPlan.user_id != user.id,
                )
            )
            partner_plan = res.scalars().first()
            if partner_plan:
                await session.refresh(partner_plan, ["meals"])
                partner_menu = extract_menu(partner_plan.meals)

            plan = await generate_weekly_plan(session, user, week_start, partner_menu)
            if plan:
                await build_weekly_shopping_list(session, week_start)
        prefix = f"{name}, " if name else ""
        if plan:
            await _send_to(application, chat_id,
                f"✅ {prefix}ilk haftalık planın hazır! /plan yazarak bugünü, /plan hafta yazarak tüm haftayı "
                "görebilirsin. Alışveriş listen de hazır: /alisveris 🛒")
            try:
                async with session_scope() as session:
                    res = await session.execute(select(User).where(User.telegram_id == telegram_id))
                    u = res.scalar_one_or_none()
                    buf = await render_week_plan_png(session, u, week_start) if u else None
                if buf:
                    await application.bot.send_document(chat_id=chat_id, document=buf, filename="plan.png")
            except Exception:
                log.exception("post-onboarding plan image failed for %s", telegram_id)
        else:
            await _send_to(application, chat_id,
                f"{prefix}planını şu an oluşturamadım 😕 Birazdan \"bana plan hazırla\" yazarsan tekrar denerim.")
    except Exception:
        log.exception("post-onboarding plan generation failed for %s", telegram_id)
        await _send_to(application, chat_id,
            "Planını şu an oluşturamadım 😕 Birazdan \"bana plan hazırla\" yazarsan tekrar denerim.")
