from datetime import date

from app.core.errors import DomainError
from app.modules.inventario_productos.shared.access import actor_scope, transaction
from app.modules.inventario_productos.cu18_gestionar_catalogo.repositories.catalogo import get
from app.modules.inventario_productos.cu20_gestionar_inventario.repositories import inventario as repo
from app.modules.cliente_experiencia_compra.shared.models.catalogo import Inventario, VarianteProd, Producto
from app.modules.cliente_experiencia_compra.shared.models.comercio import MovimientoInv
from app.modules.seguridad_accesos.shared.models import Sucursal
from app.modules.seguridad_accesos.services.bitacora import record


def enforce_branch(scope, branch):
    if scope is not None and branch != scope:
        raise DomainError(403, 'sucursal_no_autorizada', 'Solo puedes gestionar el inventario de tu sucursal')


def register(db, data, actor, peer):
    with transaction(db):
        scope = actor_scope(db, actor, 'CU20', True)
        enforce_branch(scope, data.nroSuc)
        get(db, Sucursal, data.nroSuc, reference=True)
        variant = get(db, VarianteProd, data.idVariante, reference=True)
        get(db, Producto, variant.idprod, reference=True)
        row = repo.inventory_row(db, data.nroSuc, data.idVariante)
        scope = actor_scope(db, actor, 'CU20', True)
        enforce_branch(scope, data.nroSuc)
        delta = -data.cantidad if data.tipoMov == 'salida' or (data.tipoMov == 'ajuste' and data.ajusteDireccion == 'disminuir') else data.cantidad
        if row is None:
            if delta < 0:
                raise DomainError(409, 'stock_insuficiente', 'No hay inventario para registrar esta salida o ajuste')
            row = Inventario(nrosuc=data.nroSuc, idvar=data.idVariante, stock=0, cantdisp=0)
            db.add(row); db.flush()
        if row.cantdisp < 0 or row.stock < row.cantdisp:
            raise DomainError(409, 'inventario_inconsistente', 'El inventario actual necesita revisión antes de registrar movimientos')
        if delta < 0 and -delta > row.cantdisp:
            raise DomainError(409, 'stock_insuficiente', 'La cantidad supera las unidades disponibles; no puedes retirar unidades reservadas')
        if row.stock + delta > 2147483647 or row.cantdisp + delta > 2147483647:
            raise DomainError(422, 'stock_fuera_de_rango', 'La cantidad supera el límite de inventario')
        if db.get_bind().dialect.name == 'postgresql':
            # El procedimiento modifica stock y registra el movimiento: no duplicar esos cambios aquí.
            repo.call_movement(db, row.nroinv, data.tipoMov, delta if data.tipoMov == 'ajuste' else data.cantidad, data.motivo)
            db.refresh(row)
        else:
            # Equivalente exclusivamente para pruebas SQLite; producción usa CALL.
            row.stock += delta; row.cantdisp += delta
            db.add(MovimientoInv(nroinv=row.nroinv, tipomov=data.tipoMov,
                               cantidad=delta if data.tipoMov == 'ajuste' else data.cantidad, motivo=data.motivo, fecha=date.today()))
            db.flush()
        record(db, 'inventario_movimiento_registrado', actor, peer, True)
        result = {'nroInv': row.nroinv, 'stock': row.stock, 'cantDisp': row.cantdisp, 'mensaje': 'Movimiento registrado correctamente'}
    return result
