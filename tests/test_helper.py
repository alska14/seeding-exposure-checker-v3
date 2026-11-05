import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import conftest
from app import helper, jobs
from app.main import app
from test_api import OA, YT, H, client, wait
from fastapi.testclient import TestClient  # noqa: F401


@pytest.fixture
def no_server_video(monkeypatch):
    monkeypatch.setattr(jobs, "SERVER_VIDEO", False)


def prepared(industry="fashion"):
    t = client.post("/api/job", data={"brand": "b", "channels_text": "@a", "industry": industry}).json()["token"]
    client.post("/api/job/prepare", headers=H(t, **{"X-Youtube-Key": YT}))
    assert wait(t, {"prepared", "failed"})["stage"] == "prepared"
    return t


def job_dir_of(t):
    """토큰으로 작업 폴더 찾기: 가장 최근에 만든 작업"""
    dirs = sorted((p for p in Path(conftest.TMP, "jobs").iterdir()), key=lambda p: p.stat().st_mtime)
    return dirs[-1]


def make_zip(t, cap="1", **extra):
    r = client.post("/api/job/helper-package", headers=H(t), data={"cap_usd": cap, **extra})
    return r


def good_result(t, sig=None, vid="a0", ch="1", conf="medium"):
    sig = sig or helper.expected_sig(job_dir_of(t).name)
    entry = {"seen": "상의: 검은색", "worn": [{"product": "예시 상품", "confidence": conf, "frames": [1, 2], "evidence": "근거", "at_sec": 12.5,
                                              "verify": {"verdict": "match", "distinctive_visible": True, "matching": ["x"], "differences": []}}],
             "note": "메모", "_sig": sig, "n_frames": 16}
    return {"format": 1, "sig": sig, "visual": {ch: {vid: entry}}}


def upload(t, obj):
    raw = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return client.post("/api/job/helper-result", headers=H(t), files={"result": ("result.json", raw, "application/json")})


def test_server_video_off_blocks_server_side_analysis_and_explains(no_server_video):
    t = prepared()
    r = client.post("/api/job/analyze", headers=H(t, **{"X-Openai-Key": OA}), data={"cap_usd": "1"})
    assert r.status_code == 400 and "내 PC" in r.json()["detail"]
    r = client.post("/api/job/recheck", headers=H(t, **{"X-Youtube-Key": YT, "X-Openai-Key": OA}), data={"cap_usd": "1"})
    assert r.status_code == 400 and "내 PC" in r.json()["detail"]
    r = client.post("/api/job/recheck", headers=H(t, **{"X-Youtube-Key": YT}))      # 무료 점검은 그대로 된다
    assert r.status_code == 200
    assert client.get("/api/config").json() == {"server_video": False}


def test_package_contents_and_no_secrets():
    t = prepared()
    r = make_zip(t, "0.5")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    for need in ("seeding_checker.py", "requirements.txt", "run_helper.py", "실행.bat", "읽어보세요.txt", "config.yaml", "manifest.json",
                 "work/channels.json", "work/screen.json"):
        assert f"seeding-helper/{need}" in names, need
    man = json.loads(z.read("seeding-helper/manifest.json"))
    assert man["per_channel"] == 5 and man["videos"] == 5 and man["est_usd"] == 0.15 and len(man["expected_sig"]) >= 6
    blob = b"".join(z.read(n) for n in names)
    tok_hash_dir = job_dir_of(t).name.encode()
    assert YT.encode() not in blob and OA.encode() not in blob and tok_hash_dir not in blob   # 키·작업 폴더 이름이 없다
    cfg = z.read("seeding-helper/config.yaml").decode("utf-8")
    assert "max_visual_videos_per_channel: 5" in cfg and ("cookies_from_browser: ''" in cfg or 'cookies_from_browser: ""' in cfg)


def test_package_validations():
    t = client.post("/api/job", data={"brand": "b", "channels_text": "@a"}).json()["token"]
    assert make_zip(t).status_code == 409                                         # 준비 분석 전
    t = prepared()
    assert make_zip(t, "0.5").status_code == 200
    assert make_zip(t, "0").status_code == 400 and make_zip(t, "99999").status_code == 400 and make_zip(t, "0.1").status_code == 400
    t2 = prepared("beauty")
    r = make_zip(t2)
    assert r.status_code == 400 and "검증되지 않았습니다" in r.json()["detail"]
    assert make_zip(t2, ack_unverified="1").status_code == 200
    assert client.post("/api/job/helper-package", data={"cap_usd": "1"}).status_code == 404   # 토큰 없이는 안 됨


def test_merge_accepts_valid_result_and_updates_board():
    t = prepared()
    r = upload(t, good_result(t))
    assert r.status_code == 200 and r.json()["accepted"] == 1
    assert wait(t, {"done", "failed"})["stage"] == "done"
    saved = json.loads((job_dir_of(t) / "work" / "visual.json").read_text(encoding="utf-8"))
    e = saved["1"]["a0"]
    assert e["worn"][0]["confidence"] == "medium" and e["worn"][0]["verify"]["verdict"] == "match" and e["n_frames"] == 16


def test_merge_rejects_unknown_ids_wrong_signature_and_bad_shape():
    t = prepared()
    d = job_dir_of(t)
    bad_sig = good_result(t, sig="deadbeef00")
    r = upload(t, bad_sig)
    assert r.status_code == 400 and "설정 지문" in r.json()["detail"]
    assert not (d / "work" / "visual.json").exists()
    r = upload(t, good_result(t, vid="not_a_video"))
    assert r.status_code == 400 and "맞는 결과가 없습니다" in r.json()["detail"]
    r = upload(t, good_result(t, ch="999"))
    assert r.status_code == 400
    assert upload(t, b"not json").status_code == 400
    assert upload(t, {"format": 2, "visual": {}}).status_code == 400
    assert upload(t, good_result(t, conf="certain")).status_code == 400            # 허용되지 않은 값 -> 형식 불량으로 제외
    assert not (d / "work" / "visual.json").exists()


def test_merge_sanitizes_fields_and_limits_sizes():
    t = prepared()
    obj = good_result(t)
    e = obj["visual"]["1"]["a0"]
    e["evil"] = "<script>alert(1)</script>"
    e["worn"][0]["evidence"] = "가" * 5000
    e["worn"][0]["extra"] = {"x": 1}
    e["worn"][0]["frames"] = list(range(500))
    assert upload(t, obj).status_code == 200
    wait(t, {"done", "failed"})
    s = json.loads((job_dir_of(t) / "work" / "visual.json").read_text(encoding="utf-8"))["1"]["a0"]
    assert "evil" not in s and "extra" not in s["worn"][0]
    assert len(s["worn"][0]["evidence"]) == 2000 and len(s["worn"][0]["frames"]) == 64
    huge = b'{"format":1,"visual":{}}' + b" " * (helper.MAX_RESULT + 10)
    assert upload(t, huge).status_code == 400


def test_result_upload_needs_token_and_idle_job():
    t = prepared()
    assert client.post("/api/job/helper-result", files={"result": ("r.json", b"{}", "application/json")}).status_code == 404


def test_helper_script_runs_end_to_end_without_writing_the_key(tmp_path):
    """패키지를 풀어 도우미를 실제로 실행한다(영상 분석 엔진은 가짜로 바꿔서). 키는 파일에 남지 않아야 한다."""
    t = prepared()
    z = zipfile.ZipFile(io.BytesIO(make_zip(t, "1").content))
    z.extractall(tmp_path)
    pkg = tmp_path / "seeding-helper"
    sig = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))["expected_sig"]
    fake = ("import json, os, sys\nfrom pathlib import Path\n"
            "assert os.environ.get('OPENAI_API_KEY'), 'no key'\n"
            f"e={{'seen':'x','worn':[{{'product':'p','confidence':'low','frames':[],'evidence':'e','at_sec':None}}],'note':'','_sig':{sig!r},'n_frames':1}}\n"
            "Path('work').mkdir(exist_ok=True)\nPath('work/visual.json').write_text(json.dumps({'1':{'a0':e}}))\nprint('fake watch ok')\n")
    (pkg / "seeding_checker.py").write_text(fake, encoding="utf-8")
    secret = "sk-HELPERTESTKEY1234567890abcdef"
    env = {**{k: v for k, v in os.environ.items() if k in ("SYSTEMROOT", "PATH", "TEMP", "TMP", "HOME", "USERPROFILE")},
           "OPENAI_API_KEY": secret, "PYTHONIOENCODING": "utf-8"}
    # 환경 점검 단계가 이 PC에서 통과하도록(ffmpeg 등) 점검 함수만 건너뛴다
    runner = ("import runpy,sys\nsys.argv=['run_helper.py']\nimport builtins\nbuiltins.input=lambda *a: ''\n"
              "ns=runpy.run_path('run_helper.py', run_name='not_main')\nns['check_environment']=lambda: None\n"
              "ns['main'].__globals__['check_environment']=lambda: None\nns['main']()\n")
    p = subprocess.run([sys.executable, "-c", runner], cwd=pkg, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "fake watch ok" in p.stdout and "분석한 영상 1개" in p.stdout
    result = json.loads((pkg / "result.json").read_text(encoding="utf-8"))
    assert result["format"] == 1 and result["sig"] == sig and result["visual"]["1"]["a0"]["worn"][0]["confidence"] == "low"
    for f in pkg.rglob("*"):
        if f.is_file():
            assert secret.encode() not in f.read_bytes(), f
    assert secret not in p.stdout + p.stderr
    # 만든 result.json 을 서버가 그대로 받아들인다
    r = upload(t, (pkg / "result.json").read_bytes())
    assert r.status_code == 200 and r.json()["accepted"] == 1
