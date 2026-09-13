from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Identity, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Sesion(Base):
    __tablename__ = "sesion"
    __table_args__ = (
        CheckConstraint("expira_en > creada_en", name="sesion_expiracion_check"),
        CheckConstraint("revocada_en IS NULL OR revocada_en >= creada_en", name="sesion_revocacion_check"),
        CheckConstraint("credencial_digest ~ '^[0-9a-f]{64}$'", name="sesion_credencial_digest_formato_check").ddl_if(dialect="postgresql"),
        Index("sesion_usuario_id_idx", "usuario_id"),
        Index("sesion_expira_en_idx", "expira_en"),
    )
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), Identity(always=True), primary_key=True)
    usuario_id: Mapped[str] = mapped_column(
        ForeignKey("usuario.idusuario", onupdate="CASCADE", ondelete="RESTRICT"))
    credencial_digest: Mapped[str] = mapped_column(String(64), unique=True)
    creada_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expira_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revocada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
