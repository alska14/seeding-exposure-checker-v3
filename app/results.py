"""엔진이 만든 리포트(요약 시트)를 보드용 JSON으로 바꾼다."""
from pathlib import Path

from openpyxl import load_workbook

HEADER_ROW = 13
GROUPS = ["확인됨", "의심", "미확인", "업로드 없음", "채널 오류"]


def read_report(path):
    path = Path(path)
    if not path.exists():
        return None
    ws = load_workbook(path, read_only=False)["요약"]
    hdr = [c.value for c in ws[HEADER_ROW]]
    rows = []
    for r in ws.iter_rows(min_row=HEADER_ROW + 1, values_only=True):
        if r[0] is None:
            continue
        d = dict(zip(hdr, r))
        note = " ".join(str(d.get("비고") or "").split())
        rows.append({"n": d["번호"], "name": d["유튜버"], "date": str(d["발송일"] or ""), "prod": d["협찬 상품"] or "",
                     "url": d["채널 링크"] or "", "cnt": d["발송 후 영상 수"] or 0, "st": d["판정"],
                     "ev": d["근거"] or "", "vid": d["근거 영상"] or "",
                     "note": note[:260] + ("…" if len(note) > 260 else ""), "scope": d["영상 분석 범위"] or ""})
    return rows


def latest_report(job_dir):
    out = Path(job_dir) / "output"
    files = sorted(out.glob("seeding_report*.xlsx"), key=lambda p: p.stat().st_mtime) if out.exists() else []
    return files[-1] if files else None


def counts(rows):
    c = {g: 0 for g in GROUPS}
    for r in rows:
        for g in GROUPS:
            if r["st"].startswith(g):
                c[g] += 1
                break
    return c
