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

## API 키 넣는 방법
**키는 환경변수가 아니라 화면에서 입력합니다.** 서버는 키를 읽지도 저장하지도 않고, 입력한 키는 그 작업 한 건을 실행하는 동안만 쓰입니다.
1. 서버를 켭니다(위 "로컬 실행").
2. 브라우저에서 새 작업을 만들고 엑셀을 올립니다.
3. ① 준비 분석 칸에 YouTube 키, ② 영상 분석·③ 재검증 칸에 OpenAI 키를 붙여넣고 시작합니다. 칸은 제출하면 바로 비워집니다.
- 키 발급: YouTube Data API v3(Google Cloud Console), OpenAI API 키(platform.openai.com).
- 서버 환경변수는 아래 표의 설정값뿐입니다. 키 이름(`YOUTUBE_API_KEY` 등)을 서버 환경에 넣어도 엔진에는 전달되지 않습니다.

## 재검증(미확인 채널)
③ 칸의 "계획 보기"는 아무것도 호출하지 않고 대상 수·쿼터·예상 비용만 보여줍니다. "재검증 시작"은 댓글 재검토, 표기 변형, 설명란 링크, 분석 서술 재검색을 실행하고, 비용 상한을 적으면 안 본 영상도 분석합니다. 한 번에 쓰는 YouTube 쿼터와 링크 수는 `MAX_COMMENT_QUOTA`, `MAX_LINKS`로 제한합니다. 결과로 바뀐 판정은 보드에 반영됩니다.

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
| MAX_COMMENT_QUOTA | 2000 | 재검증 한 번에 쓸 YouTube 쿼터 상한 |
| MAX_LINKS | 300 | 재검증 한 번에 따라갈 설명란 링크 수 |
| REQUIRE_HTTPS | 0 | 1이면 HTTPS가 아닌 요청 거부(운영에서 켬) |

## 배포
`Dockerfile`이 있습니다(ffmpeg 포함). 아직 실제 배포와 도커 빌드는 해보지 않았습니다.
**배포 전에 확인할 것:** 클라우드 서버 IP에서 영상 다운로드(yt-dlp)가 막히는지.

## 구조
- `seeding_checker.py`: 기존 분석 엔진(명령줄·캐시 형식 유지). 웹 서버가 작업 폴더에서 별도 프로세스로 실행합니다.
- `app/`: FastAPI 서버(업로드, 작업 실행, 키 가리기, 비용 상한, 확인 기록)
- `web/`: 화면
