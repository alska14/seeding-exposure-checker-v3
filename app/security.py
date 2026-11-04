"""토큰·키·업로드 검증. 키 값은 이 모듈 밖에서 저장하지 않는다."""
import hashlib
import io
import re
import secrets
import time
from collections import defaultdict

MAX_XLSX = 5 * 1024 * 1024
MAX_IMG = 5 * 1024 * 1024
MAX_IMAGES = 30

_KEY_PATTERNS = [
    re.compile(r"(?i)([?&]key=)[^&\s\"']+"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}"),
]


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def redact(text: str, secrets_list=()) -> str:
    """키 값과 흔한 키 형태를 *** 로 바꾼다."""
    for s in secrets_list:
        if s and len(s) >= 6:
            text = text.replace(s, "***")
    text = _KEY_PATTERNS[0].sub(r"\1***", text)
    text = _KEY_PATTERNS[1].sub("***", text)
    text = _KEY_PATTERNS[2].sub("***", text)
    text = _KEY_PATTERNS[3].sub(r"\1***", text)
    return text


def clean_key(value):
    """키 문자열 점검. 비어 있으면 None, 이상하면 ValueError."""
    if value is None or value == "":
        return None
    if not (16 <= len(value) <= 300) or re.search(r"[\s\x00-\x1f\x7f]", value):
        raise ValueError("API 키 형식이 올바르지 않습니다")
    return value


def check_xlsx(data: bytes) -> None:
    if len(data) > MAX_XLSX:
        raise ValueError("엑셀 파일이 5MB를 넘습니다")
    if data[:2] != b"PK":
        raise ValueError("xlsx 형식 파일이 아닙니다")
    from openpyxl import load_workbook
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True)
        wb.active.max_row  # noqa: B018
    except Exception as e:  # noqa: BLE001
        raise ValueError("엑셀 파일을 읽을 수 없습니다") from e


_MAGIC = [(b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF8", ".gif")]


def image_ext(data: bytes):
    """이미지 내용으로 확장자를 정한다. 이미지가 아니면 None."""
    if len(data) > MAX_IMG:
        return None
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


class FailLimiter:
    """잘못된 토큰 요청을 IP별로 제한한다."""

    def __init__(self, limit=10, window=60):
        self.limit, self.window, self.hits = limit, window, defaultdict(list)

    def blocked(self, ip):
        now = time.time()
        self.hits[ip] = [t for t in self.hits[ip] if now - t < self.window]
        return len(self.hits[ip]) >= self.limit

    def fail(self, ip):
        self.hits[ip].append(time.time())
