"""Nombres de días en SQL; números ISO únicamente para controles y validación."""
import unicodedata

TODOS_DIAS = [1, 2, 3, 4, 5, 6, 7]
NOMBRES_DIAS = ("Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo")


def leer_dias(value):
    if not value:
        return TODOS_DIAS.copy()
    names = {name: i for i, name in enumerate(
        ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"), 1)}
    normalized = "".join(c for c in unicodedata.normalize("NFD", value.lower()) if not unicodedata.combining(c))
    tokens = normalized.replace(";", ",").split(",")
    days = [int(token.strip()) if token.strip().isdigit() else names.get(token.strip(), 0) for token in tokens]
    if not days or any(day not in TODOS_DIAS for day in days):
        raise ValueError("Días de atención inválidos en la base de datos")
    return sorted(set(days))


def guardar_dias(days):
    if not days or any(type(day) is not int or day not in TODOS_DIAS for day in days):
        raise ValueError("Días de atención inválidos")
    return ",".join(NOMBRES_DIAS[day - 1] for day in sorted(set(days)))
