"""
pytest configuration for SensorIoT-REST_server tests.

mongomock patches pymongo.MongoClient *before* server.py is imported, because
server.py calls MongoClient at module level.  The patcher is started here at
conftest.py module-load time, which pytest guarantees happens before any test
file is collected or imported.
"""
import os
import sys

# Make server.py and sibling modules importable from the tests/ subdirectory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mongomock
import pytest

# ── Activate mongomock before server is imported ─────────────────────────────
_mongo_patcher = mongomock.patch(servers=(('localhost', 27017),))
_mongo_patcher.start()

import server as _server  # noqa: E402  (intentionally after patch)

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope='session')
def app():
    _server.app.config['TESTING'] = True
    return _server.app


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


# ── Reset in-memory OAuth / device state between tests ───────────────────────

@pytest.fixture(autouse=True)
def reset_app_state():
    """Clear OAuth dicts and reset mock device state before every test."""
    import app_state
    app_state.OAUTH_CODES.clear()
    app_state.OAUTH_TOKENS.clear()
    app_state.MOCK_DEVICES['device_1']['state']['on'] = False
    yield
    app_state.OAUTH_CODES.clear()
    app_state.OAUTH_TOKENS.clear()


# ── Database seed / teardown ──────────────────────────────────────────────────

SEED_TS = 1_708_643_284.0  # fixed timestamp for deterministic assertions


@pytest.fixture
def seed_db():
    """Insert representative documents and clean up afterward."""
    _server.userProfiles.insert_one({'email': 'test@example.com', 'gateway_ids': ['GW-TEST']})
    _server.sensorsLatest.insert_many([
        {
            'gateway_id': 'GW-TEST',
            'node_id': 'node_1',
            'type': 'F',
            'model': 'DHT22',
            'value': '72.5',
            'time': SEED_TS,
        },
        {
            'gateway_id': 'GW-TEST',
            'node_id': 'node_1',
            'type': 'H',
            'model': 'DHT22',
            'value': '45.0',
            'time': SEED_TS,
        },
    ])
    _server.sensors.insert_many([
        {
            'gateway_id': 'GW-TEST',
            'node_id': 'node_1',
            'type': 'F',
            'model': 'DHT22',
            'value': '72.5',
            'time': SEED_TS,
        },
    ])
    yield
    # Teardown: wipe all collections used by tests
    _server.sensors.drop()
    _server.sensorsLatest.drop()
    _server.db['Nicknames'].drop()
    _server.db['GWNicknames'].drop()
    _server.userProfiles.drop()
    _server.db['ThirdPartyServices'].drop()
