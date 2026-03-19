# SensorIoT Server

Consolidated backend services for the SensorIoT environmental sensing system. Receives sensor data via MQTT, stores it in MongoDB, and exposes a REST API for querying.

## Architecture

```
MQTT (from Gateway)
        │
        ▼
┌───────────────┐     ┌──────────┐     ┌───────────────┐
│   Mosquitto   │────▶│  Broker  │────▶│    MongoDB     │
│  MQTT Broker  │     │ (Python) │     │                │
└───────────────┘     └──────────┘     └───────┬───────┘
                                               │
                                       ┌───────▼───────┐
                                       │   REST API    │
                                       │(Flask/Gunicorn)│
                                       └───────┬───────┘
                                               │
                                          HTTPS │
                                               ▼
                                        Mobile App
```

## Components

### broker/
MQTT-to-MongoDB bridge. Subscribes to sensor topics, parses messages, and persists to MongoDB.
- `DataBroker.py` — Main broker application
- `Database.py` — MongoDB helper

### api/
Flask REST API for querying sensor data.
- `server.py` — Main API server (Flask + Gunicorn)
- Endpoints for latest readings, historical data, node lists, nicknames

### docker/
Container configuration for the backend stack.
- `Dockerfile` — Broker container
- `docker-compose.yml` — Full stack orchestration (Mosquitto + MongoDB + Broker + API)
- `mosquitto.conf` — MQTT broker configuration

### scripts/
Operational and maintenance scripts.
- `runserver.sh` / `stopserver.sh` — API server lifecycle
- `runbroker_prod.sh` — Broker startup
- `trimdb.py` — Database maintenance (prune old data)

### docs/
Setup guides for MongoDB and MQTT.

## Quick Start

### With Docker Compose
```bash
cd docker
docker-compose up -d
```

### Manual Setup
```bash
# Install dependencies
pipenv install

# Start the broker (terminal 1)
pipenv run python broker/DataBroker.py --db PROD --dbconn localhost:27017 --host localhost --port 1883

# Start the API server (terminal 2)
cd scripts && ./runserver.sh
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Health check |
| GET | `/stats` | Database row count |
| GET | `/latests?gw=<id>` | Latest readings for a gateway |
| GET | `/nodelists?gw=<id>&period=<days>` | Active nodes for a gateway |
| GET | `/gw/<id>?node=<n>&type=<t>&period=<d>` | Historical sensor data |
| GET | `/get_nicknames?gw=<id>` | Node/gateway friendly names |
| POST | `/save_nicknames` | Save node/gateway nicknames |

## Dependencies

- Python 3.10+
- MongoDB
- Mosquitto MQTT broker
- pymongo, paho-mqtt, Flask, flask-cors, gunicorn

## Related Repositories

- **SensorIoT-GW** — Embedded firmware and hardware designs (gateway, nodes, display)
- **SensorIoT_app** — React Native mobile app

## Consolidation History

This repo now contains code previously split across:
- `SensorIoT-Broker` — MQTT broker and data bridge (original home of this repo)
- `SensorIoT-REST_server` — REST API server
