"""Real configured boto3 client and transfers, with dummy credentials and an in-memory HTTP transport."""

from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape
import io
import tempfile
import unittest

from podvigil import storage as storage_module
from podvigil.storage import Storage, verify_local_backup
from tests.support.fakes import FakeS3
from tests.support.jobs import fixture

try:
    import boto3
    from botocore.awsrequest import AWSResponse
except ImportError:
    boto3 = None


class ResponseBody(io.BytesIO):
    def stream(self, amt=None, decode_content=False):
        while data := self.read(amt or 65536):
            yield data


@unittest.skipIf(
    boto3 is None, "boto3 is not installed; run this SDK boundary test with the project venv"
)
class StorageSdkTests(unittest.TestCase):
    def test_configured_client_signs_and_transfers_verified_results_without_network(self):
        with tempfile.TemporaryDirectory(prefix="podvigil-sdk-") as temporary:
            folder = Path(temporary)
            job = fixture()
            fake = FakeS3()
            fake.finished(job["job_id"])
            objects = dict(fake.objects)
            calls = []

            def send(request):
                url = urlsplit(request.url)
                self.assertEqual(url.netloc, "s3api-eu-ro-1.runpod.io")
                self.assertTrue(request.headers["Authorization"].startswith(b"AWS4-HMAC-SHA256 "))
                bucket, _, key = unquote(url.path.lstrip("/")).partition("/")
                calls.append((request.method, key))
                headers = {"content-type": "application/octet-stream"}
                payload = b""
                if request.method == "PUT":
                    payload = request.body.read() if hasattr(request.body, "read") else request.body
                    if b"aws-chunked" in request.headers.get("Content-Encoding", b""):
                        chunks, remaining = [], payload
                        while remaining:
                            line, remaining = remaining.split(b"\r\n", 1)
                            size = int(line.split(b";", 1)[0], 16)
                            if size == 0:
                                break
                            chunks.append(remaining[:size])
                            remaining = remaining[size + 2 :]
                        payload = b"".join(chunks)
                    objects[(bucket, key)] = payload
                    payload = b""
                    headers["etag"] = '"test-etag"'
                elif request.method == "HEAD":
                    headers.update(
                        {"content-length": str(len(objects[(bucket, key)])), "etag": '"test-etag"'}
                    )
                elif request.method == "GET" and not key:
                    prefix = parse_qs(url.query).get("prefix", [""])[0]
                    entries = sorted(
                        (name, data)
                        for (owner, name), data in objects.items()
                        if owner == bucket and name.startswith(prefix)
                    )
                    xml = (
                        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                        "<IsTruncated>false</IsTruncated><KeyCount>"
                        + str(len(entries))
                        + "</KeyCount>"
                        + "".join(
                            "<Contents><Key>"
                            + escape(name)
                            + "</Key><Size>"
                            + str(len(data))
                            + "</Size></Contents>"
                            for name, data in entries
                        )
                        + "</ListBucketResult>"
                    )
                    payload = xml.encode()
                    headers["content-type"] = "application/xml"
                elif request.method == "GET":
                    payload = objects[(bucket, key)]
                else:
                    raise AssertionError("Unexpected SDK operation")
                headers.setdefault("content-length", str(len(payload)))
                return AWSResponse(request.url, 200, headers, ResponseBody(payload))

            (folder / "no-config").write_text("[default]\n", encoding="utf-8")
            environment = {
                "AWS_EC2_METADATA_DISABLED": "true",
                "AWS_PROFILE": "default",
                "AWS_DEFAULT_PROFILE": "default",
                "AWS_CONFIG_FILE": str(folder / "no-config"),
                "AWS_SHARED_CREDENTIALS_FILE": str(folder / "no-credentials"),
            }
            with (
                patch.dict("os.environ", environment),
                patch.object(boto3, "DEFAULT_SESSION", None),
                patch.object(
                    storage_module,
                    "local_secrets",
                    return_value={
                        "RUNPOD_S3_ACCESS_KEY": "dummy-sdk-access",
                        "RUNPOD_S3_SECRET_KEY": "dummy-sdk-secret",
                    },
                ),
                patch("botocore.httpsession.URLLib3Session.send", side_effect=send),
                patch(
                    "socket.create_connection",
                    side_effect=AssertionError("network access is forbidden"),
                ),
            ):
                store = Storage("EU-RO-1")
                self.addCleanup(store.client.close)
                config = store.client.meta.config
                self.assertEqual(
                    (config.region_name, config.signature_version), ("EU-RO-1", "s3v4")
                )
                self.assertEqual((config.connect_timeout, config.read_timeout), (15, 30))
                self.assertEqual(config.s3["addressing_style"], "path")
                self.assertEqual(config.retries, {"total_max_attempts": 5, "mode": "standard"})
                store.put_json("testvolume", "managed-owner.json", {"job_id": job["job_id"]})
                self.assertEqual(
                    store.get_json("testvolume", "managed-owner.json"), {"job_id": job["job_id"]}
                )
                source = folder / "source.txt"
                source.write_bytes(b"sdk upload contents\n")
                store.upload("testvolume", "inputs/source.txt", source)
                self.assertEqual(objects[("testvolume", "inputs/source.txt")], source.read_bytes())
                manifest = store.collect(job, "testvolume", folder / "downloads")
                self.assertEqual(
                    verify_local_backup(job, "testvolume", folder / "downloads"), manifest
                )
                for item in manifest["files"]:
                    self.assertEqual(
                        (folder / "downloads" / item["path"]).read_bytes(),
                        objects[("testvolume", "jobs/" + job["job_id"] + "/" + item["path"])],
                    )
                # The actual S3Transfer download path issues HEAD.
                self.assertTrue(any(method == "HEAD" for method, _ in calls))
                self.assertTrue(any(method == "PUT" for method, _ in calls))


if __name__ == "__main__":
    unittest.main()
