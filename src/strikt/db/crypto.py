"""Fernet encryption for integration tokens at rest (``TOKEN_ENCRYPTION_KEY``)."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class TokenCipher:
    """Symmetric encrypt/decrypt of short strings. Ciphertext is urlsafe base64 text."""

    def __init__(self, key: str) -> None:
        if not key:
            raise ValueError("TOKEN_ENCRYPTION_KEY is empty; run `make keygen` to create one")
        self._fernet = Fernet(key.encode("ascii") if isinstance(key, str) else key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError(
                "token ciphertext is invalid or was encrypted with another key"
            ) from exc

    def encrypt_optional(self, plaintext: str | None) -> str | None:
        return None if plaintext is None else self.encrypt(plaintext)

    def decrypt_optional(self, ciphertext: str | None) -> str | None:
        return None if ciphertext is None else self.decrypt(ciphertext)


def generate_key() -> str:
    """A fresh Fernet key, suitable for ``TOKEN_ENCRYPTION_KEY``."""
    return Fernet.generate_key().decode("ascii")
