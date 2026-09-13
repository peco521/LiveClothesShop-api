from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def create_database(url: str):
    # Creating the engine does not connect or create tables.
    engine = create_engine(url, pool_pre_ping=True, echo=False, hide_parameters=True)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def get_db(request: Request):
    with request.app.state.session_factory() as db:
        try:
            yield db
        finally:
            db.rollback()
