import os
from io import BytesIO
from unittest.mock import patch

from botocore.response import StreamingBody
from botocore.stub import Stubber
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from driver_profiles.source_media import SourceMediaReader


class DriverKycSourceMediaTests(SimpleTestCase):
    @patch.dict(
        os.environ,
        {
            "AWS_ACCESS_KEY_ID": "local-test-key",
            "AWS_SECRET_ACCESS_KEY": "local-test-secret",
            "AWS_EC2_METADATA_DISABLED": "true",
        },
    )
    def test_s3_reads_are_scoped_to_the_configured_bucket_and_key(self):
        reader = SourceMediaReader(
            "s3",
            1024,
            s3_buckets={
                "default": "legacy-private",
                "id_document": "legacy-id-private",
            },
            region_name="af-south-1",
        )
        stubber = Stubber(reader.s3_client)
        stubber.add_response(
            "get_object",
            {
                "Body": StreamingBody(BytesIO(b"private-document"), 16),
                "ContentLength": 16,
                "ETag": '"source-etag"',
            },
            {"Bucket": "legacy-id-private", "Key": "drivers/ids/id-7.png"},
        )
        with stubber:
            content, path = reader.read("drivers/ids/id-7.png", "id_document", 7)

        self.assertEqual(content, b"private-document")
        self.assertIsNone(path)

    @patch.dict(
        os.environ,
        {
            "AWS_ACCESS_KEY_ID": "local-test-key",
            "AWS_SECRET_ACCESS_KEY": "local-test-secret",
            "AWS_EC2_METADATA_DISABLED": "true",
        },
    )
    def test_s3_key_traversal_is_rejected_before_fetch(self):
        reader = SourceMediaReader(
            "s3",
            1024,
            s3_buckets={"default": "legacy-private"},
        )

        with self.assertRaisesMessage(CommandError, "unsafe"):
            reader.read("../private/secret.png", "license_photo", 7)

    @patch.dict(
        os.environ,
        {
            "AWS_ACCESS_KEY_ID": "local-test-key",
            "AWS_SECRET_ACCESS_KEY": "local-test-secret",
            "AWS_EC2_METADATA_DISABLED": "true",
        },
    )
    def test_s3_object_size_is_bounded_before_stream_read(self):
        reader = SourceMediaReader(
            "s3",
            8,
            s3_buckets={"default": "legacy-private"},
        )
        stubber = Stubber(reader.s3_client)
        stubber.add_response(
            "get_object",
            {
                "Body": StreamingBody(BytesIO(b"too-large"), 9),
                "ContentLength": 9,
                "ETag": '"source-etag"',
            },
            {"Bucket": "legacy-private", "Key": "drivers/docs/id.pdf"},
        )
        with stubber, self.assertRaisesMessage(CommandError, "oversized"):
            reader.read("drivers/docs/id.pdf", "default", 7)
