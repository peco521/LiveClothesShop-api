from sqlalchemy import CheckConstraint, ForeignKey, Integer, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Ciudad(Base):
    __tablename__ = "ciudad"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    nombre: Mapped[str] = mapped_column(String(50))


class Sucursal(Base):
    __tablename__ = "sucursal"
    __table_args__ = (CheckConstraint("estado IN ('activo', 'inactivo')"),)

    nro: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(50))
    direccion: Mapped[str] = mapped_column(String(100))
    estado: Mapped[str] = mapped_column(String(20), server_default="activo")
    idciud: Mapped[int] = mapped_column(SmallInteger, ForeignKey(
        "ciudad.id", onupdate="CASCADE", ondelete="RESTRICT"))
