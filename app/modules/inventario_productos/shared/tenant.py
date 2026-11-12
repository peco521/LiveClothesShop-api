"""Explicit tenant boundaries for administrative inventory queries (RLS is additional)."""
from sqlalchemy import select
from sqlalchemy.orm import with_loader_criteria

from app.core.database import Base
from app.core.errors import DomainError


def tenant_id(db):
    info = getattr(db, 'info', None)
    if not isinstance(info, dict) or info.get('multitenant') is not True:
        return None
    tenant = info.get('idemp')
    if not isinstance(tenant, int) or tenant <= 0:
        raise DomainError(403, 'empresa_requerida', 'La sesión requiere una empresa válida')
    return tenant


def select_for(db, *entities):
    query = select(*entities)
    tenant = tenant_id(db)
    if tenant is not None:
        # Local to this statement: no global Session listeners or changes to other modules.
        for mapper in Base.registry.mappers:
            model = mapper.class_
            if hasattr(model, 'idemp'):
                query = query.options(with_loader_criteria(model, model.idemp == tenant,
                                                           include_aliases=True))
    return query


def legacy_write_only(db):
    if tenant_id(db) is not None:
        raise DomainError(503, 'operacion_no_habilitada',
                          'Las escrituras administrativas de este módulo aún no están habilitadas en SaaS')
