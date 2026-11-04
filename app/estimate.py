"""영상 분석 대상 수, 예상 비용, 상한에 맞는 채널당 영상 수 계산 (OpenAI 호출 없음)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import seeding_checker as eng  # noqa: E402

DEFAULT_COST_PER_VIDEO = 0.03  # 1차 실행 실측: 영상 669개 약 $21.5
MAX_PER_CHANNEL = 30


def estimate(job_dir, cfg, cap_usd, cost_per_video=DEFAULT_COST_PER_VIDEO):
    job_dir = Path(job_dir)
    channels = {str(c["no"]): c for c in eng.jload(job_dir / "work" / "channels.json", [])}
    data = eng.jload(job_dir / "work" / "screen.json", {})
    visual = eng.jload(job_dir / "work" / "visual.json", {})
    wide = dict(cfg, max_visual_videos_per_channel=10 ** 6)
    todo_by_channel = {}
    for key, entry in data.items():
        if eng.decide(entry, {})["status"] not in ("미확인", "의심(수동확인)"):
            continue
        pool, _ = eng.pick_candidates(channels[key], entry, wide)
        seen = set(visual.get(key, {}))
        todo_by_channel[key] = [v["id"] for v in pool if v["id"] not in seen]

    def cost_videos(k):
        return sum(min(k, len(ids)) for ids in todo_by_channel.values())

    all_videos = cost_videos(MAX_PER_CHANNEL)
    k = 0
    for cand in range(1, MAX_PER_CHANNEL + 1):
        if cost_videos(cand) * cost_per_video <= cap_usd:
            k = cand
        else:
            break
    k = min(k, max((len(ids) for ids in todo_by_channel.values()), default=0))  # 후보 수보다 크게 잡지 않는다
    n = cost_videos(k)
    return {"channels": len(todo_by_channel), "videos_all": all_videos, "per_channel": k, "videos": n,
            "est_usd": round(n * cost_per_video, 2), "est_usd_all": round(all_videos * cost_per_video, 2),
            "cap_usd": cap_usd, "cost_per_video": cost_per_video}
