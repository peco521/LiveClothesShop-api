from fastapi import APIRouter, Depends, Query, Request, Response
from app.core.database import get_db
from app.modules.inventario_productos.shared.operaciones_access import access
from app.modules.inventario_productos.shared.operaciones_referencias import add_references, peer
from app.modules.inventario_productos.cu25_reportes.services import reportes
from datetime import date
from typing import Literal

router = APIRouter(prefix='/api/admin/reportes', tags=['CU25'])
a25 = access('CU25')
add_references(router, a25)


@router.get('')
def managerial_dashboard(fechaIni: date, fechaFin: date, request: Request, nroSuc: int | None = Query(None, ge=1), idCat: int | None = Query(None, ge=1), idTemp: int | None = Query(None, ge=1), db=Depends(get_db), actor=Depends(a25)):
    return reportes.dashboard(db, fechaIni, fechaFin, nroSuc, idCat, idTemp,
                              request.app.state.settings.reportes_zona_horaria)


@router.get('/exportar')
def report_export(fechaIni: date, fechaFin: date, tipo: Literal['ventas', 'inventario', 'reservas', 'devoluciones'], formato: Literal['pdf', 'xlsx'], request: Request, nroSuc: int | None = Query(None, ge=1), idCat: int | None = Query(None, ge=1), idTemp: int | None = Query(None, ge=1), db=Depends(get_db), actor=Depends(a25)):
    model = reportes.report_model(db, tipo, fechaIni, fechaFin, nroSuc, idCat, idTemp,
                                  request.app.state.settings.reportes_zona_horaria)
    content = reportes.pdf(model) if formato == 'pdf' else reportes.excel(model)
    media = 'application/pdf' if formato == 'pdf' else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    return Response(content, media_type=media, headers={'Content-Disposition': f'attachment; filename="reporte-{tipo}.{formato}"'})
