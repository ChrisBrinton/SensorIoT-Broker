"""Train and evaluate MADI anomaly detectors on per-node sensor data.

Preprocesses gdtechdb_prod.Sensors.csv into per-node (F, H) feature matrices,
generates negative-sampled datasets, trains all four detector types, evaluates
each with AUC, and writes results to ad/output/.

Usage:
    python ad/train_detectors.py
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from madi.detectors.isolation_forest_detector import IsolationForestAd
from madi.detectors.one_class_svm import OneClassSVMAd
from madi.detectors.neg_sample_random_forest import NegativeSamplingRandomForestAd
from madi.detectors.neg_sample_neural_net_detector import NegativeSamplingNeuralNetworkAD
import madi.utils.sample_utils as sample_utils
import madi.utils.evaluation_utils as evaluation_utils

DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "gdtechdb_prod.Sensors.csv")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
RANDOM_SEED = 42


def load_and_pivot(csv_path: str) -> dict:
    """Load long-format sensor CSV and pivot to per-node (F, H) DataFrames.

    Returns:
        Dict mapping node_id -> DataFrame with float columns [F, H].
    """
    df = pd.read_csv(csv_path)

    # Strip byte-string wrapper from value column: b'71.08' -> 71.08
    df["value"] = df["value"].astype(str).str.replace(r"b'([^']*)'", r"\1", regex=True)
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"])

    # Round timestamps to nearest second to pair F and H sent within ~35ms
    df["time_rounded"] = df["time"].round().astype(int)

    node_dfs = {}
    for node_id, group in df.groupby("node_id"):
        pivoted = group.pivot_table(
            index="time_rounded", columns="type", values="value", aggfunc="first"
        )
        if "F" not in pivoted.columns or "H" not in pivoted.columns:
            print(f"  Node {node_id}: missing F or H column, skipping.")
            continue
        pivoted = pivoted[["F", "H"]].dropna()
        pivoted = pivoted.reset_index(drop=True)
        node_dfs[node_id] = pivoted
        print(f"  Node {node_id}: {len(pivoted)} paired (F, H) readings")

    return node_dfs


def make_detectors(log_dir: str) -> dict:
    """Return a fresh dict of detector instances keyed by name."""
    return {
        "IsolationForest": IsolationForestAd(contamination=0.05, random_state=RANDOM_SEED),
        "OneClassSVM": OneClassSVMAd(nu=0.1),
        "NS-RandomForest": NegativeSamplingRandomForestAd(
            n_estimators=100,
            sample_ratio=2.0,
            sample_delta=0.05,
            random_state=RANDOM_SEED,
        ),
        "NS-NeuralNet": NegativeSamplingNeuralNetworkAD(
            sample_ratio=2.0,
            sample_delta=0.05,
            batch_size=64,
            steps_per_epoch=10,
            epochs=50,
            patience=5,
            dropout=0.2,
            learning_rate=0.001,
            layer_width=64,
            n_hidden_layers=2,
            log_dir=log_dir,
        ),
    }


def main():
    np.random.seed(RANDOM_SEED)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    logs_dir = os.path.join(OUTPUT_DIR, "logs")
    os.makedirs(logs_dir, exist_ok=True)

    print("=== Loading and pivoting sensor data ===")
    node_dfs = load_and_pivot(DATA_PATH)

    results = {}  # {node_id: {detector_name: auc}}

    for node_id, df in node_dfs.items():
        print(f"\n=== Node {node_id} ({len(df)} samples) ===")

        # --- Train / test split (80/20) ---
        n_train = int(0.8 * len(df))
        shuffled = df.sample(frac=1, random_state=RANDOM_SEED).reset_index(drop=True)
        x_train = shuffled.iloc[:n_train].copy()
        x_test_raw = shuffled.iloc[n_train:].copy()

        # --- Generate and save negative-sampled dataset ---
        print(f"  Generating negative-sampled dataset (ratio=2.0, delta=0.05)…")
        neg_sampled = sample_utils.apply_negative_sample(
            x_train.copy(), sample_ratio=2.0, sample_delta=0.05
        )
        out_path = os.path.join(OUTPUT_DIR, f"node_{node_id}_negative_sampled.csv")
        neg_sampled.to_csv(out_path, index=False)
        pos = int((neg_sampled["class_label"] == 1).sum())
        neg = int((neg_sampled["class_label"] == 0).sum())
        print(f"  Saved {out_path}  ({pos} real, {neg} synthetic)")

        # --- Build a labeled test set with synthetic negatives ---
        # get_train_data samples n_points/(sample_ratio+1) positive rows without
        # replacement, so cap n_test_points to len(x_test_raw) * (sample_ratio+1).
        test_sample_ratio = 1.0
        # n_pos = n_points / (sample_ratio + 1) must not exceed len(x_test_raw)
        n_test_points = int(len(x_test_raw) * (1 + test_sample_ratio))
        x_test, y_test = sample_utils.get_train_data(
            x_test_raw, n_points=n_test_points, sample_ratio=test_sample_ratio, do_permute=True
        )

        # --- Train and evaluate each detector ---
        node_results = {}
        detectors = make_detectors(os.path.join(logs_dir, f"node_{node_id}"))

        for det_name, detector in detectors.items():
            print(f"  Training {det_name}…", end=" ", flush=True)
            try:
                detector.train_model(x_train.copy())
                result_df = detector.predict(x_test.copy())
                auc = evaluation_utils.compute_auc(
                    y_test.values, result_df["class_prob"].values
                )
                node_results[det_name] = round(auc, 4)
                print(f"AUC = {auc:.4f}")
            except Exception as exc:
                print(f"FAILED: {exc}")
                node_results[det_name] = None

        results[node_id] = node_results

    # --- Print summary table ---
    print("\n=== AUC Results ===")
    detector_names = list(next(iter(results.values())).keys())
    header = f"{'Node':>6} | " + " | ".join(f"{d:>16}" for d in detector_names)
    print(header)
    print("-" * len(header))
    for node_id, scores in sorted(results.items()):
        row = f"{node_id:>6} | " + " | ".join(
            f"{scores[d]:>16.4f}" if scores[d] is not None else f"{'N/A':>16}"
            for d in detector_names
        )
        print(row)

    # --- Save results CSV ---
    results_df = pd.DataFrame(results).T
    results_df.index.name = "node_id"
    results_path = os.path.join(OUTPUT_DIR, "results.csv")
    results_df.to_csv(results_path)
    print(f"\nSaved results to {results_path}")


if __name__ == "__main__":
    main()
