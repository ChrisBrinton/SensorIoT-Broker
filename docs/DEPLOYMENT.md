# SensorIoT — Deployment Guide

This document covers building, deploying, and operating the SensorIoT platform.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Local Development](#2-local-development)
3. [Docker Containers](#3-docker-containers)
4. [Remote Deployment](#4-remote-deployment)
5. [Nginx & SSL](#5-nginx--ssl)
6. [Terraform Infrastructure](#6-terraform-infrastructure)
7. [Firebase Setup](#7-firebase-setup)
8. [Database Maintenance](#8-database-maintenance)
9. [Monitoring & Logs](#9-monitoring--logs)
10. [Environment Variables](#10-environment-variables)

---

## 1. Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| Docker | 20+ | Container builds and runtime |
| Docker Compose | v2 | Multi-container orchestration |
| Python | 3.10+ | REST server and broker |
| Pipenv | latest | Python dependency management |
| Flutter SDK | 3.x (Dart ^3.6.1) | Mobile app builds |
| Xcode | latest | iOS builds |
| Terraform | 1.x | Infrastructure provisioning (optional) |
| SSH | — | Remote server access |

---

## 2. Local Development

### 2.1 MongoDB Access

Start an SSH tunnel to the production database:

```bash
bash mongo_tunnel.sh
# Tunnels localhost:27017 → brintontech.com:27017
```

### 2.2 REST Server

```bash
cd sensoriot-rest
pipenv install                  # Install dependencies
cp .env.example .env            # Configure MONGO_URI and AES_SHARED_KEY
./runinteractivesvr.sh          # Run Flask dev server (interactive)
```

Production-style local run:
```bash
./runserver.sh                  # Gunicorn on port 5050, 4 workers, background
./logs.sh                       # Tail logs
./stopserver.sh                 # Stop gunicorn + rotate logs
```

### 2.3 MQTT Broker

```bash
cd sensoriot-broker
pipenv install
pipenv run python3 DataBroker.py --db TEST     # Test database
pipenv run python3 DataBroker.py --db PROD     # Production database
```

Background run:
```bash
./runbroker_prod.sh             # nohup background process (PROD)
```

### 2.4 NOAA Publisher

```bash
# Run once:
pipenv run python3 NOAAPublisher.py --db PROD

# Run in a loop (every 60 minutes):
pipenv run python3 NOAAPublisher.py --db PROD --interval 60
```

Cron alternative:
```
0 * * * *  cd /path/to/sensoriot-broker && pipenv run python3 NOAAPublisher.py --db PROD >> noaa.log 2>&1
```

### 2.5 Alert Publisher

```bash
pipenv run python3 AlertPublisher.py --db PROD --interval 5    # Every 5 minutes
pipenv run python3 AlertPublisher.py --db PROD                 # Run once
```

### 2.6 Flutter App

```bash
cd sensoriot-app
flutter pub get
cd ios && pod install && cd ..   # iOS only
flutter run                      # Run on connected device
flutter analyze                  # Lint
```

### 2.7 Full Stack (Docker Compose)

```bash
docker-compose up --build        # MongoDB + REST + Broker
```

---

## 3. Docker Containers

### 3.1 REST Server Container

**Dockerfile:** `sensoriot-rest/Dockerfile`
**Base image:** `nginx` (latest stable)

Layers:
1. Install Python 3, pip, create venv at `/app/venv`
2. Copy `requirements.txt` → pip install
3. Copy application code (`*.py`, `anomalydetection/`)
4. Copy `nginx.conf` → `/etc/nginx/`
5. Copy SSL config files → `/etc/letsencrypt/`
6. Pre-create `/models` and `/public` directories
7. Entry point: `startup.sh`

**Startup sequence** (`startup.sh`):
1. Start nginx in background
2. Start gunicorn (`-w 4 -b 0.0.0.0:5050 --timeout 120 server:app`)
3. Wait for any process to exit

**Exposed ports:** 80 (HTTP), 443 (HTTPS)

**Volumes:**
- `anomaly_models:/models` — persistent ML model storage
- `/etc/letsencrypt:/etc/letsencrypt:ro` — SSL certificates (read-only mount)

### 3.2 Broker Container

**Dockerfile:** `sensoriot-broker/Dockerfile`
**Base image:** `ubuntu:22.04`

Layers:
1. Install Python 3, pip, mosquitto
2. pip install: `paho-mqtt`, `pymongo`, `requests`, `firebase-admin`, etc.
3. Copy application code (`DataBroker.py`, `NOAAPublisher.py`, `AlertPublisher.py`, `Database.py`, etc.)
4. Copy `mosquitto.conf`
5. Copy Firebase service account JSON → `/firebase_service_account.json`
6. Entry point: `startup.sh`

**Startup sequence** (`startup.sh`):
1. Start Mosquitto daemon (`-v -c /mosquitto.conf -d`)
2. Start NOAAPublisher in background (`--interval 60`, hourly)
3. Start AlertPublisher in background (`--interval 1`, every minute)
4. Start DataBroker in foreground (main process)

**Exposed ports:** 1883, 1884 (MQTT)

**Environment variable:** `MONGODB_HOST` (default: `127.0.0.1`) — used by all three Python processes

### 3.3 Docker Compose

**File:** `docker-compose.yml`

```yaml
services:
  rest_server:
    build: ./sensoriot-rest
    restart: unless-stopped
    ports: [80, 443]
    env_file: ./sensoriot-rest/.env
    extra_hosts: ["host.docker.internal:host-gateway"]
    volumes:
      - anomaly_models:/models
      - /etc/letsencrypt:/etc/letsencrypt:ro
    networks: [backend]

  broker:
    build: ./sensoriot-broker
    restart: unless-stopped
    ports: [1883]
    extra_hosts: ["host.docker.internal:host-gateway"]
    networks: [backend]

volumes:
  anomaly_models:

networks:
  backend:
    driver: bridge
```

MongoDB runs on the host (not in Docker). Containers access it via `host.docker.internal`.

---

## 4. Remote Deployment

### 4.1 Deploy Script (`rebuild_container.sh`)

Builds Docker images locally, compresses them, transfers to the remote server via SCP, loads them, and restarts services.

**Remote server:** `azamike@brintontech.com`
**Build platform:** `linux/amd64`
**SSH:** Uses ControlMaster multiplexing (single password prompt)

Usage:

```bash
# Rebuild and deploy both containers
./rebuild_container.sh

# Rebuild and deploy broker only
./rebuild_container.sh -t broker

# Rebuild and deploy REST server only
./rebuild_container.sh -t rest

# Deploy only (skip rebuild, use existing images)
./rebuild_container.sh -d

# Deploy broker only, skip rebuild
./rebuild_container.sh -t broker -d
```

**Process:**
1. Build Docker images locally (unless `-d`)
2. Save images to `.tar.gz` files
3. SCP compressed images + `docker-compose.yml` to remote
4. SSH: `docker load` images on remote
5. SSH: `docker compose up -d --no-build` to restart services

### 4.2 Systemd Service (on remote server)

Terraform cloud-init installs a systemd unit:

```ini
[Unit]
Description=SensorIoT Docker Compose
After=docker.service

[Service]
Type=simple
WorkingDirectory=/opt/sensoriot
ExecStart=/usr/bin/docker compose up
ExecStop=/usr/bin/docker compose down
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

This ensures containers auto-restart on server reboot.

---

## 5. Nginx & SSL

### 5.1 Configuration (`sensoriot-rest/nginx.conf`)

```
Internet → Nginx (port 443, SSL) → Gunicorn (localhost:5050) → Flask app
```

Key settings:
- **Worker processes:** auto (matches CPU cores)
- **HTTP → HTTPS redirect:** port 80 → 443 for `brintontech.com`
- **Default server:** returns 444 (close) for unmatched hostnames
- **SSL certificates:** `/etc/letsencrypt/live/brintontech.com/`
- **Static files:** served from `/public` directory
- **Proxy:** falls back to `http://localhost:5050` for dynamic requests
- **Headers:** `X-Forwarded-For`, `X-Forwarded-Proto`, `Host`
- **Max body size:** 4G
- **Gzip:** enabled
- **Keepalive timeout:** 5s

### 5.2 SSL Certificate Renewal

Certificates are issued by Let's Encrypt. They are mounted into the container as a read-only volume from the host.

```bash
./upgrade_certbot.sh            # Renew certificates on host
```

After renewal, restart the REST server container to pick up new certs.

---

## 6. Terraform Infrastructure

**Directory:** `terraform/`
**Provider:** IONOS Cloud (`ionoscloud ~> 6.4`)

### 6.1 Resources Provisioned

| Resource | Specification |
|---|---|
| Datacenter | `sensoriot-datacenter` in `us/ewr` (Newark) |
| Server | 2 vCPUs, 4 GB RAM, 50 GB SSD, Ubuntu 22.04 |
| Public LAN | VLAN with dedicated IP block |
| IP Block | 1 static public IP |
| NIC | DHCP enabled, attached to public LAN |

### 6.2 Firewall Rules (Ingress)

| Port | Protocol | Purpose |
|---|---|---|
| 22 | TCP | SSH |
| 80 | TCP | HTTP (redirects to HTTPS) |
| 443 | TCP | HTTPS |
| 1883 | TCP | MQTT |

### 6.3 Cloud-Init

On first boot, the server:
1. Installs Docker CE
2. Clones the repository to `/opt/sensoriot`
3. Writes `.env` file with Google OAuth credentials
4. Runs `docker compose up -d --build`
5. Enables the systemd unit for auto-start on reboot

### 6.4 Usage

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars    # Fill in real values
terraform init
terraform plan
terraform apply
```

### 6.5 Variables

| Variable | Sensitive | Purpose |
|---|---|---|
| `ionos_token` | Yes | IONOS Cloud API token |
| `ssh_public_key` | No | SSH key for root access |
| `repo_url` | No | Git repository URL |
| `google_web_client_id` | Yes | Google OAuth web client ID |
| `google_home_client_id` | Yes | Google Home OAuth client ID |
| `google_home_client_secret` | Yes | Google Home OAuth client secret |

### 6.6 Outputs

| Output | Value |
|---|---|
| `server_public_ip` | Static public IP of the server |
| `ssh_connection` | SSH command (`ssh root@<ip>`) |
| `datacenter_id` | IONOS datacenter reference ID |

---

## 7. Firebase Setup

Firebase is required for push notifications (FCM). Setup is manual.

### 7.1 Mobile App

| Platform | File | Location |
|---|---|---|
| Android | `google-services.json` | `sensoriot-app/android/app/` |
| iOS | `GoogleService-Info.plist` | `sensoriot-app/ios/Runner/` |

After adding these files:
```bash
cd sensoriot-app/ios && pod install
```

### 7.2 iOS APNs Configuration

Upload your APNs authentication key in:
**Firebase Console → Project Settings → Cloud Messaging → Apple app configuration**

This is required for iOS push notification delivery.

### 7.3 Broker Service Account

The broker needs a Firebase Admin SDK service account key for sending FCM messages.

| Environment | File path |
|---|---|
| Local development | `sensoriot-broker/firebase_service_account.json` |
| Docker container | `/firebase_service_account.json` (copied during build) |
| AlertPublisher flag | `--firebase-key <path>` (default: `../sensoriot-rest/firebase_service_account.json`) |

Both `AlertPublisher.py` and `NOAAPublisher.py` try/except import `firebase_admin` — they degrade gracefully to webhook-only mode if the SDK or key file is unavailable.

---

## 8. Database Maintenance

### 8.1 Archive Old Data

`archivedb.py` streams records older than N months to gzipped JSONL files, then optionally deletes them. Writes a `.meta.json` sidecar per archive file.

```bash
# Dry run (count only):
cd sensoriot-rest
pipenv run python3 archivedb.py -d PROD -m 6

# Archive to gzipped JSONL then delete:
pipenv run python3 archivedb.py -d PROD -m 6 --output-dir ./archives --remove
```

### 8.2 Trim Without Archive

```bash
pipenv run python3 trimdb.py --db=PROD --months=6 --remove
```

### 8.3 Monthly Cron Job

Install an automated monthly archive (runs 2 AM on the 1st):

```bash
./install_archive_cron.sh
```

This creates:
```
0 2 1 * * cd <dir> && pipenv run python3 archivedb.py -d PROD -m 6 --remove >> archivedb.log 2>&1
```

The script is idempotent — it checks if the cron job already exists before installing.

---

## 9. Monitoring & Logs

### 9.1 REST Server Logs

```bash
# Docker container logs:
./logs.sh

# Gunicorn log file (inside container or bare-metal):
tail -f gunicorn.log

# Nginx logs (inside container):
tail -f /var/log/nginx/access.log
tail -f /var/log/nginx/error.log
```

### 9.2 Broker Logs

Inside the broker container, each process logs to its own file:
- `broker.log` — DataBroker
- `noaa.log` — NOAAPublisher
- `alert.log` — AlertPublisher

Mosquitto logs to stdout (symlinked via `/var/log/test.log → /proc/1/fd/1`).

### 9.3 Health Checks

| Endpoint | Method | Expected |
|---|---|---|
| `GET /` | HTTP | `"hello "` |
| `GET /stats` | HTTP | `"total rows:{n}"` |
| `GET /fulfillment/test` | HTTP | `"OK"` |

---

## 10. Environment Variables

### REST Server (`sensoriot-rest/.env`)

| Variable | Required | Purpose |
|---|---|---|
| `MONGO_URI` | Yes | MongoDB connection string |
| `AES_SHARED_KEY` | Yes | Base64-encoded 256-bit key for AES-256-CBC credential decryption |

### Broker Container

| Variable | Default | Purpose |
|---|---|---|
| `MONGODB_HOST` | `127.0.0.1` | MongoDB host used by DataBroker, NOAAPublisher, AlertPublisher |

### Terraform / Cloud-Init

| Variable | Purpose |
|---|---|
| `GOOGLE_WEB_CLIENT_ID` | Google OAuth web client ID |
| `GOOGLE_HOME_CLIENT_ID` | Google Home OAuth client ID |
| `GOOGLE_HOME_CLIENT_SECRET` | Google Home OAuth client secret |

### CLI Flags (Common)

| Flag | Used By | Default | Purpose |
|---|---|---|---|
| `--db` | All broker scripts | `TEST` | `PROD` or `TEST` |
| `--dbconn` | All broker scripts | `host.docker.internal` | MongoDB host override |
| `--interval` | NOAA/Alert publishers | — | Loop interval in minutes |
| `--host` | DataBroker | `0.0.0.0` | MQTT listener address |
| `--port` | DataBroker | `1883` | MQTT listener port |
| `--log` | DataBroker | off | Verbose message logging |
| `--firebase-key` | AlertPublisher | `../sensoriot-rest/firebase_service_account.json` | Firebase service account path |

---

## Quick Reference

```bash
# === Local Development ===
bash mongo_tunnel.sh                                    # SSH tunnel to prod MongoDB
cd sensoriot-rest && ./runinteractivesvr.sh              # Dev REST server
cd sensoriot-broker && pipenv run python3 DataBroker.py --db TEST   # Dev broker

# === Docker (local) ===
docker-compose up --build                                # Full stack

# === Deploy to production ===
./rebuild_container.sh                                   # Build + deploy all
./rebuild_container.sh -t rest                           # REST server only
./rebuild_container.sh -t broker                         # Broker only
./rebuild_container.sh -d                                # Deploy only (no rebuild)

# === Database maintenance ===
cd sensoriot-rest
pipenv run python3 archivedb.py -d PROD -m 6 --output-dir ./archives --remove
./install_archive_cron.sh                                # Monthly cron

# === Tests ===
cd sensoriot-rest && pipenv run pytest -v
cd sensoriot-broker && pipenv run pytest test_databroker.py -v
cd sensoriot-app && flutter test --reporter=expanded
```
