import os
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT))

TEST_CONFIG_DIR = Path(tempfile.gettempdir()) / "metafusion-tests"
TEST_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("CONFIG_DIR", str(TEST_CONFIG_DIR))
