from datetime import date, time
from decimal import Decimal
from uuid import UUID
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Id = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=15)]


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')
