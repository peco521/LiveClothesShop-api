from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.ventas_reportes.cu25_reportes.schemas.dinamicos import ReportPlan


class SaasReportScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idEmp: int | None = Field(default=None, ge=1, strict=True)
    idPlan: int | None = Field(default=None, ge=1, strict=True)


class SaasVoiceInterpretRequest(SaasReportScope):
    texto: str = Field(min_length=3, max_length=1200)


class SaasVoiceExecutionRequest(SaasReportScope):
    plan: ReportPlan


class SaasVoiceExportRequest(SaasVoiceExecutionRequest):
    formato: Literal["pdf", "xlsx"]


class SaasReportExportRequest(SaasReportScope):
    fechaIni: date
    fechaFin: date
    tipo: Literal["ventas", "inventario", "reservas", "devoluciones"]
    formato: Literal["pdf", "xlsx"]
