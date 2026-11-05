"""PC 도우미: 영상 분석을 사용자 PC에서 돌릴 수 있게 분석 패키지(zip)를 만들고, 돌려받은 결과를 검증해서 합친다.

YouTube가 클라우드 IP의 영상 다운로드를 막으므로(배포 환경 진단으로 확인), 영상 다운로드가 필요한 단계만 사용자 PC로 분리한다.
패키지에는 API 키도 작업 토큰도 넣지 않는다. 결과 파일은 서버가 필드 단위로 다시 걸러서(화이트리스트) 합친다.
"""
import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

from . import jobs
from .estimate import ROOT, estimate
from .quote import COST_PER_VIDEO

MAX_PACKAGE = 60 * 1024 * 1024
MAX_RESULT = 25 * 1024 * 1024
CONF = {"none", "low", "medium", "high"}
PKG_DIR = "seeding-helper"
HELPER_FILES = ["run_helper.py", "실행.bat", "읽어보세요.txt"]


def expected_sig(job_id):
    """이 작업 설정의 영상 분석 결과 지문. 사용자 PC 결과가 같은 설정·같은 엔진으로 만들어졌는지 확인하는 데 쓴다."""
    code = (f"import sys;sys.path.insert(0,{str(ROOT)!r});import seeding_checker as s;"
            "print(s.config_sig(s.load_cfg('config.yaml')))")
    p = subprocess.run([*jobs.PY, "-c", code], cwd=jobs.job_dir(job_id), env=jobs._env({}), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    sig = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ""
    if p.returncode != 0 or not re.fullmatch(r"[0-9a-f]{6,40}", sig):
        raise RuntimeError("설정 지문을 계산하지 못했습니다")
    return sig


def build_package(job_id, cap_usd):
    """분석 대상 영상 수를 비용 상한에 맞춰 정하고, 사용자 PC에서 실행할 패키지를 zip 으로 돌려준다."""
    d = jobs.job_dir(job_id)
    cfg = jobs.read_cfg(job_id)
    est = estimate(d, cfg, cap_usd, COST_PER_VIDEO)
    if est["per_channel"] < 1:
        raise ValueError(f"상한이 너무 낮아 영상을 한 개도 분석할 수 없습니다 (영상당 약 ${COST_PER_VIDEO})")
    cfg["max_visual_videos_per_channel"] = est["per_channel"]
    cfg["cookies_from_browser"] = ""
    jobs.write_cfg(job_id, cfg)
    sig = expected_sig(job_id)
    manifest = {"format": 1, "expected_sig": sig, "cap_usd": cap_usd, "per_channel": est["per_channel"], "videos": est["videos"],
                "est_usd": round(est["videos"] * COST_PER_VIDEO, 2)}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        def add(src, name):
            z.write(src, f"{PKG_DIR}/{name}")
        add(ROOT / "seeding_checker.py", "seeding_checker.py")
        add(ROOT / "requirements.txt", "requirements.txt")
        for f in HELPER_FILES:
            add(ROOT / "helper" / f, f)
        add(d / "config.yaml", "config.yaml")
        for name in ("channels.json", "screen.json", "visual.json"):
            if (d / "work" / name).exists():
                add(d / "work" / name, f"work/{name}")
        ref = d / "reference"
        if ref.exists():
            for f in sorted(ref.iterdir()):
                if f.is_file():
                    add(f, f"reference/{f.name}")
        z.writestr(f"{PKG_DIR}/manifest.json", json.dumps(manifest, ensure_ascii=False))
    data = buf.getvalue()
    if len(data) > MAX_PACKAGE:
        raise ValueError("패키지가 너무 큽니다. 채널 수를 줄여 다시 시도하세요")
    return data, manifest


def _s(v, n):
    return v[:n] if isinstance(v, str) else ""


def sanitize_entry(r):
    """사용자 PC가 올린 영상 1개 결과를 알려진 필드만 남기고 길이를 제한해서 다시 만든다. 형식이 틀리면 None"""
    if not isinstance(r, dict) or not isinstance(r.get("worn"), list) or not isinstance(r.get("_sig"), str):
        return None
    worn = []
    for w in r["worn"][:12]:
        if not isinstance(w, dict) or w.get("confidence") not in CONF:
            return None
        item = {"product": _s(w.get("product"), 200), "confidence": w["confidence"], "evidence": _s(w.get("evidence"), 2000),
                "frames": [int(x) for x in (w.get("frames") or [])[:64] if isinstance(x, (int, float)) and not isinstance(x, bool)],
                "at_sec": w["at_sec"] if isinstance(w.get("at_sec"), (int, float)) and not isinstance(w.get("at_sec"), bool) else None}
        v = w.get("verify")
        if isinstance(v, dict):
            item["verify"] = {"verdict": _s(v.get("verdict"), 20), "distinctive_visible": bool(v.get("distinctive_visible")),
                              "matching": [_s(x, 300) for x in (v.get("matching") or [])[:10]],
                              "differences": [_s(x, 300) for x in (v.get("differences") or [])[:10]]}
        worn.append(item)
    nf = r.get("n_frames")
    return {"seen": _s(r.get("seen"), 4000), "worn": worn, "note": _s(r.get("note"), 1500),
            "_sig": r["_sig"][:40], "n_frames": nf if isinstance(nf, int) and not isinstance(nf, bool) and 0 <= nf <= 500 else 0}


def merge_result(job_id, raw: bytes):
    """result.json 을 검증해 work/visual.json 에 합친다. {accepted, rejected_sig, rejected_unknown, rejected_bad} 를 돌려준다."""
    if len(raw) > MAX_RESULT:
        raise ValueError("결과 파일이 너무 큽니다(25MB 초과)")
    try:
        data = json.loads(raw.decode("utf-8"))
        visual = data["visual"]
        assert data.get("format") == 1 and isinstance(visual, dict)
    except Exception as e:  # noqa: BLE001
        raise ValueError("PC 도우미가 만든 result.json 이 아닙니다") from e
    d = jobs.job_dir(job_id)
    screen = json.loads((d / "work" / "screen.json").read_text(encoding="utf-8"))
    allowed = {k: {v["id"] for v in e.get("videos", [])} for k, e in screen.items()}
    sig = expected_sig(job_id)
    path = d / "work" / "visual.json"
    merged = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    c = {"accepted": 0, "rejected_sig": 0, "rejected_unknown": 0, "rejected_bad": 0}
    for ch, vids in visual.items():
        if not isinstance(vids, dict):
            c["rejected_bad"] += 1
            continue
        for vid, r in vids.items():
            if ch not in allowed or vid not in allowed[ch]:
                c["rejected_unknown"] += 1
                continue
            clean = sanitize_entry(r)
            if clean is None:
                c["rejected_bad"] += 1
            elif clean["_sig"] != sig:
                c["rejected_sig"] += 1
            else:
                merged.setdefault(ch, {})[vid] = clean
                c["accepted"] += 1
    if c["accepted"]:
        path.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
    return c
