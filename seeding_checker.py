#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
협찬 유튜버 착용 확인 파이프라인

  parse   : 시딩 리스트(xlsx) -> work/channels.json
  collect : YouTube Data API로 발송일 이후 영상 수집 -> work/videos.json
  screen  : 제목/설명란/태그(+대본) 키워드 1차 판별 -> work/screen.json
  watch   : 미확인 채널의 후보 영상 프레임을 비전 모델(OpenAI 기본/Claude 선택)로 분석 -> work/visual.json
  validate: 브랜드 언급 영상으로 영상 분석 정확도 검증 (--limit N)
  check   : 참고 이미지 파일이 설정대로 있는지 점검
  report  : 결과 엑셀 생성 -> output/seeding_report.xlsx
  all     : 위 단계를 순서대로 실행

환경변수(.env): YOUTUBE_API_KEY (collect), OPENAI_API_KEY 또는 ANTHROPIC_API_KEY (watch)
"""
import argparse
import base64
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import requests
import yaml

WORK = Path("work")
OUT = Path("output")
KST = timezone(timedelta(hours=9))
YT = "https://www.googleapis.com/youtube/v3"
RANK = {"high": 3, "medium": 2, "low": 1}


# ───────────────────────── 공통 ─────────────────────────
def load_cfg(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        msg = f"⚠ {path} 파일의 형식이 잘못되었습니다."
        if mark:
            line = text.splitlines()[mark.line] if mark.line < len(text.splitlines()) else ""
            msg += f"\n  {mark.line + 1}번째 줄, {mark.column + 1}번째 글자 근처:\n  {line[:150]}"
        sys.exit(msg + "\n  → 확인할 것: ① 따옴표(\")가 짝이 맞는지 ② 한 줄에 값이 두 번 들어가지 않았는지(붙여넣다가 기존 내용이 남은 경우)"
                 "\n    ③ 줄 앞 들여쓰기가 다른 줄과 같은지.  해결이 어려우면 최신 config.yaml로 통째로 교체하세요.")


def norm(s):
    """공백/기호 제거 + 소문자. '풋볼 져지' == '풋볼져지'"""
    return re.sub(r"[\s\-_·.,!?()\[\]#]", "", str(s)).lower()


def find_hits(text, keywords):
    """한글 키워드: 공백·기호 무시하고 포함 여부 비교.
    영문 키워드: 단어 경계로 비교 (공백을 지우면 'socia[l engagement]' 처럼 오탐이 생기므로).
    영문 키워드 안의 공백은 공백/_/- 가 있든 없든 매칭 ('field system' == 'fieldsystem')."""
    t, low = norm(text), str(text).lower()
    hits = []
    for k in keywords:
        if re.fullmatch(r"[A-Za-z0-9_ \-]+", k.strip()):
            pat = r"(?<![a-z0-9])" + r"[\s_-]*".join(map(re.escape, k.lower().split())) + r"(?![a-z0-9])"
            if re.search(pat, low):
                hits.append(k)
        elif norm(k) and norm(k) in t:
            hits.append(k)
    return hits


def jdump(obj, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def jload(path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def parse_only(text):
    """'1,5,12' 또는 범위 '1-20,25' -> {1..20, 25}"""
    out = set()
    for part in str(text).split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out |= set(range(int(a), int(b) + 1))
        elif part:
            out.add(int(part))
    return out


def only_filter(channels, only):
    if not only:
        return channels
    want = parse_only(only)
    return [c for c in channels if c["no"] in want]


# ───────────────────────── 1. parse ─────────────────────────
def parse_channel_url(raw):
    """다양한 형태의 유튜브 링크 -> {handle|channel_id, url}"""
    u = unquote(str(raw).strip())
    if not u.startswith("http"):
        u = "https://" + u
    m = re.search(r"youtube\.com/(@[^/?#\s]+)", u)
    if m:
        return {"handle": m.group(1), "url": f"https://www.youtube.com/{m.group(1)}"}
    m = re.search(r"youtube\.com/channel/(UC[\w-]+)", u)
    if m:
        return {"channel_id": m.group(1), "url": f"https://www.youtube.com/channel/{m.group(1)}"}
    # 채널이 아니라 영상/쇼츠 링크가 들어 있는 경우 -> 영상에서 채널을 역추적
    m = re.search(r"(?:youtube\.com/(?:shorts/|watch\?v=|live/)|youtu\.be/)([\w-]{11})", u)
    if m:
        return {"video_id": m.group(1), "url": u}
    return {"url": u}


def parse_item(name, option):
    name = re.sub(r"^\s*\[\d+\]\s*", "", str(name).strip())  # '[상품번호]' 접두어 제거
    m = re.match(r"^(.*?)\s*\[(.+)\]\s*$", name)
    base, color = (m.group(1), m.group(2)) if m else (name, "")
    return {"name": name, "base": base, "color": color, "option": option}


def cmd_parse(args, cfg):
    from openpyxl import load_workbook

    ws = load_workbook(args.xlsx, data_only=True).active
    channels, cur = [], None
    for row in ws.iter_rows(min_row=3, values_only=True):
        a, b, c, d, e, f = (list(row) + [None] * 6)[:6]
        if b is not None and str(b).strip().isdigit() and c:
            m = re.match(r"\s*(\d{6})", str(a))
            ship = datetime.strptime(m.group(1), "%y%m%d").replace(tzinfo=KST) if m else None
            memo = re.sub(r"^\s*\d{6}\s*", "", str(a or "")).strip()  # 날짜 칸에 적힌 메모
            cur = {
                "no": int(b),
                "name": " ".join(str(c).split()),  # 셀 내 줄바꿈 제거
                "ship_date": ship.isoformat() if ship else None,
                "memo": " ".join(memo.split()),
                "couple": "커플" in str(a),
                "items": [],
                **(parse_channel_url(f) if f else {"url": None}),
            }
            channels.append(cur)
        if cur is not None and d:
            cur["items"].append(parse_item(d, e))
    overrides = cfg.get("channel_overrides") or {}
    for c in channels:  # 엑셀 링크가 틀렸거나 채널 주소가 바뀐 경우, config.yaml 의 번호별 링크로 덮어씀
        if c["no"] in overrides:
            for k in ("handle", "channel_id", "video_id"):
                c.pop(k, None)
            c.update(parse_channel_url(overrides[c["no"]]))
            c["override"] = True
    jdump(channels, WORK / "channels.json")
    print(f"[parse] 유튜버 {len(channels)}명 -> {WORK/'channels.json'}" + (f" (링크 수정 {len(overrides)}건 적용)" if overrides else ""))
    for c in channels:
        flag = ""
        if c.get("override"):
            flag = "  (config 에서 링크 수정됨)"
        elif c.get("video_id"):
            flag = "  ⚠ 영상 링크(채널 역추적)"
        elif not (c.get("handle") or c.get("channel_id")):
            flag = "  ⚠ 링크 형식 확인 필요"
        if not c["ship_date"]:
            flag += f"  ⚠ 발송일 없음({c['memo']})"
        print(f"  {c['no']:>2} {c['name']:<22} {c.get('handle') or c.get('channel_id') or c.get('video_id') or c['url']}{flag}")


# ───────────────────────── 2. collect ─────────────────────────
def yt(endpoint, **params):
    params["key"] = os.environ["YOUTUBE_API_KEY"]
    r = requests.get(f"{YT}/{endpoint}", params=params, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"{endpoint} {r.status_code}: {r.text[:200]}")
    return r.json()


def resolve_channel(ch):
    if ch.get("video_id"):
        v = yt("videos", part="snippet", id=ch["video_id"])
        if not v.get("items"):
            return None
        ch = {**ch, "channel_id": v["items"][0]["snippet"]["channelId"]}
    p = {"id": ch["channel_id"]} if ch.get("channel_id") else {"forHandle": ch.get("handle")}
    if not any(p.values()):
        return None
    j = yt("channels", part="snippet,contentDetails,statistics", **p)
    if not j.get("items"):
        return None
    it = j["items"][0]
    return {
        "channel_id": it["id"],
        "title": it["snippet"]["title"],
        "subscribers": it.get("statistics", {}).get("subscriberCount"),
        "uploads": it["contentDetails"]["relatedPlaylists"]["uploads"],
    }


def uploads_since(uploads, since):
    ids, token = [], None
    while True:
        extra = {"pageToken": token} if token else {}
        j = yt("playlistItems", part="contentDetails", playlistId=uploads, maxResults=50, **extra)
        stop = False
        for it in j["items"]:
            pub = it["contentDetails"].get("videoPublishedAt")
            if pub and parse_iso(pub) < since:
                stop = True  # 업로드 목록은 최신순이므로 발송일 이전이 나오면 종료
                continue
            ids.append(it["contentDetails"]["videoId"])
        token = j.get("nextPageToken")
        if stop or not token:
            return ids


def duration_sec(iso):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    h, mi, s = (int(x or 0) for x in m.groups()) if m else (0, 0, 0)
    return h * 3600 + mi * 60 + s


def video_details(ids):
    out = []
    for i in range(0, len(ids), 50):
        j = yt("videos", part="snippet,contentDetails", id=",".join(ids[i:i + 50]))
        for it in j.get("items", []):
            sn = it["snippet"]
            out.append({
                "id": it["id"],
                "url": f"https://www.youtube.com/watch?v={it['id']}",
                "title": sn.get("title", ""),
                "description": sn.get("description", ""),
                "tags": sn.get("tags", []),
                "published": sn["publishedAt"],
                "duration": duration_sec(it["contentDetails"].get("duration")),
            })
    return out


QUOTA = {"out": False}


def fetch_comments(video_id, owner_id):
    """상단(고정 포함) 댓글 + 채널 주인이 쓴 댓글·답글. 댓글이 막힌 영상은 disabled, 쿼타 초과면 None(캐시 안 함)"""
    try:
        j = yt("commentThreads", part="snippet,replies", videoId=video_id, order="relevance", maxResults=50, textFormat="plainText")
    except RuntimeError as e:
        if "quota" in str(e).lower():
            QUOTA["out"] = True
            return None
        return {"owner": [], "other": [], "disabled": True}  # 댓글 막힘 등
    owner, other = [], []
    for it in j.get("items", []):
        top = it["snippet"]["topLevelComment"]["snippet"]
        text = top.get("textOriginal") or top.get("textDisplay", "")
        (owner if (top.get("authorChannelId") or {}).get("value") == owner_id else other).append(text)
        for rp in (it.get("replies") or {}).get("comments", []):  # 시청자가 "어디 옷이에요?" 물었을 때 주인의 답글
            sn = rp["snippet"]
            if (sn.get("authorChannelId") or {}).get("value") == owner_id:
                owner.append(sn.get("textOriginal") or sn.get("textDisplay", ""))
    return {"owner": [t[:600] for t in owner][:15], "other": [t[:200] for t in other][:20]}


def attach_comments(vids, old_vids, owner_id, since, cfg):
    """발송 후 N일 안의 영상에 댓글을 붙인다 (이미 받은 영상은 재사용해 쿼타 절약). 새로 요청한 영상 수를 반환"""
    old = {v["id"]: v for v in old_vids}
    win = cfg.get("comment_window_days", 90)
    need = []
    for v in vids:
        prev = old.get(v["id"], {}).get("comments")
        if prev is not None:
            v["comments"] = prev
        elif parse_iso(v["published"]) <= since + timedelta(days=win):
            need.append(v)
    if not need or QUOTA["out"]:
        return 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for v, res in zip(need, ex.map(lambda x: fetch_comments(x["id"], owner_id), need)):
            if res is not None:
                v["comments"] = res
    return len(need)


def cmd_collect(args, cfg):
    channels = only_filter(jload(WORK / "channels.json", []), args.only)
    data = jload(WORK / "videos.json", {})
    window = cfg.get("window_days")
    for ch in channels:
        key = str(ch["no"])
        try:
            info = resolve_channel(ch)
            if not info:
                data[key] = {"error": "채널을 찾지 못함(핸들 변경/삭제 가능)", "videos": []}
                print(f"  {ch['no']:>2} {ch['name']}: ⚠ 채널 못 찾음")
                continue
            since = parse_iso(ch["ship_date"] or cfg.get("default_since", "2026-05-13T00:00:00+09:00"))
            ids = uploads_since(info["uploads"], since)
            vids = video_details(ids) if ids else []
            vids = [v for v in vids if parse_iso(v["published"]) >= since]
            if window:
                vids = [v for v in vids if parse_iso(v["published"]) <= since + timedelta(days=window)]
            vids.sort(key=lambda v: v["published"])
            n_cm = attach_comments(vids, (data.get(key) or {}).get("videos", []), info["channel_id"], since, cfg) \
                if cfg.get("fetch_comments", True) else 0
            data[key] = {"channel": info, "error": None, "videos": vids,
                         "since_assumed": not ch["ship_date"]}
            title = info["title"]
            note = ""
            if ch.get("override") or not (norm(ch["name"]) in norm(title) or norm(title) in norm(ch["name"])):
                subs = info.get("subscribers")  # 시트 이름과 실제 채널명이 다르면 동일인인지 확인할 수 있게 표시
                note = f"  ※ 실제 채널명 '{title}'" + (f" · 구독자 {int(subs):,}명" if subs else "")
            print(f"  {ch['no']:>2} {ch['name']}: 발송 후 영상 {len(vids)}개" + (f", 댓글 {n_cm}개 확인" if n_cm else "") + note)
        except Exception as e:  # noqa: BLE001
            data[key] = {"error": str(e)[:200], "videos": []}
            print(f"  {ch['no']:>2} {ch['name']}: ⚠ {e}")
        jdump(data, WORK / "videos.json")  # 채널 하나 끝날 때마다 저장
    if QUOTA["out"]:
        print("\n  ⚠ YouTube API 일일 쿼터를 다 써서 일부 댓글을 못 가져왔습니다. 내일(쿼터 초기화 후) 같은 명령을 다시 실행하면 나머지를 이어서 가져옵니다.")
    jdump(data, WORK / "videos.json")
    print(f"[collect] 저장 -> {WORK/'videos.json'}")


# ───────────────────────── 3. screen ─────────────────────────
DISCLOSURE = ["협찬", "유료광고", "제품제공", "제공받", "gifted", "sponsored", "광고포함"]


def get_transcript(video_id, langs):
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        api = YouTubeTranscriptApi()
        return " ".join(s.text for s in api.fetch(video_id, languages=langs))
    except Exception:  # noqa: BLE001  (자막 없음/차단 등)
        return ""


def cmd_screen(args, cfg):
    channels = {str(c["no"]): c for c in jload(WORK / "channels.json", [])}
    data = jload(WORK / "videos.json", {})
    brand = cfg.get("brand_keywords", [])
    prod = cfg.get("product_keywords", [])
    langs = cfg.get("transcript_langs", ["ko", "en"])
    max_tr = cfg.get("transcript_max_min", 20) * 60
    tr_win = cfg.get("transcript_window_days", cfg.get("visual_window_days", 45))  # 자막은 발송 직후 영상만 (요청 수 절약)
    rows = []
    for key, entry in data.items():
        selected = not args.only or int(key) in parse_only(args.only)
        ch = channels.get(key, {})
        since = parse_iso(ch.get("ship_date") or cfg.get("default_since", "2026-05-13T00:00:00+09:00"))
        for v in entry.get("videos", []):
            text = " ".join([v["title"], v["description"], " ".join(v["tags"])])
            sig = {
                "brand_hits": find_hits(text, brand),
                "product_hits": find_hits(text, prod),
                "disclosure": find_hits(text, DISCLOSURE),
                "transcript_hits": (v.get("signals") or {}).get("transcript_hits", []),  # 이전 자막 결과 유지
            }
            cm = v.get("comments") or {}
            owner_text, other_text = " ".join(cm.get("owner", [])), " ".join(cm.get("other", []))
            sig["comment_hits"] = find_hits(owner_text, brand)            # 채널 주인의 댓글·답글에 브랜드 언급 (강한 근거)
            sig["comment_other_hits"] = find_hits(other_text, brand)      # 시청자 댓글에 브랜드 언급 (약한 근거)
            sig["product_hits"] = sorted(set(sig["product_hits"]) | set(find_hits(owner_text, prod)))
            in_win = not tr_win or parse_iso(v["published"]) <= since + timedelta(days=tr_win)
            if args.transcripts and selected and in_win and not sig["brand_hits"] and v["duration"] <= max_tr:
                tr = get_transcript(v["id"], langs)
                sig["transcript_hits"] = find_hits(tr, brand)
                time.sleep(0.5)
            v["signals"] = sig
        if selected:
            vids = entry.get("videos", [])
            rows.append((key, channels[key]["name"], sum(1 for v in vids if v["signals"]["brand_hits"]),
                         sum(1 for v in vids if v["signals"]["transcript_hits"]),
                         sum(1 for v in vids if v["signals"]["comment_hits"]),
                         sum(1 for v in vids if v["signals"]["comment_other_hits"])))
    many = len(rows) > 10
    for key, name, n_hit, n_tr, n_cm, n_co in rows:
        if not many or n_hit or n_tr or n_cm or n_co:  # 채널이 많으면 언급이 있는 채널만 출력
            print(f"  {key:>2} {name}: 설명란·제목 언급 영상 {n_hit}개"
                  + (f", 자막 {n_tr}개" if n_tr else "") + (f", 주인 댓글 {n_cm}개" if n_cm else "") + (f", 시청자 댓글 {n_co}개" if n_co else ""))
    hits = [r for r in rows if r[2] or r[3] or r[4] or r[5]]
    print(f"\n  → 브랜드 언급이 있는 채널: {len(hits)}명 / {len(rows)}명" + ("  (언급 없는 채널은 출력 생략)" if many else ""))
    jdump(data, WORK / "screen.json")
    print(f"[screen] 저장 -> {WORK/'screen.json'}")


# ───────────────────────── 판정 로직 (watch/report 공용) ─────────────────────────
TS = re.compile(r"(?<!\d)(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?!\d)")


def ts_to_sec(m):
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def get_transcript_segments(video_id, langs):
    """[(시작 초, 문장)] — 자막이 없거나 막히면 빈 리스트"""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        return [(float(sn.start), sn.text) for sn in YouTubeTranscriptApi().fetch(video_id, languages=langs)]
    except Exception:  # noqa: BLE001
        return []


def focus_times(video, cfg):
    """브랜드·상품이 언급된 구간(설명란 챕터, 자막)에서 집중적으로 뽑을 시각(초) 목록"""
    kws = list(cfg.get("brand_keywords", [])) + list(cfg.get("product_keywords", []))
    dur, wins = video["duration"], []
    lines = video.get("description", "").splitlines()
    stamps = [(i, ts_to_sec(m)) for i, ln in enumerate(lines) for m in [TS.search(ln)] if m]
    for j, (i, t) in enumerate(stamps):
        if find_hits(lines[i], kws):  # '2:12 브랜드명' 같은 챕터 줄 -> 다음 챕터 시작까지(최대 90초)
            end = stamps[j + 1][1] if j + 1 < len(stamps) else t + 60
            wins.append((t, min(end, t + 90)))
    if cfg.get("focus_transcript", True):
        for start, text in get_transcript_segments(video["id"], cfg.get("transcript_langs", ["ko", "en"])):
            if find_hits(text, kws):
                wins.append((start - 3, start + 25))
    wins = sorted((max(0, a), min(b, dur)) for a, b in wins if min(b, dur) > max(0, a))
    merged = []
    for a, b in wins:
        if merged and a <= merged[-1][1] + 5:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    times = []
    for a, b in merged:
        step, t = max(3, (b - a) / 6), a
        while t < b:
            times.append(int(t))
            t += step
    cap = cfg.get("focus_frames", 12)
    if len(times) > cap:  # 너무 많으면 고르게 솎아냄
        idx = sorted({round(i * (len(times) - 1) / max(cap - 1, 1)) for i in range(cap)})
        times = [times[i] for i in idx]
    return times


def decide(entry, vis):
    """채널 1곳의 최종 판정. vis = {video_id: 분석결과}"""
    if entry is None:
        return {"status": "미처리", "basis": "", "video": None, "note": "아직 수집하지 않음(--only 로 일부만 실행)"}
    if entry.get("error"):
        return {"status": "채널 오류", "basis": "", "video": None, "note": entry["error"]}
    vids = entry["videos"]
    for v in vids:
        v.setdefault("signals", {"brand_hits": [], "product_hits": [], "disclosure": [], "transcript_hits": [],
                                 "comment_hits": [], "comment_other_hits": []})
    if not vids:
        return {"status": "업로드 없음", "basis": "", "video": None, "note": "발송일 이후 영상 없음"}
    for v in vids:
        if v["signals"]["brand_hits"]:
            return {"status": "확인됨(텍스트)", "basis": "/".join(v["signals"]["brand_hits"]), "video": v, "note": ""}
    for v in vids:
        if v["signals"].get("comment_hits"):
            return {"status": "확인됨(댓글)", "basis": "/".join(v["signals"]["comment_hits"]), "video": v, "note": "채널 주인의 댓글·답글에 브랜드 언급"}
    for v in vids:
        if v["signals"]["transcript_hits"]:
            return {"status": "확인됨(대본)", "basis": "/".join(v["signals"]["transcript_hits"]), "video": v, "note": ""}
    best = None  # (rank, video, worn)
    by_id = {v["id"]: v for v in vids}
    for vid, r in (vis or {}).items():
        for w in r.get("worn", []):
            rk = RANK.get(w.get("confidence"), 0)
            if rk and vid in by_id and (best is None or rk > best[0]):
                best = (rk, by_id[vid], w)
    if best and best[0] == 3:
        return {"status": "확인됨(영상)", "basis": best[2].get("product", ""), "video": best[1], "note": best[2].get("evidence", "")}
    if best and best[0] == 2:
        return {"status": "의심(수동확인)", "basis": best[2].get("product", ""), "video": best[1], "note": "영상 분석 중간 신뢰도: " + best[2].get("evidence", "")}
    for v in vids:
        if v["signals"].get("comment_other_hits"):
            return {"status": "의심(수동확인)", "basis": "댓글: " + "/".join(v["signals"]["comment_other_hits"]), "video": v,
                    "note": "시청자 댓글에 브랜드 언급 (영상에 실제로 나오는지 확인)"}
    for v in vids:
        if v["signals"]["product_hits"]:
            return {"status": "의심(수동확인)", "basis": "/".join(v["signals"]["product_hits"]), "video": v, "note": "상품 키워드만 언급(브랜드명 없음)"}
    note = "영상 분석했으나 확신 낮음" if best else "텍스트 단서 없음" + ("" if vis else " (영상 분석 전)")
    return {"status": "미확인", "basis": "", "video": None, "note": note}


# ───────────────────────── 4. watch (프레임 + 비전 모델) ─────────────────────────
PROMPT = """위 프레임들은 유튜버 '{creator}'의 영상({title})에서 일정 간격으로 추출한 장면입니다 (프레임마다 영상 내 초 단위 시각 표기).
이 유튜버에게 협찬(선물)으로 보낸 의류는 다음과 같습니다:
{items}

{ref_note}
다음 순서로 판단하세요.
1) 먼저 유튜버가 입고 있는 상의·하의를 관찰해 seen에 적습니다 (색, 로고/프린트 유무와 위치, 핏, 소재감). 옷에 인쇄된 글자(로고·문구)가 읽히면 그대로 적으세요. 단, 참고 이미지에서 본 글자·디자인을 영상 프레임에서 본 것처럼 적으면 안 됩니다. evidence에는 영상 프레임에서 직접 확인한 것만 쓰세요.
2) 협찬 상품과 비교합니다. 상품명과 색상이 같다는 것만으로는 일치로 보지 마세요. 흔한 기본 아이템(무지 티셔츠, 검정 상의, 회색 반바지)은 다른 브랜드 제품과 구분이 어렵습니다.
3) confidence 기준 (구분할 수 있는 근거가 있어야만 올라갑니다):
   - high: 참고 이미지와 로고·프린트·패널 구성·배색·핏 등 **고유한 디자인 요소**가 일치하는 것을 프레임에서 직접 확인했을 때만
   - medium: 고유한 요소가 **일부만** 일치하거나(로고 일부가 보임, 독특한 배색·패널이 맞음) 가려져서 확신하지 못할 때. evidence에 어떤 고유 요소가 보였는지 반드시 적으세요.
   - low: 색상·종류만 같거나 비슷해 보이는 정도. 무지 티셔츠, 검정 상의, 회색 반바지처럼 어느 브랜드에서나 나올 수 있는 모습은 모두 low입니다.
   evidence에 구체적인 고유 요소를 쓸 수 없다면 medium 이상을 주지 마세요.
worn에는 위 협찬 상품을 **하나도 빠뜨리지 말고 전부** 한 항목씩 넣으세요. 해당 종류의 옷이 프레임에 보이지 않으면 confidence를 "none"으로 하세요. 추측으로 채우지 마세요.
반드시 아래 JSON만 출력하세요(설명 문장 금지):
{{"seen": "관찰한 착장 요약", "worn": [{{"product": "상품명 [색상]", "confidence": "high|medium|low|none", "frames": [프레임번호,...], "evidence": "근거 한 줄"}}], "note": "참고사항"}}"""


MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}


def mime_of(path):
    ext = Path(path).suffix.lower()
    if ext not in MIME:
        sys.exit(f"⚠ 지원하지 않는 이미지 형식입니다: {path}  (jpg, png, webp, gif만 가능)")
    return MIME[ext]


def img_block(path):
    data = base64.standard_b64encode(Path(path).read_bytes()).decode()
    mt = mime_of(path)
    return {"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}}


def frames_from_file(video_path, duration, n, outdir, hires=False, extra_times=None):
    """균등 간격 n장 + 집중 시각(extra_times)의 프레임을 추출 -> (프레임 경로, 초 단위 시각, 고해상도 프레임 경로)"""
    outdir.mkdir(parents=True, exist_ok=True)
    extra = sorted({int(t) for t in (extra_times or []) if 0 <= t < max(duration - 1, 1)})
    base_n = n if not extra else max(6, n // 2)  # 집중 구간이 있으면 균등 프레임은 절반으로
    interval = max(duration / base_n, 2)
    times, last = [], -99
    for t in sorted({int(i * interval) for i in range(base_n) if i * interval < duration} | set(extra)):
        if t - last >= 2:  # 너무 붙은 시각은 제거
            times.append(t)
            last = t
    hdir = outdir.parent / "hi"
    if hires:
        hdir.mkdir(parents=True, exist_ok=True)

    def grab(t, dest, width, q):
        """한 장 추출. 일부 영상은 JPEG 인코더가 'Non full-range YUV' 로 실패하므로 옵션을 바꿔 재시도하고, 그래도 안 되면 PNG 로 저장"""
        base = ["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", str(video_path), "-frames:v", "1", "-vf", f"scale={width}:-2"]
        tries = [(["-q:v", str(q)], dest),
                 (["-strict", "unofficial", "-pix_fmt", "yuvj420p", "-q:v", str(q)], dest),
                 ([], dest.with_suffix(".png"))]
        for extra, out in tries:
            r = subprocess.run(base + extra + [str(out)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
                return out
        return None

    frames, stamps, hi, failed = [], [], [], 0
    for t in times:
        got = grab(t, outdir / f"f_{len(frames):03d}.jpg", 768, 4)
        if got is None:
            failed += 1
            continue
        frames.append(got)
        stamps.append(t)
        if hires:
            hd = grab(t, hdir / f"h_{len(hi):03d}.jpg", 1280, 3)
            hi.append(hd if hd else got)  # 실패 시 저해상도로 대체해 인덱스 정렬 유지
    if failed:
        print(f"       ⚠ 프레임 {failed}장 추출 실패 (나머지 {len(frames)}장으로 분석)")
    if not frames:  # 한 장도 없으면 '안보임'으로 오판하지 않도록 실패 처리 (결과 저장 안 함 -> 다음 실행 때 재시도)
        raise RuntimeError("프레임 추출 실패 (ffmpeg). 영상 파일 형식 문제일 수 있습니다.")
    return frames, stamps, hi


def ytdlp_formats(cfg):  # 403 등으로 막히면 다음 형식으로 재시도
    h = cfg.get("download_height", 720)
    return [f"bv*[height<={h}]/b[height<={h}]/b", "18/b[height<=720]/b", "w"]


def cleanup_tmp(cfg, video_id):
    """분석이 끝난 임시 프레임 삭제 (keep_frames: true 면 work/tmp/<영상ID>/ 에 남겨 모델이 본 장면을 직접 확인 가능)"""
    if not cfg.get("keep_frames"):
        shutil.rmtree(WORK / "tmp" / video_id, ignore_errors=True)


def downloaded_file(out):
    return next((p for p in out.glob("v.*") if not p.name.endswith((".part", ".ytdl"))), None)


def run_ytdlp(video_id, out, cfg):
    base = [sys.executable, "-m", "yt_dlp", "--no-playlist", "-q", "--no-warnings", "--retries", "3",
            "-o", str(out / "v.%(ext)s")]
    if cfg.get("cookies_from_browser"):
        base += ["--cookies-from-browser", cfg["cookies_from_browser"]]
    for fmt in ytdlp_formats(cfg):
        for leftover in out.glob("v.*"):
            leftover.unlink()
        try:
            subprocess.run(base + ["-f", fmt, f"https://www.youtube.com/watch?v={video_id}"], check=True, timeout=900)
            if downloaded_file(out):
                return
        except subprocess.CalledProcessError:
            time.sleep(2)
    raise RuntimeError("영상 다운로드 실패(403 등). ① python -m pip install -U yt-dlp 로 업데이트 "
                       "② config.yaml 의 cookies_from_browser 를 \"chrome\" 으로 설정 ③ 잠시 후 재실행")


def download_frames(video_id, duration, cfg, extra_times=None, tag=None):
    out = WORK / "tmp" / (tag or video_id)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    run_ytdlp(video_id, out, cfg)
    vfile = downloaded_file(out)
    frames, stamps, hi = frames_from_file(vfile, duration, cfg.get("frames_per_video", 16), out / "frames",
                                          hires=cfg.get("verify", True), extra_times=extra_times)
    vfile.unlink()
    return frames, stamps, hi


def config_sig(cfg):
    """영상 분석 결과가 유효한지 판단하는 지문: 모델/프롬프트/프레임 수/참고 이미지(파일명+크기)가 바뀌면 달라짐"""
    files = []
    for v in (cfg.get("reference_images") or {}).values():
        for path in ([v] if isinstance(v, str) else v or []):
            fp = resolve_ref(path)
            files.append((path, fp.stat().st_size if fp else -1))
    blob = json.dumps([cfg.get("provider"), cfg.get("model"), cfg.get("image_detail"), cfg.get("frames_per_video"),
                       sorted(files), PROMPT, VERIFY_PROMPT, cfg.get("verify", True), cfg.get("verify_model"),
                       cfg.get("product_hints"), cfg.get("focus_frames"), cfg.get("focus_transcript", True),
                       cfg.get("download_height", 720)],
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10]


SHORT = [("풋볼", "져지"), ("솔트 워싱 크롭", "크롭티"), ("버뮤다", "쇼츠"), ("블루종", "블루종"), ("피케", "피케티")]
CONF_KO = {"none": "안보임"}


def short_name(name):
    """'예시 상품 풀네임 [화이트]' -> '크롭티[화이트]' (콘솔 표시용)"""
    color = re.search(r"\[(.+?)\]", name)
    for key, label in SHORT:
        if key in name:
            return label + (f"[{color.group(1)}]" if color else "")
    return name[:10]


USAGE, _ULOCK = {}, threading.Lock()


def add_usage(model, inp, out):
    with _ULOCK:
        u = USAGE.setdefault(model, [0, 0])
        u[0] += int(inp or 0)
        u[1] += int(out or 0)


def print_usage(cfg):
    """이번 실행의 토큰 사용량 (config 의 prices 가 있으면 예상 비용도 계산)"""
    if not USAGE:
        return
    prices = cfg.get("prices") or {}
    total, priced = 0.0, 0
    print("\n[API 사용량] 이번 실행")
    for model, (i, o) in USAGE.items():
        line = f"  {model}: 입력 {i:,} · 출력 {o:,} 토큰"
        pr = prices.get(model) or {}
        if pr.get("input") or pr.get("output"):
            cost = i / 1e6 * pr.get("input", 0) + o / 1e6 * pr.get("output", 0)
            total, priced = total + cost, priced + 1
            line += f"  ≈ ${cost:,.2f}"
        print(line)
    if priced == len(USAGE):
        print(f"  예상 합계 ≈ ${total:,.2f}  (config.yaml 의 단가 기준. 실제 청구액은 OpenAI 대시보드를 확인하세요)")
    else:
        print("  ※ config.yaml 의 prices 에 모델 단가(100만 토큰당 USD)를 적으면 예상 비용도 계산해 줍니다.")


def usage_brief(cfg):
    """진행 중 표시용 누적 사용량 한 줄"""
    if not USAGE:
        return ""
    inp = sum(u[0] for u in USAGE.values())
    out = f" · 누적 입력 {inp:,} 토큰"
    prices = cfg.get("prices") or {}
    cost = sum(u[0] / 1e6 * (prices.get(m) or {}).get("input", 0) + u[1] / 1e6 * (prices.get(m) or {}).get("output", 0) for m, u in USAGE.items())
    return out + (f" (≈ ${cost:,.2f})" if cost else "")


def make_client(cfg):
    if cfg.get("provider", "openai") == "anthropic":
        import anthropic

        return anthropic.Anthropic()
    from openai import OpenAI

    return OpenAI()


VERIFY_PROMPT = """당신은 1차 판단을 반박하는 검수자입니다. 앞쪽 이미지는 협찬 상품 '{product}'의 참고 이미지이고, 뒤쪽 이미지는 영상에서 뽑은 프레임입니다.
1차 판단은 "프레임 속 유튜버가 이 상품을 입었다"이며 근거는 다음과 같습니다: {claim}

다음 순서로 엄격하게 검수하세요.
1) 프레임 속 해당 의류의 구체적 특징(로고의 위치·모양, 패널/배색, 프린트, 워싱·소재감, 핏, 디테일)을 직접 관찰해 적습니다. 보이지 않는 것을 상상해서 적지 마세요.
2) 참고 이미지와 하나씩 대조해, 확실히 일치하는 고유 요소(matching)와 다르거나 확인 불가한 요소(differences)를 나눠 나열합니다.
3) 판정(verdict)을 셋 중 하나로 내리세요.
   - "match": 로고·프린트 문구·배색·패널 같은 고유 요소가 프레임에서 실제로 보이고 참고 이미지와 일치하며 모순이 없을 때만
   - "mismatch": 참고 이미지와 명백히 다른 요소(다른 로고·프린트·색·디자인)를 프레임에서 직접 확인했을 때만
   - "inconclusive": 비슷해 보이지만 해상도·각도·가림 때문에 고유 요소를 확인할 수 없을 때 (다른 브랜드의 기본 아이템일 가능성이 남은 경우 포함)
또한 distinctive_visible을 판단하세요: 참고 상품의 고유 요소(프린트 문구·로고·특징적 배색/패널) 중 최소 하나가 프레임에서 일부라도 실제로 식별되면 true입니다. 단지 같은 색·같은 종류의 옷이라는 이유만으로는 false입니다.
반드시 아래 JSON만 출력하세요:
{{"verdict": "match", "distinctive_visible": true, "matching": ["일치하는 고유 요소"], "differences": ["다른/확인 불가 요소"], "reason": "판정 이유 한 줄"}}"""


def resolve_ref(path):
    """설정에 적은 경로의 실제 파일을 찾는다. 없으면 같은 이름(확장자만 다른 파일, 이중 확장자 포함)을 찾고, 그래도 없으면 None"""
    fp = Path(path)
    if fp.is_file():
        return fp
    if fp.parent.is_dir():
        for alt in sorted(fp.parent.glob(fp.stem + ".*")):
            if alt.is_file() and alt.suffix.lower() in MIME:
                return alt
    return None


def refs_for(cfg, it):
    """상품(시트 한 줄)의 참고 이미지 경로 목록 (실제 존재하는 파일만)"""
    refs = cfg.get("reference_images") or {}
    val = refs.get(it["name"]) or refs.get(it["base"])  # 사진 1장(문자열) 또는 여러 장(리스트)
    found = [resolve_ref(p) for p in ([val] if isinstance(val, str) else (val or []))]
    return [str(f) for f in found if f]


def check_refs(cfg, quiet_ok=False):
    """설정의 참고 이미지가 실제로 있는지 점검하고 결과를 출력. (없는 파일은 조용히 건너뛰므로 실행 전에 꼭 확인)"""
    refs = cfg.get("reference_images") or {}
    folders = {Path(p).parent for v in refs.values() for p in ([v] if isinstance(v, str) else v or [])}
    present = sorted(f.name for d in folders if d.is_dir() for f in d.iterdir() if f.is_file() and f.suffix.lower() in MIME)
    total = found = 0
    problems = []
    for name, val in refs.items():
        for path in ([val] if isinstance(val, str) else val or []):
            total += 1
            r = resolve_ref(path)
            if r is None:
                near = difflib.get_close_matches(Path(path).name, present, n=2, cutoff=0.5)
                problems.append(f"  ✗ {name}\n      없음: {path}" + (f"\n      비슷한 파일: {', '.join(near)}" if near else ""))
            else:
                found += 1
                if r.name != Path(path).name:
                    problems.append(f"  △ {name}\n      설정 {Path(path).name} → 실제 {r.name} 사용 (설정 파일의 이름도 맞춰주세요)")
    if problems or not quiet_ok:
        print(f"[참고 이미지 점검] 설정 {total}장 중 {found}장 확인" + (f", {total - found}장 없음" if total > found else ""))
        for line in problems:
            print(line)
    return total - found


def print_cfg_summary(cfg):
    """실행에 실제로 적용된 설정 요약 (설정 파일이 예전 내용으로 바뀌었는지 바로 알아볼 수 있게)"""
    hints = sum(1 for v in (cfg.get("product_hints") or {}).values() if v)
    print("[적용된 설정] "
          f"모델={cfg.get('model', 'gpt-5.4-mini')} · 검수={'켜짐' if cfg.get('verify', True) else '꺼짐'}"
          f"(검수모델={cfg.get('verify_model') or '동일'}) · 이미지={cfg.get('image_detail', 'auto')} · "
          f"브랜드키워드 {len(cfg.get('brand_keywords', []))}개 · 상품힌트 {hints}개 · 정답표 {len(cfg.get('ground_truth') or {})}개 · "
          f"집중프레임 {cfg.get('focus_frames', 12)} · 다운로드 {cfg.get('download_height', 720)}p")
    if not hints or not cfg.get("ground_truth"):
        print("  ※ 상품힌트/정답표가 비어 있습니다. config.yaml이 예전 버전이면 최신 config.yaml로 교체하세요.")


def cmd_check(args, cfg):
    missing = check_refs(cfg)
    if missing == 0:
        print("  모든 참고 이미지가 정상입니다.")


def call_vlm(client, cfg, parts, model=None, detail=None, max_out=2000):
    """parts = [("text", 문자열) | ("image", 경로)] 를 비전 모델에 보내고 응답 텍스트를 돌려준다 (OpenAI / Anthropic)"""
    model = model or cfg.get("model", "gpt-5.4-mini")
    if cfg.get("provider", "openai") == "anthropic":
        content = [{"type": "text", "text": v} if k == "text" else img_block(v) for k, v in parts]
        msg = client.messages.create(model=model, max_tokens=1200, messages=[{"role": "user", "content": content}])
        u = getattr(msg, "usage", None)
        if u:
            add_usage(model, getattr(u, "input_tokens", 0), getattr(u, "output_tokens", 0))
        return "".join(b.text for b in msg.content if b.type == "text")
    content = []
    for k, v in parts:
        if k == "text":
            content.append({"type": "text", "text": v})
        else:
            b64 = base64.standard_b64encode(Path(v).read_bytes()).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime_of(v)};base64,{b64}",
                                                               "detail": detail or cfg.get("image_detail", "auto")}})
    resp = client.chat.completions.create(model=model, max_completion_tokens=max_out,
                                          response_format={"type": "json_object"},
                                          messages=[{"role": "user", "content": content}])
    u = getattr(resp, "usage", None)
    if u:
        add_usage(model, getattr(u, "prompt_tokens", 0), getattr(u, "completion_tokens", 0))
    return resp.choices[0].message.content or ""


def parse_json(text, default):
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(m.group(0)) if m else default
    except json.JSONDecodeError:
        return default


def verify_worn(client, cfg, ch, w, frames, stamps, hi):
    """1차에서 high/medium 이 나온 항목을 고해상도 프레임으로 다시 반박 검수. 검수 불가면 None"""
    pn = norm(w.get("product", ""))
    items = [it for it in ch["items"] if pn and (norm(it["name"]) in pn or pn in norm(it["name"]))]
    ref_paths = [(it["name"], p) for it in items for p in refs_for(cfg, it)]
    idx = [f for f in w.get("frames", []) if isinstance(f, int) and 0 <= f < len(frames)][:3]
    if not ref_paths or not idx:
        return None
    pool = hi if hi and len(hi) == len(frames) else frames
    parts = []
    for name, p in ref_paths:
        parts += [("text", f"[참고 상품 이미지] {name}"), ("image", p)]
    for i in idx:
        parts += [("text", f"[영상 프레임 {i} · {stamps[i]}초]"), ("image", pool[i])]
    parts.append(("text", VERIFY_PROMPT.format(product=w.get("product"), claim=w.get("evidence", ""))))
    text = call_vlm(client, cfg, parts, model=cfg.get("verify_model") or None, detail="high", max_out=1500)
    return parse_json(text, None)


def ask_vlm(client, cfg, creator, video, ch, frames, stamps, hi=None):
    """1차: 프레임+참고이미지로 착용 판단 -> 2차: high/medium 은 반박 검수 (OpenAI / Anthropic 선택)"""
    parts, ref_names = [], []  # ("text", 문자열) | ("image", 경로)
    for it in ch["items"]:
        for k, path in enumerate(refs_for(cfg, it), 1):
            parts += [("text", f"[참고 상품 이미지 {k}] {it['name']}"), ("image", path)]
            ref_names.append(it["name"])
    for i, (f, sec) in enumerate(zip(frames, stamps)):
        parts += [("text", f"[프레임 {i} · {sec}초]"), ("image", f)]
    hints = cfg.get("product_hints") or {}
    items = "\n".join(f"- {it['name']}" + (f" — 특징: {hints.get(it['name']) or hints.get(it['base'])}" if (hints.get(it["name"]) or hints.get(it["base"])) else "")
                      for it in ch["items"])
    ref_note = "참고 상품 이미지가 앞쪽에 첨부되어 있습니다.\n" if ref_names else "참고 이미지는 없습니다. 상품명(색상 포함)만으로 판단하세요.\n"
    parts.append(("text", PROMPT.format(creator=creator, title=video["title"], items=items, ref_note=ref_note)))

    text = call_vlm(client, cfg, parts)
    res = parse_json(text, {"worn": [], "note": "응답 파싱 실패: " + text[:100]})
    res.setdefault("worn", [])
    for it in ch["items"]:  # 모델이 빠뜨린 상품 보충
        nn = norm(it["name"])
        if not any(norm(w.get("product", "")) and (nn in norm(w["product"]) or norm(w["product"]) in nn) for w in res["worn"]):
            res["worn"].append({"product": it["name"], "confidence": "none", "frames": [], "evidence": "모델 응답에 없음"})
    ref_set = {norm(n) for n in ref_names}
    for w in res.get("worn", []):
        pn = norm(w.get("product", ""))
        has_ref = bool(pn) and any(r in pn or pn in r for r in ref_set)
        if w.get("confidence") == "high" and not has_ref:  # 참고 이미지 없는 상품의 high 는 신뢰 불가
            w["confidence"] = "medium"
            w["evidence"] = "(해당 상품 참고 이미지 없어 등급 하향) " + str(w.get("evidence", ""))
        elif has_ref and cfg.get("verify", True) and w.get("confidence") in ("high", "medium"):
            v = verify_worn(client, cfg, ch, w, frames, stamps, hi)
            if v is None:  # 인용한 프레임이 없는 등 검수 불가
                if w["confidence"] == "high":
                    w["confidence"] = "medium"
                w["evidence"] = "(검수 불가→하향) " + str(w.get("evidence", ""))
            else:
                w["verify"] = v
                first = w["confidence"]
                verdict = v.get("verdict") or ("match" if v.get("same_product") is True else "mismatch")  # 구버전 응답 호환
                reason = v.get("reason", "")
                if verdict == "match":
                    w["evidence"] = f"{w.get('evidence', '')} | 검수 일치: {', '.join(map(str, v.get('matching', [])[:3]))}"
                elif verdict == "inconclusive" and first == "high" and v.get("distinctive_visible") is True:
                    # 1차가 강했고 고유 요소가 흐릿하게나마 보임 -> 사람이 확인할 의심 건으로 유지
                    w["confidence"] = "medium"
                    w["evidence"] = f"(검수 판단불가: {reason}) 원판단: {w.get('evidence', '')}"
                else:  # 명백히 다르거나, 같은 색 옷일 뿐 고유 요소가 보이지 않음
                    w["confidence"] = "low"
                    why = "고유 요소 미확인" if verdict == "inconclusive" else "다른 제품"
                    w["evidence"] = f"(검수 기각: {why} — {reason}) 원판단: {w.get('evidence', '')}"
    for w in res.get("worn", []):  # 프레임 번호 -> 영상 시각 링크용 초
        fr = [f for f in w.get("frames", []) if isinstance(f, int) and 0 <= f < len(stamps)]
        w["at_sec"] = stamps[fr[0]] if fr else None
    res["_sig"] = config_sig(cfg)
    res["n_frames"] = len(frames)
    return res


def pick_candidates(ch, entry, cfg):
    """채널의 영상 분석 후보: 발송 후 N일 이내 + 길이 제한 -> 협찬표기·상품키워드 > 착장 제목 > 나머지, 같은 순위에선 발송 직후 순"""
    max_vids = cfg.get("max_visual_videos_per_channel", 4)
    max_dur = cfg.get("max_duration_min", 20) * 60
    since = parse_iso(ch["ship_date"] or cfg.get("default_since", "2026-05-13T00:00:00+09:00"))
    win = cfg.get("visual_window_days")
    pool = [v for v in entry["videos"] if v["duration"] <= max_dur
            and (not win or parse_iso(v["published"]) <= since + timedelta(days=win))]
    kws = cfg.get("outfit_title_keywords", [])

    def priority(v):
        sg = v.get("signals") or {}
        return (0 if (sg.get("disclosure") or sg.get("product_hits") or sg.get("comment_other_hits")) else 1 if find_hits(v["title"], kws) else 2, v["published"])

    pool.sort(key=priority)
    return pool, pool[:max_vids]


def fmt_eta(sec):
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    return f"{h}시간 {m}분" if h else f"{m}분"


def cmd_watch(args, cfg):
    if not shutil.which("ffmpeg"):
        sys.exit("⚠ ffmpeg를 찾을 수 없습니다. 설치 후 터미널을 완전히 닫았다가 새로 열어주세요.\n"
                 "  Windows: winget install Gyan.FFmpeg   /   Mac: brew install ffmpeg\n"
                 "  확인: ffmpeg -version")
    print_cfg_summary(cfg)
    check_refs(cfg, quiet_ok=True)
    client = make_client(cfg)
    channels = {str(c["no"]): c for c in jload(WORK / "channels.json", [])}
    data = jload(WORK / "screen.json", {})
    visual = jload(WORK / "visual.json", {})
    sig = config_sig(cfg)
    tasks = []
    for key, entry in data.items():
        if args.only and int(key) not in parse_only(args.only):
            continue
        ch = channels[key]
        if decide(entry, {})["status"] not in ("미확인", "의심(수동확인)"):
            continue  # 텍스트로 이미 확인됐거나 영상이 없는 채널은 건너뜀
        pool, cands = pick_candidates(ch, entry, cfg)
        vis = visual.setdefault(key, {})
        todo = [v for v in cands if vis.get(v["id"], {}).get("_sig") != sig]  # 같은 설정으로 분석한 영상은 재사용
        print(f"  {key:>2} {ch['name']}: 영상 {len(entry['videos'])}개 중 {len(cands)}개 대상 (조건 충족 {len(pool)}개, 새로 분석 {len(todo)}개)")
        tasks += [(key, ch, v) for v in todo]
    total, workers = len(tasks), max(1, int(cfg.get("workers", 1)))
    print(f"\n  → 새로 분석할 영상 {total}개 · 동시 작업 {workers}개 (중단해도 저장돼 있어 같은 명령으로 이어집니다)")

    def work(task):
        key, ch, v = task
        tag = f"{key}_{v['id']}"
        try:
            frames, stamps, hi = download_frames(v["id"], v["duration"], cfg, focus_times(v, cfg), tag=tag)
            return ask_vlm(client, cfg, ch["name"], v, ch, frames, stamps, hi)
        finally:
            cleanup_tmp(cfg, tag)

    done, t0 = 0, time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(work, t): t for t in tasks}
        try:
            for fut in as_completed(futs):
                key, ch, v = futs[fut]
                done += 1
                prefix = f"  [{done}/{total}] {key:>2} {ch['name']} · "
                try:
                    res = fut.result()
                    visual[key][v["id"]] = res
                    worn = res.get("worn", [])
                    print(prefix + f"{v['title'][:30]}: {fmt_worn(worn)}")
                    print_details(worn)
                except Exception as e:  # noqa: BLE001
                    print(prefix + f"{v['id']}: ⚠ {type(e).__name__}: {str(e)[:160]}")
                jdump(visual, WORK / "visual.json")  # 영상 하나 끝날 때마다 저장 -> 중단 후 이어하기 가능
                if done % 10 == 0 and done < total:
                    el = time.time() - t0
                    print(f"       … 진행 {done}/{total} · 경과 {fmt_eta(el)} · 남은 시간 약 {fmt_eta(el / done * (total - done))}{usage_brief(cfg)}")
        except KeyboardInterrupt:
            ex.shutdown(wait=False, cancel_futures=True)
            jdump(visual, WORK / "visual.json")
            print("\n  ⏸ 중단했습니다. 지금까지 분석한 영상은 저장돼 있어 같은 명령으로 이어서 실행하면 됩니다.")
            raise SystemExit(1)
    jdump(visual, WORK / "visual.json")
    print_usage(cfg)
    print(f"[watch] 저장 -> {WORK/'visual.json'}")


def print_details(worn):
    """high/medium 근거(↳)와, 1차에서 통과했다가 2차 검수에서 기각된 항목과 이유(✗)를 출력"""
    for w in worn:
        ev = str(w.get("evidence", ""))
        if w.get("confidence") in ("high", "medium"):
            print(f"       ↳ {w.get('product')} [{w.get('confidence')}] {ev[:140]}")
        elif ev.startswith("(검수 기각"):
            print(f"       ✗ {w.get('product')} — 1차 통과 후 {ev[:300]}")


def fmt_worn(worn):
    return ", ".join(f"{short_name(w.get('product', ''))}={CONF_KO.get(w.get('confidence'), w.get('confidence'))}" for w in worn) or "착용 미확인"


def cmd_validate(args, cfg):
    """정답 검증: 설명란에 브랜드명이 적힌 영상(협찬 영상일 가능성 높음)에 영상 분석을 돌려, 도구가 실제로 잡아내는지 확인"""
    if not shutil.which("ffmpeg"):
        sys.exit("⚠ ffmpeg를 찾을 수 없습니다. (ffmpeg -version 으로 확인)")
    print_cfg_summary(cfg)
    check_refs(cfg, quiet_ok=True)
    client = make_client(cfg)
    channels = {str(c["no"]): c for c in jload(WORK / "channels.json", [])}
    data = jload(WORK / "screen.json", {})
    store = jload(WORK / "validate.json", {})
    sig = config_sig(cfg)
    max_dur = cfg.get("max_duration_min", 20) * 60
    only = parse_only(args.only) if args.only else None
    cands = [(k, v) for k, e in data.items() if not only or int(k) in only
             for v in e.get("videos", []) if v.get("signals", {}).get("brand_hits") and v["duration"] <= max_dur]
    cands = cands[: args.limit]
    print(f"  브랜드 언급 영상 {len(cands)}개를 검증합니다 (최대 {args.limit}개). 이 영상에서 high/medium 이 나와야 정상입니다.")
    rank_of = {"high": 3, "medium": 2, "low": 1}
    tally = {"high": 0, "medium": 0, "miss": 0}
    missed = []
    for key, v in cands:
        ch = channels[key]
        sk = f"{key}|{v['id']}"
        try:
            if store.get(sk, {}).get("_sig") != sig:
                frames, stamps, hi = download_frames(v["id"], v["duration"], cfg, focus_times(v, cfg))
                store[sk] = ask_vlm(client, cfg, ch["name"], v, ch, frames, stamps, hi)
                cleanup_tmp(cfg, v["id"])
                jdump(store, WORK / "validate.json")
        except Exception as e:  # noqa: BLE001
            print(f"  {key:>2} {ch['name']} · {v['id']}: ⚠ {type(e).__name__}: {str(e)[:160]}")
            continue
        worn = store[sk].get("worn", [])
        best = max((rank_of.get(w.get("confidence"), 0) for w in worn), default=0)
        tally["high" if best == 3 else "medium" if best == 2 else "miss"] += 1
        if best < 2:
            missed.append((ch["name"], v["url"]))
        print(f"  {key:>2} {ch['name']} · {v['title'][:30]}  {v['url']}\n       {fmt_worn(worn)}")
        print_details(worn)
    gt = cfg.get("ground_truth") or {}
    if gt:
        total = hit = fp = used = 0
        missed_gt = []
        for key, v in cands:
            sk = f"{key}|{v['id']}"
            if v["id"] not in gt or sk not in store:
                continue
            used += 1
            worn = store[sk].get("worn", [])
            for kw in gt[v["id"]]:
                total += 1
                got = any(kw in w.get("product", "") and w.get("confidence") in ("high", "medium") for w in worn)
                hit += got
                if not got:
                    missed_gt.append(f"{channels[key]['name']}-{kw}")
            fp += sum(1 for w in worn if w.get("confidence") in ("high", "medium")
                      and not any(kw in w.get("product", "") for kw in gt[v["id"]]))
        print(f"\n[정답표 채점] 정답표 적용 영상 {used}/{len(gt)}개 · 정답 상품 {total}개 중 {hit}개 검출 · 오탐 {fp}개"
              + (f"\n  놓친 것: {', '.join(missed_gt)}" if missed_gt else ""))
        if used < len(gt):
            print("  ※ 정답표의 영상ID가 위 출력된 링크의 ID와 같은지 확인하세요 (오타가 있으면 적용되지 않습니다)")
    n = sum(tally.values())
    print(f"\n[검증 결과] {n}개 영상 중 high {tally['high']} · medium {tally['medium']} · 못 잡음 {tally['miss']}"
          + (f"  → 시각 검출률 {(tally['high'] + tally['medium']) / n:.0%}" if n else ""))
    if missed:
        print("못 잡은 영상(직접 열어 협찬 옷이 정말 나오는지 확인하세요. 설명란에만 브랜드를 적고 옷은 안 나올 수도 있습니다):")
        for name, url in missed:
            print(f"  - {name}  {url}")
    jdump(store, WORK / "validate.json")
    print_usage(cfg)


# ───────────────────────── 5. report ─────────────────────────
STATUSES = ["확인됨(텍스트)", "확인됨(대본)", "확인됨(댓글)", "확인됨(영상)", "의심(수동확인)", "미확인", "업로드 없음", "채널 오류", "미처리"]
FILLS = {"확인됨": "C6EFCE", "의심": "FFEB9C", "미확인": "F8CBAD", "업로드": "D9D9D9", "채널": "D9D9D9", "미처리": "EDEDED"}


def cmd_report(args, cfg):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    channels = jload(WORK / "channels.json", [])
    data = jload(WORK / "screen.json", {})
    visual = jload(WORK / "visual.json", {})
    sig = config_sig(cfg)
    F = lambda **k: Font(name="Arial", size=10, **k)  # noqa: E731
    wb = Workbook()

    # --- 요약 시트
    ws = wb.active
    ws.title = "요약"
    ws["A1"] = "유튜브 협찬 착용 확인 결과"
    ws["A1"].font = Font(name="Arial", size=14, bold=True)
    ws["A2"] = f"생성: {datetime.now(KST):%Y-%m-%d %H:%M} · 자동 판정이므로 '의심/미확인'은 직접 확인하세요"
    ws["A2"].font = F(italic=True)
    n = len(channels)
    first, last = 14, 14 + n - 1
    for i, st in enumerate(STATUSES):
        r = 4 + i
        ws.cell(r, 1, st).font = F()
        c = ws.cell(r, 2, f'=COUNTIF($G${first}:$G${last},A{r})')
        c.font = F(bold=True)
    hdr = ["번호", "유튜버", "발송일", "협찬 상품", "채널 링크", "발송 후 영상 수", "판정", "근거", "근거 영상", "비고", "영상 분석 범위", "실제 채널명"]
    for j, h in enumerate(hdr, 1):
        c = ws.cell(first - 1, j, h)
        c.font = F(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F3864")
        c.alignment = Alignment(horizontal="center", vertical="center")
    rows_detail, outcomes = [], []
    for i, ch in enumerate(channels):
        r = first + i
        entry = data.get(str(ch["no"]))
        vis = {k: r for k, r in visual.get(str(ch["no"]), {}).items() if r.get("_sig") == sig}  # 옛 설정 결과 제외
        d = decide(entry, vis)
        outcomes.append((ch, d))
        v = d["video"]
        link = v["url"] if v else ""
        for w in (vis.get(v["id"], {}).get("worn", []) if v else []):  # 영상 근거(확인됨/의심)는 해당 장면 시점으로 바로 연결
            if w.get("at_sec") is not None and d["status"] in ("확인됨(영상)", "의심(수동확인)") and w.get("product") == d["basis"]:
                link = f"{v['url']}&t={w['at_sec']}s"
                break
        vals = [ch["no"], ch["name"], (ch["ship_date"] or "")[:10], " / ".join(it["name"] for it in ch["items"]),
                ch.get("url") or "", len((entry or {}).get("videos", [])), d["status"], d["basis"], link,
                "; ".join(x for x in [d["note"], "커플 채널" if ch.get("couple") else "",
                                      f"발송일 미기재→{cfg.get('default_since','2026-05-13')[:10]}부터 검색" if not ch["ship_date"] else "",
                                      ch.get("memo") if ch.get("memo") and "커플" not in ch["memo"] else "",
                                      str((cfg.get("manual_notes") or {}).get(ch["no"], ""))] if x),
                (f"{len(vis)}/{len((entry or {}).get('videos', []))}개 분석" if entry and not entry.get("error") else ""),
                ((entry or {}).get("channel") or {}).get("title", "") + (f" (구독자 {int(sub):,})" if (sub := ((entry or {}).get("channel") or {}).get("subscribers")) else "")]
        for j, val in enumerate(vals, 1):
            c = ws.cell(r, j, val)
            c.font = F()
            c.alignment = Alignment(vertical="top", wrap_text=j in (4, 10))
            if j in (5, 9) and val:
                c.hyperlink = val
                c.font = F(color="0563C1", underline="single")
        key = next((k for k in FILLS if d["status"].startswith(k)), None)
        if key:
            ws.cell(r, 7).fill = PatternFill("solid", fgColor=FILLS[key])
        for v2 in (entry or {}).get("videos", []):
            rows_detail.append((ch, v2, vis.get(v2["id"])))
    for col, w in zip("ABCDEFGHIJ", [6, 24, 12, 50, 36, 12, 16, 22, 40, 40]):
        ws.column_dimensions[col].width = w
    ws.column_dimensions["K"].width = 16
    ws.column_dimensions["L"].width = 30
    ws.column_dimensions["A"].width = 16
    ws.freeze_panes = ws.cell(first, 3)

    # --- 영상별 시트
    w2 = wb.create_sheet("영상별")
    h2 = ["번호", "유튜버", "영상 제목", "링크", "업로드일", "길이(분)", "브랜드 언급", "상품 키워드", "대본 브랜드", "광고 표기", "영상 분석", "분석 근거", "모델이 본 착장", "댓글 언급"]
    for j, h in enumerate(h2, 1):
        c = w2.cell(1, j, h)
        c.font = F(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F3864")
    for i, (ch, v, vr) in enumerate(rows_detail, 2):
        s = v["signals"]
        worn = [w for w in (vr or {}).get("worn", []) if w.get("confidence") != "none"]
        vals = [ch["no"], ch["name"], v["title"], v["url"], v["published"][:10], round(v["duration"] / 60, 1),
                ", ".join(s["brand_hits"]), ", ".join(s["product_hits"]), ", ".join(s["transcript_hits"]), ", ".join(s["disclosure"]),
                ("; ".join(f"{w.get('product')}({w.get('confidence')})" for w in worn) if vr is not None and worn else ("착용 미확인" if vr is not None else "")),
                "; ".join(w.get("evidence", "") for w in worn), (vr or {}).get("seen", ""),
                ", ".join(s.get("comment_hits", [])) + (f" / 시청자: {', '.join(s['comment_other_hits'])}" if s.get("comment_other_hits") else "")]
        for j, val in enumerate(vals, 1):
            c = w2.cell(i, j, val)
            c.font = F()
            if j == 4:
                c.hyperlink = val
                c.font = F(color="0563C1", underline="single")
    for col, w in zip("ABCDEFGHIJKLMN", [6, 22, 44, 44, 12, 9, 14, 18, 14, 14, 30, 50, 50, 26]):
        w2.column_dimensions[col].width = w
    w2.freeze_panes = "C2"

    OUT.mkdir(exist_ok=True)
    path = OUT / "seeding_report.xlsx"
    try:
        wb.save(path)
    except PermissionError:  # 엑셀에서 같은 파일을 열어두면 윈도우가 저장을 막음
        path = OUT / f"seeding_report_{datetime.now(KST):%m%d_%H%M%S}.xlsx"
        wb.save(path)
        print("  ⚠ seeding_report.xlsx 가 엑셀에서 열려 있어 새 이름으로 저장했습니다. (엑셀을 닫으면 원래 이름으로 저장됩니다)")
    print(f"[report] 저장 -> {path}")

    from collections import Counter

    cnt = Counter(d["status"] for _, d in outcomes)
    print("\n[판정 요약] " + " · ".join(f"{st} {cnt.get(st, 0)}" for st in STATUSES))
    for st in ("확인됨(텍스트)", "확인됨(대본)", "확인됨(댓글)", "확인됨(영상)", "의심(수동확인)", "업로드 없음", "채널 오류"):
        rows = [(ch, d) for ch, d in outcomes if d["status"] == st]
        if rows:
            print(f"\n  ■ {st} ({len(rows)}명)")
            for ch, d in rows:
                extra = d["basis"] if st.startswith(("확인됨", "의심")) else d["note"]
                url = f"  {d['video']['url']}" if d["video"] else ""
                print(f"    #{ch['no']:>2} {ch['name']}" + (f" — {str(extra)[:60]}" if extra else "") + url)


# ───────────────────────── main ─────────────────────────
def load_env(path=".env"):
    """같은 폴더의 .env 파일에서 KEY=값 을 읽어 환경변수로 등록 (이미 설정된 값이 있으면 유지)"""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def need_keys(order, cfg):
    missing = []
    if any(x in order for x in ("collect",)) and not os.environ.get("YOUTUBE_API_KEY"):
        missing.append("YOUTUBE_API_KEY  (https://console.cloud.google.com)")
    if "watch" in order or "validate" in order:
        if cfg.get("provider", "openai") == "anthropic":
            if not os.environ.get("ANTHROPIC_API_KEY"):
                missing.append("ANTHROPIC_API_KEY  (https://console.anthropic.com)")
        elif not os.environ.get("OPENAI_API_KEY"):
            missing.append("OPENAI_API_KEY  (https://platform.openai.com/api-keys)")
    if missing:
        sys.exit("⚠ .env 파일에 아래 키가 없습니다:\n  " + "\n  ".join(missing)
                 + "\n  → .env.example을 .env로 복사한 뒤 키를 채워주세요.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["parse", "collect", "screen", "watch", "validate", "report", "check", "all"])
    ap.add_argument("--xlsx", default="통합_문서1.xlsx", help="시딩 리스트 엑셀 경로")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--only", help="특정 번호만 처리 (예: 1,5,12 또는 범위 1-20,25) — 테스트·나눠 실행용")
    ap.add_argument("--transcripts", action="store_true", help="자막에서도 브랜드 언급 검색(느림)")
    ap.add_argument("--limit", type=int, default=10, help="validate 에서 검증할 영상 최대 개수")
    ap.add_argument("--skip-watch", action="store_true", help="all 실행 시 영상 분석 단계 건너뛰기")
    args = ap.parse_args()
    load_env()
    cfg = load_cfg(args.config)
    steps = {"parse": cmd_parse, "collect": cmd_collect, "screen": cmd_screen, "watch": cmd_watch, "validate": cmd_validate, "report": cmd_report, "check": cmd_check}
    order = ["parse", "collect", "screen"] + ([] if args.skip_watch else ["watch"]) + ["report"] if args.cmd == "all" else [args.cmd]
    need_keys(order, cfg)
    for name in order:
        print(f"\n=== {name} ===")
        steps[name](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
