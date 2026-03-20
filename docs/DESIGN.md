# SensorIoT — System Design

This document describes the architecture, data flow, database schema, ML pipelines, and mobile app design of the SensorIoT platform.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Data Flow](#2-data-flow)
3. [Component Architecture](#3-component-architecture)
4. [Database Design](#4-database-design)
5. [REST API](#5-rest-api)
6. [MQTT Protocol](#6-mqtt-protocol)
7. [ML & Analytics Pipelines](#7-ml--analytics-pipelines)
8. [Mobile App Architecture](#8-mobile-app-architecture)
9. [Google Home Integration](#9-google-home-integration)
10. [Security](#10-security)

---

## 1. System Overview

SensorIoT is a full-stack IoT sensor monitoring platform. Physical sensors publish readings over MQTT; a Python broker persists them to MongoDB; a Flask REST API serves the data; and a Flutter mobile app displays live gauges, historical charts, anomaly alerts, weather forecast overlays, and regression forecasts.

### Component Map

| Directory | Language / Stack | Role |
|---|---|---|
| `sensoriot-app/` | Flutter / Dart | Mobile app (iOS & Android) |
| `sensoriot-rest/` | Python, Flask, Gunicorn, Nginx | REST API + Google Home OAuth + ML (anomaly + regression) |
| `sensoriot-broker/` | Python, Paho MQTT, Mosquitto | MQTT→MongoDB bridge + NOAA publisher + alert evaluation |
| `terraform/` | Terraform (IONOS Cloud) | Infrastructure provisioning |

Each component is independently deployable. Infrastructure files (`docker-compose.yml`, `rebuild_container.sh`, `mongo_tunnel.sh`, `terraform/`) live in the separate [sensoriot-deploy](https://github.com/keyvanazami/sensoriot-deploy) repo.

---

## 2. Data Flow

```
                    ┌──────────────────────────────────────────────┐
                    │               IoT Sensors                     │
                    │  (DHT22, power monitors, pressure sensors)    │
                    └──────────────────┬───────────────────────────┘
                                       │  MQTT (port 1883)
                                       │  Topic: /GDESGW1/{model}/{gw}/{node}/{type}
                                       ▼
                    ┌──────────────────────────────────────────────┐
                    │             Mosquitto Broker                  │
                    │           (sensoriot-broker container)        │
                    └──────────────────┬───────────────────────────┘
                                       │
              ┌────────────────────────┼────────────────────────┐
              │                        │                        │
              ▼                        ▼                        ▼
┌─────────────────────┐  ┌─────────────────────┐  ┌─────────────────────┐
│   DataBroker.py     │  │  NOAAPublisher.py   │  │  AlertPublisher.py  │
│   MQTT→MongoDB      │  │  NOAA API→MongoDB   │  │  Rules→FCM/Webhook  │
│   (Sensors +        │  │  (hourly forecast   │  │  (every 1–5 min)    │
│    SensorsLatest)   │  │   virtual records)  │  │                     │
└────────┬────────────┘  └────────┬────────────┘  └────────┬────────────┘
         │                        │                        │
         └────────────────────────┼────────────────────────┘
                                  ▼
                    ┌──────────────────────────────────────────────┐
                    │               MongoDB                        │
                    │       gdtechdb_prod / gdtechdb_test          │
                    │  Sensors · SensorsLatest · Nicknames ·       │
                    │  AlertRules · DeviceTokens · NOAASettings ·  │
                    │  UserProfiles · Baselines · …                │
                    └──────────────────┬───────────────────────────┘
                                       │
                                       ▼
                    ┌──────────────────────────────────────────────┐
                    │         REST API (server.py)                 │
                    │  Flask / Gunicorn / Nginx (SSL)              │
                    │  + anomaly_training.py (Isolation Forest,    │
                    │    OC-SVM, NS-RF)                            │
                    │  + regression_training.py (Ridge, RF, GBT)   │
                    └──────────────────┬───────────────────────────┘
                                       │  HTTPS (brintontech.com)
                         ┌─────────────┼─────────────┐
                         ▼                           ▼
              ┌─────────────────────┐     ┌─────────────────────┐
              │   Flutter App       │     │   Google Home        │
              │   iOS + Android     │     │   (auth.py,          │
              │   + iOS Widget      │     │    fulfillment.py)   │
              └─────────────────────┘     └─────────────────────┘
```

---

## 3. Component Architecture

### 3.1 MQTT Broker Service (`sensoriot-broker/`)

The broker container runs three Python processes supervised by `startup.sh`:

| Process | Schedule | Purpose |
|---|---|---|
| **DataBroker.py** | Continuous | Subscribes to `/GDESGW1/#`, parses topics, writes to `Sensors` + upserts `SensorsLatest` |
| **NOAAPublisher.py** | Every 60 min | Fetches hourly NOAA forecasts for opted-in users, writes virtual sensor records; fires predictive weather alerts (frost/heat/cold-front) via FCM |
| **AlertPublisher.py** | Every 1 min | Evaluates all enabled `AlertRules`, fires FCM push and/or webhook notifications |

Also starts a local **Mosquitto** MQTT daemon on ports 1883/1884.

### 3.2 REST API Service (`sensoriot-rest/`)

Flask application served by Gunicorn (4 workers, port 5050) behind Nginx (SSL on 443).

Key modules:

| Module | Role |
|---|---|
| `server.py` | All REST endpoints, CORS, Google token verification |
| `anomaly_training.py` | Unsupervised anomaly detection pipeline (per-gateway) |
| `regression_training.py` | Supervised regression forecasting pipeline (per-sensor) |
| `auth.py` | Google Home OAuth mock (login + approval) |
| `fulfillment.py` | Google Home Smart Home webhook (SYNC/QUERY/EXECUTE) |
| `app_state.py` | In-memory OAuth codes/tokens/mock devices |
| `archivedb.py` | Archive old sensor data to gzipped JSONL |
| `trimdb.py` | Dry-run / delete old records |

### 3.3 Flutter Mobile App (`sensoriot-app/`)

Provider-based state management with a service-layer pattern. See [Section 8](#8-mobile-app-architecture) for full detail.

---

## 4. Database Design

**Engine:** MongoDB
**Databases:** `gdtechdb_prod` (production), `gdtechdb_test` (testing)

### Collections

#### Sensors (historical archive)

One document per MQTT message. Also stores NOAA virtual records and baseline aggregations. Grows continuously; pruned monthly by `archivedb.py`.

```json
{
  "model":      "DHT22",
  "gateway_id": "GW-001",
  "node_id":    "node_5",
  "type":       "F",
  "value":      "72.5",
  "time":       1708643284.123
}
```

NOAA records use `model: "NOAA"`, `node_id: "noaa_forecast"`.

#### SensorsLatest (current state)

Same schema as `Sensors`. Upserted on every MQTT message — one document per `(gateway_id, node_id, type)`. NOAA records are **not** written here.

#### Nicknames

Sensor display names keyed by `(gateway_id, node_id)`.

```json
{
  "gateway_id": "GW-001",
  "node_id":    "node_5",
  "shortname":  "Living Room",
  "longname":   "Living Room Temperature",
  "seq_no":     1
}
```

#### GWNicknames

Gateway display names.

```json
{ "gateway_id": "GW-001", "longname": "Main House", "seq_no": 1 }
```

#### UserProfiles

```json
{
  "email":       "user@example.com",
  "gateway_ids": ["GW-001", "GW-002"],
  "updated_at":  1708643284.123
}
```

#### ThirdPartyServices (encrypted credentials)

```json
{
  "service_name": "Sense Energy",
  "login":        "user@example.com",
  "password":     "<base64 AES-256-CBC ciphertext>",
  "service_type": "Sense"
}
```

#### NOAASettings

Per-user NOAA config. Unique index on `email`.

```json
{
  "email":                    "user@example.com",
  "lat":                      42.35,
  "lon":                     -71.06,
  "gateway_id":               "GW-001",
  "outside_sensor_id":        "node_5",
  "enabled":                  true,
  "predictive_alerts_enabled": true,
  "frost_threshold":          32,
  "heat_threshold":           95
}
```

#### AlertRules

Per-user alert rules. Each rule targets a specific sensor reading.

```json
{
  "rule_id":          "uuid-string",
  "email":            "user@example.com",
  "gateway_id":       "GW-001",
  "node_id":          "node_5",
  "type":             "F",
  "operator":         "lt",
  "threshold":        35.0,
  "offline_minutes":  30,
  "cooldown_minutes": 60,
  "push_enabled":     true,
  "webhook_url":      "https://example.com/hook",
  "webhook_secret":   "secret-key",
  "label":            "Freeze warning",
  "enabled":          true,
  "last_triggered":   1708643284.123
}
```

Supported operators: `lt` (less than), `gt` (greater than), `eq` (equal), `offline` (no data for N minutes).

#### DeviceTokens

FCM device tokens. Upserted on `(email, platform)` — only one token per platform per user.

```json
{
  "email":      "user@example.com",
  "platform":   "ios",
  "token":      "fcm-device-token-string",
  "updated_at": 1708643284.123
}
```

#### Baselines

Per-hour-of-week baseline aggregations for a gateway's sensors. Computed via `POST /compute_baseline`.

---

## 5. REST API

**Base URL:** `https://brintontech.com`
**Auth:** None for most endpoints; Google ID token (`Authorization: Bearer`) for user-specific endpoints.

### Sensor Data Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/latest/{gw}` | — | Current readings for all sensors on a gateway |
| GET | `/latests` | — | Batch current readings (`?gw=gw1&gw=gw2`) |
| GET | `/sensor/{node}` | — | Historical readings (`?period=days&skip=0&type=F`) |
| GET | `/gw/{gw}` | — | Per-node history with timezone (`?node=n&type=F&period=24&timezone=America/New_York`) |
| GET | `/nodelist/{gw}` | — | List node IDs on a gateway |
| GET | `/nodelists` | — | Batch node lists (`?gw=gw1&gw=gw2`) |
| GET | `/forecast/{gw}` | — | NOAA forecast records (`?node=noaa_forecast&hours_back=0`) |
| GET | `/heatmap/{gw}` | — | Daily min/max/avg aggregation (`?node=&type=&year=`) |

### User & Configuration Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| GET/POST | `/user_profile` | Google | Fetch or upsert `{email, gateway_ids[]}` |
| GET/POST | `/noaa_settings` | Google | Fetch or upsert NOAA config |
| GET | `/get_nicknames` | — | Display names (`?gw={gatewayId}`) |
| POST | `/save_nicknames` | — | Update sensor/gateway display names |
| POST | `/add_3p_service` | — | Save encrypted third-party credentials |
| GET | `/get_3p_services` | — | Retrieve stored credentials |
| GET | `/testsense` | — | Fetch Sense Energy active power |

### Alert Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| GET/POST | `/alert_rules` | Google | List or create alert rules |
| PUT/DELETE | `/alert_rules/<rule_id>` | Google | Update or delete a rule |
| POST | `/device_token` | Google | Register FCM device token |

### Anomaly Detection Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/train_anomaly_model` | — | Start background ML training for gateways |
| GET | `/training_status` | — | Poll training job progress |
| GET | `/predict_anomaly` | — | Get anomalous timestamps for a node |
| GET | `/anomaly_model_status` | — | Model metadata for a gateway |

### Regression Forecasting Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/train_regression_model` | — | Start per-sensor regression training |
| GET | `/regression_training_status` | — | Poll regression training job |
| GET | `/regression_model_status` | — | Model metadata (R², RMSE, row count) |
| GET | `/regression_forecast` | — | Get predicted future values for a sensor |

### Baseline Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/compute_baseline` | — | Compute per-hour-of-week baseline |
| GET | `/baseline/{gw}` | — | Fetch saved baseline buckets |
| GET | `/baseline_status/{gw}` | — | Check whether a baseline exists |

### Google Home Endpoints

| Method | Path | Description |
|---|---|---|
| GET/POST | `/auth` | OAuth login page + approval |
| POST | `/token` | Exchange code for access/refresh tokens |
| POST | `/fulfillment` | SYNC / QUERY / EXECUTE intents |

---

## 6. MQTT Protocol

### Topic Structure

```
/GDESGW1/{model}/{gateway_id}/{node_id}/{type}
```

Example: `/GDESGW1/DHT22/GW-001/node_5/F` with payload `72.5`

### Type Codes

| Code | Meaning | Unit |
|---|---|---|
| `F` | Temperature | °F |
| `H` | Humidity | % |
| `PWR` | Power | Watts |
| `P` | Pressure | — |
| `BAT` | Battery level | — |
| `RSSI` | Signal strength | dBm |

`BAT` and `RSSI` are stored in the database but filtered out in the mobile app UI. They are visible on the Sensor Health screen.

### DataBroker Message Flow

1. `on_connect` — subscribes to `/GDESGW1/#`
2. `on_message` — splits topic into 6 parts, validates structure
3. Inserts one document into `Sensors` (full history)
4. Upserts one document into `SensorsLatest` (keyed on `gateway_id + node_id + type`)

Both documents carry: `model`, `gateway_id`, `node_id`, `type`, `value`, `time` (Unix float).

---

## 7. ML & Analytics Pipelines

### 7.1 Anomaly Detection (`anomaly_training.py`)

**Scope:** One unsupervised model per gateway, trained on aligned multi-node sensor readings.

**Pipeline:**

1. **Data retrieval** — Query all F/H/P readings for the gateway; pivot to wide format with `{node_id}_{type}` columns
2. **Bucketing** — Compute per-node median inter-reading interval; pick smallest candidate bucket from `[60, 120, 300, 600, 900, 1800, 3600]` seconds
3. **Feature engineering** (`_add_engineered_features`):
   - Cyclic time features: hour sin/cos, day-of-week sin/cos
   - Per-sensor rolling trend: delta, rolling mean, rolling std (6-bucket window)
4. **NOAA integration** — When `NOAASettings.enabled=True`, `noaa_forecast_F` is forward-filled and included as a feature
5. **Training** — Three detectors trained in parallel:
   - Isolation Forest
   - One-Class SVM
   - Negative-Sampling Random Forest (MADI library)
6. **Selection** — Winner chosen by AUC score
7. **Storage** — `models/{gateway_id}/model.joblib` + `metadata.json`

**Prediction:** `predict_anomalies()` returns Unix timestamps where `class_prob < threshold` (1.0 = normal). Old models without new feature columns continue to work — prediction filters to available columns.

### 7.2 Regression Forecasting (`regression_training.py`)

**Scope:** One supervised model per sensor (node × type), predicting future values.

**Pipeline:**

1. **Data retrieval** — All historical data for the sensor (no 90-day cap)
2. **Feature engineering** — Cyclic time features, lag features, NOAA temperature (when available)
3. **Hyperparameter search** — 10 variants across three model families:
   - Ridge Regression
   - Random Forest Regressor
   - Gradient Boosted Trees
4. **Validation** — TimeSeriesSplit cross-validation; winner chosen by mean R²
5. **Storage** — `models/{gw}/regression/{node}_{type}.joblib` + `_meta.json` (includes `has_noaa` flag, R², RMSE, row count)

**NOAA integration:** Reuses `anomaly_training._backfill_noaa_history()` to get historical NOAA data as a feature. The `has_noaa` flag in metadata indicates whether the model was trained with NOAA features.

### 7.3 Baseline Analysis

Per-hour-of-week aggregation for a gateway's sensors. Computed on demand via `POST /compute_baseline`. Used in the chart as a `RangeAreaSeries` band showing the "normal range" for each hour of the week.

### 7.4 Heatmap Aggregation

Daily min/max/avg aggregation served by `GET /heatmap/{gw}`. Displayed as a GitHub-style 52×7 calendar grid in the app.

### 7.5 Client-Side Analytics

**Rolling Z-score** (`lib/utils/anomaly_detection.dart`):
- Window size: 10 readings; threshold: 3.0σ (user-adjustable 1.5–5.0)
- Rendered as red scatter dots on the chart

**Derived metrics** (`lib/utils/derived_metrics.dart`):
- `computeHeatIndex()` and `computeDewPoint()` — computed client-side from F+H readings
- Appended to gauge grid on the dashboard

### 7.6 Alert Evaluation (`AlertPublisher.py`)

Runs inside the broker container, evaluating all enabled `AlertRules`:

1. Reads `SensorsLatest` for the latest value of each rule's `(gateway_id, node_id, type)`
2. Handles legacy values stored as `"b'49.46'"` by stripping the wrapper
3. Compares value against rule's `operator` and `threshold`
4. Respects per-rule `cooldown_minutes`; updates `last_triggered` on fire
5. **FCM push** — Looks up `DeviceTokens` for the rule owner's email; auto-removes stale tokens on `UnregisteredError`
6. **Webhook** — POSTs JSON payload with HMAC-SHA256 signature (`X-SensorIoT-Signature: sha256=...`)

Notification text uses `longname` from `Nicknames` collection (falls back to `shortname`, then `node_id`).

Type labels in notifications: `F`→temperature (°F), `H`→humidity (%), `PWR`→power (W), `P`→pressure (hPa), `HI`→heat index, `DP`→dew point. Phrase templates: `>` → "rose above", `<` → "dropped below", etc.

**Baseline-actual alerts** (opt-in via `baseline_actual_alert_enabled=True` in `NOAASettings`):
- Compares each sensor's current reading against its baseline ±2σ band
- Fires push if reading is outside expected range
- Per-sensor 60-minute cooldown stored in `NOAASettings.baseline_actual_cooldowns`

### 7.7 NOAA Predictive Weather Alerts (`NOAAPublisher.py`)

After publishing forecast records, checks next-24h forecast for:
- **Frost** — temperature ≤ `frost_threshold`
- **Heat** — temperature ≥ `heat_threshold`
- **Cold-front** — rapid temperature drop events

Fires FCM push notifications with a 6-hour per-gateway cooldown when `predictive_alerts_enabled=true` in `NOAASettings`.

**Baseline-forecast alerts** (when `baseline_forecast_alert_enabled=True`):
- Compares next-24h forecast against outside sensor's baseline ±2σ band
- Fires if any forecast period falls outside expected range
- Separate 6-hour cooldown (`last_baseline_forecast_alert_sent`)

---

## 8. Mobile App Architecture

### 8.1 Entry Point & Initialization (`lib/main.dart`)

1. Initialize Firebase + request notification permission
2. `WidgetService.initialize()` — iOS widget App Group setup
3. `AuthProvider.restoreSession()` — loads saved email
4. Wrap widget tree in `MultiProvider`
5. Register FCM device token on login via `POST /device_token`
6. Start at `/dashboard` (skips login if session exists)

### 8.2 State Management (Provider)

| Provider | Responsibility |
|---|---|
| `AuthProvider` | Google/Apple sign-in; persists email in `SharedPreferences` |
| `SenseProvider` | Sense Energy integration; polls every 60s; encrypted credentials |
| `AnomalyModelProvider` | ML anomaly toggle, training lifecycle, polling, gateway metadata, auto-retrain if >30 days stale |
| `NoaaWeatherProvider` | NOAA config: enabled, lat/lon, outside sensor ID; all persisted |
| `AlertRulesProvider` | Alert rules CRUD; syncs with REST API |
| `RegressionModelProvider` | Regression model toggle, training status, per-sensor metadata (R², RMSE) |

### 8.3 Screens

| Screen | Purpose | Access |
|---|---|---|
| `DashboardScreen` | Live radial-gauge grid; pull-to-refresh; gateway selector chips | Main screen |
| `ChartScreen` | Historical line chart; time presets (3h/24h/3d/7d); anomaly + NOAA + regression overlays | Tap gauge |
| `LoginScreen` | Google and Apple OAuth | `/` route |
| `SettingsScreen` | NOAA config, ML anomaly toggle, regression model toggle, Z-score slider | AppBar |
| `AlertRulesScreen` | Alert rules CRUD UI | AppBar |
| `HeatmapScreen` | GitHub-style 52×7 calendar grid (daily min/max/avg) | Chart AppBar |
| `SensorHealthScreen` | BAT/RSSI table per node | Dashboard AppBar |
| `GoogleHomeScreen` | Sense Energy connect, Google Home link, server URL selector | AppBar |
| `UpdateNicknameScreen` | Edit short/long display names | Settings |
| `SenseLoginScreen` | Sense Energy credentials form | Google Home screen |

### 8.4 Base Screen Class (`lib/screens/base_state.dart`)

Abstract `ScreenState<T>` extended by Dashboard and Chart screens. Manages:
- `latestData` — current sensor readings
- `gatewayIds` / `selectedGatewayId` — gateway selection (persisted)
- `sensorNicknames` — display-name map
- `loadLatestData()`, `loadGatewayIds()`, `fetchNicknames()`, `saveGatewayIds()`

### 8.5 Chart Features

**`HistoricalChart`** (`lib/widgets/timeseries_chart.dart`):
- Syncfusion `SfCartesianChart` with line series per sensor
- **Anomaly markers**: Red dots (Z-score) or orange dots (ML) — mutually exclusive
- **NOAA overlay**: Muted past series + bright future forecast series (cyan dashed)
- **Baseline overlay**: `RangeAreaSeries` band showing normal range
- **Regression forecast**: Teal dashed line showing predicted values (toggled via AppBar icon)
- Polynomial trendline per sensor
- Pinch/scroll zoom, pan, tap tooltip + long-press trackball
- Dynamic x-axis labels based on zoom level

### 8.6 iOS Widget Extension

**Location:** `ios/SensorWidgetExtension/`
**App Group:** `group.com.brintontech.sensoriot`

| Component | Role |
|---|---|
| `SensorWidgetExtension.swift` | `AppIntentTimelineProvider`; fetches `/latest/{gw}` every 15 min; rolling 10-point history; linear-regression trend |
| `SensorIntent.swift` | `SensorConfigurationIntent`; user picks sensor from widget settings |
| `SensorWidgetExtensionEntryView` | SwiftUI; small (1×1) and medium (2×1) layouts; colour-coded by type |

### 8.7 Services

| Service | Purpose |
|---|---|
| `ApiService` | All HTTP calls; base URL overridable via Settings |
| `EncryptionService` | AES-256-CBC encrypt/decrypt; IV prepended to ciphertext |
| `SecureStorageService` | Wraps `flutter_secure_storage` |
| `GoogleHomeService` | `requestSync()` + `openGoogleHomeApp()` |
| `WidgetService` | Writes sensor data to App Group UserDefaults for iOS widget |

---

## 9. Google Home Integration

### OAuth Flow

1. Google Home app directs user to `GET /auth` → login page
2. User approves → redirect with authorization code
3. Google exchanges code via `POST /token` → access + refresh tokens
4. Google calls `POST /fulfillment` with Smart Home intents

### Intents

| Intent | Action |
|---|---|
| SYNC | Returns list of devices (sensors) with traits |
| QUERY | Returns current sensor states from `SensorsLatest` |
| EXECUTE | Acknowledges commands (sensors are read-only) |

**Limitation:** `app_state.py` holds OAuth codes, tokens, and mock devices **in-memory** — state is lost on server restart.

---

## 10. Security

### Authentication
- Most sensor data endpoints are unauthenticated (read-only sensor data)
- User-specific endpoints (`/user_profile`, `/noaa_settings`, `/alert_rules`, `/device_token`) require Google ID token verification
- Google Home OAuth uses a separate mock flow via `auth.py`

### Encryption
- Third-party credentials (Sense Energy) encrypted with AES-256-CBC
- Shared key stored in `.env` (`AES_SHARED_KEY`) — base64-encoded 256-bit key
- IV prepended to ciphertext
- Mobile app uses `flutter_secure_storage` (iOS Keychain / Android Keystore)

### Webhook Security
- Alert webhooks include HMAC-SHA256 signature in `X-SensorIoT-Signature: sha256=...` header
- Secret is per-rule (`webhook_secret` field in `AlertRules`)

### Network
- All client traffic over HTTPS (Nginx SSL termination via Let's Encrypt)
- MQTT on port 1883 (unencrypted, intended for LAN/VPN sensor traffic)
- MongoDB accessed via SSH tunnel for local development
- Firewall rules (Terraform): only SSH, HTTP, HTTPS, and MQTT ports open
