#!/bin/bash
# rebuild_container.sh — build, ship, and restart SensorIoT Docker containers
#
# Usage:
#   ./rebuild_container.sh              # rebuild + deploy both containers
#   ./rebuild_container.sh -t broker    # rebuild + deploy broker only
#   ./rebuild_container.sh -t rest      # rebuild + deploy rest_server only
#   ./rebuild_container.sh -d           # deploy only (skip rebuild, use existing images)
#   ./rebuild_container.sh -t broker -d # deploy broker only, skip rebuild

set -e

# cd to repo root (where docker-compose.yml lives)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

REMOTE_USER="azamike"
REMOTE_HOST="brintontech.com"
REMOTE_DIR="~"

# SSH multiplexing — one password prompt for all scp/ssh calls
SSH_SOCKET="/tmp/sensoriot_ssh_ctl_$$"
SSH_OPTS="-o ControlMaster=auto -o ControlPath=$SSH_SOCKET -o ControlPersist=60"

# Open the master connection (prompts for password once)
ssh $SSH_OPTS -N -f "$REMOTE_USER@$REMOTE_HOST"
trap "ssh -O exit -o ControlPath=$SSH_SOCKET $REMOTE_USER@$REMOTE_HOST 2>/dev/null; rm -f $SSH_SOCKET" EXIT

TARGET="all"     # all | broker | rest
REBUILD=true

while getopts "t:d" opt; do
  case $opt in
    t) TARGET="$OPTARG" ;;
    d) REBUILD=false ;;
    *) echo "Usage: $0 [-t broker|rest|all] [-d]"; exit 1 ;;
  esac
done

# ── 1. Build ─────────────────────────────────────────────────────────────────
if [ "$REBUILD" = true ]; then
  echo "==> Building images (platform: linux/amd64, target: $TARGET)..."
  if [ "$TARGET" = "all" ]; then
    DOCKER_DEFAULT_PLATFORM=linux/amd64 docker compose build
  elif [ "$TARGET" = "broker" ]; then
    DOCKER_DEFAULT_PLATFORM=linux/amd64 docker compose build broker
  elif [ "$TARGET" = "rest" ]; then
    DOCKER_DEFAULT_PLATFORM=linux/amd64 docker compose build rest_server
  fi
fi

# ── 2. Save + SCP ─────────────────────────────────────────────────────────────
FILES_TO_SCP="docker-compose.yml appbackend/.env"

if [ "$TARGET" = "all" ] || [ "$TARGET" = "rest" ]; then
  echo "==> Saving rest_server image..."
  docker save sensoriot-rest_server | gzip > rest_server.tar.gz
  FILES_TO_SCP="$FILES_TO_SCP rest_server.tar.gz"
fi

if [ "$TARGET" = "all" ] || [ "$TARGET" = "broker" ]; then
  echo "==> Saving broker image..."
  docker save sensoriot-broker | gzip > broker.tar.gz
  FILES_TO_SCP="$FILES_TO_SCP broker.tar.gz"
fi

echo "==> Copying to $REMOTE_USER@$REMOTE_HOST..."
scp $SSH_OPTS $FILES_TO_SCP "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"

# ── 3. Load + restart on server ──────────────────────────────────────────────
echo "==> Loading and restarting on remote server..."

REMOTE_CMDS=""

if [ "$TARGET" = "all" ] || [ "$TARGET" = "rest" ]; then
  REMOTE_CMDS="$REMOTE_CMDS
    echo '--- Loading rest_server ---'
    docker load < rest_server.tar.gz"
fi

if [ "$TARGET" = "all" ] || [ "$TARGET" = "broker" ]; then
  REMOTE_CMDS="$REMOTE_CMDS
    echo '--- Loading broker ---'
    docker load < broker.tar.gz"
fi

if [ "$TARGET" = "all" ]; then
  REMOTE_CMDS="$REMOTE_CMDS
    echo '--- Restarting all services ---'
    docker compose up -d --no-build"
elif [ "$TARGET" = "rest" ]; then
  REMOTE_CMDS="$REMOTE_CMDS
    echo '--- Restarting rest_server ---'
    docker compose up -d --no-build rest_server"
elif [ "$TARGET" = "broker" ]; then
  REMOTE_CMDS="$REMOTE_CMDS
    echo '--- Restarting broker ---'
    docker compose up -d --no-build broker"
fi

ssh $SSH_OPTS "$REMOTE_USER@$REMOTE_HOST" "cd $REMOTE_DIR && $REMOTE_CMDS"

echo "==> Done."
