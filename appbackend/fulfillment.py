import time
import app_state
from flask import Blueprint, request, jsonify
from app_state import OAUTH_TOKENS

fulfillment_bp = Blueprint('fulfillment', __name__)


def verify_token(auth_header):
    if not auth_header or not auth_header.startswith('Bearer '):
        return None
    token = auth_header.split(' ')[1]
    token_info = OAUTH_TOKENS.get(token)
    if not token_info:
        return None
    if time.time() > token_info.get('expires_at', 0):
        return None
    return token_info


@fulfillment_bp.route('/fulfillment/test', methods=['GET'])
def fulfillment_test():
    return jsonify({"status": "ok", "message": "Fulfillment route is accessible."})


@fulfillment_bp.route('/fulfillment', methods=['POST'])
def fulfillment():
    """
    The main webhook for Google Smart Home Action.
    Receives SYNC, QUERY, and EXECUTE intents.
    """
    auth_header = request.headers.get('Authorization')
    user_info = verify_token(auth_header)

    if not user_info:
        return jsonify({"error": "invalid_grant"}), 401

    user_id = user_info.get('user_id')

    body = request.get_json()
    intent = body['inputs'][0]['intent']
    request_id = body.get('requestId')

    print(f"Received Google Home intent: {intent} for user: {user_id}")

    if intent == 'action.devices.SYNC':
        return handle_sync(request_id, user_id)
    elif intent == 'action.devices.QUERY':
        return handle_query(request_id, body['inputs'][0]['payload'])
    elif intent == 'action.devices.EXECUTE':
        return handle_execute(request_id, body['inputs'][0]['payload'])

    return jsonify({"error": "unknown_intent"}), 400


# Supported sensor types and their Google Home metadata
_TYPE_META = {
    'F': ('Temperature', 'CELSIUS', 'Temp Sensor'),
    'H': ('Humidity', 'PERCENTAGE', 'Humidity Sensor'),
}


def _doc_to_gh_device(doc, nick_map):
    """Convert a SensorsLatest document to a Google Home device descriptor."""
    meta = _TYPE_META.get(doc['type'])
    if not meta:
        return None
    sensor_name, unit, default_label = meta
    device_id = f"{doc['gateway_id']}/{doc['node_id']}/{doc['type']}"
    display_name = nick_map.get((doc['gateway_id'], doc['node_id']), f"{doc['node_id']} {default_label}")
    return {
        "id": device_id,
        "type": "action.devices.types.SENSOR",
        "traits": ["action.devices.traits.SensorState"],
        "name": {"name": display_name},
        "willReportState": False,
        "attributes": {
            "sensorStatesSupported": [{
                "name": sensor_name,
                "numericCapabilities": {"rawValueUnit": unit}
            }]
        }
    }


def handle_sync(request_id, agent_user_id):
    # Look up which gateways belong to this user
    profile = None
    if app_state.user_profiles is not None:
        profile = app_state.user_profiles.find_one({'email': agent_user_id}, {'_id': 0})
    gateway_ids = profile.get('gateway_ids', []) if profile else []

    # Fetch nicknames for human-readable display names
    nick_map = {}
    if app_state.nicknames_col is not None:
        for n in app_state.nicknames_col.find({'gateway_id': {'$in': gateway_ids}}, {'_id': 0}):
            nick_map[(n['gateway_id'], n['node_id'])] = n.get('longname') or n.get('shortname')

    # Build device list from latest sensor readings (temperature and humidity only)
    devices = []
    if app_state.sensors_latest is not None:
        supported_types = list(_TYPE_META.keys())
        for doc in app_state.sensors_latest.find(
            {'gateway_id': {'$in': gateway_ids}, 'type': {'$in': supported_types}}
        ):
            device = _doc_to_gh_device(doc, nick_map)
            if device:
                devices.append(device)

    return jsonify({
        "requestId": request_id,
        "payload": {
            "agentUserId": agent_user_id,
            "devices": devices
        }
    })


def handle_query(request_id, payload):
    device_states = {}
    for device in payload.get('devices', []):
        device_id = device['id']
        parts = device_id.split('/')
        if len(parts) == 3 and app_state.sensors_latest is not None:
            gw, node, typ = parts
            doc = app_state.sensors_latest.find_one(
                {'gateway_id': gw, 'node_id': node, 'type': typ}, {'_id': 0}
            )
            if doc and typ in _TYPE_META:
                raw = float(doc['value'].replace('b', '').replace('v', '').replace("'", ''))
                if typ == 'F':
                    raw = round((raw - 32) * 5 / 9, 1)  # convert °F → °C for Google Home
                sensor_name = _TYPE_META[typ][0]
                device_states[device_id] = {
                    "online": True,
                    "currentSensorStateData": [{"name": sensor_name, "rawValue": raw}]
                }

    return jsonify({"requestId": request_id, "payload": {"devices": device_states}})


def handle_execute(request_id, payload):
    commands = []

    for command in payload.get('commands', []):
        for device in command.get('devices', []):
            device_id = device['id']
            if device_id in app_state.MOCK_DEVICES:
                for execution in command.get('execution', []):
                    if execution['command'] == 'action.devices.commands.OnOff':
                        new_state = execution['params']['on']
                        app_state.MOCK_DEVICES[device_id]['state']['on'] = new_state
                        print(f"Device {device_id} turned {'On' if new_state else 'Off'}")

                commands.append({
                    "ids": [device_id],
                    "status": "SUCCESS",
                    "states": app_state.MOCK_DEVICES[device_id]['state']
                })
            else:
                commands.append({
                    "ids": [device_id],
                    "status": "ERROR",
                    "errorCode": "deviceNotFound"
                })

    return jsonify({
        "requestId": request_id,
        "payload": {
            "commands": commands
        }
    })
