import os
from flask import Blueprint, request, jsonify, render_template_string, redirect
import uuid
import time
from app_state import OAUTH_CODES, OAUTH_TOKENS

auth_bp = Blueprint('auth', __name__)

EXPECTED_CLIENT_ID = os.getenv('GOOGLE_HOME_CLIENT_ID')
EXPECTED_CLIENT_SECRET = os.getenv('GOOGLE_HOME_CLIENT_SECRET')


@auth_bp.route('/auth', methods=['GET', 'POST'])
def authorize():
    """
    Google Home app directs the user here to link their account.
    """
    client_id = request.args.get('client_id')
    redirect_uri = request.args.get('redirect_uri')
    state = request.args.get('state')

    if request.method == 'GET':
        if EXPECTED_CLIENT_ID and client_id != EXPECTED_CLIENT_ID:
            return jsonify({"error": "invalid_client"}), 400

        return render_template_string('''
            <h2>SensorIoT Account Linking</h2>
            <form method="POST">
                <p>Enter your SensorIoT account email to link with Google Home.</p>
                <input type="email" name="email" placeholder="your@email.com" required
                       style="display:block;margin-bottom:8px;padding:6px;width:260px;">
                <input type="hidden" name="client_id" value="{{ client_id }}">
                <input type="hidden" name="redirect_uri" value="{{ redirect_uri }}">
                <input type="hidden" name="state" value="{{ state }}">
                <button type="submit">Approve Linking</button>
            </form>
        ''', client_id=client_id, redirect_uri=redirect_uri, state=state)

    elif request.method == 'POST':
        auth_code = str(uuid.uuid4())

        OAUTH_CODES[auth_code] = {
            "client_id": request.form.get('client_id'),
            "user_id": request.form.get('email', 'unknown')
        }

        callback_url = f"{request.form.get('redirect_uri')}?code={auth_code}&state={request.form.get('state')}"
        return redirect(callback_url)


@auth_bp.route('/token', methods=['POST'])
def token():
    """
    Google exchanges the authorization code for an access token.
    """
    client_id = request.form.get('client_id')
    client_secret = request.form.get('client_secret')

    if EXPECTED_CLIENT_ID and client_id != EXPECTED_CLIENT_ID:
        return jsonify({"error": "invalid_client"}), 400
    if EXPECTED_CLIENT_SECRET and client_secret != EXPECTED_CLIENT_SECRET:
        return jsonify({"error": "invalid_client"}), 400

    grant_type = request.form.get('grant_type')

    if grant_type == 'authorization_code':
        code = request.form.get('code')
        if code in OAUTH_CODES:
            access_token = str(uuid.uuid4())
            refresh_token = str(uuid.uuid4())

            OAUTH_TOKENS[access_token] = {
                "user_id": OAUTH_CODES[code]["user_id"],
                "expires_at": time.time() + 3600
            }

            return jsonify({
                "token_type": "Bearer",
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_in": 3600
            })
    elif grant_type == 'refresh_token':
        access_token = str(uuid.uuid4())
        OAUTH_TOKENS[access_token] = {
            "user_id": "unknown",
            "expires_at": time.time() + 3600
        }
        return jsonify({
            "token_type": "Bearer",
            "access_token": access_token,
            "expires_in": 3600
        })

    return jsonify({"error": "invalid_grant"}), 400
