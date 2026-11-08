"""Explicit tenant criteria for shared commerce queries; RLS is a second boundary."""
from sqlalchemy.orm import with_loader_criteria
from sqlalchemy.exc import IntegrityError
from app.core.errors import DomainError


def scoped(db, query, *models):
    if db.info.get("multitenant") is True:
        tenant = db.info.get("idemp") or 0
        for model in models:
            query = query.options(with_loader_criteria(model, model.idemp == tenant,
                                                       include_aliases=True))
    return query


def insert_identity(db, row):
    """Reuse current identity sequences without resetting shared seed counters.

    Only a primary-key collision is retried, inside a savepoint. All business
    changes and audit remain in the caller's enclosing transaction.
    """
    if db.info.get('multitenant') is not True or db.get_bind().dialect.name!='postgresql':
        db.add(row);db.flush();return row
    model=type(row)
    values={key:value for key,value in row.__dict__.items() if key!='_sa_instance_state'}
    for attempt in range(512):
        try:
            with db.begin_nested():
                fresh=model(**values)
                db.add(fresh);db.flush()
            return fresh
        except IntegrityError as error:
            if getattr(getattr(error.orig,'diag',None),'constraint_name',None)!=model.__tablename__+'_pkey':
                raise
    raise DomainError(409,'secuencia_no_disponible','No se pudo asignar el identificador de la operación')
