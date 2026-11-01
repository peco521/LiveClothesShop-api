from sqlalchemy import CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, deferred, mapped_column

from app.core.database import Base
from app.modules.seguridad_accesos.shared.models import Ciudad, Sucursal  # Register FK metadata.


class Empleado(Base):
    __tablename__ = "empleado"
    __table_args__ = (CheckConstraint("estado IN ('activo', 'inactivo')"),)

    idusuario: Mapped[str] = mapped_column(ForeignKey(
        "usuario.idusuario", onupdate="CASCADE", ondelete="CASCADE"), primary_key=True)
    # Required by the tenant INSERT trigger/RLS. before_flush fills this from
    # the authenticated session; it is deliberately not accepted from HTTP.
    idemp: Mapped[int | None] = deferred(mapped_column(Integer, server_default="NULL"))
    cod_emp: Mapped[str] = mapped_column(String(10))
    cargo: Mapped[str] = mapped_column(String(50))
    nrosuc: Mapped[int] = mapped_column(Integer, ForeignKey(
        "sucursal.nro", onupdate="CASCADE", ondelete="CASCADE"))
    # CU05: baja lógica del empleado. La columna ya existe en la base de datos
    # (estado varchar(15) NOT NULL DEFAULT 'activo'); no requiere migración.
    estado: Mapped[str] = mapped_column(String(15), server_default="activo")
