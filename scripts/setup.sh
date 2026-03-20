#!/bin/bash
# setup.sh — clone the Flutter app alongside the backend monorepo
#
# Usage:
#   git clone https://github.com/keyvanazami/SensorIoT-Broker.git
#   cd SensorIoT-Broker
#   ./scripts/setup.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$SCRIPT_DIR/.."
cd "$REPO_ROOT/.."

[ -d sensoriot-app ] || git clone https://github.com/keyvanazami/sensoriot_app2.git sensoriot-app

echo ""
echo "Done. Layout:"
echo "  $(basename "$(pwd)")/"
echo "  ├── SensorIoT-Broker/   (backend monorepo — you are here)"
echo "  │   ├── appbackend/     (REST API + ML)"
echo "  │   ├── broker/         (MQTT bridge + alerts)"
echo "  │   ├── scripts/        (deploy tools)"
echo "  │   └── terraform/"
echo "  └── sensoriot-app/      (Flutter app)"
echo ""
echo "Next steps:"
echo "  1. Copy your .env file into SensorIoT-Broker/appbackend/"
echo "  2. Run: docker compose up --build"
