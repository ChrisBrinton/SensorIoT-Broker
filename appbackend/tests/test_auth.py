"""Tests for the OAuth auth blueprint (/auth, /token)."""
import json
from urllib.parse import parse_qs, urlparse

import pytest

import app_state


# ── Helpers ───────────────────────────────────────────────────────────────────

def _post_auth(client, redirect_uri='https://example.com/cb', state='s'):
    return client.post('/auth', data={
        'client_id': 'test-client',
        'redirect_uri': redirect_uri,
        'state': state,
    })


def _get_code(client, **kwargs):
    resp = _post_auth(client, **kwargs)
    location = resp.headers['Location']
    return parse_qs(urlparse(location).query)['code'][0]


# ── GET /auth ─────────────────────────────────────────────────────────────────

class TestAuthGet:
    def test_returns_200(self, client):
        resp = client.get('/auth?client_id=c&redirect_uri=https://example.com/cb&state=s')
        assert resp.status_code == 200

    def test_contains_approve_button(self, client):
        resp = client.get('/auth?client_id=c&redirect_uri=https://example.com/cb&state=s')
        assert b'Approve Linking' in resp.data

    def test_embeds_client_id_in_form(self, client):
        resp = client.get('/auth?client_id=MY-CLIENT&redirect_uri=https://example.com/cb&state=s')
        assert b'MY-CLIENT' in resp.data

    def test_embeds_state_in_form(self, client):
        resp = client.get('/auth?client_id=c&redirect_uri=https://example.com/cb&state=MYSTATE')
        assert b'MYSTATE' in resp.data


# ── POST /auth ────────────────────────────────────────────────────────────────

class TestAuthPost:
    def test_redirects(self, client):
        assert _post_auth(client).status_code == 302

    def test_redirect_contains_code_param(self, client):
        location = _post_auth(client).headers['Location']
        params = parse_qs(urlparse(location).query)
        assert 'code' in params
        assert len(params['code'][0]) > 0

    def test_redirect_contains_state(self, client):
        location = _post_auth(client, state='unique-42').headers['Location']
        assert parse_qs(urlparse(location).query)['state'][0] == 'unique-42'

    def test_redirect_points_to_redirect_uri(self, client):
        location = _post_auth(client, redirect_uri='https://myapp.com/callback').headers['Location']
        assert location.startswith('https://myapp.com/callback')

    def test_code_stored_in_oauth_codes(self, client):
        code = _get_code(client)
        assert code in app_state.OAUTH_CODES

    def test_stored_code_has_correct_client_id(self, client):
        resp = client.post('/auth', data={
            'client_id': 'special-client',
            'redirect_uri': 'https://example.com/cb',
            'state': 's',
        })
        code = parse_qs(urlparse(resp.headers['Location']).query)['code'][0]
        assert app_state.OAUTH_CODES[code]['client_id'] == 'special-client'

    def test_different_requests_produce_different_codes(self, client):
        code1 = _get_code(client)
        app_state.OAUTH_CODES.clear()  # reset so second code is fresh
        code2 = _get_code(client)
        assert code1 != code2


# ── POST /token ───────────────────────────────────────────────────────────────

class TestToken:
    def test_authorization_code_grant_returns_200(self, client):
        code = _get_code(client)
        resp = client.post('/token', data={'grant_type': 'authorization_code', 'code': code})
        assert resp.status_code == 200

    def test_authorization_code_grant_returns_access_token(self, client):
        code = _get_code(client)
        data = json.loads(client.post('/token', data={'grant_type': 'authorization_code', 'code': code}).data)
        assert 'access_token' in data
        assert len(data['access_token']) > 0

    def test_authorization_code_grant_returns_refresh_token(self, client):
        code = _get_code(client)
        data = json.loads(client.post('/token', data={'grant_type': 'authorization_code', 'code': code}).data)
        assert 'refresh_token' in data

    def test_authorization_code_grant_token_type_is_bearer(self, client):
        code = _get_code(client)
        data = json.loads(client.post('/token', data={'grant_type': 'authorization_code', 'code': code}).data)
        assert data['token_type'] == 'Bearer'

    def test_authorization_code_grant_expires_in_3600(self, client):
        code = _get_code(client)
        data = json.loads(client.post('/token', data={'grant_type': 'authorization_code', 'code': code}).data)
        assert data['expires_in'] == 3600

    def test_authorization_code_stores_access_token(self, client):
        code = _get_code(client)
        data = json.loads(client.post('/token', data={'grant_type': 'authorization_code', 'code': code}).data)
        assert data['access_token'] in app_state.OAUTH_TOKENS

    def test_refresh_token_grant_returns_200(self, client):
        resp = client.post('/token', data={'grant_type': 'refresh_token', 'refresh_token': 'any'})
        assert resp.status_code == 200

    def test_refresh_token_grant_returns_new_access_token(self, client):
        data = json.loads(
            client.post('/token', data={'grant_type': 'refresh_token', 'refresh_token': 'any'}).data
        )
        assert 'access_token' in data
        assert data['token_type'] == 'Bearer'

    def test_invalid_code_returns_400(self, client):
        resp = client.post('/token', data={'grant_type': 'authorization_code', 'code': 'bad-code'})
        assert resp.status_code == 400

    def test_unknown_grant_type_returns_400(self, client):
        resp = client.post('/token', data={'grant_type': 'client_credentials'})
        assert resp.status_code == 400
