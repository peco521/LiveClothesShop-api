"""Explicit, interactive provisioning; never runs on application startup."""
import argparse
import warnings
from getpass import GetPassWarning, getpass

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.core.database import create_database
from app.core.security import Passwords
from app.modules.seguridad_accesos.schemas.bootstrap import SuperAdminInput
from app.modules.seguridad_accesos.services.bootstrap import BootstrapError, provision
from app.modules.seguridad_accesos.services.bootstrap import provision_initial_permissions


def prompt_superadmin():
    fields = (
        ("correo", "Correo"), ("ci", "CI"), ("nombres", "Nombres"),
        ("apellidoPat", "Apellido paterno"), ("apellidoMat", "Apellido materno"),
        ("sexo", "Sexo (M/F)"), ("telefono", "Teléfono"), ("direccion", "Dirección"),
        ("fechaNac", "Fecha de nacimiento (AAAA-MM-DD)"), ("cod_adm", "cod_adm"),
    )
    values = {field: input(f"{label}: ") for field, label in fields}
    # Refuse getpass's echoing fallback when no secure terminal is available.
    with warnings.catch_warnings():
        warnings.simplefilter("error", GetPassWarning)
        password = getpass("Contraseña: ")
        confirmation = getpass("Confirmar contraseña: ")
    if password != confirmation:
        raise BootstrapError("Las contraseñas no coinciden.")
    return SuperAdminInput(**values, contrasena=password)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bootstrap explícito de Cliente, SuperAdmin y permisos iniciales")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--crear-rol-cliente", action="store_true")
    mode.add_argument("--crear-superadmin", action="store_true")
    mode.add_argument("--crear-permisos-iniciales", action="store_true")
    args = parser.parse_args(argv)
    try:
        data = prompt_superadmin() if args.crear_superadmin else None
        settings = Settings()
        passwords = Passwords() if data is not None else None
        engine, factory = create_database(settings.database_url.get_secret_value())
        try:
            with factory() as db:
                if args.crear_permisos_iniciales:
                    created = provision_initial_permissions(db, settings.cliente_rol_id)
                else:
                    created = provision(db, settings.cliente_rol_id, data, passwords)
        finally:
            engine.dispose()
    except BootstrapError as exc:
        raise SystemExit(str(exc)) from None
    except (ValidationError, SQLAlchemyError):
        raise SystemExit("Datos o configuración inválidos, o base de datos no disponible. No se completó el bootstrap.") from None
    except (EOFError, KeyboardInterrupt, GetPassWarning):
        raise SystemExit("Bootstrap cancelado; se requiere una terminal segura para las contraseñas.") from None
    if args.crear_permisos_iniciales:
        print(f"Catálogo inicial verificado: {created[0]} funciones y {created[1]} relaciones creadas.")
        print("Cliente sin permisos. Roles, usuarios, perfiles y contraseñas sin cambios.")
    elif args.crear_superadmin:
        print("SuperAdmin creado." if created else "SuperAdmin existente verificado; contraseña sin cambios.")
        print("Rol Cliente verificado. No se crearon ni modificaron permisos.")
    else:
        print("Rol Cliente verificado. No se crearon usuarios ni permisos administrativos.")


if __name__ == "__main__":
    main()
