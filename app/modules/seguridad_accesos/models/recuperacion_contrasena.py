from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Identity, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class RecuperacionContrasena(Base):
    __tablename__ = "recuperacion_contrasena"
    __table_args__ = (
        CheckConstraint("expira_en > creada_en", name="recuperacion_contrasena_expiracion_check"),
        CheckConstraint("utilizada_en IS NULL OR utilizada_en >= creada_en", name="recuperacion_contrasena_utilizacion_check"),
        CheckConstraint("token_digest ~ '^[0-9a-f]{64}$'", name="recuperacion_contrasena_token_digest_formato_check").ddl_if(dialect="postgresql"),
        Index("recuperacion_contrasena_usuario_id_idx", "usuario_id"),
        Index("recuperacion_contrasena_expira_en_idx", "expira_en"),
    )
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), Identity(always=True), primary_key=True)
    usuario_id: Mapped[str] = mapped_column(ForeignKey("usuario.idusuario", onupdate="CASCADE", ondelete="RESTRICT"))
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    creada_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expira_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    utilizada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
