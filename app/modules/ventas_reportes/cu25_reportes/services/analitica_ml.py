"""Isolated synthetic ML demo. Never queries sales, customers, or tenant data.

Only load artifacts generated locally by scripts/train_fashionstore_ml.py:
joblib is a pickle format and must never receive an uploaded/untrusted file.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path


ARTIFACT_DIR = Path(__file__).resolve().parents[5] / "artifacts" / "ml"
CATEGORIES = ("camiseta", "pantalon", "vestido")
FEATURES = ("category", "day_of_week", "month", "price", "discount", "promotion")
LIMITATION = "Demostración sintética: no representa ventas reales ni precisión en producción."


def validate_features(features: Mapping) -> dict:
    """Reject missing/extra inputs, unknown categories, booleans and nonfinite values."""
    if not isinstance(features, Mapping) or set(features) != set(FEATURES):
        raise ValueError(f"Se requieren exactamente estos campos: {', '.join(FEATURES)}")
    if features["category"] not in CATEGORIES:
        raise ValueError("category no pertenece al catálogo sintético")
    result = {"category": features["category"]}
    for key, low, high in (
        ("day_of_week", 0, 6), ("month", 1, 12), ("price", 1, 500),
        ("discount", 0, 0.7), ("promotion", 0, 1),
    ):
        value = features[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} debe ser numérico")
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{key} fuera de rango [{low}, {high}]")
        if key in ("day_of_week", "month", "promotion") and value != int(value):
            raise ValueError(f"{key} debe ser entero")
        result[key] = value
    return result


class AnaliticaML:
    """Read-only service; artifact_dir is trusted deployment configuration, not user input."""

    def __init__(self, artifact_dir: Path = ARTIFACT_DIR):
        self.artifact_dir = Path(artifact_dir)
        self._bundle = None

    def summary(self) -> dict:
        path = self.artifact_dir / "metadata.json"
        if not path.is_file():
            return {"status": "not_trained", "synthetic": True, "limitation": LIMITATION}
        metadata = json.loads(path.read_text(encoding="utf-8"))
        return {"status": "ready", **metadata}

    def predict(self, features: Mapping) -> dict:
        validated = validate_features(features)
        if self._bundle is None:
            metadata = self.summary()
            if metadata["status"] != "ready":
                raise RuntimeError("Ejecute scripts/train_fashionstore_ml.py antes de predecir")
            model_path = self.artifact_dir / "model.joblib"
            if not model_path.is_file() or hashlib.sha256(model_path.read_bytes()).hexdigest() != metadata["model_sha256"]:
                raise RuntimeError("Artefacto ML ausente o checksum incorrecto")
            try:
                import joblib
                import sklearn
            except ImportError as exc:
                raise RuntimeError("Se requiere scikit-learn==1.7.2 (incluye numpy y joblib)") from exc
            if sklearn.__version__ != metadata["versions"]["scikit_learn"]:
                raise RuntimeError("Versión scikit-learn incompatible: reentrene el artefacto")
            self._bundle = joblib.load(model_path)
        row = [[validated[key] for key in FEATURES]]
        demand = max(0.0, float(self._bundle["demand"].predict(row)[0]))
        return {
            "synthetic": True, "predicted_daily_units": round(demand, 3),
            "features": validated, "limitation": LIMITATION,
        }


_service = AnaliticaML()


def summary() -> dict:
    return _service.summary()


def predict(features: Mapping) -> dict:
    return _service.predict(features)
