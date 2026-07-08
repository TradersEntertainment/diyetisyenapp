import enum
from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Food(Base):
    """Food database entry — Turkish / Mediterranean staples. Values per 100 g."""

    __tablename__ = "foods"

    id: Mapped[int] = mapped_column(primary_key=True)
    name_tr: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(64))  # sebze, meyve, protein, sut_urunu, ...
    kcal: Mapped[float]
    protein_g: Mapped[float]
    carb_g: Mapped[float]
    fat_g: Mapped[float]
    fiber_g: Mapped[float] = mapped_column(default=0.0)
    typical_portion_g: Mapped[float | None]
    typical_portion_name: Mapped[str | None] = mapped_column(String(120))  # "1 kase", "1 dilim"


class TargetHistory(Base):
    """Every calorie/macro target and diet-strategy change is a new row.

    Invariant: protein_g is never below the body-analysis protein floor
    (see services/calculations.py::protein_floor_g).
    """

    __tablename__ = "target_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    kcal: Mapped[int]
    protein_g: Mapped[int]
    fat_g: Mapped[int]
    carb_g: Mapped[int]
    fiber_g: Mapped[int]
    water_ml: Mapped[int]
    diet_strategy: Mapped[str] = mapped_column(String(255), default="dengeli")
    reason: Mapped[str] = mapped_column(String(2000), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MealSlot(str, enum.Enum):
    breakfast = "kahvalti"
    snack1 = "ara_ogun_1"
    lunch = "ogle"
    snack2 = "ara_ogun_2"
    dinner = "aksam"
    night_snack = "gece_atistirmasi"


class MealPlan(Base):
    __tablename__ = "meal_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    week_start: Mapped[date] = mapped_column(Date, index=True)  # Monday
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | archived
    diet_strategy: Mapped[str] = mapped_column(String(255), default="dengeli")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    meals: Mapped[list["PlannedMeal"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="PlannedMeal.day_index"
    )


class PlannedMeal(Base):
    __tablename__ = "planned_meals"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("meal_plans.id"), index=True)
    day_index: Mapped[int]  # 0 = Monday .. 6 = Sunday
    slot: Mapped[str] = mapped_column(String(32))  # MealSlot value
    name: Mapped[str] = mapped_column(String(255))
    recipe: Mapped[str] = mapped_column(String(4000), default="")
    prep_minutes: Mapped[int] = mapped_column(default=0)
    kcal: Mapped[int] = mapped_column(default=0)
    protein_g: Mapped[float] = mapped_column(default=0.0)
    carb_g: Mapped[float] = mapped_column(default=0.0)
    fat_g: Mapped[float] = mapped_column(default=0.0)
    fiber_g: Mapped[float] = mapped_column(default=0.0)
    ingredients: Mapped[list | None] = mapped_column(JSON)  # [{"name","qty","unit","category"}]
    alternatives: Mapped[list | None] = mapped_column(JSON)  # [{"name","note"}]
    shared_with_partner: Mapped[bool] = mapped_column(default=False)

    plan: Mapped[MealPlan] = relationship(back_populates="meals")


class MealLog(Base):
    __tablename__ = "meal_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    slot: Mapped[str | None] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(String(1000))
    kcal: Mapped[int | None]
    protein_g: Mapped[float | None]
    carb_g: Mapped[float | None]
    fat_g: Mapped[float | None]
    fiber_g: Mapped[float | None]
    is_cheat: Mapped[bool] = mapped_column(default=False)
    planned_meal_id: Mapped[int | None] = mapped_column(ForeignKey("planned_meals.id"))
