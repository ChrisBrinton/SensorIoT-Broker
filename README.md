# SensorIoT Backend

Monorepo for the SensorIoT IoT monitoring platform backend. Physical sensors publish readings over MQTT; a Python broker persists them to MongoDB; a Flask REST API serves the data with ML anomaly detection and regression forecasting.

## Repository Structure

```
├── appbackend/       Flask REST API + ML pipelines + Google Home integration
├── broker/           MQTT→MongoDB bridge + NOAA publisher + alert evaluation
├── scripts/          Deployment tools (rebuild, deploy, SSH tunnel)
├── terraform/        IONOS Cloud infrastructure provisioning
└── docs/             Architecture, design, and deployment documentation
```

## Quick Start

```bash
# Clone and set up
git clone https://github.com/keyvanazami/SensorIoT-Broker.git
cd SensorIoT-Broker

# Copy your environment file
cp /path/to/.env appbackend/.env

# Run with Docker Compose
docker compose up --build
```

## Deploy to Production

```bash
scripts/rebuild_container.sh              # Build + deploy both containers
scripts/rebuild_container.sh -t rest      # REST server only
scripts/rebuild_container.sh -t broker    # Broker only
```

## Related Repos

- **Flutter app**: [sensoriot_app2](https://github.com/keyvanazami/sensoriot_app2) — iOS/Android mobile app
- Run `scripts/setup.sh` to clone the app repo alongside this one

See [docs/](docs/) for detailed architecture, design, and deployment documentation.
