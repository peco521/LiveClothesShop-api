from datetime import date
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, ForeignKey, Integer, Numeric, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, declared_attr, deferred, mapped_column

from app.core.database import Base


class TenantCatalog:
    # These columns already exist in the canonical SaaS schema. Deferred keeps
    # legacy reads compatible with installations that have no tenant columns.
    @declared_attr
    def idemp(cls) -> Mapped[int | None]:
        return deferred(mapped_column(Integer, nullable=True, server_default="NULL"))


class Categoria(TenantCatalog, Base):
    __tablename__ = "categoria"

    idcat: Mapped[int] = mapped_column(Integer, primary_key=True)
    descripcion: Mapped[str] = mapped_column(String(30))


class Marca(TenantCatalog, Base):
    __tablename__ = "marca"
    __table_args__ = (CheckConstraint("estado IN ('activo', 'inactivo')"),)

    idmarca: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(30))
    estado: Mapped[str] = mapped_column(String(20))


class Coleccion(TenantCatalog, Base):
    __tablename__ = "coleccion"

    idcol: Mapped[int] = mapped_column(Integer, primary_key=True)
    descripcion: Mapped[str] = mapped_column(String(150))


class Temporada(TenantCatalog, Base):
    __tablename__ = "temporada"
    __table_args__ = (CheckConstraint("estado IN ('activo', 'inactivo')"),)

    idtemp: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(100))
    fechaini: Mapped[date] = mapped_column(Date)
    fechafin: Mapped[date] = mapped_column(Date)
    estado: Mapped[str] = mapped_column(String(20))


class TempColeccion(TenantCatalog, Base):
    __tablename__ = "tempcoleccion"

    idtemp: Mapped[int] = mapped_column(ForeignKey("temporada.idtemp", onupdate="CASCADE", ondelete="CASCADE"),
                                        primary_key=True)
    idcol: Mapped[int] = mapped_column(ForeignKey("coleccion.idcol", onupdate="CASCADE", ondelete="CASCADE"),
                                       primary_key=True)


class Proveedor(TenantCatalog, Base):
    __tablename__ = "proveedor"

    idprov: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(100))
    correo: Mapped[str] = mapped_column(String(150))
    direccion: Mapped[str] = mapped_column(String(150))
    telefono: Mapped[str] = mapped_column(String(20))


class Promocion(TenantCatalog, Base):
    __tablename__ = "promocion"
    __table_args__ = (
        CheckConstraint("tipoDescuento IN ('porcentaje', 'montoFijo')"),
        CheckConstraint("valorDescuento > 0"),
        CheckConstraint("estado IN ('activo', 'inactivo')"),
    )

    idpromo: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(100))
    descripcion: Mapped[str | None] = mapped_column(String(300))
    tipodescuento: Mapped[str] = mapped_column(String(30))
    valordescuento: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    fechaini: Mapped[date] = mapped_column(Date)
    fechafin: Mapped[date] = mapped_column(Date)
    estado: Mapped[str] = mapped_column(String(20))


class Producto(TenantCatalog, Base):
    __tablename__ = "producto"
    __table_args__ = (CheckConstraint("estado IN ('activo', 'inactivo')"),)

    idprod: Mapped[str] = mapped_column(String(50), primary_key=True)
    descripcion: Mapped[str] = mapped_column(String(100))
    estado: Mapped[str] = mapped_column(String(20), server_default="activo")
    idpromo: Mapped[int | None] = mapped_column(ForeignKey("promocion.idpromo", onupdate="CASCADE", ondelete="CASCADE"))
    idcat: Mapped[int] = mapped_column(ForeignKey("categoria.idcat", onupdate="CASCADE", ondelete="CASCADE"))
    idmarca: Mapped[int] = mapped_column(ForeignKey("marca.idmarca", onupdate="CASCADE", ondelete="CASCADE"))
    idcol: Mapped[int] = mapped_column(ForeignKey("coleccion.idcol", onupdate="CASCADE", ondelete="CASCADE"))
    idprov: Mapped[int] = mapped_column(ForeignKey("proveedor.idprov", onupdate="CASCADE", ondelete="CASCADE"))


class Talla(TenantCatalog, Base):
    __tablename__ = "talla"

    idtalla: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    descripcion: Mapped[str] = mapped_column(String(8))


class VarianteProd(TenantCatalog, Base):
    __tablename__ = "varianteprod"
    __table_args__ = (
        CheckConstraint("precio > 0"),
        CheckConstraint("estado IN ('activo', 'inactivo')"),
    )

    idvariante: Mapped[str] = mapped_column(String(15), primary_key=True)
    sku: Mapped[str] = mapped_column(String(30), unique=True)
    precio: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    estado: Mapped[str] = mapped_column(String(20), server_default="activo")
    img: Mapped[str | None] = mapped_column(String(255))
    idtalla: Mapped[int] = mapped_column(ForeignKey("talla.idtalla", onupdate="CASCADE", ondelete="CASCADE"))
    idprod: Mapped[str] = mapped_column(ForeignKey("producto.idprod", onupdate="CASCADE", ondelete="CASCADE"))


class Color(TenantCatalog, Base):
    __tablename__ = "color"

    idcolor: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    descripcion: Mapped[str] = mapped_column(String(20))
    hex: Mapped[str] = mapped_column(String(20))


class VarianteColor(TenantCatalog, Base):
    __tablename__ = "variantecolors"

    idvar: Mapped[str] = mapped_column(ForeignKey("varianteprod.idvariante", onupdate="CASCADE", ondelete="CASCADE"),
                                       primary_key=True)
    idcolor: Mapped[int] = mapped_column(ForeignKey("color.idcolor", onupdate="CASCADE", ondelete="CASCADE"),
                                         primary_key=True)


class Inventario(TenantCatalog, Base):
    __tablename__ = "inventario"

    nroinv: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock: Mapped[int] = mapped_column(Integer)
    cantdisp: Mapped[int] = mapped_column(Integer)
    stockdanado: Mapped[int] = deferred(
        mapped_column(Integer, nullable=False, server_default="0"))
    nrosuc: Mapped[int] = mapped_column(ForeignKey("sucursal.nro", onupdate="CASCADE", ondelete="CASCADE"))
    idvar: Mapped[str] = mapped_column(ForeignKey("varianteprod.idvariante", onupdate="CASCADE", ondelete="CASCADE"))
