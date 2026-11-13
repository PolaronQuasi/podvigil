"""The account's REST client as the controller configures it: pacing, cleanup priority, provider clock."""

from contextlib import nullcontext
import time

from ..podrt.api import Api
from ..settings import pacing_file


def configured_api(key):
    api = Api(key)
    if hasattr(api, "configure_pacing"):
        api.configure_pacing(pacing_file(key), max_wait=1, read_retries=1)
    return api


def cleanup_priority(api):
    return api.pacing(cleanup=True, max_wait=1) if hasattr(api, "pacing") else nullcontext()


def provider_now(api):
    if hasattr(api, "_clock_anchor") and api._clock_anchor is None:
        raise RuntimeError("Provider Date clock has not been established.")
    return api.now() if hasattr(api, "now") else time.time()
