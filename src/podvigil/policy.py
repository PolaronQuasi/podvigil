"""Non-optional deployment policy for this project's paid GPU jobs."""

import re

CLOUD = "SECURE"
VOLUME_TYPE = "STANDARD"
MIN_CUDA_VERSION = "12.8"  # the host CUDA floor every job and the survey use
STANDARD_ARTIFACTS = ("result.json", "train.log")
S3_DATACENTERS = frozenset(
    (
        "EU-CZ-1",
        "EU-RO-1",
        "EUR-IS-1",
        "EUR-NO-1",
        "US-CA-2",
        "US-GA-2",
        "US-IL-1",
        "US-KS-2",
        "US-MD-1",
        "US-MO-1",
        "US-MO-2",
        "US-NC-1",
        "US-NC-2",
        "US-NE-1",
        "US-WA-1",
    )
)


def cuda_version(text):
    if not isinstance(text, str) or not re.fullmatch(r"\d+\.\d+", text):
        raise ValueError(
            "A CUDA version must be major.minor, for example " + MIN_CUDA_VERSION + "."
        )
    return tuple(map(int, text.split(".")))
