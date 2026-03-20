OAUTH_CODES = {}
OAUTH_TOKENS = {}

# Basic mock database for demonstration
MOCK_USERS = {
    "test_user_id": {
        "email": "test@example.com"
    }
}
MOCK_DEVICES = {
    "device_1": {
        "id": "device_1",
        "type": "action.devices.types.OUTLET",
        "traits": [
            "action.devices.traits.OnOff"
        ],
        "name": {
            "name": "Smart Plug"
        },
        "willReportState": False,
        "state": {
            "on": False,
            "online": True
        }
    }
}

# MongoDB collection references — injected by server.py at startup
sensors_latest = None
user_profiles = None
nicknames_col = None
