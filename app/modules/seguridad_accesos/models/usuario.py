from datetime import date

from sqlalchemy import CheckConstraint, Date, ForeignKey, JSON, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Usuario(Base):
    __tablename__ = "usuario"
    __table_args__ = (
        CheckConstraint("sexo IN ('M', 'F')"),
        CheckConstraint("tipo IN ('E', 'A', 'C')"),
    )

    idusuario: Mapped[str] = mapped_column(String, primary_key=True)
    ci: Mapped[str] = mapped_column(String)
    nombre: Mapped[str] = mapped_column(String(100))
    apellidopat: Mapped[str] = mapped_column(String(50))
    apellidomat: Mapped[str] = mapped_column(String(50))
    sexo: Mapped[str] = mapped_column(String(1))
    correo: Mapped[str] = mapped_column(String(100), unique=True)
    telefono: Mapped[str] = mapped_column(String(20))
    direccion: Mapped[str] = mapped_column(String(150))
    contrasena: Mapped[str] = mapped_column(Text)
    fechanac: Mapped[date] = mapped_column(Date)
    tipo: Mapped[str] = mapped_column(String(1))
    nrorol: Mapped[str] = mapped_column(ForeignKey("rol.nro", onupdate="CASCADE", ondelete="RESTRICT"))


class Cliente(Base):
    __tablename__ = "cliente"
    __table_args__ = (CheckConstraint("estado IN ('frecuente', 'casual', 'inactivo')"),)

    idusuario: Mapped[str] = mapped_column(
        ForeignKey("usuario.idusuario", onupdate="CASCADE", ondelete="CASCADE"), primary_key=True)
    cod_cl: Mapped[str] = mapped_column(String(10))
    estado: Mapped[str] = mapped_column(String(15), server_default="frecuente")
    proporciones: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
