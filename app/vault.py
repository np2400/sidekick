"""Encrypts what's stored in the database (Google tokens, saved conversations) with SECRETS_KEY from .env.

The key never goes into the database or its backups, so a leaked database or S3 copy holds only locked boxes.
Fernet = AES encryption plus a tamper check (HMAC): a modified box fails to open instead of decrypting to junk.
Make a key with:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
import json

from cryptography.fernet import Fernet, InvalidToken

from app.config import SECRETS_KEY


class VaultError(Exception):
    pass


def _fernet() -> Fernet:
    if not SECRETS_KEY:
        raise VaultError("SECRETS_KEY is not set, so secrets can't be stored safely.")
    return Fernet(SECRETS_KEY)


def lock(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data).encode()).decode()


def unlock(box: str) -> dict:
    try:
        return json.loads(_fernet().decrypt(box.encode()))
    except InvalidToken as exc:
        raise VaultError("Can't open a stored secret: SECRETS_KEY changed or the data was altered.") from exc


def lock_bytes(data: bytes) -> bytes:
    return _fernet().encrypt(data)


def unlock_bytes(box: bytes) -> bytes:
    try:
        return _fernet().decrypt(box)
    except InvalidToken as exc:
        raise VaultError("Can't open a stored file: SECRETS_KEY changed or the data was altered.") from exc


def lock_text(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def unlock_text(box: str) -> str:
    try:
        return _fernet().decrypt(box.encode()).decode()
    except InvalidToken as exc:
        raise VaultError("Can't open stored text: SECRETS_KEY changed or the data was altered.") from exc
