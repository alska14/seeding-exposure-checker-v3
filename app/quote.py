"""견적: 예상 비용·시간·YouTube 쿼터. 숫자는 모두 '추정'이며, 근거는 실제 실행 기록이다.

기본값은 패션 시딩 93명 1회 실행에서 잰 값이다(표본 1건). 이 서버에서 작업이 끝날 때마다 단계별 실측(work/metrics.json)을
calibration.json 에 쌓고, 있으면 그 중앙값으로 바꿔 쓴다.
"""
import json
import math
import re
import statistics
from pathlib import Path

from . import estimate as est_mod

# 실측 기본값 (패션 시딩 93명, 2026-11-04 실행)
COST_PER_VIDEO = 0.03           # 영상 669개 약 $21.5
SECONDS_PER_VIDEO = 5.0         # 54분 / 669개, 동시 2개 실행 기준
VIDEOS_PER_CHANNEL = 38.3       # 채널당 발송 후 영상 수 평균
COMMENT_VIDEOS_PER_CHANNEL = 23.7   # 채널당 댓글을 받는 영상 수 평균
ANALYZED_PER_CHANNEL = 8.4      # 영상 분석 대상 채널당 분석 영상 수 평균
ANALYZED_CHANNEL_SHARE = 0.91   # 분석 대상이 되는 채널 비율 (84/92)
CAL_KEEP = 20


def prepare_quota_per_channel():
    """채널 해석 1 + 업로드 목록 페이지 + 영상 상세 페이지(50개 단위) + 영상별 댓글 1"""
    pages = math.ceil(VIDEOS_PER_CHANNEL / 50)
    return 1 + pages + pages + COMMENT_VIDEOS_PER_CHANNEL


def load_calibration(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def update_calibration(cal_path, job_dir):
    """작업 폴더의 실행 기록에서 영상당 분석 시간을 뽑아 서버 기록에 쌓는다."""
    try:
        metrics = json.loads((Path(job_dir) / "work" / "metrics.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    cal = load_calibration(cal_path)
    seen = set(cal.get("_seen", []))
    for m in metrics:
        key = f"{job_dir.name}:{m.get('at')}:{m.get('step')}"
        if key in seen:
            continue
        v = (m.get("stats") or {}).get("videos")
        if m.get("step") in ("watch", "recheck") and v and m.get("seconds", 0) > 0 and (m.get("stats") or {}).get("videos"):
            cal.setdefault("seconds_per_video", []).append(round(m["seconds"] / v, 2))
            cal["seconds_per_video"] = cal["seconds_per_video"][-CAL_KEEP:]
        seen.add(key)
    cal["_seen"] = sorted(seen)[-400:]
    Path(cal_path).write_text(json.dumps(cal), encoding="utf-8")


def seconds_per_video(cal):
    vals = cal.get("seconds_per_video") or []
    return (statistics.median(vals), "이 서버의 실제 기록") if vals else (SECONDS_PER_VIDEO, "패션 93명 실측(표본 1건)")


def fmt_minutes(sec):
    m = max(1, round(sec / 60))
    return f"약 {m}분" if m < 90 else f"약 {m / 60:.1f}시간"


def count_input_channels(xlsx_path):
    """엔진 형식(3행부터, B열 번호·C열 이름)의 채널 수"""
    from openpyxl import load_workbook
    ws = load_workbook(xlsx_path, read_only=True).active
    n = 0
    for row in ws.iter_rows(min_row=3, values_only=True):
        b, c = (list(row) + [None, None, None])[1:3]
        if b is not None and re.fullmatch(r"\d+", str(b).strip()) and c:
            n += 1
    return n


def rough_quote(channels, cal, cap_usd=None):
    """준비 분석 전: 채널 수만으로 대략의 견적(표본 1건 평균 기준)"""
    sp, sp_src = seconds_per_video(cal)
    vids = channels * ANALYZED_CHANNEL_SHARE * ANALYZED_PER_CHANNEL
    return {"kind": "rough", "channels": channels,
            "prepare": {"yt_quota": round(channels * prepare_quota_per_channel()), "note": "채널·영상·댓글 수집 (OpenAI 비용 없음)"},
            "analysis": {"videos": round(vids), "usd": round(vids * COST_PER_VIDEO, 2), "seconds": round(vids * sp),
                         "time": fmt_minutes(vids * sp), "note": f"영상 분석(유료). 시간 근거: {sp_src}"},
            "basis": "채널 수만 알 때의 평균 추정입니다. 준비 분석이 끝나면 실제 영상 수로 다시 계산합니다."}


def exact_quote(job_dir, cfg, cap_usd, cal, max_comment_quota, max_links):
    """준비 분석 후: 실제 수집 결과로 계산"""
    job_dir = Path(job_dir)
    sp, sp_src = seconds_per_video(cal)
    cap = cap_usd if cap_usd else 1e9
    plan = est_mod.estimate(job_dir, cfg, cap, COST_PER_VIDEO)
    data = est_mod.eng.jload(job_dir / "work" / "screen.json", {})
    videos = est_mod.eng.jload(job_dir / "work" / "videos.json", {})
    visual = est_mod.eng.jload(job_dir / "work" / "visual.json", {})
    from recheck import collect_links
    targets, cm_todo = {}, 0
    for key, entry in data.items():
        if est_mod.eng.decide(entry, visual.get(key, {}))["status"] == "미확인":
            v = videos.get(key) or entry
            targets[key] = v
            cm_todo += sum(1 for x in v.get("videos", []) if x.get("comments") is None)
    links = collect_links(targets)
    n_links = len(links)
    out = {"kind": "exact", "channels": len(data), "targets": len(targets),
           "analysis": {"videos": plan["videos"], "videos_all": plan["videos_all"], "per_channel": plan["per_channel"],
                        "usd": round(plan["videos"] * COST_PER_VIDEO, 2), "usd_all": round(plan["videos_all"] * COST_PER_VIDEO, 2),
                        "seconds": round(plan["videos"] * sp), "time": fmt_minutes(plan["videos"] * sp),
                        "time_all": fmt_minutes(plan["videos_all"] * sp),
                        "note": f"시간 근거: {sp_src}. 상한이 없으면 전부 분석하는 값(usd_all)을 기준으로 표시"},
           "recheck": {"comment_videos": cm_todo, "yt_quota": min(cm_todo, max_comment_quota), "links": n_links,
                       "links_followed": min(n_links, max_links),
                       "note": "무료 점검(댓글·표기 변형·링크·분석 서술). 시간은 아직 측정되지 않아 표시하지 않습니다."},
           "basis": "실제 수집 결과로 계산한 추정입니다. 영상 길이·네트워크 속도에 따라 달라질 수 있습니다."}
    return out
