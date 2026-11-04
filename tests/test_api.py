import io
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

import conftest
from app.main import app, limiter

YT = "AIzaFAKEYOUTUBEKEY1234567890abcdef"
OA = "sk-FAKEOPENAIKEY1234567890abcdefgh"
client = TestClient(app)


def xlsx_bytes():
    wb = Workbook()
    ws = wb.active
    ws.append(["제목"])
    ws.append(["발송일", "번호", "유튜브 아이디", "상품명[색상]", "옵션", "링크"])
    ws.append(["260513", 1, "a", "예시 티셔츠[화이트]", "L", "https://www.youtube.com/@a"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def new_job(brand="브랜드명, brandname"):
    r = client.post("/api/job", data={"brand": brand, "product": "예시티"},
                    files={"xlsx": ("a.xlsx", xlsx_bytes(), "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def H(token, **extra):
    return {"X-Job-Token": token, **extra}


def wait(token, stages, timeout=30):
    end = time.time() + timeout
    s = None
    while time.time() < end:
        s = client.get("/api/job", headers=H(token)).json()
        if s["stage"] in stages:
            return s
        time.sleep(0.2)
    raise AssertionError(f"timeout, last={s}")


@pytest.fixture(autouse=True)
def reset_limiter():
    limiter.hits.clear()


def all_bytes_under_data():
    blob = b""
    for p in Path(conftest.TMP).rglob("*"):
        if p.is_file():
            blob += p.read_bytes()
    return blob


def test_security_headers_and_index():
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store" and "script-src 'self'" in r.headers["content-security-policy"]


def test_bad_token_and_missing_token():
    assert client.get("/api/job").status_code == 404
    assert client.get("/api/job", headers=H("x" * 40)).status_code == 404


def test_rate_limit_after_failures():
    codes = [client.get("/api/job", headers=H("bad" * 15)).status_code for _ in range(12)]
    assert codes[0] == 404 and codes[-1] == 429


def test_upload_validation():
    r = client.post("/api/job", data={"brand": "b"}, files={"xlsx": ("a.xlsx", b"junk", "x")})
    assert r.status_code == 400
    r = client.post("/api/job", data={"brand": "b"},
                    files=[("xlsx", ("a.xlsx", xlsx_bytes(), "x")), ("refs", ("evil.png", b"<html>x</html>", "image/png"))])
    assert r.status_code == 400


def test_prepare_requires_valid_key():
    t = new_job()
    assert client.post("/api/job/prepare", headers=H(t)).status_code == 400
    assert client.post("/api/job/prepare", headers=H(t, **{"X-Youtube-Key": "short"})).status_code == 400


def test_full_flow_and_key_never_persisted():
    t = new_job()
    assert client.post("/api/job/prepare", headers=H(t, **{"X-Youtube-Key": YT})).status_code == 200
    s = wait(t, {"prepared", "failed"})
    assert s["stage"] == "prepared", s
    assert s["estimate"]["channels"] == 1 and s["estimate"]["videos_all"] == 5
    res = client.get("/api/job/results", headers=H(t)).json()
    assert res["counts"]["확인됨"] == 1 and res["counts"]["미확인"] == 1
    assert client.put("/api/job/records/1", headers=H(t), json={"value": "착용 확인"}).status_code == 200
    assert client.put("/api/job/records/1", headers=H(t), json={"value": "<script>"}).status_code == 400
    assert client.get("/api/job/results", headers=H(t)).json()["records"] == {"1": "착용 확인"}
    csv = client.post("/api/job/export", headers=H(t)).content.decode("utf-8-sig")
    assert "채널A" in csv and "착용 확인" in csv
    # 영상 분석: 상한 검증과 시작
    assert client.post("/api/job/analyze", headers=H(t, **{"X-Openai-Key": OA}), data={"cap_usd": "0.1"}).status_code == 400
    assert client.post("/api/job/analyze", headers=H(t), data={"cap_usd": "5"}).status_code == 400
    r = client.post("/api/job/analyze", headers=H(t, **{"X-Openai-Key": OA}), data={"cap_usd": "0.5"})
    assert r.status_code == 200, r.text
    assert r.json()["estimate"]["per_channel"] == 5  # 영상 5개 x $0.03 = $0.15 <= $0.5
    assert wait(t, {"done", "failed"})["stage"] == "done"
    # 키 값은 DB·로그·설정·작업 폴더·응답 어디에도 없어야 한다
    blob = all_bytes_under_data()
    for secret in (YT, OA):
        assert secret.encode() not in blob
    log = client.get("/api/job/log", headers=H(t)).text
    assert "***" in log and YT not in log and OA not in log


def test_engine_failure_message_is_redacted():
    t = new_job()
    bad = "FAIL" + YT
    client.post("/api/job/prepare", headers=H(t, **{"X-Youtube-Key": bad}))
    s = wait(t, {"prepared", "failed"})
    assert s["stage"] == "failed"
    assert bad not in s["message"] and "key=***" in s["message"]
    assert bad.encode() not in all_bytes_under_data()


def test_recheck_and_delete():
    t = new_job()
    client.post("/api/job/prepare", headers=H(t, **{"X-Youtube-Key": YT}))
    wait(t, {"prepared"})
    r = client.post("/api/job/recheck", headers=H(t))
    assert r.status_code == 200 and r.json()["channels"] == 1
    assert wait(t, {"done", "failed"})["stage"] == "done"
    assert client.delete("/api/job", headers=H(t)).status_code == 200
    assert client.get("/api/job", headers=H(t)).status_code == 404


def test_config_is_server_built_and_safe():
    new_job("랑가,\x00bad\x07," + "x" * 100)
    cfgs = [p.read_text(encoding="utf-8") for p in Path(conftest.TMP, "jobs").glob("*/config.yaml")]
    mine = [c for c in cfgs if "랑가" in c]
    assert mine
    for cfg in mine:
        assert "cookies_from_browser: ''" in cfg or 'cookies_from_browser: ""' in cfg
        assert "x" * 100 not in cfg and "\x07" not in cfg and "\x00" not in cfg
