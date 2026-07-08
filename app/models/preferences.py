import enum
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PreferenceLevel(str, enum.Enum):
    love = "bayilirim"
    like = "severim"
    can_eat = "yiyebilirim"
    dislike = "sevmem"
    never = "asla"


class FoodPreference(Base):
    """Learned food/cuisine preferences — the ground truth that keeps meal plans
    personal instead of random. NEVER/DISLIKE entries are hard-excluded from plans."""

    __tablename__ = "food_preferences"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_food_pref_user_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    level: Mapped[str] = mapped_column(String(32))  # PreferenceLevel value
    # sebze, meyve, et, tatli, icecek, atistirmalik, kahvalti, aksam, restoran, kacamak, mutfak, genel
    category: Mapped[str] = mapped_column(String(32), default="genel")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
