import re
import time

import conftest
from app import jobs
from app.store import Store
from test_api import YT, H, client, new_job, wait


def job_with_log(tmp_path, log, channels=3):
    d = tmp_path / "j"
    d.mkdir(parents=True, exist_ok=True)
    from app import intake
    (d / "input.xlsx").write_bytes(intake.from_text("\n".join(f"@c{i}" for i in range(channels))))
    (d / "log.txt").write_text(log, encoding="utf-8")
    (d / "config.yaml").write_text("brand_keywords: [하늘마루]\nindustry: beauty\nwindow_days: 90\n", encoding="utf-8")
    return d


def progress_of(monkeypatch, tmp_path, log, **kw):
    d = job_with_log(tmp_path, log, **kw)
    monkeypatch.setattr(jobs, "job_dir", lambda job_id: d)
    return jobs.progress("x")


def test_collect_progress_counts_channels_against_input(monkeypatch, tmp_path):
    log = ("\n$ engine all\n=== parse ===\n[parse] 3명\n\n=== collect ===\n"
           "   1 채널A: 발송 후 영상 12개, 댓글 8개 확인\n   2 채널B: ⚠ 채널 못 찾음\n")
    p = progress_of(monkeypatch, tmp_path, log)
    assert p == {"step": "채널·영상 수집 중", "done": 2, "total": 3}


def test_only_last_run_counts(monkeypatch, tmp_path):
    old = "\n$ engine all\n=== collect ===\n   1 A: 발송 후 영상 1개\n   2 B: 발송 후 영상 1개\n   3 C: 발송 후 영상 1개\n"
    new = "\n$ engine watch\n=== watch ===\n  → 새로 분석할 영상 20개\n  [4/20] 1 채널A · 제목: 안보임\n  [5/20] 1 채널A · 제목: 안보임\n"
    p = progress_of(monkeypatch, tmp_path, old + new)
    assert p == {"step": "영상 분석 중", "done": 5, "total": 20}


def test_recheck_phases_and_latest_counter_wins(monkeypatch, tmp_path):
    log = ("\n$ engine recheck\n=== recheck ===\n[1] 댓글 재검토\n  … 200/1150\n  … 400/1150\n[2] 표기 변형(퍼지) 매칭\n[3] 설명란 링크 추적\n"
           "[5] 안 본 영상 재확인\n  [3/157] 2 채널 · 제목: 안보임\n")
    p = progress_of(monkeypatch, tmp_path, log)
    assert p["step"] == "안 본 영상 분석 중" and (p["done"], p["total"]) == (3, 157)
    p = progress_of(monkeypatch, tmp_path, "\n$ engine recheck\n=== recheck ===\n[1] 댓글 재검토\n  … 400/1150\n")
    assert p["step"] == "댓글 받는 중" and (p["done"], p["total"]) == (400, 1150)


def test_unknown_or_missing_log(monkeypatch, tmp_path):
    p = progress_of(monkeypatch, tmp_path, "아무 줄\n")
    assert p == {"step": "", "done": None, "total": None}
    monkeypatch.setattr(jobs, "job_dir", lambda job_id: tmp_path / "nope")
    assert jobs.progress("x") is None


def test_meta_reads_config_and_input(monkeypatch, tmp_path):
    d = job_with_log(tmp_path, "")
    monkeypatch.setattr(jobs, "job_dir", lambda job_id: d)
    assert jobs.meta("x") == {"brand": "하늘마루", "industry": "beauty", "channels": 3, "window_days": 90}


def test_stage_since_updates_with_stage_only(tmp_path):
    s = Store(tmp_path / "t.db")
    s.create_job("j1", "h1", 7)
    a = s.by_token_hash("h1")["stage_since"]
    time.sleep(0.02)
    s.update("j1", message="진행 중")
    assert s.by_token_hash("h1")["stage_since"] == a                      # 메시지만 바꾸면 그대로
    s.update("j1", stage="preparing")
    assert s.by_token_hash("h1")["stage_since"] > a


def test_status_endpoint_exposes_header_fields_and_progress_while_running():
    t = new_job("하늘마루, sky")
    s = client.get("/api/job", headers=H(t)).json()
    assert s["brand"] == "하늘마루" and s["industry_label"] == "패션·의류" and s["channels"] == 1
    assert s["progress"] is None and s["stage_since"] and s["now"] >= s["stage_since"]
    client.post("/api/job/prepare", headers=H(t, **{"X-Youtube-Key": YT}))
    wait(t, {"prepared"})
    assert client.get("/api/job", headers=H(t)).json()["progress"] is None   # 실행이 끝나면 진행률은 없다
