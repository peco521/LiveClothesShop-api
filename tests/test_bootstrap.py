from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock
import warnings

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import bootstrap as cli
from app.core.database import Base
from app.core.security import Passwords
from app.modules.seguridad_accesos.models import Admin, Cliente, Funcion, Rol, RolFuncion, Usuario
from app.modules.seguridad_accesos.repositories import admin
from app.modules.seguridad_accesos.repositories.bootstrap import lock_writes
from app.modules.seguridad_accesos.schemas.bootstrap import SuperAdminInput
from app.modules.seguridad_accesos.services.bootstrap import BootstrapError, provision
from app.modules.seguridad_accesos.services.bootstrap import INITIAL_PERMISSIONS, provision_initial_permissions
from app.modules.seguridad_accesos.repositories import bootstrap as bootstrap_repository, rol
from app.modules.seguridad_accesos.cu05_usuarios_empleados.models import Empleado
from app.modules.seguridad_accesos.shared.models import Ciudad, Sucursal


@pytest.fixture
def data(registration):
    return SuperAdminInput(**registration, cod_adm="ADM001")


@pytest.fixture(scope="module")
def passwords():
    return Passwords()


def make_factory(url):
    engine = create_engine(url, hide_parameters=True, connect_args={"timeout": 10})

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def empty_factory():
    engine, factory = make_factory("sqlite+pysqlite:///:memory:")
    yield factory
    engine.dispose()


def run(factory, data=None, passwords=None, role="cliente"):
    with factory() as db:
        return provision(db, role, data, passwords)


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_creation_and_repeat(empty_factory, data, passwords):
    assert run(empty_factory, data, passwords) is True
    with empty_factory() as db:
        user = db.scalar(select(Usuario))
        encoded = user.contrasena
        assert user.tipo == "A" and user.nrorol == "superadmin" and user.activo
        assert encoded != data.contrasena.get_secret_value()
        assert encoded.startswith("$argon2id$")
        assert passwords.verify(encoded, data.contrasena.get_secret_value())
        assert db.get(Admin, user.idusuario).cod_adm == data.cod_adm
        assert db.get(Rol, "cliente").descripcion == "Cliente"
        assert db.get(Rol, "superadmin").descripcion == "SuperAdmin"
        assert count(db, Cliente) == count(db, Funcion) == count(db, RolFuncion) == 0
    changed = SuperAdminInput(**(data.model_dump() | {"contrasena": "Otra frase ficticia para pruebas"}))
    never_hash = Mock()
    assert run(empty_factory, changed, never_hash) is False
    never_hash.hash.assert_not_called()
    with empty_factory() as db:
        assert count(db, Usuario) == count(db, Admin) == 1
        assert db.scalar(select(Usuario.contrasena)) == encoded


@pytest.mark.parametrize("kind", ["C", "E", "wrong_role", "missing_admin", "code", "cliente_profile", "empleado_profile", "inactive"])
def test_incompatible_or_partial_aborts(empty_factory, data, passwords, kind):
    run(empty_factory, data, passwords)
    with empty_factory.begin() as db:
        user = db.scalar(select(Usuario))
        if kind in {"C", "E"}:
            user.tipo = kind
        elif kind == "wrong_role":
            user.nrorol = "cliente"
        elif kind == "missing_admin":
            db.delete(db.get(Admin, user.idusuario))
        elif kind == "code":
            db.get(Admin, user.idusuario).cod_adm = "OTHER"
        elif kind == "cliente_profile":
            db.add(Cliente(idusuario=user.idusuario, cod_cl="CL001"))
        elif kind == "empleado_profile":
            db.add(Ciudad(id=1, nombre="Ciudad de prueba"))
            db.flush()
            db.add(Sucursal(nro=1, nombre="Sucursal de prueba", direccion="Prueba", idciud=1))
            db.flush()
            db.add(Empleado(idusuario=user.idusuario, cod_emp="EMP001", cargo="Prueba", nrosuc=1))
        else:
            user.activo = False
    with empty_factory() as db:
        before = tuple(db.execute(select(Usuario.__table__)).one())
        profile_before = db.scalar(select(Admin.cod_adm))
    with pytest.raises(BootstrapError):
        run(empty_factory, data, passwords)
    with empty_factory() as db:
        assert tuple(db.execute(select(Usuario.__table__)).one()) == before
        assert db.scalar(select(Admin.cod_adm)) == profile_before


def test_cod_adm_is_not_unique(empty_factory, data, passwords):
    run(empty_factory, data, passwords)
    other = SuperAdminInput(**(data.model_dump() | {"correo": "otro@example.com"}))
    assert run(empty_factory, other, passwords)
    with empty_factory() as db:
        assert count(db, Admin) == 2


def test_existing_cliente_preserved(empty_factory, data, passwords):
    run(empty_factory, data, passwords)
    with empty_factory.begin() as db:
        user = db.scalar(select(Usuario))
        db.delete(db.get(Admin, user.idusuario))
        user.tipo, user.nrorol = "C", "cliente"
        db.add(Cliente(idusuario=user.idusuario, cod_cl="CL001"))
        db.flush()
        db.delete(db.get(Rol, "superadmin"))
    assert run(empty_factory) is False
    with pytest.raises(BootstrapError, match="incompatible"):
        run(empty_factory, data, passwords)
    with empty_factory() as db:
        assert db.get(Rol, "superadmin") is None  # creation rolled back
        assert count(db, Cliente) == 1 and count(db, Admin) == 0


@pytest.mark.parametrize("role,description", [("cliente", "Administrador"), ("superadmin", "Cliente")])
def test_role_identity_and_rollback(empty_factory, data, passwords, role, description):
    with empty_factory.begin() as db:
        db.add(Rol(nro=role, descripcion=description))
    with pytest.raises(BootstrapError, match="descripción"):
        run(empty_factory, data, passwords)
    with empty_factory() as db:
        assert count(db, Rol) == 1 and count(db, Usuario) == 0
        assert db.get(Rol, role).descripcion == description


def test_public_role_permissions_rejected(empty_factory):
    run(empty_factory)
    with empty_factory.begin() as db:
        db.add(Funcion(id="test", descripcion="Ficticia"))
        db.flush()
        db.add(RolFuncion(nrorol="cliente", idfun="test", descripcion="Ficticia"))
    with pytest.raises(BootstrapError, match="permisos"):
        run(empty_factory)


@pytest.mark.parametrize("description", ["Cliente", " cliente ", "SuperAdmin"])
def test_existing_role_under_other_id_not_duplicated(empty_factory, data, passwords, description):
    with empty_factory.begin() as db:
        db.add(Rol(nro="legacy", descripcion=description))
    with pytest.raises(BootstrapError, match="otro ID"):
        run(empty_factory, data, passwords)
    with empty_factory() as db:
        assert count(db, Rol) == 1 and count(db, Usuario) == 0


def test_normalized_existing_email(empty_factory, data, passwords):
    run(empty_factory, data, passwords)
    with empty_factory.begin() as db:
        db.scalar(select(Usuario)).correo = "  " + str(data.correo).upper() + "  "
    assert run(empty_factory, data, passwords) is False


def test_existing_superadmin_permissions_unchanged(empty_factory, data, passwords):
    run(empty_factory, data, passwords)
    with empty_factory.begin() as db:
        db.add(Funcion(id="existing", descripcion="Ficticia"))
        db.flush()
        db.add(RolFuncion(nrorol="superadmin", idfun="existing", descripcion="Ficticia"))
    assert run(empty_factory, data, passwords) is False
    with empty_factory() as db:
        assert count(db, RolFuncion) == 1


def test_admin_mapping_matches_official_constraints():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable
    ddl = str(CreateTable(Admin.__table__).compile(dialect=postgresql.dialect()))
    assert "PRIMARY KEY (idusuario)" in ddl
    assert "cod_adm VARCHAR(10) NOT NULL" in ddl and "UNIQUE" not in ddl
    assert "ON DELETE CASCADE ON UPDATE CASCADE" in ddl


@pytest.mark.parametrize("role", ["", " cliente ", "superadmin", "SUPERADMIN", "x" * 16])
def test_bad_cliente_id(empty_factory, role):
    with pytest.raises(BootstrapError, match="CLIENTE_ROL_ID"):
        run(empty_factory, role=role)


def test_rollback_after_user_insert(empty_factory, data, passwords, monkeypatch):
    def fail(db, user_id, code):
        assert count(db, Usuario) == 1
        raise IntegrityError("private SQL", {}, Exception("private details"))
    monkeypatch.setattr(admin, "add", fail)
    with pytest.raises(BootstrapError, match="revertida") as error:
        run(empty_factory, data, passwords)
    assert "private" not in str(error.value)
    with empty_factory() as db:
        assert count(db, Rol) == count(db, Usuario) == count(db, Admin) == 0


def test_cliente_repeat(empty_factory):
    assert run(empty_factory) is False
    assert run(empty_factory) is False
    with empty_factory() as db:
        assert count(db, Rol) == 1 and count(db, Usuario) == 0


@pytest.mark.parametrize("superadmin", [False, True])
def test_concurrent_bootstraps(tmp_path, data, passwords, superadmin):
    engine, factory = make_factory("sqlite+pysqlite:///" + (tmp_path / "isolated.db").as_posix())
    barrier = Barrier(2)
    def worker():
        barrier.wait(timeout=10)
        return run(factory, data if superadmin else None, passwords)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            results = [future.result(timeout=20) for future in futures]
        assert sum(results) == int(superadmin)
        with factory() as db:
            assert count(db, Rol) == (2 if superadmin else 1)
            assert count(db, Usuario) == count(db, Admin) == int(superadmin)
    finally:
        engine.dispose()


def test_pg_lock_contract_without_connection():
    db = Mock()
    db.get_bind.return_value.dialect.name = "postgresql"
    lock_writes(db)
    statements = [str(call.args[0]) for call in db.execute.call_args_list]
    assert statements == ["SET LOCAL lock_timeout = '5s'",
                          "LOCK TABLE rol, usuario, admin, cliente, empleado IN SHARE ROW EXCLUSIVE MODE"]


def mock_prompts(monkeypatch, data, confirmation=None):
    values = data.model_dump()
    fields = ["correo", "ci", "nombres", "apellidoPat", "apellidoMat", "sexo",
              "telefono", "direccion", "fechaNac", "cod_adm"]
    inputs = Mock(side_effect=[str(values[field]) for field in fields])
    secret = data.contrasena.get_secret_value()
    hidden = Mock(side_effect=[secret, secret if confirmation is None else confirmation])
    monkeypatch.setattr("builtins.input", inputs)
    monkeypatch.setattr(cli, "getpass", hidden)
    return inputs, hidden


def test_prompts_and_getpass(monkeypatch, data):
    inputs, hidden = mock_prompts(monkeypatch, data)
    assert cli.prompt_superadmin() == data
    assert inputs.call_count == 10
    assert [call.args[0] for call in hidden.call_args_list] == ["Contraseña: ", "Confirmar contraseña: "]


@pytest.mark.parametrize("updates", [{"correo": "invalid"}, {"sexo": "X"},
    {"fechaNac": "2999-01-01"}, {"cod_adm": "x" * 11}, {"cod_adm": " "},
    {"contrasena": "short"}, {"contrasena": " " * 12}, {"ci": " "}])
def test_validation(data, updates):
    with pytest.raises(ValidationError):
        SuperAdminInput(**(data.model_dump() | updates))


def test_cli_mismatch_never_opens_db(monkeypatch, data, capsys):
    mock_prompts(monkeypatch, data, confirmation="not matching")
    settings = Mock(side_effect=AssertionError("Must not load .env"))
    monkeypatch.setattr(cli, "Settings", settings)
    with pytest.raises(SystemExit, match="no coinciden"):
        cli.main(["--crear-superadmin"])
    settings.assert_not_called()
    assert data.contrasena.get_secret_value() not in capsys.readouterr().out


def test_cli_invalid_input_is_sanitized(monkeypatch, data):
    invalid = data.model_copy(update={"correo": "invalid"})
    mock_prompts(monkeypatch, invalid)
    settings = Mock(side_effect=AssertionError("Must not load .env"))
    monkeypatch.setattr(cli, "Settings", settings)
    with pytest.raises(SystemExit, match="Datos o configuración inválidos") as error:
        cli.main(["--crear-superadmin"])
    assert data.contrasena.get_secret_value() not in str(error.value)
    settings.assert_not_called()


def test_getpass_fallback_aborts(monkeypatch, data):
    mock_prompts(monkeypatch, data)
    def insecure(prompt):
        warnings.warn("No terminal", cli.GetPassWarning)
        pytest.fail("Must abort before echoing fallback")
    monkeypatch.setattr(cli, "getpass", insecure)
    with pytest.raises(SystemExit, match="terminal segura"):
        cli.main(["--crear-superadmin"])


def test_cli_success_and_repeat(monkeypatch, data, empty_factory, settings, passwords, capsys):
    engine = Mock()
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "Passwords", lambda: passwords)
    monkeypatch.setattr(cli, "create_database", lambda _: (engine, empty_factory))
    for expected in ["SuperAdmin creado", "contraseña sin cambios"]:
        mock_prompts(monkeypatch, data)
        cli.main(["--crear-superadmin"])
        output = capsys.readouterr().out
        assert expected in output
        assert data.contrasena.get_secret_value() not in output and "$argon2" not in output
    assert engine.dispose.call_count == 2


def test_cli_failure_disposes(monkeypatch, empty_factory, settings):
    engine = Mock()
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "create_database", lambda _: (engine, empty_factory))
    with empty_factory.begin() as db:
        db.add(Rol(nro="cliente", descripcion="Incorrecto"))
    with pytest.raises(SystemExit, match="descripción"):
        cli.main(["--crear-rol-cliente"])
    engine.dispose.assert_called_once()


def seed_catalog_roles(factory):
    with factory.begin() as db:
        db.add_all([Rol(nro="cliente", descripcion="Cliente"),
                    Rol(nro="superadmin", descripcion="SuperAdmin")])


def run_catalog(factory):
    with factory() as db:
        return provision_initial_permissions(db, "cliente")


def catalog_snapshot(factory):
    with factory() as db:
        return tuple(tuple(db.execute(select(model.__table__).order_by(*model.__table__.primary_key)).all())
                     for model in (Rol, Funcion, RolFuncion, Usuario, Admin))


def test_catalog_exact_success_repeat_and_preservation(empty_factory, data, passwords):
    run(empty_factory, data, passwords)
    before = catalog_snapshot(empty_factory)
    assert run_catalog(empty_factory) == (5, 5)
    after = catalog_snapshot(empty_factory)
    assert before[0] == after[0] and before[3:] == after[3:]
    assert after[1] == INITIAL_PERMISSIONS
    assert after[2] == tuple(("superadmin", code, f"Permite {description}.")
                            for code, description in INITIAL_PERMISSIONS)
    assert all(len(description) <= 50 and len(f"Permite {description}.") <= 100
               for _, description in INITIAL_PERMISSIONS)
    assert run_catalog(empty_factory) == (0, 0)
    assert catalog_snapshot(empty_factory) == after
    with empty_factory() as db:
        assert rol.permissions(db, "cliente") == []
        assert rol.public_role(db, "cliente") is not None


@pytest.mark.parametrize("assignment", [False, True])
def test_catalog_reuses_partial_exact_rows(empty_factory, assignment):
    seed_catalog_roles(empty_factory)
    with empty_factory.begin() as db:
        code, description = INITIAL_PERMISSIONS[0]
        db.add(Funcion(id=code, descripcion=description))
        db.flush()
        if assignment:
            db.add(RolFuncion(nrorol="superadmin", idfun=code, descripcion=f"Permite {description}."))
    assert run_catalog(empty_factory) == (4, 4 if assignment else 5)


@pytest.mark.parametrize("kind", ["function", "null_description", "assignment", "cliente_permission",
                                  "cliente_missing", "superadmin_missing", "role_description"])
def test_catalog_conflicts_leave_everything_unchanged(empty_factory, kind):
    seed_catalog_roles(empty_factory)
    with empty_factory.begin() as db:
        if kind.endswith("missing"):
            db.delete(db.get(Rol, kind.removesuffix("_missing")))
        elif kind == "role_description":
            db.get(Rol, "superadmin").descripcion = "Otro"
        else:
            # Last entry ensures previous inserts must roll back on contradiction.
            code, description = INITIAL_PERMISSIONS[-1]
            db.add(Funcion(id=code, descripcion=("Contradictoria" if kind == "function" else
                                                None if kind == "null_description" else description)))
            db.flush()
            if kind in {"assignment", "cliente_permission"}:
                db.add(RolFuncion(nrorol="cliente" if kind == "cliente_permission" else "superadmin",
                                  idfun=code, descripcion="Contradictoria"))
    before = catalog_snapshot(empty_factory)
    with pytest.raises(BootstrapError):
        run_catalog(empty_factory)
    assert catalog_snapshot(empty_factory) == before


@pytest.mark.parametrize("error_type", [IntegrityError, RuntimeError])
def test_catalog_failure_after_inserts_rolls_back(empty_factory, monkeypatch, error_type):
    seed_catalog_roles(empty_factory)
    before = catalog_snapshot(empty_factory)
    original = bootstrap_repository.ensure_permission
    def fail(db, *args):
        result = original(db, *args)
        if args[0] == "CU09":
            assert count(db, Funcion) == count(db, RolFuncion) == 5
            if error_type is IntegrityError:
                raise IntegrityError("private SQL", {}, Exception("private details"))
            raise RuntimeError("simulated failure")
        return result
    monkeypatch.setattr(bootstrap_repository, "ensure_permission", fail)
    with pytest.raises(BootstrapError if error_type is IntegrityError else RuntimeError) as error:
        run_catalog(empty_factory)
    assert "private" not in str(error.value)
    assert catalog_snapshot(empty_factory) == before


def test_catalog_pg_lock_contract():
    db = Mock()
    db.get_bind.return_value.dialect.name = "postgresql"
    bootstrap_repository.lock_permission_writes(db)
    assert [str(call.args[0]) for call in db.execute.call_args_list] == [
        "SET LOCAL lock_timeout = '5s'",
        "LOCK TABLE rol, usuario, funcion, rol_funcion IN SHARE ROW EXCLUSIVE MODE"]


def test_catalog_cli_success_repeat_no_prompts(monkeypatch, empty_factory, settings, capsys):
    seed_catalog_roles(empty_factory)
    engine = Mock()
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "create_database", lambda _: (engine, empty_factory))
    forbidden = Mock(side_effect=AssertionError("No prompts or passwords allowed"))
    monkeypatch.setattr(cli, "prompt_superadmin", forbidden)
    monkeypatch.setattr(cli, "Passwords", forbidden)
    for expected in ["5 funciones y 5 relaciones", "0 funciones y 0 relaciones"]:
        cli.main(["--crear-permisos-iniciales"])
        assert expected in capsys.readouterr().out
    forbidden.assert_not_called()
    assert engine.dispose.call_count == 2


def test_catalog_cli_missing_roles_disposes(monkeypatch, empty_factory, settings):
    engine = Mock()
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "create_database", lambda _: (engine, empty_factory))
    with pytest.raises(SystemExit, match="Falta un rol"):
        cli.main(["--crear-permisos-iniciales"])
    engine.dispose.assert_called_once()


@pytest.mark.parametrize("args", [[], ["--crear-permisos-iniciales", "--crear-superadmin"],
                                  ["--crear-permisos-iniciales", "--crear-rol-cliente"]])
def test_catalog_cli_requires_explicit_exclusive_mode(monkeypatch, args):
    forbidden = Mock(side_effect=AssertionError("Must not connect"))
    monkeypatch.setattr(cli, "Settings", forbidden)
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2
    forbidden.assert_not_called()


def test_catalog_login_identity_uses_assignments(empty_factory, data, passwords, settings):
    from app.core.dependencies import require_permission
    from app.core.errors import DomainError
    from app.modules.seguridad_accesos.schemas.auth import Login
    from app.modules.seguridad_accesos.services.auth import login

    run(empty_factory, data, passwords)
    run_catalog(empty_factory)
    credentials = Login(correo=data.correo, contrasena=data.contrasena)
    with empty_factory() as db:
        identity, _ = login(db, credentials, settings, passwords, None)
    assert identity.permisos == [code for code, _ in INITIAL_PERMISSIONS]
    for code, _ in INITIAL_PERMISSIONS:
        assert require_permission(code)(identity) is identity
    with pytest.raises(DomainError):
        require_permission("CU01")(identity)
    # Same role name, no assignment: no implicit SuperAdmin bypass.
    with empty_factory.begin() as db:
        db.delete(db.get(RolFuncion, ("superadmin", "CU05")))
    with empty_factory() as db:
        identity, _ = login(db, credentials, settings, passwords, None)
    with pytest.raises(DomainError):
        require_permission("CU05")(identity)
