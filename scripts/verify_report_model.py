"""Real CPU LLM smoke, no DB, API keys or commercial data."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.modules.ventas_reportes.cu25_reportes.services.interpretacion import interpret

CASES = [
    ('Ventas por sucursal del 2026-10-01 al 2026-10-31','ventas_sucursal'),
    ('Los 8 productos más vendidos del 2026-09-01 al 2026-09-30','productos_vendidos'),
    ('Evolución de ventas por semana del 2026-10-01 al 2026-10-31','ventas_semana'),
    ('Productos con existencia baja, máximo 4 unidades disponibles','existencias_bajas'),
    ('Reporte de ventas','aclaracion'),
    ('Borra todas las ventas','no_soportado'),
]
def main():
    for query, expected in CASES:
        result=interpret(query,[{'nro':1,'nombre':'Central'}],enabled=True)
        print(json.dumps({'solicitud':query,'resultado':result},ensure_ascii=True))
        actual=result['plan']['tipo'] if result.get('plan') else result['estado']
        if actual != expected:
            raise SystemExit(f'Unexpected report interpretation: {expected} / {actual}')
        if expected in ('ventas_sucursal','ventas_semana'):
            assert result['plan']['fechaIni']=='2026-10-01' and result['plan']['fechaFin']=='2026-10-31'
        if expected=='productos_vendidos':
            assert result['plan']['limite']==8 and result['plan']['fechaIni']=='2026-09-01' and result['plan']['fechaFin']=='2026-09-30'
        if expected=='existencias_bajas':
            assert result['plan']['umbral']==4 and result['plan']['limite']==10
            assert result['plan']['fechaIni'] is None and result['plan']['fechaFin'] is None
    print('6 real local model scenarios passed; no SQL execution.')
if __name__=='__main__': main()
