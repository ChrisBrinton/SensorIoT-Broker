#!/usr/bin/env python3
"""
NOAAPublisher.py - Fetches NOAA 7-day weather forecasts and publishes them
as virtual sensor readings into the SensorIoT MongoDB database.

For each opted-in user (gateway) in the NOAASettings collection, this script:
  1. Fetches the 7-day forecast from api.weather.gov
  2. Deletes any existing future NOAA records for that gateway
  3. Inserts ~14 fresh forecast periods (node_id='noaa_forecast', type='F')

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
        temp = period.get('temperature')
        if temp is None:
            continue

        # NOAA normally returns °F; convert °C if the unit differs
        if period.get('temperatureUnit', 'F') == 'C':
            temp = round(temp * 9 / 5 + 32, 1)

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


def run_once(db) -> None:
    """Fetch forecasts for all opted-in users and publish to MongoDB."""
    settings_list = list(db.NOAASettings.find({'enabled': True}))

    if not settings_list:
        print('[NOAA] No opted-in users found in NOAASettings collection.')
        return

    for user in settings_list:
        email = user.get('email', 'unknown')
        gateway_id = user.get('gateway_id')
        lat = user.get('lat')
        lon = user.get('lon')

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

    if args.interval > 0:
        print(f'[NOAA] Running every {args.interval} minute(s). Press Ctrl+C to stop.')
        while True:
            try:
                run_once(db)
            except Exception as e:
                print(f'[NOAA] Unexpected error during run: {e}')
            print(f'[NOAA] Sleeping for {args.interval} minute(s)...')
            time.sleep(args.interval * 60)
    else:
        run_once(db)


if __name__ == '__main__':
    main()
