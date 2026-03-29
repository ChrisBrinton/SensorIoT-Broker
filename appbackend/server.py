from flask import Flask, request, g
from flask_cors import CORS
from dotenv import load_dotenv
from functools import wraps
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from dateutil.tz import tzutc, gettz
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from pymongo import MongoClient
import pymongo
import json
import os
import threading
import time
import uuid
import datetime as dt
import base64
load_dotenv()

import anomaly_training as _at
import regression_training as _rt

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

print('connecting to mongo...')
client = MongoClient(os.getenv('MONGODB_HOST', 'localhost'), 27017)
db = client['gdtechdb_prod']
sensors = db['Sensors']
sensorsLatest = db['SensorsLatest']
nicknames = db['Nicknames']
userProfiles = db['UserProfiles']
noaaSettings = db['NOAASettings']

import app_state as _app_state
_app_state.sensors_latest = sensorsLatest
_app_state.user_profiles = userProfiles
_app_state.nicknames_col = nicknames

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _verify_google_token(token):
    """Verify a Google ID token and return the email, or None if invalid."""
    try:
        audience = os.getenv('GOOGLE_WEB_CLIENT_ID')
        print(f'[Auth] Verifying token (len={len(token)}, audience={audience!r})')
        idinfo = id_token.verify_oauth2_token(token, google_requests.Request(), audience)
        email = idinfo.get('email')
        print(f'[Auth] Token OK — email={email}, iss={idinfo.get("iss")}, exp={idinfo.get("exp")}')
        return email
    except Exception as e:
        print(f'[Auth] Token verification FAILED: {e}')
        return None


def require_google_auth(f):
    """Decorator: verifies Bearer token, sets g.user_email, or returns 401."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        print(f'[Auth] {request.method} {request.path} — Authorization header present: {bool(auth_header)}')
        if not auth_header.startswith('Bearer '):
            print(f'[Auth] Missing/malformed Authorization header: {auth_header!r}')
            return json.dumps({'error': 'missing token'}), 401
        token = auth_header[len('Bearer '):]
        email = _verify_google_token(token)
        if not email:
            return json.dumps({'error': 'invalid token'}), 401
        g.user_email = email
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = Flask(__name__)
CORS(app)

from auth import auth_bp
from fulfillment import fulfillment_bp
app.register_blueprint(auth_bp)
app.register_blueprint(fulfillment_bp)

timefmt = '%Y-%m-%d %H:%M:%S'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def cleanvalue(value):
    return float(value.replace('b', '').replace('v', '').replace("'", ""))


def getstart(p):
    """Return a Unix timestamp p hours before now. Defaults to 24 h."""
    nowdatetime = dt.datetime.now(tzutc())
    if p is None:
        diff = dt.timedelta(hours=24)
    else:
        diff = dt.timedelta(hours=int(p))
    return (nowdatetime - diff).timestamp()


def decrypt_password_aes(encrypted_password_base64, shared_key_base64):
    """Decrypt a base64-encoded AES-256-CBC password. IV is prepended to ciphertext."""
    try:
        shared_key_bytes = base64.urlsafe_b64decode(shared_key_base64)
        encrypted_data = base64.urlsafe_b64decode(encrypted_password_base64)
        iv = encrypted_data[:16]
        ciphertext = encrypted_data[16:]
        cipher = Cipher(algorithms.AES(shared_key_bytes), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        decrypted_padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
        decrypted_data = unpadder.update(decrypted_padded) + unpadder.finalize()
        return decrypted_data.decode('utf-8')
    except Exception as e:
        print(f"Decryption error: {e}")
        return None

# ---------------------------------------------------------------------------
# Sensor Data
# ---------------------------------------------------------------------------

@app.route("/", methods=['GET'])
def hello():
    return 'hello ' + request.args.get('name', '')


@app.route("/stats", methods=['GET'])
def stats():
    ct = sensors.countDocuments()
    return 'total rows:' + str(ct)


@app.route('/sensorlist', methods=['GET'])
def sensorlist():
    d = sensors.distinct('node_id')
    return json.dumps(d)


@app.route("/sensor/<node>", methods=['GET'])
def sensor(node):
    skip = request.args.get('skip', '')
    type = request.args.get('type', '')
    period = request.args.get('period')
    try:
        skip = int(skip)
    except ValueError:
        skip = 0
    try:
        period = int(period) * 24
    except (ValueError, TypeError):
        period = 24
    return json.dumps(getdata(node, getstart(period), skip, type))


@app.route("/latest/<gw>", methods=['GET'])
def latest(gw):
    try:
        period = int(request.args.get('period', ''))
    except ValueError:
        period = 24
    return json.dumps(getlatest(gw, getstart(period)))


@app.route("/latests", methods=['GET'])
def latests():
    gateways = request.args.getlist('gw')
    try:
        period = int(request.args.get('period', ''))
    except ValueError:
        period = 1
    start = getstart(period)
    results = [{'gateway_id': gw, 'latest': getlatest(gw, start)} for gw in gateways]
    return json.dumps(results)


@app.route("/nodelist/<gw>", methods=['GET'])
def nodelist(gw):
    try:
        period = int(request.args.get('period')) * 24
    except TypeError:
        period = 24
    return json.dumps(sorted(getnodelist(gw, getstart(period))))


@app.route("/nodelists", methods=['GET'])
def nodelists():
    gateways = request.args.getlist('gw')
    try:
        period = int(request.args.get('period')) * 24
    except TypeError:
        period = 24
    start = getstart(period)
    results = [{'gateway_id': gw, 'nodes': getnodelist(gw, start)} for gw in gateways]
    return json.dumps(results)


@app.route("/gw/<gw>", methods=['GET'])
def gw_data(gw):
    nodes = request.args.getlist('node')
    type = request.args.get('type', '')
    timezone = request.args.get('timezone', 'None')
    try:
        period = int(request.args.get('period')) * 24
    except (ValueError, TypeError):
        period = 24
    return json.dumps(gwiteratenodes(gw, nodes, type, period, timezone))


def getnodelist(gw, start):
    qry = {'gateway_id': gw, 'time': {'$gte': start}}
    values = sensorsLatest.distinct('node_id', qry)
    return values


def getlatest(gw, start):
    docs = []
    qry = {'gateway_id': gw, 'time': {'$gte': int(start)}}
    sortparam = [('node_id', -1)]
    cursor = sensorsLatest.find(qry).sort(sortparam)
    for doc in cursor:
        docs.append({
            'node_id': doc['node_id'],
            'type': doc['type'],
            'gateway_id': gw,
            'value': cleanvalue(doc['value']),
            'time': doc['time'],
            'human_time': dt.datetime.fromtimestamp(doc['time']).strftime(timefmt),
        })
    return docs


def gwiteratenodes(gw, nodes, type, period, timezone):
    start = getstart(period)
    returndocs = []
    for node in nodes:
        print('calling getdatausinggw with', gw, node, start, type, timezone, dt.datetime.now())
        record = {
            'gateway_id': gw,
            'nodeID': node,
            'sensorData': getdatausinggw(gw, node, start, type, timezone),
        }
        returndocs.append(record)
    return returndocs


def getdatausinggw(gw, node, start, mytype, timezone):
    docs = []
    empty_results = {'results': '0'}

    try:
        toZone = gettz(timezone)
        fromZone = tzutc()
    except ValueError:
        print('Invalid timezone parameter %s. Defaulting to 0' % timezone)
        toZone = gettz('UTC')
        fromZone = tzutc()

    qry = {'gateway_id': gw, 'node_id': str(node), 'time': {'$gte': start}}
    sortparam = [('time', 1)]
    if mytype:
        qry['type'] = mytype

    resultsarray = list(sensors.find(qry).sort(sortparam).batch_size(100000))
    count = len(resultsarray)
   
    if count == 0:
        return empty_results

    ct = 0
    total = 0
    skip = 0
    if count > 300:
        skip = int(count / 300 + .49)
        print('Since more than 300 records were returned, skip is set to %i' % skip, dt.datetime.now())

    # Insert initial goalpost doc at start time
    newdoc = {'value': 0, 'human_time': '', 'time': 0}
    newdoc['human_time'] = dt.datetime.fromtimestamp(start).replace(tzinfo=fromZone).astimezone(toZone).strftime(timefmt)
    newdoc['time'] = start
    newdoc['value'] = cleanvalue(resultsarray[skip + 1]['value'])
    docs.append(newdoc)

    latestvalue = newdoc['value']
    for doc in resultsarray:
        total += 1
        ct += 1
        newdoc = {'value': 0, 'human_time': '', 'time': 0}
        if ct > skip:
            newvalue = cleanvalue(doc['value'])
            newdoc['value'] = newvalue
            latestvalue = newvalue
            newdoc['human_time'] = dt.datetime.fromtimestamp(doc['time']).replace(tzinfo=fromZone).astimezone(toZone).strftime(timefmt)
            newdoc['time'] = doc['time']
            docs.append(newdoc)
            ct = 0
    if ct != 0:
        newdoc['value'] = cleanvalue(doc['value'])
        newdoc['human_time'] = dt.datetime.fromtimestamp(doc['time']).replace(tzinfo=fromZone).astimezone(toZone).strftime(timefmt)
        newdoc['time'] = doc['time']
        docs.append(newdoc)

    # Insert final goalpost doc at current time
    now = dt.datetime.timestamp(dt.datetime.now())
    docs.append({'value': latestvalue, 'human_time': now, 'time': now})

    return docs


def getdata(node, start, skip, mytype):
    docs = []
    qry = {'node_id': node, 'time': {'$gte': start}}
    sortparam = [('time', -1)]
    if mytype:
        qry['type'] = mytype
    cursor = sensors.find(qry).sort(sortparam)
    ct = 0
    total = 0
    for doc in cursor:
        total += 1
        ct += 1
        if ct > skip:
            doc['_id'] = str(doc['_id'])
            doc['value'] = cleanvalue(doc['value'])
            doc['human_time'] = dt.datetime.fromtimestamp(doc['time']).strftime(timefmt)
            if 'iso_time' in doc:
                doc['iso_time'] = str(doc['iso_time'])
            docs.append(doc)
            ct = 0
    docs.insert(0, len(docs))
    return docs

# ---------------------------------------------------------------------------
# Nicknames
# ---------------------------------------------------------------------------

@app.route("/get_nicknames", methods=['GET'])
def get_nicknames():
    gateways = request.args.getlist('gw')
    returndoc = []
    for gateway in gateways:
        filt = {'gateway_id': gateway}
        node_rows = list(db.Nicknames.find(filt, {'_id': 0}).sort([('node_id', 1)]))
        nicknames_list = [
            {'node_id': r['node_id'], 'shortname': r['shortname'],
             'longname': r['longname'], 'seq_no': r['seq_no']}
            for r in node_rows
        ]
        gw_doc = db.GWNicknames.find_one(filt) or {}
        returndoc.append({
            'gateway_id': gateway,
            'longname': gw_doc.get('longname', ''),
            'seq_no': gw_doc.get('seq_no', 0),
            'nicknames': nicknames_list,
        })
    return json.dumps(returndoc)


@app.route("/save_nicknames", methods=['POST'])
def save_nicknames():
    for group in request.get_json():
        gw = group['gateway_id']
        gwLongname = group.get('longname', '')
        db.GWNicknames.update_one(
            {'gateway_id': gw},
            {'$set': {'gateway_id': gw, 'longname': gwLongname}, '$inc': {'seq_no': 1}},
            upsert=True)
        for item in group.get('nicknames', []):
            db.Nicknames.update_one(
                {'gateway_id': gw, 'node_id': item['nodeID']},
                {'$set': {
                    'gateway_id': gw, 'node_id': item['nodeID'],
                    'shortname': item['shortname'], 'longname': item['longname'],
                }, '$inc': {'seq_no': 1}},
                upsert=True)
    return 'OK'

# ---------------------------------------------------------------------------
# Third-Party Services (Sense Energy)
# ---------------------------------------------------------------------------

def _load_3p_services(logins):
    results = []
    for login in logins:
        for row in db.ThirdPartyServices.find({'login': login}).sort([('service_name', 1)]):
            results.append({
                'service_name': row['service_name'],
                'login': row['login'],
                'password': row['password'],
                'type': row['service_type'],
            })
    return results


@app.route("/add_3p_service", methods=['POST'])
def add_3p_service():
    conn_data = request.get_json()
    db.ThirdPartyServices.update_one(
        {'service_name': conn_data['service_name'], 'login': conn_data['login']},
        {'$set': {
            'service_name': conn_data['service_name'],
            'login': conn_data['login'],
            'password': conn_data['password'],
            'service_type': conn_data['service_type'],
        }},
        upsert=True)
    return 'OK'


@app.route("/get_3p_services", methods=['GET'])
def get_3p_services():
    logins = request.args.getlist('logins')
    return json.dumps(_load_3p_services(logins))


@app.route("/testsense", methods=['GET'])
def testsense():
    from sense_energy import Senseable
    login = request.args.get('login')
    key = request.args.get('key')
    svc = _load_3p_services([login])
    password = decrypt_password_aes(svc[0]['password'], key)
    sense = Senseable()
    sense.authenticate(login, password)
    sense.update_realtime()
    pwr = round(sense.active_power, 2)
    gw_name = '%s@%s' % (login, svc[0]['service_name'])
    now = dt.datetime.timestamp(dt.datetime.now())
    db.Sensors.insert_one({
        'model': svc[0]['type'], 'gateway_id': gw_name,
        'node_id': '0', 'type': 'PWR', 'value': str(pwr), 'time': now,
    })
    db.SensorsLatest.update_one(
        {'gateway_id': gw_name, 'node_id': '0', 'type': 'PWR'},
        {'$set': {
            'model': svc[0]['type'], 'gateway_id': gw_name,
            'node_id': '0', 'type': 'PWR', 'value': str(pwr), 'time': now,
        }},
        upsert=True)
    return json.dumps(pwr)

# ---------------------------------------------------------------------------
# User Profiles
# ---------------------------------------------------------------------------

@app.route("/user_profile", methods=['GET'])
@require_google_auth
def get_user_profile():
    email = g.user_email
    doc = db.UserProfiles.find_one({'email': email}, {'_id': 0})
    if doc is None:
        return json.dumps({}), 404
    return json.dumps(doc)


@app.route("/user_profile", methods=['POST'])
@require_google_auth
def save_user_profile():
    data = request.get_json() or {}
    email = g.user_email
    db.UserProfiles.update_one(
        {'email': email},
        {'$set': {
            'email': email,
            'gateway_ids': data.get('gateway_ids', []),
            'updated_at': dt.datetime.now(dt.timezone.utc).timestamp(),
        }},
        upsert=True)
    return 'OK'

# ---------------------------------------------------------------------------
# NOAA Weather Forecast
# ---------------------------------------------------------------------------

@app.route('/forecast/<gw>', methods=['GET'])
def forecast(gw):
    """Return NOAA forecast records for a gateway, sorted by time.

    Query params:
      node       — node_id to filter on (default: 'noaa_forecast')
      type       — sensor type to filter on (default: 'F')
      hours_back — also include historical records this many hours into the past
                   (default: 0, i.e. future only)
    """
    node = request.args.get('node', 'noaa_forecast')
    mytype = request.args.get('type', 'F')
    hours_back = int(request.args.get('hours_back', 0))
    now_ts = time.time()
    since_ts = now_ts - hours_back * 3600
    cursor = sensors.find(
        {'gateway_id': gw, 'node_id': node, 'time': {'$gt': since_ts}, 'type': mytype},
        sort=[('time', 1)],
    )
    docs = []
    for doc in cursor:
        try:
            docs.append({
                'node_id': doc['node_id'],
                'type': doc['type'],
                'gateway_id': doc['gateway_id'],
                'value': cleanvalue(str(doc.get('value', 0))),
                'time': doc['time'],
                'human_time': dt.datetime.fromtimestamp(doc['time']).strftime(timefmt),
            })
        except Exception:
            pass
    return json.dumps(docs)


@app.route('/noaa_settings', methods=['GET'])
@require_google_auth
def get_noaa_settings():
    """Return the NOAA settings for the authenticated user."""
    doc = noaaSettings.find_one({'email': g.user_email}, {'_id': 0})
    return json.dumps(doc or {})


@app.route('/noaa_settings', methods=['POST'])
@require_google_auth
def save_noaa_settings():
    """Create or update NOAA settings for the authenticated user."""
    data = request.get_json() or {}
    noaaSettings.update_one(
        {'email': g.user_email},
        {'$set': {
            'email': g.user_email,
            'lat': data.get('lat'),
            'lon': data.get('lon'),
            'gateway_id': data.get('gateway_id'),
            'outside_sensor_id': data.get('outside_sensor_id'),
            'enabled': data.get('enabled', True),
            'predictive_alerts_enabled': data.get('predictive_alerts_enabled', False),
            'frost_threshold': data.get('frost_threshold', 35.0),
            'heat_threshold': data.get('heat_threshold', 95.0),
            'baseline_forecast_alert_enabled': data.get('baseline_forecast_alert_enabled', False),
            'baseline_actual_alert_enabled': data.get('baseline_actual_alert_enabled', False),
        }},
        upsert=True,
    )
    return 'OK'


# ---------------------------------------------------------------------------
# Analytics Settings (anomaly detection, baseline, regression model toggles)
# ---------------------------------------------------------------------------

@app.route('/analytics_settings', methods=['GET'])
@require_google_auth
def get_analytics_settings():
    """Return the analytics settings for the authenticated user."""
    doc = db.AnalyticsSettings.find_one({'email': g.user_email}, {'_id': 0, 'email': 0})
    return json.dumps(doc or {})


@app.route('/analytics_settings', methods=['POST'])
@require_google_auth
def save_analytics_settings():
    """Create or update analytics settings for the authenticated user."""
    data = request.get_json(force=True) or {}
    allowed = {'anomaly_detection_enabled', 'anomaly_threshold',
                'baseline_enabled', 'regression_model_enabled'}
    updates = {k: v for k, v in data.items() if k in allowed}
    if updates:
        updates['email'] = g.user_email
        db.AnalyticsSettings.update_one(
            {'email': g.user_email}, {'$set': updates}, upsert=True)
    return 'OK'


# ---------------------------------------------------------------------------
# ML Anomaly Detection
# ---------------------------------------------------------------------------

# In-memory job registry (lives for the process lifetime).
_training_jobs: dict = {}


def _run_training(job_id: str, gateway_ids: list) -> None:
    """Background thread: train models for all nodes in all gateways."""
    all_results = []
    try:
        for gw_id in gateway_ids:
            results = _at.train_for_gateway(gw_id, db)
            all_results.extend(results)
        _training_jobs[job_id] = {'status': 'done', 'results': all_results}
        print(f'[train] Job {job_id} done: {len(all_results)} node(s) processed')
    except Exception as exc:
        _training_jobs[job_id] = {'status': 'failed', 'error': str(exc)}
        print(f'[train] Job {job_id} failed: {exc}')


@app.route('/train_anomaly_model', methods=['POST'])
def train_anomaly_model():
    data = request.get_json() or {}
    gateway_ids = data.get('gateway_ids', [])
    if not gateway_ids:
        return json.dumps({'error': 'gateway_ids required'}), 400

    job_id = str(uuid.uuid4())
    _training_jobs[job_id] = {'status': 'running', 'started_at': time.time()}

    t = threading.Thread(target=_run_training, args=(job_id, gateway_ids), daemon=True)
    t.start()

    return json.dumps({'job_id': job_id, 'status': 'started'})


@app.route('/training_status', methods=['GET'])
def training_status():
    job_id = request.args.get('job_id', '')
    job = _training_jobs.get(job_id)
    if job is None:
        return json.dumps({'error': 'unknown job_id'}), 404
    return json.dumps({'job_id': job_id, **job})


@app.route('/predict_anomaly', methods=['GET'])
def predict_anomaly():
    gateway_id = request.args.get('gateway_id', '')
    node_id    = request.args.get('node_id', '')
    try:
        period_days = int(request.args.get('period', '7'))
    except (ValueError, TypeError):
        period_days = 7

    if not gateway_id or not node_id:
        return json.dumps({'error': 'gateway_id and node_id required'}), 400

    if not _at.model_exists(gateway_id):
        return json.dumps({'error': 'no model trained yet for this gateway'}), 404

    try:
        model, metadata = _at.load_model(gateway_id)
    except FileNotFoundError:
        return json.dumps({'error': 'model files not found'}), 404

    feature_columns = metadata.get('feature_columns', [])

    # Build the same wide gateway DataFrame used at training time, for the
    # requested period. get_gateway_dataframe includes the 'time_rounded' column.
    gw_df = _at.get_gateway_dataframe(db, gateway_id, lookback_days=period_days)
    if gw_df is None or gw_df.empty:
        return json.dumps({'anomalous_timestamps': []})

    # Restrict to columns the model was trained on (ignore nodes added post-training)
    available = [c for c in feature_columns if c in gw_df.columns]
    pred_df = gw_df[['time_rounded'] + available].dropna(subset=available)

    anomalous_ts = _at.predict_anomalies(model, pred_df, feature_columns=available)

    # Filter to timestamps where the requested node has a temperature reading
    node_f_col = f'{node_id}_F'
    if node_f_col in pred_df.columns:
        node_ts = set(pred_df.loc[pred_df[node_f_col].notna(), 'time_rounded'].tolist())
        anomalous_ts = [ts for ts in anomalous_ts if ts in node_ts]

    return json.dumps({'anomalous_timestamps': anomalous_ts})


@app.route('/anomaly_model_status', methods=['GET'])
def anomaly_model_status():
    gateway_id = request.args.get('gateway_id', '')
    if not gateway_id:
        return json.dumps({'error': 'gateway_id required'}), 400

    meta_path = os.path.join(_at.MODELS_DIR, str(gateway_id), 'metadata.json')
    if not os.path.isfile(meta_path):
        return json.dumps({})

    with open(meta_path) as f:
        return json.dumps(json.load(f))


# ---------------------------------------------------------------------------
# Regression Forecasting
# ---------------------------------------------------------------------------

_regression_jobs: dict = {}


def _run_regression_training(job_id: str, gateway_ids: list) -> None:
    """Background thread: train regression models for all gateways."""
    all_results = []
    try:
        for gw_id in gateway_ids:
            results = _rt.train_regression_for_gateway(gw_id, db)
            all_results.extend(results)
        _regression_jobs[job_id] = {'status': 'done', 'results': all_results}
        print(f'[regression] Job {job_id} done: {len(all_results)} sensor(s) processed')
    except Exception as exc:
        _regression_jobs[job_id] = {'status': 'failed', 'error': str(exc)}
        print(f'[regression] Job {job_id} failed: {exc}')


@app.route('/train_regression_model', methods=['POST'])
def train_regression_model():
    data = request.get_json() or {}
    gateway_ids = data.get('gateway_ids', [])
    if not gateway_ids:
        return json.dumps({'error': 'gateway_ids required'}), 400

    job_id = str(uuid.uuid4())
    _regression_jobs[job_id] = {'status': 'running', 'started_at': time.time()}

    t = threading.Thread(target=_run_regression_training,
                         args=(job_id, gateway_ids), daemon=True)
    t.start()
    return json.dumps({'job_id': job_id, 'status': 'started'})


@app.route('/regression_training_status', methods=['GET'])
def regression_training_status():
    job_id = request.args.get('job_id', '')
    job = _regression_jobs.get(job_id)
    if job is None:
        return json.dumps({'error': 'unknown job_id'}), 404
    return json.dumps({'job_id': job_id, **job})


@app.route('/regression_model_status', methods=['GET'])
def regression_model_status():
    gateway_id = request.args.get('gateway_id', '')
    if not gateway_id:
        return json.dumps({'error': 'gateway_id required'}), 400
    metas = _rt.load_all_regression_metadata(gateway_id)
    return json.dumps({'models': metas})


@app.route('/regression_forecast', methods=['GET'])
def regression_forecast():
    gateway_id  = request.args.get('gateway_id', '')
    node_id     = request.args.get('node_id', '')
    sensor_type = request.args.get('type', 'F')
    try:
        hours = int(request.args.get('hours', '48'))
    except (ValueError, TypeError):
        hours = 48

    if not gateway_id or not node_id:
        return json.dumps({'error': 'gateway_id and node_id required'}), 400

    if not _rt.regression_model_exists(gateway_id, node_id, sensor_type):
        return json.dumps({'error': 'no regression model trained for this sensor'}), 404

    forecast = _rt.predict_sensor_forecast(
        gateway_id, node_id, sensor_type, db, hours=hours)
    return json.dumps({'forecast': forecast})


@app.route('/regression_forecast_history', methods=['GET'])
def regression_forecast_history():
    """Retrieve stored past forecasts for comparison with actuals.

    Query params:
        gateway_id (required)
        node_id (required)
        type (default 'F')
        hours_back (default 48) — how far back to look for recorded forecasts
    """
    gateway_id  = request.args.get('gateway_id', '')
    node_id     = request.args.get('node_id', '')
    sensor_type = request.args.get('type', 'F')
    try:
        hours_back = int(request.args.get('hours_back', '48'))
    except (ValueError, TypeError):
        hours_back = 48

    if not gateway_id or not node_id:
        return json.dumps({'error': 'gateway_id and node_id required'}), 400

    cutoff = time.time() - hours_back * 3600
    now = time.time()

    # Find forecasts that were *for* times in [cutoff, now] (past predictions
    # whose forecast_time has already passed, so we can compare with actuals)
    docs = list(db.RegressionForecasts.find(
        {
            'gateway_id': gateway_id,
            'node_id': node_id,
            'type': sensor_type,
            'forecast_time': {'$gte': cutoff, '$lte': now},
        },
        {'_id': 0, 'forecast_time': 1, 'predicted': 1,
         'recorded_at': 1, 'batch_id': 1},
    ).sort('forecast_time', 1))

    # For each forecast_time, keep only the most recent prediction (closest
    # recorded_at to forecast_time = most relevant forecast)
    best = {}
    for d in docs:
        ft = d['forecast_time']
        if ft not in best or d['recorded_at'] > best[ft]['recorded_at']:
            best[ft] = d

    result = [{'timestamp': d['forecast_time'], 'predicted': d['predicted']}
              for d in sorted(best.values(), key=lambda x: x['forecast_time'])]

    return json.dumps({'forecast_history': result})


# ---------------------------------------------------------------------------
# Alert Rules & Device Tokens
# ---------------------------------------------------------------------------

@app.route('/alert_rules', methods=['GET'])
@require_google_auth
def get_alert_rules():
    rules = list(db.AlertRules.find({'email': g.user_email}, {'_id': 0}))
    print(f'[AlertRules] GET — email={g.user_email}, found {len(rules)} rule(s)')
    return json.dumps(rules)


@app.route('/alert_rules', methods=['POST'])
@require_google_auth
def create_alert_rule():
    body = request.get_json(silent=True) or {}
    print(f'[AlertRules] POST — email={g.user_email}, body_keys={list(body.keys())}, '
          f'gateway_id={body.get("gateway_id")!r}, node_id={body.get("node_id")!r}, '
          f'type={body.get("type")!r}, operator={body.get("operator")!r}')
    body['email'] = g.user_email
    body['rule_id'] = str(uuid.uuid4())
    body.setdefault('enabled', True)
    body.setdefault('cooldown_minutes', 60)
    body.setdefault('push_enabled', True)
    body.pop('_id', None)
    db.AlertRules.insert_one(body)
    body.pop('_id', None)
    print(f'[AlertRules] POST — inserted rule_id={body.get("rule_id")}, label={body.get("label")!r}')
    return json.dumps(body), 201


@app.route('/alert_rules/<rule_id>', methods=['PUT'])
@require_google_auth
def update_alert_rule(rule_id):
    body = request.get_json(silent=True) or {}
    body.pop('_id', None)
    body.pop('rule_id', None)
    body.pop('email', None)
    result = db.AlertRules.update_one(
        {'rule_id': rule_id, 'email': g.user_email},
        {'$set': body},
    )
    if result.matched_count == 0:
        return json.dumps({'error': 'not found or not yours'}), 404
    return json.dumps({'updated': True})


@app.route('/alert_rules/<rule_id>', methods=['DELETE'])
@require_google_auth
def delete_alert_rule(rule_id):
    result = db.AlertRules.delete_one(
        {'rule_id': rule_id, 'email': g.user_email}
    )
    if result.deleted_count == 0:
        return json.dumps({'error': 'not found or not yours'}), 404
    return json.dumps({'deleted': True})


@app.route('/device_token', methods=['POST'])
@require_google_auth
def register_device_token():
    body = request.get_json(silent=True) or {}
    token    = body.get('token', '')
    platform = body.get('platform', 'ios')
    if not token:
        return json.dumps({'error': 'token required'}), 400
    db.DeviceTokens.update_one(
        {'email': g.user_email, 'platform': platform},
        {'$set': {'token': token, 'updated_at': int(dt.datetime.utcnow().timestamp())}},
        upsert=True,
    )
    return json.dumps({'registered': True})


# ---------------------------------------------------------------------------
# Baseline Learning
# ---------------------------------------------------------------------------

@app.route('/compute_baseline', methods=['POST'])
def compute_baseline():
    body = request.get_json(silent=True) or {}
    gateway_id = body.get('gateway_id', '')
    node_id    = body.get('node_id', '')
    sensor_type = body.get('type', 'F')
    days       = int(body.get('days', 30))

    if not gateway_id or not node_id:
        return json.dumps({'error': 'gateway_id and node_id required'}), 400

    cutoff_unix = int(dt.datetime.utcnow().timestamp()) - days * 86400

    # Some older records have value stored as b'39.61' (Python bytes repr).
    # $trim strips the b and ' characters from both ends before conversion;
    # $convert onError:null silently skips any remaining unparseable values.
    _safe_val = {'$convert': {
        'input': {'$trim': {'input': '$value', 'chars': "b'"}},
        'to': 'double', 'onError': None,
    }}

    pipeline = [
        {'$match': {
            'gateway_id': gateway_id,
            'node_id':    node_id,
            'type':       sensor_type,
            'time':       {'$gte': cutoff_unix},
        }},
        {'$group': {
            '_id': {
                'hour':        {'$hour': {'$toDate': {'$multiply': [{'$toDouble': '$time'}, 1000]}}},
                'day_of_week': {'$dayOfWeek': {'$toDate': {'$multiply': [{'$toDouble': '$time'}, 1000]}}},
            },
            'mean':  {'$avg': _safe_val},
            'std':   {'$stdDevSamp': _safe_val},
            'count': {'$sum': 1},
        }},
    ]

    cursor = sensors.aggregate(pipeline)
    computed_at = int(dt.datetime.utcnow().timestamp())
    bucket_count = 0

    for doc in cursor:
        hour = doc['_id']['hour']
        dow  = doc['_id']['day_of_week']
        db.Baselines.update_one(
            {'gateway_id': gateway_id, 'node_id': node_id, 'type': sensor_type,
             'hour': hour, 'day_of_week': dow},
            {'$set': {
                'mean': doc['mean'],
                'std':  doc['std'] or 0.0,
                'count': doc['count'],
                'computed_at': computed_at,
            }},
            upsert=True,
        )
        bucket_count += 1

    return json.dumps({'bucket_count': bucket_count, 'computed_at': computed_at})


@app.route('/baseline/<gw>', methods=['GET'])
def get_baseline(gw):
    node = request.args.get('node', '')
    sensor_type = request.args.get('type', 'F')
    if not node:
        return json.dumps({'error': 'node parameter required'}), 400

    cursor = db.Baselines.find(
        {'gateway_id': gw, 'node_id': node, 'type': sensor_type},
        {'_id': 0, 'gateway_id': 0, 'node_id': 0, 'type': 0},
    )
    return json.dumps(list(cursor))


@app.route('/baseline_status/<gw>', methods=['GET'])
def baseline_status(gw):
    node = request.args.get('node', '')
    sensor_type = request.args.get('type', 'F')
    if not node:
        # Gateway-level check: does any baseline exist for this gateway?
        doc = db.Baselines.find_one(
            {'gateway_id': gw},
            {'computed_at': 1, '_id': 0},
            sort=[('computed_at', -1)],
        )
        if not doc:
            return json.dumps({'exists': False})
        return json.dumps({'exists': True, 'computed_at': doc.get('computed_at')})

    doc = db.Baselines.find_one(
        {'gateway_id': gw, 'node_id': node, 'type': sensor_type},
        {'computed_at': 1, '_id': 0},
    )
    if not doc:
        return json.dumps({})

    count = db.Baselines.count_documents(
        {'gateway_id': gw, 'node_id': node, 'type': sensor_type}
    )
    return json.dumps({'computed_at': doc['computed_at'], 'bucket_count': count})


# ---------------------------------------------------------------------------
# Heatmap / Calendar View
# ---------------------------------------------------------------------------

@app.route('/heatmap/<gw>', methods=['GET'])
def heatmap(gw):
    node = request.args.get('node', '')
    sensor_type = request.args.get('type', 'F')
    try:
        year = int(request.args.get('year', dt.datetime.utcnow().year))
    except ValueError:
        return json.dumps({'error': 'year must be an integer'}), 400

    if not node:
        return json.dumps({'error': 'node parameter required'}), 400

    year_start = dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc)
    year_end   = dt.datetime(year + 1, 1, 1, tzinfo=dt.timezone.utc)
    year_start_unix = int(year_start.timestamp())
    year_end_unix   = int(year_end.timestamp())

    _safe_val = {'$convert': {
        'input': {'$trim': {'input': '$value', 'chars': "b'"}},
        'to': 'double', 'onError': None,
    }}

    pipeline = [
        {'$match': {
            'gateway_id': gw,
            'node_id':    node,
            'type':       sensor_type,
            'time':       {'$gte': year_start_unix, '$lt': year_end_unix},
        }},
        {'$group': {
            '_id': {'$dateToString': {
                'format': '%Y-%m-%d',
                'date': {'$toDate': {'$multiply': [{'$toDouble': '$time'}, 1000]}},
            }},
            'min':   {'$min': _safe_val},
            'max':   {'$max': _safe_val},
            'avg':   {'$avg': _safe_val},
            'count': {'$sum': 1},
        }},
        {'$sort': {'_id': 1}},
    ]

    cursor = sensors.aggregate(pipeline)
    results = [
        {'date': doc['_id'], 'min': doc['min'], 'max': doc['max'],
         'avg': doc['avg'], 'count': doc['count']}
        for doc in cursor
    ]
    return json.dumps(results)


# ---------------------------------------------------------------------------
# Google Home
# ---------------------------------------------------------------------------

@app.route('/google-home/sync', methods=['POST'])
def google_home_sync():
    import requests, os
    user_id = (request.get_json(silent=True) or {}).get('userId') or request.form.get('userId')
    if not user_id:
        return json.dumps({'error': 'userId required'}), 400
    api_key = os.getenv('GOOGLE_HOMEGRAPH_API_KEY')
    if not api_key:
        return json.dumps({'error': 'HomeGraph API key not configured'}), 500
    resp = requests.post(
        'https://homegraph.googleapis.com/v1/devices:requestSync',
        params={'key': api_key},
        json={'agentUserId': user_id}
    )
    if resp.status_code == 200:
        return json.dumps({'success': True}), 200
    return json.dumps({'error': 'sync failed', 'details': resp.text}), resp.status_code

# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050, debug=True)
