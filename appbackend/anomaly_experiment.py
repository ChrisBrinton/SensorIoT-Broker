"""anomaly_experiment.py — Evaluate anomaly detection algorithms and feature sets.

Run with MongoDB accessible on localhost:27017 (via SSH tunnel or local).

Usage:
    python3 anomaly_experiment.py [--gateway 140E71] [--db gdtechdb_prod]
"""

import argparse
import os
import sys
import time
import warnings
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from pymongo import MongoClient
from sklearn.covariance import EllipticEnvelope
from sklearn.ensemble import IsolationForest, RandomForestClassifier
from sklearn.metrics import f1_score, roc_auc_score
from sklearn.neighbors import LocalOutlierFactor
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.svm import OneClassSVM

warnings.filterwarnings("ignore")

# ---------------------------------------------------------------------------
# Constants (matching anomaly_training.py)
# ---------------------------------------------------------------------------

_LOOKBACK_DAYS = 90
_NOAA_NODE_ID = "noaa_forecast"
_BUCKET_CANDIDATES = (60, 120, 300, 600, 900, 1800, 3600)
_TREND_WINDOW = 6
_SAMPLE_RATIO = 2.0
_SAMPLE_DELTA = 0.05
_TEST_RATIO = 1.0
_ANOMALY_THRESHOLD = 0.5


# ---------------------------------------------------------------------------
# Data loading (replicates anomaly_training.py logic)
# ---------------------------------------------------------------------------

def _clean(v):
    try:
        return float(str(v).replace("b'", "").replace("'", ""))
    except (ValueError, TypeError):
        return float("nan")


def _optimal_bucket_seconds(df, node_ids):
    intervals = []
    for n in node_ids:
        times = np.sort(df[(df["node_id"] == n) & (df["type"] == "F")]["time"].values)
        if len(times) >= 2:
            intervals.append(float(np.median(np.diff(times))))
    if not intervals:
        return 60
    max_interval = max(intervals)
    for snap in _BUCKET_CANDIDATES:
        if snap >= max_interval:
            return snap
    return _BUCKET_CANDIDATES[-1]


def load_gateway_data(db, gateway_id, lookback_days=_LOOKBACK_DAYS):
    """Load and pivot gateway sensor data into wide format."""
    start_ts = time.time() - lookback_days * 86400
    rows = list(db.Sensors.find(
        {"gateway_id": gateway_id, "time": {"$gte": start_ts},
         "type": {"$in": ["F", "H", "P"]}},
        {"_id": 0, "node_id": 1, "type": 1, "value": 1, "time": 1},
    ))
    if not rows:
        return None

    df = pd.DataFrame(rows)
    df["value"] = df["value"].apply(_clean)
    df = df.dropna(subset=["value"])
    df["node_id"] = df["node_id"].astype(str)

    node_ids = sorted(df["node_id"].unique())
    real_node_ids = [n for n in node_ids if n != _NOAA_NODE_ID]
    bucket_secs = _optimal_bucket_seconds(df, real_node_ids)

    df["bucket"] = (df["time"] // bucket_secs).astype(int) * bucket_secs
    df["col"] = df["node_id"] + "_" + df["type"]

    pivoted = df.pivot_table(index="bucket", columns="col", values="value", aggfunc="first")
    pivoted.columns.name = None

    required = [f"{n}_{t}" for n in real_node_ids for t in ("F", "H")
                if f"{n}_{t}" in pivoted.columns]
    if not required:
        return None

    # Handle NOAA
    noaa_col = f"{_NOAA_NODE_ID}_F"
    noaa_doc = db.NOAASettings.find_one({"gateway_id": gateway_id, "enabled": True})
    if noaa_doc and noaa_col in pivoted.columns:
        pivoted[noaa_col] = pivoted[noaa_col].ffill().bfill()
    elif noaa_col in pivoted.columns:
        pivoted = pivoted.drop(columns=[noaa_col])

    result = pivoted.dropna(subset=required).reset_index(drop=False)
    result = result.rename(columns={"bucket": "time_rounded"})
    return result


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

def add_baseline_features(df):
    """Current production features: cyclic time + rolling delta/mean/std."""
    df = df.copy().sort_values("time_rounded").reset_index(drop=True)

    hours = (df["time_rounded"] % 86400) / 3600
    dows = ((df["time_rounded"] // 86400) % 7).astype(int)
    df["hour_sin"] = np.sin(2 * np.pi * hours / 24)
    df["hour_cos"] = np.cos(2 * np.pi * hours / 24)
    df["dow_sin"] = np.sin(2 * np.pi * dows / 7)
    df["dow_cos"] = np.cos(2 * np.pi * dows / 7)

    _meta = {"time_rounded", "hour_sin", "hour_cos", "dow_sin", "dow_cos"}
    sensor_cols = [c for c in df.columns if c not in _meta
                   and not c.startswith(_NOAA_NODE_ID) and "_" in c]
    for col in sensor_cols:
        df[f"{col}_delta"] = df[col].diff().fillna(0.0)
        df[f"{col}_roll_mean"] = df[col].rolling(_TREND_WINDOW, min_periods=1).mean()
        df[f"{col}_roll_std"] = df[col].rolling(_TREND_WINDOW, min_periods=1).std(ddof=0).fillna(0.0)

    return df


def add_seasonal_features(df):
    """Add month and week-of-year cyclic encoding."""
    ts = pd.to_datetime(df["time_rounded"], unit="s", utc=True)
    months = ts.dt.month - 1
    weeks = ts.dt.isocalendar().week.values.astype(float) - 1
    df["month_sin"] = np.sin(2 * np.pi * months / 12)
    df["month_cos"] = np.cos(2 * np.pi * months / 12)
    df["woy_sin"] = np.sin(2 * np.pi * weeks / 53)
    df["woy_cos"] = np.cos(2 * np.pi * weeks / 53)
    return df


def add_inter_sensor_diffs(df):
    """Add temperature differentials between sensor pairs."""
    _meta = {"time_rounded", "hour_sin", "hour_cos", "dow_sin", "dow_cos",
             "month_sin", "month_cos", "woy_sin", "woy_cos"}
    f_cols = [c for c in df.columns if c.endswith("_F") and c not in _meta
              and not c.startswith(_NOAA_NODE_ID)
              and "_delta" not in c and "_roll_" not in c and "_zscore" not in c]
    # Pairwise diffs for temperature sensors
    for i, c1 in enumerate(f_cols):
        for c2 in f_cols[i+1:]:
            df[f"diff_{c1}_{c2}"] = df[c1] - df[c2]
    return df


def add_rolling_zscore(df):
    """Add 24-bucket rolling z-score per sensor column."""
    _meta = {"time_rounded", "hour_sin", "hour_cos", "dow_sin", "dow_cos",
             "month_sin", "month_cos", "woy_sin", "woy_cos"}
    sensor_cols = [c for c in df.columns if c not in _meta
                   and not c.startswith(_NOAA_NODE_ID) and "_" in c
                   and "_delta" not in c and "_roll_" not in c
                   and "_zscore" not in c and "diff_" not in c]
    for col in sensor_cols:
        rm = df[col].rolling(24, min_periods=1).mean()
        rs = df[col].rolling(24, min_periods=1).std(ddof=0).replace(0, 1)
        df[f"{col}_zscore_24"] = (df[col] - rm) / rs
    return df


# ---------------------------------------------------------------------------
# Feature set definitions
# ---------------------------------------------------------------------------

def get_feature_sets():
    def _baseline(df):
        return add_baseline_features(df)

    def _baseline_seasonal(df):
        df = add_baseline_features(df)
        return add_seasonal_features(df)

    def _baseline_diffs(df):
        df = add_baseline_features(df)
        return add_inter_sensor_diffs(df)

    def _baseline_zscore(df):
        df = add_baseline_features(df)
        return add_rolling_zscore(df)

    def _enhanced(df):
        df = add_baseline_features(df)
        df = add_seasonal_features(df)
        df = add_inter_sensor_diffs(df)
        df = add_rolling_zscore(df)
        return df

    return [
        ("baseline", _baseline),
        ("+seasonal", _baseline_seasonal),
        ("+inter_diffs", _baseline_diffs),
        ("+zscore_24", _baseline_zscore),
        ("enhanced", _enhanced),
    ]


# ---------------------------------------------------------------------------
# Negative sampling utils (replicate madi logic)
# ---------------------------------------------------------------------------

def generate_negative_samples(pos_df, n_points, do_permute=False, delta=0.05):
    """Generate synthetic anomalous data points."""
    neg = pd.DataFrame()
    for col in pos_df.columns:
        if col == "class_label":
            continue
        if do_permute:
            neg[col] = np.random.permutation(pos_df[col].values[:n_points]
                                              if len(pos_df) >= n_points
                                              else np.random.choice(pos_df[col].values, n_points))
        else:
            lo = pos_df[col].min()
            hi = pos_df[col].max()
            rng = hi - lo
            neg[col] = np.random.uniform(lo - delta * rng, hi + delta * rng, n_points)
    neg["class_label"] = 0
    return neg


def normalize_df(df):
    """Return (normalized_df, means, stds)."""
    means = df.mean()
    stds = df.std().replace(0, 1)
    return (df - means) / stds, means, stds


# ---------------------------------------------------------------------------
# Detector wrappers (unified train/predict interface returning class_prob)
# ---------------------------------------------------------------------------

class IFDetector:
    """IsolationForest wrapper."""
    name = "IsolationForest"

    def __init__(self, contamination=0.05, random_state=42):
        self.model = IsolationForest(contamination=contamination, random_state=random_state)

    def train(self, x_train):
        self.model.fit(x_train)

    def predict_proba(self, x_test):
        preds = self.model.predict(x_test)
        return np.where(preds == 1, 1.0, 0.0)


class OCSVMDetector:
    """OneClassSVM wrapper with normalization."""
    name = "OneClassSVM"

    def __init__(self, nu=0.1):
        self.model = OneClassSVM(kernel="rbf", nu=nu, gamma="scale")
        self.scaler = StandardScaler()

    def train(self, x_train):
        x_scaled = self.scaler.fit_transform(x_train)
        self.model.fit(x_scaled)

    def predict_proba(self, x_test):
        x_scaled = self.scaler.transform(x_test)
        preds = self.model.predict(x_scaled)
        return np.where(preds == 1, 1.0, 0.0)


class NSRFDetector:
    """Negative-Sampling Random Forest."""
    name = "NS-RandomForest"

    def __init__(self, n_estimators=100, sample_ratio=2.0, sample_delta=0.05, random_state=42):
        self.model = RandomForestClassifier(n_estimators=n_estimators, random_state=random_state)
        self.sample_ratio = sample_ratio
        self.sample_delta = sample_delta
        self.scaler = StandardScaler()

    def train(self, x_train):
        x_scaled = pd.DataFrame(self.scaler.fit_transform(x_train), columns=x_train.columns)
        pos = x_scaled.copy()
        pos["class_label"] = 1
        n_neg = int(len(pos) * self.sample_ratio)
        neg = generate_negative_samples(x_scaled, n_neg, do_permute=False, delta=self.sample_delta)
        combined = pd.concat([pos, neg], ignore_index=True).sample(frac=1)
        self.model.fit(combined.drop(columns=["class_label"]), combined["class_label"])

    def predict_proba(self, x_test):
        x_scaled = self.scaler.transform(x_test)
        probs = self.model.predict_proba(x_scaled)
        # Column 1 = normal class probability
        return probs[:, 1]


class NSMLPDetector:
    """Negative-Sampling MLP (Neural Network) — sklearn replacement for TF NS-NN."""
    name = "NS-MLP"

    def __init__(self, hidden_layers=(128, 64, 32), sample_ratio=2.0, sample_delta=0.05,
                 random_state=42):
        self.model = MLPClassifier(
            hidden_layer_sizes=hidden_layers, activation="relu",
            max_iter=200, early_stopping=True, validation_fraction=0.15,
            random_state=random_state)
        self.sample_ratio = sample_ratio
        self.sample_delta = sample_delta
        self.scaler = StandardScaler()

    def train(self, x_train):
        x_scaled = pd.DataFrame(self.scaler.fit_transform(x_train), columns=x_train.columns)
        pos = x_scaled.copy()
        pos["class_label"] = 1
        n_neg = int(len(pos) * self.sample_ratio)
        neg = generate_negative_samples(x_scaled, n_neg, do_permute=False, delta=self.sample_delta)
        combined = pd.concat([pos, neg], ignore_index=True).sample(frac=1)
        self.model.fit(combined.drop(columns=["class_label"]), combined["class_label"])

    def predict_proba(self, x_test):
        x_scaled = self.scaler.transform(x_test)
        probs = self.model.predict_proba(x_scaled)
        return probs[:, 1]


class LOFDetector:
    """Local Outlier Factor (novelty detection mode)."""
    name = "LOF"

    def __init__(self, n_neighbors=20, contamination=0.05):
        self.model = LocalOutlierFactor(n_neighbors=n_neighbors, contamination=contamination,
                                         novelty=True)
        self.scaler = StandardScaler()

    def train(self, x_train):
        x_scaled = self.scaler.fit_transform(x_train)
        self.model.fit(x_scaled)

    def predict_proba(self, x_test):
        x_scaled = self.scaler.transform(x_test)
        preds = self.model.predict(x_scaled)
        return np.where(preds == 1, 1.0, 0.0)


class EllipticDetector:
    """Elliptic Envelope (robust covariance)."""
    name = "EllipticEnvelope"

    def __init__(self, contamination=0.05):
        self.model = EllipticEnvelope(contamination=contamination, support_fraction=0.9)
        self.scaler = StandardScaler()

    def train(self, x_train):
        x_scaled = self.scaler.fit_transform(x_train)
        self.model.fit(x_scaled)

    def predict_proba(self, x_test):
        x_scaled = self.scaler.transform(x_test)
        preds = self.model.predict(x_scaled)
        return np.where(preds == 1, 1.0, 0.0)


class EnsembleDetector:
    """Average class_prob across multiple detectors."""

    def __init__(self, detectors):
        self.detectors = detectors
        self.name = "Ensemble(" + "+".join(d.name for d in detectors) + ")"

    def train(self, x_train):
        for d in self.detectors:
            d.train(x_train)

    def predict_proba(self, x_test):
        probs = np.stack([d.predict_proba(x_test) for d in self.detectors])
        return probs.mean(axis=0)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate_detector(detector, x_train, x_test, y_test):
    """Train detector and evaluate on synthetic test set. Returns (auc, f1, time)."""
    t0 = time.time()
    try:
        detector.train(x_train)
        probs = detector.predict_proba(x_test)
        elapsed = time.time() - t0

        auc = roc_auc_score(y_test, probs)
        y_pred = (probs >= _ANOMALY_THRESHOLD).astype(int)
        f1 = f1_score(y_test, y_pred, pos_label=0, zero_division=0)
        return auc, f1, elapsed
    except Exception as e:
        elapsed = time.time() - t0
        print(f"    FAILED: {e}")
        return None, None, elapsed


def prepare_train_test(node_df, feature_cols, random_state=42):
    """80/20 split + synthetic negatives for test set."""
    np.random.seed(random_state)
    n_train = int(0.8 * len(node_df))
    shuffled = node_df.sample(frac=1, random_state=random_state).reset_index(drop=True)
    x_train = shuffled.iloc[:n_train][feature_cols]
    x_test_raw = shuffled.iloc[n_train:][feature_cols]

    # Synthetic test: real (label=1) + permuted negatives (label=0)
    pos_test = x_test_raw.copy()
    pos_test["class_label"] = 1
    neg_test = generate_negative_samples(x_test_raw, int(len(x_test_raw) * _TEST_RATIO),
                                          do_permute=True)
    test_combined = pd.concat([pos_test, neg_test], ignore_index=True).sample(frac=1,
                                                                               random_state=random_state)
    x_test = test_combined[feature_cols]
    y_test = test_combined["class_label"].values

    return x_train, x_test, y_test


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Anomaly detection experiment")
    parser.add_argument("--gateway", default="140E71")
    parser.add_argument("--db", default="gdtechdb_prod")
    parser.add_argument("--dbconn", default="localhost:27017")
    args = parser.parse_args()

    client = MongoClient(f"mongodb://{args.dbconn}/")
    db = client[args.db]

    print(f"Loading data for gateway {args.gateway}...")
    df_raw = load_gateway_data(db, args.gateway)
    if df_raw is None:
        print("No data. Exiting.")
        return

    print(f"Loaded {len(df_raw)} aligned rows, "
          f"{len([c for c in df_raw.columns if c != 'time_rounded'])} raw columns")
    print()

    feature_sets = get_feature_sets()

    # Define individual detectors
    def make_detectors():
        return [
            # Current production detectors
            IFDetector(),
            OCSVMDetector(),
            NSRFDetector(),
            # New detectors
            NSMLPDetector(hidden_layers=(128, 64, 32)),
            NSMLPDetector(hidden_layers=(64, 32)),
            LOFDetector(n_neighbors=20),
            EllipticDetector(),
        ]

    # ---- Phase 1: Individual detector x feature set experiments ----
    print("=" * 110)
    print("PHASE 1: Individual Detectors x Feature Sets")
    print("=" * 110)
    header = f"{'Features':<18} {'Detector':<22} {'AUC':>8} {'F1':>8} {'Time(s)':>8} {'#Feats':>7}"
    print(header)
    print("-" * len(header))

    all_results = []
    for feat_name, build_fn in feature_sets:
        df_feat = build_fn(df_raw)
        feat_cols = [c for c in df_feat.columns if c != "time_rounded"]

        # Clean: drop all-NaN, fill remaining, drop zero-variance
        df_clean = df_feat[feat_cols].copy()
        df_clean = df_clean.dropna(axis=1, how="all")
        df_clean = df_clean.fillna(df_clean.median())
        zero_var = df_clean.columns[df_clean.var() == 0].tolist()
        df_clean = df_clean.drop(columns=zero_var)
        clean_cols = df_clean.columns.tolist()

        x_train, x_test, y_test = prepare_train_test(df_clean, clean_cols)

        for det in make_detectors():
            auc, f1, elapsed = evaluate_detector(det, x_train, x_test, y_test)
            if auc is not None:
                result = {
                    "features": feat_name, "detector": det.name,
                    "auc": auc, "f1": f1, "time": elapsed, "n_feats": len(clean_cols),
                }
                all_results.append(result)
                print(f"{feat_name:<18} {det.name:<22} "
                      f"{auc:>8.4f} {f1:>8.4f} {elapsed:>8.2f} {len(clean_cols):>7}")

    # ---- Phase 2: Ensemble experiments on best feature set ----
    print()
    print("=" * 110)
    print("PHASE 2: Ensemble Experiments")
    print("=" * 110)

    # Find best feature set by average F1 across detectors
    feat_f1s = {}
    for r in all_results:
        feat_f1s.setdefault(r["features"], []).append(r["f1"])
    best_feat = max(feat_f1s, key=lambda k: np.mean(feat_f1s[k]))
    print(f"Using best feature set: {best_feat}")
    print()

    build_fn = dict(feature_sets)[best_feat]
    df_feat = build_fn(df_raw)
    feat_cols = [c for c in df_feat.columns if c != "time_rounded"]
    df_clean = df_feat[feat_cols].copy()
    df_clean = df_clean.dropna(axis=1, how="all")
    df_clean = df_clean.fillna(df_clean.median())
    zero_var = df_clean.columns[df_clean.var() == 0].tolist()
    df_clean = df_clean.drop(columns=zero_var)
    clean_cols = df_clean.columns.tolist()

    x_train, x_test, y_test = prepare_train_test(df_clean, clean_cols)

    ensembles = [
        # Current 3 (pick-best equivalent for comparison)
        EnsembleDetector([IFDetector(), OCSVMDetector(), NSRFDetector()]),
        # Current 3 + LOF
        EnsembleDetector([IFDetector(), OCSVMDetector(), NSRFDetector(), LOFDetector()]),
        # Current 3 + NS-MLP
        EnsembleDetector([IFDetector(), OCSVMDetector(), NSRFDetector(),
                          NSMLPDetector(hidden_layers=(128, 64, 32))]),
        # All detectors
        EnsembleDetector([IFDetector(), OCSVMDetector(), NSRFDetector(),
                          NSMLPDetector(hidden_layers=(128, 64, 32)),
                          LOFDetector(), EllipticDetector()]),
        # Best 3 from Phase 1
        # (determined dynamically below)
    ]

    # Find top 3 individual detectors on this feature set
    phase1_on_best = [r for r in all_results if r["features"] == best_feat]
    top3 = sorted(phase1_on_best, key=lambda r: r["f1"], reverse=True)[:3]
    top3_names = [r["detector"] for r in top3]
    print(f"Top 3 individual detectors: {top3_names}")

    det_map = {
        "IsolationForest": IFDetector,
        "OneClassSVM": OCSVMDetector,
        "NS-RandomForest": NSRFDetector,
        "NS-MLP": NSMLPDetector,
        "LOF": LOFDetector,
        "EllipticEnvelope": EllipticDetector,
    }
    top3_dets = []
    for name in top3_names:
        if name in det_map:
            top3_dets.append(det_map[name]())
    if top3_dets:
        ensembles.append(EnsembleDetector(top3_dets))

    header2 = f"{'Ensemble':<55} {'AUC':>8} {'F1':>8} {'Time(s)':>8}"
    print(header2)
    print("-" * len(header2))

    ensemble_results = []
    for ens in ensembles:
        auc, f1, elapsed = evaluate_detector(ens, x_train, x_test, y_test)
        if auc is not None:
            ensemble_results.append({
                "ensemble": ens.name, "auc": auc, "f1": f1, "time": elapsed,
            })
            print(f"{ens.name:<55} {auc:>8.4f} {f1:>8.4f} {elapsed:>8.2f}")

    # ---- Summary ----
    print()
    print("=" * 110)
    print("SUMMARY")
    print("=" * 110)

    if all_results:
        best_single = max(all_results, key=lambda r: r["f1"])
        print(f"Best individual: {best_single['features']} + {best_single['detector']} "
              f"(F1={best_single['f1']:.4f}, AUC={best_single['auc']:.4f})")

    if ensemble_results:
        best_ens = max(ensemble_results, key=lambda r: r["f1"])
        print(f"Best ensemble:   {best_ens['ensemble']} "
              f"(F1={best_ens['f1']:.4f}, AUC={best_ens['auc']:.4f})")

    # Compare current vs best
    current = [r for r in all_results
               if r["features"] == "baseline" and r["detector"] in ("IsolationForest", "OneClassSVM", "NS-RandomForest")]
    if current:
        best_current = max(current, key=lambda r: r["f1"])
        print()
        print(f"Current production best: baseline + {best_current['detector']} "
              f"(F1={best_current['f1']:.4f}, AUC={best_current['auc']:.4f})")
        overall_best_f1 = max(
            best_single["f1"],
            best_ens["f1"] if ensemble_results else 0
        )
        improvement = overall_best_f1 - best_current["f1"]
        print(f"Best experiment F1: {overall_best_f1:.4f} "
              f"(improvement: {'+' if improvement >= 0 else ''}{improvement:.4f})")


if __name__ == "__main__":
    main()
