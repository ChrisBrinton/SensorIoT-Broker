# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**See also:** [DESIGN.md](DESIGN.md) (system architecture, data flow, ML pipelines) · [DEPLOYMENT.md](DEPLOYMENT.md) (Docker, Nginx, Terraform, Firebase)

## Components

| Directory | Language | Role |
|---|---|---|
| `sensoriot-app/` | Flutter/Dart | Mobile app (iOS/Android) |
| `sensoriot-rest/` | Python/Flask | REST API + Google Home OAuth/fulfillment + ML anomaly detection |
| `sensoriot-broker/` | Python | MQTT→MongoDB bridge + NOAA publisher + alert evaluation (FCM/webhook) |
| `terraform/` | Terraform | IONOS Cloud infrastructure provisioning |

Root-level infrastructure files (`docker-compose.yml`, `rebuild_container.sh`, `mongo_tunnel.sh`, `terraform/`) are tracked in the separate [sensoriot-deploy](https://github.com/keyvanazami/sensoriot-deploy) repo.

Each component is independently deployable. See `sensoriot-app/CLAUDE.md` for Flutter-specific details.

## Commands

### Flutter app (`sensoriot-app/`)
```bash
flutter pub get               # Install dependencies
flutter analyze               # Lint
flutter run                   # Run on connected device/simulator
flutter build ios --release   # Build iOS release
flutter build apk --release   # Build Android release
cd ios && pod install         # Update iOS native pods after pubspec changes
flutter test --reporter=expanded  # Run tests (test/ is currently empty)
```

### REST server (`sensoriot-rest/`)
```bash
pipenv install                # Install dependencies
./runinteractivesvr.sh        # Run interactively for development
./runserver.sh                # Start gunicorn (port 5050, 4 workers, background)
./logs.sh                     # Tail gunicorn logs
./stopserver.sh               # Stop gunicorn
pipenv run pytest -v          # Run tests
```

### MQTT Broker (`sensoriot-broker/`)
```bash
pipenv install
pipenv run python3 DataBroker.py --db TEST    # Connect to test DB
pipenv run python3 DataBroker.py --db PROD    # Connect to prod DB
./runbroker_prod.sh                           # Start broker in background (PROD)
pipenv run pytest test_databroker.py -v       # Run tests
# Docker:
./build_docker.sh && ./docker_run.sh
```

### NOAA Publisher (`sensoriot-broker/`)
```bash
pipenv run python3 NOAAPublisher.py --db PROD --interval 60   # Run hourly loop
pipenv run python3 NOAAPublisher.py --db PROD                 # Run once
# Cronfile entry (every hour):
# 0 * * * *  cd /path/to/sensoriot-broker && pipenv run python3 NOAAPublisher.py --db PROD >> noaa.log 2>&1
```

### Alert Publisher (`sensoriot-broker/`)
```bash
pipenv run python3 AlertPublisher.py --db PROD --interval 5   # Run every 5 min
pipenv run python3 AlertPublisher.py --db PROD                # Run once (e.g. from cron)
```

### Full stack (Docker Compose)
```bash
docker-compose up --build     # Start MongoDB + REST server + Broker together
```

### Deploy to remote server (`rebuild_container.sh`)
```bash
./rebuild_container.sh              # Rebuild + deploy both containers
./rebuild_container.sh -t broker    # Rebuild + deploy broker only
./rebuild_container.sh -t rest      # Rebuild + deploy rest_server only
./rebuild_container.sh -d           # Deploy only (skip rebuild, use existing images)
./rebuild_container.sh -t broker -d # Deploy broker only, skip rebuild
# SSH ControlMaster is used — password prompted once for the whole operation.
```

### Local MongoDB access
```bash
bash mongo_tunnel.sh          # SSH tunnel: localhost:27017 → brintontech.com:27017
```

## Architecture

### System data flow

```
IoT Sensors → MQTT (1883) → DataBroker.py → MongoDB
NOAA API    → NOAAPublisher.py             → MongoDB (noaa_forecast virtual nodes)
                                                ↓
Flutter App ←→ REST API (server.py) ←→ MongoDB
Google Home ←→ auth.py / fulfillment.py (in sensoriot-rest/)
```

### Environment variables (`sensoriot-rest/.env`, not committed)

| Variable | Purpose |
|---|---|
| `MONGO_URI` | MongoDB connection string |
| `AES_SHARED_KEY` | Base64-encoded 256-bit key for AES-256-CBC credential decryption |

### MongoDB databases

- `gdtechdb_prod` / `gdtechdb_test`
- `Sensors` — full historical readings (one document per MQTT message); also stores NOAA virtual sensor records (`node_id='noaa_forecast'`, `model='NOAA'`)
- `SensorsLatest` — one document per sensor, updated via upsert (current state; NOAA records are not upserted here)
- `Nicknames` — sensor display names keyed by `(gateway_id, node_id)`
- `GWNicknames` — gateway display names
- `UserProfiles` — `{email, gateway_ids[], updated_at}`
- `ThirdPartyServices` — encrypted third-party credentials (e.g. Sense Energy)
- `NOAASettings` — per-user NOAA config `{email, lat, lon, gateway_id, outside_sensor_id, enabled, predictive_alerts_enabled, frost_threshold, heat_threshold}`; unique index on `email`
- `AlertRules` — per-user alert rules `{rule_id, email, gateway_id, node_id, type, operator, threshold, offline_minutes, cooldown_minutes, push_enabled, webhook_url, webhook_secret, label, enabled, last_triggered}`
- `DeviceTokens` — FCM device tokens `{email, platform, token, updated_at}`; upserted on `(email, platform)` so only one token per platform per user is kept

### MQTT topic structure

```
/GDESGW1/{model}/{gateway_id}/{node_id}/{type}
```

`type` values: `F` (temp °F), `H` (humidity %), `PWR` (watts), `P` (pressure), `BAT`, `RSSI`.
BAT and RSSI are stored but filtered out in the mobile app UI.

### REST API (`sensoriot-rest/server.py`)

Most endpoints require no authentication. CORS enabled. Base URL: `https://brintontech.com`.

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/latest/{gatewayId}` | GET | — | Current readings for all sensors on a gateway |
| `/latests` | GET | — | Batch current readings (`?gw=gw1&gw=gw2`) |
| `/sensor/{sensorId}` | GET | — | Historical readings (`?period=days&skip=0&type=F`) |
| `/gw/{gw}` | GET | — | Per-node history with timezone (`?node=n&type=F&period=24&timezone=America/New_York`) |
| `/nodelist/{gw}` | GET | — | List node IDs on a gateway |
| `/nodelists` | GET | — | Batch node lists (`?gw=gw1&gw=gw2`) |
| `/get_nicknames` | GET | — | Display names (`?gw={gatewayId}`) |
| `/save_nicknames` | POST | — | Update sensor/gateway display names |
| `/user_profile` | GET/POST | Google | Fetch or upsert `{email, gateway_ids[]}` |
| `/add_3p_service` | POST | — | Save encrypted third-party credentials |
| `/get_3p_services` | GET | — | Retrieve stored credentials |
| `/testsense` | GET | — | Fetch Sense Energy active power |
| `/forecast/{gw}` | GET | — | NOAA forecast records (`?node=noaa_forecast&hours_back=0`) |
| `/noaa_settings` | GET/POST | Google | Fetch or upsert per-user NOAA config (incl. predictive alert thresholds) |
| `/alert_rules` | GET/POST | Google | List or create alert rules for authenticated user |
| `/alert_rules/<rule_id>` | PUT/DELETE | Google | Update or delete a specific alert rule |
| `/device_token` | POST | Google | Register FCM device token; upserted on `(email, platform)` |
| `/heatmap/<gw>` | GET | — | Daily min/max/avg aggregation (`?node=&type=&year=`) for calendar view |
| `/compute_baseline` | POST | — | Compute per-hour-of-week baseline for a gateway's sensors |
| `/baseline/<gw>` | GET | — | Fetch saved baseline buckets |
| `/baseline_status/<gw>` | GET | — | Check whether a baseline exists for a gateway |
| `/train_regression_model` | POST | — | Start per-sensor regression training; returns `{job_id, status}` |
| `/regression_training_status` | GET | — | Poll regression training job (`?job_id=…`) |
| `/regression_model_status` | GET | — | Model metadata per sensor (`?gateway_id=…&node_id=…&type=…`); returns `{r2, rmse, num_rows, has_noaa, trained_at}` |
| `/regression_forecast` | GET | — | Predicted future values (`?gateway_id=…&node_id=…&type=…&hours=24`) |

Google Home endpoints:

| Endpoint | Method | Purpose |
|---|---|---|
| `/auth` | GET/POST | OAuth login page + approval |
| `/token` | POST | Exchange code for access/refresh tokens |
| `/fulfillment` | POST | SYNC / QUERY / EXECUTE intents |

Note: `app_state.py` holds OAuth codes, tokens, and mock devices **in-memory** — state is lost on restart.

### DataBroker CLI flags

| Flag | Default | Purpose |
|---|---|---|
| `--db` | `TEST` | `PROD` (`gdtechdb_prod`) or `TEST` (`gdtechdb_test`) |
| `--dbconn` | `host.docker.internal` | MongoDB host |
| `--host` | `0.0.0.0` | MQTT listener address |
| `--port` | `1883` | MQTT port |
| `--log` | off | Print each message to stdout |

### NOAAPublisher CLI flags (`sensoriot-broker/NOAAPublisher.py`)

| Flag | Default | Purpose |
|---|---|---|
| `--db` | `TEST` | `PROD` or `TEST` |
| `--dbconn` | `host.docker.internal` | MongoDB host |
| `--interval` | — | If set, run in a loop sleeping this many minutes between runs |

On each run: queries all `NOAASettings` docs where `enabled=true`, fetches hourly NOAA forecast for each user's lat/lon, deletes stale future `noaa_forecast` records from `Sensors`, inserts fresh 48-period forecast. Uses `User-Agent` header per NOAA ToS. After publishing, checks next-24h forecast for frost (`<= frost_threshold`), heat (`>= heat_threshold`), and cold-front events; fires FCM push notifications with a 6-hour per-gateway cooldown when `predictive_alerts_enabled=true`.

### AlertPublisher (`sensoriot-broker/AlertPublisher.py`)

Evaluates all enabled `AlertRules` in MongoDB and fires notifications. Started by `startup.sh` in the Docker container (runs every 5 minutes via `--interval 5`). Can also run as a cron job.

Key behaviours:
- Reads `SensorsLatest` for the latest value of each rule's `(gateway_id, node_id, type)`
- Handles legacy values stored as `"b'49.46'"` (str-of-bytes) by stripping the wrapper
- Respects per-rule `cooldown_minutes`; updates `last_triggered` on fire
- `push_enabled=True`: looks up `DeviceTokens` for the rule owner's email, sends FCM via `firebase_admin`; auto-removes `UnregisteredError` (stale) tokens from DB
- `webhook_url` set: POSTs JSON payload with HMAC-SHA256 signature (`X-SensorIoT-Signature: sha256=...`)
- Notification text uses `longname` from `Nicknames` collection (falls back to `shortname`, then `node_id`)
- `firebase_admin` is an optional import — runs without it in webhook-only mode

| Flag | Default | Purpose |
|---|---|---|
| `--db` | `TEST` | `PROD` or `TEST` |
| `--dbconn` | `''` | MongoDB host (overrides `--db`) |
| `--interval` | `0` | Run every N minutes; `0` = run once and exit |
| `--firebase-key` | `../sensoriot-rest/firebase_service_account.json` | Path to Firebase service account JSON |

### Flutter app architecture (summary — see `sensoriot-app/CLAUDE.md` for full detail)

- **State management**: Provider (`AuthProvider`, `SenseProvider`, `AnomalyModelProvider`, `NoaaWeatherProvider`, `AlertRulesProvider`, `RegressionModelProvider`)
- **Base screen class**: `lib/screens/base_state.dart` — owns gateway selection, latest data, and nickname map; all sensor screens extend it
- **API client**: `lib/services/api_service.dart` — no auth headers (except Google-auth endpoints)
- **Encryption**: AES-256-CBC via `lib/services/encryption_service.dart` (Sense credentials)
- **Anomaly detection**: Rolling Z-score in `lib/utils/anomaly_detection.dart` (`windowSize=10`, `threshold=3.0`); optional server-side ML model
- **Derived metrics**: `lib/utils/derived_metrics.dart` — `computeHeatIndex()`, `computeDewPoint()` computed client-side from F+H readings; appended to gauge grid on dashboard
- **NOAA overlay**: Configured in Settings (lat/lon + outside sensor picker + predictive alert thresholds); chart fetches `/forecast/{gw}?hours_back=N`; renders muted past + bright future series
- **Alert rules**: `lib/screens/alert_rules_screen.dart` — CRUD UI; rules stored in MongoDB via REST API; push delivery via FCM (AlertPublisher.py) or webhook
- **Heatmap**: `lib/screens/heatmap_screen.dart` — GitHub-style 52×7 calendar grid, accessible from chart AppBar
- **Sensor health**: `lib/screens/sensor_health_screen.dart` — BAT/RSSI table per node, accessible from dashboard AppBar
- **Baseline overlay**: `lib/widgets/timeseries_chart.dart` — `RangeAreaSeries` band showing normal range when baseline is active
- **FCM push**: `main.dart` initialises Firebase, requests notification permission, registers device token on login via `POST /device_token`; platform detected with `dart:io Platform.isIOS`
- **iOS widget**: `ios/SensorWidgetExtension/` — shares data via App Group `group.com.brintontech.sensoriot`

### Regression forecasting (`appbackend/regression_training.py`)

Per-sensor (node×type) supervised regression pipeline (v2). Key details:

- **Data**: All historical data used (no lookback cap), plus sibling sensor data and NOAA outdoor temperature
- **Feature engineering (v2)**: 15 features — cyclic time (hour/dow), seasonal (month/week-of-year), rolling stats (mean/std over 6/12/24h windows), NOAA outdoor temp, sibling sensor value
- **Models**: 8 hyperparameter variants across Ridge, GradientBoosting, HistGradientBoosting, and RandomForest
- **Validation**: TimeSeriesSplit cross-validation (5 folds); winner chosen by mean R²; MAE also tracked
- **Multi-step forecasting**: Direct prediction (no lag features) — rolling stats are computed from `recent_values` stored at training time, extended with each prediction step
- **Storage**: `models/{gw}/regression/{node}_{type}.joblib` + `_meta.json` (includes `feature_version`, `has_noaa`, `r2`, `rmse`, `mae`, `recent_values`, `sibling_mean`, `num_rows`, `trained_at`)
- **Backward compatibility**: v1 models (without `feature_version`) use legacy prediction path; v2 uses enriched features
- **Experiment script**: `regression_experiment.py` evaluates feature/model combinations on real data via SSH tunnel

### Anomaly detection (`sensoriot-rest/anomaly_training.py`)

Online training pipeline integrated into the REST server. Each gateway gets its own model trained on aligned multi-node sensor readings. Key details:

- **Feature engineering** (`_add_engineered_features`): cyclic time features (hour sin/cos, day-of-week sin/cos), and per-sensor rolling trend features (delta, rolling mean, rolling std over 6-bucket window)
- **NOAA integration**: when `NOAASettings.enabled=True` for a gateway, `noaa_forecast_F` is forward-filled and included as a feature; otherwise it is dropped
- **Bucket size**: computed from median inter-reading interval per node; smallest of `[60, 120, 300, 600, 900, 1800, 3600]` s that covers all nodes
- **Detectors**: Isolation Forest, One-Class SVM, Negative-Sampling Random Forest (MADI library); winner chosen by AUC
- **Model storage**: `sensoriot-rest/models/{gateway_id}/model.joblib` + `metadata.json` (`model_type`, `auc`, `feature_columns`, `nodes`, `num_rows`, `trained_at`)
- Old models (without new feature columns) continue to work — `predict_anomaly` filters `feature_columns` to those present in the current dataframe

## Deployment

- REST server and Broker each have a `Dockerfile` and wrapper scripts
- Full stack: `docker-compose.yml` orchestrates MongoDB + REST server + Broker
- Nginx reverse proxy: `sensoriot-rest/nginx.conf` (SSL via Let's Encrypt)
- Terraform: provisions IONOS Cloud VDC, networking, and server (`terraform/`)
- Database maintenance: `trimdb.py` (dry-run/delete) and `archivedb.py` (archive to gzipped JSONL then delete):
  ```bash
  pipenv run python3 archivedb.py -d PROD -m 6 --output-dir ./archives --remove
  ./install_archive_cron.sh    # Install monthly cron job
  ```
