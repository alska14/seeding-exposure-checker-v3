# 시딩 노출 확인 (3차: 웹 버전)

시딩(협찬) 리스트를 올리면 유튜버가 영상에서 제품을 노출했는지 1차로 골라주는 웹 서비스입니다.
사용자가 자기 YouTube·OpenAI API 키를 입력해서 쓰며, 키는 서버에 저장하지 않습니다.
설계는 `docs/superpowers/specs/2026-11-04-seeding-web-design.md`에 있습니다.

## 로컬 실행
```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt -r requirements-web.txt -r requirements-dev.txt
.venv/Scripts/python -m uvicorn app.main:app --port 8000
```
브라우저에서 http://127.0.0.1:8000 을 엽니다. 영상 분석에는 ffmpeg가 필요합니다.

## 테스트
```bash
.venv/Scripts/python -m pytest -q
```
외부 API 없이 가짜 엔진으로 업로드 → 준비 분석 → 영상 분석 → 재검증 → 삭제 흐름과,
**키가 DB·로그·파일·응답에 남지 않는지**를 검사합니다.

## 설정 (환경변수)
| 이름 | 기본값 | 뜻 |
| --- | --- | --- |
| DATA_DIR | ./data | 작업 데이터와 DB 위치 |
| MAX_JOBS | 1 | 동시에 도는 작업 수 |
| MAX_CAP_USD | 50 | 사용자가 정할 수 있는 비용 상한의 최댓값 |
| JOB_TTL_DAYS | 7 | 작업 데이터 보관 일수 |
| REQUIRE_HTTPS | 0 | 1이면 HTTPS가 아닌 요청 거부(운영에서 켬) |

## 배포
`Dockerfile`이 있습니다(ffmpeg 포함). 아직 실제 배포와 도커 빌드는 해보지 않았습니다.
**배포 전에 확인할 것:** 클라우드 서버 IP에서 영상 다운로드(yt-dlp)가 막히는지.

## 구조
- `seeding_checker.py`: 기존 분석 엔진(명령줄·캐시 형식 유지). 웹 서버가 작업 폴더에서 별도 프로세스로 실행합니다.
- `app/`: FastAPI 서버(업로드, 작업 실행, 키 가리기, 비용 상한, 확인 기록)
- `web/`: 화면
