from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, Identity, Integer, SmallInteger, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Plan(Base):
    __tablename__ = 'plan'

    idplan: Mapped[int] = mapped_column(SmallInteger().with_variant(Integer, 'sqlite'), Identity(), primary_key=True)
    nombre: Mapped[str] = mapped_column(String(30), unique=True)
    maxsucursales: Mapped[int] = mapped_column(SmallInteger)
    estado: Mapped[str] = mapped_column(String(20), server_default='activo')


class FuncionalidadSaas(Base):
    __tablename__ = 'funcionalidad_saas'

    codigo: Mapped[str] = mapped_column(String(30), primary_key=True)
    descripcion: Mapped[str] = mapped_column(String(100))


class PlanFuncionalidad(Base):
    __tablename__ = 'plan_funcionalidad'

    idplan: Mapped[int] = mapped_column(ForeignKey('plan.idplan'), primary_key=True)
    codigo: Mapped[str] = mapped_column(ForeignKey('funcionalidad_saas.codigo'), primary_key=True)


class SuscripcionPlan(Base):
    __tablename__ = 'suscripcion'

    idsusc: Mapped[int] = mapped_column(Integer, primary_key=True)
    idemp: Mapped[int] = mapped_column(Integer)
    idplan: Mapped[int] = mapped_column(ForeignKey('plan.idplan'))
    fechainicio: Mapped[date] = mapped_column(Date)
    fechavencimiento: Mapped[date] = mapped_column(Date)
    estado: Mapped[str] = mapped_column(String(20))


class PlanVersion(Base):
    __tablename__ = 'plan_version'
    __table_args__ = (UniqueConstraint('idplan', 'numero'),)

    idversion: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    idplan: Mapped[int] = mapped_column(ForeignKey('plan.idplan'))
    numero: Mapped[int] = mapped_column(Integer)
    nombre: Mapped[str] = mapped_column(String(30))
    maxsucursales: Mapped[int] = mapped_column(SmallInteger)
    estado: Mapped[str] = mapped_column(String(20))
    vigente: Mapped[bool] = mapped_column(Boolean)


class PlanVersionFuncionalidad(Base):
    __tablename__ = 'plan_version_funcionalidad'

    idversion: Mapped[int] = mapped_column(ForeignKey('plan_version.idversion'), primary_key=True)
    codigo: Mapped[str] = mapped_column(ForeignKey('funcionalidad_saas.codigo'), primary_key=True)


class SuscripcionVersion(Base):
    __tablename__ = 'suscripcion_plan_version'

    idsusc: Mapped[int] = mapped_column(ForeignKey('suscripcion.idsusc'), primary_key=True)
    idplan: Mapped[int] = mapped_column(ForeignKey('plan.idplan'))
    idversion: Mapped[int] = mapped_column(ForeignKey('plan_version.idversion'))
