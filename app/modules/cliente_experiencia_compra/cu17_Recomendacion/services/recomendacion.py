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
