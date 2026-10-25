from sqlalchemy import CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, deferred, mapped_column

from app.core.database import Base


class Rol(Base):
    __tablename__ = "rol"
    __mapper_args__ = {"eager_defaults": False}
    __table_args__ = (CheckConstraint("estado IN ('activo', 'inactivo')"),)

    nro: Mapped[str] = mapped_column(String(15), primary_key=True)
    idemp: Mapped[int | None] = deferred(mapped_column(Integer, server_default="NULL"))
    descripcion: Mapped[str] = mapped_column(String(50))
    # CU06: baja lógica del rol. La columna ya existe en la base de datos
    # (estado varchar(15) NOT NULL DEFAULT 'activo'); no requiere migración.
    estado: Mapped[str] = mapped_column(String(15), server_default="activo")


class Funcion(Base):
    __tablename__ = "funcion"
    id: Mapped[str] = mapped_column(String(15), primary_key=True)
    descripcion: Mapped[str | None] = mapped_column(String(50))


class RolFuncion(Base):
    __tablename__ = "rol_funcion"
    nrorol: Mapped[str] = mapped_column(
        ForeignKey("rol.nro", onupdate="CASCADE", ondelete="CASCADE"), primary_key=True)
    idfun: Mapped[str] = mapped_column(
        ForeignKey("funcion.id", onupdate="CASCADE", ondelete="CASCADE"), primary_key=True)
    descripcion: Mapped[str] = mapped_column(String(100))
