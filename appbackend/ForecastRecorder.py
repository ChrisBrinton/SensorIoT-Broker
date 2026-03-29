"""ForecastRecorder.py — Periodically generate and store regression forecasts.

Runs as a background loop inside the rest_server container (alongside
gunicorn). On each run, discovers all trained regression models, generates
a 48-hour forecast for each sensor, and inserts the predictions into the
`RegressionForecasts` MongoDB collection.

Stored forecasts can be compared against actual observations for model
evaluation, chart overlays, and future fine-tuning.

Usage:
    python3 ForecastRecorder.py --dbconn host:port --interval 60
    python3 ForecastRecorder.py --dbconn host:port   # run once
"""

import argparse
import os
import sys
import time

import pymongo as mongodb

import regression_training as _rt


def run_once(db):
    """Generate forecasts for all trained sensors and store in MongoDB."""
    now = time.time()
    batch_id = int(now)  # group all forecasts from this run

    models_dir = _rt.MODELS_DIR
    if not os.path.isdir(models_dir):
        print('[ForecastRecorder] No models directory found')
        return

    total_inserted = 0
    for gw_dir in sorted(os.listdir(models_dir)):
        reg_dir = os.path.join(models_dir, gw_dir, 'regression')
        if not os.path.isdir(reg_dir):
            continue

        metas = _rt.load_all_regression_metadata(gw_dir, models_dir)
        if not metas:
            continue

        for meta in metas:
            node_id = meta.get('node_id')
            sensor_type = meta.get('type')
            if not node_id or not sensor_type:
                continue

            try:
                forecast = _rt.predict_sensor_forecast(
                    gw_dir, node_id, sensor_type, db, hours=48,
                    models_dir=models_dir)
            except Exception as e:
                print(f'[ForecastRecorder] Prediction failed for '
                      f'{gw_dir}/{node_id}/{sensor_type}: {e}')
                continue

            if not forecast:
                continue

            docs = []
            for point in forecast:
                docs.append({
                    'gateway_id': gw_dir,
                    'node_id': node_id,
                    'type': sensor_type,
                    'forecast_time': point['timestamp'],
                    'predicted': point['predicted'],
                    'recorded_at': now,
                    'batch_id': batch_id,
                    'model_type': meta.get('model_type', ''),
                    'r2': meta.get('r2'),
                })

            if docs:
                db.RegressionForecasts.insert_many(docs)
                total_inserted += len(docs)

    print(f'[ForecastRecorder] Inserted {total_inserted} forecast point(s) '
          f'at {time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now))}')


def main():
    parser = argparse.ArgumentParser(description='Record regression forecasts')
    parser.add_argument('--db', default='PROD', choices=['PROD', 'TEST'])
    parser.add_argument('--dbconn', default='')
    parser.add_argument('--interval', type=int, default=0,
                        help='Run every N minutes; 0 = run once')
    args = parser.parse_args()

    if args.dbconn:
        client = mongodb.MongoClient(f'mongodb://{args.dbconn}/')
    else:
        host = 'host.docker.internal' if args.db == 'PROD' else 'localhost'
        client = mongodb.MongoClient(f'mongodb://{host}:27017/')

    db_name = 'gdtechdb_prod' if args.db == 'PROD' else 'gdtechdb_test'
    db = client[db_name]

    # Ensure indexes for efficient querying
    db.RegressionForecasts.create_index([
        ('gateway_id', 1), ('node_id', 1), ('type', 1), ('forecast_time', 1),
    ])
    db.RegressionForecasts.create_index([('recorded_at', 1)])

    if args.interval > 0:
        print(f'[ForecastRecorder] Running every {args.interval} minute(s)...')
        while True:
            try:
                run_once(db)
            except Exception as e:
                print(f'[ForecastRecorder] Unexpected error: {e}')
            print(f'[ForecastRecorder] Sleeping for {args.interval} minute(s)...')
            time.sleep(args.interval * 60)
    else:
        run_once(db)


if __name__ == '__main__':
    main()
