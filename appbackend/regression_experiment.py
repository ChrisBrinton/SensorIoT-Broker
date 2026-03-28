"""regression_experiment.py — Evaluate feature engineering and model combinations
for indoor climate regression forecasting.

Run with MongoDB accessible on localhost:27017 (via SSH tunnel or local).

Usage:
    python3 regression_experiment.py [--gateway 140E71] [--node 1] [--type F]
"""

import argparse
import json
import time
import warnings
from datetime import datetime

import numpy as np
import pandas as pd
from pymongo import MongoClient
from sklearn.ensemble import (
    ExtraTreesRegressor,
    GradientBoostingRegressor,
    HistGradientBoostingRegressor,
    RandomForestRegressor,
)
from sklearn.linear_model import ElasticNet, Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVR

warnings.filterwarnings("ignore", category=UserWarning)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOAA_NODE_ID = "noaa_forecast"
_CV_SPLITS = 5
_MIN_ROWS = 100


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _clean_value(v) -> float:
    try:
        return float(str(v).replace("b'", "").replace("'", ""))
    except (ValueError, TypeError):
        return float("nan")


def load_sensor_data(db, gateway_id, node_id, sensor_type):
    """Load hourly-bucketed sensor data with NOAA and sibling sensor."""
    rows = list(db.Sensors.find(
        {"gateway_id": gateway_id, "node_id": str(node_id), "type": sensor_type},
        {"_id": 0, "value": 1, "time": 1},
    ))
    if len(rows) < _MIN_ROWS:
        return None

    df = pd.DataFrame(rows)
    df["value"] = df["value"].apply(_clean_value)
    df = df.dropna(subset=["value"])
    df["hour_bucket"] = (df["time"] // 3600).astype(int) * 3600
    df = (df.groupby("hour_bucket")["value"]
            .mean().reset_index()
            .rename(columns={"value": "sensor_value"}))
    df = df.sort_values("hour_bucket").reset_index(drop=True)

    # NOAA outdoor temperature
    noaa_rows = list(db.Sensors.find(
        {"gateway_id": gateway_id, "node_id": _NOAA_NODE_ID, "type": "F"},
        {"_id": 0, "value": 1, "time": 1},
    ))
    if noaa_rows:
        ndf = pd.DataFrame(noaa_rows)
        ndf["value"] = ndf["value"].apply(_clean_value)
        ndf = ndf.dropna(subset=["value"])
        ndf["hour_bucket"] = (ndf["time"] // 3600).astype(int) * 3600
        ndf = (ndf.groupby("hour_bucket")["value"]
                   .first().reset_index()
                   .rename(columns={"value": "noaa_temp_f"}))
        df = df.merge(ndf, on="hour_bucket", how="left")
    else:
        df["noaa_temp_f"] = float("nan")

    # Sibling sensor (H when predicting F, F when predicting H)
    sibling_type = "H" if sensor_type == "F" else "F"
    sib_rows = list(db.Sensors.find(
        {"gateway_id": gateway_id, "node_id": str(node_id), "type": sibling_type},
        {"_id": 0, "value": 1, "time": 1},
    ))
    if sib_rows:
        sdf = pd.DataFrame(sib_rows)
        sdf["value"] = sdf["value"].apply(_clean_value)
        sdf = sdf.dropna(subset=["value"])
        sdf["hour_bucket"] = (sdf["time"] // 3600).astype(int) * 3600
        sdf = (sdf.groupby("hour_bucket")["value"]
                   .mean().reset_index()
                   .rename(columns={"value": f"sibling_{sibling_type}"}))
        df = df.merge(sdf, on="hour_bucket", how="left")

    return df


# ---------------------------------------------------------------------------
# Feature engineering functions
# ---------------------------------------------------------------------------

def add_time_features(df):
    """Cyclic hour-of-day and day-of-week."""
    hours = (df["hour_bucket"] % 86400) / 3600
    dows = ((df["hour_bucket"] // 86400) % 7).astype(int)
    df["hour_sin"] = np.sin(2 * np.pi * hours / 24)
    df["hour_cos"] = np.cos(2 * np.pi * hours / 24)
    df["dow_sin"] = np.sin(2 * np.pi * dows / 7)
    df["dow_cos"] = np.cos(2 * np.pi * dows / 7)
    return df


def add_seasonal_features(df):
    """Month and week-of-year cyclic encoding."""
    ts = pd.to_datetime(df["hour_bucket"], unit="s", utc=True)
    months = ts.dt.month - 1  # 0-11
    weeks = ts.dt.isocalendar().week.values.astype(float) - 1  # 0-52
    df["month_sin"] = np.sin(2 * np.pi * months / 12)
    df["month_cos"] = np.cos(2 * np.pi * months / 12)
    df["woy_sin"] = np.sin(2 * np.pi * weeks / 53)
    df["woy_cos"] = np.cos(2 * np.pi * weeks / 53)
    return df


def add_lag_features(df, lags=(1, 2, 3, 6, 12, 24)):
    """Autoregressive lag features from sensor_value."""
    for lag in lags:
        df[f"lag_{lag}"] = df["sensor_value"].shift(lag)
    return df


def add_rolling_features(df, windows=(6, 12, 24)):
    """Rolling mean and std of sensor_value."""
    for w in windows:
        df[f"roll_mean_{w}"] = df["sensor_value"].rolling(w, min_periods=1).mean()
        df[f"roll_std_{w}"] = (df["sensor_value"].rolling(w, min_periods=1)
                                .std(ddof=0).fillna(0.0))
    return df


def add_cross_sensor_features(df):
    """Include sibling sensor columns if present."""
    # Sibling columns are already in df from load_sensor_data
    # Add a lag-1 of sibling for temporal context
    for col in df.columns:
        if col.startswith("sibling_"):
            df[f"{col}_lag1"] = df[col].shift(1)
    return df


# ---------------------------------------------------------------------------
# Experiment definitions
# ---------------------------------------------------------------------------

def get_feature_sets():
    """Define feature set experiments. Each returns (name, build_fn)."""
    def _build_baseline(df):
        df = add_time_features(df.copy())
        return df, ["hour_sin", "hour_cos", "dow_sin", "dow_cos"]

    def _build_baseline_noaa(df):
        df = add_time_features(df.copy())
        cols = ["hour_sin", "hour_cos", "dow_sin", "dow_cos"]
        if df["noaa_temp_f"].notna().mean() >= 0.5:
            df["noaa_temp_f"] = df["noaa_temp_f"].ffill().bfill()
            cols = ["noaa_temp_f"] + cols
        return df, cols

    def _build_lags(df):
        df = add_time_features(df.copy())
        df = add_lag_features(df)
        cols = ["hour_sin", "hour_cos", "dow_sin", "dow_cos"]
        if df["noaa_temp_f"].notna().mean() >= 0.5:
            df["noaa_temp_f"] = df["noaa_temp_f"].ffill().bfill()
            cols = ["noaa_temp_f"] + cols
        lag_cols = [c for c in df.columns if c.startswith("lag_")]
        cols = cols + lag_cols
        return df, cols

    def _build_rolling(df):
        df = add_time_features(df.copy())
        df = add_rolling_features(df)
        cols = ["hour_sin", "hour_cos", "dow_sin", "dow_cos"]
        if df["noaa_temp_f"].notna().mean() >= 0.5:
            df["noaa_temp_f"] = df["noaa_temp_f"].ffill().bfill()
            cols = ["noaa_temp_f"] + cols
        roll_cols = [c for c in df.columns if c.startswith("roll_")]
        cols = cols + roll_cols
        return df, cols

    def _build_seasonal(df):
        df = add_time_features(df.copy())
        df = add_seasonal_features(df)
        cols = ["hour_sin", "hour_cos", "dow_sin", "dow_cos",
                "month_sin", "month_cos", "woy_sin", "woy_cos"]
        if df["noaa_temp_f"].notna().mean() >= 0.5:
            df["noaa_temp_f"] = df["noaa_temp_f"].ffill().bfill()
            cols = ["noaa_temp_f"] + cols
        return df, cols

    def _build_cross_sensor(df):
        df = add_time_features(df.copy())
        df = add_cross_sensor_features(df)
        cols = ["hour_sin", "hour_cos", "dow_sin", "dow_cos"]
        if df["noaa_temp_f"].notna().mean() >= 0.5:
            df["noaa_temp_f"] = df["noaa_temp_f"].ffill().bfill()
            cols = ["noaa_temp_f"] + cols
        sib_cols = [c for c in df.columns
                    if c.startswith("sibling_") and not c.endswith("_lag1")]
        sib_lag_cols = [c for c in df.columns if c.endswith("_lag1")]
        cols = cols + sib_cols + sib_lag_cols
        return df, cols

    def _build_combined(df):
        df = add_time_features(df.copy())
        df = add_seasonal_features(df)
        df = add_lag_features(df)
        df = add_rolling_features(df)
        df = add_cross_sensor_features(df)
        cols = ["hour_sin", "hour_cos", "dow_sin", "dow_cos",
                "month_sin", "month_cos", "woy_sin", "woy_cos"]
        if df["noaa_temp_f"].notna().mean() >= 0.5:
            df["noaa_temp_f"] = df["noaa_temp_f"].ffill().bfill()
            cols = ["noaa_temp_f"] + cols
        lag_cols = sorted([c for c in df.columns if c.startswith("lag_")])
        roll_cols = sorted([c for c in df.columns if c.startswith("roll_")])
        sib_cols = sorted([c for c in df.columns if c.startswith("sibling_")])
        cols = cols + lag_cols + roll_cols + sib_cols
        return df, cols

    def _build_combined_no_lags(df):
        """Combined features minus lags — for direct multi-step forecasting."""
        df = add_time_features(df.copy())
        df = add_seasonal_features(df)
        df = add_rolling_features(df)
        df = add_cross_sensor_features(df)
        cols = ["hour_sin", "hour_cos", "dow_sin", "dow_cos",
                "month_sin", "month_cos", "woy_sin", "woy_cos"]
        if df["noaa_temp_f"].notna().mean() >= 0.5:
            df["noaa_temp_f"] = df["noaa_temp_f"].ffill().bfill()
            cols = ["noaa_temp_f"] + cols
        roll_cols = sorted([c for c in df.columns if c.startswith("roll_")])
        sib_cols = sorted([c for c in df.columns if c.startswith("sibling_")])
        cols = cols + roll_cols + sib_cols
        return df, cols

    return [
        ("baseline", _build_baseline),
        ("baseline+noaa", _build_baseline_noaa),
        ("+lags", _build_lags),
        ("+rolling", _build_rolling),
        ("+seasonal", _build_seasonal),
        ("+cross_sensor", _build_cross_sensor),
        ("combined", _build_combined),
        ("combined_no_lags", _build_combined_no_lags),
    ]


def get_models():
    """Model variants to test."""
    return [
        # Current models (baseline comparison)
        ("Ridge_1.0", Ridge, {"alpha": 1.0}),
        ("RF_d8", RandomForestRegressor,
         {"n_estimators": 100, "max_depth": 8, "random_state": 42}),
        ("GBR_d3", GradientBoostingRegressor,
         {"n_estimators": 100, "learning_rate": 0.1, "max_depth": 3, "random_state": 42}),
        # New models
        ("HistGBR_d4", HistGradientBoostingRegressor,
         {"max_iter": 200, "max_depth": 4, "learning_rate": 0.1, "random_state": 42}),
        ("HistGBR_d6", HistGradientBoostingRegressor,
         {"max_iter": 200, "max_depth": 6, "learning_rate": 0.05, "random_state": 42}),
        ("ExtraTrees_d8", ExtraTreesRegressor,
         {"n_estimators": 200, "max_depth": 8, "random_state": 42}),
        ("ElasticNet", ElasticNet, {"alpha": 0.1, "l1_ratio": 0.5}),
        ("SVR_C1", SVR, {"kernel": "rbf", "C": 1.0}),
        ("SVR_C10", SVR, {"kernel": "rbf", "C": 10.0}),
    ]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def run_cv_experiment(df, feature_cols, model_name, model_cls, model_params):
    """Run TimeSeriesSplit CV and return metrics."""
    work = df.dropna(subset=feature_cols + ["sensor_value"]).copy()
    if len(work) < _MIN_ROWS:
        return None

    X = work[feature_cols].values
    y = work["sensor_value"].values.astype(np.float64)

    # SVR is slow — limit to 5000 rows
    if model_cls == SVR and len(X) > 5000:
        X = X[-5000:]
        y = y[-5000:]

    tscv = TimeSeriesSplit(n_splits=_CV_SPLITS)
    r2s, rmses, maes = [], [], []

    t0 = time.time()
    for train_idx, val_idx in tscv.split(X):
        X_tr, X_val = X[train_idx], X[val_idx]
        y_tr, y_val = y[train_idx], y[val_idx]

        if model_cls in (HistGradientBoostingRegressor,):
            pipe = Pipeline([("model", model_cls(**model_params))])
        else:
            pipe = Pipeline([
                ("scaler", StandardScaler()),
                ("model", model_cls(**model_params)),
            ])
        pipe.fit(X_tr, y_tr)
        y_pred = pipe.predict(X_val)

        r2s.append(r2_score(y_val, y_pred))
        rmses.append(np.sqrt(mean_squared_error(y_val, y_pred)))
        maes.append(mean_absolute_error(y_val, y_pred))

    elapsed = time.time() - t0
    return {
        "r2": np.mean(r2s),
        "r2_std": np.std(r2s),
        "rmse": np.mean(rmses),
        "mae": np.mean(maes),
        "time": elapsed,
    }


def run_multistep_test(df_raw, feature_cols, model_cls, model_params, has_lags, horizon=48):
    """Hold out last `horizon` hours and evaluate multi-step forecast accuracy.

    For models with lag features: recursive prediction (feed predictions back).
    For models without: direct prediction.
    """
    work = df_raw.copy()
    if len(work) < horizon + _MIN_ROWS:
        return None

    train = work.iloc[:-horizon].dropna(subset=feature_cols + ["sensor_value"])
    test = work.iloc[-horizon:]

    if len(train) < _MIN_ROWS:
        return None

    X_train = train[feature_cols].values
    y_train = train["sensor_value"].values.astype(np.float64)

    if model_cls in (HistGradientBoostingRegressor,):
        pipe = Pipeline([("model", model_cls(**model_params))])
    else:
        pipe = Pipeline([
            ("scaler", StandardScaler()),
            ("model", model_cls(**model_params)),
        ])
    pipe.fit(X_train, y_train)

    if not has_lags:
        # Direct: predict all hours at once
        X_test = test[feature_cols].fillna(0).values
        preds = pipe.predict(X_test)
    else:
        # Recursive: step-by-step, feeding predictions back as lags
        recent_values = list(train["sensor_value"].values[-24:])
        preds = []
        for i in range(len(test)):
            row = test.iloc[i:i+1].copy()
            # Update lag features from recent_values
            lag_map = {1: -1, 2: -2, 3: -3, 6: -6, 12: -12, 24: -24}
            for lag, idx in lag_map.items():
                col = f"lag_{lag}"
                if col in feature_cols and abs(idx) <= len(recent_values):
                    row[col] = recent_values[idx]

            # Update rolling features from recent_values
            recent_arr = np.array(recent_values)
            for w in (6, 12, 24):
                mc = f"roll_mean_{w}"
                sc = f"roll_std_{w}"
                if mc in feature_cols:
                    window = recent_arr[-w:] if len(recent_arr) >= w else recent_arr
                    row[mc] = np.mean(window)
                if sc in feature_cols:
                    window = recent_arr[-w:] if len(recent_arr) >= w else recent_arr
                    row[sc] = np.std(window, ddof=0)

            X_row = row[feature_cols].fillna(0).values
            pred = pipe.predict(X_row)[0]
            preds.append(pred)
            recent_values.append(pred)

    actuals = test["sensor_value"].values[:len(preds)]
    preds = np.array(preds[:len(actuals)])

    return {
        "ms_r2": r2_score(actuals, preds),
        "ms_rmse": np.sqrt(mean_squared_error(actuals, preds)),
        "ms_mae": mean_absolute_error(actuals, preds),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Regression model experiment")
    parser.add_argument("--gateway", default="140E71")
    parser.add_argument("--node", default="1")
    parser.add_argument("--type", default="F", dest="sensor_type")
    parser.add_argument("--db", default="gdtechdb_prod")
    parser.add_argument("--dbconn", default="localhost:27017")
    args = parser.parse_args()

    client = MongoClient(f"mongodb://{args.dbconn}/")
    db = client[args.db]

    print(f"Loading data for {args.gateway}/{args.node}/{args.sensor_type}...")
    df_raw = load_sensor_data(db, args.gateway, args.node, args.sensor_type)
    if df_raw is None:
        print("Not enough data. Exiting.")
        return

    noaa_pct = df_raw["noaa_temp_f"].notna().mean() * 100
    sib_cols = [c for c in df_raw.columns if c.startswith("sibling_")]
    print(f"Loaded {len(df_raw)} hourly rows, NOAA coverage: {noaa_pct:.1f}%, "
          f"sibling columns: {sib_cols}")
    print()

    feature_sets = get_feature_sets()
    models = get_models()

    # ---- Phase 1: Cross-validation experiments ----
    print("=" * 100)
    print("PHASE 1: Cross-Validation Experiments")
    print("=" * 100)
    header = f"{'Experiment':<22} {'Model':<20} {'R2':>8} {'R2_std':>8} {'RMSE':>8} {'MAE':>8} {'Time(s)':>8}"
    print(header)
    print("-" * len(header))

    results = []
    for feat_name, build_fn in feature_sets:
        df_feat, feat_cols = build_fn(df_raw)

        for model_name, model_cls, model_params in models:
            metrics = run_cv_experiment(df_feat, feat_cols, model_name, model_cls, model_params)
            if metrics is None:
                continue
            results.append({
                "experiment": feat_name,
                "model": model_name,
                **metrics,
            })
            print(f"{feat_name:<22} {model_name:<20} "
                  f"{metrics['r2']:>8.4f} {metrics['r2_std']:>8.4f} "
                  f"{metrics['rmse']:>8.4f} {metrics['mae']:>8.4f} "
                  f"{metrics['time']:>8.2f}")

    # ---- Phase 2: Multi-step forecast test on top candidates ----
    print()
    print("=" * 100)
    print("PHASE 2: 48-Hour Multi-Step Forecast Test (top 10 by CV R2)")
    print("=" * 100)

    top_results = sorted(results, key=lambda r: r["r2"], reverse=True)[:10]
    header2 = f"{'Experiment':<22} {'Model':<20} {'CV_R2':>8} {'MS_R2':>8} {'MS_RMSE':>8} {'MS_MAE':>8}"
    print(header2)
    print("-" * len(header2))

    ms_results = []
    for res in top_results:
        feat_name = res["experiment"]
        model_name = res["model"]

        # Rebuild features
        build_fn = dict(feature_sets)[feat_name]
        df_feat, feat_cols = build_fn(df_raw)

        # Find model params
        model_cls, model_params = None, None
        for mn, mc, mp in models:
            if mn == model_name:
                model_cls, model_params = mc, mp
                break

        has_lags = any(c.startswith("lag_") for c in feat_cols)
        ms = run_multistep_test(df_feat, feat_cols, model_cls, model_params, has_lags)
        if ms is None:
            continue

        ms_results.append({**res, **ms})
        print(f"{feat_name:<22} {model_name:<20} "
              f"{res['r2']:>8.4f} {ms['ms_r2']:>8.4f} "
              f"{ms['ms_rmse']:>8.4f} {ms['ms_mae']:>8.4f}")

    # ---- Summary ----
    print()
    print("=" * 100)
    print("SUMMARY")
    print("=" * 100)

    if results:
        best_cv = max(results, key=lambda r: r["r2"])
        print(f"Best CV R2:        {best_cv['experiment']} + {best_cv['model']} "
              f"(R2={best_cv['r2']:.4f}, RMSE={best_cv['rmse']:.4f})")

    if ms_results:
        best_ms = max(ms_results, key=lambda r: r["ms_r2"])
        print(f"Best 48h forecast: {best_ms['experiment']} + {best_ms['model']} "
              f"(MS_R2={best_ms['ms_r2']:.4f}, MS_RMSE={best_ms['ms_rmse']:.4f})")

        # Check if lags help or hurt for multi-step
        lag_results = [r for r in ms_results
                       if any(c.startswith("lag_") for c in
                              dict(feature_sets)[r["experiment"]](df_raw)[1])]
        nolag_results = [r for r in ms_results if r not in lag_results]

        if lag_results and nolag_results:
            best_lag = max(lag_results, key=lambda r: r["ms_r2"])
            best_nolag = max(nolag_results, key=lambda r: r["ms_r2"])
            print()
            print(f"Best with lags (recursive):    {best_lag['experiment']} + {best_lag['model']} "
                  f"MS_R2={best_lag['ms_r2']:.4f}")
            print(f"Best without lags (direct):    {best_nolag['experiment']} + {best_nolag['model']} "
                  f"MS_R2={best_nolag['ms_r2']:.4f}")
            if best_lag["ms_r2"] > best_nolag["ms_r2"]:
                print(">> Recursive lag-based forecasting WINS for 48h horizon")
            else:
                print(">> Direct forecasting (no lags) WINS for 48h horizon")


if __name__ == "__main__":
    main()
