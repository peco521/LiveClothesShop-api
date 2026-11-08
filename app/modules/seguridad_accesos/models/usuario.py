from datetime import date

from sqlalchemy import CheckConstraint, Date, ForeignKey, Integer, JSON, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, deferred, mapped_column

from app.core.database import Base


class Usuario(Base):
    __tablename__ = "usuario"
    __mapper_args__ = {"eager_defaults": False}
    __table_args__ = (
        CheckConstraint("sexo IN ('M', 'F')"),
        CheckConstraint("tipo IN ('E', 'A', 'C', 'S')"),
    )

    idusuario: Mapped[str] = mapped_column(String, primary_key=True)
    ci: Mapped[str] = mapped_column(String)
    # CU07 admite nombres NULL en cuentas heredadas; las altas siguen validadas.
    nombre: Mapped[str | None] = mapped_column(String(100))
    apellidopat: Mapped[str] = mapped_column(String(50))
    apellidomat: Mapped[str] = mapped_column(String(50))
    sexo: Mapped[str] = mapped_column(String(1))
    correo: Mapped[str] = mapped_column(String(100))
    telefono: Mapped[str] = mapped_column(String(20))
    direccion: Mapped[str] = mapped_column(String(150))
    contrasena: Mapped[str] = mapped_column(Text)
    fechanac: Mapped[date] = mapped_column(Date)
    tipo: Mapped[str] = mapped_column(String(1))
    idemp: Mapped[int | None] = deferred(mapped_column(Integer, nullable=True, server_default=text("NULL")))
    nrorol: Mapped[str] = mapped_column(ForeignKey("rol.nro", onupdate="CASCADE", ondelete="RESTRICT"))


class Cliente(Base):
    __tablename__ = "cliente"
    __mapper_args__ = {"eager_defaults": False}
    idemp: Mapped[int | None] = deferred(mapped_column(Integer, server_default=text("NULL")))
    __table_args__ = (CheckConstraint("estado IN ('frecuente', 'casual', 'inactivo')"),)

    idusuario: Mapped[str] = mapped_column(
        ForeignKey("usuario.idusuario", onupdate="CASCADE", ondelete="CASCADE"), primary_key=True)
    cod_cl: Mapped[str] = mapped_column(String(10))
    estado: Mapped[str] = mapped_column(String(15), server_default="frecuente")
    proporciones: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
