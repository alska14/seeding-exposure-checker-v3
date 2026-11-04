import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="seedtest_")
os.environ["DATA_DIR"] = TMP
os.environ["ENGINE_PATH"] = str(ROOT / "tests" / "fake_engine.py")
os.environ["MAX_CAP_USD"] = "20"
sys.path.insert(0, str(ROOT))
