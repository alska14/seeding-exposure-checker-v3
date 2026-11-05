"""2차 재검증 (recheck): 1차에서 '미확인'으로 남은 채널에서 1차가 보지 못한 단서를 더 찾는다.

점검 5가지
  comments  댓글 재검토   : 아직 댓글을 받지 않은 영상(발송 후 90일 밖 등)의 댓글을 받아 브랜드 언급을 다시 찾는다 (YouTube 쿼터)
  fuzzy     표기 변형     : 제목·설명란·태그·댓글에서 브랜드명과 비슷한 표기('하늘마루' → '하늘마류' 등)를 찾는다 (무료)
  links     설명란 링크   : 설명란의 링크를 따라가 최종 주소·페이지에 브랜드가 있는지 본다 (무료)
  seen      분석 서술     : 이전 영상 분석이 적어둔 '본 착장' 글에 상품·브랜드 단어가 있는지 다시 찾는다 (무료)
  watch     안 본 영상    : 아직 분석하지 않은 영상을 비용 상한(--budget) 안에서 분석한다 (OpenAI 비용)

결과는 work/recheck.json 에 저장되고, screen 단계와 decide()가 읽어서 '의심(수동확인)' 후보로 올린다.
링크·표기 변형·분석 서술 단서는 확정이 아니라 사람이 확인할 후보다.
"""
import json
import re
import sys
import time
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import requests

STEPS = ["comments", "fuzzy", "links", "seen", "watch"]
COST_PER_VIDEO = 0.03  # 1차 실행 실측: 영상 669개 약 $21.5
MAX_PER_CHANNEL = 30
LINK_RE = re.compile(r"https?://[^\s<>\"')\]]+")
SKIP_HOSTS = ("youtube.com", "youtu.be", "instagram.com", "facebook.com", "twitter.com", "x.com", "tiktok.com", "discord.gg",
              "maps.app.goo.gl", "surl.amap.com", "temu.to", "goo.gl")  # 지도·해외 쇼핑 공유 링크는 브랜드 단서가 아님
# 상품 페이지·단축 링크는 브랜드명이 페이지에 나올 수 있어 먼저 따라간다
SHOP_HOSTS = ("musinsa.com", "musinsa.onelink.me", "s.zigzag.kr", "29cm.co.kr", "29cm.onelink.me", "a-bly.com", "link.coupang.com",
              "bit.ly", "naver.me", "litt.ly", "linktr.ee", "link.inpock.co.kr", "buly.kr", "me2.do", "vo.la", "tinyurl.com")
HANGUL = re.compile(r"[가-힣]")


# ───────────────────────── 공통 ─────────────────────────
def unconfirmed(sc, cfg):
    """현재 판정이 '미확인'인 채널: {번호: screen 항목}"""
    data = sc.jload(sc.WORK / "screen.json", {})
    visual = sc.jload(sc.WORK / "visual.json", {})
    sig = sc.config_sig(cfg)
    out = {}
    for key, entry in data.items():
        vis = {k: r for k, r in visual.get(key, {}).items() if r.get("_sig") == sig}
        if sc.decide(entry, vis)["status"] == "미확인":
            out[key] = entry
    return out


def all_statuses(sc, cfg):
    data = sc.jload(sc.WORK / "screen.json", {})
    visual = sc.jload(sc.WORK / "visual.json", {})
    sig = sc.config_sig(cfg)
    res = {}
    for key, entry in data.items():
        vis = {k: r for k, r in visual.get(key, {}).items() if r.get("_sig") == sig}
        res[key] = sc.decide(entry, vis)["status"]
    return res


def outfit_title(sc, cfg, video):
    return bool(sc.find_hits(video.get("title", ""), cfg.get("outfit_title_keywords", [])))


# ───────────────────────── 표기 변형(퍼지) 매칭 ─────────────────────────
def dist_le(a, b, maxd):
    """편집 거리가 maxd 이하인지 (삽입·삭제·교체 각 1)"""
    if abs(len(a) - len(b)) > maxd:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > maxd:
            return False
        prev = cur
    return prev[-1] <= maxd


def nfd(s):
    """한글 음절을 자모로 풀어 거리를 잰다"""
    import unicodedata
    return unicodedata.normalize("NFD", s)


def fuzzy_find(sc, text, keyword):
    """keyword 와 정확히 같지는 않지만 한 글자 차이로 비슷한 표기를 찾아 목록으로 돌려준다."""
    k = sc.norm(keyword)
    if len(k) < 4:
        return []
    out = []
    if HANGUL.search(k):  # 한글: 공백·기호를 지운 글에서 비슷한 글자 묶음을 찾는다
        t = sc.norm(text)
        L, seen = len(k), set()
        for i in range(L - 1):
            bg, start = k[i:i + 2], 0
            while True:
                p = t.find(bg, start)
                if p < 0:
                    break
                start = p + 1
                for ln in (L - 1, L, L + 1):
                    for off in (-1, 0, 1):
                        s = p - i + off
                        if s < 0 or s + ln > len(t):
                            continue
                        w = t[s:s + ln]
                        # 정확한 표기의 일부·확장은 변형이 아님. 거리는 자모 단위로 2 이하(주→쥬, 망→만 처럼 자모 하나씩 다른 표기)
                        if w not in k and k not in w and w not in seen and dist_le(nfd(w), nfd(k), 2):
                            seen.add(w)
                            out.append(w)
    else:  # 영문: 단어 단위로 비교. 첫 글자가 같고 한 글자 차이인 단어만 (engagement 같은 흔한 단어 제외)
        for tok in set(re.findall(r"[A-Za-z0-9_]+", text.lower())):
            if tok != k and tok[:1] == k[:1] and len(tok) >= 4 and dist_le(tok, k, 1):
                out.append(tok)
    return out


def video_texts(v):
    cm = v.get("comments") or {}
    return " ".join([v.get("title", ""), v.get("description", ""), " ".join(v.get("tags") or []),
                     " ".join(cm.get("owner", [])), " ".join(cm.get("other", []))])


def step_fuzzy(sc, cfg, targets):
    brand = cfg.get("brand_keywords", [])
    found = {}
    for key, entry in targets.items():
        for v in entry.get("videos", []):
            text = video_texts(v)
            hits = []
            for kw in brand:
                if sc.find_hits(text, [kw]):  # 정확한 표기는 1차가 이미 잡았다
                    continue
                hits += [f"{kw}≈{w}" for w in fuzzy_find(sc, text, kw)]
            if hits:
                found.setdefault(key, {})[v["id"]] = sorted(set(hits))[:6]
    return found


# ───────────────────────── 분석 서술 재검색 ─────────────────────────
def step_seen(sc, cfg, targets):
    kws = list(cfg.get("brand_keywords", [])) + list(cfg.get("product_keywords", [])) + list(cfg.get("recheck_seen_keywords", []))
    visual = sc.jload(sc.WORK / "visual.json", {})
    found = {}
    for key in targets:
        for vid, r in visual.get(key, {}).items():
            hits = sc.find_hits(r.get("seen", ""), kws)  # '본 착장'만 본다. 근거(evidence)에는 "~가 안 보임" 같은 부정문에도 상품명이 나온다
            if hits:
                found.setdefault(key, {})[vid] = sorted(set(hits))[:6]
    return found


# ───────────────────────── 설명란 링크 추적 ─────────────────────────
def extract_links(text):
    urls = []
    for u in LINK_RE.findall(text or ""):
        u = u.rstrip(".,;:!?)]}'\"")
        host = re.sub(r"^https?://(www\.)?", "", u).split("/")[0].lower()
        if not any(host == h or host.endswith("." + h) for h in SKIP_HOSTS) and u not in urls:
            urls.append(u)
    return urls


def is_shop(url):
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower()
    return any(host == h or host.endswith("." + h) for h in SHOP_HOSTS)


def collect_links(targets):
    """{url: [(채널번호, 영상ID), ...]}"""
    out = {}
    for key, entry in targets.items():
        for v in entry.get("videos", []):
            for u in extract_links(v.get("description", "")):
                out.setdefault(u, []).append((key, v["id"]))
    return out


def resolve_link(url):
    r = requests.get(url, timeout=8, allow_redirects=True, stream=True, headers={"User-Agent": "Mozilla/5.0 (seeding-checker recheck)"})
    body = b""
    for chunk in r.iter_content(8192):
        body += chunk
        if len(body) >= 150_000:
            break
    r.close()
    return r.url, body.decode(r.encoding or "utf-8", errors="ignore")


def step_links(sc, cfg, targets, max_links, cache):
    brand = cfg.get("brand_keywords", [])
    links = collect_links(targets)
    order = sorted(links, key=lambda u: (0 if is_shop(u) else 1, -len(links[u])))  # 쇼핑·단축 링크 먼저, 같은 종류면 반복이 많은 것
    todo = [u for u in order if u not in cache][:max_links]

    def work(u):
        try:
            final, body = resolve_link(u)
            return u, {"final": final[:300], "hits": sc.find_hits(final + " " + body, brand), "ok": True}
        except Exception as e:  # noqa: BLE001  (연결 실패·시간 초과 등은 단서 없음으로 기록)
            return u, {"final": "", "hits": [], "ok": False, "err": type(e).__name__}

    with ThreadPoolExecutor(max_workers=8) as ex:
        for u, res in ex.map(work, todo):
            cache[u] = res
    found = {}
    for u, where in links.items():
        res = cache.get(u)
        if res and res.get("hits"):
            for key, vid in where:
                found.setdefault(key, {})[vid] = sorted(set(found.get(key, {}).get(vid, []) + res["hits"]))
    return found, len(links), len(todo)


# ───────────────────────── 댓글 재검토 ─────────────────────────
def yt_retry(sc, endpoint, params, tries=3):
    """네트워크 오류(시간 초과 등)는 잠깐 쉬고 다시 시도. API가 거절한 오류(RuntimeError)는 그대로 올린다"""
    for i in range(tries):
        try:
            return sc.yt(endpoint, **params)
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def fetch_comment_pages(sc, video_id, owner_id, pages):
    """주인 댓글·답글과 시청자 댓글을 pages 페이지까지 받는다. 쿼터 초과면 None"""
    owner, other, token = [], [], None
    for _ in range(max(1, pages)):
        params = dict(part="snippet,replies", videoId=video_id, order="relevance", maxResults=100, textFormat="plainText")
        if token:
            params["pageToken"] = token
        try:
            j = yt_retry(sc, "commentThreads", params)
        except requests.RequestException:  # 연결 실패·시간 초과: 이 영상은 건너뛰고(댓글 미수집으로 남김) 다음 실행에서 다시 받는다
            return None
        except RuntimeError as e:
            if "quota" in str(e).lower():
                sc.QUOTA["out"] = True
                return None
            if not (owner or other):
                return {"owner": [], "other": [], "disabled": True}  # 댓글 막힘 등
            break
        for it in j.get("items", []):
            top = it["snippet"]["topLevelComment"]["snippet"]
            text = top.get("textOriginal") or top.get("textDisplay", "")
            (owner if (top.get("authorChannelId") or {}).get("value") == owner_id else other).append(text)
            for rp in (it.get("replies") or {}).get("comments", []):
                sn = rp["snippet"]
                if (sn.get("authorChannelId") or {}).get("value") == owner_id:
                    owner.append(sn.get("textOriginal") or sn.get("textDisplay", ""))
        token = j.get("nextPageToken")
        if not token:
            break
    return {"owner": [t[:600] for t in owner][:30], "other": [t[:200] for t in other][:40]}


def comment_targets(sc, cfg, targets, videos_json, quota):
    """댓글을 아직 받지 않은 영상. 착장 제목 영상을 먼저, quota 개수까지"""
    window = cfg.get("window_days")
    todo = []
    for key, entry in targets.items():
        vids = (videos_json.get(key) or {}).get("videos", [])
        for v in vids:
            if v.get("comments") is None:
                todo.append((0 if outfit_title(sc, cfg, v) else 1, v["published"], key, v["id"]))
    todo.sort()
    return [(k, vid) for _, _, k, vid in todo][:quota], len(todo)


def step_comments(sc, args, cfg, targets):
    videos = sc.jload(sc.WORK / "videos.json", {})
    pick, total = comment_targets(sc, cfg, targets, videos, args.comment_quota)
    print(f"  댓글을 아직 안 받은 영상 {total}개 중 {len(pick)}개 (페이지 {args.comment_pages}, 예상 쿼터 약 {len(pick) * args.comment_pages})")
    if args.dry_run or not pick:
        return set()
    by_id = {(k, v["id"]): v for k, e in videos.items() for v in e.get("videos", [])}
    changed = set()

    def work(item):
        key, vid = item
        return item, fetch_comment_pages(sc, vid, videos[key]["channel"]["channel_id"], args.comment_pages)

    done = skipped = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for (key, vid), res in ex.map(work, pick):
            if res is not None:
                by_id[(key, vid)]["comments"] = res
                changed.add(key)
            else:
                skipped += 1
            done += 1
            if done % 200 == 0:  # 중간에 멈춰도 받은 댓글은 남도록 주기적으로 저장
                sc.jdump(videos, sc.WORK / "videos.json")
                print(f"  … {done}/{len(pick)}")
    sc.jdump(videos, sc.WORK / "videos.json")
    if skipped and not sc.QUOTA["out"]:
        print(f"  ⚠ 네트워크 오류로 {skipped}개 영상은 건너뛰었습니다. 같은 명령을 다시 실행하면 그 영상만 이어서 받습니다.")
    if sc.QUOTA["out"]:
        print("  ⚠ YouTube API 일일 쿼터를 다 써서 일부 댓글을 못 가져왔습니다. 내일 같은 명령을 다시 실행하면 이어서 받습니다.")
    return changed


# ───────────────────────── 안 본 영상 재확인 ─────────────────────────
def watch_plan(sc, cfg, targets, budget, cost=COST_PER_VIDEO):
    """비용 상한 안에서 채널당 분석할 영상 수 k 와 대상 채널, 영상 수를 계산한다."""
    channels = {str(c["no"]): c for c in sc.jload(sc.WORK / "channels.json", [])}
    visual = sc.jload(sc.WORK / "visual.json", {})
    sig = sc.config_sig(cfg)
    wide = dict(cfg, max_visual_videos_per_channel=10 ** 6, visual_window_days=cfg.get("window_days") or cfg.get("visual_window_days"))
    pools = {}
    for key, entry in targets.items():
        pool, _ = sc.pick_candidates(channels[key], entry, wide)
        pools[key] = [v["id"] for v in pool if visual.get(key, {}).get(v["id"], {}).get("_sig") != sig]

    def n(k):
        return sum(min(k, len(p)) for p in pools.values())

    k = 0
    for cand in range(1, MAX_PER_CHANNEL + 1):
        if n(cand) * cost <= (budget or 0):
            k = cand
        else:
            break
    k = min(k, max((len(p) for p in pools.values()), default=0))
    ids = [key for key, p in pools.items() if p]
    return {"k": k, "videos": n(k), "videos_all": n(MAX_PER_CHANNEL), "cost": round(n(k) * cost, 2),
            "cost_all": round(n(MAX_PER_CHANNEL) * cost, 2), "channels": len(ids), "ids": ids, "window": wide["visual_window_days"]}


# ───────────────────────── 실행 ─────────────────────────
def replace_kind(store, kind, found, keys):
    """이번에 점검한 채널(keys)의 kind 단서만 새 결과로 바꾼다. 점검 대상이 아니게 된 채널(이미 의심으로 올라간 곳 등)의 이전 단서는 유지"""
    for key in keys:
        for vid in (store.get(key) or {}):
            store[key][vid].pop(kind, None)
    for key, vids in found.items():
        for vid, hits in vids.items():
            store.setdefault(key, {}).setdefault(vid, {})[kind] = hits


def cmd_recheck(sc, args, cfg):
    import os
    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    bad = [s for s in steps if s not in STEPS]
    if bad:
        sys.exit(f"⚠ 알 수 없는 점검: {', '.join(bad)} (가능: {', '.join(STEPS)})")
    do_watch = "watch" in steps and args.budget and args.budget > 0
    if not args.dry_run:
        if "comments" in steps and not os.environ.get("YOUTUBE_API_KEY"):
            sys.exit("⚠ 댓글 재검토에는 YOUTUBE_API_KEY가 필요합니다 (.env 확인). 키 없이 하려면 --steps fuzzy,links,seen")
        if do_watch and not os.environ.get("OPENAI_API_KEY"):
            sys.exit("⚠ 영상 재분석에는 OPENAI_API_KEY가 필요합니다 (.env 확인)")
    targets = unconfirmed(sc, cfg)
    if not targets:
        sys.exit("미확인 채널이 없습니다. 먼저 all 또는 screen 단계를 실행하세요.")
    base_path = sc.WORK / "recheck_base.json"
    if not args.dry_run and not base_path.exists():  # 처음 한 번만: 재검증 전 판정을 저장해 전후를 비교한다
        sc.jdump(all_statuses(sc, cfg), base_path)
    print(f"  재검증 대상: 미확인 {len(targets)}개 채널 ({'점검만 계산, 저장 없음' if args.dry_run else '실행'})")
    store = sc.jload(sc.WORK / "recheck.json", {})
    cache = store.get("_link_cache", {})

    changed_channels = set()
    if "comments" in steps:
        print("\n[1] 댓글 재검토")
        changed_channels = step_comments(sc, args, cfg, targets)
        if changed_channels and not args.dry_run:
            sc.cmd_screen(Namespace(only=",".join(sorted(changed_channels)), transcripts=False), cfg)  # 새 댓글로 신호 다시 계산
            targets = unconfirmed(sc, cfg)
            print(f"  → 댓글 재검토 후 미확인 {len(targets)}개 채널")
    videos = sc.jload(sc.WORK / "videos.json", {})
    targets_v = {k: videos.get(k, e) for k, e in targets.items()}  # 댓글이 반영된 원본 영상 정보(설명란·댓글)
    if "fuzzy" in steps:
        print("\n[2] 표기 변형(퍼지) 매칭")
        found = step_fuzzy(sc, cfg, targets_v)
        print(f"  비슷한 표기가 나온 채널 {len(found)}개, 영상 {sum(len(v) for v in found.values())}개")
        for key, vids in list(found.items())[:10]:
            print(f"    #{key}: " + ", ".join(sorted({h for hs in vids.values() for h in hs})[:4]))
        if not args.dry_run:
            replace_kind(store, "fuzzy", found, targets_v)
    if "links" in steps:
        print("\n[3] 설명란 링크 추적")
        links = collect_links(targets_v)
        todo = [u for u in links if u not in cache]
        print(f"  서로 다른 링크 {len(links)}개 (이미 확인 {len(links) - len(todo)}개, 이번에 따라갈 것 {min(len(todo), args.max_links)}개, 최대 {args.max_links}개)")
        if not args.dry_run:
            found, total, n = step_links(sc, cfg, targets_v, args.max_links, cache)
            store["_link_cache"] = cache
            replace_kind(store, "link", found, targets_v)
            print(f"  링크 {n}개를 따라가 봤고, 브랜드가 나온 채널 {len(found)}개")
    if "seen" in steps:
        print("\n[4] 기존 분석 서술 재검색")
        found = step_seen(sc, cfg, targets)
        print(f"  분석 서술에 상품·브랜드 단어가 있는 채널 {len(found)}개, 영상 {sum(len(v) for v in found.values())}개")
        if not args.dry_run:
            replace_kind(store, "seen", found, targets)
    if not args.dry_run:
        sc.jdump(store, sc.WORK / "recheck.json")
        sc.cmd_screen(Namespace(only=None, transcripts=False), cfg)  # 새 단서를 판정에 반영
    targets = unconfirmed(sc, cfg) if not args.dry_run else targets
    if "watch" in steps:
        print("\n[5] 안 본 영상 재확인")
        plan = watch_plan(sc, cfg, targets, args.budget)
        print(f"  분석 안 한 영상 {plan['videos_all']}개 (최대 {MAX_PER_CHANNEL}개/채널 기준 전부 분석 시 약 ${plan['cost_all']}, 영상당 ${COST_PER_VIDEO} 추정, 범위 {plan['window']}일)")
        if not args.budget:
            print("  --budget 을 주지 않아 영상 분석은 건너뜁니다. 예: --budget 5")
        elif plan["k"] < 1:
            print(f"  상한 ${args.budget}로는 영상을 한 개도 분석할 수 없습니다")
        else:
            print(f"  상한 ${args.budget} → 채널당 {plan['k']}개, 영상 {plan['videos']}개, 예상 약 ${plan['cost']}")
            if not args.dry_run and plan["ids"]:
                cfg2 = dict(cfg, max_visual_videos_per_channel=plan["k"], visual_window_days=plan["window"])
                sc.cmd_watch(Namespace(only=",".join(plan["ids"])), cfg2)
    if args.dry_run:
        print("\n(--dry-run: 아무것도 호출하거나 저장하지 않았습니다)")
        return
    sc.cmd_report(args, cfg)
    base = sc.jload(base_path, {})
    now = all_statuses(sc, cfg)
    changed = [(k, base.get(k), s) for k, s in now.items() if base.get(k) and base.get(k) != s]
    n0 = sum(1 for s in base.values() if s == "미확인")
    n1 = sum(1 for s in now.values() if s == "미확인")
    print(f"\n[2차 재검증 결과] 미확인 {n0} → {n1}  (바뀐 채널 {len(changed)}개)")
    names = {str(c["no"]): c["name"] for c in sc.jload(sc.WORK / "channels.json", [])}
    for k, a, b in changed:
        print(f"    #{k} {names.get(k, '')}: {a} → {b}")
