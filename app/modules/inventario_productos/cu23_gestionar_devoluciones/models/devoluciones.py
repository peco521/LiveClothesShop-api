from datetime import datetime
from decimal import Decimal
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base


class PoliticaDevolucion(Base):
    __tablename__ = 'politica_devolucion'
    __table_args__ = (CheckConstraint('dias > 0'), CheckConstraint('porcentaje > 0 AND porcentaje <= 100'), UniqueConstraint('idprod', 'idvar'))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    idprod: Mapped[str] = mapped_column(ForeignKey('producto.idprod'))
    idvar: Mapped[str | None] = mapped_column(ForeignKey('varianteprod.idvariante'))
    dias: Mapped[int] = mapped_column(Integer)
    porcentaje: Mapped[Decimal] = mapped_column(Numeric(5, 2))


class Devolucion(Base):
    __tablename__ = 'devolucion'
    __table_args__ = (CheckConstraint("estado IN ('pendiente','rechazada','aprobada')"),)
    nrodev: Mapped[int] = mapped_column(Integer, primary_key=True)
    fechahora: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    monto: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    estado: Mapped[str] = mapped_column(String(30))
    nroventa: Mapped[int] = mapped_column(ForeignKey('venta.nroventa'))
    motivo: Mapped[str | None] = mapped_column(String(255))


class DetalleDev(Base):
    __tablename__ = 'detalledev'
    __table_args__ = (ForeignKeyConstraint(['nroventa', 'iddetalleventa'], ['detalleventa.nroventa', 'detalleventa.iddetalleventa']),)
    nrodev: Mapped[int] = mapped_column(ForeignKey('devolucion.nrodev'), primary_key=True)
    iddetalledev: Mapped[int] = mapped_column(Integer, primary_key=True)
    cantidad: Mapped[int] = mapped_column(Integer)
    nroventa: Mapped[int] = mapped_column(Integer)
    iddetalleventa: Mapped[int] = mapped_column(Integer)


class Reembolso(Base):
    __tablename__ = 'reembolso'
    __table_args__ = (UniqueConstraint('nrodev'),)
    idreembolso: Mapped[int] = mapped_column(Integer, primary_key=True)
    metodo: Mapped[str] = mapped_column(String(30))
    monto: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    fechahora: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    estado: Mapped[str] = mapped_column(String(20))
    idpago: Mapped[int] = mapped_column(ForeignKey('pago.idpago'))
    nrodev: Mapped[int] = mapped_column(ForeignKey('devolucion.nrodev'))
    referencia: Mapped[str | None] = mapped_column(String(100))
