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


_WORD_NUMBERS = {
    'un': 1, 'uno': 1, 'una': 1, 'dos': 2, 'tres': 3, 'cuatro': 4,
    'cinco': 5, 'seis': 6, 'siete': 7, 'ocho': 8, 'nueve': 9,
    'diez': 10, 'once': 11, 'doce': 12,
}


def _parse_quantity(token: str | None, default: int = 1) -> int:
    if not token:
        return default
    cleaned = token.strip().lower()
    if cleaned.isdigit():
        return int(cleaned)
    return _WORD_NUMBERS.get(cleaned, default)


def _subtract_months(ref: date, months: int) -> date:
    total = ref.year * 12 + (ref.month - 1) - months
    year, month_zero = divmod(total, 12)
    month = month_zero + 1
    day = min(ref.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _extract_dates(text: str, lower: str, today: date) -> tuple[date | None, date | None]:
    explicit = re.findall(r'\b(\d{4}-\d{2}-\d{2})\b', text)
    if len(explicit) >= 2:
        return date.fromisoformat(explicit[0]), date.fromisoformat(explicit[1])
    if len(explicit) == 1:
        single = date.fromisoformat(explicit[0])
        return single, single

    slash_dates = re.findall(r'\b(\d{2})/(\d{2})/(20\d{2})\b', text)
    if len(slash_dates) >= 2:
        d1 = date(int(slash_dates[0][2]), int(slash_dates[0][1]), int(slash_dates[0][0]))
        d2 = date(int(slash_dates[1][2]), int(slash_dates[1][1]), int(slash_dates[1][0]))
        return d1, d2
    if len(slash_dates) == 1:
        d1 = date(int(slash_dates[0][2]), int(slash_dates[0][1]), int(slash_dates[0][0]))
        return d1, d1

    if re.search(r'\bhoy\b', lower):
        return today, today
    if re.search(r'\bayer\b', lower):
        yesterday = today - timedelta(days=1)
        return yesterday, yesterday
    if re.search(r'\besta\s+semana\b', lower):
        return today - timedelta(days=today.weekday()), today
    if re.search(r'\bsemana\s+pasada\b|\bsemana\s+anterior\b', lower):
        this_monday = today - timedelta(days=today.weekday())
        prev_sunday = this_monday - timedelta(days=1)
        return prev_sunday - timedelta(days=6), prev_sunday
    if re.search(r'\b(?:este\s+mes|mes\s+actual)\b', lower):
        return date(today.year, today.month, 1), today
    if re.search(r'\b(?:mes\s+pasado|mes\s+anterior)\b', lower):
        first_this = date(today.year, today.month, 1)
        last_prev = first_this - timedelta(days=1)
        return date(last_prev.year, last_prev.month, 1), last_prev
    if re.search(r'\beste\s+a[ñn]o\b|\ba[ñn]o\s+actual\b', lower):
        return date(today.year, 1, 1), today
    if re.search(r'\ba[ñn]o\s+pasado\b|\ba[ñn]o\s+anterior\b', lower):
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)

    qty_pat = r'(\d+|un[oa]?|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce)'
    rel_months = re.search(rf'(?:[uú]ltim[oa]s?|hace|durante\s+los?\s+[uú]ltim[oa]s?|de\s+los?\s+[uú]ltim[oa]s?)\s+{qty_pat}\s+mes(?:es)?\b', lower)
    if rel_months:
        months = max(1, _parse_quantity(rel_months.group(1), 1))
        return _subtract_months(today, months), today
    if re.search(r'\b[uú]ltim[oa]\s+mes\b', lower):
        return _subtract_months(today, 1), today
    if re.search(r'\b(?:[uú]ltim[oa]|este)\s+trimestre\b', lower):
        return _subtract_months(today, 3), today
    if re.search(r'\b(?:[uú]ltim[oa]|este)\s+semestre\b', lower):
        return _subtract_months(today, 6), today

    rel_weeks = re.search(rf'(?:[uú]ltim[oa]s?|hace)\s+{qty_pat}\s+semanas?\b', lower)
    if rel_weeks:
        weeks = max(1, min(52, _parse_quantity(rel_weeks.group(1), 1)))
        return today - timedelta(days=weeks * 7), today
    if re.search(r'\b[uú]ltim[oa]\s+semana\b', lower):
        return today - timedelta(days=7), today

    rel_days = re.search(rf'(?:[uú]ltim[oa]s?|hace)\s+{qty_pat}\s+d[ií]as?\b', lower)
    if rel_days:
        days = max(1, min(366, _parse_quantity(rel_days.group(1), 1)))
        return today - timedelta(days=days), today

    rel_years = re.search(rf'(?:[uú]ltim[oa]s?|hace)\s+{qty_pat}\s+a[ñn]os?\b', lower)
    if rel_years:
        years = max(1, _parse_quantity(rel_years.group(1), 1))
        return _subtract_months(today, years * 12), today
    if re.search(r'\b[uú]ltim[oa]\s+a[ñn]o\b', lower):
        return _subtract_months(today, 12), today

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
        elif re.search(r'\b(productos?\s+m[aá]s\s+vendid|m[aá]s\s+vendid|top\s+\d*\s*productos?|prendas?\s+m[aá]s\s+vendid|cada\s+prenda|prendas?\s+vendid|productos?\s+vendid)\b', lower):
            tipo = 'productos_vendidos'
            label = 'Productos más vendidos'
        elif re.search(r'\b(venta|ventas|sucursal|facturaci[oó]n|ingresos)\b', lower):
            tipo = 'ventas_sucursal'
            label = 'Ventas por sucursal'
        else:
            return self._reply('aclaracion', None, None, None, 10, 5,
                               'Indica el tipo de reporte (ventas por sucursal, productos más vendidos, evolución semanal o existencias bajas).')

        ini, fin = _extract_dates(user_text, lower, today)
        if ini is None or fin is None:
            return self._reply('aclaracion', None, None, nro_suc, limite, 5,
                               'Indica en la solicitud el período que deseas consultar (por ejemplo: "últimos 3 meses", "este mes", "mes pasado" o fechas exactas).')
        if (fin - ini).days > 366:
            return self._reply('aclaracion', None, None, nro_suc, limite, 5,
                               'El período máximo permitido para reportes es de 12 meses (366 días).')
        return self._reply(tipo, ini.isoformat(), fin.isoformat(), nro_suc, limite, 5,
                           f'{label} del {ini.isoformat()} al {fin.isoformat()}.')

    @staticmethod
    def _extract_dates(text: str, lower: str, today: date) -> tuple[date | None, date | None]:
        return _extract_dates(text, lower, today)

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
        today_date = datetime.now(ZoneInfo('America/La_Paz')).date()
        today = today_date.isoformat()
        explicit = re.findall(r'\b\d{4}-\d{2}-\d{2}\b', text)
        resolved_ini, resolved_fin = _extract_dates(text, text.lower(), today_date)
        if resolved_ini is not None and resolved_fin is not None and (resolved_fin - resolved_ini).days > 366:
            return {'estado': 'aclaracion',
                    'mensaje': 'El período máximo permitido para reportes es de 12 meses (366 días).',
                    'plan': None, 'modelo': MODEL_NAME}
        date_guidance = (
            f'Pre-resolved period from the request: fechaIni="{resolved_ini.isoformat()}", fechaFin="{resolved_fin.isoformat()}". '
            'Use these exact dates for sales reports and do NOT return aclaracion asking for dates.'
            if resolved_ini is not None and resolved_fin is not None
            else 'If the user specifies a relative period (e.g. últimos 3 meses, este mes, mes pasado, hoy), compute fechaIni and fechaFin from Today.'
        )
        prompt = f"""Translate a Spanish FashionStore report request to JSON. Do not answer the report, only extract its parameters.
Today: {today}. No SQL, company, user, data changes or secrets are allowed.
Types: ventas_sucursal=sales by branch; productos_vendidos=top selling products or garments sold;
ventas_semana=weekly sales; existencias_bajas=current low inventory;
aclaracion=missing report or dates; no_soportado=other requests.
Sales need dates. Never invent dates. Low inventory uses null dates.
{date_guidance}
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
Input: Prendas vendidas durante los últimos 3 meses
Output: {{"tipo":"productos_vendidos","fechaIni":"2026-08-15","fechaFin":"2026-11-15","nroSuc":null,"limite":10,"umbral":5,"mensaje":"Prendas vendidas en los últimos 3 meses."}}
Input: Evolución de ventas por semana del 2026-10-01 al 2026-10-31
Output: {{"tipo":"ventas_semana","fechaIni":"2026-10-01","fechaFin":"2026-10-31","nroSuc":null,"limite":10,"umbral":5,"mensaje":"Evolución semanal en octubre."}}
Input: Inventario con hasta 3 unidades disponibles
Output: {{"tipo":"existencias_bajas","fechaIni":null,"fechaFin":null,"nroSuc":null,"limite":10,"umbral":3,"mensaje":"Existencias disponibles menores o iguales a tres."}}
Input: Reporte de ventas
Output: {{"tipo":"aclaracion","fechaIni":null,"fechaFin":null,"nroSuc":null,"limite":10,"umbral":5,"mensaje":"Indica en tu solicitud el período que deseas consultar (ej.: 'últimos 3 meses', 'este mes' o fechas exactas)."}}
Input: Borra todas las ventas
Output: {{"tipo":"no_soportado","fechaIni":null,"fechaFin":null,"nroSuc":null,"limite":10,"umbral":5,"mensaje":"Solo consultas de los cuatro reportes permitidos."}}
"""
        try:
            output = _model.create_chat_completion(messages=[{'role':'system','content':prompt}, {'role':'user','content':text}],
                        temperature=0, max_tokens=250, response_format={'type':'json_object','schema':InterpreterPlan.model_json_schema()})
            raw = InterpreterPlan.model_validate_json(output['choices'][0]['message']['content'])
            if raw.tipo == 'aclaracion' and resolved_ini is not None and resolved_fin is not None:
                # If relative dates were already resolved from the user's text, re-prompt
                # the LLM strictly for report classification so Qwen does not refuse on dates.
                retry_prompt = (
                    f'Dates are ALREADY resolved: fechaIni="{resolved_ini.isoformat()}", fechaFin="{resolved_fin.isoformat()}". '
                    'Classify the Spanish report request into tipo: ventas_sucursal, productos_vendidos, ventas_semana, existencias_bajas, or no_soportado. '
                    f'Authorized branches: {json.dumps(branches, ensure_ascii=False)}. Use the resolved dates for sales reports.'
                )
                retry_output = _model.create_chat_completion(
                    messages=[{'role': 'system', 'content': retry_prompt}, {'role': 'user', 'content': text}],
                    temperature=0, max_tokens=250, response_format={'type': 'json_object', 'schema': InterpreterPlan.model_json_schema()})
                raw = InterpreterPlan.model_validate_json(retry_output['choices'][0]['message']['content'])
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
            elif resolved_ini is not None and resolved_fin is not None and len(explicit) != 2:
                raw.fechaIni = resolved_ini
                raw.fechaFin = resolved_fin
            plan = ReportPlan.model_validate(raw.model_dump(exclude={'mensaje'}))
            if plan.tipo!='existencias_bajas':
                # Grounding validation, not a replacement natural-language
                # parser: never accept model-invented dates for a vague request.
                temporal = resolved_ini is not None or bool(explicit) or re.search(r'\b(hoy|ayer|enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre|20\d{2})\b|\b(esta semana|semana pasada|este mes|mes pasado|este año|año pasado|[uú]ltim[oa]s?\s+(?:\w+\s+)?(?:d[ií]as?|semanas?|mes(?:es)?|a[ñn]os?))\b',text.lower())
                if not temporal or (len(explicit)==2 and [plan.fechaIni.isoformat(),plan.fechaFin.isoformat()]!=explicit):
                    return {'estado':'aclaracion','mensaje':'Indica en tu solicitud el período que deseas consultar (ej.: "últimos 3 meses", "este mes", "mes pasado" o "del YYYY-MM-DD al YYYY-MM-DD").',
                            'plan':None,'modelo':MODEL_NAME}
            if plan.nroSuc is not None and plan.nroSuc not in {b['nro'] for b in branches}:
                return {'estado':'aclaracion','mensaje':'Selecciona una sucursal autorizada del listado.','plan':None,'modelo':MODEL_NAME}
            return {'estado':'listo','mensaje':raw.mensaje,'plan':plan.model_dump(mode='json'), 'modelo':MODEL_NAME,
                    'requiereConfirmacion':True}
        except (ValidationError, ValueError, KeyError, TypeError):
            return {'estado':'aclaracion','mensaje':'No se obtuvo un plan válido. Indica en tu mensaje el tipo de reporte y período (ej.: "ventas últimos 3 meses").',
                    'plan':None,'modelo':MODEL_NAME}
    finally:
        _lock.release()
