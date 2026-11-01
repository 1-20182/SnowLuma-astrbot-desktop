import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="slab-test-"))
os.environ.setdefault("SLAB_DATA_DIR", str(_TMP / "data"))
os.environ.setdefault("SLAB_RUNTIME", str(_TMP / "runtime"))
