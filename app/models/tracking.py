from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class WeightLog(Base):
    __tablename__ = "weight_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    weight_kg: Mapped[float]


class BodyCompositionLog(Base):
    __tablename__ = "body_composition_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    body_fat_pct: Mapped[float | None]
    muscle_mass_kg: Mapped[float | None]


class BodyMeasurement(Base):
    __tablename__ = "body_measurements"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    waist_cm: Mapped[float | None]
    hip_cm: Mapped[float | None]
    neck_cm: Mapped[float | None]


class WaterLog(Base):
    __tablename__ = "water_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    amount_ml: Mapped[int]


class StepsLog(Base):
    __tablename__ = "steps_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    steps: Mapped[int]


class ExerciseLog(Base):
    __tablename__ = "exercise_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    activity: Mapped[str] = mapped_column(String(120))
    duration_min: Mapped[int | None]
    intensity: Mapped[str | None] = mapped_column(String(32))  # hafif / orta / yogun
    note: Mapped[str | None] = mapped_column(String(500))


class SleepLog(Base):
    __tablename__ = "sleep_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    hours: Mapped[float]
    quality: Mapped[int | None]  # 1..5


class MoodLog(Base):
    __tablename__ = "mood_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    mood: Mapped[int | None]  # 1..5
    stress: Mapped[int | None]  # 1..5
    energy: Mapped[int | None]  # 1..5
    note: Mapped[str | None] = mapped_column(String(500))


class HungerLog(Base):
    __tablename__ = "hunger_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    hunger: Mapped[int | None]  # 1..5
    craving: Mapped[str | None] = mapped_column(String(255))


class ProgressPhoto(Base):
    __tablename__ = "progress_photos"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    telegram_file_id: Mapped[str] = mapped_column(String(255))
    note: Mapped[str | None] = mapped_column(String(255))
