"""AnomalyAlertPublisher.py — Evaluate ML anomaly alert rules and send notifications.

Runs as a background loop in the rest_server container (every 15 minutes).
For each AlertRule with operator='ml_anomaly', loads the trained gateway model,
predicts anomalies on recent data, and fires FCM push / webhook notifications
when the rule's sensor is flagged as anomalous.

Usage:
    python3 AnomalyAlertPublisher.py --dbconn host:port --interval 15
    python3 AnomalyAlertPublisher.py --dbconn host:port  # run once
"""

import argparse
import hashlib
import hmac
import json
import time
from collections import defaultdict

import requests
import pymongo as mongodb

import anomaly_training as _at

# Firebase Admin SDK — optional import
try:
    import firebase_admin
    from firebase_admin import credentials, messaging
    _FIREBASE_AVAILABLE = True
except ImportError:
    _FIREBASE_AVAILABLE = False

_firebase_initialised = False

_TYPE_LABELS = {
    'F':   ('temperature', '°F'),
    'H':   ('humidity',    '%'),
    'PWR': ('power',       'W'),
    'P':   ('pressure',    'hPa'),
    'HI':  ('heat index',  '°F'),
    'DP':  ('dew point',   '°F'),
}


def _init_firebase(service_account_path: str) -> bool:
    global _firebase_initialised
    if _firebase_initialised:
        return True
    if not _FIREBASE_AVAILABLE:
        print('[AnomalyAlert] firebase_admin not installed — push disabled.')
        return False
    try:
        cred = credentials.Certificate(service_account_path)
        firebase_admin.initialize_app(cred)
        _firebase_initialised = True
        print('[AnomalyAlert] Firebase initialised.')
        return True
    except Exception as e:
        print(f'[AnomalyAlert] Firebase init failed: {e}')
        return False


def _send_fcm_push(tokens, title, body, db=None):
    if not _firebase_initialised or not tokens:
        return
    for token in tokens:
        try:
            messaging.send(messaging.Message(
                notification=messaging.Notification(title=title, body=body),
                token=token,
            ))
            print(f'[AnomalyAlert] FCM sent OK for token {token[:12]}...')
        except messaging.UnregisteredError:
            print(f'[AnomalyAlert] Stale token {token[:12]}... — removing')
            if db is not None:
                db.DeviceTokens.delete_one({'token': token})
        except Exception as e:
            print(f'[AnomalyAlert] FCM failed for {token[:12]}...: {e}')


def _send_webhook(url, payload, secret):
    body = json.dumps(payload)
    headers = {'Content-Type': 'application/json'}
    if secret:
        sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        headers['X-SensorIoT-Signature'] = f'sha256={sig}'
    try:
        resp = requests.post(url, data=body, headers=headers, timeout=10)
        if not resp.ok:
            print(f'[AnomalyAlert] Webhook {url} returned {resp.status_code}')
    except Exception as e:
        print(f'[AnomalyAlert] Webhook failed ({url}): {e}')


def _node_name(db, gateway_id, node_id):
    doc = db.Nicknames.find_one(
        {'gateway_id': gateway_id, 'node_id': node_id},
        {'shortname': 1, 'longname': 1, '_id': 0},
    )
    if doc:
        for field in ('longname', 'shortname'):
            name = doc.get(field, '').strip()
            if name and name != node_id:
                return name
    return node_id


def run_once(db, firebase_available):
    now = time.time()

    # Find all enabled ml_anomaly rules
    rules = list(db.AlertRules.find({
        'operator': 'ml_anomaly',
        'enabled': True,
    }))
    if not rules:
        print('[AnomalyAlert] No enabled ml_anomaly rules found.')
        return

    print(f'[AnomalyAlert] Evaluating {len(rules)} ml_anomaly rule(s)')

    # Group rules by gateway_id (one model load per gateway)
    gw_rules = defaultdict(list)
    for rule in rules:
        gw_rules[rule.get('gateway_id', '')].append(rule)

    for gateway_id, gw_rule_list in gw_rules.items():
        if not gateway_id:
            continue

        # Check model exists
        if not _at.model_exists(gateway_id):
            print(f'[AnomalyAlert] No model for gateway {gateway_id} — skipping')
            continue

        # Load model
        try:
            model, metadata = _at.load_model(gateway_id)
            feature_columns = metadata.get('feature_columns', [])
        except Exception as e:
            print(f'[AnomalyAlert] Failed to load model for {gateway_id}: {e}')
            continue

        # Build recent gateway DataFrame (1 day lookback)
        try:
            gw_df = _at.get_gateway_dataframe(db, gateway_id, lookback_days=1)
        except Exception as e:
            print(f'[AnomalyAlert] Failed to get data for {gateway_id}: {e}')
            continue

        if gw_df is None or gw_df.empty:
            print(f'[AnomalyAlert] No recent data for gateway {gateway_id}')
            continue

        # Predict anomalies
        available_cols = [c for c in feature_columns if c in gw_df.columns]
        pred_df = gw_df[['time_rounded'] + available_cols].dropna(subset=available_cols)
        if pred_df.empty:
            continue

        try:
            anomalous_ts = _at.predict_anomalies(
                model, pred_df, feature_columns=available_cols)
        except Exception as e:
            print(f'[AnomalyAlert] Prediction failed for {gateway_id}: {e}')
            continue

        anomalous_set = set(anomalous_ts)

        # Get the most recent timestamps in the data
        latest_ts = pred_df['time_rounded'].max()

        # Evaluate each rule on this gateway
        for rule in gw_rule_list:
            rule_id = rule.get('rule_id', '')
            email = rule.get('email', '')
            node_id = rule.get('node_id', '')
            sensor_type = rule.get('type', 'F')
            cooldown_min = rule.get('cooldown_minutes', 360)
            last_triggered = rule.get('last_triggered', 0) or 0
            push_enabled = rule.get('push_enabled', True)
            webhook_url = rule.get('webhook_url')
            webhook_secret = rule.get('webhook_secret')
            label = rule.get('label', 'ML Anomaly Alert')

            # Cooldown check
            cooldown_remaining = cooldown_min * 60 - (now - last_triggered)
            if cooldown_remaining > 0:
                print(f'[AnomalyAlert] Rule {rule_id}: cooling down '
                      f'({cooldown_remaining/60:.0f} min remaining)')
                continue

            # Check if the latest reading for this node is anomalous
            # The anomaly model is gateway-wide; filter to timestamps where
            # this node had a reading
            node_col = f'{node_id}_{sensor_type}'
            if node_col in pred_df.columns:
                node_ts = set(pred_df.loc[
                    pred_df[node_col].notna(), 'time_rounded'].tolist())
                node_anomalies = anomalous_set & node_ts
            else:
                node_anomalies = anomalous_set

            if not node_anomalies:
                print(f'[AnomalyAlert] Rule {rule_id}: no anomalies for '
                      f'{node_id}/{sensor_type}')
                continue

            # Check if the most recent bucket is anomalous
            latest_node_ts = max(node_anomalies)
            # Only fire if the anomaly is recent (within last hour)
            if now - latest_node_ts > 3600:
                print(f'[AnomalyAlert] Rule {rule_id}: anomaly too old '
                      f'({(now - latest_node_ts)/60:.0f} min ago)')
                continue

            # Fire alert
            name = _node_name(db, gateway_id, node_id)
            type_label, unit = _TYPE_LABELS.get(sensor_type, (sensor_type, ''))
            notif_body = f'ML anomaly detected: {name} {type_label} readings are unusual.'

            print(f'[AnomalyAlert] Rule {rule_id} TRIGGERED for {email}: {notif_body}')

            # FCM push
            if push_enabled and firebase_available:
                token_docs = list(db.DeviceTokens.find(
                    {'email': email}, {'token': 1, '_id': 0}))
                tokens = [d['token'] for d in token_docs if d.get('token')]
                _send_fcm_push(tokens, label, notif_body, db=db)

            # Webhook
            if webhook_url:
                payload = {
                    'rule_id': rule_id,
                    'label': label,
                    'gateway_id': gateway_id,
                    'node_id': node_id,
                    'type': sensor_type,
                    'operator': 'ml_anomaly',
                    'anomaly_timestamp': latest_node_ts,
                    'triggered_at': now,
                }
                _send_webhook(webhook_url, payload, webhook_secret)

            # Update last_triggered
            db.AlertRules.update_one(
                {'rule_id': rule_id},
                {'$set': {'last_triggered': now}},
            )


def main():
    parser = argparse.ArgumentParser(
        description='AnomalyAlertPublisher — evaluate ML anomaly alert rules')
    parser.add_argument('--db', choices=['PROD', 'TEST'], default='PROD')
    parser.add_argument('--dbconn', default='')
    parser.add_argument('--interval', type=int, default=0,
                        help='Run every N minutes; 0 = run once')
    parser.add_argument('--firebase-key',
                        default='../firebase_service_account.json')
    args = parser.parse_args()

    if args.dbconn:
        client = mongodb.MongoClient(f'mongodb://{args.dbconn}/')
    else:
        host = 'host.docker.internal' if args.db == 'PROD' else 'localhost'
        client = mongodb.MongoClient(f'mongodb://{host}:27017/')

    db_name = 'gdtechdb_prod' if args.db == 'PROD' else 'gdtechdb_test'
    db = client[db_name]

    firebase_ok = _init_firebase(args.firebase_key)

    if args.interval > 0:
        print(f'[AnomalyAlert] Running every {args.interval} minute(s)...')
        while True:
            try:
                run_once(db, firebase_ok)
            except Exception as e:
                print(f'[AnomalyAlert] Unexpected error: {e}')
            print(f'[AnomalyAlert] Sleeping for {args.interval} minute(s)...')
            time.sleep(args.interval * 60)
    else:
        run_once(db, firebase_ok)


if __name__ == '__main__':
    main()
