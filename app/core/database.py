from fastapi import Request
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def create_database(url: str):
    # Creating the engine does not connect or create tables.
    engine = create_engine(url, pool_pre_ping=True, echo=False, hide_parameters=True)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def get_db(request: Request):
    with request.app.state.session_factory() as db:
        try:
            db.info["multitenant"] = request.app.state.settings.multitenant_enabled
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


def tenant_context(db, idemp: int):
    if not isinstance(idemp, int) or idemp <= 0:
        raise ValueError("Empresa inválida")
    existing = db.info.get("idemp")
    if existing is not None and existing != idemp:
        raise ValueError("No se puede cambiar la empresa de una sesión")
    db.info["idemp"] = idemp
    if db.info.get("multitenant") and db.in_transaction() and is_postgresql(db):
        db.execute(text("SELECT set_config('app.idemp', :idemp, true)"), {"idemp": str(idemp)})


def is_postgresql(db) -> bool:
    """Indica si la sesión puede usar los procedimientos de la BD oficial."""
    return db.get_bind().dialect.name == "postgresql"


def sqlstate(exc) -> str | None:
    """Extrae SQLSTATE sin incluir texto ni parámetros sensibles del driver."""
    original = getattr(exc, "orig", None)
    return getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
