from datetime import datetime, time

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, String, Time, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    language: Mapped[str] = mapped_column(String(8), default="tr")
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Istanbul")
    # "new" -> "onboarding" -> "active"
    onboarding_state: Mapped[str] = mapped_column(String(32), default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    profile: Mapped["Profile | None"] = relationship(back_populates="user", uselist=False)
    reminders: Mapped[list["ReminderSetting"]] = relationship(back_populates="user")


class Profile(Base):
    """Everything collected during onboarding. Lists/dicts stored as JSON."""

    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)

    # --- Basics ---
    age: Mapped[int | None]
    gender: Mapped[str | None] = mapped_column(String(16))  # "kadin" | "erkek"
    height_cm: Mapped[float | None]
    start_weight_kg: Mapped[float | None]
    goal_weight_kg: Mapped[float | None]
    body_fat_pct: Mapped[float | None]
    muscle_mass_kg: Mapped[float | None]
    waist_cm: Mapped[float | None]
    hip_cm: Mapped[float | None]
    neck_cm: Mapped[float | None]
    activity_level: Mapped[str | None] = mapped_column(String(32))  # sedanter..cok_aktif
    occupation: Mapped[str | None] = mapped_column(String(120))
    daily_movement: Mapped[str | None] = mapped_column(String(255))
    sleep_hours: Mapped[float | None]
    # Typical wake-up time; when set, reminders shift relative to it.
    wake_time: Mapped[time | None] = mapped_column(Time)
    # When true, water reminders auto-log a glass and just notify (opt-out),
    # instead of asking the user to confirm/tap each time.
    auto_water: Mapped[bool] = mapped_column(default=False)

    # --- Health ---
    diseases: Mapped[str | None] = mapped_column(String(1000))
    has_diabetes: Mapped[bool] = mapped_column(default=False)
    has_thyroid: Mapped[bool] = mapped_column(default=False)
    has_insulin_resistance: Mapped[bool] = mapped_column(default=False)
    has_hypertension: Mapped[bool] = mapped_column(default=False)
    has_cholesterol: Mapped[bool] = mapped_column(default=False)
    has_digestive_issues: Mapped[bool] = mapped_column(default=False)
    allergies: Mapped[str | None] = mapped_column(String(1000))
    intolerances: Mapped[str | None] = mapped_column(String(1000))
    medications: Mapped[str | None] = mapped_column(String(1000))
    supplements: Mapped[str | None] = mapped_column(String(1000))
    surgeries: Mapped[str | None] = mapped_column(String(1000))

    # --- Goals (multi-select) ---
    goals: Mapped[list | None] = mapped_column(JSON)  # e.g. ["yag_kaybi", "kas_kazanimi", ...]

    # --- Exercise ---
    exercise_types: Mapped[list | None] = mapped_column(JSON)  # ["gym","yuruyus",...]
    exercise_frequency_per_week: Mapped[int | None]
    exercise_duration_min: Mapped[int | None]

    # --- Nutrition habits ---
    eats_breakfast: Mapped[bool] = mapped_column(default=True)
    eats_lunch: Mapped[bool] = mapped_column(default=True)
    eats_dinner: Mapped[bool] = mapped_column(default=True)
    eats_snacks: Mapped[bool] = mapped_column(default=True)
    eats_outside: Mapped[str | None] = mapped_column(String(255))
    coffee_per_day: Mapped[int | None]
    tea_per_day: Mapped[int | None]
    water_glasses_per_day: Mapped[int | None]
    alcohol: Mapped[str | None] = mapped_column(String(255))
    smoking: Mapped[str | None] = mapped_column(String(255))

    # --- Kitchen / budget / shopping ---
    cooking_skill: Mapped[str | None] = mapped_column(String(32))  # yok / temel / iyi / cok_iyi
    kitchen_equipment: Mapped[list | None] = mapped_column(JSON)
    monthly_food_budget: Mapped[str | None] = mapped_column(String(120))
    shopping_preferences: Mapped[str | None] = mapped_column(String(500))

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="profile")


class ReminderSetting(Base):
    """Per-user reminder times. kind is one of the REMINDER_KINDS in scheduler/jobs.py."""

    __tablename__ = "reminder_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    time_of_day: Mapped[time] = mapped_column(Time)
    enabled: Mapped[bool] = mapped_column(default=True)

    user: Mapped[User] = relationship(back_populates="reminders")
