"""anomaly_training.py — server-side ML anomaly detection training and prediction.

Uses the madi detector wrappers (IsolationForest, OneClassSVM, NS-RandomForest) and
utilities from the shared anomalydetection/ module. TensorFlow is NOT required on the
server; the deferred TF import in sample_utils only fires if you call
write_normalization_info / read_normalization_info, which we do not.

Trains one gateway-level model on a flattened multi-node DataFrame where each node's
readings appear as prefixed columns (e.g. 1_F, 1_H, 2_F, 2_H). This enables relative
anomaly detection across nodes. Saves the model with joblib and provides prediction
via the unified madi predict() interface.
"""

import datetime
import json
import logging
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

import requests

import joblib
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Locate the shared anomalydetection/ package regardless of working directory.
# Locally:  ./anomalydetection  (relative to this file in SensorIoT-REST_server/)
# Docker:   /anomalydetection    (COPY anomalydetection/ /anomalydetection/ in Dockerfile)
# ---------------------------------------------------------------------------
_madi_parent = os.path.abspath(
    os.path.join(os.path.dirname(__file__), 'anomalydetection'))
if _madi_parent not in sys.path:
    sys.path.insert(0, _madi_parent)

# Purge any previously-imported (e.g. pip-installed) madi modules so the
# local copy from _madi_parent is used instead.
for _key in [k for k in sys.modules if k == 'madi' or k.startswith('madi.')]:
    del sys.modules[_key]

from madi.detectors.isolation_forest_detector import IsolationForestAd
from madi.detectors.one_class_svm import OneClassSVMAd
from madi.detectors.neg_sample_random_forest import NegativeSamplingRandomForestAd
import madi.utils.sample_utils as sample_utils
from madi.utils.evaluation_utils import compute_auc
from sklearn.covariance import EllipticEnvelope
from sklearn.metrics import f1_score as sk_f1_score
from sklearn.neighbors import LocalOutlierFactor
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

_log_fmt = logging.Formatter(
    '%(asctime)s  %(levelname)-8s  %(name)s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

# Log to stdout
_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.setLevel(logging.DEBUG)
_stdout_handler.setFormatter(_log_fmt)
logger.addHandler(_stdout_handler)

# Log to file (in the same directory as this module)
_log_file = os.path.join(os.path.dirname(__file__), 'anomaly_training.log')
_file_handler = logging.FileHandler(_log_file)
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(_log_fmt)
logger.addHandler(_file_handler)

MODELS_DIR = os.path.join(os.path.dirname(__file__), 'models')
_LOOKBACK_DAYS     = 90
_SAMPLE_RATIO      = 2.0
_SAMPLE_DELTA      = 0.05
_TEST_RATIO        = 1.0
_ANOMALY_THRESHOLD = 0.5
_BUCKET_SECONDS    = 60   # fallback / minimum bucket size
# Candidate bucket sizes tried in order (seconds); first one >= max node interval is used.
_BUCKET_CANDIDATES = (60, 120, 300, 600, 900, 1800, 3600)
_NOAA_NODE_ID      = 'noaa_forecast'
_TREND_WINDOW      = 6   # rolling window (buckets) for delta/mean/std trend features


# ---------------------------------------------------------------------------
# Additional detector wrappers (same train_model/predict interface as madi)
# ---------------------------------------------------------------------------

class _LOFDetector:
    """Local Outlier Factor in novelty detection mode."""

    def __init__(self, n_neighbors=20, contamination=0.05):
        self._model = LocalOutlierFactor(
            n_neighbors=n_neighbors, contamination=contamination, novelty=True)
        self._scaler = StandardScaler()

    def train_model(self, x_train: pd.DataFrame) -> None:
        self._scaler.fit(x_train)
        self._model.fit(self._scaler.transform(x_train))

    def predict(self, sample_df: pd.DataFrame) -> pd.DataFrame:
        preds = self._model.predict(self._scaler.transform(sample_df))
        sample_df['class_prob'] = np.where(preds == 1, 1.0, 0.0)
        return sample_df


class _EllipticEnvelopeDetector:
    """Robust covariance-based outlier detection."""

    def __init__(self, contamination=0.05):
        self._model = EllipticEnvelope(contamination=contamination, support_fraction=0.9)
        self._scaler = StandardScaler()

    def train_model(self, x_train: pd.DataFrame) -> None:
        self._scaler.fit(x_train)
        self._model.fit(self._scaler.transform(x_train))

    def predict(self, sample_df: pd.DataFrame) -> pd.DataFrame:
        preds = self._model.predict(self._scaler.transform(sample_df))
        sample_df['class_prob'] = np.where(preds == 1, 1.0, 0.0)
        return sample_df


class _NSMLPDetector:
    """Negative-Sampling MLP — scikit-learn replacement for the TF NS-NeuralNet."""

    def __init__(self, hidden_layer_sizes=(128, 64, 32), sample_ratio=2.0,
                 sample_delta=0.05, random_state=42):
        self._model = MLPClassifier(
            hidden_layer_sizes=hidden_layer_sizes, activation='relu',
            max_iter=200, early_stopping=True, validation_fraction=0.15,
            random_state=random_state)
        self._scaler = StandardScaler()
        self._sample_ratio = sample_ratio
        self._sample_delta = sample_delta

    def train_model(self, x_train: pd.DataFrame) -> None:
        self._normalization_info = sample_utils.get_normalization_info(x_train)
        column_order = sample_utils.get_column_order(self._normalization_info)
        normalized = sample_utils.normalize(x_train[column_order], self._normalization_info)
        training_sample = sample_utils.apply_negative_sample(
            positive_sample=normalized,
            sample_ratio=self._sample_ratio,
            sample_delta=self._sample_delta)
        self._model.fit(training_sample[column_order], training_sample['class_label'])
        self._column_order = column_order

    def predict(self, sample_df: pd.DataFrame) -> pd.DataFrame:
        normalized = sample_utils.normalize(sample_df, self._normalization_info)
        x = normalized[self._column_order].astype(np.float32)
        probs = self._model.predict_proba(x)
        sample_df['class_prob'] = probs[:, 1]
        return sample_df


class _EnsembleDetector:
    """Average class_prob across multiple trained detectors.

    Satisfies the same train_model/predict interface as madi detectors so it
    can be saved with joblib and used by predict_anomalies() transparently.
    """

    def __init__(self, detectors: list, names: list):
        self._detectors = detectors
        self._names = names

    def train_model(self, x_train: pd.DataFrame) -> None:
        for name, det in zip(self._names, self._detectors):
            logger.debug('Ensemble: training %s...', name)
            det.train_model(x_train.copy())

    def predict(self, sample_df: pd.DataFrame) -> pd.DataFrame:
        all_probs = []
        for det in self._detectors:
            result = det.predict(sample_df.copy())
            all_probs.append(result['class_prob'].values)
        sample_df['class_prob'] = np.mean(all_probs, axis=0)
        return sample_df


def _add_engineered_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add temporal and rolling trend features to a time-bucketed wide DataFrame.

    Temporal: cyclic sin/cos encoding of hour-of-day and day-of-week so the model
    learns diurnal and weekly patterns without artificial discontinuities at midnight
    or the week boundary.

    Trend: per real sensor column (not NOAA, not time meta), three features capture
    the local trajectory around each reading:
      {col}_delta      — change from the prior bucket (rate of change)
      {col}_roll_mean  — rolling mean over the last _TREND_WINDOW buckets
      {col}_roll_std   — rolling std  over the last _TREND_WINDOW buckets
    min_periods=1 avoids introducing NaNs at the start of the series.
    """
    df = df.sort_values('time_rounded').reset_index(drop=True)

    # Cyclic time features derived from Unix timestamps
    hours = (df['time_rounded'] % 86400) / 3600              # float 0–24
    dows  = ((df['time_rounded'] // 86400) % 7).astype(int)  # int 0–6
    df['hour_sin'] = np.sin(2 * np.pi * hours / 24)
    df['hour_cos'] = np.cos(2 * np.pi * hours / 24)
    df['dow_sin']  = np.sin(2 * np.pi * dows  / 7)
    df['dow_cos']  = np.cos(2 * np.pi * dows  / 7)

    # Rolling trend features for real sensor columns only
    _meta = {'time_rounded', 'hour_sin', 'hour_cos', 'dow_sin', 'dow_cos'}
    sensor_cols = [
        c for c in df.columns
        if c not in _meta
        and not c.startswith(_NOAA_NODE_ID)
        and '_' in c   # node_id_type pattern: "1_F", "2_H", etc.
    ]
    for col in sensor_cols:
        df[f'{col}_delta']     = df[col].diff().fillna(0.0)
        df[f'{col}_roll_mean'] = df[col].rolling(_TREND_WINDOW, min_periods=1).mean()
        df[f'{col}_roll_std']  = (df[col].rolling(_TREND_WINDOW, min_periods=1)
                                         .std(ddof=0).fillna(0.0))

    return df


def _optimal_bucket_seconds(df: pd.DataFrame, node_ids: List[str]) -> int:
    """Return the smallest snap interval that covers every node's median reporting gap.

    Nodes send F/H/P in one shot, so aligning across nodes only requires a bucket
    large enough that all nodes fire at least once per window.  We compute the
    median inter-reading gap for each node and pick the first _BUCKET_CANDIDATES
    value that is >= the slowest node's median interval.
    """
    intervals = []
    for n in node_ids:
        times = np.sort(df[(df['node_id'] == n)  & (df['type'] == 'F')]['time'].values)
        if len(times) >= 2:
            intervals.append(float(np.median(np.diff(times))))

    if not intervals:
        return _BUCKET_SECONDS

    max_interval = max(intervals)
    logger.debug('Node intervals: %s, max=%.1f s', ', '.join(f'{i:.1f}' for i in intervals), max_interval)
    for snap in _BUCKET_CANDIDATES:
        if snap >= max_interval:
            return snap
    return _BUCKET_CANDIDATES[-1]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def get_gateway_dataframe(db, gateway_id: str,
                          lookback_days: int = _LOOKBACK_DAYS) -> Optional[pd.DataFrame]:
    """Query all nodes for gateway_id; return a wide time-bucketed DataFrame.

    Columns are '{node_id}_{type}' (e.g. '1_F', '1_H', '2_F', '2_P').
    The bucket size is chosen dynamically: for each node the median inter-reading
    interval is computed; the bucket is set to the smallest value in
    _BUCKET_CANDIDATES that is >= the slowest node's interval. This ensures all
    nodes have at least one reading per bucket regardless of their duty cycle.
    Rows missing F or H for any node are dropped; P columns are allowed to be
    sparse. The returned DataFrame includes a 'time_rounded' column.
    Returns None if fewer than 20 aligned rows remain.
    """
    start_ts = time.time() - lookback_days * 86400
    try:
        cursor = db.Sensors.find(
            {'gateway_id': gateway_id,
             'time': {'$gte': start_ts},
             'type': {'$in': ['F', 'H', 'P']},
             },
            {'_id': 0, 'node_id': 1, 'type': 1, 'value': 1, 'time': 1},
        )
        rows = list(cursor)
    except Exception as exc:
        logger.warning('MongoDB query failed for gateway %s: %s', gateway_id, exc)
        return None

    if not rows:
        logger.info('No sensor rows found for gateway %s in last %d days', gateway_id, lookback_days)
        return None

    df = pd.DataFrame(rows)

    def _clean(v):
        try:
            return float(str(v).replace("b'", '').replace("'", ''))
        except (ValueError, TypeError):
            return float('nan')

    df['value'] = df['value'].apply(_clean)
    df = df.dropna(subset=['value'])
    df['node_id'] = df['node_id'].astype(str)

    node_ids = sorted(df['node_id'].unique())

    # Check if NOAA is opted-in for this gateway
    noaa_doc = db.NOAASettings.find_one({'gateway_id': gateway_id, 'enabled': True})
    noaa_enabled = noaa_doc is not None

    # Bucket size is driven by real sensor nodes only (NOAA is hourly, not a sensor)
    real_node_ids = [n for n in node_ids if n != _NOAA_NODE_ID]
    bucket_secs = _optimal_bucket_seconds(df, real_node_ids)
    logger.info('Gateway %s: using %d s buckets (nodes=%s)', gateway_id, bucket_secs, real_node_ids)

    df['bucket'] = (df['time'] // bucket_secs).astype(int) * bucket_secs
    df['col'] = df['node_id'] + '_' + df['type']

    pivoted = df.pivot_table(
        index='bucket', columns='col', values='value', aggfunc='first'
    )
    pivoted.columns.name = None

    # Require F and H only from real sensor nodes; NOAA is treated as sparse
    required = [f'{n}_{t}' for n in real_node_ids for t in ('F', 'H')
                if f'{n}_{t}' in pivoted.columns]
    if not required:
        logger.info('No usable F/H columns for gateway %s', gateway_id)
        return None

    # Handle NOAA column: forward-fill when enabled (hourly data → sub-hourly gaps),
    # drop when NOAA is not opted-in so it doesn't pollute the feature space
    noaa_col = f'{_NOAA_NODE_ID}_F'
    if noaa_enabled and noaa_col in pivoted.columns:
        pivoted[noaa_col] = pivoted[noaa_col].ffill().bfill()
        logger.info('Gateway %s: NOAA enabled — %s included as feature', gateway_id, noaa_col)
    elif noaa_col in pivoted.columns:
        pivoted = pivoted.drop(columns=[noaa_col])

    result = pivoted.dropna(subset=required).reset_index(drop=False)
    result = result.rename(columns={'bucket': 'time_rounded'})
    result = _add_engineered_features(result)

    if len(result) < 20:
        logger.info('Gateway %s: only %d aligned rows after dropna', gateway_id, len(result))
        return None

    feature_cols = [c for c in result.columns if c != 'time_rounded']
    logger.info('Gateway %s: %d aligned rows, %d feature columns: %s',
                gateway_id, len(result), len(feature_cols), feature_cols)
    return result


# ---------------------------------------------------------------------------
# Model training & selection
# ---------------------------------------------------------------------------

def train_and_select_best(
    node_df: pd.DataFrame,
    random_state: int = 42,
) -> Tuple[object, str, float, float, List[str]]:
    """Train multiple detectors, build an ensemble, and pick the best by F1.

    Detectors trained:
      - IsolationForest, OneClassSVM, NS-RandomForest (original madi wrappers)
      - LOF, EllipticEnvelope, NS-MLP (new scikit-learn detectors)
      - Ensemble (average of top-3 by F1)

    F1 is computed on the anomaly class (pos_label=0): it measures how well the
    model identifies the synthetic anomalies in the held-out test set.  AUC is
    also computed and retained for reference.

    Returns (best_detector, model_type_name, auc, f1, feature_columns).
    """
    np.random.seed(random_state)

    # Drop all-NaN columns (e.g. noaa_forecast_F when NOAA has no coverage at all).
    all_nan_cols = node_df.columns[node_df.isna().all()].tolist()
    if all_nan_cols:
        logger.info('Dropping %d all-NaN column(s) before training: %s',
                    len(all_nan_cols), all_nan_cols)
        node_df = node_df.drop(columns=all_nan_cols)

    # Fill any remaining NaN values with the column median (safety net for partial
    # coverage, e.g. noaa_forecast_F rows before the first NOAA observation).
    if node_df.isna().any().any():
        node_df = node_df.fillna(node_df.median())

    # Drop zero-variance columns before training.  Constant features carry no
    # information and cause the NS-RandomForest negative sampler to fail:
    # normalization divides by std=0 → NaN bounds → "Range exceeds valid bounds".
    variances = node_df.var()
    zero_var_cols = variances[variances == 0].index.tolist()
    if zero_var_cols:
        logger.info('Dropping %d zero-variance column(s) before training: %s',
                    len(zero_var_cols), zero_var_cols)
        node_df = node_df.drop(columns=zero_var_cols)

    feature_cols = node_df.columns.tolist()

    n_train = int(0.8 * len(node_df))
    shuffled = node_df.sample(frac=1, random_state=random_state).reset_index(drop=True)
    x_train    = shuffled.iloc[:n_train][feature_cols]
    x_test_raw = shuffled.iloc[n_train:][feature_cols]

    # Synthetic test set: real positives + permuted negatives
    pos_test = x_test_raw.copy()
    pos_test['class_label'] = 1
    neg_test = sample_utils.get_neg_sample(
        x_test_raw, int(len(x_test_raw) * _TEST_RATIO), do_permute=True)
    test_combined = pd.concat([pos_test, neg_test], ignore_index=True).sample(frac=1)
    X_test_df = test_combined[feature_cols]
    y_test    = test_combined['class_label'].values

    detectors = {
        'IsolationForest': IsolationForestAd(contamination=0.05, random_state=random_state),
        'OneClassSVM':     OneClassSVMAd(nu=0.1),
        'NS-RandomForest': NegativeSamplingRandomForestAd(
            n_estimators=100, random_state=random_state,
            sample_ratio=_SAMPLE_RATIO, sample_delta=_SAMPLE_DELTA),
        'LOF':             _LOFDetector(n_neighbors=20, contamination=0.05),
        'EllipticEnvelope': _EllipticEnvelopeDetector(contamination=0.05),
        'NS-MLP':          _NSMLPDetector(hidden_layer_sizes=(128, 64, 32),
                                          sample_ratio=_SAMPLE_RATIO,
                                          sample_delta=_SAMPLE_DELTA,
                                          random_state=random_state),
    }

    results = {}
    for name, det in detectors.items():
        try:
            logger.debug('Training %s...', name)
            det.train_model(x_train)
            pred_df = det.predict(X_test_df.copy())
            probs   = pred_df['class_prob'].values
            auc = float(compute_auc(y_test, probs))
            y_pred = (probs >= _ANOMALY_THRESHOLD).astype(int)
            f1  = float(sk_f1_score(y_test, y_pred, pos_label=0, zero_division=0))
            results[name] = (det, auc, f1)
            logger.info('%s AUC=%.4f  F1=%.4f', name, auc, f1)
        except Exception as exc:
            logger.warning('%s failed: %s', name, exc)

    if not results:
        raise RuntimeError('All detectors failed to train')

    # Build an ensemble from the top 3 individual detectors (by F1)
    ranked = sorted(results.items(), key=lambda kv: kv[1][2], reverse=True)
    top_n = min(3, len(ranked))
    top_names = [kv[0] for kv in ranked[:top_n]]
    top_dets  = [kv[1][0] for kv in ranked[:top_n]]
    ensemble_label = 'Ensemble(' + '+'.join(top_names) + ')'

    if top_n >= 2:
        ensemble = _EnsembleDetector(top_dets, top_names)
        # Ensemble is already trained — just evaluate
        try:
            pred_df = ensemble.predict(X_test_df.copy())
            probs = pred_df['class_prob'].values
            ens_auc = float(compute_auc(y_test, probs))
            y_pred = (probs >= _ANOMALY_THRESHOLD).astype(int)
            ens_f1 = float(sk_f1_score(y_test, y_pred, pos_label=0, zero_division=0))
            results[ensemble_label] = (ensemble, ens_auc, ens_f1)
            logger.info('%s AUC=%.4f  F1=%.4f', ensemble_label, ens_auc, ens_f1)
        except Exception as exc:
            logger.warning('Ensemble evaluation failed: %s', exc)

    best_name = max(results, key=lambda k: results[k][2])   # select by F1
    best_det, best_auc, best_f1 = results[best_name]
    logger.info('Best model: %s (F1=%.4f  AUC=%.4f) features=%s',
                best_name, best_f1, best_auc, feature_cols)
    return best_det, best_name, best_auc, best_f1, feature_cols


# ---------------------------------------------------------------------------
# Model persistence
# ---------------------------------------------------------------------------

def _model_dir(gateway_id: str, models_dir: str = MODELS_DIR) -> str:
    return os.path.join(models_dir, str(gateway_id))


def save_model(gateway_id: str, model, model_type: str,
               auc: float, f1: float, feature_columns: List[str], nodes: List[str],
               num_rows: int,
               models_dir: str = MODELS_DIR) -> None:
    """Persist gateway-level model + metadata to models/{gateway}/.

    Normalization is embedded inside the detector object and saved by joblib;
    no separate normalization.json is needed.
    """
    path = _model_dir(gateway_id, models_dir)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        logger.error('Failed to create model directory %s: %s', path, exc)
        raise

    model_path = os.path.join(path, 'model.joblib')
    meta_path = os.path.join(path, 'metadata.json')

    try:
        joblib.dump(model, model_path)
    except Exception as exc:
        logger.error('Failed to save model to %s: %s', model_path, exc)
        raise

    try:
        with open(meta_path, 'w') as f:
            json.dump({'model_type': model_type, 'auc': auc, 'f1': f1,
                       'feature_columns': feature_columns,
                       'nodes': nodes,
                       'num_rows': num_rows,
                       'trained_at': time.time()}, f)
    except Exception as exc:
        logger.error('Failed to save metadata to %s: %s', meta_path, exc)
        if os.path.isfile(model_path):
            os.remove(model_path)
        raise

    logger.info('Saved %s gateway model (F1=%.4f  AUC=%.4f) nodes=%s num_rows=%d to %s',
                model_type, f1, auc, nodes, num_rows, path)


def load_model(gateway_id: str, models_dir: str = MODELS_DIR) -> Tuple[object, Dict]:
    """Load (model, metadata). Raises FileNotFoundError if absent."""
    path = _model_dir(gateway_id, models_dir)
    model = joblib.load(os.path.join(path, 'model.joblib'))
    with open(os.path.join(path, 'metadata.json')) as f:
        metadata = json.load(f)
    return model, metadata


def model_exists(gateway_id: str, models_dir: str = MODELS_DIR) -> bool:
    return os.path.isfile(os.path.join(_model_dir(gateway_id, models_dir), 'model.joblib'))


# ---------------------------------------------------------------------------
# Prediction
# ---------------------------------------------------------------------------

def predict_anomalies(model, fh_df: pd.DataFrame,
                      threshold: float = _ANOMALY_THRESHOLD,
                      feature_columns: Optional[List[str]] = None) -> List[float]:
    """Return Unix timestamps where class_prob < threshold (anomalous).

    fh_df must contain the feature columns used during training (at minimum F and H)
    and either a numeric index of Unix timestamps or a 'time_rounded' column.
    feature_columns defaults to ['F', 'H'] for backwards compatibility with models
    trained before P support was added. class_prob: 1.0 = normal, 0.0 = anomalous.
    """
    
    logger.debug('Predicting anomalies for %s with threshold %.2f', fh_df.shape, threshold)
    logger.debug('Feature columns: %s', feature_columns if feature_columns else ['F', 'H', 'P'])
    logger.debug('Input columns: %s', fh_df.columns.tolist())
    logger.debug('Input head:\n%s', fh_df.head())

    if fh_df.empty:
        return []
    

    logger.debug('Predicting anomalies for %d rows with features %s',
                 len(fh_df), feature_columns if feature_columns else ['F', 'H', 'P'])
    cols = feature_columns if feature_columns else ['F', 'H', 'P']
    # Only use columns present in the dataframe (P may be absent for some nodes)
    cols = [c for c in cols if c in fh_df.columns]

    timestamps = (fh_df['time_rounded'].values
                  if 'time_rounded' in fh_df.columns
                  else fh_df.index.values.astype(float))

    try:
        result = model.predict(fh_df[cols].copy())
        mask = result['class_prob'].values < threshold
    except Exception as exc:
        logger.warning('Prediction failed: %s', exc)
        return []

    anomalies = [float(ts) for ts, m in zip(timestamps, mask) if m]

    logger.info('Predicted %d anomalies out of %d rows (%.2f%%) with threshold %.2f',
                len(anomalies), len(fh_df), 100 * len(anomalies) / len(fh_df), threshold)

    return anomalies


# ---------------------------------------------------------------------------
# NOAA historical backfill (inlined from NOAAHistoricalFetcher.py logic)
# ---------------------------------------------------------------------------

_NOAA_API_BASE  = 'https://api.weather.gov'
_NOAA_UA        = '(SensorIoT, keyvanazami@gmail.com)'
_NOAA_SLEEP     = 0.5   # seconds between NOAA API calls (per ToS)
_NOAA_PAGE_LIMIT = 500


def _backfill_noaa_history(db, gateway_id: str, lat: float, lon: float,
                            lookback_days: int) -> None:
    """Fetch historical NOAA observations and insert any missing hour-buckets.

    Called at the start of train_for_gateway() when NOAA is enabled so that
    the training window is fully populated before get_gateway_dataframe() runs.
    Uses the same document schema and deduplication logic as NOAAHistoricalFetcher.py.
    Failures are logged and swallowed — training proceeds without NOAA features
    if the API is unavailable.
    """
    now_utc  = datetime.datetime.now(datetime.timezone.utc)
    start_dt = (now_utc - datetime.timedelta(days=lookback_days)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    end_dt   = (now_utc - datetime.timedelta(days=1)).replace(
        hour=23, minute=59, second=59, microsecond=0)

    logger.info('Gateway %s: backfilling NOAA history %s → %s',
                gateway_id, start_dt.date(), end_dt.date())

    # --- Resolve nearest observation station ---
    try:
        r = requests.get(
            f'{_NOAA_API_BASE}/points/{lat:.4f},{lon:.4f}',
            headers={'User-Agent': _NOAA_UA}, timeout=15)
        r.raise_for_status()
        time.sleep(_NOAA_SLEEP)
        stations_url = r.json().get('properties', {}).get('observationStations')
    except Exception as exc:
        logger.warning('Gateway %s: NOAA points API failed: %s', gateway_id, exc)
        return

    if not stations_url:
        logger.warning('Gateway %s: no observationStations in NOAA response', gateway_id)
        return

    try:
        r2 = requests.get(stations_url, params={'limit': 5},
                          headers={'User-Agent': _NOAA_UA}, timeout=15)
        r2.raise_for_status()
        time.sleep(_NOAA_SLEEP)
        station_features = r2.json().get('features', [])
    except Exception as exc:
        logger.warning('Gateway %s: NOAA stations list failed: %s', gateway_id, exc)
        return

    if not station_features:
        logger.warning('Gateway %s: no observation stations near (%.4f, %.4f)',
                       gateway_id, lat, lon)
        return

    station_id = station_features[0]['properties']['stationIdentifier']
    logger.info('Gateway %s: NOAA station resolved to %s', gateway_id, station_id)

    # --- Load existing hour-bucket timestamps for deduplication ---
    start_ts = start_dt.timestamp()
    end_ts   = end_dt.timestamp()
    existing = {
        int(doc['time'] // 3600) * 3600
        for doc in db.Sensors.find(
            {'gateway_id': gateway_id, 'node_id': _NOAA_NODE_ID,
             'type': 'F', 'time': {'$gte': start_ts, '$lte': end_ts}},
            {'time': 1, '_id': 0},
        )
    }
    logger.info('Gateway %s: %d existing NOAA hour-buckets in range', gateway_id, len(existing))

    # --- Fetch observations in weekly chunks ---
    docs: list = []
    chunk_start = start_dt
    chunk_delta = datetime.timedelta(days=7)

    while chunk_start < end_dt:
        chunk_end = min(chunk_start + chunk_delta, end_dt)
        start_iso = chunk_start.strftime('%Y-%m-%dT%H:%M:%SZ')
        end_iso   = chunk_end.strftime('%Y-%m-%dT%H:%M:%SZ')

        next_url: str | None = None
        pages = 0
        while True:
            try:
                if next_url:
                    obs_r = requests.get(next_url,
                                         headers={'User-Agent': _NOAA_UA}, timeout=20)
                else:
                    obs_r = requests.get(
                        f'{_NOAA_API_BASE}/stations/{station_id}/observations',
                        params={'start': start_iso, 'end': end_iso,
                                'limit': _NOAA_PAGE_LIMIT},
                        headers={'User-Agent': _NOAA_UA}, timeout=20)
                obs_r.raise_for_status()
                time.sleep(_NOAA_SLEEP)
                data = obs_r.json()
            except Exception as exc:
                logger.warning('Gateway %s: observation fetch error: %s', gateway_id, exc)
                break

            pages += 1
            for feat in data.get('features', []):
                try:
                    ts_str   = feat['properties']['timestamp']
                    obs_ts   = datetime.datetime.fromisoformat(ts_str).timestamp()
                    temp_obj = feat['properties']['temperature']
                    if temp_obj is None or temp_obj.get('value') is None:
                        continue
                    temp_f     = round(float(temp_obj['value']) * 9 / 5 + 32, 1)
                    rounded_ts = round(obs_ts / 3600) * 3600
                    if rounded_ts in existing:
                        continue
                    docs.append({
                        'model': 'NOAA', 'gateway_id': gateway_id,
                        'node_id': _NOAA_NODE_ID, 'type': 'F',
                        'value': str(temp_f), 'time': float(rounded_ts),
                    })
                    existing.add(rounded_ts)
                except (KeyError, TypeError, ValueError):
                    continue

            next_url = data.get('@odata.nextLink')
            if not next_url or not data.get('features') or pages >= 20:
                break

        chunk_start = chunk_end

    # --- Insert ---
    if docs:
        try:
            db.Sensors.insert_many(docs, ordered=False)
            logger.info('Gateway %s: inserted %d NOAA history records', gateway_id, len(docs))
        except Exception as exc:
            logger.warning('Gateway %s: NOAA history insert error: %s', gateway_id, exc)
    else:
        logger.info('Gateway %s: NOAA history already up to date (0 new records)', gateway_id)


# ---------------------------------------------------------------------------
# Gateway-level orchestration (called from server.py background thread)
# ---------------------------------------------------------------------------

def train_for_gateway(gateway_id: str, db,
                      models_dir: str = MODELS_DIR) -> List[Dict]:
    """Train one gateway-level model on flattened multi-node data.

    All nodes' F/H/P readings are combined into a single wide DataFrame with
    columns prefixed by node_id (e.g. '1_F', '1_H', '2_F'). One model is trained
    per gateway and saved to models/{gateway_id}/.
    """
    # If NOAA is enabled for this gateway, backfill historical observations
    # before building the training DataFrame so noaa_forecast_F is populated.
    noaa_doc = db.NOAASettings.find_one({'gateway_id': gateway_id, 'enabled': True})
    if noaa_doc and noaa_doc.get('lat') is not None and noaa_doc.get('lon') is not None:
        _backfill_noaa_history(db, gateway_id,
                               float(noaa_doc['lat']), float(noaa_doc['lon']),
                               _LOOKBACK_DAYS)

    logger.info('Building gateway-wide DataFrame for %s', gateway_id)
    gw_df = get_gateway_dataframe(db, gateway_id)
    if gw_df is None:
        logger.info('Skipping gateway %s: insufficient aligned data', gateway_id)
        return [{'gateway_id': gateway_id, 'status': 'skipped',
                 'reason': 'insufficient aligned data'}]

    feature_df = gw_df.drop(columns=['time_rounded'])
    try:
        model, model_type, auc, f1, feature_cols = train_and_select_best(feature_df)
    except Exception as exc:
        logger.error('Training failed for gateway %s: %s', gateway_id, exc)
        return [{'gateway_id': gateway_id, 'status': 'failed', 'error': str(exc)}]

    nodes = sorted({c.rsplit('_', 1)[0] for c in feature_cols})
    num_rows = len(feature_df)
    save_model(gateway_id, model, model_type, auc, f1, feature_cols, nodes, num_rows, models_dir)
    logger.info('Training complete for gateway %s: %s F1=%.4f AUC=%.4f nodes=%s num_rows=%d',
                gateway_id, model_type, f1, auc, nodes, num_rows)
    return [{'gateway_id': gateway_id, 'status': 'done',
             'model_type': model_type, 'auc': round(auc, 4), 'f1': round(f1, 4),
             'feature_columns': feature_cols, 'nodes': nodes, 'num_rows': num_rows}]
