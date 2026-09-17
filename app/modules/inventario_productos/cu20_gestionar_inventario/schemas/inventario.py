from typing import Literal

from pydantic import Field, model_validator
from app.modules.inventario_productos.cu18_gestionar_catalogo.schemas.catalogo import Input, texto


class MovimientoDatos(Input):
    nroSuc: int = Field(ge=1)
    idVariante: texto(15)
    tipoMov: Literal['entrada', 'salida', 'ajuste']
    cantidad: int = Field(gt=0, le=2147483647, strict=True)
    motivo: texto(255)
    ajusteDireccion: Literal['aumentar', 'disminuir'] | None = None

    @model_validator(mode='after')
    def adjustment(self):
        if self.tipoMov == 'ajuste' and self.ajusteDireccion is None:
            raise ValueError('Indica si el ajuste aumenta o disminuye las unidades')
        if self.tipoMov != 'ajuste' and self.ajusteDireccion is not None:
            raise ValueError('La dirección solo corresponde a un ajuste')
        return self
