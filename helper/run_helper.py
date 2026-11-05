"""시딩 노출 확인 — PC 도우미

웹에서 받은 이 폴더로 '영상 분석'(영상 다운로드 + OpenAI)을 내 컴퓨터에서 실행하고, 결과 파일(result.json)을 만든다.
YouTube가 클라우드 서버의 영상 다운로드를 막기 때문에, 이 단계만 내 PC에서 돌린다.

- OpenAI 키는 이 창에서 입력하고, 어디에도 저장하지 않는다 (실행 중인 프로그램의 환경변수로만 쓰인다).
- 이 폴더에는 내 채널 목록이 들어 있다. 끝나면 폴더를 지우는 것을 권장한다.
"""
import getpass
import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULES = [("requests", "requests"), ("yaml", "pyyaml"), ("openpyxl", "openpyxl"), ("openai", "openai"), ("yt_dlp", "yt-dlp")]


def fail(msg):
    print(f"\n⚠ {msg}")
    input("\nEnter 를 누르면 창이 닫힙니다")
    sys.exit(1)


def check_environment():
    if sys.version_info < (3, 10):
        fail(f"Python 3.10 이상이 필요합니다 (지금 {sys.version.split()[0]}). https://www.python.org 에서 설치하세요")
    if not shutil.which("ffmpeg"):
        fail("ffmpeg를 찾을 수 없습니다. Windows: 명령 프롬프트에서  winget install Gyan.FFmpeg  실행 후 이 창을 닫고 다시 여세요")
    missing = []
    for mod, pkg in MODULES:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        fail("필요한 패키지가 없습니다. 명령 프롬프트에서 아래를 실행한 뒤 다시 하세요:\n  "
             f"{sys.executable} -m pip install -r requirements.txt")


def write_result(manifest):
    p = HERE / "work" / "visual.json"
    visual = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    n = sum(len(v) for v in visual.values())
    (HERE / "result.json").write_text(json.dumps({"format": 1, "sig": manifest.get("expected_sig"), "visual": visual}, ensure_ascii=False),
                                      encoding="utf-8")
    return n


def main():
    os.chdir(HERE)
    manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    print("=== 시딩 노출 확인 · PC 도우미 ===")
    check_environment()
    print(f"\n분석할 영상: 최대 {manifest.get('videos', '?')}개 · 예상 비용 약 ${manifest.get('est_usd', '?')} (상한 ${manifest.get('cap_usd', '?')}, 추정)")
    key = os.environ.get("OPENAI_API_KEY") or getpass.getpass("OpenAI API 키를 붙여넣고 Enter (화면에 보이지 않고 저장되지 않습니다): ").strip()
    if not key:
        fail("OpenAI API 키가 없습니다")
    input("Enter 를 누르면 시작합니다. 중간에 멈추려면 Ctrl+C — 같은 파일을 다시 실행하면 이어서 합니다 ")
    env = dict(os.environ, OPENAI_API_KEY=key, PYTHONIOENCODING="utf-8")
    try:
        code = subprocess.run([sys.executable, "seeding_checker.py", "watch", "--config", "config.yaml"], env=env, cwd=HERE).returncode
    except KeyboardInterrupt:
        code = 130
    del key, env
    n = write_result(manifest)
    print("\n" + "=" * 50)
    if code not in (0,):
        print(f"⚠ 분석이 끝까지 가지 못했습니다(종료 코드 {code}). 지금까지 분석한 영상 {n}개의 결과는 저장했습니다.")
        print("  이 파일을 다시 실행하면 이어서 분석합니다.")
    print(f"결과 파일: {HERE / 'result.json'}  (분석한 영상 {n}개)")
    print("다음: 웹 페이지의 '결과 올리기'에서 이 result.json 을 올리세요. 끝나면 이 폴더를 지우는 것을 권장합니다.")
    input("\nEnter 를 누르면 창이 닫힙니다")


if __name__ == "__main__":
    main()
