from typing import Annotated

from pydantic import ConfigDict, StringConstraints

from app.modules.seguridad_accesos.schemas.auth import Registro


class SuperAdminInput(Registro):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    cod_adm: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=10)]
