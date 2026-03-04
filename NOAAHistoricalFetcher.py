#!/usr/bin/env python3
"""
NOAAHistoricalFetcher.py — Backfills historical NOAA weather observations into
the SensorIoT MongoDB database, using the same document schema as NOAAPublisher.py.

IMPORTANT: NOAA's api.weather.gov does NOT expose archived forecast data (past
predictions are stored as binary GRIB2 files). What this script fetches is
historical *observations* — actual recorded readings from the nearest NOAA
weather station — which is more useful for correlating with real sensor data.

Because records are stored with node_id='noaa_forecast' and type='F', they are
returned by the existing /forecast/<gw>?hours_back=N REST endpoint and rendered
as the muted past series on the Flutter chart overlay without any app changes.

Typical one-off backfill (uses all enabled NOAASettings entries):
    pipenv run python3 NOAAHistoricalFetcher.py --db PROD --start 2024-01-01 --end 2024-12-31

Override with a specific location:
    pipenv run python3 NOAAHistoricalFetcher.py --db PROD --start 2024-01-01 \\
        --lat 41.8827 --lon -87.6233 --gateway my-gw-id
"""

import argparse
import datetime
import time
from typing import Generator

import pymongo as mongodb
import requests

USER_AGENT  = '(SensorIoT, keyvanazami@gmail.com)'
NOAA_BASE   = 'https://api.weather.gov'
PAGE_LIMIT  = 500
SLEEP_SECS  = 0.5   # per NOAA ToS
NODE_ID     = 'noaa_forecast'
MODEL       = 'NOAA'
SENSOR_TYPE = 'F'


# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------

def connect_db(args: argparse.Namespace):
    """Connect to MongoDB using the same three-branch logic as NOAAPublisher.py."""
    if args.dbconn:
        client = mongodb.MongoClient(f'mongodb://{args.dbconn}/')
        db = client.gdtechdb_prod
        print(f'[NOAAHist] Connected to {args.dbconn} (gdtechdb_prod)')
    elif args.db == 'PROD':
        client = mongodb.MongoClient('mongodb://localhost:27017/')
        db = client.gdtechdb_prod
        print('[NOAAHist] Connected to localhost (gdtechdb_prod)')
    else:
        client = mongodb.MongoClient('mongodb://localhost:27017/')
        db = client.gdtechdb_test
        print('[NOAAHist] Connected to localhost (gdtechdb_test)')
    return db


# ---------------------------------------------------------------------------
# NOAA API helpers
# ---------------------------------------------------------------------------

def get_station_id(lat: float, lon: float) -> str | None:
    """
    Resolve a lat/lon to the nearest NOAA observation station identifier.

    Uses the same NOAA Points API as NOAAPublisher.get_forecast_url(), but
    follows the observationStations URL instead of forecastHourly.
    Returns a station ID string like 'KORD', or None on failure.
    """
    points_url = f'{NOAA_BASE}/points/{lat:.4f},{lon:.4f}'
    try:
        resp = requests.get(points_url, headers={'User-Agent': USER_AGENT}, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f'[NOAAHist] Failed to get points for ({lat}, {lon}): {e}')
        return None

    time.sleep(SLEEP_SECS)

    stations_url = resp.json().get('properties', {}).get('observationStations')
    if not stations_url:
        print(f'[NOAAHist] No observationStations URL in Points response for ({lat}, {lon})')
        return None

    try:
        resp2 = requests.get(
            stations_url,
            params={'limit': 5},
            headers={'User-Agent': USER_AGENT},
            timeout=15,
        )
        resp2.raise_for_status()
    except Exception as e:
        print(f'[NOAAHist] Failed to fetch station list: {e}')
        return None

    features = resp2.json().get('features', [])
    if not features:
        print(f'[NOAAHist] No observation stations found near ({lat}, {lon}). '
              'Note: NOAA only covers US territories.')
        return None

    station_id = features[0]['properties']['stationIdentifier']
    print(f'[NOAAHist] Resolved ({lat}, {lon}) → station {station_id}')
    return station_id


def celsius_to_fahrenheit(c: float) -> float:
    """Convert Celsius to Fahrenheit, rounded to one decimal place."""
    return round(c * 9 / 5 + 32, 1)


def fetch_observations_page(
    station_id: str,
    start_iso: str,
    end_iso: str,
    url_override: str | None = None,
) -> dict:
    """
    Fetch one page of observations from the NOAA Observations API.
    If url_override is given (e.g. @odata.nextLink), fetch that URL directly.
    Returns the parsed JSON dict, or {} on any error.
    """
    try:
        if url_override:
            resp = requests.get(url_override, headers={'User-Agent': USER_AGENT}, timeout=20)
        else:
            url = f'{NOAA_BASE}/stations/{station_id}/observations'
            resp = requests.get(
                url,
                params={'start': start_iso, 'end': end_iso, 'limit': PAGE_LIMIT},
                headers={'User-Agent': USER_AGENT},
                timeout=20,
            )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f'[NOAAHist] HTTP error fetching observations for {station_id}: {e}')
        return {}


def iter_observations(
    station_id: str,
    start_dt: datetime.datetime,
    end_dt: datetime.datetime,
) -> Generator[dict, None, None]:
    """
    Yield individual observation feature dicts for station_id in [start_dt, end_dt].

    Splits the range into weekly chunks (7 days × 24 h = 168 records, well under
    the 500-record page limit) to avoid pagination in the common case. Follows
    @odata.nextLink if present, with a safety cap of 20 pages per chunk.
    """
    chunk_start = start_dt
    week = datetime.timedelta(days=7)

    while chunk_start < end_dt:
        chunk_end = min(chunk_start + week, end_dt)
        start_iso = chunk_start.strftime('%Y-%m-%dT%H:%M:%SZ')
        end_iso   = chunk_end.strftime('%Y-%m-%dT%H:%M:%SZ')

        print(f'[NOAAHist]   Fetching {start_iso[:10]} → {end_iso[:10]} ...')

        next_url: str | None = None
        pages_fetched = 0

        while True:
            data = fetch_observations_page(station_id, start_iso, end_iso, url_override=next_url)
            time.sleep(SLEEP_SECS)
            pages_fetched += 1

            features = data.get('features', [])
            for feature in features:
                yield feature

            next_url = data.get('@odata.nextLink')
            if not next_url or not features or pages_fetched >= 20:
                break

        chunk_start = chunk_end


def parse_observation_timestamp(feature: dict) -> float | None:
    """Extract the observation's ISO 8601 timestamp as a Unix float, or None on failure."""
    try:
        ts_str = feature['properties']['timestamp']
        return datetime.datetime.fromisoformat(ts_str).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def round_to_hour(ts: float) -> float:
    """Round a Unix timestamp to the nearest hour boundary."""
    return round(ts / 3600) * 3600


def parse_observation_temp_f(feature: dict) -> float | None:
    """
    Extract temperature in °F from an observation feature.
    NOAA observations return temperature in Celsius (wmoUnit:degC).
    Returns None if the reading is null or missing.
    """
    try:
        temp_obj = feature['properties']['temperature']
        if temp_obj is None or temp_obj.get('value') is None:
            return None
        return celsius_to_fahrenheit(float(temp_obj['value']))
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# MongoDB helpers
# ---------------------------------------------------------------------------

def get_existing_timestamps(
    db,
    gateway_id: str,
    start_ts: float,
    end_ts: float,
) -> set:
    """
    Return a set of hour-bucket integers (floor-to-hour Unix timestamps) for
    NOAA records already present in the Sensors collection for this gateway
    and time range. Used for O(1) deduplication during insertion.
    """
    cursor = db.Sensors.find(
        {
            'gateway_id': gateway_id,
            'node_id':    NODE_ID,
            'type':       SENSOR_TYPE,
            'time':       {'$gte': start_ts, '$lte': end_ts},
        },
        {'time': 1, '_id': 0},
    )
    return {int(doc['time'] // 3600) * 3600 for doc in cursor}


def build_document(gateway_id: str, temp_f: float, unix_ts: float) -> dict:
    """Build a Sensors collection document matching the NOAAPublisher.py schema."""
    return {
        'model':      MODEL,
        'gateway_id': gateway_id,
        'node_id':    NODE_ID,
        'type':       SENSOR_TYPE,
        'value':      str(temp_f),
        'time':       float(unix_ts),
    }


# ---------------------------------------------------------------------------
# Per-gateway pipeline
# ---------------------------------------------------------------------------

def process_gateway(
    db,
    gateway_id: str,
    lat: float,
    lon: float,
    start_dt: datetime.datetime,
    end_dt: datetime.datetime,
) -> dict:
    """
    Fetch historical NOAA observations for one gateway and insert into MongoDB.
    Returns a summary dict with counts for reporting.
    """
    print(f'[NOAAHist] Processing gateway={gateway_id} lat={lat} lon={lon} '
          f'from {start_dt.date()} to {end_dt.date()}')

    station_id = get_station_id(lat, lon)
    if station_id is None:
        return {'gateway_id': gateway_id, 'station_id': None,
                'fetched': 0, 'skipped': 0, 'inserted': 0}

    start_ts = start_dt.timestamp()
    end_ts   = end_dt.timestamp()

    existing = get_existing_timestamps(db, gateway_id, start_ts, end_ts)
    print(f'[NOAAHist] Found {len(existing)} existing hour-bucket(s) in range.')

    docs_to_insert = []
    fetched = skipped = 0

    for feature in iter_observations(station_id, start_dt, end_dt):
        fetched += 1

        obs_ts = parse_observation_timestamp(feature)
        temp_f = parse_observation_temp_f(feature)

        if obs_ts is None or temp_f is None:
            skipped += 1
            continue

        rounded_ts = round_to_hour(obs_ts)
        if rounded_ts in existing:
            skipped += 1
            continue

        docs_to_insert.append(build_document(gateway_id, temp_f, rounded_ts))
        existing.add(rounded_ts)  # prevent intra-run duplicates

    inserted = 0
    if docs_to_insert:
        try:
            db.Sensors.insert_many(docs_to_insert, ordered=False)
            inserted = len(docs_to_insert)
        except mongodb.errors.BulkWriteError as e:
            inserted = e.details.get('nInserted', 0)
            print(f'[NOAAHist] BulkWriteError for gateway={gateway_id}: '
                  f'{inserted} inserted before error — {e.details.get("writeErrors", [])}')

    print(f'[NOAAHist] gateway={gateway_id}: '
          f'fetched={fetched} skipped={skipped} inserted={inserted}')

    return {
        'gateway_id': gateway_id,
        'station_id': station_id,
        'fetched':    fetched,
        'skipped':    skipped,
        'inserted':   inserted,
    }


# ---------------------------------------------------------------------------
# CLI and entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='NOAAHistoricalFetcher — Backfill historical NOAA weather observations '
                    'into the SensorIoT MongoDB database.'
    )
    parser.add_argument(
        '--db', choices=['PROD', 'TEST'], default='TEST',
        help='Database to use (default: TEST)',
    )
    parser.add_argument(
        '--dbconn', default='',
        help='MongoDB host:port override (connects to gdtechdb_prod)',
    )
    parser.add_argument(
        '--start', required=True,
        help='Start date in YYYY-MM-DD format (required)',
    )
    parser.add_argument(
        '--end', default=None,
        help='End date in YYYY-MM-DD format (default: yesterday)',
    )
    parser.add_argument('--lat',     type=float, default=None, help='Latitude override')
    parser.add_argument('--lon',     type=float, default=None, help='Longitude override')
    parser.add_argument('--gateway', type=str,   default=None, help='Gateway ID override')

    args = parser.parse_args()

    # --lat / --lon / --gateway must all appear together or not at all
    manual = [args.lat, args.lon, args.gateway]
    if any(v is not None for v in manual) and not all(v is not None for v in manual):
        parser.error('--lat, --lon, and --gateway must all be provided together.')

    return args


def main() -> None:
    args = parse_args()
    db   = connect_db(args)

    # Parse date range
    try:
        start_dt = datetime.datetime.strptime(args.start, '%Y-%m-%d').replace(
            tzinfo=datetime.timezone.utc
        )
    except ValueError:
        print(f'[NOAAHist] Invalid --start date: {args.start!r}. Expected YYYY-MM-DD.')
        return

    if args.end:
        try:
            end_dt = datetime.datetime.strptime(args.end, '%Y-%m-%d').replace(
                hour=23, minute=59, second=59, tzinfo=datetime.timezone.utc
            )
        except ValueError:
            print(f'[NOAAHist] Invalid --end date: {args.end!r}. Expected YYYY-MM-DD.')
            return
    else:
        yesterday = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
        end_dt = yesterday.replace(hour=23, minute=59, second=59, microsecond=0)

    if end_dt <= start_dt:
        print(f'[NOAAHist] --end ({end_dt.date()}) must be after --start ({start_dt.date()}).')
        return

    # Build list of (gateway_id, lat, lon) targets
    if args.lat is not None:
        targets = [{'gateway_id': args.gateway, 'lat': args.lat, 'lon': args.lon}]
    else:
        targets = list(db.NOAASettings.find({'enabled': True},
                                            {'gateway_id': 1, 'lat': 1, 'lon': 1, '_id': 0}))
        if not targets:
            print('[NOAAHist] No enabled entries found in NOAASettings collection. Exiting.')
            return

    print(f'[NOAAHist] Processing {len(targets)} gateway(s) '
          f'from {start_dt.date()} to {end_dt.date()}')

    results = []
    for entry in targets:
        gw  = entry.get('gateway_id')
        lat = entry.get('lat')
        lon = entry.get('lon')
        if not gw or lat is None or lon is None:
            print(f'[NOAAHist] Skipping incomplete entry: {entry}')
            continue
        results.append(process_gateway(db, gw, lat, lon, start_dt, end_dt))

    total_inserted = sum(r['inserted'] for r in results)
    print(f'\n[NOAAHist] Done. Total inserted across all gateways: {total_inserted}')
    for r in results:
        print(f"  gateway={r['gateway_id']}  station={r.get('station_id') or 'N/A'}"
              f"  fetched={r['fetched']}  skipped={r['skipped']}  inserted={r['inserted']}")


if __name__ == '__main__':
    main()
