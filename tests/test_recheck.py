import json
import os
from argparse import Namespace

import pytest

import recheck as rc
import seeding_checker as sc

CFG = {"brand_keywords": ["하늘마루", "zenfield"], "product_keywords": ["풋볼져지"], "window_days": 180, "visual_window_days": 60,
       "max_duration_min": 20, "outfit_title_keywords": ["코디"], "provider": "openai", "model": "m", "reference_images": {},
       "default_since": "2026-05-13T00:00:00+09:00"}


def video(vid, title="영상", desc="", comments=None, published="2026-05-20T00:00:00Z", duration=600):
    v = {"id": vid, "url": f"https://www.youtube.com/watch?v={vid}", "title": title, "description": desc, "tags": [],
         "published": published, "duration": duration}
    if comments is not None:
        v["comments"] = comments
    return v


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """채널 3곳: 1=표기 변형 설명, 2=아무 단서 없음, 3=이미 확인됨(텍스트). 작업 폴더를 tmp 로 옮긴다."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "work").mkdir()
    chans = [{"no": n, "name": f"채널{n}", "ship_date": "2026-05-13T00:00:00+09:00", "url": f"https://www.youtube.com/@c{n}",
              "items": [{"name": "예시 상품 [블랙]", "option": ""}], "couple": False, "memo": ""} for n in (1, 2, 3)]
    vids = {
        "1": {"channel": {"channel_id": "UC1", "title": "채널1"}, "error": None, "since_assumed": False,
              "videos": [video("a1", desc="협찬 하늘마류 제품입니다", comments={"owner": [], "other": []}), video("a2")]},
        "2": {"channel": {"channel_id": "UC2", "title": "채널2"}, "error": None, "since_assumed": False,
              "videos": [video("b1", comments={"owner": [], "other": []}), video("b2", title="오늘의 코디")]},
        "3": {"channel": {"channel_id": "UC3", "title": "채널3"}, "error": None, "since_assumed": False,
              "videos": [video("c1", desc="하늘마루 협찬", comments={"owner": [], "other": []})]},
    }
    sc.jdump(chans, sc.WORK / "channels.json")
    sc.jdump(vids, sc.WORK / "videos.json")
    sc.cmd_screen(Namespace(only=None, transcripts=False), CFG)
    return tmp_path


def args(**kw):
    base = dict(dry_run=False, budget=None, steps="comments,fuzzy,links,seen,watch", comment_quota=3000, comment_pages=1,
                max_links=300, only=None, transcripts=False)
    base.update(kw)
    return Namespace(**base)


# ───── 퍼지 매칭
def test_fuzzy_korean_variants():
    assert "하늘마류" in rc.fuzzy_find(sc, "이 옷은 하늘마류 거예요", "하늘마루")
    assert "하늘마룽" in rc.fuzzy_find(sc, "하늘 마룽 협찬", "하늘마루")  # 공백이 있어도
    assert rc.fuzzy_find(sc, "오늘 점심은 김치찌개", "하늘마루") == []
    assert rc.fuzzy_find(sc, "하늘마루", "하늘마루") == []  # 정확한 표기는 대상이 아님


def test_fuzzy_english_uses_word_boundary_and_first_letter():
    assert rc.fuzzy_find(sc, "zen field notes", "zenfield") == []   # 띄어 쓴 다른 단어는 제외
    assert rc.fuzzy_find(sc, "enfield", "zenfield") == []             # 첫 글자가 다르면 제외
    assert "zenfeld" in rc.fuzzy_find(sc, "wearing zenfeld today", "zenfield")
    assert rc.fuzzy_find(sc, "zenfield", "zenfield") == []


def test_dist_le():
    assert rc.dist_le("abcd", "abxd", 1) and not rc.dist_le("abcd", "axyd", 1) and rc.dist_le("abcd", "abc", 1)


# ───── 링크
def test_extract_links_skips_social_and_maps():
    t = "보기 https://youtu.be/x https://www.instagram.com/a https://maps.app.goo.gl/q https://bit.ly/3abc). 끝 https://musinsa.com/p/1,"
    assert rc.extract_links(t) == ["https://bit.ly/3abc", "https://musinsa.com/p/1"]
    assert rc.is_shop("https://www.musinsa.com/x") and not rc.is_shop("https://example.com")


def test_step_links_finds_brand_in_final_page(ws, monkeypatch):
    videos = sc.jload(sc.WORK / "videos.json", {})
    videos["2"]["videos"][0]["description"] = "구매 https://bit.ly/aaa 그리고 https://example.com/zzz"
    pages = {"https://bit.ly/aaa": ("https://shop.test/p?brand=x", "<title>하늘마루 반팔</title>"),
             "https://example.com/zzz": ("https://example.com/zzz", "nothing")}
    monkeypatch.setattr(rc, "resolve_link", lambda u: pages[u])
    cache = {}
    found, total, n = rc.step_links(sc, CFG, {"2": videos["2"]}, 10, cache)
    assert found == {"2": {"b1": ["하늘마루"]}} and total == 2 and n == 2
    assert cache["https://bit.ly/aaa"]["final"].startswith("https://shop.test")
    found2, _, n2 = rc.step_links(sc, CFG, {"2": videos["2"]}, 10, cache)  # 캐시: 다시 따라가지 않는다
    assert n2 == 0 and found2 == found


def test_step_links_failure_is_recorded_not_raised(ws, monkeypatch):
    videos = sc.jload(sc.WORK / "videos.json", {})
    videos["2"]["videos"][0]["description"] = "https://bit.ly/zzz"

    def boom(u):
        raise ConnectionError("x")
    monkeypatch.setattr(rc, "resolve_link", boom)
    cache = {}
    found, _, _ = rc.step_links(sc, CFG, {"2": videos["2"]}, 10, cache)
    assert found == {} and cache["https://bit.ly/zzz"]["ok"] is False


# ───── 판정(decide) 규칙
def entry_with(**sig):
    base = {"brand_hits": [], "product_hits": [], "disclosure": [], "transcript_hits": [], "comment_hits": [],
            "comment_other_hits": [], "link_hits": [], "fuzzy_hits": [], "seen_hits": []}
    base.update(sig)
    return {"channel": {}, "error": None, "videos": [{"id": "v", "url": "u", "title": "t", "duration": 1, "published": "2026-05-20T00:00:00Z", "signals": base}]}


def test_decide_new_signals_become_suspect_in_order():
    assert sc.decide(entry_with(link_hits=["하늘마루"]), {})["status"] == "의심(수동확인)"
    assert sc.decide(entry_with(fuzzy_hits=["하늘마루≈하늘마류"]), {})["basis"].startswith("표기 변형")
    assert sc.decide(entry_with(seen_hits=["풋볼져지"]), {})["basis"].startswith("분석 서술")
    both = sc.decide(entry_with(link_hits=["하늘마루"], seen_hits=["풋볼져지"], product_hits=["풋볼져지"]), {})
    assert both["basis"].startswith("링크")
    assert sc.decide(entry_with(), {})["status"] == "미확인"
    assert sc.decide(entry_with(brand_hits=["하늘마루"], link_hits=["하늘마루"]), {})["status"] == "확인됨(텍스트)"  # 정확한 표기가 우선


def test_decide_old_entries_without_new_keys_still_work():
    e = entry_with()
    for k in ("link_hits", "fuzzy_hits", "seen_hits"):
        del e["videos"][0]["signals"][k]
    assert sc.decide(e, {})["status"] == "미확인"


def test_screen_attaches_recheck_json(ws):
    sc.jdump({"2": {"b1": {"link": ["하늘마루"], "fuzzy": [], "seen": []}}}, sc.WORK / "recheck.json")
    sc.cmd_screen(Namespace(only=None, transcripts=False), CFG)
    e = sc.jload(sc.WORK / "screen.json", {})["2"]
    assert e["videos"][0]["signals"]["link_hits"] == ["하늘마루"]
    assert sc.decide(e, {})["status"] == "의심(수동확인)"


# ───── 댓글
def test_fetch_comment_pages_paginates_and_splits_owner(monkeypatch):
    calls = []

    def fake_yt(endpoint, **p):
        calls.append(p.get("pageToken"))
        mk = lambda a, t: {"snippet": {"topLevelComment": {"snippet": {"authorChannelId": {"value": a}, "textOriginal": t}}}}  # noqa: E731
        if p.get("pageToken") is None:
            return {"items": [mk("OWN", "하늘마루 협찬"), mk("X", "어디꺼예요")], "nextPageToken": "T2"}
        return {"items": [mk("Y", "예뻐요")]}
    monkeypatch.setattr(sc, "yt", fake_yt)
    r = rc.fetch_comment_pages(sc, "vid", "OWN", 3)
    assert r["owner"] == ["하늘마루 협찬"] and r["other"] == ["어디꺼예요", "예뻐요"] and calls == [None, "T2"]


def test_fetch_comment_pages_quota_and_disabled(monkeypatch):
    monkeypatch.setattr(sc, "yt", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("commentThreads 403: quotaExceeded")))
    sc.QUOTA["out"] = False
    assert rc.fetch_comment_pages(sc, "v", "o", 1) is None and sc.QUOTA["out"] is True
    sc.QUOTA["out"] = False
    monkeypatch.setattr(sc, "yt", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("commentThreads 403: commentsDisabled")))
    assert rc.fetch_comment_pages(sc, "v", "o", 1) == {"owner": [], "other": [], "disabled": True}


def test_fetch_comment_pages_network_error_retries_then_skips(monkeypatch):
    import requests
    monkeypatch.setattr(rc.time, "sleep", lambda s: None)
    calls = []

    def flaky(endpoint, **p):
        calls.append(1)
        raise requests.ReadTimeout("timed out")
    monkeypatch.setattr(sc, "yt", flaky)
    assert rc.fetch_comment_pages(sc, "v", "o", 1) is None and len(calls) == 3  # 3번 시도 후 건너뜀(미수집으로 남김)
    n = {"i": 0}

    def once(endpoint, **p):
        n["i"] += 1
        if n["i"] == 1:
            raise requests.ReadTimeout("x")
        return {"items": []}
    monkeypatch.setattr(sc, "yt", once)
    assert rc.fetch_comment_pages(sc, "v", "o", 1) == {"owner": [], "other": []}


def test_comments_step_survives_network_errors_and_keeps_others(ws, monkeypatch):
    import requests
    monkeypatch.setattr(rc.time, "sleep", lambda s: None)

    def yt(endpoint, **p):
        if p["videoId"] == "a2":
            raise requests.ConnectionError("down")
        return {"items": []}
    monkeypatch.setattr(sc, "yt", yt)
    changed = rc.step_comments(sc, args(), CFG, rc.unconfirmed(sc, CFG))
    v = sc.jload(sc.WORK / "videos.json", {})
    assert v["1"]["videos"][1].get("comments") is None          # 실패한 영상은 미수집으로 남아 다음에 다시 받음
    assert v["2"]["videos"][1]["comments"] == {"owner": [], "other": []} and "2" in changed


def test_comment_targets_prefers_outfit_titles_and_respects_quota(ws):
    videos = sc.jload(sc.WORK / "videos.json", {})
    pick, total = rc.comment_targets(sc, CFG, {"1": {}, "2": {}}, videos, 1)
    assert total == 2 and pick == [("2", "b2")]  # 댓글 없는 영상은 a2, b2(코디 제목). 착장 제목이 먼저


# ───── 영상 재분석 계획
def test_watch_plan_budget_caps_per_channel(ws):
    t = rc.unconfirmed(sc, CFG)
    plan = rc.watch_plan(sc, CFG, t, 0.0)
    assert plan["k"] == 0 and plan["videos_all"] == 4
    plan = rc.watch_plan(sc, CFG, t, 0.06)  # 영상 2개분
    assert plan["k"] == 1 and plan["videos"] == 2 and plan["cost"] == 0.06
    plan = rc.watch_plan(sc, CFG, t, 100)
    assert plan["k"] == 2 and plan["videos"] == 4


# ───── 명령 전체
def snapshot(path):
    return {str(p.relative_to(path)): p.stat().st_mtime_ns for p in path.rglob("*") if p.is_file()}


def test_dry_run_writes_nothing(ws, capsys):
    before = snapshot(ws)
    rc.cmd_recheck(sc, args(dry_run=True, budget=5), CFG)
    out = capsys.readouterr().out
    assert snapshot(ws) == before and "dry-run" in out and not (ws / "work" / "recheck_base.json").exists()


def test_recheck_free_steps_find_candidates_and_report_diff(ws, monkeypatch, capsys):
    monkeypatch.setattr(rc, "resolve_link", lambda u: (u, "nothing"))
    rc.cmd_recheck(sc, args(steps="fuzzy,links,seen"), CFG)
    out = capsys.readouterr().out
    assert "미확인 2 → 1" in out and "#1 채널1: 미확인 → 의심(수동확인)" in out
    assert (ws / "work" / "recheck_base.json").exists() and (ws / "output").exists()
    st = rc.all_statuses(sc, CFG)
    assert st == {"1": "의심(수동확인)", "2": "미확인", "3": "확인됨(텍스트)"}
    rc.cmd_recheck(sc, args(steps="fuzzy,links,seen"), CFG)  # 다시 실행해도 같은 결과(멱등), 기준(base)은 유지
    assert json.load(open(ws / "work" / "recheck_base.json", encoding="utf-8"))["1"] == "미확인"
    assert rc.all_statuses(sc, CFG) == st


def test_recheck_requires_keys_and_rejects_bad_steps(ws, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    with pytest.raises(SystemExit) as e:
        rc.cmd_recheck(sc, args(steps="comments"), CFG)
    assert "YOUTUBE_API_KEY" in str(e.value)
    with pytest.raises(SystemExit):
        rc.cmd_recheck(sc, args(steps="nope"), CFG)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(SystemExit) as e2:
        rc.cmd_recheck(sc, args(steps="watch", budget=5), CFG)
    assert "OPENAI_API_KEY" in str(e2.value)


def test_recheck_comments_step_updates_videos_and_signals(ws, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "k" * 30)
    monkeypatch.setattr(sc, "yt", lambda endpoint, **p: {"items": [{"snippet": {"topLevelComment": {"snippet": {
        "authorChannelId": {"value": "UC2"}, "textOriginal": "하늘마루 협찬이에요"}}}}]})
    rc.cmd_recheck(sc, args(steps="comments"), CFG)
    assert rc.all_statuses(sc, CFG)["2"] == "확인됨(댓글)"
    videos = sc.jload(sc.WORK / "videos.json", {})
    assert videos["2"]["videos"][1]["comments"]["owner"] == ["하늘마루 협찬이에요"]
    assert os.path.exists("output")
