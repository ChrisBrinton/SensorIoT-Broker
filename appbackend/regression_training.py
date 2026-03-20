"""regression_training.py — per-sensor regression models for predicting indoor climate.

Trains one supervised regression model per (gateway_id, node_id, type) using:
  - All available historical sensor readings (no lookback cap)
  - NOAA outdoor temperature (when available) as a key predictor
  - Cyclic temporal features (hour-of-day, day-of-week sin/cos)

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
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_squared_error, r2_score
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

# Hyperparameter grid: all variants trained; winner by mean CV R² is kept.
# Format: (model_class, param_dict)
_REGRESSION_GRID: List[Tuple] = [
    # Ridge regression — fast linear baseline
    (Ridge,                     {'alpha': 0.1}),
    (Ridge,                     {'alpha': 1.0}),
    (Ridge,                     {'alpha': 10.0}),
    (Ridge,                     {'alpha': 100.0}),
    # Random Forest — captures non-linear weather/time interactions
    (RandomForestRegressor,     {'n_estimators': 100, 'max_depth': 4,    'random_state': 42}),
    (RandomForestRegressor,     {'n_estimators': 100, 'max_depth': 8,    'random_state': 42}),
    (RandomForestRegressor,     {'n_estimators': 200, 'max_depth': None, 'random_state': 42}),
    # Gradient Boosting — typically best for tabular time-series with weather
    (GradientBoostingRegressor, {'n_estimators': 100, 'learning_rate': 0.1,  'max_depth': 3, 'random_state': 42}),
    (GradientBoostingRegressor, {'n_estimators': 100, 'learning_rate': 0.05, 'max_depth': 4, 'random_state': 42}),
    (GradientBoostingRegressor, {'n_estimators': 200, 'learning_rate': 0.05, 'max_depth': 3, 'random_state': 42}),
]

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

def get_sensor_dataframe(
    db,
    gateway_id: str,
    node_id: str,
    sensor_type: str,
) -> Optional[Tuple[pd.DataFrame, float]]:
    """Load all historical readings for one sensor and join with NOAA outdoor temp.

    No lookback cap — all Sensors collection records are used to maximise
    training data.  Each reading is rounded to the nearest hour bucket;
    multiple readings in the same hour are averaged.

    Returns (df, noaa_coverage) where:
      df has columns: hour_bucket, sensor_value, noaa_temp_f, hour_sin,
                      hour_cos, dow_sin, dow_cos
      noaa_coverage is the fraction of rows (0.0–1.0) with valid NOAA data.
    Returns None if fewer than _MIN_ROWS valid hour-buckets are found.
    """
    # --- Load all sensor readings ---
    try:
        rows = list(db.Sensors.find(
            {'gateway_id': gateway_id, 'node_id': str(node_id), 'type': sensor_type},
            {'_id': 0, 'value': 1, 'time': 1},
        ))
    except Exception as exc:
        logger.warning('MongoDB query failed for %s/%s/%s: %s',
                       gateway_id, node_id, sensor_type, exc)
        return None

    if len(rows) < _MIN_ROWS:
        logger.info('Skipping %s/%s/%s: only %d rows',
                    gateway_id, node_id, sensor_type, len(rows))
        return None

    df = pd.DataFrame(rows)
    df['value'] = df['value'].apply(_clean_value)
    df = df.dropna(subset=['value'])
    df['hour_bucket'] = (df['time'] // 3600).astype(int) * 3600

    # One reading per hour bucket (mean)
    df = (df.groupby('hour_bucket')['value']
            .mean()
            .reset_index()
            .rename(columns={'value': 'sensor_value'}))

    if len(df) < _MIN_ROWS:
        logger.info('Skipping %s/%s/%s: only %d unique hour-buckets after dedup',
                    gateway_id, node_id, sensor_type, len(df))
        return None

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

    # --- Temporal features ---
    df = _add_time_features(df, ts_col='hour_bucket')
    df = df.sort_values('hour_bucket').reset_index(drop=True)

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
) -> Tuple[Pipeline, str, dict, float, float, List[str], float]:
    """Train all hyperparameter variants and return the best pipeline.

    Uses TimeSeriesSplit CV to preserve temporal ordering.  The variant with
    the highest mean R² across folds is refitted on the full dataset.

    Returns:
        (pipeline, model_name, best_params, mean_r2, mean_rmse,
         feature_columns, noaa_mean)
    """
    has_noaa = noaa_coverage >= _NOAA_COVERAGE_THRESHOLD
    base_features = ['hour_sin', 'hour_cos', 'dow_sin', 'dow_cos']
    features = (['noaa_temp_f'] + base_features) if has_noaa else base_features

    X = df[features].copy()
    y = df['sensor_value'].values.astype(np.float64)

    # Impute noaa NaNs with column mean (handles partial NOAA coverage)
    noaa_mean = float(X['noaa_temp_f'].mean()) if has_noaa else 0.0
    if has_noaa:
        X['noaa_temp_f'] = X['noaa_temp_f'].fillna(noaa_mean)

    tscv = TimeSeriesSplit(n_splits=_CV_SPLITS)
    results = {}

    for model_cls, params in _REGRESSION_GRID:
        variant_key = f'{model_cls.__name__}_{json.dumps(params, sort_keys=True)}'
        cv_r2s, cv_rmses = [], []
        try:
            for train_idx, val_idx in tscv.split(X):
                X_tr,  X_val  = X.iloc[train_idx], X.iloc[val_idx]
                y_tr,  y_val  = y[train_idx],       y[val_idx]
                pipe = Pipeline([
                    ('scaler', StandardScaler()),
                    ('model',  model_cls(**params)),
                ])
                pipe.fit(X_tr, y_tr)
                y_pred = pipe.predict(X_val)
                cv_r2s.append(float(r2_score(y_val, y_pred)))
                cv_rmses.append(float(np.sqrt(mean_squared_error(y_val, y_pred))))

            mean_r2   = float(np.mean(cv_r2s))
            mean_rmse = float(np.mean(cv_rmses))
            results[variant_key] = {
                'model_cls': model_cls, 'params': params,
                'mean_r2':   mean_r2,   'mean_rmse': mean_rmse,
            }
            logger.info('  %-65s R²=%+.4f  RMSE=%.4f', variant_key, mean_r2, mean_rmse)
        except Exception as exc:
            logger.warning('  Variant %s failed: %s', variant_key, exc)

    if not results:
        raise RuntimeError('All regression variants failed during cross-validation')

    best_key  = max(results, key=lambda k: results[k]['mean_r2'])
    best      = results[best_key]
    model_display_name = best['model_cls'].__name__
    logger.info('Best variant: %s (R²=%.4f  RMSE=%.4f)',
                best_key, best['mean_r2'], best['mean_rmse'])

    # Final refit on full dataset
    final_pipe = Pipeline([
        ('scaler', StandardScaler()),
        ('model',  best['model_cls'](**best['params'])),
    ])
    final_pipe.fit(X, y)

    return (
        final_pipe, model_display_name, best['params'],
        best['mean_r2'], best['mean_rmse'], features, noaa_mean,
    )


# ---------------------------------------------------------------------------
# Model persistence
# ---------------------------------------------------------------------------

def save_regression_model(
    gateway_id: str, node_id: str, sensor_type: str,
    pipeline: Pipeline, model_name: str, best_params: dict,
    r2: float, rmse: float, feature_columns: List[str],
    has_noaa: bool, noaa_mean: float, num_rows: int,
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
        'feature_columns': feature_columns,
        'has_noaa':        has_noaa,
        'noaa_mean':       noaa_mean,
        'num_rows':        num_rows,
        'trained_at':      time.time(),
    }
    with open(_meta_path(gateway_id, node_id, sensor_type, models_dir), 'w') as f:
        json.dump(meta, f)

    logger.info('Saved regression model %s/%s/%s: %s R²=%.4f RMSE=%.4f rows=%d',
                gateway_id, node_id, sensor_type, model_name, r2, rmse, num_rows)


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
             mean_r2, mean_rmse, features, noaa_mean) = train_regression_for_sensor(
                df, noaa_coverage)

            has_noaa = noaa_coverage >= _NOAA_COVERAGE_THRESHOLD
            save_regression_model(
                gateway_id, node_id, sensor_type,
                pipeline, model_name, best_params,
                mean_r2, mean_rmse, features,
                has_noaa, noaa_mean, len(df), models_dir,
            )
            all_results.append({
                'gateway_id': gateway_id, 'node_id': node_id,
                'type': sensor_type, 'status': 'done',
                'model_type': model_name,
                'r2':         round(mean_r2,   4),
                'rmse':       round(mean_rmse, 4),
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
    """Predict future sensor values using stored NOAA forecast records.

    Returns [{'timestamp': float, 'predicted': float}, ...] — one entry per
    forecast hour found in the Sensors collection (or per synthetic hour if
    the model was trained without NOAA).  Empty list if no model exists.
    """
    if not regression_model_exists(gateway_id, node_id, sensor_type, models_dir):
        return []

    pipeline, meta = load_regression_model(gateway_id, node_id, sensor_type, models_dir)
    has_noaa     = meta.get('has_noaa', False)
    feature_cols = meta.get('feature_columns', [])
    noaa_mean    = meta.get('noaa_mean', 0.0)

    now_ts = time.time()
    cutoff = now_ts + hours * 3600

    if has_noaa:
        # Attempt to use real NOAA forecast records
        try:
            noaa_rows = list(db.Sensors.find(
                {'gateway_id': gateway_id, 'node_id': _at._NOAA_NODE_ID,
                 'type': 'F', 'time': {'$gte': now_ts, '$lte': cutoff}},
                {'_id': 0, 'value': 1, 'time': 1},
            ))
        except Exception as exc:
            logger.warning('NOAA forecast query failed: %s', exc)
            noaa_rows = []

        if noaa_rows:
            noaa_df = pd.DataFrame(noaa_rows)
            noaa_df['value'] = noaa_df['value'].apply(_clean_value)
            noaa_df = noaa_df.dropna(subset=['value'])
            noaa_df['hour_bucket'] = (noaa_df['time'] // 3600).astype(int) * 3600
            feat_df = (noaa_df.groupby('hour_bucket')['value']
                              .first()
                              .reset_index()
                              .rename(columns={'value': 'noaa_temp_f'}))
            feat_df = _add_time_features(feat_df)
        else:
            # Fall back: time-only features, fill noaa_temp_f with training mean
            logger.info('No NOAA forecast records for %s; using time features + mean',
                        gateway_id)
            has_noaa = False  # reuse synthetic path below

    if not has_noaa:
        # Generate synthetic hourly timestamps from now to cutoff
        start_bucket = int(now_ts // 3600 + 1) * 3600
        timestamps = [start_bucket + i * 3600 for i in range(hours)]
        feat_df = pd.DataFrame({'hour_bucket': timestamps})
        feat_df = _add_time_features(feat_df)
        if 'noaa_temp_f' in feature_cols:
            feat_df['noaa_temp_f'] = noaa_mean

    if feat_df.empty:
        return []

    # Ensure every training feature is present; fill any missing with noaa_mean
    for col in feature_cols:
        if col not in feat_df.columns:
            feat_df[col] = noaa_mean if col == 'noaa_temp_f' else 0.0

    X = feat_df[feature_cols].fillna(noaa_mean)
    predictions = pipeline.predict(X)

    return [
        {'timestamp': float(row['hour_bucket']), 'predicted': round(float(pred), 2)}
        for (_, row), pred in zip(feat_df.iterrows(), predictions)
    ]
