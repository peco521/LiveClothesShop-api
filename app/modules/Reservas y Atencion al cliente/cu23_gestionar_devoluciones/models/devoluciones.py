from datetime import datetime
from decimal import Decimal
from uuid import UUID
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, Numeric, String, UniqueConstraint, Uuid, func, text
from sqlalchemy.orm import Mapped, deferred, mapped_column
from app.core.database import Base


class PoliticaDevolucion(Base):
    __tablename__ = 'politica_devolucion'
    __mapper_args__ = {"eager_defaults": False}
    __table_args__ = (CheckConstraint('dias > 0'), CheckConstraint('porcentaje > 0 AND porcentaje <= 100'), UniqueConstraint('idprod', 'idvar'))
    idemp: Mapped[int | None] = deferred(mapped_column(Integer, server_default=text('NULL')))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    idprod: Mapped[str] = mapped_column(ForeignKey('producto.idprod'))
    idvar: Mapped[str | None] = mapped_column(ForeignKey('varianteprod.idvariante'))
    dias: Mapped[int] = mapped_column(Integer)
    porcentaje: Mapped[Decimal] = mapped_column(Numeric(5, 2))


class Devolucion(Base):
    __tablename__ = 'devolucion'
    __mapper_args__ = {"eager_defaults": False}
    __table_args__ = (CheckConstraint("estado IN ('pendiente','rechazada','aprobada')"),)
    idemp: Mapped[int | None] = deferred(mapped_column(Integer, server_default=text('NULL')))
    nrodev: Mapped[int] = mapped_column(Integer, primary_key=True)
    fechahora: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    monto: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    estado: Mapped[str] = mapped_column(String(30))
    nroventa: Mapped[int] = mapped_column(ForeignKey('venta.nroventa'))
    motivo: Mapped[str | None] = mapped_column(String(255))
    idoperacion: Mapped[UUID | None] = deferred(mapped_column(Uuid(as_uuid=True), server_default=text('NULL')))
    huellasolicitud: Mapped[str | None] = deferred(mapped_column(String(64), server_default=text('NULL')))


class DetalleDev(Base):
    __tablename__ = 'detalledev'
    __mapper_args__ = {"eager_defaults": False}
    __table_args__ = (ForeignKeyConstraint(['nroventa', 'iddetalleventa'], ['detalleventa.nroventa', 'detalleventa.iddetalleventa']),)
    idemp: Mapped[int | None] = deferred(mapped_column(Integer, server_default=text('NULL')))
    nrodev: Mapped[int] = mapped_column(ForeignKey('devolucion.nrodev'), primary_key=True)
    iddetalledev: Mapped[int] = mapped_column(Integer, primary_key=True)
    cantidad: Mapped[int] = mapped_column(Integer)
    nroventa: Mapped[int] = mapped_column(Integer)
    iddetalleventa: Mapped[int] = mapped_column(Integer)
    condicion: Mapped[str | None] = deferred(mapped_column(String(20), server_default=text('NULL')))


class Cambio(Base):
    __tablename__ = 'cambio'
    __mapper_args__ = {"eager_defaults": False}
    idemp: Mapped[int | None] = deferred(mapped_column(Integer, server_default=text('NULL')))
    nrocambio: Mapped[int] = mapped_column(Integer, primary_key=True)
    fechahora: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    estado: Mapped[str] = mapped_column(String(20), server_default='pendiente')
    motivo: Mapped[str | None] = mapped_column(String(255))
    difmonto: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    nroventa: Mapped[int] = mapped_column(Integer)
    nrosuc: Mapped[int] = mapped_column(Integer)
    idusuarioemp: Mapped[str | None] = mapped_column(String)
    nrodev: Mapped[int | None] = deferred(mapped_column(Integer, server_default=text('NULL')))
    nroventanueva: Mapped[int | None] = deferred(mapped_column(Integer, server_default=text('NULL')))


class DetalleCambio(Base):
    __tablename__ = 'detallecambio'
    __mapper_args__ = {"eager_defaults": False}
    idemp: Mapped[int | None] = deferred(mapped_column(Integer, server_default=text('NULL')))
    nrocambio: Mapped[int] = mapped_column(Integer, primary_key=True)
    iddetallecambio: Mapped[int] = mapped_column(Integer, primary_key=True)
    cantidad: Mapped[int] = mapped_column(Integer)
    nroventa: Mapped[int] = mapped_column(Integer)
    iddetalleventa: Mapped[int] = mapped_column(Integer)
    idvarnueva: Mapped[str] = mapped_column(String(15))
    precionuevo: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    retenida: Mapped[bool] = deferred(mapped_column(server_default=text('false')))


class Reembolso(Base):
    __tablename__ = 'reembolso'
    __mapper_args__ = {"eager_defaults": False}
    __table_args__ = (UniqueConstraint('nrodev'),)
    idemp: Mapped[int | None] = deferred(mapped_column(Integer, server_default=text('NULL')))
    idreembolso: Mapped[int] = mapped_column(Integer, primary_key=True)
    metodo: Mapped[str] = mapped_column(String(30))
    monto: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    fechahora: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    estado: Mapped[str] = mapped_column(String(20))
    idpago: Mapped[int] = mapped_column(ForeignKey('pago.idpago'))
    nrodev: Mapped[int] = mapped_column(ForeignKey('devolucion.nrodev'))
    referencia: Mapped[str | None] = mapped_column(String(100))
