"""Standalone ML tests: no database, ASGI server or inherited conftest fixtures.

python -m pytest --noconftest tests/test_autonomo_ml.py -q
"""
import json

import pytest

pytest.importorskip("sklearn")

from app.modules.ventas_reportes.cu25_reportes.services.analitica_ml import AnaliticaML, validate_features
from scripts.train_fashionstore_ml import generate_dataset, train


VALID = {"category": "camiseta", "day_of_week": 5, "month": 6,
         "price": 45.0, "discount": 0.2, "promotion": 1}


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    path = tmp_path_factory.mktemp("synthetic_ml")
    return path, train(path)


def test_temporal_evaluation_and_baseline(trained):
    _, metadata = trained
    split = metadata["split"]
    assert split["train_end"] < split["eval_start"]
    assert split["train_rows"] + split["eval_rows"] == 1620
    assert split["preprocessing_fit"] == "train_only"
    score = metadata["supervised"]
    assert 0 < score["metrics"]["mae"] < score["baseline"]["metrics"]["mae"]
    assert -1 <= metadata["unsupervised"]["eval_silhouette"] <= 1
    assert sum(metadata["unsupervised"]["eval_counts"]) == split["eval_rows"]


def test_predict_and_summary(trained):
    path, metadata = trained
    service = AnaliticaML(path)
    assert service.summary()["synthetic"] is True
    result = service.predict(VALID)
    assert result["predicted_daily_units"] >= 0
    assert result["synthetic"] is True
    assert result == service.predict(VALID)
    assert metadata["provenance"]["external_dataset"] is False


def test_reproducible_training(trained, tmp_path):
    import numpy as np
    path, first = trained
    second = train(tmp_path)
    # Parallel BLAS/OpenMP reduction order may change a centroid by a few ULPs.
    # Keep dataset/splits/labels/counts/metrics exact, except these floating-point
    # reductions. Serialization bytes were never the reproducibility contract.
    assert {k: v for k, v in first.items() if k not in {"model_sha256", "unsupervised"}} == {
        k: v for k, v in second.items() if k not in {"model_sha256", "unsupervised"}
    }
    a, b = first['unsupervised'], second['unsupervised']
    floating = {'centers_original_units', 'eval_silhouette'}
    assert {k:v for k,v in a.items() if k not in floating} == {k:v for k,v in b.items() if k not in floating}
    np.testing.assert_allclose(a['centers_original_units'], b['centers_original_units'], rtol=1e-12, atol=1e-12)
    assert a['eval_silhouette'] == pytest.approx(b['eval_silhouette'], rel=1e-12, abs=1e-12)
    assert AnaliticaML(path).predict(VALID) == AnaliticaML(tmp_path).predict(VALID)
    assert generate_dataset(25, 5) == generate_dataset(25, 5)
    assert generate_dataset(25, 5) != generate_dataset(26, 5)


@pytest.mark.parametrize("change", [
    {"category": "unknown"}, {"price": -1}, {"price": float("nan")},
    {"discount": float("inf")}, {"month": 1.5}, {"promotion": True},
    {"day_of_week": 7}, {"tenant_id": "must-not-be-accepted"}, {"price": "45"},
])
def test_invalid_inputs(change):
    with pytest.raises(ValueError):
        validate_features({**VALID, **change})


def test_missing_features():
    with pytest.raises(ValueError):
        validate_features({})


def test_missing_or_tampered_artifact(tmp_path):
    service = AnaliticaML(tmp_path)
    assert service.summary()["status"] == "not_trained"
    with pytest.raises(RuntimeError):
        service.predict(VALID)
    (tmp_path / "metadata.json").write_text(json.dumps({"model_sha256": "wrong"}))
    (tmp_path / "model.joblib").write_bytes(b"not trusted")
    with pytest.raises(RuntimeError, match="checksum"):
        service.predict(VALID)
