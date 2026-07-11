from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class LabResult(Base):
    """A single blood-test / lab value, e.g. panel='LDL kolesterol', value=145, unit='mg/dL'.
    Read from a photographed lab report or entered in chat."""

    __tablename__ = "lab_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    panel: Mapped[str] = mapped_column(String(120))
    value: Mapped[float]
    unit: Mapped[str | None] = mapped_column(String(32))
    taken_on: Mapped[date | None] = mapped_column(Date)
