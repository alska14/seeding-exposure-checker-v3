"""배포 환경 진단: 이 서버(클라우드 IP)에서 영상 다운로드와 프레임 추출이 되는지 확인한다.

고정된 공개 영상 하나만 쓰고 사용자 입력은 받지 않는다. DIAG_TOKEN 환경변수가 있을 때만 켜진다.
"""
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TEST_VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # 19초 공개 영상


def run(cmd, timeout):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, (p.stdout + p.stderr)[-600:]


def check_download():
    out = {"video": TEST_VIDEO, "ffmpeg": bool(shutil.which("ffmpeg"))}
    t0 = time.time()
    tmp = Path(tempfile.mkdtemp(prefix="diag_"))
    try:
        code, log = run([sys.executable, "-m", "yt_dlp", "-f", "worst[height<=360]/worst", "--no-playlist", "--max-filesize", "20M",
                         "-o", str(tmp / "v.%(ext)s"), TEST_VIDEO], 120)
        files = list(tmp.glob("v.*"))
        out["download_ok"] = code == 0 and bool(files)
        out["download_log"] = log if not out["download_ok"] else ""
        if out["download_ok"]:
            out["download_bytes"] = files[0].stat().st_size
            code, log = run(["ffmpeg", "-y", "-i", str(files[0]), "-frames:v", "1", str(tmp / "f.jpg")], 60)
            out["frame_ok"] = code == 0 and (tmp / "f.jpg").exists()
            out["frame_log"] = log if not out["frame_ok"] else ""
        out["ok"] = bool(out.get("download_ok") and out.get("frame_ok"))
    except Exception as e:  # noqa: BLE001
        out.update(ok=False, error=f"{type(e).__name__}: {e}"[:300])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out["seconds"] = round(time.time() - t0, 1)
    return out
