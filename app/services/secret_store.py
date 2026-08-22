import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


def _fernet():
    secret_key = str(current_app.config["SECRET_KEY"]).encode("utf-8")
    derived_key = hashlib.sha256(secret_key).digest()
    return Fernet(base64.urlsafe_b64encode(derived_key))


def encrypt_secret(value):
    if not value:
        return None
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value):
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""
