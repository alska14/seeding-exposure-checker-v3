import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="seedtest_")
os.environ["DATA_DIR"] = TMP
os.environ["ENGINE_PATH"] = str(ROOT / "tests" / "fake_engine.py")
os.environ["MAX_CAP_USD"] = "20"
os.environ["SERVER_VIDEO"] = "1"  # 기존 테스트는 서버가 영상 분석을 직접 하는 경로(가짜 엔진)를 검사한다. 끄는 기본값은 test_helper.py 에서 검사
sys.path.insert(0, str(ROOT))
