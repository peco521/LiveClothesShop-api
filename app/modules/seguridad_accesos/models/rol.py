from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Rol(Base):
    __tablename__ = "rol"
    nro: Mapped[str] = mapped_column(String(15), primary_key=True)
    descripcion: Mapped[str] = mapped_column(String(50))


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
