"""Token-protected JSON API feeding the dashboard / management panel."""
from datetime import date, datetime, time as dtime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select

from app.api.log_registry import LOG_REGISTRY, get_kind
from app.api.schemas import (
    LogCreate,
    LogUpdate,
    PreferenceUpsert,
    ProfileUpdate,
    ReminderCreate,
    ReminderUpdate,
    ShoppingItemUpdate,
    TargetOverride,
    UserCreate,
    UserUpdate,
)
from app.config import get_settings
from app.db import session_scope
from app.models import (
    ExerciseLog,
    FoodPreference,
    MealLog,
    MealPlan,
    PlannedMeal,
    Profile,
    ReminderSetting,
    ShoppingItem,
    ShoppingList,
    SleepLog,
    TargetHistory,
    User,
    WaterLog,
)
from app.services.calculations import protein_floor_g
from app.services.reports import (
    body_comp_series,
    gather_weekly_stats,
    get_current_targets,
    weight_series,
)
from app.services.targets import (
    get_profile,
    latest_body_fat,
    latest_weight,
    primary_goal_of,
    save_targets,
)

router = APIRouter(prefix="/api")


def require_token(request: Request, token: str | None = Query(default=None)):
    """Auth disabled by request; dashboard is open to anyone with the URL."""
    return


@router.get("/health")
async def health():
    return {"status": "ok"}


# ------------------------------------------------------------------ users


@router.get("/users", dependencies=[Depends(require_token)])
async def list_users(include_inactive: bool = Query(default=False)):
    async with session_scope() as session:
        stmt = select(User)
        if not include_inactive:
            stmt = stmt.where(User.onboarding_state == "active")
        res = await session.execute(stmt)
        return [
            {"id": u.id, "name": u.name, "telegram_id": u.telegram_id, "onboarding_state": u.onboarding_state}
            for u in res.scalars()
        ]


@router.post("/users", dependencies=[Depends(require_token)])
async def create_user(body: UserCreate):
    async with session_scope() as session:
        res = await session.execute(select(User).where(User.telegram_id == body.telegram_id))
        if res.scalar_one_or_none():
            raise HTTPException(409, "telegram_id already registered")
        user = User(telegram_id=body.telegram_id, name=body.name, onboarding_state="new")
        session.add(user)
        await session.flush()
        return {"id": user.id, "telegram_id": user.telegram_id, "onboarding_state": user.onboarding_state}


@router.patch("/users/{user_id}", dependencies=[Depends(require_token)])
async def update_user(user_id: int, body: UserUpdate):
    async with session_scope() as session:
        user = await session.get(User, user_id)
        if not user:
            raise HTTPException(404)
        if body.name is not None:
            user.name = body.name
        if body.onboarding_state is not None:
            user.onboarding_state = body.onboarding_state
        return {"id": user.id, "name": user.name, "onboarding_state": user.onboarding_state}


@router.delete("/users/{user_id}", dependencies=[Depends(require_token)])
async def delete_user(user_id: int):
    from app.services.user_admin import delete_user_cascade

    async with session_scope() as session:
        user = await session.get(User, user_id)
        if not user:
            raise HTTPException(404)
        await delete_user_cascade(session, user_id)
        return {"deleted": True}


# ------------------------------------------------------------------ profile


@router.get("/users/{user_id}/profile", dependencies=[Depends(require_token)])
async def read_profile(user_id: int):
    async with session_scope() as session:
        if not await session.get(User, user_id):
            raise HTTPException(404)
        profile = await get_profile(session, user_id)
        if not profile:
            return None
        return {c.name: getattr(profile, c.name) for c in Profile.__table__.columns if c.name != "id"}


@router.patch("/users/{user_id}/profile", dependencies=[Depends(require_token)])
async def update_profile(user_id: int, body: ProfileUpdate):
    async with session_scope() as session:
        user = await session.get(User, user_id)
        if not user:
            raise HTTPException(404)
        profile = await get_profile(session, user_id)
        if not profile:
            profile = Profile(user_id=user_id)
            session.add(profile)
            await session.flush()
        updates = body.model_dump(exclude_unset=True)
        for key, value in updates.items():
            setattr(profile, key, value)
        await session.flush()
        # updated_at has server_default/onupdate -> expired post-flush; refresh
        # so the dict comprehension below reads it without a lazy (unawaited) load.
        await session.refresh(profile)
        return {c.name: getattr(profile, c.name) for c in Profile.__table__.columns if c.name != "id"}


# ------------------------------------------------------------------ targets


@router.get("/users/{user_id}/targets", dependencies=[Depends(require_token)])
async def read_targets(user_id: int):
    async with session_scope() as session:
        if not await session.get(User, user_id):
            raise HTTPException(404)
        targets = await get_current_targets(session, user_id)
        profile = await get_profile(session, user_id)
        floor = None
        if profile:
            weight = await latest_weight(session, user_id) or profile.start_weight_kg
            body_fat = await latest_body_fat(session, user_id) or profile.body_fat_pct
            if weight:
                floor = protein_floor_g(weight, body_fat, primary_goal_of(profile))
        if not targets:
            return {"targets": None, "protein_floor_g": floor}
        return {
            "targets": {
                "kcal": targets.kcal,
                "protein_g": targets.protein_g,
                "carb_g": targets.carb_g,
                "fat_g": targets.fat_g,
                "fiber_g": targets.fiber_g,
                "water_ml": targets.water_ml,
                "diet_strategy": targets.diet_strategy,
                "reason": targets.reason,
                "effective_date": targets.effective_date.isoformat(),
            },
            "protein_floor_g": floor,
        }


@router.post("/users/{user_id}/targets/override", dependencies=[Depends(require_token)])
async def override_targets(user_id: int, body: TargetOverride):
    """Manual panel override. The protein floor from body analysis is a hard
    minimum — a lower value is silently raised to the floor and flagged in the
    response, never accepted as-is."""
    async with session_scope() as session:
        user = await session.get(User, user_id)
        profile = await get_profile(session, user_id)
        current = await get_current_targets(session, user_id)
        if not user or not profile:
            raise HTTPException(404, "user or profile not found")

        weight = await latest_weight(session, user_id) or profile.start_weight_kg
        if not weight:
            raise HTTPException(400, "no weight on record — cannot compute protein floor")
        body_fat = await latest_body_fat(session, user_id) or profile.body_fat_pct
        floor = protein_floor_g(weight, body_fat, primary_goal_of(profile))

        base = current
        kcal = body.kcal if body.kcal is not None else (base.kcal if base else 2000)
        protein = body.protein_g if body.protein_g is not None else (base.protein_g if base else floor)
        clamped = False
        if protein < floor:
            protein = floor
            clamped = True
        carb = body.carb_g if body.carb_g is not None else (base.carb_g if base else 0)
        fat = body.fat_g if body.fat_g is not None else (base.fat_g if base else 0)
        fiber = body.fiber_g if body.fiber_g is not None else (base.fiber_g if base else 25)
        water = body.water_ml if body.water_ml is not None else (base.water_ml if base else 2000)
        strategy = body.diet_strategy or (base.diet_strategy if base else "dengeli")
        reason = body.reason or "Panelden manuel düzenleme."

        from app.services.calculations import Targets

        targets = Targets(
            kcal=kcal, protein_g=protein, fat_g=fat, carb_g=carb, fiber_g=fiber,
            water_ml=water, protein_floor_g=floor,
        )
        row = await save_targets(session, user_id, targets, diet_strategy=strategy, reason=reason)
        return {
            "protein_clamped_to_floor": clamped,
            "protein_floor_g": floor,
            "targets": {
                "kcal": row.kcal, "protein_g": row.protein_g, "carb_g": row.carb_g,
                "fat_g": row.fat_g, "fiber_g": row.fiber_g, "water_ml": row.water_ml,
                "diet_strategy": row.diet_strategy,
            },
        }


@router.get("/users/{user_id}/targets/history", dependencies=[Depends(require_token)])
async def targets_history(user_id: int, limit: int = Query(default=20, le=100)):
    async with session_scope() as session:
        res = await session.execute(
            select(TargetHistory)
            .where(TargetHistory.user_id == user_id)
            .order_by(TargetHistory.effective_date.desc(), TargetHistory.id.desc())
            .limit(limit)
        )
        return [
            {
                "id": t.id, "effective_date": t.effective_date.isoformat(), "kcal": t.kcal,
                "protein_g": t.protein_g, "carb_g": t.carb_g, "fat_g": t.fat_g, "fiber_g": t.fiber_g,
                "water_ml": t.water_ml, "diet_strategy": t.diet_strategy, "reason": t.reason,
            }
            for t in res.scalars()
        ]


# ------------------------------------------------------------------ series / summary (existing)


@router.get("/users/{user_id}/summary", dependencies=[Depends(require_token)])
async def user_summary(user_id: int):
    async with session_scope() as session:
        user = await session.get(User, user_id)
        if not user:
            raise HTTPException(404)
        stats = await gather_weekly_stats(session, user)
        targets = await get_current_targets(session, user_id)
        return {
            "name": user.name,
            "stats": {
                "weight_change_kg_per_week": stats.weight_change_kg_per_week,
                "plateau": stats.plateau,
                "avg_kcal": stats.avg_kcal_logged,
                "avg_protein": stats.avg_protein_logged,
                "avg_water_ml": stats.avg_water_ml,
                "exercise_sessions": stats.exercise_sessions,
                "nutrition_adherence": stats.nutrition_adherence,
                "water_adherence": stats.water_adherence,
                "exercise_adherence": stats.exercise_adherence,
                "cheat_meals": stats.cheat_meals,
            },
            "targets": {
                "kcal": targets.kcal,
                "protein_g": targets.protein_g,
                "carb_g": targets.carb_g,
                "fat_g": targets.fat_g,
                "fiber_g": targets.fiber_g,
                "water_ml": targets.water_ml,
                "diet_strategy": targets.diet_strategy,
            }
            if targets
            else None,
        }


@router.get("/users/{user_id}/series/{kind}", dependencies=[Depends(require_token)])
async def user_series(user_id: int, kind: str, days: int = Query(default=90, le=365)):
    async with session_scope() as session:
        user = await session.get(User, user_id)
        if not user:
            raise HTTPException(404)
        if kind == "weight":
            data = await weight_series(session, user_id, days)
        elif kind == "fat":
            data, _ = await body_comp_series(session, user_id, days)
        elif kind == "muscle":
            _, data = await body_comp_series(session, user_id, days)
        elif kind in ("kcal", "protein"):
            since = datetime.now(timezone.utc) - timedelta(days=days)
            res = await session.execute(
                select(MealLog).where(MealLog.user_id == user_id, MealLog.ts >= since)
            )
            by_day: dict[date, float] = {}
            for m in res.scalars():
                val = (m.kcal or 0) if kind == "kcal" else (m.protein_g or 0)
                by_day[m.ts.date()] = by_day.get(m.ts.date(), 0) + val
            data = sorted(by_day.items())
        elif kind == "water":
            since = datetime.now(timezone.utc) - timedelta(days=days)
            res = await session.execute(
                select(WaterLog).where(WaterLog.user_id == user_id, WaterLog.ts >= since)
            )
            by_day = {}
            for w in res.scalars():
                by_day[w.ts.date()] = by_day.get(w.ts.date(), 0) + w.amount_ml
            data = sorted(by_day.items())
        elif kind == "sleep":
            since = datetime.now(timezone.utc) - timedelta(days=days)
            res = await session.execute(
                select(SleepLog).where(SleepLog.user_id == user_id, SleepLog.ts >= since)
            )
            data = sorted((s.ts.date(), s.hours) for s in res.scalars())
        elif kind == "exercise":
            since = datetime.now(timezone.utc) - timedelta(days=days)
            res = await session.execute(
                select(ExerciseLog).where(ExerciseLog.user_id == user_id, ExerciseLog.ts >= since)
            )
            by_day = {}
            for e in res.scalars():
                by_day[e.ts.date()] = by_day.get(e.ts.date(), 0) + (e.duration_min or 0)
            data = sorted(by_day.items())
        else:
            raise HTTPException(400, "unknown series kind")
        return [{"date": d.isoformat(), "value": v} for d, v in data]


# ------------------------------------------------------------------ generic log CRUD


@router.get("/users/{user_id}/logs/{kind}", dependencies=[Depends(require_token)])
async def list_logs(user_id: int, kind: str, limit: int = Query(default=50, le=200)):
    try:
        entry = get_kind(kind)
    except KeyError:
        raise HTTPException(400, f"unknown log kind: {kind}")
    model = entry["model"]
    async with session_scope() as session:
        res = await session.execute(
            select(model).where(model.user_id == user_id).order_by(model.ts.desc()).limit(limit)
        )
        rows = list(res.scalars())
        out = []
        for r in rows:
            item = {"id": r.id, "ts": r.ts.isoformat()}
            for f in entry["fields"]:
                item[f] = getattr(r, f)
            out.append(item)
        return out


@router.post("/users/{user_id}/logs/{kind}", dependencies=[Depends(require_token)])
async def create_log(user_id: int, kind: str, body: LogCreate):
    try:
        entry = get_kind(kind)
    except KeyError:
        raise HTTPException(400, f"unknown log kind: {kind}")
    model = entry["model"]
    bad_keys = set(body.fields) - set(entry["fields"])
    if bad_keys:
        raise HTTPException(400, f"unknown fields for {kind}: {sorted(bad_keys)}")
    async with session_scope() as session:
        user = await session.get(User, user_id)
        if not user:
            raise HTTPException(404)
        row = model(user_id=user_id, **body.fields)
        session.add(row)
        await session.flush()
        if kind in ("weight", "bodycomp"):
            from app.services.targets import ensure_protein_floor

            await ensure_protein_floor(session, user)
        return {"id": row.id}


@router.patch("/logs/{kind}/{log_id}", dependencies=[Depends(require_token)])
async def update_log(kind: str, log_id: int, body: LogUpdate):
    try:
        entry = get_kind(kind)
    except KeyError:
        raise HTTPException(400, f"unknown log kind: {kind}")
    bad_keys = set(body.fields) - set(entry["fields"])
    if bad_keys:
        raise HTTPException(400, f"unknown fields for {kind}: {sorted(bad_keys)}")
    async with session_scope() as session:
        row = await session.get(entry["model"], log_id)
        if not row:
            raise HTTPException(404)
        for k, v in body.fields.items():
            setattr(row, k, v)
        await session.flush()
        if kind in ("weight", "bodycomp"):
            from app.services.targets import ensure_protein_floor

            user = await session.get(User, row.user_id)
            await ensure_protein_floor(session, user)
        return {"updated": True}


@router.delete("/logs/{kind}/{log_id}", dependencies=[Depends(require_token)])
async def delete_log(kind: str, log_id: int):
    try:
        entry = get_kind(kind)
    except KeyError:
        raise HTTPException(400, f"unknown log kind: {kind}")
    async with session_scope() as session:
        row = await session.get(entry["model"], log_id)
        if not row:
            raise HTTPException(404)
        await session.delete(row)
        return {"deleted": True}


@router.get("/log-kinds", dependencies=[Depends(require_token)])
async def log_kinds():
    return {k: v["fields"] for k, v in LOG_REGISTRY.items()}


# ------------------------------------------------------------------ food preferences


@router.get("/users/{user_id}/preferences", dependencies=[Depends(require_token)])
async def list_preferences(user_id: int):
    async with session_scope() as session:
        res = await session.execute(
            select(FoodPreference).where(FoodPreference.user_id == user_id).order_by(FoodPreference.level)
        )
        return [{"id": p.id, "name": p.name, "level": p.level, "category": p.category} for p in res.scalars()]


@router.post("/users/{user_id}/preferences", dependencies=[Depends(require_token)])
async def upsert_preference(user_id: int, body: PreferenceUpsert):
    async with session_scope() as session:
        user = await session.get(User, user_id)
        if not user:
            raise HTTPException(404)
        res = await session.execute(
            select(FoodPreference).where(FoodPreference.user_id == user_id, FoodPreference.name == body.name)
        )
        pref = res.scalar_one_or_none()
        if pref:
            pref.level = body.level
            pref.category = body.category
        else:
            pref = FoodPreference(user_id=user_id, name=body.name, level=body.level, category=body.category)
            session.add(pref)
        await session.flush()
        return {"id": pref.id}


@router.delete("/preferences/{pref_id}", dependencies=[Depends(require_token)])
async def delete_preference(pref_id: int):
    async with session_scope() as session:
        pref = await session.get(FoodPreference, pref_id)
        if not pref:
            raise HTTPException(404)
        await session.delete(pref)
        return {"deleted": True}


# ------------------------------------------------------------------ reminders


@router.get("/users/{user_id}/reminders", dependencies=[Depends(require_token)])
async def list_reminders(user_id: int):
    async with session_scope() as session:
        res = await session.execute(
            select(ReminderSetting).where(ReminderSetting.user_id == user_id).order_by(ReminderSetting.time_of_day)
        )
        return [
            {"id": r.id, "kind": r.kind, "time": r.time_of_day.strftime("%H:%M"), "enabled": r.enabled}
            for r in res.scalars()
        ]


@router.post("/users/{user_id}/reminders", dependencies=[Depends(require_token)])
async def create_reminder(user_id: int, body: ReminderCreate):
    async with session_scope() as session:
        user = await session.get(User, user_id)
        if not user:
            raise HTTPException(404)
        try:
            hh, mm = body.time.split(":")
            t = dtime(int(hh), int(mm))
        except (ValueError, AttributeError):
            raise HTTPException(400, "time must be HH:MM")
        row = ReminderSetting(user_id=user_id, kind=body.kind, time_of_day=t, enabled=body.enabled)
        session.add(row)
        await session.flush()
        return {"id": row.id, "kind": row.kind, "time": row.time_of_day.strftime("%H:%M"), "enabled": row.enabled}


@router.delete("/reminders/{reminder_id}", dependencies=[Depends(require_token)])
async def delete_reminder(reminder_id: int):
    async with session_scope() as session:
        r = await session.get(ReminderSetting, reminder_id)
        if not r:
            raise HTTPException(404)
        await session.delete(r)
        return {"deleted": True}


@router.patch("/reminders/{reminder_id}", dependencies=[Depends(require_token)])
async def update_reminder(reminder_id: int, body: ReminderUpdate):
    async with session_scope() as session:
        r = await session.get(ReminderSetting, reminder_id)
        if not r:
            raise HTTPException(404)
        if body.time is not None:
            try:
                hh, mm = body.time.split(":")
                r.time_of_day = dtime(int(hh), int(mm))
            except (ValueError, AttributeError):
                raise HTTPException(400, "time must be HH:MM")
        if body.enabled is not None:
            r.enabled = body.enabled
        return {"id": r.id, "time": r.time_of_day.strftime("%H:%M"), "enabled": r.enabled}


# ------------------------------------------------------------------ meal plan


@router.get("/users/{user_id}/plan", dependencies=[Depends(require_token)])
async def user_plan(user_id: int):
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    async with session_scope() as session:
        res = await session.execute(
            select(MealPlan)
            .where(MealPlan.user_id == user_id, MealPlan.week_start == week_start, MealPlan.status == "active")
            .order_by(MealPlan.id.desc())
            .limit(1)
        )
        plan = res.scalar_one_or_none()
        if not plan:
            return {"week_start": week_start.isoformat(), "days": []}
        res = await session.execute(select(PlannedMeal).where(PlannedMeal.plan_id == plan.id))
        meals = list(res.scalars())
        days = []
        for i in range(7):
            day_meals = [m for m in meals if m.day_index == i]
            days.append(
                {
                    "day_index": i,
                    "meals": [
                        {
                            "id": m.id,
                            "slot": m.slot,
                            "name": m.name,
                            "kcal": m.kcal,
                            "protein_g": m.protein_g,
                            "carb_g": m.carb_g,
                            "fat_g": m.fat_g,
                        }
                        for m in day_meals
                    ],
                }
            )
        return {"week_start": week_start.isoformat(), "strategy": plan.diet_strategy, "days": days}


# Plan generation runs in a background task: the AI call takes minutes, and a
# synchronous request would be killed by proxy timeouts / redeploys.
_plan_jobs: dict[int, str] = {}  # user_id -> "running" | "done" | "error"


async def _regenerate_plan_bg(user_id: int, app) -> None:
    import logging

    from app.services.mealplan import extract_dinners, generate_weekly_plan
    from app.services.shopping import build_weekly_shopping_list

    log = logging.getLogger(__name__)
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    try:
        async with session_scope() as session:
            user = await session.get(User, user_id)
            if not user:
                _plan_jobs[user_id] = "error"
                return
            res = await session.execute(
                select(MealPlan).where(
                    MealPlan.week_start == week_start, MealPlan.status == "active", MealPlan.user_id != user_id
                )
            )
            partner_plan = res.scalars().first()
            partner_dinners = None
            if partner_plan:
                await session.refresh(partner_plan, ["meals"])
                partner_dinners = extract_dinners(partner_plan.meals)
            plan = await generate_weekly_plan(session, user, week_start, partner_dinners)
            if plan:
                await build_weekly_shopping_list(session, week_start)
            user_name = user.name
        _plan_jobs[user_id] = "done" if plan else "error"

        # Announce in the shared group so the household hears about it too.
        bot_app = getattr(app.state, "bot_app", None)
        if plan and bot_app:
            from app.scheduler.jobs import _resolve_chat, _send_to

            async with session_scope() as session:
                user = await session.get(User, user_id)
                chat_id, _ = await _resolve_chat(session, user)
            await _send_to(
                bot_app, chat_id,
                f"📋 {user_name} için yeni haftalık plan hazır! /plan yazarak bugüne, "
                "/plan hafta yazarak tüm haftaya bakabilirsiniz. 🛒 Alışveriş listesi de güncellendi.",
            )
    except Exception:
        log.exception("background plan generation failed for user %s", user_id)
        _plan_jobs[user_id] = "error"


@router.post("/users/{user_id}/plan/regenerate", dependencies=[Depends(require_token)])
async def regenerate_plan(user_id: int, request: Request):
    import asyncio

    async with session_scope() as session:
        user = await session.get(User, user_id)
        if not user:
            raise HTTPException(404)
    if _plan_jobs.get(user_id) == "running":
        return {"status": "running"}
    _plan_jobs[user_id] = "running"
    asyncio.create_task(_regenerate_plan_bg(user_id, request.app))
    return {"status": "started"}


@router.get("/users/{user_id}/plan/status", dependencies=[Depends(require_token)])
async def plan_status(user_id: int):
    return {"status": _plan_jobs.get(user_id, "idle")}


# ------------------------------------------------------------------ shopping


@router.get("/shopping", dependencies=[Depends(require_token)])
async def shopping():
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    async with session_scope() as session:
        res = await session.execute(select(ShoppingList).where(ShoppingList.week_start == week_start))
        slist = res.scalar_one_or_none()
        if not slist:
            return {"week_start": week_start.isoformat(), "items": []}
        await session.refresh(slist, ["items"])
        return {
            "week_start": week_start.isoformat(),
            "items": [
                {"id": i.id, "name": i.name, "quantity": i.quantity, "category": i.category, "checked": i.checked}
                for i in slist.items
            ],
        }


@router.patch("/shopping/items/{item_id}", dependencies=[Depends(require_token)])
async def update_shopping_item(item_id: int, body: ShoppingItemUpdate):
    async with session_scope() as session:
        item = await session.get(ShoppingItem, item_id)
        if not item:
            raise HTTPException(404)
        item.checked = body.checked
        return {"id": item.id, "checked": item.checked}
