"""Small encryption boundary. Keys are generated only for the first write."""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class CredentialCipher:
    def __init__(self, data_dir: Path, has_encrypted_rows):
        self.path = Path(data_dir) / 'provider-master.key'
        self._has_encrypted_rows = has_encrypted_rows

    def _fernet(self, *, create=False):
        override = os.getenv('PROVIDER_POOL_MASTER_KEY', '').strip()
        if override:
            key = override.encode('ascii', errors='replace')
        else:
            try:
                key = self.path.read_bytes().strip()
            except FileNotFoundError:
                if not create or self._has_encrypted_rows():
                    raise ValueError('Provider master key is missing; restore the existing key') from None
                self.path.parent.mkdir(parents=True, exist_ok=True)
                candidate = Fernet.generate_key()
                try:
                    fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    key = self.path.read_bytes().strip()
                else:
                    with os.fdopen(fd, 'wb') as file:
                        file.write(candidate)
                        file.flush()
                        os.fsync(file.fileno())
                    try:
                        self.path.chmod(0o600)
                    except OSError:
                        pass
                    key = candidate
        try:
            return Fernet(key)
        except (ValueError, TypeError):
            raise ValueError('Provider master key is invalid') from None

    def encrypt(self, value: str) -> str:
        return self._fernet(create=True).encrypt(value.encode('utf-8')).decode('ascii')

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet().decrypt(value.encode('ascii')).decode('utf-8')
        except (InvalidToken, UnicodeError, TypeError):
            raise ValueError('Provider credential cannot be decrypted with the configured master key') from None
