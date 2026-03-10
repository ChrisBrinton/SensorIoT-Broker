# SensorIoT — MQTT Broker

MQTT-to-MongoDB bridge with NOAA weather forecast publishing and alert rule evaluation. Runs three Python processes inside a single Docker container.

## Quick Start

```bash
pipenv install

# DataBroker (MQTT listener):
pipenv run python3 DataBroker.py --db TEST
pipenv run python3 DataBroker.py --db PROD

# Background (production):
./runbroker_prod.sh

# NOAA Publisher:
pipenv run python3 NOAAPublisher.py --db PROD                 # Run once
pipenv run python3 NOAAPublisher.py --db PROD --interval 60   # Hourly loop

# Alert Publisher:
pipenv run python3 AlertPublisher.py --db PROD --interval 5   # Every 5 min
pipenv run python3 AlertPublisher.py --db PROD                # Run once
```

Docker:
```bash
./build_docker.sh && ./docker_run.sh
```

Tests:
```bash
pipenv install --dev
pipenv run pytest test_databroker.py -v
```

## Components

### DataBroker.py

Subscribes to MQTT topic `/GDESGW1/#`, parses incoming messages, and writes to MongoDB.

**Message flow:**
1. `on_connect` — subscribes to `/GDESGW1/#`
2. `on_message` — splits topic into `model/gateway_id/node_id/type`, validates structure
3. Inserts document into `Sensors` collection (full history)
4. Upserts document into `SensorsLatest` collection (current state)

**MQTT topic structure:**
```
/GDESGW1/{model}/{gateway_id}/{node_id}/{type}
```

Type codes: `F` (temp °F), `H` (humidity %), `PWR` (watts), `P` (pressure), `BAT`, `RSSI`.

**CLI flags:**

| Flag | Default | Purpose |
|---|---|---|
| `--db` | `TEST` | `PROD` (`gdtechdb_prod`) or `TEST` (`gdtechdb_test`) |
| `--dbconn` | `host.docker.internal` | MongoDB host |
| `--host` | `0.0.0.0` | MQTT listener address |
| `--port` | `1883` | MQTT listener port |
| `--log` | off | Print each message to stdout |

### NOAAPublisher.py

Fetches hourly NOAA weather forecasts for all opted-in users and writes virtual sensor records to MongoDB.

**Per-run loop** (for each `NOAASettings` doc where `enabled=true`):
1. `GET https://api.weather.gov/points/{lat},{lon}` → extract forecast URL
2. Fetch up to 48 hourly forecast periods
3. Delete stale future `noaa_forecast` records from `Sensors`
4. Insert fresh records: `{model:'NOAA', node_id:'noaa_forecast', type:'F', value, time}`

Past records accumulate naturally (only future records are replaced).

**Predictive weather alerts** (after publishing):
- Checks next-24h forecast for frost (`<= frost_threshold`), heat (`>= heat_threshold`), and cold-front events
- Fires FCM push notifications with 6-hour per-gateway cooldown when `predictive_alerts_enabled=true`
- Requires `firebase_admin` (degrades gracefully without it)

**CLI flags:**

| Flag | Default | Purpose |
|---|---|---|
| `--db` | `TEST` | `PROD` or `TEST` |
| `--dbconn` | `host.docker.internal` | MongoDB host |
| `--interval` | — | Loop interval in minutes; omit to run once |
| `--firebase-key` | `../sensoriot-rest/firebase_service_account.json` | Firebase service account path |

### AlertPublisher.py

Evaluates all enabled `AlertRules` in MongoDB and fires notifications.

**Evaluation loop:**
1. Read `SensorsLatest` for each rule's `(gateway_id, node_id, type)`
2. Handle legacy values stored as `"b'49.46'"` by stripping the wrapper
3. Compare against rule's operator and threshold
4. Respect per-rule `cooldown_minutes`; update `last_triggered` on fire
5. **FCM push** — Look up `DeviceTokens` for the owner; auto-remove stale tokens (`UnregisteredError`)
6. **Webhook** — POST JSON with HMAC-SHA256 signature (`X-SensorIoT-Signature: sha256=...`)

Notification text uses `longname` from `Nicknames` (falls back to `shortname`, then `node_id`).

**CLI flags:**

| Flag | Default | Purpose |
|---|---|---|
| `--db` | `TEST` | `PROD` or `TEST` |
| `--dbconn` | `''` | MongoDB host (overrides `--db`) |
| `--interval` | `0` | Loop interval in minutes; `0` = run once |
| `--firebase-key` | `../sensoriot-rest/firebase_service_account.json` | Firebase service account path |

## Docker Container

**Base image:** `ubuntu:22.04`
**Exposed ports:** 1883, 1884 (MQTT)

**Startup sequence** (`startup.sh`):
1. Start Mosquitto daemon
2. Start NOAAPublisher in background (every 60 min)
3. Start AlertPublisher in background (every 1 min)
4. Start DataBroker in foreground

**Environment:** `MONGODB_HOST` (default: `127.0.0.1`)

## Firebase Setup

Place Firebase Admin SDK service account JSON at:
- Local: `firebase_service_account.json` in this directory
- Docker: Copied to `/firebase_service_account.json` during build

Both `AlertPublisher` and `NOAAPublisher` try/except import `firebase_admin` — they run in webhook-only mode if the SDK or key is unavailable.

## Cron Alternative

Instead of running publishers with `--interval`, use system cron:

```bash
# NOAA (hourly):
0 * * * *  cd /path/to/sensoriot-broker && pipenv run python3 NOAAPublisher.py --db PROD >> noaa.log 2>&1

# Alerts (every 5 min):
*/5 * * * *  cd /path/to/sensoriot-broker && pipenv run python3 AlertPublisher.py --db PROD >> alert.log 2>&1
```
