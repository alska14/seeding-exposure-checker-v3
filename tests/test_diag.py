from fastapi.testclient import TestClient

from app import diag
from app.main import app

client = TestClient(app)


def test_diag_is_hidden_unless_token_configured(monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.get("/api/diag/download", headers={"X-Diag-Token": "x"}).status_code == 404


def test_diag_requires_matching_token(monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "secret-token-value")
    monkeypatch.setattr(diag, "check_download", lambda: {"ok": True, "seconds": 1.0})
    assert client.get("/api/diag/download").status_code == 404
    assert client.get("/api/diag/download", headers={"X-Diag-Token": "wrong"}).status_code == 404
    r = client.get("/api/diag/download", headers={"X-Diag-Token": "secret-token-value"})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_check_download_reports_failure_without_raising(monkeypatch):
    monkeypatch.setattr(diag, "run", lambda cmd, timeout: (1, "ERROR: Sign in to confirm you're not a bot"))
    out = diag.check_download()
    assert out["ok"] is False and out["download_ok"] is False and "not a bot" in out["download_log"]


def test_check_download_success_path(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, timeout):
        calls.append(cmd[0])
        for a in cmd:
            if str(a).endswith("v.%(ext)s"):
                from pathlib import Path
                Path(str(a).replace("%(ext)s", "mp4")).write_bytes(b"x" * 10)
            if str(a).endswith("f.jpg"):
                from pathlib import Path
                Path(str(a)).write_bytes(b"jpg")
        return 0, ""
    monkeypatch.setattr(diag, "run", fake_run)
    out = diag.check_download()
    assert out["ok"] is True and out["download_bytes"] == 10 and out["frame_ok"] is True
