# SensorIoT

A full-stack IoT sensor monitoring platform. Physical sensors publish readings over MQTT; a Python broker persists them to MongoDB; a Flask REST API serves the data; a Flutter mobile app displays live gauges, historical charts, anomaly alerts, weather forecast overlays, and regression-based room temperature forecasts. An iOS home-screen widget and Google Home integration are also included.

**Detailed documentation:**
- [DESIGN.md](DESIGN.md) — System architecture, data flow, database schema, ML pipelines, app design
- [DEPLOYMENT.md](DEPLOYMENT.md) — Docker, Nginx, Terraform, remote deployment, Firebase, database maintenance

---

## System Architecture

```
IoT Sensors
    │
    │  MQTT (port 1883)
    │  Topic: /GDESGW1/{model}/{gateway_id}/{node_id}/{type}
    ▼
┌─────────────────────────┐
│   DataBroker.py         │  Python / Paho MQTT
│   (sensoriot-broker/)   │  Parses topic, writes to MongoDB
└────────────┬────────────┘
             │
┌─────────────────────────┐     ┌─────────────────────────┐
│   NOAAPublisher.py      │     │   AlertPublisher.py     │
│   Hourly NOAA forecasts │     │   Alert rule evaluation │
│   + predictive weather  │     │   FCM push + webhooks   │
│     alerts (frost/heat) │     │   (every 1–5 min)       │
└────────────┬────────────┘     └────────────┬────────────┘
             │                               │
             ▼                               ▼
┌─────────────────────────────────────────────────────────┐
│   MongoDB                                               │
│   Sensors · SensorsLatest · Nicknames · AlertRules ·    │
│   DeviceTokens · NOAASettings · UserProfiles · …        │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│   REST API (server.py)  Flask / Gunicorn / Nginx (SSL)  │
│   + anomaly_training.py (IF / OC-SVM / NS-RF)          │
│   + regression_training.py (Ridge / RF / GBT)           │
└────────────────────────┬────────────────────────────────┘
                         │  HTTPS (brintontech.com)
              ┌──────────┼──────────┐
              ▼                     ▼
┌──────────────────────┐  ┌──────────────────────┐
│   Flutter Mobile App │  │   Google Home        │
│   iOS + Android      │  │   (OAuth + Smart     │
│   + iOS Widget       │  │    Home fulfillment) │
└──────────────────────┘  └──────────────────────┘
```

---

## Components

| Directory | Language / Stack | Role |
|---|---|---|
| `sensoriot-app/` | Flutter / Dart | Mobile app (iOS & Android) |
| `sensoriot-rest/` | Python, Flask, Gunicorn, Nginx | REST API + Google Home OAuth + ML anomaly detection + regression forecasting |
| `sensoriot-broker/` | Python, Paho MQTT | MQTT→MongoDB bridge + NOAA publisher + alert evaluation (FCM/webhook) |
| `terraform/` | Terraform | IONOS Cloud infrastructure provisioning |

---

## Features

### Sensor Monitoring
- Live radial-gauge dashboard with pull-to-refresh
- Historical time-series charts (3h / 24h / 3d / 7d presets)
- Multi-gateway support with per-sensor/gateway nicknames
- Derived metrics: heat index and dew point (computed client-side)
- Sensor health view: battery level and signal strength per node

### Analytics & ML
- **Anomaly detection** — Client-side rolling Z-score (configurable σ threshold) + server-side ML (Isolation Forest / One-Class SVM / Negative-Sampling RF)
- **Regression forecasting** — Per-sensor predictive model (Ridge / Random Forest / Gradient Boosted Trees) with TimeSeriesSplit CV
- **Baseline overlay** — Per-hour-of-week normal range band on charts
- **Heatmap** — GitHub-style 52×7 calendar grid showing daily min/max/avg

### Weather & Alerts
- **NOAA forecast overlay** — Hourly weather forecast plotted alongside outdoor sensor data
- **Predictive weather alerts** — Frost, heat, and cold-front push notifications based on NOAA forecast
- **Custom alert rules** — Threshold, comparison, and offline alerts with per-rule cooldown
- **Dual delivery** — FCM push notifications + webhook (HMAC-SHA256 signed)

### Integrations
- **Google Home** — OAuth account linking + Smart Home SYNC/QUERY/EXECUTE
- **Sense Energy** — Power monitoring via encrypted credential storage
- **iOS Widget** — Home-screen widget with configurable sensor, trend detection, and 15-min refresh

---

## Getting Started

```bash
git clone https://github.com/keyvanazami/sensoriot-deploy.git
cd sensoriot-deploy
./setup.sh          # clones sensoriot-rest, sensoriot-broker, sensoriot-app
```

### Prerequisites

- Python 3.10+, Pipenv
- Flutter SDK 3.x (Dart ^3.6.1)
- Docker & Docker Compose
- MongoDB (local or remote)

### Local Development

```bash
# SSH tunnel to production MongoDB
bash mongo_tunnel.sh

# REST server (interactive)
cd sensoriot-rest && pipenv install && ./runinteractivesvr.sh

# MQTT broker
cd sensoriot-broker && pipenv install && pipenv run python3 DataBroker.py --db TEST

# Flutter app
cd sensoriot-app && flutter pub get && flutter run
```

### Docker Compose (Full Stack)

```bash
docker compose up --build
```

### Deploy to Production

```bash
./rebuild_container.sh              # Build + deploy both containers
./rebuild_container.sh -t rest      # REST server only
./rebuild_container.sh -t broker    # Broker only
```

See [DEPLOYMENT.md](DEPLOYMENT.md) for complete deployment instructions.

---

## API Reference

Base URL: `https://brintontech.com` — All responses are JSON. Authentication via Google ID token where noted.

### Sensor Data

| Method | Path | Description |
|---|---|---|
| GET | `/latest/{gw}` | Current readings for all sensors on a gateway |
| GET | `/latests` | Batch current readings (`?gw=gw1&gw=gw2`) |
| GET | `/sensor/{node}` | Historical readings (`?period=days&skip=0&type=F`) |
| GET | `/gw/{gw}` | Per-node history with timezone |
| GET | `/nodelist/{gw}` | List node IDs on a gateway |
| GET | `/nodelists` | Batch node lists |
| GET | `/forecast/{gw}` | NOAA forecast records |
| GET | `/heatmap/{gw}` | Daily min/max/avg aggregation |

### User & Configuration (Google auth required)

| Method | Path | Description |
|---|---|---|
| GET/POST | `/user_profile` | Fetch or upsert user profile |
| GET/POST | `/noaa_settings` | Fetch or upsert NOAA config |
| GET/POST | `/alert_rules` | List or create alert rules |
| PUT/DELETE | `/alert_rules/<rule_id>` | Update or delete a rule |
| POST | `/device_token` | Register FCM device token |

### Nicknames & Third-Party

| Method | Path | Description |
|---|---|---|
| GET | `/get_nicknames` | Sensor/gateway display names |
| POST | `/save_nicknames` | Update display names |
| POST | `/add_3p_service` | Save encrypted credentials |
| GET | `/get_3p_services` | Retrieve stored credentials |
| GET | `/testsense` | Fetch Sense Energy power |

### ML & Analytics

| Method | Path | Description |
|---|---|---|
| POST | `/train_anomaly_model` | Start anomaly model training |
| GET | `/training_status` | Poll anomaly training job |
| GET | `/predict_anomaly` | Get anomalous timestamps |
| GET | `/anomaly_model_status` | Anomaly model metadata |
| POST | `/train_regression_model` | Start regression model training |
| GET | `/regression_training_status` | Poll regression training job |
| GET | `/regression_model_status` | Regression model metadata (R², RMSE) |
| GET | `/regression_forecast` | Get predicted future values |
| POST | `/compute_baseline` | Compute baseline |
| GET | `/baseline/{gw}` | Fetch baseline buckets |
| GET | `/baseline_status/{gw}` | Check baseline existence |

### Google Home

| Method | Path | Description |
|---|---|---|
| GET/POST | `/auth` | OAuth login + approval |
| POST | `/token` | Token exchange |
| POST | `/fulfillment` | Smart Home intents |

---

## Database

**MongoDB:** `gdtechdb_prod` / `gdtechdb_test`

| Collection | Purpose |
|---|---|
| `Sensors` | Full historical readings (one doc per MQTT message + NOAA virtual records) |
| `SensorsLatest` | Current state (one doc per sensor, upserted) |
| `Nicknames` | Sensor display names `(gateway_id, node_id)` |
| `GWNicknames` | Gateway display names |
| `UserProfiles` | `{email, gateway_ids[]}` |
| `NOAASettings` | Per-user NOAA config (unique on `email`) |
| `AlertRules` | Per-user alert rules with cooldown and delivery config |
| `DeviceTokens` | FCM tokens, upserted on `(email, platform)` |
| `ThirdPartyServices` | Encrypted third-party credentials |

See [DESIGN.md](DESIGN.md) for full schema documentation.

---

## MQTT Topic Structure

```
/GDESGW1/{model}/{gateway_id}/{node_id}/{type}
```

| Type | Meaning | Unit |
|---|---|---|
| `F` | Temperature | °F |
| `H` | Humidity | % |
| `PWR` | Power | Watts |
| `P` | Pressure | — |
| `BAT` | Battery | — |
| `RSSI` | Signal strength | dBm |

---

## Running Tests

```bash
cd sensoriot-rest && pipenv install --dev && pipenv run pytest -v
cd sensoriot-broker && pipenv install --dev && pipenv run pytest test_databroker.py -v
cd sensoriot-app && flutter test --reporter=expanded
```
