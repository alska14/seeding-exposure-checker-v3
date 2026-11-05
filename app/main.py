import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import intake, jobs, presets, quote, security
from .estimate import estimate
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
REQUIRE_HTTPS = os.environ.get("REQUIRE_HTTPS", "0") == "1"
RECORD_VALUES = ["", "착용 확인", "업로드했으나 미착용", "업로드 안 함", "확인 불가"]

store = Store(jobs.DATA / "app.db")
limiter = security.FailLimiter()


@asynccontextmanager
async def lifespan(_app):
    for jid in store.expired():  # 보관 기간이 지난 작업 삭제
        jobs.delete_job(store, jid)
    yield


app = FastAPI(title="시딩 노출 확인", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.middleware("http")
async def guard(request: Request, call_next):
    if REQUIRE_HTTPS and request.headers.get("x-forwarded-proto", request.url.scheme) != "https":
        return JSONResponse({"error": "HTTPS로만 접속할 수 있습니다"}, status_code=400)
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                               "script-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
    return resp


def auth(request: Request, token):
    ip = request.client.host if request.client else "?"
    if limiter.blocked(ip):
        raise HTTPException(429, "요청이 너무 많습니다. 잠시 후 다시 시도하세요")
    job = store.by_token_hash(security.hash_token(token)) if token else None
    if not job or job["expires"] < time.time():
        limiter.fail(ip)
        raise HTTPException(404, "작업을 찾을 수 없습니다")
    return job


def keys_from(**named):
    out = {}
    for env, value in named.items():
        try:
            v = security.clean_key(value)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if v:
            out[env] = v
    return out


def public(job):
    est = json.loads(job["estimate"]) if job["estimate"] else None
    return {"stage": job["stage"], "message": job["message"], "estimate": est, "cap_usd": job["cap_usd"],
            "expires": job["expires"]}


@app.get("/api/presets")
def list_presets():
    return presets.public_list()


@app.get("/api/template.xlsx")
def template():
    from fastapi.responses import Response
    return Response(intake.make_template(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''%EC%8B%9C%EB%94%A9_%EB%A6%AC%EC%8A%A4%ED%8A%B8_%ED%85%9C%ED%94%8C%EB%A6%BF.xlsx"})


@app.post("/api/xlsx-preview")
async def xlsx_preview(xlsx: UploadFile = File(...)):
    """올린 엑셀의 앞부분과 자동 인식한 열 연결, 채널 수, 대략의 견적을 돌려준다 (작업은 만들지 않는다)."""
    data = await xlsx.read(security.MAX_XLSX + 1)
    try:
        security.check_xlsx(data)
        pv = intake.preview(data)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    pv["rough"] = quote.rough_quote(pv["channels"], quote.load_calibration(jobs.DATA / "calibration.json"))
    return pv


@app.post("/api/quote-preview")
def quote_preview(channels: int = Form(...)):
    """채널 수만으로 대략의 견적 (주소 붙여넣기용)"""
    if not (1 <= channels <= intake.MAX_CHANNELS):
        raise HTTPException(400, f"채널은 1~{intake.MAX_CHANNELS}곳이어야 합니다")
    return quote.rough_quote(channels, quote.load_calibration(jobs.DATA / "calibration.json"))


def parse_options(industry, title_keywords, window_days, since_date):
    preset = presets.get(industry if industry in presets.PRESETS else presets.DEFAULT_PRESET)
    words = [w for w in title_keywords.replace("\n", ",").split(",")] if title_keywords.strip() else preset["title_keywords"]
    if window_days and not (7 <= window_days <= 365):
        raise HTTPException(400, "수집 기간은 7~365일이어야 합니다")
    if since_date:
        try:
            time.strptime(since_date, "%Y-%m-%d")
        except ValueError as e:
            raise HTTPException(400, "시작일은 YYYY-MM-DD 형식이어야 합니다") from e
    return (industry if industry in presets.PRESETS else presets.DEFAULT_PRESET), words, (window_days or preset["window_days"]), since_date


def check_industry(job, ack):
    """검증되지 않은 업종의 영상 분석은 사용자가 확인했을 때만 시작한다"""
    ind = jobs.read_cfg(job["id"]).get("industry", presets.DEFAULT_PRESET)
    if not presets.get(ind)["verified"] and ack != "1":
        raise HTTPException(400, "이 업종의 영상 분석은 아직 검증되지 않았습니다(영상 분석 기준이 의류 착용입니다). "
                                 "그래도 진행하려면 확인란을 체크하세요")


@app.post("/api/job")
async def create(xlsx: UploadFile = File(None), channels_text: str = Form(""), mapping: str = Form(""),
                 brand: str = Form(...), product: str = Form(""), product_name: str = Form("상품"),
                 industry: str = Form("fashion"), title_keywords: str = Form(""), window_days: int = Form(0),
                 since_date: str = Form(""),
                 ref_labels: str = Form("[]"), refs: list[UploadFile] = File(default=[])):
    industry, title_words, window, since = parse_options(industry, title_keywords, window_days, since_date)
    try:
        if channels_text.strip():
            data = intake.from_text(channels_text, product_name.strip()[:60] or "상품")
        elif xlsx is not None:
            data = await xlsx.read(security.MAX_XLSX + 1)
            security.check_xlsx(data)
            if mapping.strip():
                m = json.loads(mapping)
                header_row = int(m["header_row"])
                cols = {k: (None if m["map"].get(k) in (None, "") else int(m["map"][k])) for k in intake.STD_MAP}
                if cols["url"] is None and cols["name"] is None:
                    raise ValueError("채널 링크 열이나 이름 열을 하나는 골라야 합니다")
                data = intake.normalize(data, header_row, cols, product_name.strip()[:60] or "상품")
        else:
            raise ValueError("엑셀 파일을 올리거나 채널 주소를 붙여넣어 주세요")
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, str(e) if isinstance(e, ValueError) else "열 연결 정보 형식이 올바르지 않습니다") from e
    try:
        labels = json.loads(ref_labels)
        assert isinstance(labels, list)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, "참고 사진 이름 형식이 올바르지 않습니다") from e
    if len(refs) > security.MAX_IMAGES:
        raise HTTPException(400, f"참고 사진은 최대 {security.MAX_IMAGES}장입니다")
    images = []
    for i, f in enumerate(refs):
        b = await f.read(security.MAX_IMG + 1)
        if not security.image_ext(b):
            raise HTTPException(400, "참고 사진은 5MB 이하의 png/jpg/webp/gif만 가능합니다")
        label = str(labels[i]).strip()[:80] if i < len(labels) else "상품"
        images.append((label or "상품", b))
    brand_words = brand.replace("\n", ",").split(",")
    product_words = product.replace("\n", ",").split(",")
    _job_id, token = jobs.create_job(store, data, brand_words, product_words, images, industry=industry,
                                     title_keywords=title_words, window_days=window, since_date=since)
    return {"token": token}


@app.get("/api/job/quote")
def job_quote(request: Request, cap_usd: float = 0, x_job_token: str = Header(None)):
    job = auth(request, x_job_token)
    d = jobs.job_dir(job["id"])
    cal = quote.load_calibration(jobs.DATA / "calibration.json")
    if (d / "work" / "screen.json").exists():
        q = quote.exact_quote(d, jobs.read_cfg(job["id"]), cap_usd or None, cal, jobs.MAX_COMMENT_QUOTA, jobs.MAX_LINKS)
    else:
        q = quote.rough_quote(quote.count_input_channels(d / "input.xlsx"), cal)
    q["industry"] = jobs.read_cfg(job["id"]).get("industry", presets.DEFAULT_PRESET)
    q["verified"] = presets.get(q["industry"])["verified"]
    return q


@app.get("/api/job")
def status(request: Request, x_job_token: str = Header(None)):
    return public(auth(request, x_job_token))


@app.post("/api/job/prepare")
def prepare(request: Request, x_job_token: str = Header(None), x_youtube_key: str = Header(None)):
    job = auth(request, x_job_token)
    keys = keys_from(YOUTUBE_API_KEY=x_youtube_key)
    if "YOUTUBE_API_KEY" not in keys:
        raise HTTPException(400, "YouTube API 키가 필요합니다")
    if job["stage"] in ("queued", "preparing", "analyzing", "rechecking"):
        raise HTTPException(409, "이미 실행 중입니다")
    steps = [["all", "--skip-watch", "--xlsx", "input.xlsx", "--config", "config.yaml"]]
    jobs.start(store, job["id"], steps, keys, "preparing", "prepared", lambda: jobs.after_prepare(store, job["id"]))
    return {"ok": True}


@app.post("/api/job/analyze")
def analyze(request: Request, cap_usd: float = Form(...), ack_unverified: str = Form(""), x_job_token: str = Header(None),
            x_openai_key: str = Header(None)):
    job = auth(request, x_job_token)
    check_industry(job, ack_unverified)
    keys = keys_from(OPENAI_API_KEY=x_openai_key)
    if "OPENAI_API_KEY" not in keys:
        raise HTTPException(400, "OpenAI API 키가 필요합니다")
    if job["stage"] not in ("prepared", "done", "failed", "interrupted"):
        raise HTTPException(409, "지금은 영상 분석을 시작할 수 없습니다")
    if not (0.5 <= cap_usd <= jobs.MAX_CAP_USD):
        raise HTTPException(400, f"비용 상한은 $0.5 ~ ${jobs.MAX_CAP_USD:g} 사이여야 합니다")
    cfg = jobs.read_cfg(job["id"])
    est = estimate(jobs.job_dir(job["id"]), cfg, cap_usd)
    if est["per_channel"] < 1:
        raise HTTPException(400, f"상한이 너무 낮아 영상을 한 개도 분석할 수 없습니다 (영상당 약 ${est['cost_per_video']})")
    cfg["max_visual_videos_per_channel"] = est["per_channel"]
    jobs.write_cfg(job["id"], cfg)
    store.update(job["id"], cap_usd=cap_usd, estimate=est)
    jobs.start(store, job["id"], [["watch", "--config", "config.yaml"], ["report", "--config", "config.yaml"]],
               keys, "analyzing", "done")
    return {"ok": True, "estimate": est}


RECHECK_BLOCKED = ("queued", "preparing", "analyzing", "rechecking", "created")


def check_cap(cap_usd):
    if cap_usd and not (0.5 <= cap_usd <= jobs.MAX_CAP_USD):
        raise HTTPException(400, f"비용 상한은 $0.5 ~ ${jobs.MAX_CAP_USD:g} 사이여야 합니다 (영상 분석을 하지 않으려면 비워두세요)")


@app.post("/api/job/recheck-plan")
def recheck_plan(request: Request, cap_usd: float = Form(0), x_job_token: str = Header(None)):
    """재검증을 실행하지 않고 대상 수·쿼터·예상 비용만 보여준다 (키 불필요)."""
    job = auth(request, x_job_token)
    if job["stage"] in RECHECK_BLOCKED:
        raise HTTPException(409, "지금은 계획을 볼 수 없습니다")
    check_cap(cap_usd)
    return PlainTextResponse(jobs.recheck_plan(job["id"], cap_usd))


@app.post("/api/job/recheck")
def recheck(request: Request, cap_usd: float = Form(0), ack_unverified: str = Form(""), x_job_token: str = Header(None),
            x_youtube_key: str = Header(None), x_openai_key: str = Header(None)):
    """미확인 채널 재검증: 댓글 재검토·표기 변형·설명란 링크·분석 서술(+ cap_usd가 있으면 안 본 영상 분석)."""
    job = auth(request, x_job_token)
    if job["stage"] in RECHECK_BLOCKED:
        raise HTTPException(409, "지금은 재검증을 시작할 수 없습니다")
    check_cap(cap_usd)
    if cap_usd:
        check_industry(job, ack_unverified)
    keys = keys_from(YOUTUBE_API_KEY=x_youtube_key, OPENAI_API_KEY=x_openai_key)
    if "YOUTUBE_API_KEY" not in keys:
        raise HTTPException(400, "댓글 재검토에 YouTube API 키가 필요합니다")
    if cap_usd and "OPENAI_API_KEY" not in keys:
        raise HTTPException(400, "영상 분석에는 OpenAI API 키가 필요합니다")
    if not cap_usd:
        keys.pop("OPENAI_API_KEY", None)  # 영상 분석을 안 하면 OpenAI 키는 엔진에 넘기지 않는다
    store.update(job["id"], cap_usd=cap_usd or None)
    jobs.start(store, job["id"], [jobs.recheck_args(cap_usd)], keys, "rechecking", "done")
    return {"ok": True}


@app.get("/api/job/results")
def results(request: Request, x_job_token: str = Header(None)):
    job = auth(request, x_job_token)
    b = jobs.board(job["id"])
    b["records"] = {str(k): v for k, v in store.records(job["id"]).items()}
    return b


@app.put("/api/job/records/{channel_no}")
async def set_record(channel_no: int, request: Request, x_job_token: str = Header(None)):
    job = auth(request, x_job_token)
    body = await request.json()
    value = str(body.get("value", ""))
    if value not in RECORD_VALUES:
        raise HTTPException(400, "허용되지 않은 값입니다")
    store.set_record(job["id"], channel_no, value)
    return {"ok": True}


@app.get("/api/job/log")
def log(request: Request, x_job_token: str = Header(None)):
    job = auth(request, x_job_token)
    p = jobs.job_dir(job["id"]) / "log.txt"
    text = p.read_text(encoding="utf-8", errors="replace")[-4000:] if p.exists() else ""
    return PlainTextResponse(text)


@app.post("/api/job/export")
def export(request: Request, x_job_token: str = Header(None)):
    job = auth(request, x_job_token)
    rows = jobs.board(job["id"])["rows"] or []
    rec = store.records(job["id"])
    lines = ["번호,유튜버,자동 판정,근거,확인 결과"]
    for r in rows:
        name = '"' + str(r["name"]).replace('"', '""') + '"'
        ev = '"' + str(r["ev"]).replace('"', '""') + '"'
        lines.append(",".join([str(r["n"]), name, r["st"], ev, rec.get(r["n"], "")]))
    return PlainTextResponse("﻿" + "\n".join(lines), media_type="text/csv; charset=utf-8")


@app.delete("/api/job")
def delete(request: Request, x_job_token: str = Header(None)):
    job = auth(request, x_job_token)
    jobs.delete_job(store, job["id"])
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/static", StaticFiles(directory=WEB), name="static")
