"""Encryption of MT5 passwords at rest (Fernet / AES-128-CBC + HMAC)."""

from cryptography.fernet import Fernet, InvalidToken

from .config import settings


def _fernet(key=None):
    return Fernet((key or settings.encryption_key).encode())


def encrypt(plaintext, key=None):
    return _fernet(key).encrypt(plaintext.encode()).decode()


def decrypt(token, key=None):
    try:
        return _fernet(key).decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise ValueError("Cannot decrypt: ENCRYPTION_KEY changed or data corrupted") from e


if __name__ == "__main__":
    print(Fernet.generate_key().decode())
