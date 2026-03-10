#!/usr/bin/env python3
"""
NOAAPublisher.py - Fetches NOAA 7-day weather forecasts and publishes them
as virtual sensor readings into the SensorIoT MongoDB database.

For each opted-in user (gateway) in the NOAASettings collection, this script:
  1. Fetches the 7-day forecast from api.weather.gov
  2. Deletes any existing future NOAA records for that gateway
  3. Inserts ~14 fresh forecast periods (node_id='noaa_forecast', type='F')
  4. Optionally sends FCM push notifications for predictive weather alerts
     (frost, heat, cold-front) when predictive_alerts_enabled is True.

Run continuously (reads location config from MongoDB):
  pipenv run python3 NOAAPublisher.py --db PROD --interval 60

Run once (e.g. via cron):
  pipenv run python3 NOAAPublisher.py --db PROD
"""

import argparse
import datetime
import time
import requests
import pymongo as mongodb

USER_AGENT = '(SensorIoT, keyvanazami@gmail.com)'

# Cooldown for predictive alerts: skip if last alert sent < 6 hours ago
_ALERT_COOLDOWN_SECONDS = 6 * 3600

# Look-ahead window for frost/heat checks (hours)
_LOOKAHEAD_HOURS = 24

# Temperature drop threshold for cold-front detection (°F within 12 hours)
_COLD_FRONT_DROP = 20.0
_COLD_FRONT_WINDOW = 12


# ---------------------------------------------------------------------------
# Firebase (optional dependency — script runs without it for webhook-only use)
# ---------------------------------------------------------------------------

try:
    import firebase_admin
    from firebase_admin import credentials as fb_credentials, messaging as fb_messaging
    _FIREBASE_AVAILABLE = True
except ImportError:
    _FIREBASE_AVAILABLE = False


def _init_firebase(key_path: str) -> bool:
    """Initialise the Firebase Admin SDK once. Returns True if successful."""
    if not _FIREBASE_AVAILABLE:
        print('[NOAA] firebase_admin not installed — push notifications disabled.')
        return False
    if firebase_admin._apps:
        return True
    try:
        cred = fb_credentials.Certificate(key_path)
        firebase_admin.initialize_app(cred)
        return True
    except Exception as e:
        print(f'[NOAA] Firebase init failed: {e}')
        return False


def _send_fcm_push(tokens: list, title: str, body: str) -> None:
    """Send an FCM push notification to each token in `tokens`."""
    if not _FIREBASE_AVAILABLE or not tokens:
        return
    for token in tokens:
        try:
            msg = fb_messaging.Message(
                notification=fb_messaging.Notification(title=title, body=body),
                token=token,
            )
            fb_messaging.send(msg)
            print(f'[NOAA] FCM sent to ...{token[-8:]}: {title}')
        except Exception as e:
            print(f'[NOAA] FCM send failed for token ...{token[-8:]}: {e}')


# ---------------------------------------------------------------------------
# NOAA API helpers
# ---------------------------------------------------------------------------

def get_forecast_url(lat: float, lon: float) -> str | None:
    """Retrieve the hourly forecast URL for a lat/lon from the NOAA Points API."""
    url = f'https://api.weather.gov/points/{lat},{lon}'
    try:
        resp = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=15)
        resp.raise_for_status()
        # Use hourlyForecast for finer time resolution matching sensor readings
        return resp.json()['properties']['forecastHourly']
    except Exception as e:
        print(f'[NOAA] Failed to get points for ({lat}, {lon}): {e}')
        return None


def fetch_forecast_periods(forecast_url: str) -> list:
    """Fetch forecast periods from the NOAA forecast URL."""
    try:
        resp = requests.get(forecast_url, headers={'User-Agent': USER_AGENT}, timeout=15)
        resp.raise_for_status()
        return resp.json()['properties']['periods']
    except Exception as e:
        print(f'[NOAA] Failed to fetch forecast from {forecast_url}: {e}')
        return []


def period_start_unix(period: dict) -> float:
    """Convert a forecast period's startTime ISO 8601 string to a Unix timestamp."""
    start_str = period['startTime']  # e.g. "2026-03-01T18:00:00-05:00"
    dt = datetime.datetime.fromisoformat(start_str)
    return dt.timestamp()


def period_temp_f(period: dict) -> float | None:
    """Return the temperature in °F for a forecast period, or None if missing."""
    temp = period.get('temperature')
    if temp is None:
        return None
    if period.get('temperatureUnit', 'F') == 'C':
        temp = round(temp * 9 / 5 + 32, 1)
    return float(temp)


# ---------------------------------------------------------------------------
# Database writes
# ---------------------------------------------------------------------------

def publish_forecast(db, gateway_id: str, periods: list) -> int:
    """
    Delete stale future NOAA records for this gateway, then insert fresh
    forecast periods into the Sensors collection.

    Returns the number of records inserted.
    """
    now_ts = time.time()

    # Remove stale future forecast records so old predictions don't linger
    db.Sensors.delete_many({
        'gateway_id': gateway_id,
        'node_id': 'noaa_forecast',
        'time': {'$gt': now_ts},
    })

    docs = []
    for period in periods:
        temp = period_temp_f(period)
        if temp is None:
            continue

        start_ts = period_start_unix(period)
        # Only store future periods
        if start_ts <= now_ts:
            continue

        docs.append({
            'model': 'NOAA',
            'gateway_id': gateway_id,
            'node_id': 'noaa_forecast',
            'type': 'F',
            'value': str(temp),
            'time': start_ts,
        })

    if docs:
        db.Sensors.insert_many(docs)

    return len(docs)


# ---------------------------------------------------------------------------
# Predictive alert logic
# ---------------------------------------------------------------------------

def _check_predictive_alerts(
    db,
    user: dict,
    periods: list,
    firebase_available: bool,
) -> None:
    """
    Evaluate predictive weather alerts for a user based on the next
    _LOOKAHEAD_HOURS hours of forecast periods.

    Checks performed:
      - Frost: any period temp ≤ frost_threshold (default 35 °F)
      - Heat:  any period temp ≥ heat_threshold  (default 95 °F)
      - Cold front: temp drop > _COLD_FRONT_DROP within _COLD_FRONT_WINDOW hours
    """
    email = user.get('email', '')
    frost_threshold = float(user.get('frost_threshold', 35.0))
    heat_threshold  = float(user.get('heat_threshold', 95.0))
    last_sent       = user.get('last_noaa_alert_sent', 0) or 0
    now_ts          = time.time()

    # Respect cooldown
    if now_ts - last_sent < _ALERT_COOLDOWN_SECONDS:
        print(f'[NOAA] Predictive alert cooldown active for {email}, skipping.')
        return

    # Build a list of (unix_ts, temp_f) for the next _LOOKAHEAD_HOURS hours
    lookahead_end = now_ts + _LOOKAHEAD_HOURS * 3600
    upcoming: list[tuple[float, float]] = []
    for period in periods:
        ts   = period_start_unix(period)
        temp = period_temp_f(period)
        if temp is None or ts <= now_ts or ts > lookahead_end:
            continue
        upcoming.append((ts, temp))

    upcoming.sort(key=lambda x: x[0])

    if not upcoming:
        return

    temps = [t for _, t in upcoming]

    # --- Frost check ---
    frost_hit = next((t for t in temps if t <= frost_threshold), None)
    if frost_hit is not None:
        _fire_alert(db, email, firebase_available,
                    title='Frost Warning',
                    body=f'Temperatures forecast to drop to {frost_hit:.0f}°F in the next {_LOOKAHEAD_HOURS}h.')
        return

    # --- Heat check ---
    heat_hit = next((t for t in temps if t >= heat_threshold), None)
    if heat_hit is not None:
        _fire_alert(db, email, firebase_available,
                    title='Heat Alert',
                    body=f'Temperatures forecast to reach {heat_hit:.0f}°F in the next {_LOOKAHEAD_HOURS}h.')
        return

    # --- Cold-front check: drop > _COLD_FRONT_DROP within _COLD_FRONT_WINDOW hours ---
    window_end = now_ts + _COLD_FRONT_WINDOW * 3600
    window_temps = [t for ts, t in upcoming if ts <= window_end]
    if len(window_temps) >= 2:
        drop = window_temps[0] - min(window_temps)
        if drop >= _COLD_FRONT_DROP:
            _fire_alert(db, email, firebase_available,
                        title='Cold Front Alert',
                        body=f'Temperature expected to drop {drop:.0f}°F in the next {_COLD_FRONT_WINDOW}h.')


def _fire_alert(db, email: str, firebase_available: bool, title: str, body: str) -> None:
    """Send FCM push to all device tokens for the user and update last_noaa_alert_sent."""
    print(f'[NOAA] Firing predictive alert for {email}: {title}')

    if firebase_available:
        token_docs = list(db.DeviceTokens.find({'email': email}, {'token': 1}))
        tokens = [d['token'] for d in token_docs if d.get('token')]
        _send_fcm_push(tokens, title, body)

    db.NOAASettings.update_one(
        {'email': email},
        {'$set': {'last_noaa_alert_sent': time.time()}},
    )


def _check_baseline_forecast_alert(
    db,
    user: dict,
    periods: list,
    firebase_available: bool,
) -> None:
    """
    Compare the next _LOOKAHEAD_HOURS hours of NOAA forecast against the
    outside sensor's baseline ±2σ band. Fires a push notification if any
    forecast period falls outside the expected range.

    Uses a separate 6-hour cooldown key (last_baseline_forecast_alert_sent)
    so it doesn't interfere with frost/heat/cold-front alerts.
    """
    if not user.get('baseline_forecast_alert_enabled'):
        return

    email             = user.get('email', '')
    gateway_id        = user.get('gateway_id')
    outside_sensor_id = user.get('outside_sensor_id')
    if not gateway_id or not outside_sensor_id:
        return

    last_sent = user.get('last_baseline_forecast_alert_sent', 0) or 0
    if time.time() - last_sent < _ALERT_COOLDOWN_SECONDS:
        print(f'[NOAA] Baseline forecast alert cooldown active for {email}, skipping.')
        return

    # Fetch baseline buckets for the outside sensor (temperature only)
    buckets = list(db.Baselines.find({
        'gateway_id': gateway_id,
        'node_id': outside_sensor_id,
        'type': 'F',
    }))
    if not buckets:
        print(f'[NOAA] No baseline for {gateway_id}/{outside_sensor_id}/F — skipping.')
        return

    # Build lookup: (hour_utc, day_of_week) → bucket
    # Baselines are keyed by MongoDB $dayOfWeek (1=Sun … 7=Sat, UTC-based).
    bucket_map = {(b['hour'], b['day_of_week']): b for b in buckets}

    now_ts        = time.time()
    lookahead_end = now_ts + _LOOKAHEAD_HOURS * 3600

    for period in periods:
        ts   = period_start_unix(period)
        temp = period_temp_f(period)
        if temp is None or ts <= now_ts or ts > lookahead_end:
            continue

        # Convert to UTC datetime for hour-of-week lookup (same convention as DB)
        dt_utc = datetime.datetime.utcfromtimestamp(ts)
        # Python weekday(): 0=Mon … 6=Sun  →  MongoDB $dayOfWeek: 1=Sun … 7=Sat
        dow  = (dt_utc.weekday() + 1) % 7 + 1
        hour = dt_utc.hour

        bucket = bucket_map.get((hour, dow))
        if bucket is None:
            continue
        mean = float(bucket.get('mean', 0))
        std  = float(bucket.get('std',  0))
        if std < 0.1:
            continue  # near-flat signal, skip

        if temp < mean - 2 * std or temp > mean + 2 * std:
            lo  = mean - 2 * std
            hi  = mean + 2 * std
            dir_word = 'above' if temp > hi else 'below'
            time_str = dt_utc.strftime('%a %-I%p').lower()  # e.g. "mon 3pm"
            body = (
                f'Forecast of {temp:.0f}°F at {time_str} (UTC) is {dir_word} the '
                f'expected range of {lo:.0f}–{hi:.0f}°F for that hour.'
            )
            print(f'[NOAA] Baseline forecast alert for {email}: {temp}°F outside [{lo:.1f}, {hi:.1f}]')

            if firebase_available:
                token_docs = list(db.DeviceTokens.find({'email': email}, {'token': 1}))
                tokens = [d['token'] for d in token_docs if d.get('token')]
                _send_fcm_push(tokens, 'Unusual Weather Forecast', body)

            db.NOAASettings.update_one(
                {'email': email},
                {'$set': {'last_baseline_forecast_alert_sent': time.time()}},
            )
            return  # one notification per run is enough


# ---------------------------------------------------------------------------
# Main run loop
# ---------------------------------------------------------------------------

def run_once(db, firebase_available: bool = False) -> None:
    """Fetch forecasts for all opted-in users and publish to MongoDB."""
    settings_list = list(db.NOAASettings.find({'enabled': True}))

    if not settings_list:
        print('[NOAA] No opted-in users found in NOAASettings collection.')
        return

    for user in settings_list:
        email      = user.get('email', 'unknown')
        gateway_id = user.get('gateway_id')
        lat        = user.get('lat')
        lon        = user.get('lon')

        if not gateway_id or lat is None or lon is None:
            print(f'[NOAA] Skipping {email}: missing gateway_id, lat, or lon.')
            continue

        print(f'[NOAA] Processing {email} gateway={gateway_id} lat={lat} lon={lon}')

        forecast_url = get_forecast_url(lat, lon)
        if not forecast_url:
            continue

        periods = fetch_forecast_periods(forecast_url)
        if not periods:
            continue

        count = publish_forecast(db, gateway_id, periods)
        print(f'[NOAA] Inserted {count} forecast record(s) for gateway={gateway_id}')

        # Predictive alerts (opt-in per user)
        if user.get('predictive_alerts_enabled'):
            _check_predictive_alerts(db, user, periods, firebase_available)

        # Baseline forecast alert (opt-in per user)
        if user.get('baseline_forecast_alert_enabled'):
            _check_baseline_forecast_alert(db, user, periods, firebase_available)


def main():
    parser = argparse.ArgumentParser(
        description='NOAAPublisher - Publish NOAA forecast data as virtual sensor readings'
    )
    parser.add_argument(
        '--db', choices=['PROD', 'TEST'], default='TEST',
        help='Database to connect to (default: TEST)',
    )
    parser.add_argument(
        '--dbconn', default='',
        help='MongoDB host:port (overrides --db; connects to gdtechdb_prod)',
    )
    parser.add_argument(
        '--interval', type=int, default=0,
        help='Run repeatedly every N minutes. 0 (default) = run once and exit.',
    )
    parser.add_argument(
        '--firebase-key', default='./firebase_service_account.json',
        help='Path to Firebase service account JSON key (default: ./firebase_service_account.json)',
    )
    args = parser.parse_args()

    # Connect to MongoDB (mirrors DataBroker.py connection logic)
    if args.dbconn:
        mongo_client = mongodb.MongoClient(f'mongodb://{args.dbconn}/')
        db = mongo_client.gdtechdb_prod
        print(f'[NOAA] Connected to {args.dbconn} (gdtechdb_prod)')
    elif args.db == 'PROD':
        mongo_client = mongodb.MongoClient('mongodb://localhost:27017/')
        db = mongo_client.gdtechdb_prod
        print('[NOAA] Connected to localhost (gdtechdb_prod)')
    else:
        mongo_client = mongodb.MongoClient('mongodb://localhost:27017/')
        db = mongo_client.gdtechdb_test
        print('[NOAA] Connected to localhost (gdtechdb_test)')

    firebase_available = _init_firebase(args.firebase_key)

    if args.interval > 0:
        print(f'[NOAA] Running every {args.interval} minute(s). Press Ctrl+C to stop.')
        while True:
            try:
                run_once(db, firebase_available)
            except Exception as e:
                print(f'[NOAA] Unexpected error during run: {e}')
            print(f'[NOAA] Sleeping for {args.interval} minute(s)...')
            time.sleep(args.interval * 60)
    else:
        run_once(db, firebase_available)


if __name__ == '__main__':
    main()
