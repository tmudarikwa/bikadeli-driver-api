import math

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models


def _encrypted_column_length(plaintext_max_length):
    padded_length = plaintext_max_length + (16 - plaintext_max_length % 16)
    token_length = 57 + padded_length
    return math.ceil(token_length / 3) * 4 + 8


def _fernet():
    key = settings.DRIVER_FIELDS_ENCRYPTION_KEY
    if not key:
        raise ImproperlyConfigured(
            "DRIVER_FIELDS_ENCRYPTION_KEY must be configured."
        )
    try:
        return Fernet(key.encode())
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured(
            "DRIVER_FIELDS_ENCRYPTION_KEY must be a valid Fernet key."
        ) from exc


class EncryptedCharField(models.CharField):
    def db_type(self, connection):
        return f"varchar({_encrypted_column_length(self.max_length)})"

    def get_db_prep_value(self, value, connection, prepared=False):
        value = super().get_db_prep_value(value, connection, prepared)
        if value:
            return _fernet().encrypt(str(value).encode()).decode()
        return value

    def from_db_value(self, value, expression, connection):
        if not value or not value.startswith("gAAAAA"):
            return value
        try:
            return _fernet().decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("Driver license could not be decrypted.") from exc
