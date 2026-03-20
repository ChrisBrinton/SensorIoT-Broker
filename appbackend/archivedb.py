#!/usr/bin/env python3
"""
archivedb.py — Backup and prune the Sensors collection.

Archives documents older than N months from MongoDB to a compressed
JSONL (newline-delimited JSON) file, then optionally removes them.

Usage:
  pipenv run python3 archivedb.py -d PROD -m 6 [--output-dir ./archives] [--remove]
  pipenv run python3 archivedb.py --db=PROD --months=6 --output-dir=/data/archives --remove

Options:
  -d / --db           Database alias: PROD or TEST  (required)
  -m / --months       Archive records older than this many months  (default: 6)
  -o / --output-dir   Directory to write archive files  (default: ./archives)
  -r / --remove       Actually delete records after archiving (default: dry-run)
  -h                  Show this help

Output files (written to --output-dir):
  sensors_archive_{db}_{cutoff}_{ts}.jsonl.gz   — Compressed NDJSON records
  sensors_archive_{db}_{cutoff}_{ts}.meta.json  — Archive metadata / stats

The JSONL format is directly loadable for analysis:
  import pandas as pd
  df = pd.read_json('sensors_archive_PROD_2025-08-01_....jsonl.gz', lines=True)
  df['human_time'] = pd.to_datetime(df['time'], unit='s', utc=True)
"""

import sys
import getopt
import gzip
import json
import os
import datetime as dt

from bson import ObjectId
from pymongo import MongoClient
from dateutil.tz import tzutc


DB_MAP = {
    'PROD': 'gdtechdb_prod',
    'TEST': 'gdtechdb_test',
}

BATCH_SIZE = 10_000


class BSONEncoder(json.JSONEncoder):
    """Serialize BSON/datetime types that stdlib json cannot handle."""

    def default(self, obj):
        if isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, dt.datetime):
            return obj.isoformat()
        if isinstance(obj, bytes):
            return obj.decode('utf-8', errors='replace')
        return super().default(obj)


def cutoff_timestamp(months):
    """Return Unix timestamp for N months ago (30 days per month)."""
    now = dt.datetime.now(tzutc())
    delta = dt.timedelta(days=int(months) * 30)
    return (now - delta).timestamp()


def archive_to_file(collection, cutoff_ts, output_path):
    """
    Stream all Sensors documents with time <= cutoff_ts into a gzipped JSONL file.

    Returns a dict with keys: count, min_time, max_time.
    """
    qry = {'time': {'$lte': cutoff_ts}}
    total = collection.count_documents(qry)
    print(f'Documents to archive: {total:,}')

    if total == 0:
        return {'count': 0, 'min_time': None, 'max_time': None}

    min_time = float('inf')
    max_time = float('-inf')
    written = 0

    print(f'Writing to {output_path} ...')
    cursor = collection.find(qry).sort('time', 1).batch_size(BATCH_SIZE)

    with gzip.open(output_path, 'wt', encoding='utf-8') as fout:
        for doc in cursor:
            t = doc.get('time', 0)
            if t < min_time:
                min_time = t
            if t > max_time:
                max_time = t
            fout.write(json.dumps(doc, cls=BSONEncoder) + '\n')
            written += 1
            if written % BATCH_SIZE == 0:
                pct = written / total * 100
                print(f'  ... {written:,} / {total:,}  ({pct:.0f}%)')

    print(f'Archive written: {written:,} records.')
    return {
        'count': written,
        'min_time': min_time if min_time != float('inf') else None,
        'max_time': max_time if max_time != float('-inf') else None,
    }


def verify_archive(output_path, expected_count):
    """Count lines in the gzipped JSONL file and confirm it matches expected_count."""
    print('Verifying archive integrity...')
    count = 0
    with gzip.open(output_path, 'rt', encoding='utf-8') as fin:
        for line in fin:
            if line.strip():
                count += 1
    passed = count == expected_count
    status = 'PASSED' if passed else 'FAILED'
    print(f'  Verification {status}: {count:,} lines (expected {expected_count:,})')
    return passed


def delete_archived_records(collection, cutoff_ts):
    """Delete all documents with time <= cutoff_ts. Returns deleted count."""
    qry = {'time': {'$lte': cutoff_ts}}
    print('Deleting archived records from MongoDB...')
    result = collection.delete_many(qry)
    print(f'Deleted {result.deleted_count:,} documents.')
    return result.deleted_count


def write_meta(meta_path, meta):
    with open(meta_path, 'w') as f:
        json.dump(meta, f, indent=2)
    print(f'Metadata written to {meta_path}')


def printhelp():
    print(__doc__)


def main(argv):
    db_alias = ''
    months = 6
    output_dir = './archives'
    remove = False

    try:
        opts, _ = getopt.getopt(argv, 'hrd:m:o:', ['db=', 'months=', 'output-dir=', 'remove'])
    except getopt.GetoptError as e:
        print(f'Error: {e}')
        printhelp()
        sys.exit(1)

    for opt, arg in opts:
        if opt == '-h':
            printhelp()
            sys.exit(0)
        elif opt in ('-d', '--db'):
            db_alias = arg.upper()
        elif opt in ('-m', '--months'):
            months = int(arg)
        elif opt in ('-o', '--output-dir'):
            output_dir = arg
        elif opt in ('-r', '--remove'):
            remove = True

    if db_alias not in DB_MAP:
        print(f'Error: --db must be one of {list(DB_MAP.keys())}')
        printhelp()
        sys.exit(1)

    if months <= 0:
        print('Error: --months must be > 0')
        sys.exit(1)

    db_name = DB_MAP[db_alias]
    cutoff_ts = cutoff_timestamp(months)
    cutoff_dt = dt.datetime.fromtimestamp(cutoff_ts, tz=tzutc())
    cutoff_str = cutoff_dt.strftime('%Y-%m-%d')
    ts_str = dt.datetime.now(tzutc()).strftime('%Y%m%dT%H%M%SZ')

    print('=' * 60)
    print(f'  Database   : {db_name}')
    print(f'  Cutoff     : {cutoff_dt.strftime("%Y-%m-%d %H:%M:%S UTC")} ({months} months)')
    print(f'  Output dir : {output_dir}')
    print(f'  Mode       : {"REMOVE (archive + delete)" if remove else "DRY RUN (count only — use --remove to archive)"}')
    print('=' * 60)

    client = MongoClient('localhost', 27017)
    collection = client[db_name]['Sensors']

    # ---- Dry-run: just count and exit ----
    if not remove:
        qry = {'time': {'$lte': cutoff_ts}}
        count = collection.count_documents(qry)
        print(f'\nDRY RUN: {count:,} documents would be archived and removed.')
        print('Re-run with --remove to execute.')
        client.close()
        return

    # ---- Archive ----
    os.makedirs(output_dir, exist_ok=True)
    base_name = f'sensors_archive_{db_alias}_{cutoff_str}_{ts_str}'
    archive_path = os.path.join(output_dir, base_name + '.jsonl.gz')
    meta_path = os.path.join(output_dir, base_name + '.meta.json')

    started_at = dt.datetime.now(tzutc())
    stats = archive_to_file(collection, cutoff_ts, archive_path)

    if stats['count'] == 0:
        print('Nothing to archive. Exiting.')
        client.close()
        return

    # ---- Verify ----
    if not verify_archive(archive_path, stats['count']):
        print('\nERROR: Archive verification failed. Aborting deletion to prevent data loss.')
        print(f'Inspect the archive file: {archive_path}')
        client.close()
        sys.exit(1)

    # ---- Delete ----
    deleted = delete_archived_records(collection, cutoff_ts)
    finished_at = dt.datetime.now(tzutc())
    duration = (finished_at - started_at).total_seconds()

    # ---- Write metadata sidecar ----
    meta = {
        'db': db_name,
        'db_alias': db_alias,
        'cutoff_timestamp': cutoff_ts,
        'cutoff_date': cutoff_str,
        'months': months,
        'archive_file': os.path.basename(archive_path),
        'records_archived': stats['count'],
        'records_deleted': deleted,
        'earliest_record': (
            dt.datetime.fromtimestamp(stats['min_time'], tz=tzutc()).isoformat()
            if stats['min_time'] is not None else None
        ),
        'latest_record': (
            dt.datetime.fromtimestamp(stats['max_time'], tz=tzutc()).isoformat()
            if stats['max_time'] is not None else None
        ),
        'started_at': started_at.isoformat(),
        'finished_at': finished_at.isoformat(),
        'duration_seconds': round(duration, 1),
        'archive_size_bytes': os.path.getsize(archive_path),
    }
    write_meta(meta_path, meta)

    archive_mb = meta['archive_size_bytes'] / 1_048_576
    print()
    print('=' * 60)
    print(f'  Done in {duration:.1f}s')
    print(f'  Records archived : {stats["count"]:,}')
    print(f'  Records deleted  : {deleted:,}')
    print(f'  Archive size     : {archive_mb:.1f} MB  ({archive_path})')
    print(f'  Metadata         : {meta_path}')
    print('=' * 60)
    print()
    print('Load for analysis with pandas:')
    print(f'  import pandas as pd')
    print(f'  df = pd.read_json("{archive_path}", lines=True)')
    print(f'  df["human_time"] = pd.to_datetime(df["time"], unit="s", utc=True)')

    client.close()


if __name__ == '__main__':
    main(sys.argv[1:])
