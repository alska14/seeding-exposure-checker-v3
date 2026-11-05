import io
import json
from pathlib import Path

import yaml

import conftest
from app import intake
from test_api import OA, YT, H, client, wait, xlsx_bytes


def cfg_of(token_job_dirs_before):
    """방금 만든 작업의 config.yaml"""
    now = {p for p in Path(conftest.TMP, "jobs").iterdir()}
    new = list(now - token_job_dirs_before)
    assert len(new) == 1
    return yaml.safe_load((new[0] / "config.yaml").read_text(encoding="utf-8")), new[0]


def jobs_now():
    p = Path(conftest.TMP, "jobs")
    return {x for x in p.iterdir()} if p.exists() else set()


def test_presets_endpoint():
    r = client.get("/api/presets").json()
    ids = [p["id"] for p in r]
    assert ids[0] == "fashion" and "beauty" in ids and "other" in ids
    assert next(p for p in r if p["id"] == "fashion")["verified"] is True
    assert all(p["verified"] is False for p in r if p["id"] != "fashion")
    assert all(p["title_keywords"] and p["window_days"] for p in r)


def test_template_download_is_valid_standard_workbook():
    r = client.get("/api/template.xlsx")
    assert r.status_code == 200 and "spreadsheetml" in r.headers["content-type"]
    assert intake.preview(r.content)["standard"]


def test_xlsx_preview_returns_mapping_and_rough_quote():
    r = client.post("/api/xlsx-preview", files={"xlsx": ("a.xlsx", intake.make_template(), "x")})
    assert r.status_code == 200
    j = r.json()
    assert j["standard"] and j["channels"] == 2 and j["rough"]["kind"] == "rough"
    assert client.post("/api/xlsx-preview", files={"xlsx": ("a.xlsx", b"junk", "x")}).status_code == 400


def test_quote_preview_by_channel_count():
    assert client.post("/api/quote-preview", data={"channels": "93"}).json()["analysis"]["usd"] > 10
    assert client.post("/api/quote-preview", data={"channels": "0"}).status_code == 400
    assert client.post("/api/quote-preview", data={"channels": "99999"}).status_code == 400


def test_create_from_pasted_channels_applies_preset_and_dates():
    before = jobs_now()
    r = client.post("/api/job", data={"channels_text": "@sky\n하늘 https://www.youtube.com/@moon", "brand": "하늘마루",
                                      "industry": "beauty", "title_keywords": "립틴트, 리뷰", "window_days": "90",
                                      "since_date": "2026-08-01", "product_name": "립틴트"})
    assert r.status_code == 200, r.text
    cfg, d = cfg_of(before)
    assert cfg["industry"] == "beauty" and cfg["outfit_title_keywords"] == ["립틴트", "리뷰"]
    assert cfg["window_days"] == 90 and cfg["default_since"] == "2026-08-01T00:00:00+09:00"
    from app import quote
    assert quote.count_input_channels(d / "input.xlsx") == 2


def test_create_uses_preset_defaults_when_fields_empty():
    before = jobs_now()
    r = client.post("/api/job", data={"channels_text": "@a", "brand": "b", "industry": "food"})
    assert r.status_code == 200
    cfg, _ = cfg_of(before)
    assert "먹방" in cfg["outfit_title_keywords"] and cfg["window_days"] == 120


def test_create_with_column_mapping():
    rows = io.BytesIO()
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["유튜버", "채널 주소", "제품"])
    ws.append(["하늘", "https://www.youtube.com/@sky", "티셔츠"])
    wb.save(rows)
    pv = client.post("/api/xlsx-preview", files={"xlsx": ("a.xlsx", rows.getvalue(), "x")}).json()
    assert not pv["standard"] and pv["map"]["url"] == 1
    before = jobs_now()
    r = client.post("/api/job", data={"brand": "b", "mapping": json.dumps({"header_row": pv["header_row"], "map": pv["map"]})},
                    files={"xlsx": ("a.xlsx", rows.getvalue(), "x")})
    assert r.status_code == 200, r.text
    _, d = cfg_of(before)
    from app import quote
    assert quote.count_input_channels(d / "input.xlsx") == 1


def test_create_input_validation():
    assert client.post("/api/job", data={"brand": "b"}).status_code == 400                                    # 입력 없음
    assert client.post("/api/job", data={"brand": "b", "channels_text": "주소 없는 줄"}).status_code == 400
    assert client.post("/api/job", data={"brand": "b", "channels_text": "@a", "window_days": "3"}).status_code == 400
    assert client.post("/api/job", data={"brand": "b", "channels_text": "@a", "since_date": "13/05/2026"}).status_code == 400
    bad_map = json.dumps({"header_row": 1, "map": {"url": None, "name": None}})
    assert client.post("/api/job", data={"brand": "b", "mapping": bad_map},
                       files={"xlsx": ("a.xlsx", xlsx_bytes(), "x")}).status_code == 400
    assert client.post("/api/job", data={"brand": "b", "mapping": "{not json"},
                       files={"xlsx": ("a.xlsx", xlsx_bytes(), "x")}).status_code == 400


def prepared(industry):
    r = client.post("/api/job", data={"brand": "b", "channels_text": "@a", "industry": industry})
    t = r.json()["token"]
    client.post("/api/job/prepare", headers=H(t, **{"X-Youtube-Key": YT}))
    assert wait(t, {"prepared", "failed"})["stage"] == "prepared"
    return t


def test_job_quote_rough_before_and_exact_after_prepare():
    r = client.post("/api/job", data={"brand": "b", "channels_text": "@a\n@b"})
    t = r.json()["token"]
    q = client.get("/api/job/quote", headers=H(t)).json()
    assert q["kind"] == "rough" and q["channels"] == 2 and q["verified"] is True
    client.post("/api/job/prepare", headers=H(t, **{"X-Youtube-Key": YT}))
    wait(t, {"prepared"})
    q = client.get("/api/job/quote", headers=H(t), params={"cap_usd": 0.1}).json()
    assert q["kind"] == "exact" and q["analysis"]["videos_all"] == 5 and q["analysis"]["per_channel"] == 3
    assert q["recheck"]["comment_videos"] == 5 and q["industry"] == "fashion"
    assert client.get("/api/job/quote").status_code == 404                                                   # 토큰 없이는 안 됨


def test_unverified_industry_blocks_paid_analysis_until_acknowledged():
    t = prepared("beauty")
    hdr = H(t, **{"X-Openai-Key": OA})
    r = client.post("/api/job/analyze", headers=hdr, data={"cap_usd": "0.5"})
    assert r.status_code == 400 and "검증되지 않았습니다" in r.json()["detail"]
    assert client.post("/api/job/analyze", headers=hdr, data={"cap_usd": "0.5", "ack_unverified": "1"}).status_code == 200
    wait(t, {"done", "failed"})
    # 재검증에서도 영상 분석(상한 있음)은 같은 확인이 필요하고, 무료 점검은 필요 없다
    h2 = H(t, **{"X-Youtube-Key": YT, "X-Openai-Key": OA})
    assert client.post("/api/job/recheck", headers=h2, data={"cap_usd": "1"}).status_code == 400
    assert client.post("/api/job/recheck", headers=h2).status_code == 200
    wait(t, {"done", "failed"})
    assert OA.encode() not in b"".join(p.read_bytes() for p in Path(conftest.TMP).rglob("*") if p.is_file())


def test_fashion_needs_no_acknowledgement():
    t = prepared("fashion")
    assert client.post("/api/job/analyze", headers=H(t, **{"X-Openai-Key": OA}), data={"cap_usd": "0.5"}).status_code == 200
    wait(t, {"done", "failed"})
