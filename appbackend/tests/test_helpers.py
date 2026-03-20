"""Unit tests for pure helper functions in server.py."""
import base64
import os
import time

import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from server import cleanvalue, decrypt_password_aes, getstart


# ── Helpers ───────────────────────────────────────────────────────────────────

def _encrypt(plaintext: str, key: bytes) -> tuple[str, str]:
    """AES-256-CBC encrypt and return (enc_b64, key_b64)."""
    iv = os.urandom(16)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext.encode()) + padder.finalize()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    enc_b64 = base64.urlsafe_b64encode(iv + ciphertext).decode()
    key_b64 = base64.urlsafe_b64encode(key).decode()
    return enc_b64, key_b64


# ── cleanvalue ────────────────────────────────────────────────────────────────

class TestCleanvalue:
    def test_plain_float_string(self):
        assert cleanvalue('72.5') == 72.5

    def test_strips_b_prefix(self):
        assert cleanvalue('b72.5') == 72.5

    def test_strips_v_prefix(self):
        assert cleanvalue('v72.5') == 72.5

    def test_strips_single_quotes(self):
        assert cleanvalue("'72.5'") == 72.5

    def test_strips_combined_artifacts(self):
        # str(b'72.5') == "b'72.5'" — the real MQTT payload representation
        assert cleanvalue("b'72.5'") == 72.5

    def test_negative_value(self):
        assert cleanvalue('-5.3') == -5.3

    def test_zero(self):
        assert cleanvalue('0.0') == 0.0

    def test_returns_float(self):
        assert isinstance(cleanvalue('100'), float)


# ── getstart ──────────────────────────────────────────────────────────────────

class TestGetstart:
    def test_none_defaults_to_24h_ago(self):
        before = time.time()
        result = getstart(None)
        after = time.time()
        assert before - 24 * 3600 - 1 <= result <= after - 24 * 3600 + 1

    def test_explicit_1h(self):
        before = time.time()
        result = getstart(1)
        after = time.time()
        assert before - 3600 - 1 <= result <= after - 3600 + 1

    def test_zero_hours_is_approx_now(self):
        before = time.time()
        result = getstart(0)
        after = time.time()
        assert before - 1 <= result <= after + 1

    def test_returns_float(self):
        assert isinstance(getstart(1), float)

    def test_larger_period_is_further_in_past(self):
        assert getstart(48) < getstart(24)


# ── decrypt_password_aes ──────────────────────────────────────────────────────

class TestDecryptPasswordAes:
    def test_correct_key_decrypts(self):
        key = os.urandom(32)
        enc, key_b64 = _encrypt('SuperSecret!', key)
        assert decrypt_password_aes(enc, key_b64) == 'SuperSecret!'

    def test_wrong_key_returns_none(self):
        key = os.urandom(32)
        enc, _ = _encrypt('SuperSecret!', key)
        wrong_b64 = base64.urlsafe_b64encode(os.urandom(32)).decode()
        assert decrypt_password_aes(enc, wrong_b64) is None

    def test_garbage_input_returns_none(self):
        garbage = base64.urlsafe_b64encode(b'not_valid_ciphertext').decode()
        key_b64 = base64.urlsafe_b64encode(os.urandom(32)).decode()
        assert decrypt_password_aes(garbage, key_b64) is None

    def test_empty_string_returns_none(self):
        key_b64 = base64.urlsafe_b64encode(os.urandom(32)).decode()
        assert decrypt_password_aes('', key_b64) is None

    def test_roundtrip_special_characters(self):
        key = os.urandom(32)
        plaintext = 'P@$$w0rd!#&*'
        enc, key_b64 = _encrypt(plaintext, key)
        assert decrypt_password_aes(enc, key_b64) == plaintext
