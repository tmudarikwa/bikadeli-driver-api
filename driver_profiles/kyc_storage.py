from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible

from .fields import _fernet


@deconstructible
class EncryptedPrivateKYCStorage(FileSystemStorage):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("location", settings.KYC_MEDIA_ROOT)
        kwargs.setdefault("base_url", "")
        kwargs.setdefault("file_permissions_mode", 0o600)
        kwargs.setdefault("directory_permissions_mode", 0o700)
        super().__init__(*args, **kwargs)

    def _save(self, name, content):
        data = content.read()
        encrypted = _fernet().encrypt(data)
        return super()._save(name, ContentFile(encrypted))

    def open(self, name, mode="rb"):
        with super().open(name, mode) as stored_file:
            encrypted = stored_file.read()
        return ContentFile(_fernet().decrypt(encrypted), name=name)

    def url(self, name):
        raise ValueError("Driver KYC documents are private and have no public URL.")


driver_kyc_storage = EncryptedPrivateKYCStorage()
