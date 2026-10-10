"""Symmetric encryption for OAuth tokens at rest."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.models.config import TokenConfig


class TokenCipher:
    """Encrypts and decrypts token material using the application key.

    The key is `tokens.encryption_key` in config.json. Ciphertext
    is what lands in Mongo; plaintext never leaves this process.
    """

    def __init__(self, tokens: TokenConfig) -> None:
        self._fernet = Fernet(tokens.encryption_key.get_secret_value().encode())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise TokenDecryptionError(
                "Stored token could not be decrypted with the configured "
                "tokens.encryption_key. The key was most likely rotated; "
                "re-run the OAuth connection flow."
            ) from exc


class TokenDecryptionError(Exception):
    """The stored ciphertext does not match the configured encryption key."""
