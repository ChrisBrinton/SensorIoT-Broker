# SensorIoT — REST API Server

Flask REST API serving IoT sensor data from MongoDB. Includes Google Home OAuth/fulfillment, ML anomaly detection, regression forecasting, and database maintenance tools.

## Quick Start

```bash
pipenv install
cp .env.example .env              # Set MONGO_URI and AES_SHARED_KEY
./runinteractivesvr.sh            # Dev: interactive Flask server
```

Production:
```bash
./runserver.sh                    # Gunicorn on port 5050, 4 workers, background
./logs.sh                         # Tail logs
./stopserver.sh                   # Stop gunicorn + rotate logs
```

Docker:
```bash
docker build -t sensoriot_server .
docker run --network host -p 80:80 -p 443:443 -v sensoriot_models:/models sensoriot_server
```

Tests:
```bash
pipenv install --dev
pipenv run pytest -v
```

## Architecture

```
Internet → Nginx (443, SSL) → Gunicorn (localhost:5050, 4 workers) → Flask (server.py)
```

### Modules

| File | Role |
|---|---|
| `server.py` | All REST endpoints, CORS, Google token verification |
| `anomaly_training.py` | Unsupervised anomaly detection (IF / OC-SVM / NS-RF per gateway) |
| `regression_training.py` | Supervised regression forecasting (Ridge / RF / GBT per sensor) |
| `auth.py` | Google Home OAuth mock |
| `fulfillment.py` | Google Home Smart Home webhook |
| `app_state.py` | In-memory OAuth state (lost on restart) |
| `archivedb.py` | Archive old data to gzipped JSONL |
| `trimdb.py` | Dry-run / delete old records |

## API Endpoints

Base URL: `https://brintontech.com` | CORS enabled | JSON responses

### Sensor Data

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/latest/{gw}` | — | Current readings for a gateway |
| GET | `/latests` | — | Batch current readings (`?gw=gw1&gw=gw2`) |
| GET | `/sensor/{node}` | — | Historical readings (`?period=&skip=&type=`) |
| GET | `/gw/{gw}` | — | Per-node history with timezone |
| GET | `/nodelist/{gw}` | — | Node IDs on a gateway |
| GET | `/nodelists` | — | Batch node lists |
| GET | `/forecast/{gw}` | — | NOAA forecast records |
| GET | `/heatmap/{gw}` | — | Daily min/max/avg aggregation |

### User & Config (Google auth)

| Method | Path | Description |
|---|---|---|
| GET/POST | `/user_profile` | User profile CRUD |
| GET/POST | `/noaa_settings` | NOAA weather config |
| GET/POST | `/alert_rules` | Alert rules list/create |
| PUT/DELETE | `/alert_rules/<rule_id>` | Alert rule update/delete |
| POST | `/device_token` | Register FCM token |

### Nicknames & Third-Party

| Method | Path | Description |
|---|---|---|
| GET | `/get_nicknames` | Display names |
| POST | `/save_nicknames` | Update names |
| POST | `/add_3p_service` | Save encrypted credentials |
| GET | `/get_3p_services` | Retrieve credentials |
| GET | `/testsense` | Sense Energy power |

### ML Anomaly Detection

| Method | Path | Description |
|---|---|---|
| POST | `/train_anomaly_model` | Start training (`{gateway_ids:[]}`) |
| GET | `/training_status` | Poll job (`?job_id=`) |
| GET | `/predict_anomaly` | Anomalous timestamps (`?gateway_id=&node_id=&period=`) |
| GET | `/anomaly_model_status` | Model metadata (`?gateway_id=`) |

Models stored in `models/{gateway_id}/model.joblib` + `metadata.json`.

### Regression Forecasting

| Method | Path | Description |
|---|---|---|
| POST | `/train_regression_model` | Start training |
| GET | `/regression_training_status` | Poll job |
| GET | `/regression_model_status` | Model metadata (R², RMSE) |
| GET | `/regression_forecast` | Predicted future values |

Models stored in `models/{gw}/regression/{node}_{type}.joblib` + `_meta.json`.

### Baseline

| Method | Path | Description |
|---|---|---|
| POST | `/compute_baseline` | Compute per-hour-of-week baseline |
| GET | `/baseline/{gw}` | Fetch baseline buckets |
| GET | `/baseline_status/{gw}` | Check if baseline exists |

### Google Home

| Method | Path | Description |
|---|---|---|
| GET/POST | `/auth` | OAuth login + approval |
| POST | `/token` | Token exchange |
| POST | `/fulfillment` | SYNC/QUERY/EXECUTE intents |

## ML Pipelines

### Anomaly Detection (`anomaly_training.py`)

One unsupervised model per gateway. Pipeline:
1. Query all F/H/P readings; pivot to wide format (`{node_id}_{type}` columns)
2. Compute optimal bucket size from median inter-reading intervals
3. Feature engineering: cyclic time (hour/day-of-week sin/cos), rolling trends (delta, mean, std)
4. NOAA integration: forward-filled forecast temperature when enabled
5. Train Isolation Forest, One-Class SVM, and Negative-Sampling RF (MADI)
6. Select winner by AUC

### Regression Forecasting (`regression_training.py`)

One supervised model per sensor (node x type). Pipeline:
1. All historical data (no cap)
2. Feature engineering: cyclic time, lag features, NOAA temperature
3. 10 hyperparameter variants across Ridge, Random Forest, Gradient Boosted Trees
4. TimeSeriesSplit cross-validation; winner by mean R²
5. Metadata: `has_noaa`, `r2`, `rmse`, `num_rows`, `trained_at`

## Database Maintenance

### Archive

```bash
# Dry run:
pipenv run python3 archivedb.py -d PROD -m 6

# Archive + delete:
pipenv run python3 archivedb.py -d PROD -m 6 --output-dir ./archives --remove

# Install monthly cron (2 AM, 1st of month):
./install_archive_cron.sh
```

### Trim

```bash
pipenv run python3 trimdb.py --db=PROD --months=6 --remove
```

## Environment Variables (`.env`)

| Variable | Purpose |
|---|---|
| `MONGO_URI` | MongoDB connection string |
| `AES_SHARED_KEY` | Base64-encoded 256-bit AES key for credential decryption |

## Nginx Configuration

`nginx.conf` configures SSL termination via Let's Encrypt, HTTP→HTTPS redirect, static file serving from `/public`, and reverse proxy to Gunicorn on port 5050.

SSL certificates are mounted from the host at `/etc/letsencrypt`.
