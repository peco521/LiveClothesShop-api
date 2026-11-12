from fastapi import Request
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def _normalize_supabase_url(url: str) -> str:
    # Supabase direct host db.<ref>.supabase.co is IPv6-only. On IPv4-only hosting (such as Render),
    # this causes connection timeouts (503 servicio_no_disponible). Automatically rewrite to IPv4 pooler.
    if "db.vleqtsrmaqvslyugurzj.supabase.co" in url:
        url = url.replace("db.vleqtsrmaqvslyugurzj.supabase.co", "aws-0-us-west-2.pooler.supabase.com")
        if "postgres:" in url and "postgres.vleqtsrmaqvslyugurzj:" not in url:
            url = url.replace("postgres:", "postgres.vleqtsrmaqvslyugurzj:", 1)
    return url


def create_database(url: str):
    # Preserve the configured endpoint, credentials, port and SSL options.
    # Rewriting a Supabase pooler URL to the direct host breaks IPv4-only hosts.
    # Creating the engine does not connect or create tables.
    # connect_timeout evita que una peticion quede colgada si el host de la BD
    # no responde (p. ej. pooler inaccesible desde la red local).
    url = _normalize_supabase_url(url)
    connect_args = {"connect_timeout": 5} if url.startswith("postgresql") else {}
    engine = create_engine(url, pool_pre_ping=True, echo=False, hide_parameters=True,
                           connect_args=connect_args)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def create_platform_database(url: str):
    """Motor del panel SaaS / aprovisionamiento.

    Usa un rol de base de datos con BYPASSRLS y no se expone a las rutas HTTP de
    empresa: `get_db` sigue entregando el motor del rol de aplicacion.
    """
    return create_database(url)


def get_db(request: Request):
    with request.app.state.session_factory() as db:
        try:
            db.info["multitenant"] = request.app.state.settings.multitenant_enabled
            yield db
        finally:
            db.rollback()


def get_platform_db(request: Request):
    """Sesion privilegiada para /api/saas. No se usa en rutas de empresa."""
    factory = getattr(request.app.state, "platform_session_factory", None)
    if factory is None:
        from app.core.errors import DomainError
        raise DomainError(503, "plataforma_no_configurada",
                          "La administracion de plataforma no esta configurada")
    with factory() as db:
        try:
            yield db
        finally:
            db.rollback()


@event.listens_for(Session, "after_begin")
def apply_tenant_on_transaction(session, transaction, connection):
    # SET LOCAL is transaction-scoped. Reapply after every commit/rollback.
    if (connection.dialect.name == "postgresql" and session.info.get("multitenant")
            and not transaction.nested and session.info.get("idemp") is not None):
        connection.execute(text("SELECT set_config('app.idemp', :idemp, true)"),
                           {"idemp": str(session.info["idemp"])})


@event.listens_for(Session, "before_flush")
def fill_tenant_on_pending_objects(session, flush_context, instances):
    """Completa `idemp` en altas del tenant antes de emitir el INSERT.

    Evita que cada repositorio tenga que recordar el tenant, mantiene el valor
    inmutable (los triggers lo revalidan) y funciona tambien tras commit o
    rollback porque toma el valor de `session.info`, no de la transaccion.
    """
    if not session.info.get("multitenant"):
        return
    idemp = session.info.get("idemp")
    if idemp is None:
        return
    for obj in session.new:
        table = getattr(obj, "__table__", None)
        if table is None or "idemp" not in table.c:
            continue
        if getattr(obj, "idemp", None) is None:
            obj.idemp = idemp


def tenant_context(db, idemp: int):
    if not isinstance(idemp, int) or idemp <= 0:
        raise ValueError("Empresa invÃ¡lida")
    existing = db.info.get("idemp")
    if existing is not None and existing != idemp:
        raise ValueError("No se puede cambiar la empresa de una sesiÃ³n")
    db.info["idemp"] = idemp
    if db.info.get("multitenant") and db.in_transaction() and is_postgresql(db):
        db.execute(text("SELECT set_config('app.idemp', :idemp, true)"), {"idemp": str(idemp)})


def is_postgresql(db) -> bool:
    """Indica si la sesiÃ³n puede usar los procedimientos de la BD oficial."""
    return db.get_bind().dialect.name == "postgresql"


def sqlstate(exc) -> str | None:
    """Extrae SQLSTATE sin incluir texto ni parÃ¡metros sensibles del driver."""
    original = getattr(exc, "orig", None)
    return getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
