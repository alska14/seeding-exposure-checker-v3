"""웹 서버는 엔진을 격리 모드로 실행한다. 그 방식으로도 recheck 가 불러와지고 한글 출력이 깨지지 않는지 확인한다."""
import os
import subprocess
from pathlib import Path

from app.jobs import PY

ENGINE = Path(__file__).resolve().parent.parent / "seeding_checker.py"


def test_recheck_command_loads_under_isolated_mode_and_prints_korean(tmp_path):
    (tmp_path / "config.yaml").write_text("brand_keywords: [예시]\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k in ("SYSTEMROOT", "PATH", "TEMP", "TMP")}  # 서버처럼 PYTHON* 환경변수 없이
    p = subprocess.run([*PY, str(ENGINE), "recheck", "--config", "config.yaml", "--dry-run"], cwd=tmp_path,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    out = p.stdout + p.stderr
    assert "ModuleNotFoundError" not in out
    assert "미확인 채널이 없습니다" in out  # 한글이 깨지지 않았다
