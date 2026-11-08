from types import SimpleNamespace
from uuid import uuid4
from copy import deepcopy
from decimal import Decimal
import json
import pytest
from pydantic import ValidationError
from app.core.errors import DomainError
from app.modules.ventas_reportes.cu25_reportes.schemas.dinamicos import ReportPlan
from app.modules.ventas_reportes.cu25_reportes.services import dinamicos as reports, interpretacion as interpreter, reportes

@pytest.mark.parametrize('extra',[{'idemp':2},{'sql':'SELECT * FROM usuario'},{'agrupacion':'contrasena'}, {'usuario':'otro'}])
def test_plan_does_not_accept_sql_tenant_or_arbitrary_fields(extra):
    with pytest.raises(ValidationError):ReportPlan.model_validate({'tipo':'existencias_bajas',**extra})

@pytest.mark.parametrize('value',[
    {'tipo':'ventas_sucursal'}, {'tipo':'ventas_semana','fechaIni':'2026-02-01','fechaFin':'2026-01-01'},
    {'tipo':'productos_vendidos','fechaIni':'2020-01-01','fechaFin':'2026-01-01'},
    {'tipo':'existencias_bajas','limite':101}, {'tipo':'existencias_bajas','limite':True},
    {'tipo':'existencias_bajas','nroSuc':True}, {'tipo':'SELECT 1'},
])
def test_plan_rejects_invalid_ranges_limits_and_types(value):
    with pytest.raises(ValidationError):ReportPlan.model_validate(value)

def test_snapshot_is_immutable_owner_scoped_expiring_and_feature_checked(monkeypatch):
    db=SimpleNamespace(info={'multitenant':True,'idemp':1})
    monkeypatch.setattr(reports,'feature',lambda *args:None)
    actor=('admin1',None);key=str(uuid4())
    original={'titulo':'Calculado','columnas':['importe'],'filas':[{'importe':'222.00'}]}
    reports._snapshots.clear()
    reports._snapshots[key]=(reports.monotonic()+reports.TTL,1,actor,deepcopy(original))
    returned=reports.snapshot(db,key,actor);returned['filas'][0]['importe']='1.00'
    assert reports.snapshot(db,key,actor)==original
    for tenant,other in [(1,('admin2',None)),(2,actor),(1,('admin1',3))]:
        db.info['idemp']=tenant
        with pytest.raises(DomainError):reports.snapshot(db,key,other)
    db.info['idemp']=1
    def deny(*args):raise DomainError(403,'plan','No permitido')
    monkeypatch.setattr(reports,'feature',deny)
    with pytest.raises(DomainError) as denied:reports.snapshot(db,key,actor)
    assert denied.value.status==403
    monkeypatch.setattr(reports,'feature',lambda *args:None)
    monkeypatch.setattr(reports,'monotonic',lambda:10**20)
    with pytest.raises(DomainError):reports.snapshot(db,key,actor)

def test_shared_export_renderers_consume_exact_decimal_result_not_requery():
    result={'titulo':'Resultado calculado','generadoEn':'2026-11-08','procedencia':'Fixture propio','columnas':['producto','importe'],
            'filas':[{'producto':'Camisa','importe':'222.00'}],'nota':'Origen real consultado'}
    model=reports.export_model(result)
    assert model['filas']==[['Camisa',Decimal('222.00')]]
    assert reportes.pdf(model).startswith(b'%PDF')
    assert reportes.excel(model).startswith(b'PK')

class FakeModel:
    def __init__(self,data):self.data=data
    def create_chat_completion(self,**kwargs):
        assert kwargs['response_format']['schema']
        return {'choices':[{'message':{'content':json.dumps(self.data)}}]}

@pytest.mark.parametrize('changes',[{'sql':'DELETE FROM venta'},{'idemp':2},{'nroSuc':999},{'limite':999}])
def test_untrusted_model_output_never_expands_query_capabilities(monkeypatch,changes):
    plan={'tipo':'ventas_sucursal','fechaIni':'2026-10-01','fechaFin':'2026-10-31','nroSuc':None,
          'limite':10,'umbral':5,'mensaje':'Propuesta'}
    monkeypatch.setattr(interpreter,'_model',FakeModel(plan|changes))
    result=interpreter.interpret('Solicitud no confiable',[{'nro':1,'nombre':'Central'}],enabled=True)
    assert result['estado']=='aclaracion' and result['plan'] is None

def test_model_optional_unavailable_is_not_claimed_as_keyword_ai():
    with pytest.raises(DomainError) as error:interpreter.interpret('Ventas',[],enabled=False)
    assert error.value.status==503
