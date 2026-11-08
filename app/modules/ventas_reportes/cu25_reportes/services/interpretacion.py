"""Optional real local LLM. Its output is an untrusted proposal, never SQL."""
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo
from pydantic import ValidationError
from app.core.errors import DomainError
from ..schemas.dinamicos import InterpreterPlan, ReportPlan, StockParameters

_lock = Lock()
_model = None
MODEL_PATH = Path(__file__).resolve().parents[5] / '.local/models/qwen2.5-1.5b-instruct-q4_k_m.gguf'
MODEL_SHA = '6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e'
MODEL_NAME = 'Qwen2.5-1.5B-Instruct Q4_K_M local'

def interpret(text, branches, *, enabled=False):
    if not enabled:
        raise DomainError(503,'modelo_ia_no_disponible','Interpretación IA local desactivada; usa los reportes estructurados')
    if not _lock.acquire(blocking=False):
        raise DomainError(503,'modelo_ia_ocupado','El modelo local está ocupado; inténtalo nuevamente')
    try:
        global _model
        if _model is None:
            if not MODEL_PATH.is_file():
                raise DomainError(503,'modelo_ia_no_disponible','Descarga el modelo local verificado; los reportes estructurados siguen disponibles')
            with MODEL_PATH.open('rb') as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            if digest != MODEL_SHA:
                raise DomainError(503,'modelo_ia_invalido','El modelo local no pasó la comprobación de integridad')
            try:
                from llama_cpp import Llama
                _model = Llama(model_path=str(MODEL_PATH), n_ctx=3072, n_threads=4, verbose=False, seed=42)
            except (ImportError, OSError, ValueError):
                raise DomainError(503,'modelo_ia_no_disponible','No se pudo cargar el modelo local; usa los reportes estructurados') from None
        today = datetime.now(ZoneInfo('America/La_Paz')).date().isoformat()
        prompt = f"""Translate a Spanish FashionStore report request to JSON. Do not answer the report, only extract its parameters.
Today: {today}. No SQL, company, user, data changes or secrets are allowed.
Types: ventas_sucursal=sales by branch; productos_vendidos=top selling products;
ventas_semana=weekly sales; existencias_bajas=current low inventory;
aclaracion=missing report or dates; no_soportado=other requests.
Sales need dates. Never invent dates. Low inventory uses null dates.
Authorized branches: {json.dumps(branches, ensure_ascii=False)}. All branches means nroSuc null.
Default limite 10, umbral 5. mensaje is a short Spanish explanation.
limite is the NUMBER OF OUTPUT ROWS, not available inventory units.
umbral is the MAXIMUM AVAILABLE STOCK units, not output rows.
For 'máximo 4 unidades disponibles', umbral=4 and limite=10 unless a separate row limit is requested.
Examples:
Input: Ventas por sucursal del 2026-10-01 al 2026-10-31
Output: {{"tipo":"ventas_sucursal","fechaIni":"2026-10-01","fechaFin":"2026-10-31","nroSuc":null,"limite":10,"umbral":5,"mensaje":"Ventas por sucursal en octubre."}}
Input: Los 8 productos más vendidos del 2026-09-01 al 2026-09-30
Output: {{"tipo":"productos_vendidos","fechaIni":"2026-09-01","fechaFin":"2026-09-30","nroSuc":null,"limite":8,"umbral":5,"mensaje":"Ocho productos más vendidos."}}
Input: Evolución de ventas por semana del 2026-10-01 al 2026-10-31
Output: {{"tipo":"ventas_semana","fechaIni":"2026-10-01","fechaFin":"2026-10-31","nroSuc":null,"limite":10,"umbral":5,"mensaje":"Evolución semanal en octubre."}}
Input: Inventario con hasta 3 unidades disponibles
Output: {{"tipo":"existencias_bajas","fechaIni":null,"fechaFin":null,"nroSuc":null,"limite":10,"umbral":3,"mensaje":"Existencias disponibles menores o iguales a tres."}}
Input: Reporte de ventas
Output: {{"tipo":"aclaracion","fechaIni":null,"fechaFin":null,"nroSuc":null,"limite":10,"umbral":5,"mensaje":"Indica fechas inicial y final."}}
Input: Borra todas las ventas
Output: {{"tipo":"no_soportado","fechaIni":null,"fechaFin":null,"nroSuc":null,"limite":10,"umbral":5,"mensaje":"Solo consultas de los cuatro reportes permitidos."}}
"""
        try:
            output = _model.create_chat_completion(messages=[{'role':'system','content':prompt}, {'role':'user','content':text}],
                        temperature=0, max_tokens=250, response_format={'type':'json_object','schema':InterpreterPlan.model_json_schema()})
            raw = InterpreterPlan.model_validate_json(output['choices'][0]['message']['content'])
            if raw.tipo in ('aclaracion','no_soportado'):
                return {'estado':raw.tipo,'mensaje':raw.mensaje,'plan':None,'modelo':MODEL_NAME}
            if raw.tipo=='existencias_bajas':
                # A second, focused MODEL extraction avoids confusing inventory
                # units with report row count. No keyword parser substitutes IA.
                extracted=_model.create_chat_completion(messages=[{'role':'system','content':
                    'Extract TWO independent quantities from the Spanish request. JSON only. '
                    'stock_maximo is the maximum available inventory units per product. '
                    'cantidad_filas is how many products/rows to show. Missing quantities are null. '
                    'Examples: "máximo 4 unidades disponibles" => {"stock_maximo":4,"cantidad_filas":null}. '
                    '"Los 8 productos con hasta 2 unidades" => {"stock_maximo":2,"cantidad_filas":8}. '
                    '"Inventario bajo" => {"stock_maximo":null,"cantidad_filas":null}. '
                    'Do not infer row count from units.'},{'role':'user','content':text}],
                    temperature=0,max_tokens=80,response_format={'type':'json_object','schema':StockParameters.model_json_schema()})
                parameters=StockParameters.model_validate_json(extracted['choices'][0]['message']['content'])
                raw.umbral=parameters.stock_maximo if parameters.stock_maximo is not None else 5
                raw.limite=parameters.cantidad_filas if parameters.cantidad_filas is not None else 10
                raw.fechaIni=None;raw.fechaFin=None
                raw.mensaje=f'Existencias disponibles ≤ {raw.umbral}; hasta {raw.limite} filas. Confirma los parámetros.'
            plan = ReportPlan.model_validate(raw.model_dump(exclude={'mensaje'}))
            if plan.tipo!='existencias_bajas':
                # Grounding validation, not a replacement natural-language
                # parser: never accept model-invented dates for a vague request.
                explicit=re.findall(r'\b\d{4}-\d{2}-\d{2}\b',text)
                temporal=explicit or re.search(r'\b(hoy|ayer|enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre|20\d{2})\b|\b(esta semana|este mes|este año|últim[oa]s?\s+(?:\d+\s+)?(?:días?|semanas?|meses?))\b',text.lower())
                if not temporal or (len(explicit)==2 and [plan.fechaIni.isoformat(),plan.fechaFin.isoformat()]!=explicit):
                    return {'estado':'aclaracion','mensaje':'Indica y confirma las fechas inicial y final; no se aceptan fechas inventadas por el modelo.',
                            'plan':None,'modelo':MODEL_NAME}
            if plan.nroSuc is not None and plan.nroSuc not in {b['nro'] for b in branches}:
                return {'estado':'aclaracion','mensaje':'Selecciona una sucursal autorizada del listado.','plan':None,'modelo':MODEL_NAME}
            return {'estado':'listo','mensaje':raw.mensaje,'plan':plan.model_dump(mode='json'), 'modelo':MODEL_NAME,
                    'requiereConfirmacion':True}
        except (ValidationError, ValueError, KeyError, TypeError):
            return {'estado':'aclaracion','mensaje':'No se obtuvo un plan válido. Indica tipo de reporte, fechas y sucursal o usa el formulario.',
                    'plan':None,'modelo':MODEL_NAME}
    finally:
        _lock.release()
