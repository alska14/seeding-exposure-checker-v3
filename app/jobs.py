"""작업 폴더 준비와 엔진 실행. 엔진은 작업 폴더를 현재 폴더로 하는 별도 프로세스로 돈다."""
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import yaml

from . import quote, security
from .estimate import ROOT, estimate
from .results import counts, latest_report, read_report

DATA = Path(os.environ.get("DATA_DIR", ROOT / "data")).resolve()
ENGINE = Path(os.environ.get("ENGINE_PATH", ROOT / "seeding_checker.py"))
MAX_JOBS = int(os.environ.get("MAX_JOBS", "1"))
MAX_CAP_USD = float(os.environ.get("MAX_CAP_USD", "50"))
TTL_DAYS = int(os.environ.get("JOB_TTL_DAYS", "7"))
MAX_COMMENT_QUOTA = int(os.environ.get("MAX_COMMENT_QUOTA", "2000"))  # 재검증 한 번에 쓸 YouTube 쿼터 상한
MAX_LINKS = int(os.environ.get("MAX_LINKS", "300"))  # 재검증 한 번에 따라갈 설명란 링크 수
SECRET_ENV = ("YOUTUBE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
_sem = threading.BoundedSemaphore(MAX_JOBS)
# 엔진은 격리 모드(-I)로 실행한다: 작업 폴더의 파일이 import 되지 않게. -I 는 PYTHONIOENCODING 같은 환경변수를 무시하므로 -X utf8 로 한글 출력을 보장한다
PY = [sys.executable, "-I", "-X", "utf8"]

PRICES = {"gpt-5.4-mini": {"input": 0.75, "output": 4.5}, "gpt-5.4": {"input": 2.5, "output": 15.0}}


def job_dir(job_id):
    return DATA / "jobs" / job_id


def clean_words(items, limit=40, width=60):
    out = []
    for s in items or []:
        s = re.sub(r"[\x00-\x1f\x7f]", "", str(s)).strip()
        if s and len(s) <= width and s not in out:
            out.append(s)
    return out[:limit]


def build_config(brand, product, refs, industry="fashion", title_keywords=None, window_days=None, since_date=None):
    base = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    base["brand_keywords"] = clean_words(brand) or ["브랜드"]
    base["product_keywords"] = clean_words(product)
    base["industry"] = industry  # 엔진은 읽지 않는다. 검증되지 않은 업종 안내에 쓴다
    if title_keywords:
        base["outfit_title_keywords"] = clean_words(title_keywords)
    if window_days:
        base["window_days"] = int(window_days)
    if since_date:  # 발송일이 없는 채널에 쓰는 시작일
        base["default_since"] = f"{since_date}T00:00:00+09:00"
    base["reference_images"] = refs
    base["product_hints"] = {}
    base["ground_truth"], base["channel_overrides"], base["manual_notes"] = {}, {}, {}
    base["cookies_from_browser"] = ""  # 서버에서 브라우저 쿠키를 읽지 않는다
    base["prices"] = PRICES
    base["workers"] = 2
    return base


def create_job(store, xlsx: bytes, brand, product, images, industry="fashion", title_keywords=None, window_days=None,
               since_date=None):
    """images = [(product_label, bytes)]. 검증은 호출 전에 끝낸 상태여야 한다."""
    job_id = secrets.token_hex(8)
    token = security.new_token()
    d = job_dir(job_id)
    (d / "reference").mkdir(parents=True)
    (d / "input.xlsx").write_bytes(xlsx)
    refs = {}
    for i, (label, data) in enumerate(images):
        ext = security.image_ext(data)
        name = f"reference/ref_{i + 1}{ext}"
        (d / name).write_bytes(data)
        refs.setdefault(label, []).append(name)
    cfg = build_config(brand, product, refs, industry, title_keywords, window_days, since_date)
    (d / "config.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    store.create_job(job_id, security.hash_token(token), TTL_DAYS)
    return job_id, token


def read_cfg(job_id):
    return yaml.safe_load((job_dir(job_id) / "config.yaml").read_text(encoding="utf-8"))


def write_cfg(job_id, cfg):
    (job_dir(job_id) / "config.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")


def delete_job(store, job_id):
    shutil.rmtree(job_dir(job_id), ignore_errors=True)
    store.delete_job(job_id)


def _env(keys):
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
    env.update(keys)
    return env


def run_steps(store, job_id, steps, keys, running, done, on_success=None):
    """steps: 엔진 인자 목록의 목록. 키는 이 함수가 끝나면 지운다."""
    secret_values = [v for v in keys.values() if v]
    d = job_dir(job_id)
    log = d / "log.txt"
    store.update(job_id, stage="queued", message="순서를 기다리는 중")
    try:
        with _sem:
            store.update(job_id, stage=running, message="")
            for args in steps:
                with open(log, "a", encoding="utf-8") as lf:
                    lf.write(f"\n$ engine {' '.join(args[:1])}\n")
                    p = subprocess.Popen([*PY, str(ENGINE), *args], cwd=d, env=_env(keys),
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         encoding="utf-8", errors="replace")
                    tail = []
                    for line in p.stdout:
                        line = security.redact(line.rstrip(), secret_values)
                        lf.write(line + "\n")
                        lf.flush()
                        if line.strip():
                            tail.append(line.strip())
                            tail = tail[-3:]
                            store.update(job_id, message=line.strip()[:200])
                    code = p.wait()
                if code != 0:
                    msg = " / ".join(tail)[:300] or f"엔진이 종료 코드 {code}로 끝났습니다"
                    store.update(job_id, stage="failed", message=msg)
                    return
            if on_success:
                on_success()
            try:  # 이번 실행의 실제 소요 시간을 견적 보정용으로 쌓는다. 실패해도 작업 결과에는 영향 없음
                quote.update_calibration(DATA / "calibration.json", d)
            except Exception:  # noqa: BLE001
                pass
            store.update(job_id, stage=done, message="")
    except Exception as e:  # noqa: BLE001
        store.update(job_id, stage="failed", message=security.redact(f"{type(e).__name__}: {e}", secret_values)[:300])
    finally:
        keys.clear()


def start(store, job_id, steps, keys, running, done, on_success=None):
    t = threading.Thread(target=run_steps, args=(store, job_id, steps, keys, running, done, on_success), daemon=True)
    t.start()
    return t


def after_prepare(store, job_id):
    """준비 분석 직후: 전체 대상 수와 정가 기준 예상 비용을 저장한다(상한은 분석 시작 때 정한다)."""
    store.update(job_id, estimate=estimate(job_dir(job_id), read_cfg(job_id), MAX_CAP_USD))


def recheck_args(cap_usd):
    """엔진 recheck 명령의 인자. cap_usd가 0이면 영상 분석(--budget)은 하지 않는다."""
    args = ["recheck", "--config", "config.yaml", "--comment-quota", str(MAX_COMMENT_QUOTA), "--max-links", str(MAX_LINKS)]
    if cap_usd and cap_usd > 0:
        args += ["--budget", f"{cap_usd:g}"]
    return args


def recheck_plan(job_id, cap_usd):
    """recheck --dry-run 출력을 돌려준다. 키가 필요 없고 아무것도 호출·저장하지 않는다."""
    p = subprocess.run([*PY, str(ENGINE), *recheck_args(cap_usd), "--dry-run"], cwd=job_dir(job_id), env=_env({}),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    return security.redact((p.stdout + p.stderr)[-4000:])


def board(job_id):
    rep = latest_report(job_dir(job_id))
    rows = read_report(rep) if rep else None
    return {"rows": rows, "counts": counts(rows) if rows else None}
