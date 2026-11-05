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


def prepared_job():
    t = new_job()
    client.post("/api/job/prepare", headers=H(t, **{"X-Youtube-Key": YT}))
    wait(t, {"prepared"})
    return t


def test_recheck_plan_needs_no_key_and_runs_nothing():
    t = prepared_job()
    r = client.post("/api/job/recheck-plan", headers=H(t), data={"cap_usd": "5"})
    assert r.status_code == 200 and "dry-run" in r.text and YT not in r.text
    assert client.get("/api/job", headers=H(t)).json()["stage"] == "prepared"  # 상태가 바뀌지 않는다
    assert client.post("/api/job/recheck-plan", headers=H(t), data={"cap_usd": "99999"}).status_code == 400


def test_recheck_requires_keys_and_validates_cap():
    t = prepared_job()
    assert client.post("/api/job/recheck", headers=H(t)).status_code == 400                      # YouTube 키 없음
    assert client.post("/api/job/recheck", headers=H(t, **{"X-Youtube-Key": YT}), data={"cap_usd": "5"}).status_code == 400  # 영상 분석인데 OpenAI 키 없음
    assert client.post("/api/job/recheck", headers=H(t, **{"X-Youtube-Key": YT}), data={"cap_usd": "0.1"}).status_code == 400
    assert client.get("/api/job", headers=H(t)).json()["stage"] == "prepared"


def test_recheck_free_runs_without_openai_key_and_updates_board():
    t = prepared_job()
    r = client.post("/api/job/recheck", headers=H(t, **{"X-Youtube-Key": YT, "X-Openai-Key": OA}))
    assert r.status_code == 200
    assert wait(t, {"done", "failed"})["stage"] == "done"
    res = client.get("/api/job/results", headers=H(t)).json()
    assert res["counts"]["의심"] == 1 and res["counts"]["미확인"] == 0           # 재검증이 채널A를 의심으로 올림
    d = next(p for p in Path(conftest.TMP, "jobs").iterdir() if (p / "seen_env.json").exists()
             and json.loads((p / "seen_env.json").read_text(encoding="utf-8"))["cmd"][0] == "recheck")
    seen = json.loads((d / "seen_env.json").read_text(encoding="utf-8"))
    assert seen["yt"] and not seen["oa"] and "--budget" not in seen["cmd"]       # 영상 분석을 안 하면 OpenAI 키는 엔진에 안 넘어간다
    for secret in (YT, OA):
        assert secret.encode() not in all_bytes_under_data()


def test_recheck_with_budget_passes_budget_and_openai_key_then_delete():
    t = prepared_job()
    r = client.post("/api/job/recheck", headers=H(t, **{"X-Youtube-Key": YT, "X-Openai-Key": OA}), data={"cap_usd": "5"})
    assert r.status_code == 200
    assert wait(t, {"done", "failed"})["stage"] == "done"
    d = next(p for p in Path(conftest.TMP, "jobs").iterdir() if (p / "seen_env.json").exists()
             and "--budget" in json.loads((p / "seen_env.json").read_text(encoding="utf-8"))["cmd"])
    seen = json.loads((d / "seen_env.json").read_text(encoding="utf-8"))
    assert seen["oa"] and seen["cmd"][seen["cmd"].index("--budget") + 1] == "5"
    assert OA.encode() not in all_bytes_under_data()
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
