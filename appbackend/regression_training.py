"""regression_training.py — per-sensor regression models for predicting indoor climate.

Trains one supervised regression model per (gateway_id, node_id, type) using:
  - All available historical sensor readings (no lookback cap)
  - NOAA outdoor temperature (when available) as a key predictor
  - Cyclic temporal features (hour-of-day, day-of-week, month, week-of-year)
  - Rolling statistics (mean/std over 6, 12, 24 hour windows)
  - Sibling sensor data (e.g. humidity when predicting temperature)

Multiple regression algorithms × hyperparameter variants are trained using
TimeSeriesSplit cross-validation; the best variant by mean R² is selected and
refitted on the full dataset.

Saved models can predict indoor readings for future NOAA forecast hours.
"""

import json
import logging
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import (
    GradientBoostingRegressor,
    HistGradientBoostingRegressor,
    RandomForestRegressor,
)
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

# Re-use NOAA constants and backfill function from anomaly_training
import anomaly_training as _at

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

_log_fmt = logging.Formatter(
    '%(asctime)s  %(levelname)-8s  %(name)s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.setLevel(logging.DEBUG)
_stdout_handler.setFormatter(_log_fmt)
logger.addHandler(_stdout_handler)

_log_file = os.path.join(os.path.dirname(__file__), 'regression_training.log')
_file_handler = logging.FileHandler(_log_file)
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(_log_fmt)
logger.addHandler(_file_handler)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODELS_DIR              = _at.MODELS_DIR   # same top-level models/ directory
_REGRESSION_SUBDIR      = 'regression'     # models/{gw}/regression/
_TYPES_TO_PREDICT       = ('F', 'H')       # sensor types to train regression for
_MIN_ROWS               = 100              # skip sensor if fewer qualifying rows
_CV_SPLITS              = 5               # TimeSeriesSplit cross-validation folds
_NOAA_COVERAGE_THRESHOLD = 0.5            # fraction of rows with valid NOAA to include it
_NOAA_BACKFILL_DAYS     = 365             # NOAA history backfill window for training
_ROLLING_WINDOWS        = (6, 12, 24)     # hours for rolling mean/std features

# Hyperparameter grid: all variants trained; winner by mean CV R² is kept.
_REGRESSION_GRID: List[Tuple] = [
    # Ridge regression — strong with rolling/seasonal features
    (Ridge,                          {'alpha': 0.1}),
    (Ridge,                          {'alpha': 1.0}),
    (Ridge,                          {'alpha': 10.0}),
    # Gradient Boosting
    (GradientBoostingRegressor,      {'n_estimators': 100, 'learning_rate': 0.1,  'max_depth': 3, 'random_state': 42}),
    (GradientBoostingRegressor,      {'n_estimators': 200, 'learning_rate': 0.05, 'max_depth': 3, 'random_state': 42}),
    # HistGradientBoosting — handles NaN natively, fast
    (HistGradientBoostingRegressor,  {'max_iter': 200, 'max_depth': 4, 'learning_rate': 0.1,  'random_state': 42}),
    (HistGradientBoostingRegressor,  {'max_iter': 200, 'max_depth': 6, 'learning_rate': 0.05, 'random_state': 42}),
    # Random Forest
    (RandomForestRegressor,          {'n_estimators': 100, 'max_depth': 8, 'random_state': 42}),
]

# Physical value ranges for clipping predictions
_VALUE_RANGES = {
    'F': (20.0, 130.0),   # Fahrenheit
    'H': (0.0, 100.0),    # Humidity %
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_value(v) -> float:
    """Convert stored sensor values (incl. legacy b'...' strings) to float."""
    try:
        return float(str(v).replace("b'", '').replace("'", ''))
    except (ValueError, TypeError):
        return float('nan')


def _add_time_features(df: pd.DataFrame, ts_col: str = 'hour_bucket') -> pd.DataFrame:
    """Add cyclic hour-of-day and day-of-week sin/cos features in-place."""
    hours = (df[ts_col] % 86400) / 3600              # float 0-24
    dows  = ((df[ts_col] // 86400) % 7).astype(int)  # int 0-6
    df['hour_sin'] = np.sin(2 * np.pi * hours / 24)
    df['hour_cos'] = np.cos(2 * np.pi * hours / 24)
    df['dow_sin']  = np.sin(2 * np.pi * dows  / 7)
    df['dow_cos']  = np.cos(2 * np.pi * dows  / 7)
    return df


def _add_seasonal_features(df: pd.DataFrame, ts_col: str = 'hour_bucket') -> pd.DataFrame:
    """Add cyclic month and week-of-year sin/cos features."""
    ts = pd.to_datetime(df[ts_col], unit='s', utc=True)
    months = ts.dt.month - 1  # 0-11
    weeks = ts.dt.isocalendar().week.values.astype(float) - 1  # 0-52
    df['month_sin'] = np.sin(2 * np.pi * months / 12)
    df['month_cos'] = np.cos(2 * np.pi * months / 12)
    df['woy_sin']   = np.sin(2 * np.pi * weeks / 53)
    df['woy_cos']   = np.cos(2 * np.pi * weeks / 53)
    return df


def _add_rolling_features(df: pd.DataFrame, value_col: str = 'sensor_value',
                          windows: tuple = _ROLLING_WINDOWS) -> pd.DataFrame:
    """Add rolling mean and std features for the sensor value column."""
    for w in windows:
        df[f'roll_mean_{w}'] = df[value_col].rolling(w, min_periods=1).mean()
        df[f'roll_std_{w}']  = (df[value_col].rolling(w, min_periods=1)
                                 .std(ddof=0).fillna(0.0))
    return df


def _regression_dir(gateway_id: str, models_dir: str = MODELS_DIR) -> str:
    return os.path.join(models_dir, str(gateway_id), _REGRESSION_SUBDIR)


def _model_path(gateway_id: str, node_id: str, sensor_type: str,
                models_dir: str = MODELS_DIR) -> str:
    return os.path.join(_regression_dir(gateway_id, models_dir),
                        f'{node_id}_{sensor_type}.joblib')


def _meta_path(gateway_id: str, node_id: str, sensor_type: str,
               models_dir: str = MODELS_DIR) -> str:
    return os.path.join(_regression_dir(gateway_id, models_dir),
                        f'{node_id}_{sensor_type}_meta.json')


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_hourly_series(db, gateway_id: str, node_id: str, sensor_type: str,
                        col_name: str = 'sensor_value') -> Optional[pd.DataFrame]:
    """Load a single sensor series, bucketed to hourly means."""
    try:
        rows = list(db.Sensors.find(
            {'gateway_id': gateway_id, 'node_id': str(node_id), 'type': sensor_type},
            {'_id': 0, 'value': 1, 'time': 1},
        ))
    except Exception as exc:
        logger.warning('MongoDB query failed for %s/%s/%s: %s',
                       gateway_id, node_id, sensor_type, exc)
        return None

    if not rows:
        return None

    df = pd.DataFrame(rows)
    df['value'] = df['value'].apply(_clean_value)
    df = df.dropna(subset=['value'])
    if df.empty:
        return None

    df['hour_bucket'] = (df['time'] // 3600).astype(int) * 3600
    return (df.groupby('hour_bucket')['value']
              .mean().reset_index()
              .rename(columns={'value': col_name}))


def get_sensor_dataframe(
    db,
    gateway_id: str,
    node_id: str,
    sensor_type: str,
) -> Optional[Tuple[pd.DataFrame, float]]:
    """Load all historical readings for one sensor with enriched features.

    No lookback cap — all Sensors collection records are used to maximise
    training data.  Each reading is rounded to the nearest hour bucket;
    multiple readings in the same hour are averaged.

    Features added:
      - NOAA outdoor temperature (when available)
      - Cyclic time (hour, day-of-week, month, week-of-year)
      - Rolling statistics (mean/std over 6, 12, 24 hour windows)
      - Sibling sensor value (H when training F, F when training H)

    Returns (df, noaa_coverage) or None if fewer than _MIN_ROWS rows.
    """
    # --- Load primary sensor ---
    df = _load_hourly_series(db, gateway_id, node_id, sensor_type)
    if df is None or len(df) < _MIN_ROWS:
        logger.info('Skipping %s/%s/%s: insufficient data',
                    gateway_id, node_id, sensor_type)
        return None

    # --- Load sibling sensor (H↔F) ---
    sibling_type = 'H' if sensor_type == 'F' else 'F'
    sibling_col = f'sibling_{sibling_type}'
    sib_df = _load_hourly_series(db, gateway_id, node_id, sibling_type, sibling_col)
    if sib_df is not None:
        df = df.merge(sib_df, on='hour_bucket', how='left')

    # --- Load NOAA hourly records for this gateway ---
    try:
        noaa_rows = list(db.Sensors.find(
            {'gateway_id': gateway_id, 'node_id': _at._NOAA_NODE_ID, 'type': 'F'},
            {'_id': 0, 'value': 1, 'time': 1},
        ))
    except Exception as exc:
        logger.warning('NOAA query failed for gateway %s: %s', gateway_id, exc)
        noaa_rows = []

    if noaa_rows:
        noaa_df = pd.DataFrame(noaa_rows)
        noaa_df['value'] = noaa_df['value'].apply(_clean_value)
        noaa_df = noaa_df.dropna(subset=['value'])
        noaa_df['hour_bucket'] = (noaa_df['time'] // 3600).astype(int) * 3600
        noaa_df = (noaa_df.groupby('hour_bucket')['value']
                          .first()
                          .reset_index()
                          .rename(columns={'value': 'noaa_temp_f'}))
        df = df.merge(noaa_df, on='hour_bucket', how='left')
    else:
        df['noaa_temp_f'] = float('nan')

    df = df.sort_values('hour_bucket').reset_index(drop=True)

    # --- Add features ---
    df = _add_time_features(df, ts_col='hour_bucket')
    df = _add_seasonal_features(df, ts_col='hour_bucket')
    df = _add_rolling_features(df, value_col='sensor_value')

    noaa_coverage = float(df['noaa_temp_f'].notna().mean())
    logger.info('%s/%s/%s: %d hour-buckets, NOAA coverage=%.1f%%',
                gateway_id, node_id, sensor_type, len(df), noaa_coverage * 100)
    return df, noaa_coverage


# ---------------------------------------------------------------------------
# Training & model selection
# ---------------------------------------------------------------------------

def train_regression_for_sensor(
    df: pd.DataFrame,
    noaa_coverage: float,
) -> Tuple[Pipeline, str, dict, float, float, float, List[str], float, list]:
    """Train all hyperparameter variants and return the best pipeline.

    Uses TimeSeriesSplit CV to preserve temporal ordering.  The variant with
    the highest mean R² across folds is refitted on the full dataset.

    Returns:
        (pipeline, model_name, best_params, mean_r2, mean_rmse, mean_mae,
         feature_columns, noaa_mean, recent_values)
    """
    has_noaa = noaa_coverage >= _NOAA_COVERAGE_THRESHOLD

    # Build feature column list
    features = ['hour_sin', 'hour_cos', 'dow_sin', 'dow_cos',
                'month_sin', 'month_cos', 'woy_sin', 'woy_cos']
    if has_noaa:
        features = ['noaa_temp_f'] + features

    # Rolling features
    for w in _ROLLING_WINDOWS:
        features.append(f'roll_mean_{w}')
        features.append(f'roll_std_{w}')

    # Sibling sensor
    for col in df.columns:
        if col.startswith('sibling_'):
            features.append(col)

    X = df[features].copy()
    y = df['sensor_value'].values.astype(np.float64)

    # Impute noaa NaNs with column mean
    noaa_mean = float(X['noaa_temp_f'].mean()) if has_noaa else 0.0
    if has_noaa:
        X['noaa_temp_f'] = X['noaa_temp_f'].fillna(noaa_mean)

    # Forward-fill sibling NaNs, then fill remaining with mean
    for col in features:
        if col.startswith('sibling_'):
            X[col] = X[col].ffill().bfill()
            col_mean = X[col].mean()
            X[col] = X[col].fillna(col_mean if not np.isnan(col_mean) else 0.0)

    tscv = TimeSeriesSplit(n_splits=_CV_SPLITS)
    results = {}

    for model_cls, params in _REGRESSION_GRID:
        variant_key = f'{model_cls.__name__}_{json.dumps(params, sort_keys=True)}'
        cv_r2s, cv_rmses, cv_maes = [], [], []
        try:
            for train_idx, val_idx in tscv.split(X):
                X_tr,  X_val  = X.iloc[train_idx], X.iloc[val_idx]
                y_tr,  y_val  = y[train_idx],       y[val_idx]

                if model_cls == HistGradientBoostingRegressor:
                    pipe = Pipeline([('model', model_cls(**params))])
                else:
                    pipe = Pipeline([
                        ('scaler', StandardScaler()),
                        ('model',  model_cls(**params)),
                    ])
                pipe.fit(X_tr, y_tr)
                y_pred = pipe.predict(X_val)
                cv_r2s.append(float(r2_score(y_val, y_pred)))
                cv_rmses.append(float(np.sqrt(mean_squared_error(y_val, y_pred))))
                cv_maes.append(float(mean_absolute_error(y_val, y_pred)))

            mean_r2   = float(np.mean(cv_r2s))
            mean_rmse = float(np.mean(cv_rmses))
            mean_mae  = float(np.mean(cv_maes))
            results[variant_key] = {
                'model_cls': model_cls, 'params': params,
                'mean_r2': mean_r2, 'mean_rmse': mean_rmse, 'mean_mae': mean_mae,
            }
            logger.info('  %-65s R²=%+.4f  RMSE=%.4f  MAE=%.4f',
                        variant_key, mean_r2, mean_rmse, mean_mae)
        except Exception as exc:
            logger.warning('  Variant %s failed: %s', variant_key, exc)

    if not results:
        raise RuntimeError('All regression variants failed during cross-validation')

    best_key  = max(results, key=lambda k: results[k]['mean_r2'])
    best      = results[best_key]
    model_display_name = best['model_cls'].__name__
    logger.info('Best variant: %s (R²=%.4f  RMSE=%.4f  MAE=%.4f)',
                best_key, best['mean_r2'], best['mean_rmse'], best['mean_mae'])

    # Final refit on full dataset
    if best['model_cls'] == HistGradientBoostingRegressor:
        final_pipe = Pipeline([('model', best['model_cls'](**best['params']))])
    else:
        final_pipe = Pipeline([
            ('scaler', StandardScaler()),
            ('model',  best['model_cls'](**best['params'])),
        ])
    final_pipe.fit(X, y)

    # Store recent sensor values for rolling features at prediction time
    recent_values = df['sensor_value'].values[-max(_ROLLING_WINDOWS):].tolist()

    return (
        final_pipe, model_display_name, best['params'],
        best['mean_r2'], best['mean_rmse'], best['mean_mae'],
        features, noaa_mean, recent_values,
    )


# ---------------------------------------------------------------------------
# Model persistence
# ---------------------------------------------------------------------------

def save_regression_model(
    gateway_id: str, node_id: str, sensor_type: str,
    pipeline: Pipeline, model_name: str, best_params: dict,
    r2: float, rmse: float, mae: float,
    feature_columns: List[str], has_noaa: bool, noaa_mean: float,
    num_rows: int, recent_values: list,
    sibling_mean: Optional[float] = None,
    models_dir: str = MODELS_DIR,
) -> None:
    reg_dir = _regression_dir(gateway_id, models_dir)
    os.makedirs(reg_dir, exist_ok=True)

    joblib.dump(pipeline, _model_path(gateway_id, node_id, sensor_type, models_dir))

    meta = {
        'node_id':         node_id,
        'type':            sensor_type,
        'model_type':      model_name,
        'best_params':     best_params,
        'r2':              round(r2,   4),
        'rmse':            round(rmse, 4),
        'mae':             round(mae,  4),
        'feature_columns': feature_columns,
        'has_noaa':        has_noaa,
        'noaa_mean':       noaa_mean,
        'num_rows':        num_rows,
        'trained_at':      time.time(),
        'feature_version': 2,
        'recent_values':   [round(v, 4) for v in recent_values],
        'sibling_mean':    sibling_mean,
    }
    with open(_meta_path(gateway_id, node_id, sensor_type, models_dir), 'w') as f:
        json.dump(meta, f)

    logger.info('Saved regression model %s/%s/%s: %s R²=%.4f RMSE=%.4f MAE=%.4f rows=%d',
                gateway_id, node_id, sensor_type, model_name, r2, rmse, mae, num_rows)


def load_regression_model(
    gateway_id: str, node_id: str, sensor_type: str,
    models_dir: str = MODELS_DIR,
) -> Tuple[Pipeline, dict]:
    """Load (pipeline, metadata). Raises FileNotFoundError if absent."""
    pipeline = joblib.load(_model_path(gateway_id, node_id, sensor_type, models_dir))
    with open(_meta_path(gateway_id, node_id, sensor_type, models_dir)) as f:
        metadata = json.load(f)
    return pipeline, metadata


def regression_model_exists(
    gateway_id: str,
    node_id: Optional[str] = None,
    sensor_type: Optional[str] = None,
    models_dir: str = MODELS_DIR,
) -> bool:
    """Return True if any (or the specific) regression model exists."""
    if node_id and sensor_type:
        return os.path.isfile(_model_path(gateway_id, node_id, sensor_type, models_dir))
    reg_dir = _regression_dir(gateway_id, models_dir)
    return (os.path.isdir(reg_dir) and
            any(f.endswith('.joblib') for f in os.listdir(reg_dir)))


def load_all_regression_metadata(
    gateway_id: str,
    models_dir: str = MODELS_DIR,
) -> List[dict]:
    """Return a list of all per-sensor metadata dicts for a gateway."""
    reg_dir = _regression_dir(gateway_id, models_dir)
    if not os.path.isdir(reg_dir):
        return []
    metas = []
    for fname in sorted(os.listdir(reg_dir)):
        if fname.endswith('_meta.json'):
            try:
                with open(os.path.join(reg_dir, fname)) as f:
                    metas.append(json.load(f))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning('Failed to read %s: %s', fname, exc)
    return metas


# ---------------------------------------------------------------------------
# Gateway-level orchestration (called from server.py background thread)
# ---------------------------------------------------------------------------

def train_regression_for_gateway(
    gateway_id: str,
    db,
    models_dir: str = MODELS_DIR,
) -> List[Dict]:
    """Train per-sensor regression models for all nodes/types in a gateway.

    Uses all available historical sensor data (no lookback cap).  If NOAA is
    configured for the gateway, backfills _NOAA_BACKFILL_DAYS of observations
    first so outdoor temp is available as a predictor feature.
    """
    # Backfill NOAA history if enabled for this gateway
    noaa_doc = db.NOAASettings.find_one({'gateway_id': gateway_id, 'enabled': True})
    if noaa_doc and noaa_doc.get('lat') is not None and noaa_doc.get('lon') is not None:
        logger.info('Gateway %s: backfilling NOAA history (%d days)',
                    gateway_id, _NOAA_BACKFILL_DAYS)
        _at._backfill_noaa_history(
            db, gateway_id,
            float(noaa_doc['lat']), float(noaa_doc['lon']),
            _NOAA_BACKFILL_DAYS,
        )

    # Discover all unique (node_id, type) pairs present in the Sensors collection
    try:
        agg = list(db.Sensors.aggregate([
            {'$match': {'gateway_id': gateway_id,
                        'type': {'$in': list(_TYPES_TO_PREDICT)},
                        'node_id': {'$ne': _at._NOAA_NODE_ID}}},
            {'$group': {'_id': {'node_id': '$node_id', 'type': '$type'}}},
        ]))
    except Exception as exc:
        logger.error('Aggregation failed for gateway %s: %s', gateway_id, exc)
        return [{'gateway_id': gateway_id, 'status': 'failed', 'error': str(exc)}]

    pairs = [(doc['_id']['node_id'], doc['_id']['type']) for doc in agg]

    if not pairs:
        logger.info('Gateway %s: no eligible (node, type) pairs found', gateway_id)
        return [{'gateway_id': gateway_id, 'status': 'skipped',
                 'reason': 'no eligible sensor pairs'}]

    logger.info('Gateway %s: training regression for %d pairs: %s',
                gateway_id, len(pairs), pairs)

    all_results = []
    for node_id, sensor_type in pairs:
        result = get_sensor_dataframe(db, gateway_id, node_id, sensor_type)
        if result is None:
            all_results.append({
                'gateway_id': gateway_id, 'node_id': node_id,
                'type': sensor_type, 'status': 'skipped',
                'reason': f'fewer than {_MIN_ROWS} rows',
            })
            continue

        df, noaa_coverage = result
        try:
            logger.info('Training %s/%s/%s (%d rows, NOAA=%.1f%%)',
                        gateway_id, node_id, sensor_type,
                        len(df), noaa_coverage * 100)
            (pipeline, model_name, best_params,
             mean_r2, mean_rmse, mean_mae,
             features, noaa_mean, recent_values) = train_regression_for_sensor(
                df, noaa_coverage)

            has_noaa = noaa_coverage >= _NOAA_COVERAGE_THRESHOLD

            # Compute sibling mean for prediction fallback
            sibling_mean = None
            for col in df.columns:
                if col.startswith('sibling_'):
                    sibling_mean = round(float(df[col].mean()), 4)
                    break

            save_regression_model(
                gateway_id, node_id, sensor_type,
                pipeline, model_name, best_params,
                mean_r2, mean_rmse, mean_mae, features,
                has_noaa, noaa_mean, len(df), recent_values,
                sibling_mean, models_dir,
            )
            all_results.append({
                'gateway_id': gateway_id, 'node_id': node_id,
                'type': sensor_type, 'status': 'done',
                'model_type': model_name,
                'r2':         round(mean_r2,   4),
                'rmse':       round(mean_rmse, 4),
                'mae':        round(mean_mae,  4),
                'has_noaa':   has_noaa,
                'num_rows':   len(df),
            })
        except Exception as exc:
            logger.error('Training failed for %s/%s/%s: %s',
                         gateway_id, node_id, sensor_type, exc)
            all_results.append({
                'gateway_id': gateway_id, 'node_id': node_id,
                'type': sensor_type, 'status': 'failed', 'error': str(exc),
            })

    return all_results


# ---------------------------------------------------------------------------
# Forecasting
# ---------------------------------------------------------------------------

def predict_sensor_forecast(
    gateway_id: str,
    node_id: str,
    sensor_type: str,
    db,
    hours: int = 48,
    models_dir: str = MODELS_DIR,
) -> List[dict]:
    """Predict future sensor values for the given forecast horizon.

    For v2 models (feature_version=2): uses rolling stats computed from
    recent sensor values stored at training time, plus seasonal and NOAA
    features for direct multi-step prediction.

    For v1 models (legacy): uses the original time + NOAA features only.

    Returns [{'timestamp': float, 'predicted': float}, ...].
    Empty list if no model exists.
    """
    if not regression_model_exists(gateway_id, node_id, sensor_type, models_dir):
        return []

    pipeline, meta = load_regression_model(gateway_id, node_id, sensor_type, models_dir)
    feature_version = meta.get('feature_version', 1)
    has_noaa     = meta.get('has_noaa', False)
    feature_cols = meta.get('feature_columns', [])
    noaa_mean    = meta.get('noaa_mean', 0.0)

    now_ts = time.time()
    cutoff = now_ts + hours * 3600

    # --- Build NOAA data for forecast period ---
    noaa_forecast_map = {}  # hour_bucket -> noaa_temp_f
    if has_noaa:
        try:
            noaa_rows = list(db.Sensors.find(
                {'gateway_id': gateway_id, 'node_id': _at._NOAA_NODE_ID,
                 'type': 'F', 'time': {'$gte': now_ts, '$lte': cutoff}},
                {'_id': 0, 'value': 1, 'time': 1},
            ))
        except Exception as exc:
            logger.warning('NOAA forecast query failed: %s', exc)
            noaa_rows = []

        for row in noaa_rows:
            val = _clean_value(row.get('value'))
            if not np.isnan(val):
                bucket = int(row['time'] // 3600) * 3600
                noaa_forecast_map[bucket] = val

    # --- Generate hourly forecast timestamps ---
    start_bucket = int(now_ts // 3600 + 1) * 3600
    timestamps = [start_bucket + i * 3600 for i in range(hours)]

    if feature_version >= 2:
        return _predict_v2(pipeline, meta, timestamps, noaa_forecast_map,
                           feature_cols, noaa_mean, sensor_type)
    else:
        return _predict_v1(pipeline, meta, timestamps, noaa_forecast_map,
                           feature_cols, noaa_mean, has_noaa)


def _predict_v1(pipeline, meta, timestamps, noaa_map, feature_cols, noaa_mean, has_noaa):
    """Legacy prediction path for v1 models (time + NOAA features only)."""
    feat_df = pd.DataFrame({'hour_bucket': timestamps})
    feat_df = _add_time_features(feat_df)

    if 'noaa_temp_f' in feature_cols:
        feat_df['noaa_temp_f'] = [noaa_map.get(ts, noaa_mean) for ts in timestamps]

    for col in feature_cols:
        if col not in feat_df.columns:
            feat_df[col] = noaa_mean if col == 'noaa_temp_f' else 0.0

    X = feat_df[feature_cols].fillna(noaa_mean)
    predictions = pipeline.predict(X)

    return [
        {'timestamp': float(ts), 'predicted': round(float(pred), 2)}
        for ts, pred in zip(timestamps, predictions)
    ]


def _predict_v2(pipeline, meta, timestamps, noaa_map, feature_cols, noaa_mean, sensor_type):
    """V2 prediction: direct multi-step with rolling stats + seasonal features.

    Rolling stats are computed from recent_values stored at training time,
    extended with each prediction step to maintain continuity.
    """
    recent_values = list(meta.get('recent_values', []))
    sibling_mean = meta.get('sibling_mean', 0.0) or 0.0
    clip_lo, clip_hi = _VALUE_RANGES.get(sensor_type, (-1e6, 1e6))

    results = []
    for ts in timestamps:
        row = {'hour_bucket': ts}

        # Time features
        hours_frac = (ts % 86400) / 3600
        dow = int((ts // 86400) % 7)
        row['hour_sin'] = np.sin(2 * np.pi * hours_frac / 24)
        row['hour_cos'] = np.cos(2 * np.pi * hours_frac / 24)
        row['dow_sin']  = np.sin(2 * np.pi * dow / 7)
        row['dow_cos']  = np.cos(2 * np.pi * dow / 7)

        # Seasonal features
        dt = pd.Timestamp(ts, unit='s', tz='UTC')
        month = dt.month - 1
        week = dt.isocalendar().week - 1
        row['month_sin'] = np.sin(2 * np.pi * month / 12)
        row['month_cos'] = np.cos(2 * np.pi * month / 12)
        row['woy_sin']   = np.sin(2 * np.pi * week / 53)
        row['woy_cos']   = np.cos(2 * np.pi * week / 53)

        # NOAA
        if 'noaa_temp_f' in feature_cols:
            row['noaa_temp_f'] = noaa_map.get(ts, noaa_mean)

        # Rolling stats from recent values buffer
        recent_arr = np.array(recent_values) if recent_values else np.array([0.0])
        for w in _ROLLING_WINDOWS:
            window = recent_arr[-w:] if len(recent_arr) >= w else recent_arr
            mc = f'roll_mean_{w}'
            sc = f'roll_std_{w}'
            if mc in feature_cols:
                row[mc] = float(np.mean(window))
            if sc in feature_cols:
                row[sc] = float(np.std(window, ddof=0))

        # Sibling sensor — use mean as best available estimate
        for col in feature_cols:
            if col.startswith('sibling_'):
                row[col] = sibling_mean

        # Build feature vector as DataFrame to preserve feature names
        feat_values = {col: [row.get(col, 0.0)] for col in feature_cols}
        X = pd.DataFrame(feat_values)
        pred = float(pipeline.predict(X)[0])

        # Clip to physical range
        pred = max(clip_lo, min(clip_hi, pred))

        results.append({'timestamp': float(ts), 'predicted': round(pred, 2)})

        # Feed prediction into recent buffer for next step's rolling stats
        recent_values.append(pred)

    return results
