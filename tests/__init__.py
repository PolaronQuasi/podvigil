"""Local simulations only; these tests never create a paid cloud resource.

The tests import the package from this checkout's src/, whether or not it is installed. They never use the
runner's real state folder or credentials: the whole test session, child processes included, gets a
temporary state folder and a dummy credentials file, with shell S3 credentials removed. Only the manual
live lane undoes that, for the one command it runs (real_environment).
"""

from pathlib import Path
import atexit
import os
import shutil
import sys
import tempfile

_SOURCE = str(Path(__file__).resolve().parents[1] / "src")
if _SOURCE not in sys.path:
    sys.path.insert(0, _SOURCE)

REAL_ENVIRONMENT = {}  # the variables the session replaces, as they were before it; None when unset


def real_environment():
    """Restore the original state, credentials file and shell S3 variables in a copy, for the manual live lane.

    This never changes the normal test process's environment.
    """
    environment = dict(os.environ)
    for name, value in REAL_ENVIRONMENT.items():
        if value is None:
            environment.pop(name, None)
        else:
            environment[name] = value
    return environment


try:
    from podvigil import settings as _settings
except ImportError:  # a container run stages only some modules
    _settings = None
if _settings is not None:
    _S3_VARIABLES = ("RUNPOD_S3_ACCESS_KEY", "RUNPOD_S3_SECRET_KEY")
    for _name in (_settings.STATE_DIR_VARIABLE, _settings.ENV_FILE_VARIABLE, *_S3_VARIABLES):
        REAL_ENVIRONMENT[_name] = os.environ.get(_name)
    for _name in _S3_VARIABLES:
        os.environ.pop(_name, None)
    _SESSION = tempfile.mkdtemp(prefix="podvigil-test-session-")
    atexit.register(shutil.rmtree, _SESSION, True)
    os.environ[_settings.STATE_DIR_VARIABLE] = os.path.join(_SESSION, "state")
    _CREDENTIALS = os.path.join(_SESSION, "dummy-credentials.env")
    with open(_CREDENTIALS, "w", encoding="utf-8") as _file:
        _file.write("RUNPOD_API_KEY=dummy-test-session-key\n")
    os.environ[_settings.ENV_FILE_VARIABLE] = _CREDENTIALS
