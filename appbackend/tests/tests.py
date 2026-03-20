import unittest
import base64
import os

from server import decrypt_password_aes, _load_3p_services


class TestDecryption(unittest.TestCase):
    def test_aes_cbc_decryption(self):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.backends import default_backend

        key = os.urandom(32)
        iv = os.urandom(16)
        original_password = "MySecretPassword"

        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        encryptor = cipher.encryptor()
        padder = padding.PKCS7(algorithms.AES.block_size).padder()
        padded_data = padder.update(original_password.encode('utf-8')) + padder.finalize()
        ciphertext = encryptor.update(padded_data) + encryptor.finalize()
        encrypted_password = base64.urlsafe_b64encode(iv + ciphertext).decode('utf-8')
        key_b64 = base64.urlsafe_b64encode(key).decode('utf-8')

        self.assertEqual(decrypt_password_aes(encrypted_password, key_b64), original_password)

        wrong_key_b64 = base64.urlsafe_b64encode(os.urandom(32)).decode('utf-8')
        self.assertIsNone(decrypt_password_aes(encrypted_password, wrong_key_b64))

        garbage = base64.urlsafe_b64encode(b"some_garbage_data").decode('utf-8')
        self.assertIsNone(decrypt_password_aes(garbage, key_b64))


class Test3PServices(unittest.TestCase):
    def test_load_list(self):
        self.assertTrue(_load_3p_services(['keyvanazami@gmail.com']))


if __name__ == '__main__':
    unittest.main()
