import json

from app import jobs, quote
from app.quote import COST_PER_VIDEO, SECONDS_PER_VIDEO
import conftest  # noqa: F401  (DATA_DIR 등 환경변수 설정)
import seeding_checker as sc

CFG = {"brand_keywords": ["하늘마루"], "product_keywords": [], "window_days": 180, "visual_window_days": 60,
       "max_duration_min": 20, "outfit_title_keywords": [], "max_visual_videos_per_channel": 12, "provider": "openai",
       "model": "m", "reference_images": {}, "default_since": "2026-05-13T00:00:00+09:00"}


def make_job(tmp_path, n_channels=2, n_videos=6, with_comments=False):
    d = tmp_path / "job"
    (d / "work").mkdir(parents=True)
    ch, scr = [], {}
    for i in range(1, n_channels + 1):
        ch.append({"no": i, "name": f"채널{i}", "ship_date": "2026-05-13T00:00:00+09:00", "url": "u", "items": []})
        vids = [{"id": f"v{i}_{j}", "url": "u", "title": "일상", "description": f"링크 https://shop.test/p{i}_{j}", "tags": [],
                 "published": "2026-05-20T00:00:00Z", "duration": 300,
                 **({"comments": {"owner": [], "other": []}} if with_comments else {})} for j in range(n_videos)]
        scr[str(i)] = {"channel": {}, "error": None, "videos": vids, "since_assumed": False}
    (d / "work" / "channels.json").write_text(json.dumps(ch, ensure_ascii=False), encoding="utf-8")
    (d / "work" / "screen.json").write_text(json.dumps(scr, ensure_ascii=False), encoding="utf-8")
    (d / "work" / "videos.json").write_text(json.dumps(scr, ensure_ascii=False), encoding="utf-8")
    return d


def test_rough_quote_scales_with_channels():
    cal = {}
    q = quote.rough_quote(93, cal)
    # 패션 93명 실측(영상 705개, 약 $21.5, 54분)과 같은 자릿수여야 한다
    assert 600 <= q["analysis"]["videos"] <= 800 and 18 <= q["analysis"]["usd"] <= 24
    assert 45 <= q["analysis"]["seconds"] / 60 <= 70
    assert 2000 <= q["prepare"]["yt_quota"] <= 3500 and "표본" in q["analysis"]["note"]
    assert quote.rough_quote(10, cal)["analysis"]["usd"] < q["analysis"]["usd"]


def test_calibration_median_replaces_default(tmp_path):
    cal_path = tmp_path / "cal.json"
    job = tmp_path / "jobA"
    (job / "work").mkdir(parents=True)
    metrics = [{"step": "watch", "seconds": 100, "yt_calls": 0, "stats": {"videos": 10}, "at": "t1"},
               {"step": "watch", "seconds": 300, "yt_calls": 0, "stats": {"videos": 10}, "at": "t2"},
               {"step": "all", "seconds": 50, "yt_calls": 9, "stats": {}, "at": "t3"}]
    (job / "work" / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    quote.update_calibration(cal_path, job)
    quote.update_calibration(cal_path, job)  # 같은 기록을 두 번 쌓지 않는다
    cal = quote.load_calibration(cal_path)
    assert cal["seconds_per_video"] == [10.0, 30.0]
    sp, src = quote.seconds_per_video(cal)
    assert sp == 20.0 and "이 서버" in src
    assert quote.seconds_per_video({}) == (SECONDS_PER_VIDEO, "패션 93명 실측(표본 1건)")


def test_exact_quote_counts_videos_cost_time_quota_links(tmp_path):
    d = make_job(tmp_path)
    q = quote.exact_quote(d, CFG, None, {}, max_comment_quota=5, max_links=7)
    assert q["kind"] == "exact" and q["targets"] == 2
    a = q["analysis"]
    assert a["videos_all"] == 12 and a["usd_all"] == round(12 * COST_PER_VIDEO, 2)
    assert a["seconds"] > 0 and "분" in a["time"]
    r = q["recheck"]
    assert r["comment_videos"] == 12 and r["yt_quota"] == 5          # 상한으로 자름
    assert r["links"] == 12 and r["links_followed"] == 7


def test_exact_quote_respects_cap_and_skips_fetched_comments(tmp_path):
    d = make_job(tmp_path, with_comments=True)
    q = quote.exact_quote(d, CFG, 0.1, {}, 2000, 300)               # 상한 $0.1: 채널당 1개씩 2개($0.06)까지, 2개씩이면 $0.12라 넘는다
    assert q["recheck"]["comment_videos"] == 0 and q["recheck"]["yt_quota"] == 0
    assert q["analysis"]["per_channel"] == 1 and q["analysis"]["videos"] == 2 and q["analysis"]["usd"] == 0.06


def test_count_input_channels(tmp_path):
    from app import intake
    p = tmp_path / "in.xlsx"
    p.write_bytes(intake.make_template())
    assert quote.count_input_channels(p) == 2
