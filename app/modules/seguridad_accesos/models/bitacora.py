from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, JSON, String, func
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, deferred, mapped_column

from app.core.database import Base


class Bitacora(Base):
    __tablename__ = "bitacora"
    __mapper_args__ = {"eager_defaults": False}
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    idemp: Mapped[int | None] = deferred(mapped_column(Integer, server_default="NULL"))
    usuario_id: Mapped[str | None] = mapped_column(String(100), ForeignKey("usuario.idusuario", ondelete="SET NULL"))
    accion: Mapped[str] = mapped_column(String(150))
    ip: Mapped[str | None] = mapped_column(String().with_variant(INET, "postgresql"))
    detalles: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    fecha: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
