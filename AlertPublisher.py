#!/usr/bin/env python3
"""
AlertPublisher.py — Evaluates AlertRules against the latest sensor readings
and fires push notifications (Firebase Cloud Messaging) and/or webhooks.

Run continuously (checks every N minutes):
    pipenv run python3 AlertPublisher.py --db PROD --interval 5

Run once (e.g. via cron):
    pipenv run python3 AlertPublisher.py --db PROD
"""

import argparse
import datetime
import hashlib
import hmac
import json
import time

import requests
import pymongo as mongodb

# Firebase Admin SDK is optional — import lazily so the script still runs
# (in read-only / webhook-only mode) if firebase_admin is not installed.
try:
    import firebase_admin
    from firebase_admin import credentials, messaging
    _FIREBASE_AVAILABLE = True
except ImportError:
    _FIREBASE_AVAILABLE = False


# ---------------------------------------------------------------------------
# Firebase initialisation
# ---------------------------------------------------------------------------

_firebase_initialised = False


def _init_firebase(service_account_path: str) -> bool:
    """Initialise firebase_admin once; returns True on success."""
    global _firebase_initialised
    if _firebase_initialised:
        return True
    if not _FIREBASE_AVAILABLE:
        print('[Alert] firebase_admin not installed — push notifications disabled.')
        return False
    try:
        cred = credentials.Certificate(service_account_path)
        firebase_admin.initialize_app(cred)
        _firebase_initialised = True
        print('[Alert] Firebase initialised.')
        return True
    except Exception as e:
        print(f'[Alert] Firebase init failed: {e}')
        return False


# ---------------------------------------------------------------------------
# Push notification
# ---------------------------------------------------------------------------

def _send_fcm_push(tokens: list[str], title: str, body: str, db=None) -> None:
    """Send a push notification to each device token."""
    if not _firebase_initialised or not tokens:
        return
    for token in tokens:
        try:
            messaging.send(messaging.Message(
                notification=messaging.Notification(title=title, body=body),
                token=token,
            ))
            print(f'[Alert] FCM sent OK for token {token[:12]}…')
        except messaging.UnregisteredError:
            print(f'[Alert] FCM token {token[:12]}… is unregistered/stale — removing from DB')
            if db is not None:
                db.DeviceTokens.delete_one({'token': token})
        except Exception as e:
            print(f'[Alert] FCM send failed for token {token[:12]}…: {e}')


# ---------------------------------------------------------------------------
# Webhook delivery
# ---------------------------------------------------------------------------

def _send_webhook(url: str, payload: dict, secret: str | None) -> None:
    """POST payload to webhook_url; attach HMAC-SHA256 signature if secret provided."""
    body = json.dumps(payload)
    headers = {'Content-Type': 'application/json'}
    if secret:
        sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        headers['X-SensorIoT-Signature'] = f'sha256={sig}'
    try:
        resp = requests.post(url, data=body, headers=headers, timeout=10)
        if not resp.ok:
            print(f'[Alert] Webhook {url} returned {resp.status_code}')
    except Exception as e:
        print(f'[Alert] Webhook delivery failed ({url}): {e}')


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------

_OPERATORS = {
    '>':  lambda v, t: v > t,
    '<':  lambda v, t: v < t,
    '>=': lambda v, t: v >= t,
    '<=': lambda v, t: v <= t,
}

_TYPE_LABELS = {
    'F':   ('temperature', '°F'),
    'H':   ('humidity',    '%'),
    'PWR': ('power',       'W'),
    'P':   ('pressure',    'hPa'),
    'HI':  ('heat index',  '°F'),
    'DP':  ('dew point',   '°F'),
}

_OP_PHRASES = {
    '>':  'rose above',
    '<':  'dropped below',
    '>=': 'reached',
    '<=': 'dropped to',
}


def _node_name(db, gateway_id: str, node_id: str) -> str:
    """Return a human-readable name for a node, falling back to the node_id."""
    doc = db.Nicknames.find_one(
        {'gateway_id': gateway_id, 'node_id': node_id},
        {'shortname': 1, 'longname': 1, '_id': 0},
    )
    if doc:
        # Prefer longname (e.g. "Garage") over shortname (e.g. "gar") for notification text
        for field in ('longname', 'shortname'):
            name = doc.get(field, '').strip()
            if name and name != node_id:
                return name
    return node_id


def _format_notification(db, rule: dict, current_value: float | None) -> tuple[str, str]:
    """Return (title, body) for a triggered alert rule."""
    label    = rule.get('label', 'SensorIoT Alert')
    operator = rule.get('operator', '>')
    gw       = rule.get('gateway_id', '')
    node_id  = rule.get('node_id', '')
    stype    = rule.get('type', 'F')

    name               = _node_name(db, gw, node_id)
    type_label, unit   = _TYPE_LABELS.get(stype, (stype, ''))
    val_str            = f'{current_value:.1f}{unit}' if current_value is not None else '?'

    if operator == 'offline':
        minutes = rule.get('offline_minutes', 30)
        body = f'{name} has been offline for over {minutes} minutes.'
    else:
        threshold     = rule.get('threshold')
        threshold_str = f'{threshold:.1f}{unit}' if threshold is not None else '?'
        phrase        = _OP_PHRASES.get(operator, operator)
        body = f'{name} {type_label} {phrase} {threshold_str} (currently {val_str}).'

    return label, body


def _evaluate_rule(db, rule: dict) -> tuple[bool, float | None]:
    """
    Returns (triggered, current_value).
    current_value is None when no latest reading is found.
    """
    operator   = rule.get('operator', '>')
    gateway_id = rule.get('gateway_id', '')
    node_id    = rule.get('node_id', '')
    sensor_type = rule.get('type', 'F')

    doc = db.SensorsLatest.find_one(
        {'gateway_id': gateway_id, 'node_id': node_id, 'type': sensor_type},
        {'value': 1, 'time': 1, '_id': 0},
    )
    if doc is None:
        print(f'[Alert]   → SensorsLatest lookup MISS (gateway_id={gateway_id!r}, node_id={node_id!r}, type={sensor_type!r})')
        return False, None
    raw_val = doc.get('value')
    print(f'[Alert]   → SensorsLatest HIT: raw_value={raw_val!r} (type={type(raw_val).__name__}), time={doc.get("time")}')

    try:
        raw = doc['value']
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode('utf-8')
        raw = str(raw).strip()
        # Handle legacy values stored as str(bytes): "b'49.50'"
        if raw.startswith("b'") and raw.endswith("'"):
            raw = raw[2:-1]
        current_value = float(raw)
    except (ValueError, TypeError):
        return False, None

    if operator == 'offline':
        offline_minutes = rule.get('offline_minutes', 30)
        reading_age = time.time() - float(doc.get('time', 0))
        triggered = reading_age > offline_minutes * 60
        return triggered, current_value

    threshold = rule.get('threshold')
    if threshold is None:
        return False, current_value

    op_fn = _OPERATORS.get(operator)
    if op_fn is None:
        return False, current_value

    return op_fn(current_value, float(threshold)), current_value


def run_once(db, firebase_available: bool) -> None:
    """Evaluate all enabled alert rules and fire notifications as needed."""
    now = time.time()
    rules = list(db.AlertRules.find({'enabled': True}))

    print(f'[Alert] run_once — found {len(rules)} enabled rule(s)')
    if not rules:
        print('[Alert] No enabled alert rules found.')
        return

    for rule in rules:
        rule_id         = rule.get('rule_id', '')
        email           = rule.get('email', '')
        label           = rule.get('label', 'SensorIoT Alert')
        cooldown_min    = rule.get('cooldown_minutes', 60)
        last_triggered  = rule.get('last_triggered', 0) or 0
        push_enabled    = rule.get('push_enabled', True)
        webhook_url     = rule.get('webhook_url')
        webhook_secret  = rule.get('webhook_secret')

        print(f'[Alert] Evaluating rule_id={rule_id!r}, email={email!r}, label={label!r}, '
              f'gateway_id={rule.get("gateway_id")!r}, node_id={rule.get("node_id")!r}, '
              f'type={rule.get("type")!r}, operator={rule.get("operator")!r}, '
              f'threshold={rule.get("threshold")}, push_enabled={push_enabled}')

        # Respect cooldown
        cooldown_remaining = cooldown_min * 60 - (now - last_triggered)
        if cooldown_remaining > 0:
            print(f'[Alert]   → in cooldown ({cooldown_remaining/60:.1f} min remaining), skipping')
            continue

        triggered, current_value = _evaluate_rule(db, rule)
        print(f'[Alert]   → sensor value={current_value}, triggered={triggered}')
        if not triggered:
            continue

        print(f'[Alert] Rule {rule_id} triggered for {email}: {label}')

        gw   = rule.get('gateway_id', '')
        node = rule.get('node_id', '')
        stype = rule.get('type', 'F')
        notif_title, notif_body = _format_notification(db, rule, current_value)
        print(f'[Alert]   → notification: {notif_title!r} / {notif_body!r}')

        # FCM push
        if push_enabled and firebase_available:
            token_docs = list(db.DeviceTokens.find({'email': email}, {'token': 1, '_id': 0}))
            tokens = [d['token'] for d in token_docs if d.get('token')]
            print(f'[Alert]   → FCM: found {len(tokens)} device token(s) for {email!r}')
            _send_fcm_push(tokens, notif_title, notif_body, db=db)
        elif not firebase_available:
            print(f'[Alert]   → FCM skipped (Firebase not available)')
        elif not push_enabled:
            print(f'[Alert]   → FCM skipped (push_enabled=False)')

        # Webhook
        if webhook_url:
            payload = {
                'rule_id':    rule_id,
                'label':      label,
                'gateway_id': gw,
                'node_id':    node,
                'type':       stype,
                'value':      current_value,
                'threshold':  rule.get('threshold'),
                'operator':   rule.get('operator', '>'),
                'triggered_at': now,
            }
            _send_webhook(webhook_url, payload, webhook_secret)

        # Update last_triggered
        db.AlertRules.update_one(
            {'rule_id': rule_id},
            {'$set': {'last_triggered': now}},
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='AlertPublisher — evaluate alert rules and send push/webhook notifications',
    )
    parser.add_argument('--db', choices=['PROD', 'TEST'], default='TEST',
                        help='Database to use (default: TEST)')
    parser.add_argument('--dbconn', default='',
                        help='MongoDB host (overrides --db)')
    parser.add_argument('--interval', type=int, default=0,
                        help='Run every N minutes; 0 = run once and exit')
    parser.add_argument('--firebase-key',
                        default='../sensoriot-rest/firebase_service_account.json',
                        help='Path to Firebase service account JSON')
    args = parser.parse_args()

    # MongoDB
    if args.dbconn:
        client = mongodb.MongoClient(f'mongodb://{args.dbconn}/')
        db = client.gdtechdb_prod
        print(f'[Alert] Connected to {args.dbconn} (gdtechdb_prod)')
    elif args.db == 'PROD':
        client = mongodb.MongoClient('mongodb://localhost:27017/')
        db = client.gdtechdb_prod
        print('[Alert] Connected to localhost (gdtechdb_prod)')
    else:
        client = mongodb.MongoClient('mongodb://localhost:27017/')
        db = client.gdtechdb_test
        print('[Alert] Connected to localhost (gdtechdb_test)')

    firebase_ok = _init_firebase(args.firebase_key)

    if args.interval > 0:
        print(f'[Alert] Running every {args.interval} minute(s). Ctrl+C to stop.')
        while True:
            try:
                run_once(db, firebase_ok)
            except Exception as e:
                print(f'[Alert] Unexpected error: {e}')
            time.sleep(args.interval * 60)
    else:
        run_once(db, firebase_ok)


if __name__ == '__main__':
    main()
