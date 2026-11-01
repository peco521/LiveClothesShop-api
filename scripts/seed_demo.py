"""Carga reproducible de las cinco empresas de demostración de FashionStore.

Uso (desde LiveClothesShop-api):

    .venv/Scripts/python.exe scripts/seed_demo.py
    .venv/Scripts/python.exe scripts/seed_demo.py --solo manaco lupo

Qué hace:
  * Aprovisiona (si faltan) las empresas manaco, kosy-pantalones, urbanwomen,
    kosy-prendas y lupo mediante `sp_registrar_empresa`.
  * Completa roles, permisos por rol, sucursales, horarios, catálogo, variantes
    e inventario de demostración.
  * Crea administrador, encargado, cajero y cliente por empresa.
  * Crea el administrador de plataforma (usuario tipo 'S', idEmp NULL).
  * Escribe las credenciales de prueba en un archivo LOCAL fuera de Git.

Qué NO hace:
  * No toca Supabase ni la base actual: exige una URL de pruebas local.
  * No migra datos de la instalación anterior.
  * No reinicia contraseñas de cuentas existentes ni duplica registros: puede
    ejecutarse varias veces.

Los datos son ficticios y los logotipos son provisionales de demostración.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import string
import sys
import uuid
from datetime import date, datetime, time, timezone
from pathlib import Path

import psycopg
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.security import Passwords  # noqa: E402

NAMESPACE = uuid.UUID("6f5f0f5e-2c2b-4f0a-9a1e-6c0f0d9a4b11")
HOY = date.today()

FUNCIONES = [
    ("CU01", "Registrarse"),
    ("CU02", "Iniciar Sesión"),
    ("CU03", "Cerrar Sesión"),
    ("CU04", "Recuperar Contraseña"),
    ("CU05", "Gestionar Usuarios y Empleados"),
    ("CU06", "Gestionar Roles y Permisos"),
    ("CU07", "Gestionar Clientes"),
    ("CU08", "Consultar Bitácora"),
    ("CU09", "Gestionar Sucursales y Ciudades"),
    ("CU10", "Consultar Prendas"),
    ("CU11", "Gestionar Reserva"),
    ("CU12", "Agregar Productos al Carrito"),
    ("CU13", "Realizar Compra Digital"),
    ("CU14", "Realizar Pago Electrónico"),
    ("CU15", "Consultar Historial de Compras"),
    ("CU16", "Utilizar Vestidor Virtual"),
    ("CU17", "Recibir Recomendaciones IA"),
    ("CU18", "Gestionar Catálogo"),
    ("CU19", "Gestionar Proveedores"),
    ("CU20", "Gestionar Inventario"),
    ("CU21", "Gestionar Promociones y Descuentos"),
    ("CU22", "Gestionar Reservas en Sucursal"),
    ("CU23", "Registrar Devolución de Prendas"),
    ("CU24", "Registrar Venta Presencial"),
    ("CU25", "Dashboard y Reportes Gerenciales"),
]

PERMISOS_ROL = {
    "ADM": ["CU05", "CU06", "CU07", "CU08", "CU09", "CU15", "CU18", "CU19",
            "CU20", "CU21", "CU22", "CU23", "CU24", "CU25"],
    "ENC": ["CU11", "CU15", "CU20", "CU22", "CU23"],
    "CAJ": ["CU24"],
    "CLI": ["CU10", "CU11", "CU12", "CU13", "CU14", "CU15", "CU17"],
}

EMPRESAS = [
    {
        "codigo": "manaco",
        "corto": "MAN",
        "nombre": "Manaco",
        "especialidad": "Calzado",
        "plan": "Profesional",
        "ciudad": "Cochabamba",
        "colorPrimario": "#1B2A4A",
        "colorSecundario": "#C8A24A",
        "logo": "/brands/manaco-demo.svg",
        "sucursales": ["Manaco Centro", "Manaco Cala Cala", "Manaco Quillacollo"],
        "categorias": ["Zapatillas", "Zapatos formales", "Sandalias"],
        "tallas": ["35", "36", "37", "38", "39", "40", "41", "42"],
        "colores": [("Negro", "#111111"), ("Blanco", "#FFFFFF"), ("Azul", "#1E3A8A")],
        "productos": [
            ("Zapatilla urbana Manaco", 289.90),
            ("Zapato formal de cuero Manaco", 459.00),
            ("Sandalia casual Manaco", 199.90),
        ],
    },
    {
        "codigo": "kosy-pantalones",
        "corto": "KSP",
        "nombre": "Kosy Pantalones",
        "especialidad": "Pantalones para varón",
        "plan": "Profesional",
        "ciudad": "La Paz",
        "colorPrimario": "#2F4F4F",
        "colorSecundario": "#E8D6B3",
        "logo": "/brands/kosy-pantalones-demo.svg",
        "sucursales": ["Kosy Pantalones Sopocachi", "Kosy Pantalones El Alto"],
        "categorias": ["Jeans", "Pantalón chino", "Pantalón de vestir"],
        "tallas": ["28", "30", "32", "34", "36", "38"],
        "colores": [("Azul jean", "#2C3E70"), ("Negro", "#111111"), ("Beige", "#D9C7A7")],
        "productos": [
            ("Jean recto Kosy", 249.90),
            ("Pantalón chino Kosy", 219.90),
        ],
    },
    {
        "codigo": "urbanwomen",
        "corto": "URB",
        "nombre": "UrbanWomen",
        "especialidad": "Pantalones para mujer",
        "plan": "Basico",
        "ciudad": "Santa Cruz",
        "colorPrimario": "#7B2D5E",
        "colorSecundario": "#F2C4D8",
        "logo": "/brands/urbanwomen-demo.svg",
        "sucursales": ["UrbanWomen Equipetrol", "UrbanWomen Ventura Mall"],
        "categorias": ["Jeans mujer", "Pantalón palazzo", "Leggings"],
        "tallas": ["XS", "S", "M", "L", "XL"],
        "colores": [("Negro", "#111111"), ("Azul", "#1E3A8A"), ("Rosado", "#E8A0B4")],
        "productos": [
            ("Jean skinny UrbanWomen", 229.90),
            ("Pantalón palazzo UrbanWomen", 259.90),
        ],
    },
    {
        "codigo": "kosy-prendas",
        "corto": "KPR",
        "nombre": "Kosy de poleras y camisas",
        "especialidad": "Poleras y camisas para damas y varones",
        "plan": "Basico",
        "ciudad": "La Paz",
        "colorPrimario": "#0F5257",
        "colorSecundario": "#F4A259",
        "logo": "/brands/kosy-prendas-demo.svg",
        "sucursales": ["Kosy Prendas Miraflores", "Kosy Prendas San Miguel"],
        "categorias": ["Poleras", "Camisas", "Camisetas"],
        "tallas": ["XS", "S", "M", "L", "XL"],
        "colores": [("Blanco", "#FFFFFF"), ("Negro", "#111111"), ("Verde", "#0F5257")],
        "productos": [
            ("Polera básica Kosy", 89.90),
            ("Camisa manga larga Kosy", 159.90),
        ],
    },
    {
        "codigo": "lupo",
        "corto": "LUP",
        "nombre": "Lupo",
        "especialidad": "Ropa interior para damas y varones",
        "plan": "Profesional",
        "ciudad": "Santa Cruz",
        "colorPrimario": "#4B1F3A",
        "colorSecundario": "#FF6B6B",
        "logo": "/brands/lupo-demo.svg",
        "sucursales": ["Lupo Centro", "Lupo Las Brisas", "Lupo Montero"],
        "categorias": ["Ropa interior dama", "Ropa interior varón", "Pijamas"],
        "tallas": ["S", "M", "L", "XL"],
        "colores": [("Negro", "#111111"), ("Blanco", "#FFFFFF"), ("Rojo", "#B91C1C")],
        "productos": [
            ("Conjunto interior Lupo dama", 129.90),
            ("Boxer algodón Lupo varón", 69.90),
        ],
    },
]


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------

def leer_entorno() -> tuple[str, str]:
    """URLs de la base DE PRUEBAS (rol HTTP y rol de plataforma)."""
    archivo = ROOT.parent / ".local" / "backend.env"
    valores: dict[str, str] = {}
    if archivo.exists():
        for linea in archivo.read_text(encoding="utf-8").splitlines():
            if linea.strip() and not linea.strip().startswith("#") and "=" in linea:
                clave, _, valor = linea.partition("=")
                valores[clave.strip()] = valor.strip()
    app_url = os.environ.get("TEST_DATABASE_URL") or valores.get("TEST_DATABASE_URL")
    platform_url = (os.environ.get("TEST_PLATFORM_DATABASE_URL")
                    or valores.get("TEST_PLATFORM_DATABASE_URL"))
    if not app_url or not platform_url:
        raise SystemExit(
            "No se encontraron TEST_DATABASE_URL y TEST_PLATFORM_DATABASE_URL.\n"
            "Ejecute primero database/saas/00_preparar_postgres_pruebas.ps1")
    for url in (app_url, platform_url):
        parsed = make_url(url)
        if (parsed.host != "127.0.0.1" or parsed.port != 55432
                or parsed.database != "fashionstore_test"):
            raise SystemExit(
                "Por seguridad el seed solo acepta 127.0.0.1:55432/fashionstore_test; "
                "no se ejecuta contra Supabase ni otra base.")
    return app_url, platform_url


def a_psycopg(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def nueva_contrasena(largo: int = 18) -> str:
    alfabeto = string.ascii_letters + string.digits
    return "".join(secrets.choice(alfabeto) for _ in range(largo))


def id_usuario(codigo: str, rol: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{codigo}:{rol}"))


def correo(codigo: str, rol: str) -> str:
    # Cajero y cliente comparten correo entre empresas: demuestra que el mismo
    # correo puede existir en tenants distintos y que el login resuelve por código.
    # Dominio .demo: Pydantic/email-validator rechaza dominios reservados (.test,
    # .local, .example), así que no sirven para probar el login HTTP real.
    return {
        "admin": f"admin@{codigo}.demo",
        "encargado": f"encargado@{codigo}.demo",
        "cajero": "cajero@fashionstore.demo",
        "cliente": "cliente@fashionstore.demo",
    }[rol]


def siguiente(cur, tabla: str, columna: str) -> int:
    cur.execute(f"SELECT coalesce(max({columna}), 0) + 1 FROM {tabla}")
    return int(cur.fetchone()[0])


# --------------------------------------------------------------------------
# Carga
# --------------------------------------------------------------------------

def preparar_catalogos(cur):
    cur.executemany(
        "INSERT INTO funcion(id, descripcion) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        FUNCIONES)
    cur.execute("SELECT idplan, nombre FROM plan ORDER BY idplan")
    planes = {nombre: idplan for idplan, nombre in cur.fetchall()}
    for requerido in ("Basico", "Profesional"):
        if requerido not in planes:
            raise SystemExit(f"Falta el plan {requerido}: aplique 01_FashionStore_SaaS_TABLAS.sql")
    return planes


def asegurar_plataforma(cur, passwords: Passwords, credenciales: dict) -> dict:
    correo_admin = "admin@fashionstore.saas"
    previo = credenciales.get("plataforma") or {}
    contrasena = previo.get("contrasena") or nueva_contrasena()
    usuario = str(uuid.uuid5(NAMESPACE, "plataforma:saas-admin"))
    cur.execute("""
        INSERT INTO usuario(idUsuario, ci, nombre, apellidoPat, apellidoMat, sexo, correo,
                            telefono, direccion, contrasena, fechaNac, tipo, nroRol, idEmp)
        VALUES (%s,%s,%s,%s,%s,'M',%s,%s,%s,%s,%s,'S','SAAS-ADMIN',NULL)
        ON CONFLICT (idUsuario) DO NOTHING
    """, (usuario, "SaaS-000", "Administrador", "Plataforma", "FashionStore", correo_admin,
          "70000000", "Oficina central FashionStore", passwords.hash(contrasena), date(1990, 1, 1)))
    # Permisos de plataforma: catálogo separado de CU05..CU25.
    cur.execute("""
        INSERT INTO rol_funcion(nroRol, idFun, descripcion, idEmp)
        SELECT 'SAAS-ADMIN', f.id, f.descripcion, NULL FROM funcion f WHERE f.id LIKE 'SAAS-%%'
        ON CONFLICT (nroRol, idFun) DO NOTHING
    """)
    return {
        "rol": "Administrador SaaS",
        "correo": correo_admin,
        "contrasena": contrasena,
        "acceso": "/login/saas",
        "permisos": ["SAAS-EMPRESAS", "SAAS-PLANES", "SAAS-AUDITORIA", "SAAS-DEMO", "SAAS-SOPORTE"],
    }


def asegurar_empresa(cur, spec, planes, passwords, previa: dict) -> tuple[int, dict]:
    cur.execute("SELECT idemp FROM empresa WHERE codigo = %s", (spec["codigo"],))
    fila = cur.fetchone()
    admin = previa.get("usuarios", {}).get("admin", {})
    if fila is None:
        contrasena = admin.get("contrasena") or nueva_contrasena()
        cur.execute("""
            CALL sp_registrar_empresa(
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
        """, (
            spec["codigo"], spec["nombre"], None, planes[spec["plan"]],
            HOY, date(HOY.year + 1, HOY.month, min(HOY.day, 28)),
            id_usuario(spec["codigo"], "admin"), f"CI-{spec['codigo']}-ADM",
            "Administrador", spec["nombre"], "Demo", "M",
            correo(spec["codigo"], "admin"),
            "70000001", f"Dirección demo {spec['nombre']}",
            passwords.hash(contrasena), date(1990, 1, 1),
        ))
        idemp = cur.fetchone()[0]
    else:
        idemp = fila[0]
        contrasena = admin.get("contrasena")

    # Identidad visual: solo se completa lo que falte para no pisar cambios
    # hechos desde el panel SaaS.
    cur.execute("""
        UPDATE empresa
           SET nombre = CASE WHEN codigo = 'kosy-prendas' AND nombre = 'Kosy' THEN %s
                             ELSE COALESCE(NULLIF(nombre, ''), %s) END,
               logourl = COALESCE(logourl, %s),
               colorprimario = COALESCE(colorprimario, %s),
               colorsecundario = COALESCE(colorsecundario, %s),
               esdemostracion = true
         WHERE idemp = %s
    """, (spec["nombre"], spec["nombre"], spec["logo"], spec["colorPrimario"], spec["colorSecundario"], idemp))
    return idemp, {"contrasena": contrasena}


def asegurar_permisos(cur, idemp: int):
    for sufijo, permisos in PERMISOS_ROL.items():
        rol = f"E{idemp}-{sufijo}"
        for codigo in permisos:
            cur.execute("""
                INSERT INTO rol_funcion(nroRol, idFun, descripcion, idEmp)
                SELECT %s, f.id, f.descripcion, %s FROM funcion f WHERE f.id = %s
                ON CONFLICT (nroRol, idFun) DO NOTHING
            """, (rol, idemp, codigo))


def asegurar_ciudad(cur, idemp: int, nombre: str) -> int:
    cur.execute("SELECT id FROM ciudad WHERE idemp = %s AND lower(nombre) = lower(%s)",
                (idemp, nombre))
    fila = cur.fetchone()
    if fila:
        return fila[0]
    nuevo = siguiente(cur, "ciudad", "id")
    cur.execute("INSERT INTO ciudad(id, nombre, idemp) VALUES (%s, %s, %s)",
                (nuevo, nombre, idemp))
    return nuevo


def asegurar_sucursales(cur, idemp: int, spec, idciudad: int) -> list[int]:
    nros = []
    for nombre in spec["sucursales"]:
        cur.execute("SELECT nro FROM sucursal WHERE idemp = %s AND nombre = %s", (idemp, nombre))
        fila = cur.fetchone()
        if fila:
            nros.append(fila[0])
            continue
        nro = siguiente(cur, "sucursal", "nro")
        cur.execute("""
            INSERT INTO sucursal(nro, nombre, direccion, estado, idciud, idemp, latitud, longitud)
            VALUES (%s, %s, %s, 'activo', %s, %s, NULL, NULL)
        """, (nro, nombre, f"Av. Demo {nro}, {spec['ciudad']}", idciudad, idemp))
        nros.append(nro)
        # Horario 09:00-20:00 de lunes a sábado.
        idaten = siguiente(cur, "horario_atencion", "idaten")
        cur.execute("INSERT INTO horario_atencion(idaten, horaini, horafin, idemp) "
                    "VALUES (%s, %s, %s, %s)", (idaten, time(9, 0), time(20, 0), idemp))
        cur.execute("INSERT INTO horario_suc(idaten, nrosuc, dias, idemp) "
                    "VALUES (%s, %s, %s, %s)",
                    (idaten, nro, "Lunes,Martes,Miercoles,Jueves,Viernes,Sabado", idemp))
    return nros


def asegurar_usuario(cur, *, idemp, idusuario, ci, nombre, apellido, sexo, correo_e,
                     telefono, direccion, contrasena_hash, nacimiento, tipo, rol) -> str:
    cur.execute("""
        INSERT INTO usuario(idUsuario, ci, nombre, apellidoPat, apellidoMat, sexo, correo,
                            telefono, direccion, contrasena, fechaNac, tipo, nroRol, idEmp)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (idUsuario) DO NOTHING
    """, (idusuario, ci, nombre, apellido, "Demo", sexo, correo_e, telefono, direccion,
          contrasena_hash, nacimiento, tipo, rol, idemp))
    return idusuario


def asegurar_catalogo(cur, idemp: int, spec, sucursales: list[int]):
    # Marca propia de la empresa.
    cur.execute("SELECT idmarca FROM marca WHERE idemp = %s AND lower(nombre) = lower(%s)",
                (idemp, spec["nombre"]))
    fila = cur.fetchone()
    if fila:
        idmarca = fila[0]
    else:
        idmarca = siguiente(cur, "marca", "idmarca")
        cur.execute("INSERT INTO marca(idmarca, nombre, estado, idemp) VALUES (%s,%s,'activo',%s)",
                    (idmarca, spec["nombre"], idemp))

    # Colección y temporada vigentes.
    cur.execute("SELECT idcol FROM coleccion WHERE idemp = %s AND lower(descripcion) = lower(%s)",
                (idemp, f"Colección {HOY.year}"))
    fila = cur.fetchone()
    if fila:
        idcol = fila[0]
    else:
        idcol = siguiente(cur, "coleccion", "idcol")
        cur.execute("INSERT INTO coleccion(idcol, descripcion, idemp) VALUES (%s,%s,%s)",
                    (idcol, f"Colección {HOY.year}", idemp))
    cur.execute("SELECT idtemp FROM temporada WHERE idemp = %s AND lower(nombre) = lower(%s)",
                (idemp, f"Temporada {HOY.year}"))
    fila = cur.fetchone()
    if fila:
        idtemp = fila[0]
    else:
        idtemp = siguiente(cur, "temporada", "idtemp")
        cur.execute("INSERT INTO temporada(idtemp, nombre, fechaini, fechafin, estado, idemp) "
                    "VALUES (%s,%s,%s,%s,'activo',%s)",
                    (idtemp, f"Temporada {HOY.year}", date(HOY.year, 1, 1),
                     date(HOY.year, 12, 31), idemp))

    # Proveedor.
    cur.execute("SELECT idprov FROM proveedor WHERE idemp = %s AND lower(nombre) = lower(%s)",
                (idemp, f"Proveedor demostración {spec['codigo']}"))
    fila = cur.fetchone()
    if fila:
        idprov = fila[0]
    else:
        idprov = siguiente(cur, "proveedor", "idprov")
        cur.execute("""
            INSERT INTO proveedor(idprov, nombre, correo, direccion, telefono, idemp)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (idprov, f"Proveedor demostración {spec['codigo']}",
              f"proveedor@{spec['codigo']}.demo", f"Av. Proveedores {idprov}",
              "70000009", idemp))

    # Categorías.
    ids_categoria = []
    for descripcion in spec["categorias"]:
        cur.execute("SELECT idcat FROM categoria WHERE idemp = %s AND lower(descripcion) = lower(%s)",
                    (idemp, descripcion))
        fila = cur.fetchone()
        if fila:
            ids_categoria.append(fila[0])
            continue
        nuevo = siguiente(cur, "categoria", "idcat")
        cur.execute("INSERT INTO categoria(idcat, descripcion, idemp) VALUES (%s,%s,%s)",
                    (nuevo, descripcion, idemp))
        ids_categoria.append(nuevo)

    # Tallas.
    ids_talla = []
    for descripcion in spec["tallas"]:
        cur.execute("SELECT idtalla FROM talla WHERE idemp = %s AND descripcion = %s",
                    (idemp, descripcion))
        fila = cur.fetchone()
        if fila:
            ids_talla.append(fila[0])
            continue
        nuevo = siguiente(cur, "talla", "idtalla")
        cur.execute("INSERT INTO talla(idtalla, descripcion, idemp) VALUES (%s,%s,%s)",
                    (nuevo, descripcion, idemp))
        ids_talla.append(nuevo)

    # Colores.
    for nombre, hexa in spec["colores"]:
        cur.execute("SELECT idcolor FROM color WHERE idemp = %s AND lower(descripcion) = lower(%s)",
                    (idemp, nombre))
        if cur.fetchone():
            continue
        nuevo = siguiente(cur, "color", "idcolor")
        cur.execute("INSERT INTO color(idcolor, descripcion, hex, idemp) VALUES (%s,%s,%s,%s)",
                    (nuevo, nombre, hexa, idemp))

    cur.execute("SELECT idcolor FROM color WHERE idemp = %s ORDER BY idcolor", (idemp,))
    colores = [fila[0] for fila in cur.fetchall()]

    # Productos, variantes e inventario.
    corto = spec["corto"]
    for indice, (descripcion, precio) in enumerate(spec["productos"], start=1):
        # idvariante es varchar(15): se usa un codigo corto estable por empresa.
        idprod = f"{corto}-P{indice}"
        cur.execute("SELECT 1 FROM producto WHERE idemp = %s AND idprod = %s", (idemp, idprod))
        if not cur.fetchone():
            cur.execute("""
                INSERT INTO producto(idprod, descripcion, estado, idpromo, idcat, idmarca,
                                     idcol, idprov, idemp)
                VALUES (%s,%s,'activo',NULL,%s,%s,%s,%s,%s)
            """, (idprod, descripcion, ids_categoria[(indice - 1) % len(ids_categoria)],
                  idmarca, idcol, idprov, idemp))
        for n_variante, idtalla in enumerate(ids_talla[:2], start=1):
            idvariante = f"{corto}-V{indice}{n_variante}"
            cur.execute("SELECT 1 FROM varianteprod WHERE idemp = %s AND idvariante = %s",
                        (idemp, idvariante))
            if not cur.fetchone():
                cur.execute("""
                    INSERT INTO varianteprod(idvariante, sku, precio, estado, img, idtalla,
                                             idprod, idemp)
                    VALUES (%s,%s,%s,'activo',NULL,%s,%s,%s)
                """, (idvariante, f"{corto}-{indice}{n_variante}",
                      precio, idtalla, idprod, idemp))
            for nro_suc in sucursales:
                cur.execute("SELECT 1 FROM inventario WHERE idemp = %s AND nrosuc = %s AND idvar = %s",
                            (idemp, nro_suc, idvariante))
                if cur.fetchone():
                    continue
                nro_inv = siguiente(cur, "inventario", "nroinv")
                cur.execute("""
                    INSERT INTO inventario(nroinv, stock, cantdisp, nrosuc, idvar, idemp)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (nro_inv, 40, 40, nro_suc, idvariante, idemp))


def cargar_empresa(cur, spec, planes, passwords, previa: dict,
                   reponer_stock_lupo_pruebas: bool = False) -> tuple[int, dict]:
    idemp, datos = asegurar_empresa(cur, spec, planes, passwords, previa)
    # Las tablas de empresa exigen contexto de tenant incluso para el rol de
    # aprovisionamiento: los triggers llaman a fn_exigir_tenant. El contexto es
    # transaccional y solo lo fija este script, nunca una petición HTTP.
    cur.execute("SELECT set_config('app.idemp', %s, true)", (str(idemp),))
    base = previa.get("usuarios", {})
    asegurar_permisos(cur, idemp)
    idciudad = asegurar_ciudad(cur, idemp, spec["ciudad"])
    sucursales = asegurar_sucursales(cur, idemp, spec, idciudad)
    asegurar_catalogo(cur, idemp, spec, sucursales)

    def clave(rol):
        return (base.get(rol) or {}).get("contrasena") or nueva_contrasena()

    usuarios = {}
    # Administrador de empresa (la fila admin ya la creó sp_registrar_empresa).
    usuarios["admin"] = {
        "rol": "Administrador de empresa",
        "correo": correo(spec["codigo"], "admin"),
        "contrasena": datos["contrasena"],
        "sucursal": None, "tipo": "A", "idUsuario": id_usuario(spec["codigo"], "admin"),
    }
    # Encargado de sucursal.
    enc_pass = clave("encargado")
    id_enc = asegurar_usuario(
        cur, idemp=idemp, idusuario=id_usuario(spec["codigo"], "encargado"),
        ci=f"CI-{spec['codigo']}-ENC", nombre="Encargado", apellido="Demo", sexo="M",
        correo_e=correo(spec["codigo"], "encargado"), telefono="70000002",
        direccion=f"Sucursal {sucursales[0]}", contrasena_hash=passwords.hash(enc_pass),
        nacimiento=date(1992, 1, 1), tipo="E", rol=f"E{idemp}-ENC")
    cur.execute("""
        INSERT INTO empleado(idUsuario, cod_emp, cargo, nrosuc, idemp, estado)
        VALUES (%s,%s,'Encargado',%s,%s,'activo') ON CONFLICT (idUsuario) DO NOTHING
    """, (id_enc, f"ENC-{idemp}", sucursales[0], idemp))
    usuarios["encargado"] = {"rol": "Encargado de sucursal", "correo": correo(spec["codigo"], "encargado"),
                             "contrasena": enc_pass, "sucursal": sucursales[0], "tipo": "E",
                             "idUsuario": id_enc}

    # Cajero.
    caj_pass = clave("cajero")
    id_caj = asegurar_usuario(
        cur, idemp=idemp, idusuario=id_usuario(spec["codigo"], "cajero"),
        ci=f"CI-{spec['codigo']}-CAJ", nombre="Cajero", apellido="Demo", sexo="F",
        correo_e=correo(spec["codigo"], "cajero"), telefono="70000003",
        direccion=f"Sucursal {sucursales[0]}", contrasena_hash=passwords.hash(caj_pass),
        nacimiento=date(1994, 1, 1), tipo="E", rol=f"E{idemp}-CAJ")
    cur.execute("""
        INSERT INTO empleado(idUsuario, cod_emp, cargo, nrosuc, idemp, estado)
        VALUES (%s,%s,'Cajero',%s,%s,'activo') ON CONFLICT (idUsuario) DO NOTHING
    """, (id_caj, f"CAJ-{idemp}", sucursales[0], idemp))
    usuarios["cajero"] = {"rol": "Cajero", "correo": correo(spec["codigo"], "cajero"),
                          "contrasena": caj_pass, "sucursal": sucursales[0], "tipo": "E",
                          "idUsuario": id_caj}

    # Cliente.
    cli_pass = clave("cliente")
    id_cli = asegurar_usuario(
        cur, idemp=idemp, idusuario=id_usuario(spec["codigo"], "cliente"),
        ci=f"CI-{spec['codigo']}-CLI", nombre="Cliente", apellido="Demo", sexo="F",
        correo_e=correo(spec["codigo"], "cliente"), telefono="70000004",
        direccion=f"Domicilio demo {spec['nombre']}", contrasena_hash=passwords.hash(cli_pass),
        nacimiento=date(1996, 1, 1), tipo="C", rol=f"E{idemp}-CLI")
    cur.execute("""
        INSERT INTO cliente(idUsuario, cod_cl, estado, proporciones, idemp)
        VALUES (%s,%s,'frecuente',NULL,%s) ON CONFLICT (idUsuario) DO NOTHING
    """, (id_cli, f"CLI-{idemp}", idemp))
    usuarios["cliente"] = {"rol": "Cliente", "correo": correo(spec["codigo"], "cliente"),
                           "contrasena": cli_pass, "sucursal": None, "tipo": "C",
                           "idUsuario": id_cli}

    # Mantiene sincronizado el correo de las cuentas de demostración ya creadas
    # por una ejecución anterior (por ejemplo, si cambió el dominio de ejemplo).
    for datos_usuario in usuarios.values():
        cur.execute("UPDATE usuario SET correo = %s WHERE idUsuario = %s",
                    (datos_usuario["correo"], datos_usuario["idUsuario"]))

    # La prueba concurrente de sync consume deliberadamente todo el stock de
    # Lupo en la sucursal asignada al cajero. Reponerlo es una operación
    # explícita, solo sobre fashionstore_test y con movimiento de ajuste auditable;
    # el seed normal nunca pisa existencias reales ni demo existentes.
    if reponer_stock_lupo_pruebas and spec["codigo"] == "lupo":
        cur.execute("""
            SELECT i.nroInv, i.stock, i.cantDisp
            FROM inventario i JOIN empleado e ON e.idEmp=i.idEmp AND e.nroSuc=i.nroSuc
            WHERE i.idEmp=%s AND e.idUsuario=%s AND e.cargo='Cajero'
            ORDER BY i.idVar
        """, (idemp, id_caj))
        for nro_inv, stock_actual, disponible_actual in cur.fetchall():
            if stock_actual >= 40 and disponible_actual >= 40:
                continue
            if stock_actual != disponible_actual:
                raise SystemExit("No se repone Lupo: stock y disponibilidad difieren por reservas u otra operación")
            delta = 40 - stock_actual
            cur.execute("UPDATE inventario SET stock=40,cantDisp=40 WHERE idEmp=%s AND nroInv=%s",
                        (idemp, nro_inv))
            cur.execute("""
                INSERT INTO movimiento_inv(idEmp,tipoMov,cantidad,motivo,nroInv)
                VALUES (%s,'ajuste',%s,'Reaprovisionamiento fixture automatizado fashionstore_test',%s)
            """, (idemp, delta, nro_inv))

    return idemp, {
        "codigo": spec["codigo"],
        "nombre": spec["nombre"],
        "especialidad": spec["especialidad"],
        "plan": spec["plan"],
        "idEmp": idemp,
        "identidad": {"logo": spec["logo"], "colorPrimario": spec["colorPrimario"],
                      "colorSecundario": spec["colorSecundario"]},
        "sucursales": [{"nro": nro, "nombre": nombre}
                       for nro, nombre in zip(sucursales, spec["sucursales"])],
        "usuarios": usuarios,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Carga las empresas de demostración de FashionStore")
    parser.add_argument("--solo", nargs="*", help="Códigos de empresa a cargar (por defecto todas)")
    parser.add_argument("--salida", default=str(ROOT / ".local" / "demo_credentials.json"))
    parser.add_argument("--reponer-stock-lupo-pruebas", action="store_true",
                        help="Reponer a 40 unidades el inventario del cajero Lupo en fashionstore_test; registra ajuste")
    args = parser.parse_args()

    _, platform_url = leer_entorno()
    salida = Path(args.salida)
    previas = {}
    if salida.exists():
        try:
            previas = json.loads(salida.read_text(encoding="utf-8"))
        except ValueError:
            previas = {}
    credenciales_previas = {e["codigo"]: e for e in previas.get("empresas", [])}

    passwords = Passwords("argon2id")
    hoy = datetime.now(timezone.utc).isoformat(timespec="seconds")
    seleccion = [e for e in EMPRESAS if not args.solo or e["codigo"] in set(args.solo)]
    if not seleccion:
        raise SystemExit("Ningún código de empresa coincide con --solo")

    resultado = {
        "generado": hoy,
        "entorno": "demostración / pruebas",
        "advertencia": ("Credenciales ficticias de demostración. No usar en producción. "
                        "Archivo local excluido de Git."),
        "plataforma": None,
        "empresas": [],
    }

    with psycopg.connect(a_psycopg(platform_url)) as conexion:
        with conexion.cursor() as cur:
            planes = preparar_catalogos(cur)
            resultado["plataforma"] = asegurar_plataforma(cur, passwords, previas)
            for spec in seleccion:
                previa = credenciales_previas.get(spec["codigo"], {})
                _, datos = cargar_empresa(cur, spec, planes, passwords, previa,
                                          reponer_stock_lupo_pruebas=args.reponer_stock_lupo_pruebas)
                resultado["empresas"].append(datos)
            # Conservar empresas no recargadas en esta ejecución.
            for codigo, previa in credenciales_previas.items():
                if codigo not in {e["codigo"] for e in resultado["empresas"]}:
                    resultado["empresas"].append(previa)
        conexion.commit()

    # Reordenar según la lista canónica.
    orden = {e["codigo"]: i for i, e in enumerate(EMPRESAS)}
    resultado["empresas"].sort(key=lambda e: orden.get(e["codigo"], 99))

    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(salida, 0o600)
    except OSError:
        pass

    print(f"Empresas cargadas: {len(resultado['empresas'])}")
    for empresa in resultado["empresas"]:
        print(f"  - {empresa['codigo']:16s} {empresa['nombre']:16s} plan={empresa['plan']:12s} "
              f"sucursales={len(empresa['sucursales'])} usuarios={len(empresa['usuarios'])}")
    print(f"Administrador SaaS: {resultado['plataforma']['correo']}")
    print(f"Credenciales locales (no versionadas): {salida}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
