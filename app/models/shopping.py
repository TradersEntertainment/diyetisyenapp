from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

SHOPPING_CATEGORIES = [
    "sebze",
    "meyve",
    "protein",
    "sut_urunleri",
    "donuk",
    "tahil",
    "baharat",
    "icecek",
    "diger",
]


class ShoppingList(Base):
    """One shared list per week for the whole household (both users)."""

    __tablename__ = "shopping_lists"

    id: Mapped[int] = mapped_column(primary_key=True)
    week_start: Mapped[date] = mapped_column(Date, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    items: Mapped[list["ShoppingItem"]] = relationship(
        back_populates="shopping_list", cascade="all, delete-orphan", order_by="ShoppingItem.category"
    )


class ShoppingItem(Base):
    __tablename__ = "shopping_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    list_id: Mapped[int] = mapped_column(ForeignKey("shopping_lists.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[str] = mapped_column(String(64), default="")  # "500 g", "2 adet"
    category: Mapped[str] = mapped_column(String(32), default="diger")
    checked: Mapped[bool] = mapped_column(default=False)

    shopping_list: Mapped[ShoppingList] = relationship(back_populates="items")
