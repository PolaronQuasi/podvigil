"""Container tests: the Pod code in network-less Linux containers. They run only when
PODVIGIL_REVIEW_DOCKER_IMAGE (or the old WONDOMON_REVIEW_DOCKER_IMAGE) names an already-installed Python
image; no image is ever pulled.
"""

import os

import tests  # noqa: F401 -- puts src/ on sys.path also when discovery starts below tests/
from podvigil.settings import ENV_PREFIXES

IMAGE_VARIABLES = tuple(prefix + "_REVIEW_DOCKER_IMAGE" for prefix in ENV_PREFIXES)


def review_image():
    """The installed image these tests run, under either name; None when neither is set."""
    return next((os.environ[name] for name in IMAGE_VARIABLES if os.environ.get(name)), None)
