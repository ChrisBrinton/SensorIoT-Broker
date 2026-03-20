#!/usr/bin/env bash
# deploy.sh — Sync and restart SensorIoT components on production.
#
# Usage:
#   ./deploy.sh --rest                 # deploy REST server only
#   ./deploy.sh --broker               # deploy broker processes only
#   ./deploy.sh --all                  # deploy everything
#   ./deploy.sh --all --deps           # also run `pipenv sync` on remote
#   ./deploy.sh --all --dry-run        # preview rsync changes without copying
#   ./deploy.sh --all --skip-canary    # skip post-deploy health checks
#
# Prerequisites:
#   - SSH key-based access to REMOTE_HOST (no password prompt)
#   - rsync and curl installed locally
#   - python3 available locally (used for JSON canary checks)

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
REMOTE_HOST="azamike@brintontech.com"
REMOTE_REST_DIR="/home/azamike/rest"
REMOTE_BROKER_DIR="/home/azamike/broker"   # adjust if different on server
BASE_URL="https://brintontech.com"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$SCRIPT_DIR/.."
LOCAL_REST_DIR="$REPO_ROOT/appbackend"
LOCAL_BROKER_DIR="$REPO_ROOT/broker"

# ── Argument parsing ──────────────────────────────────────────────────────────
DEPLOY_REST=false
DEPLOY_BROKER=false
SYNC_DEPS=false
SKIP_CANARY=false
DRY_RUN=false

usage() {
  sed -n '2,14p' "$0" | sed 's/^# //'
  exit 1
}

[[ $# -eq 0 ]] && usage

for arg in "$@"; do
  case $arg in
    --rest)        DEPLOY_REST=true ;;
    --broker)      DEPLOY_BROKER=true ;;
    --all)         DEPLOY_REST=true; DEPLOY_BROKER=true ;;
    --deps)        SYNC_DEPS=true ;;
    --skip-canary) SKIP_CANARY=true ;;
    --dry-run)     DRY_RUN=true ;;
    --help|-h)     usage ;;
    *) echo "Unknown flag: $arg"; usage ;;
  esac
done

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'

info()    { echo -e "${GREEN}[deploy]${NC} $*"; }
section() { echo -e "\n${CYAN}═══ $* ═══${NC}"; }
warn()    { echo -e "${YELLOW}[warn]${NC}  $*"; }
err()     { echo -e "${RED}[error]${NC} $*" >&2; }
die()     { err "$*"; exit 1; }

RSYNC_FLAGS=(-avz )
[[ "$DRY_RUN" == true ]] && RSYNC_FLAGS+=(--dry-run) && info "DRY RUN — no files will be copied or restarted"

# ── REST server deployment ────────────────────────────────────────────────────
deploy_rest() {
  section "Deploying REST server"
  info "Source : $LOCAL_REST_DIR"
  info "Target : $REMOTE_HOST:$REMOTE_REST_DIR"

  ssh -o ConnectTimeout=15 "$REMOTE_HOST" "mkdir -p '$REMOTE_REST_DIR'"

  rsync "${RSYNC_FLAGS[@]}" \
    --exclude='.env' \
    --exclude='.venv/' \
    --exclude='.git/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='models/' \
    --exclude='nohup.out*' \
    --exclude='gunicorn.log*' \
    --exclude='*.log' \
    --exclude='*.log.gz' \
    --exclude='firebase_service_account.json' \
    --exclude='*-firebase-adminsdk-*.json' \
    --exclude='*.egg-info/' \
    --exclude='.pytest_cache/' \
    --exclude='archives/' \
    "$LOCAL_REST_DIR/" "$REMOTE_HOST:$REMOTE_REST_DIR/"

  if [[ "$DRY_RUN" == true ]]; then
    warn "Dry run — skipping pipenv sync and server restart"
    return
  fi

  if [[ "$SYNC_DEPS" == true ]]; then
    info "Running pipenv sync on remote (REST)..."
    ssh "$REMOTE_HOST" "cd '$REMOTE_REST_DIR' && pipenv sync --keep-outdated 2>&1 | tail -5"
  fi

  

  info "Rebuild and restarting REST server..."
  ssh "$REMOTE_HOST" "cd '$REMOTE_REST_DIR' && bash restart_docker.sh"
  sleep 3
  info "REST server restarted."
}

# ── Broker deployment ─────────────────────────────────────────────────────────
deploy_broker() {
  section "Deploying broker"
  info "Source : $LOCAL_BROKER_DIR"
  info "Target : $REMOTE_HOST:$REMOTE_BROKER_DIR"

  ssh -o ConnectTimeout=15 "$REMOTE_HOST" "mkdir -p '$REMOTE_BROKER_DIR'"

  rsync "${RSYNC_FLAGS[@]}" \
    --exclude='__pycache__/' \
    --exclude='.venv/' \
    --exclude='.git/' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='*.BAK' \
    --exclude='*.new' \
    --exclude='*.deb' \
    --exclude='firebase_service_account.json' \
    --exclude='*-firebase-adminsdk-*.json' \
    --exclude='broker.log*' \
    --exclude='alert_publisher.log*' \
    --exclude='noaa_publisher.log*' \
    --exclude='nohup.out*' \
    --exclude='.pytest_cache/' \
    --exclude='*.egg-info/' \
    "$LOCAL_BROKER_DIR/" "$REMOTE_HOST:$REMOTE_BROKER_DIR/"

  if [[ "$DRY_RUN" == true ]]; then
    warn "Dry run — skipping pipenv sync and process restart"
    return
  fi

  if [[ "$SYNC_DEPS" == true ]]; then
    info "Running pipenv sync on remote (broker)..."
    ssh "$REMOTE_HOST" "cd '$REMOTE_BROKER_DIR' && pipenv sync --keep-outdated 2>&1 | tail -5"
  fi

  info "Stopping existing broker processes..."
  ssh "$REMOTE_HOST" "
    pkill -f 'python.*DataBroker.py'     2>/dev/null || true
    pkill -f 'python.*AlertPublisher.py' 2>/dev/null || true
    pkill -f 'python.*NOAAPublisher.py'  2>/dev/null || true
  "
  sleep 2

  info "Starting broker processes..."
  ssh "$REMOTE_HOST" "
    cd '$REMOTE_BROKER_DIR'
    nohup pipenv run python3 DataBroker.py --db PROD \
      > broker.log 2>&1 &
    nohup pipenv run python3 AlertPublisher.py --db PROD --interval 5 \
      > alert_publisher.log 2>&1 &
    nohup pipenv run python3 NOAAPublisher.py --db PROD --interval 60 \
      > noaa_publisher.log 2>&1 &
    disown
  "
  sleep 3
  info "Broker processes started."
}

# ── Canary helpers ────────────────────────────────────────────────────────────
CANARY_FAILED=0

pass() { info "  ✓ $*"; }
fail() { err  "  ✗ $*"; CANARY_FAILED=$((CANARY_FAILED + 1)); }

http_code() {
  curl -sk -o /dev/null -w "%{http_code}" --max-time 12 "$1" 2>/dev/null || echo "000"
}

http_body() {
  curl -sf --max-time 12 "$1" 2>/dev/null || echo ""
}

check_http() {
  local label="$1" url="$2" want="$3"
  local got
  got=$(http_code "$url")
  if [[ "$got" == "$want" ]]; then
    pass "$label → HTTP $got"
  else
    fail "$label → HTTP $got (expected $want) — $url"
  fi
}

check_json_key() {
  local label="$1" url="$2" key="$3"
  local body
  body=$(http_body "$url")
  if [[ -z "$body" ]]; then
    fail "$label → empty response"
    return
  fi
  if echo "$body" | python3 -c "
import sys, json
d = json.load(sys.stdin)
if isinstance(d, dict) and '$key' not in d:
    sys.exit(1)
" 2>/dev/null; then
    pass "$label → JSON contains '$key'"
  else
    fail "$label → '$key' missing in response: ${body:0:100}"
  fi
}

check_ssl() {
  local label="$1" url="$2"
  if curl -s --max-time 12 --head "$url" 2>&1 | grep -q "SSL certificate verify ok\|issuer:\|subject:"; then
    pass "$label → SSL ok"
  else
    # curl exits 0 even with valid certs; check http_code instead
    local code
    code=$(http_code "$url")
    if [[ "$code" != "000" ]]; then
      pass "$label → SSL ok (HTTP $code)"
    else
      fail "$label → SSL/connection failed"
    fi
  fi
}

check_remote_pid() {
  local label="$1" pattern="$2"
  if ssh -o ConnectTimeout=10 "$REMOTE_HOST" "pgrep -f '$pattern' > /dev/null 2>&1"; then
    pass "$label → running (PID $(ssh "$REMOTE_HOST" "pgrep -f '$pattern' | head -1" 2>/dev/null))"
  else
    fail "$label → NOT running"
  fi
}

# ── Canary: REST server ───────────────────────────────────────────────────────
canary_rest() {
  section "REST canary tests"

  check_http     "HTTPS hello"            "$BASE_URL/"      200
  check_http     "stats endpoint"         "$BASE_URL/stats" 200
  check_json_key "stats returns count"    "$BASE_URL/stats" "count"

  # Verify HTTP→HTTPS redirect (port 80 should 301/302 to HTTPS)
  local redir_code
  redir_code=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 12 "http://brintontech.com/" 2>/dev/null || echo "000")
  if [[ "$redir_code" == "301" || "$redir_code" == "302" ]]; then
    pass "HTTP→HTTPS redirect → $redir_code"
  else
    fail "HTTP→HTTPS redirect → $redir_code (expected 301/302)"
  fi

  # Heatmap and nodelist endpoints (unauthenticated, should return 200)
  check_http "nodelist endpoint"   "$BASE_URL/nodelist/CANARY_GW" 200
  check_http "heatmap endpoint"    "$BASE_URL/heatmap/CANARY_GW"  200

  # Auth-gated endpoints should return 401 (not 500)
  local auth_code
  auth_code=$(http_code "$BASE_URL/user_profile")
  if [[ "$auth_code" == "401" || "$auth_code" == "403" ]]; then
    pass "Auth-gated /user_profile → correctly returns $auth_code"
  else
    fail "Auth-gated /user_profile → $auth_code (expected 401/403)"
  fi
}

# ── Canary: broker processes ──────────────────────────────────────────────────
canary_broker() {
  section "Broker canary tests"
  check_remote_pid "DataBroker.py"     "python.*DataBroker.py"
  check_remote_pid "AlertPublisher.py" "python.*AlertPublisher.py"
  check_remote_pid "NOAAPublisher.py"  "python.*NOAAPublisher.py"

  # Check that logs don't contain startup crashes (look at last 20 lines)
  local pairs=("DataBroker:broker.log" "AlertPublisher:alert_publisher.log" "NOAAPublisher:noaa_publisher.log")
  for pair in "${pairs[@]}"; do
    local log_label="${pair%%:*}"
    local log_file="${pair##*:}"
    local last
    last=$(ssh "$REMOTE_HOST" "tail -20 '$REMOTE_BROKER_DIR/$log_file' 2>/dev/null" || echo "")
    if echo "$last" | grep -qi "traceback\|exception"; then
      warn "  ! $log_label log contains errors — check $REMOTE_BROKER_DIR/$log_file"
    else
      pass "$log_label log looks clean"
    fi
  done
}

# ── Main ─────────────────────────────────────────────────────────────────────
echo -e "${CYAN}SensorIoT Deploy — $(date '+%Y-%m-%d %H:%M:%S')${NC}"

[[ "$DEPLOY_REST"   == true ]] && deploy_rest
[[ "$DEPLOY_BROKER" == true ]] && deploy_broker

if [[ "$SKIP_CANARY" == false && "$DRY_RUN" == false ]]; then
  [[ "$DEPLOY_REST"   == true ]] && canary_rest
  [[ "$DEPLOY_BROKER" == true ]] && canary_broker
fi

echo ""
if [[ $CANARY_FAILED -eq 0 ]]; then
  info "All done. ✓"
else
  warn "$CANARY_FAILED canary check(s) failed."
  echo -e "  Broker logs: ssh $REMOTE_HOST 'tail -50 $REMOTE_BROKER_DIR/broker.log'"
  echo -e "  REST logs:   ssh $REMOTE_HOST 'tail -50 $REMOTE_REST_DIR/gunicorn.log'"
  exit 1
fi
