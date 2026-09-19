from sqlalchemy import CheckConstraint, ForeignKey, Identity, Integer, Numeric, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from decimal import Decimal

from app.core.database import Base


class Ciudad(Base):
    __tablename__ = "ciudad"

    id: Mapped[int] = mapped_column(SmallInteger().with_variant(Integer, "sqlite"), Identity(), primary_key=True)
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
    # CU09: coordenadas verificadas con el proveedor de direcciones. La columna
    # ya existe en la base de datos (numeric(9,6) NULL) y las sucursales antiguas
    # pueden conservar ambos valores en NULL; no requiere migración.
    latitud: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    longitud: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
