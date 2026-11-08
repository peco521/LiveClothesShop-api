"""Reproducible, offline synthetic demo. No SQL, network, real sales or tenant inputs.

Run from the API root: python scripts/train_fashionstore_ml.py
Requires scikit-learn==1.7.2 (numpy, scipy, joblib installed transitively).
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import platform
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.modules.ventas_reportes.cu25_reportes.services.analitica_ml import (
    ARTIFACT_DIR, CATEGORIES, FEATURES, LIMITATION,
)


def generate_dataset(seed: int = 25, days: int = 540) -> list[dict]:
    import numpy as np

    rng = np.random.default_rng(seed)
    records = []
    for day in range(days):
        current = date(2024, 1, 1) + timedelta(days=day)
        for category_index, category in enumerate(CATEGORIES):
            price = float(round(rng.uniform(20, 120), 2))
            discount = float(rng.choice([0.0, 0.1, 0.2, 0.3]))
            promotion = int(rng.binomial(1, 0.25))
            mean = (42 + category_index * 9 - 0.23 * price + 45 * discount
                    + 13 * promotion + (12 if current.weekday() >= 5 else 0)
                    + 7 * np.sin(2 * np.pi * current.month / 12))
            units = max(0, int(round(mean + rng.normal(0, 3))))
            records.append({
                "date": current.isoformat(), "category": category,
                "day_of_week": current.weekday(), "month": current.month,
                "price": price, "discount": discount, "promotion": promotion,
                "units": units,
            })
    return records


def train(output_dir: Path = ARTIFACT_DIR, seed: int = 25) -> dict:
    import joblib
    import numpy as np
    import scipy
    import sklearn
    from sklearn.cluster import KMeans
    from sklearn.compose import ColumnTransformer
    from sklearn.dummy import DummyRegressor
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score, silhouette_score
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    records = generate_dataset(seed)
    # Split whole dates, never rows randomly: all categories share the same cutoff.
    cutoff = (date(2024, 1, 1) + timedelta(days=432)).isoformat()
    train_rows = [row for row in records if row["date"] < cutoff]
    eval_rows = [row for row in records if row["date"] >= cutoff]
    x_train = [[row[key] for key in FEATURES] for row in train_rows]
    x_eval = [[row[key] for key in FEATURES] for row in eval_rows]
    y_train = [row["units"] for row in train_rows]
    y_eval = [row["units"] for row in eval_rows]
    preprocessing = ColumnTransformer([
        ("category", OneHotEncoder(handle_unknown="ignore", sparse_output=False), [0]),
        ("numeric", StandardScaler(), list(range(1, len(FEATURES)))),
    ])
    model = Pipeline([
        ("preprocessing", preprocessing),
        ("regressor", GradientBoostingRegressor(n_estimators=80, max_depth=2, random_state=seed)),
    ])
    model.fit(x_train, y_train)
    baseline = DummyRegressor(strategy="mean").fit(np.zeros((len(y_train), 1)), y_train)

    def metrics(predicted):
        return {"mae": float(mean_absolute_error(y_eval, predicted)),
                "rmse": float(np.sqrt(mean_squared_error(y_eval, predicted))),
                "r2": float(r2_score(y_eval, predicted))}

    cluster_features = ["price", "discount", "promotion", "units"]
    cluster_train = [[row[key] for key in cluster_features] for row in train_rows]
    cluster_eval = [[row[key] for key in cluster_features] for row in eval_rows]
    scaler = StandardScaler().fit(cluster_train)
    clusters = KMeans(n_clusters=3, n_init=10, random_state=seed).fit(scaler.transform(cluster_train))
    eval_scaled = scaler.transform(cluster_eval)
    labels = clusters.predict(eval_scaled)
    unique_labels = np.unique(labels)
    silhouette = (float(silhouette_score(eval_scaled, labels))
                  if 1 < len(unique_labels) < len(labels) else None)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    model_path = output_dir / "model.joblib"
    joblib.dump({"demand": model, "cluster_scaler": scaler, "clusters": clusters}, model_path, compress=3)
    metadata = {
        "schema_version": 1, "synthetic": True, "seed": seed, "limitation": LIMITATION,
        "reproducibility": "Fixed seed and versions reproduce data, predictions and metrics; pickle bytes may differ",
        "provenance": {
            "source": "Local procedural generator generate_dataset; no external data or real sales",
            "dataset_license": "CC0-1.0", "external_dataset": False,
            "generator": "scripts/train_fashionstore_ml.py::generate_dataset",
            "dataset_sha256": hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest(),
        },
        "versions": {"python": platform.python_version(), "scikit_learn": sklearn.__version__,
                     "numpy": np.__version__, "scipy": scipy.__version__, "joblib": joblib.__version__},
        "features": list(FEATURES), "target": "synthetic daily units per category",
        "split": {"method": "chronological_holdout", "train_rows": len(train_rows),
                  "eval_rows": len(eval_rows), "train_start": train_rows[0]["date"],
                  "train_end": train_rows[-1]["date"], "eval_start": eval_rows[0]["date"],
                  "eval_end": eval_rows[-1]["date"], "preprocessing_fit": "train_only",
                  "target_or_future_features": False},
        "supervised": {"algorithm": "GradientBoostingRegressor", "metrics": metrics(model.predict(x_eval)),
                       "baseline": {"algorithm": "train_mean", "metrics": metrics(baseline.predict(np.zeros((len(y_eval), 1))))}},
        "unsupervised": {"algorithm": "StandardScaler + KMeans", "fit": "train_only",
                         "features": cluster_features, "n_clusters": 3,
                         "eval_silhouette": silhouette,
                         "eval_counts": [int(np.sum(labels == i)) for i in range(3)],
                         "centers_original_units": scaler.inverse_transform(clusters.cluster_centers_).tolist(),
                         "interpretation": "Unordered synthetic demand patterns; not customer segments or forecasts"},
        "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        "artifact_safety": "Trusted local joblib only; checksum detects corruption, not malicious replacement",
    }
    (output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    return metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ARTIFACT_DIR)
    parser.add_argument("--seed", type=int, default=25)
    args = parser.parse_args()
    result = train(args.output_dir, args.seed)
    print(json.dumps({"synthetic": True, "split": result["split"], "supervised": result["supervised"]}, indent=2))
