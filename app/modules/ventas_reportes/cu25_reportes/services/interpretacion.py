"""Optional real local LLM. Its output is an untrusted proposal, never SQL."""
import calendar
import hashlib
import json
import re
from datetime import date, datetime, timedelta
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

_MONTHS = {
    'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4, 'mayo': 5, 'junio': 6,
    'julio': 7, 'agosto': 8, 'septiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12,
}


class _FallbackLocalInterpreter:
    """Intérprete local determinista cuando el archivo GGUF no está descargado."""

    def create_chat_completion(self, *, messages, response_format, **kwargs):
        assert response_format and response_format.get('schema')
        system_text = messages[0]['content']
        user_text = messages[-1]['content']
        lower = user_text.lower()
        if 'stock_maximo' in system_text:
            stock_match = re.search(
                r'(?:hasta|m[aá]ximo|menor(?:es)?\s+o\s+igual(?:es)?\s+a|menor(?:es)?\s+a|<=?)\s*(\d+)\s*(?:unidades|disponibles|stock)?'
                r'|(\d+)\s*unidades\s*disponibles',
                lower,
            )
            stock_max = int(stock_match.group(1) or stock_match.group(2)) if stock_match else None
            row_match = re.search(r'(?:los|las|primeros|primeras|top|mostrar|ver)\s+(\d+)\s+(?:productos|filas|prendas)', lower)
            filas = int(row_match.group(1)) if row_match else None
            return {'choices': [{'message': {'content': json.dumps({
                'stock_maximo': stock_max, 'cantidad_filas': filas,
            })}}]}

        today_match = re.search(r'Today:\s*(\d{4}-\d{2}-\d{2})', system_text)
        today = date.fromisoformat(today_match.group(1)) if today_match else datetime.now(ZoneInfo('America/La_Paz')).date()
        branches_match = re.search(r'Authorized branches:\s*(\[.*?\])\.', system_text)
        branches = json.loads(branches_match.group(1)) if branches_match else []

        if re.search(r'\b(borra|borrar|elimina|eliminar|actualiza|modifica|inserta|drop|delete|update|insert|contrase[ñn]a|sql)\b', lower):
            return self._reply('no_soportado', None, None, None, 10, 5,
                               'Solo se permiten consultas de lectura sobre los cuatro reportes autorizados.')

        nro_suc = None
        for b in branches:
            b_name = str(b.get('nombre', '')).strip().lower()
            if b_name and b_name in lower:
                nro_suc = b['nro']
                break
        if nro_suc is None:
            suc_num = re.search(r'sucursal\s+#?(\d+)', lower)
            if suc_num:
                nro_suc = int(suc_num.group(1))

        row_match = re.search(r'(?:los|las|top|primeros|primeras|l[ií]mite)\s+(\d+)', lower)
        limite = max(1, min(100, int(row_match.group(1)))) if row_match else 10

        if re.search(r'\b(existencia|existencias|stock|inventario|agotad|disponible|disponibles)\b', lower):
            return self._reply('existencias_bajas', None, None, nro_suc, limite, 5,
                               'Existencias bajas por sucursal.')

        if re.search(r'\b(semana|semanal|semanas|evoluci[oó]n)\b', lower):
            tipo = 'ventas_semana'
            label = 'Evolución semanal de ventas'
        elif re.search(r'\b(productos?\s+m[aá]s\s+vendid|m[aá]s\s+vendid|top\s+\d*\s*productos?|prendas?\s+m[aá]s\s+vendid)\b', lower):
            tipo = 'productos_vendidos'
            label = 'Productos más vendidos'
        elif re.search(r'\b(venta|ventas|sucursal|facturaci[oó]n|ingresos)\b', lower):
            tipo = 'ventas_sucursal'
            label = 'Ventas por sucursal'
        else:
            return self._reply('aclaracion', None, None, None, 10, 5,
                               'Indica el tipo de reporte (ventas por sucursal, productos más vendidos, evolución semanal o existencias bajas).')

        ini, fin = self._extract_dates(user_text, lower, today)
        if ini is None or fin is None:
            return self._reply('aclaracion', None, None, nro_suc, limite, 5,
                               'Indica las fechas inicial y final del período que deseas consultar.')
        return self._reply(tipo, ini.isoformat(), fin.isoformat(), nro_suc, limite, 5,
                           f'{label} del {ini.isoformat()} al {fin.isoformat()}.')

    @staticmethod
    def _extract_dates(text: str, lower: str, today: date) -> tuple[date | None, date | None]:
        explicit = re.findall(r'\b(\d{4}-\d{2}-\d{2})\b', text)
        if len(explicit) >= 2:
            return date.fromisoformat(explicit[0]), date.fromisoformat(explicit[1])
        if len(explicit) == 1:
            single = date.fromisoformat(explicit[0])
            return single, single
        if 'hoy' in lower:
            return today, today
        if 'ayer' in lower:
            yesterday = today - timedelta(days=1)
            return yesterday, yesterday
        if 'esta semana' in lower:
            start = today - timedelta(days=today.weekday())
            return start, today
        if 'este mes' in lower:
            return date(today.year, today.month, 1), today
        if 'este año' in lower or 'este anio' in lower:
            return date(today.year, 1, 1), today
        rel_days = re.search(r'[uú]ltim[oa]s?\s+(\d+)\s+d[ií]as?', lower)
        if rel_days:
            days = max(1, min(366, int(rel_days.group(1))))
            return today - timedelta(days=days), today
        if re.search(r'[uú]ltim[oa]s?\s+semana', lower):
            return today - timedelta(days=7), today
        if re.search(r'[uú]ltim[oa]s?\s+mes', lower):
            return today - timedelta(days=30), today
        for name, month_num in _MONTHS.items():
            if re.search(rf'\b{name}\b', lower):
                year_match = re.search(r'\b(20\d{2})\b', lower)
                year = int(year_match.group(1)) if year_match else today.year
                last_day = calendar.monthrange(year, month_num)[1]
                return date(year, month_num, 1), date(year, month_num, last_day)
        year_only = re.search(r'\b(20\d{2})\b', lower)
        if year_only:
            year = int(year_only.group(1))
            return date(year, 1, 1), date(year, 12, 31)
        return None, None

    @staticmethod
    def _reply(tipo, ini, fin, nro_suc, limite, umbral, mensaje):
        return {'choices': [{'message': {'content': json.dumps({
            'tipo': tipo, 'fechaIni': ini, 'fechaFin': fin,
            'nroSuc': nro_suc, 'limite': limite, 'umbral': umbral, 'mensaje': mensaje,
        })}}]}


def interpret(text, branches, *, enabled=False):
    if not enabled:
        raise DomainError(503,'modelo_ia_no_disponible','Interpretación IA local desactivada; usa los reportes estructurados')
    if not _lock.acquire(blocking=False):
        raise DomainError(503,'modelo_ia_ocupado','El modelo local está ocupado; inténtalo nuevamente')
    try:
        global _model
        if _model is None:
            if not MODEL_PATH.is_file():
                _model = _FallbackLocalInterpreter()
            else:
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
