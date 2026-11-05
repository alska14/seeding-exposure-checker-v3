"""테스트용 가짜 엔진: 키를 받아 로그에 일부러 흘리고(가리기 검사용), 최소한의 결과 파일을 만든다."""
import json
import os
import sys
from pathlib import Path

from openpyxl import Workbook

cmd = sys.argv[1]
yt, oa = os.environ.get("YOUTUBE_API_KEY", ""), os.environ.get("OPENAI_API_KEY", "")
print(f"engine {cmd} 시작 key={yt} 요청 https://x/y?part=a&key={yt}&b=1 openai={oa}")
if cmd == "recheck" and "--dry-run" in sys.argv:
    print("[1] 댓글 재검토\n  댓글을 아직 안 받은 영상 7개 중 7개\n[5] 안 본 영상 재확인\n  분석 안 한 영상 4개")
    print("(--dry-run: 아무것도 호출하거나 저장하지 않았습니다)")
    sys.exit(0)
if yt.startswith("FAIL"):
    print(f"RuntimeError: 연결 실패 https://www.googleapis.com/youtube/v3/channels?key={yt}")
    sys.exit(2)
Path("work").mkdir(exist_ok=True)
Path("output").mkdir(exist_ok=True)
Path("seen_env.json").write_text(json.dumps({"yt": bool(yt), "oa": bool(oa), "cmd": sys.argv[1:]}), encoding="utf-8")

SIG = {"brand_hits": [], "product_hits": [], "disclosure": [], "transcript_hits": [],
       "comment_hits": [], "comment_other_hits": []}


def vids(p):
    return [{"id": f"{p}{i}", "title": f"영상{i}", "duration": 600, "published": "2026-05-20T00:00:00Z",
             "url": f"https://youtu.be/{p}{i}", "signals": dict(SIG)} for i in range(5)]


if cmd == "all":
    ch = [{"no": 1, "name": "채널A", "ship_date": "2026-05-13T00:00:00+09:00", "items": [], "url": "https://www.youtube.com/@a"},
          {"no": 2, "name": "채널B", "ship_date": "2026-05-13T00:00:00+09:00", "items": [], "url": "https://www.youtube.com/@b"}]
    scr = {"1": {"channel": {"title": "채널A"}, "error": None, "videos": vids("a"), "since_assumed": False},
           "2": {"channel": {"title": "채널B"}, "error": None, "videos": [], "since_assumed": False}}
    Path("work/channels.json").write_text(json.dumps(ch, ensure_ascii=False), encoding="utf-8")
    Path("work/screen.json").write_text(json.dumps(scr, ensure_ascii=False), encoding="utf-8")
if cmd in ("all", "report", "recheck"):
    status_a = "의심(수동확인)" if cmd == "recheck" else "미확인"  # 재검증이 채널A를 의심으로 올린 것처럼
    wb = Workbook()
    ws = wb.active
    ws.title = "요약"
    hdr = ["번호", "유튜버", "발송일", "협찬 상품", "채널 링크", "발송 후 영상 수", "판정", "근거", "근거 영상", "비고", "영상 분석 범위", "실제 채널명"]
    for j, h in enumerate(hdr, 1):
        ws.cell(13, j, h)
    for i, (n, name, st) in enumerate([(1, "채널A", status_a), (2, "채널B", "확인됨(텍스트)")]):
        for j, v in enumerate([n, name, "2026-05-13", "예시 상품", "https://www.youtube.com/@x", 5, st, "", "", "", "", name], 1):
            ws.cell(14 + i, j, v)
    wb.create_sheet("영상별")
    wb.save("output/seeding_report.xlsx")
