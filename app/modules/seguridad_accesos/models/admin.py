from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Admin(Base):
    __tablename__ = "admin"

    idusuario: Mapped[str] = mapped_column(
        ForeignKey("usuario.idusuario", onupdate="CASCADE", ondelete="CASCADE"),
        primary_key=True,
    )
    # schema.sql does not declare cod_adm unique.
    cod_adm: Mapped[str] = mapped_column(String(10))
