"""Integration tests for REST API endpoints (Flask test client + mongomock)."""
import json
from unittest.mock import patch

import pytest


# ── GET / ─────────────────────────────────────────────────────────────────────

class TestHello:
    def test_hello_with_name(self, client):
        resp = client.get('/?name=World')
        assert resp.status_code == 200
        assert b'hello World' in resp.data

    def test_hello_without_name(self, client):
        resp = client.get('/')
        assert resp.status_code == 200
        assert b'hello' in resp.data


# ── GET /latest/<gw> ──────────────────────────────────────────────────────────

class TestLatest:
    def test_returns_list(self, client, seed_db):
        resp = client.get('/latest/GW-TEST')
        assert resp.status_code == 200
        assert isinstance(json.loads(resp.data), list)

    def test_returns_seeded_readings(self, client, seed_db):
        data = json.loads(client.get('/latest/GW-TEST').data)
        assert len(data) >= 1
        assert any(d['node_id'] == 'node_1' for d in data)

    def test_doc_has_required_keys(self, client, seed_db):
        data = json.loads(client.get('/latest/GW-TEST').data)
        for doc in data:
            for key in ('node_id', 'type', 'gateway_id', 'value', 'time', 'human_time'):
                assert key in doc, f'Missing key: {key}'

    def test_value_is_float(self, client, seed_db):
        data = json.loads(client.get('/latest/GW-TEST').data)
        for doc in data:
            assert isinstance(doc['value'], float)

    def test_unknown_gateway_returns_empty_list(self, client):
        data = json.loads(client.get('/latest/UNKNOWN-GW').data)
        assert data == []


# ── GET /sensor/<node> ────────────────────────────────────────────────────────

class TestSensor:
    def test_returns_list(self, client, seed_db):
        resp = client.get('/sensor/node_1?period=1&type=F')
        assert resp.status_code == 200
        assert isinstance(json.loads(resp.data), list)

    def test_invalid_skip_defaults_to_zero(self, client, seed_db):
        assert client.get('/sensor/node_1?skip=abc').status_code == 200

    def test_invalid_period_defaults_to_24h(self, client, seed_db):
        assert client.get('/sensor/node_1?period=notanumber').status_code == 200


# ── GET /nodelist/<gw> ────────────────────────────────────────────────────────

class TestNodelist:
    def test_returns_sorted_list(self, client, seed_db):
        data = json.loads(client.get('/nodelist/GW-TEST').data)
        assert isinstance(data, list)
        assert data == sorted(data)

    def test_unknown_gateway_returns_empty_list(self, client):
        data = json.loads(client.get('/nodelist/UNKNOWN').data)
        assert data == []


# ── GET /nodelists ────────────────────────────────────────────────────────────

class TestNodelists:
    def test_returns_list_of_gateway_dicts(self, client, seed_db):
        data = json.loads(client.get('/nodelists?gw=GW-TEST&gw=UNKNOWN').data)
        assert len(data) == 2
        gw_ids = [d['gateway_id'] for d in data]
        assert 'GW-TEST' in gw_ids
        assert 'UNKNOWN' in gw_ids


# ── GET & POST /user_profile ─────────────────────────────────────────────────

class TestUserProfile:
    TEST_EMAIL = 'test@example.com'
    AUTH_HEADERS = {'Authorization': 'Bearer fake_token'}

    @pytest.fixture(autouse=True)
    def mock_token_verification(self):
        """Patch _verify_google_token so tests don't hit Google's servers."""
        with patch('server._verify_google_token', return_value=self.TEST_EMAIL):
            yield

    @pytest.fixture(autouse=True)
    def clean_profiles(self):
        import server as _server
        yield
        _server.db.UserProfiles.drop()

    def test_post_creates_profile(self, client):
        resp = client.post(
            '/user_profile',
            data=json.dumps({'gateway_ids': ['GW-1']}),
            content_type='application/json',
            headers=self.AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert b'OK' in resp.data

    def test_get_returns_created_profile(self, client):
        client.post(
            '/user_profile',
            data=json.dumps({'gateway_ids': ['GW-A']}),
            content_type='application/json',
            headers=self.AUTH_HEADERS,
        )
        data = json.loads(client.get('/user_profile', headers=self.AUTH_HEADERS).data)
        assert data['email'] == self.TEST_EMAIL
        assert 'GW-A' in data['gateway_ids']

    def test_get_unknown_returns_404(self, client):
        # No profile created for this token's email → 404
        assert client.get('/user_profile', headers=self.AUTH_HEADERS).status_code == 404

    def test_missing_auth_header_returns_401(self, client):
        assert client.get('/user_profile').status_code == 401

    def test_invalid_token_returns_401(self, client):
        with patch('server._verify_google_token', return_value=None):
            resp = client.get('/user_profile', headers={'Authorization': 'Bearer bad_token'})
            assert resp.status_code == 401

    def test_post_updates_existing_profile(self, client):
        for gw in (['GW-OLD'], ['GW-NEW']):
            client.post(
                '/user_profile',
                data=json.dumps({'gateway_ids': gw}),
                content_type='application/json',
                headers=self.AUTH_HEADERS,
            )
        data = json.loads(client.get('/user_profile', headers=self.AUTH_HEADERS).data)
        assert data['gateway_ids'] == ['GW-NEW']


# ── GET /get_nicknames  POST /save_nicknames ──────────────────────────────────

class TestNicknames:
    def test_save_and_retrieve(self, client):
        payload = [{
            'gateway_id': 'GW-TEST',
            'longname': 'Test Gateway',
            'nicknames': [
                {'nodeID': 'node_1', 'shortname': 'S1', 'longname': 'Living Room', 'seq_no': 1},
            ],
        }]
        assert client.post(
            '/save_nicknames',
            data=json.dumps(payload),
            content_type='application/json',
        ).status_code == 200

        data = json.loads(client.get('/get_nicknames?gw=GW-TEST').data)
        assert data[0]['gateway_id'] == 'GW-TEST'
        assert data[0]['longname'] == 'Test Gateway'
        assert any(n['node_id'] == 'node_1' for n in data[0]['nicknames'])

    def test_get_unknown_gateway_has_empty_nicknames(self, client):
        data = json.loads(client.get('/get_nicknames?gw=UNKNOWN').data)
        assert data[0]['nicknames'] == []

    def test_save_increments_seq_no(self, client):
        payload = [{'gateway_id': 'GW-INC', 'longname': 'GW', 'nicknames': []}]
        client.post('/save_nicknames', data=json.dumps(payload), content_type='application/json')
        client.post('/save_nicknames', data=json.dumps(payload), content_type='application/json')
        data = json.loads(client.get('/get_nicknames?gw=GW-INC').data)
        assert data[0]['seq_no'] == 2


# ── POST /add_3p_service  GET /get_3p_services ────────────────────────────────

class TestThirdPartyServices:
    def _add(self, client, login='user@test.com'):
        return client.post(
            '/add_3p_service',
            data=json.dumps({
                'service_name': 'Sense',
                'login': login,
                'password': 'enc_pw',
                'service_type': 'timeseries',
            }),
            content_type='application/json',
        )

    def test_add_returns_ok(self, client):
        assert self._add(client).status_code == 200

    def test_retrieve_added_service(self, client):
        self._add(client, 'user@test.com')
        data = json.loads(client.get('/get_3p_services?logins=user@test.com').data)
        assert len(data) == 1
        assert data[0]['service_name'] == 'Sense'
        assert data[0]['password'] == 'enc_pw'

    def test_unknown_login_returns_empty(self, client):
        data = json.loads(client.get('/get_3p_services?logins=nobody@test.com').data)
        assert data == []

    def test_upsert_overwrites_password(self, client):
        self._add(client, 'u@test.com')
        client.post(
            '/add_3p_service',
            data=json.dumps({
                'service_name': 'Sense',
                'login': 'u@test.com',
                'password': 'new_enc_pw',
                'service_type': 'timeseries',
            }),
            content_type='application/json',
        )
        data = json.loads(client.get('/get_3p_services?logins=u@test.com').data)
        assert len(data) == 1
        assert data[0]['password'] == 'new_enc_pw'
