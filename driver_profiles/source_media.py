from pathlib import PurePosixPath

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.core.management.base import CommandError


def contained_media_path(root, filename, profile_id, field):
    relative = PurePosixPath(str(filename).replace("\\", "/"))
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise CommandError(
            f"Driver profile {profile_id} has an unsafe {field} media path."
        )
    path = (root.joinpath(*relative.parts)).resolve()
    if not path.is_relative_to(root):
        raise CommandError(
            f"Driver profile {profile_id} has an unsafe {field} media path."
        )
    return path


class SourceMediaReader:
    def __init__(
        self,
        backend,
        max_bytes,
        filesystem_roots=None,
        s3_buckets=None,
        region_name=None,
        endpoint_url=None,
    ):
        self.backend = backend
        self.max_bytes = max_bytes
        self.filesystem_roots = filesystem_roots or {}
        self.s3_buckets = s3_buckets or {}
        self.s3_client = (
            boto3.client(
                "s3",
                region_name=region_name or None,
                endpoint_url=endpoint_url or None,
                config=Config(
                    connect_timeout=5,
                    read_timeout=30,
                    retries={"max_attempts": 2, "mode": "standard"},
                ),
            )
            if backend == "s3"
            else None
        )

    def read(self, filename, field, profile_id):
        if self.backend == "filesystem":
            return self._read_filesystem(filename, field, profile_id)
        if self.backend == "s3":
            return self._read_s3(filename, field, profile_id)
        raise CommandError("SOURCE_MEDIA_BACKEND must be filesystem or s3.")

    def _read_filesystem(self, filename, field, profile_id):
        root_field = "id_document" if field == "id_document" else "default"
        root = self.filesystem_roots.get(root_field)
        if root is None:
            raise CommandError("A source media root is not configured.")
        path = contained_media_path(root, filename, profile_id, field)
        try:
            with path.open("rb") as source_file:
                content = source_file.read(self.max_bytes + 1)
        except OSError as exc:
            raise CommandError(
                f"Driver profile {profile_id} has an unreadable {field} file."
            ) from exc
        if len(content) > self.max_bytes:
            raise CommandError(
                f"Driver profile {profile_id} has an oversized {field} file."
            )
        return content, path

    def _read_s3(self, filename, field, profile_id):
        relative = PurePosixPath(str(filename).replace("\\", "/"))
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise CommandError(
                f"Driver profile {profile_id} has an unsafe {field} object key."
            )
        bucket_field = "id_document" if field == "id_document" else "default"
        bucket = self.s3_buckets.get(bucket_field)
        if not bucket or self.s3_client is None:
            raise CommandError("Source S3 bucket configuration is incomplete.")
        try:
            response = self.s3_client.get_object(
                Bucket=bucket,
                Key=relative.as_posix(),
            )
            body = response["Body"]
            try:
                if response.get("ContentLength", 0) > self.max_bytes:
                    raise CommandError(
                        f"Driver profile {profile_id} has an oversized {field} file."
                    )
                content = body.read(self.max_bytes + 1)
            finally:
                body.close()
        except CommandError:
            raise
        except (BotoCoreError, ClientError) as exc:
            error_code = (
                exc.response.get("Error", {}).get("Code", "S3Error")
                if isinstance(exc, ClientError)
                else type(exc).__name__
            )
            raise CommandError(
                f"Driver profile {profile_id} source {field} object could not be read "
                f"({error_code})."
            ) from exc
        if len(content) > self.max_bytes:
            raise CommandError(
                f"Driver profile {profile_id} has an oversized {field} file."
            )
        return content, None
