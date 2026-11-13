from pathlib import Path
from podvigil.podrt import records as job_state
import copy
import hashlib
import io
import json
import threading
import time
import urllib.parse
from podvigil.podrt.api import ApiError
from tests.support.paths import ROOT
from podvigil.settings import ENV_PREFIX


# The live answer to POST /pods on 2026-11-12.
NO_CAPACITY_DETAIL = (
    "There are no longer any instances available with the requested specifications. "
    "Please refresh and try again."
)

# Prefix for code run by real controller or watchdog processes started from the project folder.
# Both resolve these dummy credentials, so neither reads the project's .env. Patch the function
# in the module where credential_proof looks it up.
DUMMY_CREDENTIALS_CODE = (
    "import sys;sys.path.insert(0," + repr(str(ROOT)) + ");"
    "from tests.support import runner;"
    "sys.modules[runner.credential_proof.__module__].resolve_credentials=lambda:"
    "{'RUNPOD_API_KEY':'test-key','RUNPOD_S3_ACCESS_KEY':None,'RUNPOD_S3_SECRET_KEY':None};"
)


class FakeApi:
    def now(self):
        return time.time()

    def __init__(self, job=None):
        self.pods = {}
        self.volumes = {}
        self.calls = []
        self.delete_failures = 0
        self.ignore_delete = False
        self.post_error = None
        # (status, detail), or a function of the request body returning one or None: POST /pods is
        # refused and creates nothing, like the live "no instances" 400.
        self.pod_rejection = None
        # GPU id -> catalog entry; ids not listed get the permissive default entry below.
        self.gpu_catalog = {}
        # Data center id -> network volume types; ids not listed offer STANDARD.
        self.datacenter_volumes = {}
        self.cloud = "SECURE"
        self.linked_s3 = []
        self.storage = None
        self.lock = threading.RLock()
        if job:
            self.pods["testpod"] = pod_for(job)
            self.volumes["testvolume"] = volume_for(job)

    @property
    def storage(self):
        return self._storage

    @storage.setter
    def storage(self, value):
        # The S3 view of a volume ends when the volume is deleted, as on the real provider.
        self._storage = value
        client = getattr(value, "client", None)
        if isinstance(client, FakeS3):
            self.link(client)

    def link(self, s3):
        """Deleting a volume through this API also deletes its bucket in s3."""
        if s3 not in self.linked_s3:
            self.linked_s3.append(s3)
        return s3

    def catalog_gpu(self, gpu_id):
        default = {
            "id": gpu_id,
            "secure": True,
            "price": {"secure": 0.5},
            "dataCenters": [{"id": "EU-RO-1", "availability": "HIGH"}],
        }
        return copy.deepcopy(self.gpu_catalog.get(gpu_id, default))

    def get_or_none(self, path):
        try:
            return self.request("GET", path)
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise

    def listing(self, path, field):
        return self.request("GET", path)[field]

    def request(self, method, path, body=None):
        with self.lock:
            self.calls.append((method, path))
            path = urllib.parse.urlsplit(path).path
            if path == "/catalog/gpus":
                return {"gpus": [self.catalog_gpu(gpu) for gpu in self.gpu_catalog]}
            if path.startswith("/catalog/gpus/"):
                return self.catalog_gpu(urllib.parse.unquote(path.split("/", 3)[3]))
            if path.startswith("/catalog/datacenters/"):
                center = urllib.parse.unquote(path.split("/", 3)[3])
                types = self.datacenter_volumes.get(center, ["STANDARD"])
                if types is None:
                    raise ApiError(404, method, path)  # a data center the catalog does not know
                return {"id": center, "networkVolumeTypes": list(types)}
            kind = path.split("/")[1]
            values = self.pods if kind == "pods" else self.volumes
            if method == "POST":
                rejection = None
                if kind == "pods" and self.pod_rejection:
                    rejection = (
                        self.pod_rejection(body)
                        if callable(self.pod_rejection)
                        else self.pod_rejection
                    )
                if rejection:
                    status, detail = rejection
                    raise ApiError(status, method, path, title="Bad Request", detail=detail)
                item = copy.deepcopy(body)
                item["id"] = "testpod" if kind == "pods" else "testvolume"
                if kind != "pods":
                    # Real volume IDs are unique. The fake reuses one ID, so reset its bucket
                    # for each new volume.
                    for s3 in self.linked_s3:
                        s3.deleted_buckets.discard(item["id"])
                if kind == "pods":
                    item.update(
                        cloud=self.cloud,
                        cost=0.5,
                        status="RUNNING",
                        dataCenterId=(body.get("dataCenterIds") or ["EU-RO-1"])[0],
                    )
                    if self.storage:
                        jid = body["env"][ENV_PREFIX + "_JOB_ID"]
                        self.storage.put_json(
                            "testvolume",
                            "jobs/" + jid + "/agent.json",
                            {"job_id": jid, "phase": "ready", "heartbeat_epoch": time.time()},
                        )
                values[item["id"]] = item
                if self.post_error:
                    raise ApiError(self.post_error, method, path)
                return copy.deepcopy(item)
            if path.count("/") == 1:
                return {
                    ("pods" if kind == "pods" else "networkVolumes"): copy.deepcopy(
                        list(values.values())
                    ),
                    "pagination": {"hasNextPage": False, "nextCursor": None},
                }
            rid = path.split("/")[2]
            if rid not in values:
                raise ApiError(404, method, path)
            if method == "GET":
                return copy.deepcopy(values[rid])
            if method == "DELETE":
                if self.delete_failures:
                    self.delete_failures -= 1
                    raise ApiError(503, method, path)
                if not self.ignore_delete:
                    del values[rid]
                    if kind == "network-volumes":
                        for s3 in self.linked_s3:
                            s3.drop_bucket(rid)
                return None
            raise AssertionError((method, path))


def pod_for(job):
    return {
        "id": "testpod",
        "name": job["pod_name"],
        "cloud": "SECURE",
        "status": "RUNNING",
        "env": {ENV_PREFIX + "_JOB_ID": job["job_id"]},
        "cost": 0.5,
        "dataCenterId": "EU-RO-1",
        "gpu": {"id": job["config"]["gpu_id"], "count": 1},
        "actions": ["terminate"],
        "mounts": {"network": [{"volumeId": "testvolume", "path": job_state.VOLUME_MOUNT}]},
    }


def volume_for(job):
    return {"id": "testvolume", "name": job["volume_name"], "dataCenter": "EU-RO-1", "size": 10}


class MissingObject(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class NoSuchBucket(Exception):
    """What S3 answers for a volume that has been deleted: every call fails, a listing included."""

    response = {"Error": {"Code": "NoSuchBucket"}}


class FakeS3:
    def __init__(self, page_size=1000, api=None):
        self.objects = {}
        self.corrupt_download = False
        self.on_start = None
        self.page_size = page_size
        self.list_calls = []
        self.downloads = []
        self.deleted_buckets = set()
        if api is not None:
            api.link(self)

    def drop_bucket(self, bucket):
        self.deleted_buckets.add(bucket)
        for key in [key for key in self.objects if key[0] == bucket]:
            del self.objects[key]

    def _exists(self, bucket):
        if bucket in self.deleted_buckets:
            raise NoSuchBucket()

    def put_object(self, Bucket, Key, Body):
        self._exists(Bucket)
        self.objects[(Bucket, Key)] = Body
        if Key.endswith("/start.json") and self.on_start:
            self.on_start(json.loads(Body))

    def get_object(self, Bucket, Key):
        self._exists(Bucket)
        if (Bucket, Key) not in self.objects:
            raise MissingObject()
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        self.list_calls.append((Bucket, Prefix, ContinuationToken))
        self._exists(Bucket)
        offset = 0 if ContinuationToken is None else int(ContinuationToken.removeprefix("page:"))
        matches = sorted(
            (key, content)
            for (bucket, key), content in self.objects.items()
            if bucket == Bucket and key.startswith(Prefix)
        )
        page = matches[offset : offset + self.page_size]
        next_offset = offset + len(page)
        response = {
            "IsTruncated": next_offset < len(matches),
            "KeyCount": len(page),
            "Contents": [{"Key": key, "Size": len(content)} for key, content in page],
        }
        if response["IsTruncated"]:
            response["NextContinuationToken"] = f"page:{next_offset}"
        return response

    def download_file(self, bucket, key, destination):
        self.downloads.append((bucket, key, Path(destination)))
        self._exists(bucket)
        Path(destination).write_bytes(
            b"corrupt" if self.corrupt_download else self.objects[(bucket, key)]
        )

    def upload_file(self, source, bucket, key):
        self._exists(bucket)
        self.objects[(bucket, key)] = Path(source).read_bytes()

    def finished(self, job_id, outcome="success"):
        prefix = "jobs/" + job_id + "/"
        self.objects[("testvolume", prefix + "agent.json")] = json.dumps(
            {"job_id": job_id, "pod_id": "testpod", "phase": "done", "heartbeat_epoch": time.time()}
        ).encode()
        files = {
            "artifacts/train.log": b"completed\n",
            "artifacts/result.json": json.dumps({"job_id": job_id, "outcome": outcome}).encode(),
            "artifacts/model.bin": b"example weights",
        }
        if outcome == "success":
            files["artifacts/metrics.json"] = b'{"gpu":"fake"}'
        manifest = {
            "job_id": job_id,
            "complete": True,
            "files": [],
            "created_utc": "2026-11-10T00:00:00+00:00",
        }
        for name, content in files.items():
            self.objects[("testvolume", prefix + name)] = content
            manifest["files"].append(
                {"path": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            )
        self.objects[("testvolume", prefix + "artifact-manifest.json")] = json.dumps(
            manifest
        ).encode()


class NoAgentStorage:
    def get_json(self, *args):
        return None
