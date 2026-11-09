"""CU17 - Motor de recomendación por contenido + comportamiento del cliente.

No hay servicio externo de IA, ni generación de texto, ni tablas nuevas: el
perfil se construye con las interacciones reales del cliente (compras pagadas,
reservas vigentes y carrito activo) y se puntúan las poleras disponibles del
catálogo de CU10.

Interpretación del negocio: la tienda vende únicamente poleras, por lo que
``categoria`` se lee siempre como MODELO/ESTILO de polera (Deportiva,
Semi-Formal, ...).

Todos los pesos y umbrales del algoritmo viven en este módulo como constantes
documentadas: no se dispersan números mágicos por el código.
"""

from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Literal

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

from app.modules.cliente_experiencia_compra.cu10_consultar_prendas.services.catalogo import (
    producto_resumen,
)
from app.modules.cliente_experiencia_compra.cu17_Recomendacion.repositories import (
    recomendacion as repository,
)
from app.modules.cliente_experiencia_compra.cu17_Recomendacion.schemas.recomendacion import (
    RecomendacionItem,
    RecomendacionesRespuesta,
)

# --- Modelo local (Qwen) para la variante basada en IA ---
MODEL_PATH_IA = Path(__file__).resolve().parents[5] / '.local/models/qwen2.5-1.5b-instruct-q4_k_m.gguf'
MODEL_SHA_IA = '6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e'
MODEL_NAME_IA = 'Qwen2.5-1.5B-Instruct Q4_K_M local'

_modelo_ia_singleton = None
_modelo_ia_lock = Lock()


class ModeloItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idProd: str
    motivo: str = Field(max_length=200)


class ModeloSeleccion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ModeloItem] = []
    mensaje: str = Field(max_length=300)

# --- Pesos del perfil (prioridad: compra > reserva > carrito) ---
PESO_COMPRA = 5    # compra pagada: la señal más fuerte
PESO_RESERVA = 3   # reserva vigente: interés declarado
PESO_CARRITO = 2   # carrito activo: interés inmediato

# --- Puntaje de recomendación (explicable) ---
SCORE_MODELO = 4         # coincide con el modelo de polera preferido (categoria)
SCORE_MARCA = 3          # coincide con la marca frecuente
SCORE_TEMPORADA = 2      # coincide con la temporada frecuente
SCORE_PRECIO = 2         # precio dentro del rango habitual
SCORE_POPULARIDAD = 2    # polera popular en ventas pagadas
PENALIZACION_COMPRADO = -3   # ya la compró: se privilegia algo nuevo

# --- Reglas auxiliares ---
PRECIO_TOLERANCIA_CERCANA = Decimal("0.40")  # ±40% de la media ponderada
PRECIO_TOLERANCIA_AMPLIA = Decimal("0.80")   # ±80%: influencia débil, nunca excluye
POPULARIDAD_TOP = 3                          # las 3 más vendidas reciben el puntaje completo

# --- Límites del endpoint ---
RECOMENDACIONES_POR_DEFECTO = 8
RECOMENDACIONES_MINIMAS = 1
RECOMENDACIONES_MAXIMAS = 20
CANDIDATOS_MAXIMOS = 60  # techo de candidatos analizados (no se trae todo el catálogo)

# --- Mensajes generales (nunca exponen perfil ni datos de otras personas) ---
RAZON_MODELO = "Similar a los modelos de polera que prefieres"
RAZON_MARCA = "Marca frecuente en tus interacciones"
RAZON_TEMPORADA = "Temporada que sueles elegir"
RAZON_PRECIO = "Precio dentro de tu rango habitual"
RAZON_POPULAR = "Popular entre los clientes"
RAZON_DISPONIBLE = "Disponible en el catálogo"

MENSAJE_PERSONALIZADO = ("Seleccionamos estas poleras según tus compras, reservas y "
                         "productos de interés.")
MENSAJE_GENERAL = ("Todavía estamos conociendo tus gustos. Estas son algunas poleras "
                   "populares que podrían interesarte.")


def _senales(db, user_id):
    """Interacciones propias agrupadas por fuente (3 consultas constantes)."""
    return (
        (PESO_COMPRA, repository.compras_pagadas(db, user_id)),
        (PESO_RESERVA, repository.reservas_vigentes(db, user_id)),
        (PESO_CARRITO, repository.carrito_activo(db, user_id)),
    )


def _temporadas_ponderadas(db, colecciones):
    """Reparte el peso de cada colección entre las temporadas que la componen."""
    vinculadas = repository.temporadas_por_coleccion(db, list(colecciones))
    ponderadas: dict[int, float] = defaultdict(float)
    for idcol, senal in colecciones.items():
        temporadas = vinculadas.get(idcol, [])
        if not temporadas:
            continue
        reparto = senal / len(temporadas)
        for idtemp, _nombre in temporadas:
            ponderadas[idtemp] += reparto
    return dict(ponderadas)


def _perfil(db, user_id):
    """Perfil ponderado del cliente autenticado (solo con sus propios datos)."""
    compras, reservas, carrito = _senales(db, user_id)
    modelos: dict[int, float] = defaultdict(float)
    marcas: dict[int, float] = defaultdict(float)
    colecciones: dict[int, float] = defaultdict(float)
    peso_total = 0.0
    acumulado = Decimal("0")
    peso_precio = Decimal("0")
    for peso, filas in (compras, reservas, carrito):
        for fila in filas:
            senal = float(peso * int(fila.cantidad or 1))
            peso_total += senal
            modelos[fila.idcat] += senal
            marcas[fila.idmarca] += senal
            colecciones[fila.idcol] += senal
            acumulado += Decimal(str(fila.precio)) * Decimal(str(senal))
            peso_precio += Decimal(str(senal))
    return {
        "peso_total": peso_total,
        "modelos": dict(modelos),
        "marcas": dict(marcas),
        "temporadas": _temporadas_ponderadas(db, colecciones),
        "precio_medio": (acumulado / peso_precio) if peso_precio else None,
        "comprados": {fila.idprod for fila in compras[1]},
        "en_carrito": {fila.idprod for fila in carrito[1]},
    }


def _aporte(preferencias, clave, peso):
    """Aporte proporcional a la preferencia (1 mínimo si la característica aparece)."""
    valor = preferencias.get(clave, 0)
    total = sum(preferencias.values())
    if not valor or not total:
        return 0
    return max(1, int(peso * valor / total + 0.5))


def _aporte_precio(precio, media):
    """El precio influye en el score; fuera del rango amplio solo deja de sumar."""
    if precio is None or media is None or media <= 0:
        return 0
    desvio = abs(Decimal(str(precio)) - media) / media
    if desvio <= PRECIO_TOLERANCIA_CERCANA:
        return SCORE_PRECIO
    if desvio <= PRECIO_TOLERANCIA_AMPLIA:
        return 1
    return 0


def _aporte_popularidad(idprod, unidades, populares):
    """Popularidad agregada (ventas pagadas): nunca expone datos de terceros."""
    vendidas = unidades.get(idprod, 0)
    if not vendidas:
        return 0
    return SCORE_POPULARIDAD if idprod in populares else 1


def _populares(unidades):
    """Las ``POPULARIDAD_TOP`` poleras más vendidas (desempate determinista)."""
    ordenadas = sorted(unidades.items(), key=lambda par: (-par[1], par[0]))
    return {idprod for idprod, vendidas in ordenadas[:POPULARIDAD_TOP] if vendidas > 0}


def _puntuar(product, precio, perfil, temporadas_candidato, unidades, populares):
    """Devuelve (score, aporte_personalizado, razones) de un candidato."""
    razones = []
    modelo = _aporte(perfil["modelos"], product.idcat, SCORE_MODELO)
    marca = _aporte(perfil["marcas"], product.idmarca, SCORE_MARCA)
    temporada = max((_aporte(perfil["temporadas"], idtemp, SCORE_TEMPORADA)
                     for idtemp, _nombre in temporadas_candidato), default=0)
    precio_aporte = _aporte_precio(precio, perfil["precio_medio"])
    popularidad = _aporte_popularidad(product.idprod, unidades, populares)
    if modelo:
        razones.append(RAZON_MODELO)
    if marca:
        razones.append(RAZON_MARCA)
    if temporada:
        razones.append(RAZON_TEMPORADA)
    if precio_aporte:
        razones.append(RAZON_PRECIO)
    if popularidad:
        razones.append(RAZON_POPULAR)
    if not razones:
        razones.append(RAZON_DISPONIBLE)
    personalizado = modelo + marca + temporada + precio_aporte
    penalizacion = PENALIZACION_COMPRADO if product.idprod in perfil["comprados"] else 0
    return personalizado + popularidad + penalizacion, personalizado, razones


def recomendar(db, user_id, limit=RECOMENDACIONES_POR_DEFECTO):
    """Recomendaciones del cliente autenticado, con fallback general sin perfil."""
    candidatos = repository.candidatos_disponibles(db, CANDIDATOS_MAXIMOS)
    if not candidatos:
        return RecomendacionesRespuesta(tipo="general", mensaje=MENSAJE_GENERAL,
                                        total=0, items=[])

    product_ids: list[str] = []
    colecciones: set[int] = set()
    for product, _marca, _categoria, coleccion, _promo in candidatos:
        product_ids.append(product.idprod)
        colecciones.add(coleccion.idcol)

    variantes: dict[str, list] = defaultdict(list)
    for variante in repository.variantes_activas(db, product_ids):
        variantes[variante.idprod].append(variante)
    disponibles = repository.disponibilidad_por_producto(db, product_ids)
    unidades = repository.popularidad(db, product_ids)
    populares = _populares(unidades)
    temporadas_por_col = repository.temporadas_por_coleccion(db, list(colecciones))
    perfil = _perfil(db, user_id)

    puntuados: list[tuple[RecomendacionItem, int, int]] = []
    for product, marca, categoria, coleccion, promo in candidatos:
        if product.idprod in perfil["en_carrito"]:
            # Ya está en el carrito: no se recomienda lo que el cliente ya eligió.
            continue
        product_variants = variantes.get(product.idprod, [])
        precios = [variante.precio for variante in product_variants]
        score, personalizado, razones = _puntuar(
            product, min(precios) if precios else None, perfil,
            temporadas_por_col.get(coleccion.idcol, ()), unidades, populares)
        resumen = producto_resumen(product, marca, categoria, coleccion, promo,
                                   product_variants,
                                   disponible=disponibles.get(product.idprod, 0) > 0)
        item = RecomendacionItem.model_validate(
            {**resumen.model_dump(), "score": score, "razones": razones})
        puntuados.append((item, personalizado, unidades.get(product.idprod, 0)))

    # Orden estable: score DESC, unidades pagadas DESC, nombre ASC, idProd ASC.
    puntuados.sort(key=lambda entrada: (-entrada[0].score, -entrada[2],
                                        entrada[0].descripcion, entrada[0].idProd))
    personales = [entrada for entrada in puntuados if entrada[1] > 0]
    if perfil["peso_total"] > 0 and personales:
        seleccion = personales[:limit]
        if len(seleccion) < limit:
            elegidos = {entrada[0].idProd for entrada in seleccion}
            for entrada in puntuados:
                if len(seleccion) >= limit:
                    break
                if entrada[0].idProd not in elegidos:
                    seleccion.append(entrada)
        tipo, mensaje = "personalizada", MENSAJE_PERSONALIZADO
    else:
        # Cliente nuevo o sin señales suficientes: recomendaciones generales.
        seleccion = puntuados[:limit]
        tipo, mensaje = "general", MENSAJE_GENERAL

    items = [entrada[0] for entrada in seleccion]
    return RecomendacionesRespuesta(tipo=tipo, mensaje=mensaje, total=len(items), items=items)


def _cargar_modelo_ia():
    """Carga el modelo local Qwen una sola vez (mismo patrón que CU25 reportes).

    Devuelve None si no hay archivo, el hash no coincide o no se puede cargar:
    entonces el llamador usa el respaldo heurístico sin fallar la petición.
    """
    global _modelo_ia_singleton
    if _modelo_ia_singleton is not None:
        return _modelo_ia_singleton
    with _modelo_ia_lock:
        if _modelo_ia_singleton is not None:
            return _modelo_ia_singleton
        if not MODEL_PATH_IA.is_file():
            return None
        with MODEL_PATH_IA.open('rb') as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() != MODEL_SHA_IA:
                return None
        try:
            from llama_cpp import Llama
            _modelo_ia_singleton = Llama(model_path=str(MODEL_PATH_IA), n_ctx=2048,
                                         n_threads=4, verbose=False, seed=42)
        except (ImportError, OSError, ValueError):
            return None
    return _modelo_ia_singleton


def _extraer_ids_de_texto(texto, validos, limite):
    """Extrae idProd válidos del texto libre del modelo.

    Busca patrones tipo "idProd": "MAN-P1" o menciones directas de IDs
    de candidatos. Nunca devuelve IDs que no estén en ``validos``.
    """
    import re
    encontrados = []
    # 1) Patrón JSON: "idProd": "XXX"
    for m in re.finditer(r'"idProd"\s*:\s*"([^"]+)"', texto):
        cid = m.group(1).strip()
        if cid in validos and cid not in encontrados:
            encontrados.append(cid)
    # 2) Menciones directas de IDs candidatos en el texto
    for cid in sorted(validos, key=len, reverse=True):
        if cid in texto and cid not in encontrados:
            encontrados.append(cid)
    return encontrados[:limite]


def _seleccion_con_modelo(modelo, perfil, resumenes, limit):
    """Pide al modelo local que elija entre candidatos ya verificados.

    Nunca usa datos de otros clientes ni empresas. Solo acepta idProd que
    existen en la lista de candidatos; el precio, stock y descripciones se
    toman del catálogo real, nunca del modelo.
    """
    candidatos_prompt = [
        {"idProd": r.idProd, "descripcion": r.descripcion,
         "categoria": r.categoria.descripcion, "marca": r.marca.nombre,
         "precio": float(r.precioMin or 0)}
        for r in resumenes
    ]
    preferencias = []
    if perfil["modelos"]:
        top_cats = sorted(perfil["modelos"], key=perfil["modelos"].get, reverse=True)[:3]
        preferencias.append("categorías: " + ", ".join(str(c) for c in top_cats))
    if perfil["marcas"]:
        top_marcas = sorted(perfil["marcas"], key=perfil["marcas"].get, reverse=True)[:3]
        preferencias.append("marcas: " + ", ".join(str(m) for m in top_marcas))
    if perfil["precio_medio"] is not None:
        preferencias.append(f"precio habitual alrededor de {float(perfil['precio_medio']):.2f}")
    prefs_texto = "; ".join(preferencias) if preferencias else "sin historial de compras aún"
    validos = {r.idProd for r in resumenes}
    prompt = (
        "Eres un recomendador de ropa para una tienda de poleras. Responde SOLO con un objeto JSON.\n"
        f"Preferencias del cliente (basadas en su propio historial): {prefs_texto}.\n"
        f"Candidatos disponibles (solo estos idProd son válidos): "
        f"{json.dumps(candidatos_prompt, ensure_ascii=False)}.\n"
        f"Selecciona hasta {limit} poleras que mejor coincidan con las preferencias del cliente.\n"
        "Devuelve {\"items\": [{\"idProd\": \"...\", \"motivo\": \"...\"}], \"mensaje\": \"...\"}.\n"
        "No inventes idProd que no estén en la lista de candidatos. No repitas idProd.\n"
    )
    try:
        output = modelo.create_chat_completion(
            messages=[{"role": "system", "content": prompt},
                      {"role": "user", "content": "Selecciona las recomendaciones."}],
            temperature=0.2, max_tokens=400,
            response_format={"type": "json_object"})
        texto = output["choices"][0]["message"]["content"]
    except Exception:
        return None
    # Intentar parseo estructurado primero.
    seleccion = None
    try:
        seleccion = ModeloSeleccion.model_validate_json(texto)
    except Exception:
        seleccion = None
    if seleccion is not None:
        ids_modelo = [it.idProd for it in seleccion.items if it.idProd in validos]
    else:
        ids_modelo = _extraer_ids_de_texto(texto, validos, limit)
    # Deduplicar preservando orden.
    vistos = set()
    ids_validos = []
    for cid in ids_modelo:
        if cid not in vistos:
            vistos.add(cid)
            ids_validos.append(cid)
    if not ids_validos:
        return None
    ids_validos = ids_validos[:limit]
    resumen_por_id = {r.idProd: r for r in resumenes}
    motivos = {}
    if seleccion is not None:
        for it in seleccion.items:
            if it.idProd in ids_validos and it.motivo:
                motivos[it.idProd] = it.motivo
    mensaje = seleccion.mensaje if (seleccion is not None and seleccion.mensaje) else "Selección basada en tus preferencias."
    items = []
    for idx, cid in enumerate(ids_validos):
        resumen = resumen_por_id[cid]
        score = max(1, len(ids_validos) - idx)
        item = RecomendacionItem.model_validate(
            {**resumen.model_dump(), "score": score,
             "razones": [motivos.get(cid)] if motivos.get(cid)
             else ["Disponible en el catálogo"]})
        items.append(item)
    tiene_perfil = perfil["peso_total"] > 0
    tipo = "personalizada" if tiene_perfil else "general"
    return RecomendacionesRespuesta(tipo=tipo,
                                    mensaje=mensaje,
                                    total=len(items), items=items, origen="modelo")


def recomendar_ia(db, user_id, limit=RECOMENDACIONES_POR_DEFECTO):
    """Recomendación basada en el modelo local Qwen con respaldo heurístico.

    El origen se expone explícitamente: 'modelo' cuando la selección proviene
    del modelo y se validó contra el catálogo real; 'heurístico' cuando se usa
    el recomendador de reglas como respaldo. Nunca se mezclan ni se ocultan.
    """
    candidatos = repository.candidatos_disponibles(db, CANDIDATOS_MAXIMOS)
    if not candidatos:
        return RecomendacionesRespuesta(tipo="general", mensaje=MENSAJE_GENERAL,
                                        total=0, items=[], origen="heuristico")
    product_ids = [p.idprod for p, _, _, _, _ in candidatos]
    variantes = repository.variantes_activas(db, product_ids)
    disponibles = repository.disponibilidad_por_producto(db, product_ids)
    colecciones = {c.idcol for _, _, _, c, _ in candidatos}
    temporadas_por_col = repository.temporadas_por_coleccion(db, list(colecciones))
    perfil = _perfil(db, user_id)
    # variantes_activas puede devolver lista en PG; normalizar a dict por idprod.
    variantes_raw = repository.variantes_activas(db, product_ids)
    if isinstance(variantes_raw, dict):
        variantes = variantes_raw
    else:
        variantes = {}
        for v in variantes_raw:
            clave = getattr(v, "idprod", None) or getattr(v, "idVariante", None)
            if clave is not None:
                variantes.setdefault(clave, []).append(v)
    resumenes = []
    for product, marca, categoria, coleccion, promo in candidatos:
        product_variants = variantes.get(product.idprod, [])
        resumen = producto_resumen(product, marca, categoria, coleccion, promo,
                                   product_variants,
                                   disponible=disponibles.get(product.idprod, 0) > 0)
        resumenes.append(resumen)
    modelo = _cargar_modelo_ia()
    if modelo is not None:
        try:
            seleccion = _seleccion_con_modelo(modelo, perfil, resumenes, limit)
            if seleccion is not None:
                return seleccion
        except Exception:
            # El modelo falló: responder con el heurístico, sin mezclar orígenes.
            pass
    # Respaldo heurístico claramente diferenciado del modelo.
    return recomendar(db, user_id, limit)
