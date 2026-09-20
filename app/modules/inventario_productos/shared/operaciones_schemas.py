from datetime import date, time
from decimal import Decimal
from uuid import UUID
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
# `producto.idprod` es varchar(50) y CU18 lo genera como 'Prod-' + token_hex(10)
# (25 caracteres): el alias de prenda debe aceptar el identificador real del
# catálogo. `Id` conserva los 15 caracteres de `varianteprod.idvariante`.
ProductId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
Id = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=15)]


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')
