from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.modules.seguridad_accesos.shared.models import Ciudad, Sucursal  # Register FK metadata.


class Empleado(Base):
    __tablename__ = "empleado"

    idusuario: Mapped[str] = mapped_column(ForeignKey(
        "usuario.idusuario", onupdate="CASCADE", ondelete="CASCADE"), primary_key=True)
    cod_emp: Mapped[str] = mapped_column(String(10))
    cargo: Mapped[str] = mapped_column(String(50))
    nrosuc: Mapped[int] = mapped_column(Integer, ForeignKey(
        "sucursal.nro", onupdate="CASCADE", ondelete="CASCADE"))
