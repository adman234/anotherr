import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="anotherr-test-")
os.environ["ANOTHERR_CONFIG"] = _TMP
os.environ["ANOTHERR_DB"] = os.path.join(_TMP, "test.db")
os.environ["ANOTHERR_NO_WORKER"] = "1"
os.environ["ANOTHERR_MNT"] = os.path.join(_TMP, "mnt")
os.environ["ANOTHERR_EMHTTP"] = os.path.join(_TMP, "emhttp")

import pytest  # noqa: E402

from anotherr import db, settings  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    db.init()
    for t in ("queue", "actions", "reviews", "issues", "scans", "files", "items", "settings", "activity"):
        db.x(f"DELETE FROM {t}")
    settings._cache = None
    yield
