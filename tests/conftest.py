import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT))

TEST_CONFIG_DIR = Path(tempfile.gettempdir()) / "metafusion-tests"
TEST_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("CONFIG_DIR", str(TEST_CONFIG_DIR))


@pytest.fixture(autouse=True)
def close_shared_sqlite_sessions():
    """Make every test prove that process-level SQLite stores can be released."""
    yield
    from helper import cache as cache_module

    cache_module.close_cache_session()
