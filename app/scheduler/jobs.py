"""APScheduler jobs: reminders, weekly adaptive review + planning, reports, backups.

Reminders are driven by a per-minute tick that reads ReminderSetting rows, so
time changes made via the AI tool or /ayarlar take effect immediately without
re-registering jobs.
"""
import asyncio
import logging
import subprocess
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from telegram.ext import Application

from app.config import get_settings
from app.db import session_scope
from app.models import ReminderSetting, User
from app.services.adaptive import apply_adjustments, decide_adjustments
from app.services.mealplan import extract_dinners, generate_weekly_plan
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


async def _send(application: Application, user: User, text: str) -> None:
    if not text:
        return
    try:
        for i in range(0, len(text), 4000):
            await application.bot.send_message(chat_id=user.telegram_id, text=text[i : i + 4000])
    except Exception:
        log.exception("failed to send message to %s", user.telegram_id)


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
    from app.ai.dietitian import generate_message

    if kind == "gunaydin":
        text = await generate_message(
            session,
            user,
            "Kısa, samimi bir günaydın mesajı yaz: bugünün plandaki öğünlerini 1'er satırla hatırlat, "
            "günün su ve protein hedefini söyle, motive edici tek cümleyle bitir. Emoji kullan ama abartma.",
        )
        await _send(application, user, text or f"Günaydın {user.name}! ☀️ Bugün de birlikteyiz.")
        return

    if kind == "tarti":
        await _send(
            application,
            user,
            "⚖️ Tartı zamanı! Aç karnına, tuvaletten sonra tartılıp kilonu bana yazar mısın?",
        )
        return

    if kind.startswith("su_"):
        facts = await daily_facts(session, user)
        target = facts["targets"]["water_ml"] or 2000
        drunk = facts["water_ml"]
        await _send(
            application,
            user,
            f"💧 Su molası! Bugün {drunk}/{target} ml içtin. Bir bardak daha? (kaydetmek için /su)",
        )
        return

    if kind.startswith("ogun_"):
        slot = "ogle" if kind == "ogun_ogle" else "aksam"
        slot_name = "öğle" if slot == "ogle" else "akşam"
        from app.models import MealPlan, PlannedMeal

        today = date.today()
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
        await _send(
            application,
            user,
            f"🍽 {slot_name.capitalize()} yemeği zamanı yaklaşıyor!{meal_line}\nNe yediğini bana yazmayı unutma.",
        )
        return

    if kind == "aksam_kontrol":
        text = await generate_message(
            session,
            user,
            "Kısa bir akşam check-in mesajı yaz: bugünkü kalori/protein/su durumunu 1-2 satırda özetle, "
            "sonra nazikçe sor: bugün ruh halin/enerjin nasıldı (1-5), kaç saat uyudun, açlık çektin mi? "
            "Tek mesaj, samimi ton.",
        )
        await _send(
            application,
            user,
            text or "🌙 Günün nasıl geçti? Ruh halin, uykun ve açlığın nasıldı? Bana yazarsan kaydedeyim.",
        )
        return


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
                await _send(application, user, "📋 Haftalık Değerlendirme\n\n" + message)
            except Exception:
                log.exception("weekly review failed for user %s", user.id)

        # Generate next week's plans (second user's dinners aligned with the first's)
        partner_dinners = None
        for user in users:
            try:
                plan = await generate_weekly_plan(session, user, next_monday, partner_dinners)
                if plan:
                    await session.refresh(plan, ["meals"])
                    partner_dinners = extract_dinners(plan.meals)
                    await _send(
                        application,
                        user,
                        "🍽 Yeni haftalık planın hazır! /plan hafta yazarak inceleyebilirsin.",
                    )
            except Exception:
                log.exception("plan generation failed for user %s", user.id)

        try:
            slist = await build_weekly_shopping_list(session, next_monday)
            if slist:
                for user in users:
                    await _send(
                        application,
                        user,
                        "🛒 Haftalık ortak alışveriş listeniz hazır! /alisveris ile görebilirsiniz.",
                    )
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
                text = await generate_message(
                    session,
                    user,
                    "AYLIK RAPOR yaz. 30 günlük kilo değişimi: "
                    + (f"{change:+} kg. " if change is not None else "(yeterli veri yok). ")
                    + "Son hafta verileri:\n"
                    + format_weekly_stats_tr(stats)
                    + "\nAyı değerlendir, başarıları kutla, gelecek ay için 2-3 odak öner.",
                )
                await _send(application, user, "🗓 Aylık Rapor\n\n" + text if text else "")
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


# ------------------------------------------------------------------ post-onboarding plan


async def generate_plan_for_user_bg(telegram_id: int, application: Application) -> None:
    """Generate the first weekly plan right after onboarding (current week),
    aligned with the partner's plan when one exists."""
    from app.models import MealPlan

    try:
        async with session_scope() as session:
            res = await session.execute(select(User).where(User.telegram_id == telegram_id))
            user = res.scalar_one_or_none()
            if not user:
                return
            today = date.today()
            week_start = today - timedelta(days=today.weekday())

            partner_dinners = None
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
                partner_dinners = extract_dinners(partner_plan.meals)

            plan = await generate_weekly_plan(session, user, week_start, partner_dinners)
            if plan:
                await build_weekly_shopping_list(session, week_start)
        if plan:
            await _send_by_tg(application, telegram_id,
                "✅ İlk haftalık planın hazır! /plan yazarak bugünü, /plan hafta yazarak tüm haftayı görebilirsin. "
                "Alışveriş listen de hazır: /alisveris 🛒")
        else:
            await _send_by_tg(application, telegram_id,
                "Planını şu an oluşturamadım 😕 Birazdan \"bana plan hazırla\" yazarsan tekrar denerim.")
    except Exception:
        log.exception("post-onboarding plan generation failed for %s", telegram_id)
        await _send_by_tg(application, telegram_id,
            "Planını şu an oluşturamadım 😕 Birazdan \"bana plan hazırla\" yazarsan tekrar denerim.")


async def _send_by_tg(application: Application, telegram_id: int, text: str) -> None:
    try:
        await application.bot.send_message(chat_id=telegram_id, text=text)
    except Exception:
        log.exception("failed to send message to %s", telegram_id)
