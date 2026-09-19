from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric, SmallInteger, String, Time, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Reserva(Base):
    __tablename__ = "reserva"
    __table_args__ = (CheckConstraint("estado IN ('pendiente', 'confirmada', 'atendida', 'cancelada', 'vencida')"),)

    nroreserva: Mapped[int] = mapped_column(Integer, primary_key=True)
    fechareserva: Mapped[date] = mapped_column(Date)
    horaatencion: Mapped[time] = mapped_column(Time)
    estado: Mapped[str] = mapped_column(String(20), server_default="pendiente")
    nrosuc: Mapped[int] = mapped_column(ForeignKey("sucursal.nro", onupdate="CASCADE", ondelete="CASCADE"))
    idusuariocl: Mapped[str] = mapped_column(ForeignKey("cliente.idusuario", onupdate="CASCADE", ondelete="CASCADE"))


class DetalleReserva(Base):
    __tablename__ = "detallereserva"
    __table_args__ = (CheckConstraint("cantidad > 0"),)

    nroreserva: Mapped[int] = mapped_column(ForeignKey("reserva.nroreserva", onupdate="CASCADE", ondelete="CASCADE"),
                                            primary_key=True)
    iddetalleres: Mapped[int] = mapped_column(Integer, primary_key=True)
    cantidad: Mapped[int] = mapped_column(Integer)
    idvar: Mapped[str] = mapped_column(ForeignKey("varianteprod.idvariante", onupdate="CASCADE", ondelete="CASCADE"))


class HorarioAtencion(Base):
    __tablename__ = "horario_atencion"

    idaten: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    horaini: Mapped[time] = mapped_column(Time)
    horafin: Mapped[time] = mapped_column(Time)


class HorarioSuc(Base):
    __tablename__ = "horario_suc"

    dias: Mapped[str] = mapped_column(String(100), server_default="Lunes,Martes,Miercoles,Jueves,Viernes,Sabado,Domingo")

    idaten: Mapped[int] = mapped_column(ForeignKey("horario_atencion.idaten", onupdate="CASCADE", ondelete="CASCADE"),
                                        primary_key=True)
    nrosuc: Mapped[int] = mapped_column(ForeignKey("sucursal.nro", onupdate="CASCADE", ondelete="CASCADE"),
                                        primary_key=True)


class Carrito(Base):
    __tablename__ = "carrito"
    __table_args__ = (CheckConstraint("estado IN ('activo', 'convertido', 'abandonado')"),)

    idcarrito: Mapped[int] = mapped_column(Integer, primary_key=True)
    fechahora: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    idusuariocl: Mapped[str] = mapped_column(ForeignKey("cliente.idusuario", onupdate="CASCADE", ondelete="CASCADE"))
    estado: Mapped[str] = mapped_column(String(20))


class DetalleCarro(Base):
    __tablename__ = "detallecarro"
    __table_args__ = (CheckConstraint("cantidad > 0"),)

    idcarrito: Mapped[int] = mapped_column(ForeignKey("carrito.idcarrito", onupdate="CASCADE", ondelete="CASCADE"),
                                           primary_key=True)
    iddetallecarro: Mapped[int] = mapped_column(Integer, primary_key=True)
    idvar: Mapped[str] = mapped_column(ForeignKey("varianteprod.idvariante", onupdate="CASCADE", ondelete="CASCADE"))
    cantidad: Mapped[int] = mapped_column(Integer)


class Venta(Base):
    __tablename__ = "venta"
    __table_args__ = (
        CheckConstraint("total >= 0"),
        CheckConstraint("desc_aplicado >= 0"),
        CheckConstraint("estado IN ('registrada', 'anulada')"),
    )

    nroventa: Mapped[int] = mapped_column(Integer, primary_key=True)
    nit: Mapped[str | None] = mapped_column(String(30))
    fechahora: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    total: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    desc_aplicado: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    estado: Mapped[str] = mapped_column(String(30), server_default="registrada")
    # CU24: la venta anónima de caja no identifica cliente. La columna debe admitir
    # NULL en PostgreSQL (database/sql/12_habilitar_cu24_venta_anonima.sql).
    idusuariocl: Mapped[str | None] = mapped_column(ForeignKey("cliente.idusuario", onupdate="CASCADE", ondelete="CASCADE"))
    idusuarioemp: Mapped[str | None] = mapped_column(ForeignKey("empleado.idusuario", onupdate="CASCADE", ondelete="CASCADE"))
    nrosuc: Mapped[int] = mapped_column(ForeignKey("sucursal.nro", onupdate="CASCADE", ondelete="CASCADE"))
    idcarrito: Mapped[int | None] = mapped_column(ForeignKey("carrito.idcarrito", onupdate="CASCADE", ondelete="CASCADE"))
    nroreserva: Mapped[int | None] = mapped_column(ForeignKey("reserva.nroreserva", onupdate="CASCADE", ondelete="CASCADE"))
    claveoperacion: Mapped[str | None] = mapped_column(String(36), unique=True)


class DetalleVenta(Base):
    __tablename__ = "detalleventa"
    __table_args__ = (
        CheckConstraint("precioUnitario > 0"),
        CheckConstraint("cantidad > 0"),
    )

    nroventa: Mapped[int] = mapped_column(ForeignKey("venta.nroventa", onupdate="CASCADE", ondelete="CASCADE"),
                                          primary_key=True)
    iddetalleventa: Mapped[int] = mapped_column(Integer, primary_key=True)
    preciounitario: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    descuentounitario: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    cantidad: Mapped[int] = mapped_column(Integer)
    idvar: Mapped[str] = mapped_column(ForeignKey("varianteprod.idvariante", onupdate="CASCADE", ondelete="CASCADE"))


class MovimientoInv(Base):
    __tablename__ = "movimiento_inv"
    __table_args__ = (CheckConstraint("tipoMov IN ('entrada', 'salida', 'ajuste')"),)

    idmov: Mapped[int] = mapped_column(Integer, primary_key=True)
    tipomov: Mapped[str] = mapped_column(String(20))
    cantidad: Mapped[int] = mapped_column(Integer)
    fecha: Mapped[date] = mapped_column(Date)
    motivo: Mapped[str] = mapped_column(String(255))
    nroinv: Mapped[int] = mapped_column(ForeignKey("inventario.nroinv", onupdate="CASCADE", ondelete="CASCADE"))


class Pago(Base):
    __tablename__ = "pago"
    __table_args__ = (
        CheckConstraint("metodo IN ('tarjeta', 'QR', 'transferencia', 'efectivo')"),
        CheckConstraint("monto >= 0"),
        CheckConstraint("estado IN ('pendiente', 'aprobado', 'rechazado')"),
    )

    idpago: Mapped[int] = mapped_column(Integer, primary_key=True)
    metodo: Mapped[str] = mapped_column(String(30))
    monto: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    estado: Mapped[str] = mapped_column(String(20), server_default="pendiente")
    fechahora: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    referencia: Mapped[str | None] = mapped_column(String(100))
    nroventa: Mapped[int] = mapped_column(ForeignKey("venta.nroventa", onupdate="CASCADE", ondelete="CASCADE"))
